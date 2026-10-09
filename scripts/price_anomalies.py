"""重大价格波动的独立留证；只记录事实，不因涨跌自动删除真实价格。"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
from pathlib import Path
import re
import argparse
from collections import Counter

from failure_evidence import _atomic_json, capture_redacted_snapshot, redact_text, sanitize_url

_WRITE_FAILURES = 0


def classify_change(old_price, new_price, *, old_currency, currency) -> dict | None:
    old_currency, currency = str(old_currency or "").upper(), str(currency or "").upper()
    if not currency or old_currency != currency:
        return None
    try:
        old, new = Decimal(str(old_price)), Decimal(str(new_price))
        if not old.is_finite() or not new.is_finite() or old <= 0 or new < 0:
            return None
        if not all(math.isfinite(float(x)) for x in (old, new)):
            return None
        def floating_boundary(boundary):
            # 仅补偿浮点输入至多4个ULP的表示误差；字符串/Decimal输入严格按原值比较。
            if not isinstance(old_price, float) and not isinstance(new_price, float):
                return False
            value = float(boundary)
            return math.isfinite(value) and abs(new - boundary) <= Decimal(str(math.ulp(value) * 4))
        near_up, near_down = floating_boundary(old * 2), floating_boundary(old / 2)
        if new < old * 2 and new > old / 2 and not near_up and not near_down:
            return None
        return {"old_price": float(old), "new_price": float(new), "currency": currency,
                "change_percent": 100.0 if near_up else -50.0 if near_down else float((new - old) / old * 100), "ratio": float(new / old),
                "direction": "up" if new >= old * 2 or near_up else "down", "valid_quote": new > 0}
    except (InvalidOperation, TypeError, ValueError, OverflowError):
        return None


def _source() -> dict:
    fields = (("run_id", "GITHUB_RUN_ID", r"[1-9]\d{0,19}", "local"),
              ("run_attempt", "GITHUB_RUN_ATTEMPT", r"[1-9]\d{0,5}", "1"),
              ("head_sha", "GITHUB_SHA", r"[a-fA-F0-9]{40,64}", "unknown"))
    return {key: os.environ[env] if re.fullmatch(pattern, os.environ.get(env, "")) else fallback
            for key, env, pattern, fallback in fields}


def default_output_dir() -> Path:
    return Path(os.environ.get("PRICE_ARTIFACTS_DIR", str(Path(__file__).resolve().parent / "price_artifacts")))


async def _record_price_change(*, baseline: dict | None, observation: dict, page=None,
                              output_dir: Path | None = None, evidence_source=None,
                              verification: dict | None = None) -> Path | None:
    """每个触发观测先写 JSON；截图失败、额度耗尽均可见，不向失败目录串写。"""
    try:
        candidate = Decimal(str(observation.get("price")))
        if not candidate.is_finite() or candidate < 0:
            print(json.dumps({"event": "invalid_price", "price_preserved": False, "eligible_for_publication": False}))
            return None
    except (InvalidOperation, TypeError, ValueError):
        print(json.dumps({"event": "invalid_price", "eligible_for_publication": False}))
        return None
    if not baseline:
        return None
    change = classify_change(baseline.get("price"), observation.get("price"),
                             old_currency=baseline.get("currency"), currency=observation.get("currency"))
    if change is None:
        return None
    source = _source()
    identity = {**source, **{k: observation.get(k) for k in
        ("platform", "country", "product", "asin", "listing_id", "url", "observed_at", "price", "currency", "observation_source")}}
    event_id = hashlib.sha256(json.dumps(identity, sort_keys=True, default=str).encode()).hexdigest()[:24]
    root = Path(output_dir or default_output_dir()).resolve()
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"price_change_{event_id}.json"
    if path.exists():
        return path
    document = {"schema_version": 1, "event_type": "price_change", "event_id": event_id,
                **change, "source": source, "old_observed_at": baseline.get("observed_at"),
                "new_observed_at": observation.get("observed_at"),
                "baseline_identity_precision": baseline.get("identity_precision", "unspecified"),
                "baseline_source_file": Path(str(baseline["source_file"])).name if re.fullmatch(r"[A-Za-z0-9_.-]+\.csv", Path(str(baseline.get("source_file", ""))).name) else None,
                "platform": redact_text(observation.get("platform"), 40),
                "country": redact_text(observation.get("country"), 12),
                "product": redact_text(observation.get("product"), 120),
                "asin": observation.get("asin") if re.fullmatch(r"[A-Z0-9]{10}", str(observation.get("asin", ""))) else None,
                "listing_id": str(observation.get("listing_id")) if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(observation.get("listing_id", ""))) else None,
                "requested_url": sanitize_url(observation.get("url")),
                "source_page_url": sanitize_url(observation.get("source_page_url")),
                "source_query": redact_text(observation.get("source_query"), 100) if observation.get("source_query") else None,
                "source_page": observation.get("source_page") if type(observation.get("source_page")) is int else None,
                "observation_source": redact_text(observation.get("observation_source"), 80),
                "screenshot_page_source": redact_text(evidence_source or observation.get("observation_source"), 80),
                "ingestion_status": observation.get("ingestion_status", "pending") if change["valid_quote"] else "rejected",
                "validation_state": observation.get("validation_state", "validated" if observation.get("ingestion_status") == "accepted" else "unvalidated") if change["valid_quote"] else "invalid_price",
                "ingestion_status_scope": "collection_validation_not_remote_publication",
                "verification": _safe_verification(verification),
                "screenshot": {"status": "not_attempted", "file": None}}
    _atomic_json(path, document)
    document["screenshot"] = await capture_redacted_snapshot(
        page, output_dir=root, event_type="price_change", event_id=event_id,
        screenshot_limit=_screenshot_limit(), timeout_seconds=4.0,
    )
    _atomic_json(path, document)
    return path


def _screenshot_limit():
    try:
        return max(0, int(os.environ.get("PRICE_SCREENSHOT_LIMIT", "0"))) or None
    except ValueError:
        return None


def _safe_verification(value):
    if not isinstance(value, dict):
        return None
    result = {key: redact_text(value[key], 100) for key in ("status", "currency", "observed_at", "error_type", "guard") if key in value}
    result["url"] = sanitize_url(value.get("url"))
    for key in ("price", "http_status"):
        number = value.get(key)
        if isinstance(number, (int, float)) and math.isfinite(number):
            result[key] = number
    return result


def _write_warning(exc, output_dir=None):
    global _WRITE_FAILURES
    _WRITE_FAILURES += 1
    print(json.dumps({"event": "price_evidence_write_failed", "error_type": type(exc).__name__,
                      "count": _WRITE_FAILURES, "price_preserved": True}, ensure_ascii=False))
    # 单个事件文件写失败时尽量留下跨进程计数；整个文件系统不可写时结构化日志仍在。
    try:
        root = Path(output_dir or default_output_dir())
        root.mkdir(parents=True, exist_ok=True)
        _atomic_json(root / "write-failures.json", {"source": _source(), "write_failures": _WRITE_FAILURES})
    except Exception:
        pass


async def record_price_change(**kwargs) -> Path | None:
    try:
        return await _record_price_change(**kwargs)
    except Exception as exc:
        _write_warning(exc, kwargs.get("output_dir"))
        return None


async def attach_price_verification(path: Path | None, *, page=None, verification: dict,
                                    evidence_source="pdp_verification") -> None:
    if path is None:
        return
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
        document["verification"] = _safe_verification(verification)
        document["screenshot_page_source"] = redact_text(evidence_source, 80)
        _atomic_json(Path(path), document)
        document["screenshot"] = await capture_redacted_snapshot(
            page, output_dir=Path(path).parent, event_type="price_change", event_id=document["event_id"],
            screenshot_limit=_screenshot_limit(), timeout_seconds=4.0,
        )
        _atomic_json(Path(path), document)
    except Exception as exc:
        _write_warning(exc, Path(path).parent if path else None)


def summarize(output_dir: Path | None = None, *, write=True) -> dict:
    root = Path(output_dir or default_output_dir())
    events = []
    unreadable = 0
    for path in sorted(root.glob("price_change_*.json")):
        try:
            event = json.loads(path.read_text(encoding="utf-8"))
            events.append({**{key: event.get(key) for key in ("product", "platform", "country", "old_price", "new_price", "currency", "change_percent", "new_observed_at", "ingestion_status", "observation_source")},
                           "event_file": path.name, "screenshot": event.get("screenshot", {})})
        except (ValueError, OSError):
            unreadable += 1
    statuses = Counter(e["screenshot"].get("status", "unknown") for e in events)
    previous_failures = 0
    try:
        previous = json.loads((root / "write-failures.json").read_text(encoding="utf-8"))
        if previous.get("source") == _source():
            previous_failures = max(0, int(previous.get("write_failures", 0)))
    except (OSError, ValueError, TypeError):
        pass
    failures = max(_WRITE_FAILURES, previous_failures)
    result = {"events": len(events), "screenshots_saved": statuses.get("saved", 0),
              "screenshot_statuses": dict(statuses), "write_failures_this_process": _WRITE_FAILURES,
              "write_failures": failures,
              "unreadable_events": unreadable, "evidence_complete": statuses.get("saved", 0) == len(events) and not unreadable and not failures,
              "status": "no_alerts" if not events and not unreadable and not failures else "review_required",
              "scope": "成功落盘的事件；其他进程写盘失败另见结构化日志和monitor质量摘要", "items": events}
    if write:
        try:
            root.mkdir(parents=True, exist_ok=True)
            _atomic_json(root / "summary.json", result)
        except OSError as exc:
            _write_warning(exc, root)
            result["evidence_complete"] = False
            result["write_failures"] = max(failures, _WRITE_FAILURES)
    return result


def _update_price_change_status(path: Path | None, *, ingestion_status: str, reason=None) -> None:
    if path is None:
        return
    if ingestion_status not in {"pending", "accepted", "rejected", "not_published"}:
        raise ValueError("未知采集验收状态")
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    document["ingestion_status"] = ingestion_status
    document["validation_state"] = "validated" if ingestion_status == "accepted" else "rejected" if ingestion_status == "rejected" else "unvalidated"
    if document.get("valid_quote") is False and ingestion_status == "accepted":
        raise ValueError("零价候选不能标为已验收有效价格")
    if reason is not None:
        document["ingestion_reason"] = redact_text(reason)
    _atomic_json(Path(path), document)


def update_price_change_status(path: Path | None, *, ingestion_status: str, reason=None) -> None:
    try:
        _update_price_change_status(path, ingestion_status=ingestion_status, reason=reason)
    except Exception as exc:
        _write_warning(exc, Path(path).parent if path else None)


def write_github_summary(path: Path, summary: dict) -> None:
    def cell(value):
        return str(value if value is not None else "—").replace("|", "\\|").replace("\n", " ")
    lines = ["\n### 价格波动留证\n",
             f"已留存事件 {summary['events']}；取得截图 {summary['screenshots_saved']}；不可读取事件 {summary['unreadable_events']}；写盘失败 {summary['write_failures']}。\n",
             "未触发时无需截图；已触发但未取得截图的原因见下表。写盘失败另有 price_evidence_write_failed 日志。\n",
             "采集验收只表示价格门禁结果；是否已发布以独立发布任务为准。\n",
             "| 型号 | 旧价 | 新价 | 币种 | 变化% | 采集验收 | 截图状态 | 事件文件 |",
             "|---|---:|---:|---|---:|---|---|---|"]
    for event in summary["items"][:100]:
        lines.append("| " + " | ".join(cell(x) for x in (
            event.get("product"), event.get("old_price"), event.get("new_price"), event.get("currency"),
            event.get("change_percent"), event.get("ingestion_status"), event.get("screenshot", {}).get("status"), event["event_file"])) + " |")
    if len(summary["items"]) > 100:
        lines.append(f"\n另有 {len(summary['items']) - 100} 条事件，详见价格附件中的 summary.json。")
    try:
        with Path(path).open("a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    except OSError as exc:
        _write_warning(exc)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="汇总价格波动事件与截图完整性")
    parser.add_argument("command", choices=("summary",))
    parser.add_argument("--output-dir", type=Path, default=default_output_dir())
    parser.add_argument("--github-summary", type=Path)
    args = parser.parse_args()
    summary = summarize(args.output_dir)
    if args.github_summary:
        write_github_summary(args.github_summary, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
