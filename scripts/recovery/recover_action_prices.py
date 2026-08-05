"""从被取消的 GitHub Action 日志恢复已明确抓到的 Boulanger/Currys 价格。

只接受紧邻商品标识的 ``[ok/batch]`` 类目快照记录；并发 PDP 的 ``[ok]`` 日志
没有重复输出商品名，无法可靠归属，因此仅写入排除审计，不进入 raw/prices.csv。

默认仅审计，明确传入 ``--write`` 才会修改 raw/prices.csv。
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import subprocess
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RAW_COLUMNS = (
    "Date", "Time", "Brand", "Product Name", "Country", "Platform", "Price",
    "Currency", "Page Title", "Status", "Price_Trend",
)
RUNS = (
    {
        "date": "2026-08-01",
        "run_id": "30690114835",
        "head": "e48ae90e204919a1d1752a2485987e551e472ef4",
        "expected": {"Boulanger": 374, "Currys": 421},
    },
    {
        "date": "2026-08-02",
        "run_id": "30738270556",
        "head": "06bfcf871f3bb28e93f5d4ee79fd299110d82295",
        "expected": {"Boulanger": 373, "Currys": 421},
    },
    {
        "date": "2026-08-03",
        "run_id": "30798390218",
        "head": "00bb29ba17e508a11ad5c496c4bd9b1945b7b96d",
        "expected": {"Boulanger": 375, "Currys": 408},
    },
    {
        "date": "2026-08-04",
        "run_id": "30889236108",
        "head": "8bc3c94030ae0b0caccf6bf40c306d30d07f46f3",
        "expected": {"Boulanger": 382, "Currys": 426},
    },
)

LINE_TS = re.compile(r"(?P<ts>20\d{2}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)")
START = re.compile(r"→ \[(?P<country>[A-Z]{2})\] (?P<model>.+?) \((?P<platform>Boulanger|Currys)\)")
BATCH_OK = re.compile(
    r"\[ok/batch\]\s+(?P<currency>[A-Z]{3})\s+(?P<price>\d+(?:\.\d+)?)\s+\((?P<trend>[^)]+)\)"
)
PDP_OK = re.compile(r"\[ok\]\s+[A-Z]{3}\s+\d+(?:\.\d+)?\s+\([^)]+\)")


def _run_bytes(args: list[str]) -> bytes:
    completed = subprocess.run(args, cwd=ROOT, capture_output=True, check=False)
    if completed.returncode:
        error = completed.stderr.decode("utf-8", errors="replace")
        raise RuntimeError(f"命令失败({completed.returncode}): {' '.join(args)}\n{error}")
    return completed.stdout


def _action_log(repo: str, run_id: str) -> tuple[str, str]:
    payload = _run_bytes(["gh", "run", "view", run_id, "--repo", repo, "--log"])
    return payload.decode("utf-8", errors="replace"), hashlib.sha256(payload).hexdigest()


def _brand_map(head: str) -> dict[tuple[str, str, str], str]:
    payload = _run_bytes(["git", "show", f"{head}:mapping/channel_links.csv"])
    rows = csv.DictReader(io.StringIO(payload.decode("utf-8-sig", errors="replace")))
    candidates: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for row in rows:
        if str(row.get("active", "true")).strip().lower() not in {"true", "1", "yes"}:
            continue
        model = (row.get("sku") or row.get("model") or "").strip()
        key = (model, (row.get("country") or "").strip().upper(), (row.get("platform") or "").strip())
        candidates[key].add((row.get("brand") or "").strip())
    ambiguous = {key: values for key, values in candidates.items() if len(values) != 1}
    if ambiguous:
        preview = list(ambiguous.items())[:5]
        raise RuntimeError(f"{head[:8]} 商品到品牌映射不唯一: {preview}")
    return {key: next(iter(values)) for key, values in candidates.items()}


def _parse_run(spec: dict, repo: str) -> tuple[list[dict], list[dict], dict]:
    log, log_sha = _action_log(repo, spec["run_id"])
    brands = _brand_map(spec["head"])
    recovered: list[dict] = []
    excluded: list[dict] = []
    pending: dict | None = None

    for line_number, line in enumerate(log.splitlines(), start=1):
        start = START.search(line)
        if start:
            pending = start.groupdict()
            pending["line"] = line_number
            continue

        batch = BATCH_OK.search(line)
        if batch:
            if pending is None:
                raise RuntimeError(f"run {spec['run_id']} line {line_number}: batch 价格前没有商品标识")
            price = batch.groupdict()
            timestamp = LINE_TS.search(line)
            key = (pending["model"], pending["country"], pending["platform"])
            brand = brands.get(key)
            if not brand:
                raise RuntimeError(f"run {spec['run_id']} 无法映射品牌: {key}")
            recovered.append({
                "RunId": spec["run_id"],
                "HeadSha": spec["head"],
                "LogLine": line_number,
                "LogTimestampUTC": timestamp.group("ts") if timestamp else "",
                "Date": spec["date"],
                "Time": _time_from_timestamp(timestamp.group("ts") if timestamp else ""),
                "Brand": brand,
                "Product Name": pending["model"],
                "Country": pending["country"],
                "Platform": pending["platform"],
                "Price": float(price["price"]),
                "Currency": price["currency"],
                "Page Title": "Batch category snapshot",
                "Status": "Success",
                "LoggedPriceTrend": price["trend"],
                "RecoveryMethod": "github_action_log_exact_batch_pair",
            })
            pending = None
            continue

        if PDP_OK.search(line):
            timestamp = LINE_TS.search(line)
            excluded.append({
                "RunId": spec["run_id"],
                "HeadSha": spec["head"],
                "LogLine": line_number,
                "LogTimestampUTC": timestamp.group("ts") if timestamp else "",
                "Reason": "并发 PDP 成功日志未重复输出商品名，无法可靠归属",
                "RawLogLine": line,
            })

    actual = Counter(row["Platform"] for row in recovered)
    if dict(actual) != spec["expected"]:
        raise RuntimeError(
            f"run {spec['run_id']} 解析数量不符: actual={dict(actual)}, expected={spec['expected']}"
        )
    return recovered, excluded, {"sha256": log_sha, "lineCount": len(log.splitlines())}


def _time_from_timestamp(value: str) -> str:
    if not value:
        return "00:00:00"
    return value.split("T", 1)[1].split(".", 1)[0].rstrip("Z")


def _load_raw(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _apply_recomputed_trends(existing: list[dict], recovered: list[dict]) -> None:
    recovery_dates = sorted({row["Date"] for row in recovered})
    first_date = recovery_dates[0]
    history: dict[tuple[str, str, str], float] = {}
    for row in sorted(existing, key=lambda item: (item.get("Date", ""), item.get("Time", ""))):
        if row.get("Date", "") >= first_date or row.get("Status") != "Success":
            continue
        try:
            history[(row["Product Name"], row["Country"], row["Platform"])] = float(row["Price"])
        except (KeyError, TypeError, ValueError):
            continue

    for date in recovery_dates:
        day_rows = [row for row in recovered if row["Date"] == date]
        updates: list[tuple[tuple[str, str, str], float]] = []
        for row in day_rows:
            key = (row["Product Name"], row["Country"], row["Platform"])
            old = history.get(key)
            new = float(row["Price"])
            if old is None:
                row["Price_Trend"] = "新上线"
            elif new < old:
                row["Price_Trend"] = "降价"
            elif new > old:
                row["Price_Trend"] = "涨价"
            else:
                row["Price_Trend"] = "持平"
            updates.append((key, new))
        for key, price in updates:
            history[key] = price


def _write_csv(path: Path, rows: list[dict], columns: list[str] | tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows({column: row.get(column, "") for column in columns} for row in rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default="Mrjie7205/tvrend-scraper")
    parser.add_argument("--write", action="store_true", help="通过全部审计后写入 raw/prices.csv")
    args = parser.parse_args()

    recovered: list[dict] = []
    excluded: list[dict] = []
    log_meta: dict[str, dict] = {}
    for spec in RUNS:
        rows, skipped, meta = _parse_run(spec, args.repo)
        recovered.extend(rows)
        excluded.extend(skipped)
        log_meta[spec["run_id"]] = meta

    raw_path = ROOT / "raw" / "prices.csv"
    existing = _load_raw(raw_path)
    target_dates = {spec["date"] for spec in RUNS}
    conflicts = [
        row for row in existing
        if row.get("Date") in target_dates and row.get("Platform") in {"Boulanger", "Currys"}
    ]
    if conflicts:
        raise RuntimeError(f"raw/prices.csv 已存在目标日期的 B/C 数据 {len(conflicts)} 行，拒绝重复恢复")

    _apply_recomputed_trends(existing, recovered)
    audit_dir = ROOT / "raw" / "recovery"
    audit_path = audit_dir / "action_log_recovery_20260801_20260804.csv"
    excluded_path = audit_dir / "action_log_recovery_20260801_20260804_excluded.csv"
    manifest_path = audit_dir / "action_log_recovery_20260801_20260804_manifest.json"
    audit_columns = list(recovered[0].keys())
    _write_csv(audit_path, recovered, audit_columns)
    _write_csv(excluded_path, excluded, list(excluded[0].keys()) if excluded else ["Reason"])

    manifest = {
        "createdAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "method": "github_action_stdout_exact_batch_pair_only",
        "targetDates": sorted(target_dates),
        "recoveredRows": len(recovered),
        "excludedAmbiguousPdpRows": len(excluded),
        "countsByDatePlatform": {
            f"{date}|{platform}": count
            for (date, platform), count in sorted(Counter((r["Date"], r["Platform"]) for r in recovered).items())
        },
        "sourceRuns": [
            {**spec, "log": log_meta[spec["run_id"]]}
            for spec in RUNS
        ],
        "auditCsvSha256": hashlib.sha256(audit_path.read_bytes()).hexdigest(),
        "excludedCsvSha256": hashlib.sha256(excluded_path.read_bytes()).hexdigest(),
        "trendPolicy": "按日期顺序，以恢复日前最近成功价为基线；同日记录均对比前一日基线",
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    if not args.write:
        print("[dry-run] 审计文件已生成；未修改 raw/prices.csv。通过 --write 明确写入。")
        return 0

    combined = existing + [{column: row.get(column, "") for column in RAW_COLUMNS} for row in recovered]
    _write_csv(raw_path, combined, RAW_COLUMNS)
    print(f"[write] 恢复 {len(recovered)} 行 → {raw_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
