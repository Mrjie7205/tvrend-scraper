"""Amazon 日更交接门禁：只接收本次运行验收成功的目录，不借用 checkout 旧文件。"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

COUNTRIES = ("de", "gb", "it", "es")
MIN_ROWS = 100
PRICE_COLUMNS = ("price_local", "price_eur", "price_hint_eur")
REQUIRED_COLUMNS = {"platform", "country", "scraped_at", "url", "raw_text", "currency", *PRICE_COLUMNS}


def now() -> str:
    return datetime.now(UTC).isoformat()


def parse_time(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("invalid_timestamp_type")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp_without_timezone")
    return parsed.astimezone(UTC)


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def is_priced_row(row: dict, expected_currency: str) -> bool:
    """目录允许暂时无报价商品；无价不可被算成报价，异常数值不可退化成无价。"""
    raw_values = [row.get(column) for column in PRICE_COLUMNS]
    currency = row.get("currency")
    if any(not isinstance(value, str) for value in raw_values) or not isinstance(currency, str):
        raise ValueError("malformed_price_columns")
    values = [value.strip() for value in raw_values]
    if currency not in ("", expected_currency):
        raise ValueError("wrong_currency")
    if not any(values):
        return False
    # 采集器的正式 CSV 对有报价商品同时写入本币价、EUR 价与 EUR 提示价。
    if not all(values) or currency != expected_currency:
        raise ValueError("incomplete_price_or_currency")
    for value in values:
        try:
            number = Decimal(value)
        except InvalidOperation as error:
            raise ValueError("invalid_price") from error
        if not number.is_finite() or number <= 0:
            raise ValueError("invalid_price")
    return True


def prepare(path: Path, country: str, run_id: str, attempt: str, head_sha: str, catalog_dir: Path) -> dict:
    context = {
        "schemaVersion": 1, "country": country, "runId": run_id,
        "runAttempt": attempt, "headSha": head_sha, "startedAt": now(),
        "existingCatalogs": {
            candidate.name: hashlib.sha256(candidate.read_bytes()).hexdigest()
            for candidate in catalog_dir.glob(f"amazon_{country}_*.csv")
            if candidate.is_file() and not candidate.is_symlink()
        },
    }
    write_json(path, context)
    return context


def inspect_csv(path: Path, country: str, started_at: str, finished_at: str) -> dict:
    """校验实际内容和抓取时间；不能仅因当天文件名存在便判定本轮成功。"""
    if path.is_symlink() or not path.is_file():
        raise ValueError("missing_or_linked_csv")
    start, end = parse_time(started_at), parse_time(finished_at)
    if end < start:
        raise ValueError("invalid_time_window")
    # 主采集器仅记录秒；开始边界允许同秒截断，stage 另用运行前哈希拒绝旧文件。
    start = start.replace(microsecond=0)
    currency = "GBP" if country == "gb" else "EUR"
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not REQUIRED_COLUMNS.issubset(reader.fieldnames or []):
            raise ValueError("missing_columns")
        rows = 0
        priced_rows = 0
        for row in reader:
            row_country = row.get("country")
            if row.get("platform") != "Amazon" or not isinstance(row_country, str) or row_country.lower() != country:
                raise ValueError("wrong_market")
            stamp = parse_time(row.get("scraped_at", ""))
            if not start <= stamp <= end:
                raise ValueError("stale_or_future_scrape")
            expected_name = f"amazon_{country}_{stamp:%Y%m%d}.csv"
            if path.name != expected_name:
                raise ValueError("wrong_file_date")
            if not row.get("url") or not row.get("raw_text"):
                raise ValueError("invalid_product_row")
            priced_rows += int(is_priced_row(row, currency))
            rows += 1
    if rows < MIN_ROWS:
        raise ValueError("too_few_rows")
    return {"file": path.name, "rowCount": rows, "pricedRowCount": priced_rows,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def stage(context_path: Path, catalog_dir: Path, out_dir: Path, outcome: str) -> dict:
    context = json.loads(context_path.read_text(encoding="utf-8"))
    out_dir.mkdir(parents=True, exist_ok=True)
    if any(out_dir.iterdir()):
        raise ValueError("stage_directory_not_empty")
    # 运行前哈希只在本地判定新旧，不能把历史目录清单加入公共交接物。
    result = {key: value for key, value in context.items() if key != "existingCatalogs"}
    result.update({"finishedAt": now(), "scrapeOutcome": outcome, "status": "failed"})
    if outcome != "success":
        result["reasonCode"] = "scrape_not_successful"
    else:
        country = context["country"]
        accepted = []
        for candidate in sorted(catalog_dir.glob(f"amazon_{country}_*.csv")):
            try:
                info = inspect_csv(candidate, country, context["startedAt"], result["finishedAt"])
                if context.get("existingCatalogs", {}).get(candidate.name) == info["sha256"]:
                    continue
            except (ValueError, OSError, UnicodeError, csv.Error):
                continue
            accepted.append((candidate, info))
        if len(accepted) != 1:
            result["reasonCode"] = "fresh_catalog_not_unique" if accepted else "no_valid_fresh_catalog"
        else:
            candidate, info = accepted[0]
            shutil.copyfile(candidate, out_dir / candidate.name)
            result.update(info)
            result["status"] = "validated"
    # 仅保留固定字段与错误代码，不收集 HTML、cookie 或运行环境。
    write_json(out_dir / "manifest.json", result)
    return result


def collect(
    artifacts_dir: Path, catalog_dir: Path, report_dir: Path,
    run_id: str, attempt: str, head_sha: str, scrape_result: str,
) -> dict:
    result = {"runId": run_id, "runAttempt": attempt, "headSha": head_sha,
              "scrapeResult": scrape_result, "markets": {}, "publishFiles": []}
    catalog_dir.mkdir(parents=True, exist_ok=True)
    for country in COUNTRIES:
        folder = artifacts_dir / f"amazon-result-{country}"
        try:
            manifest_path = folder / "manifest.json"
            if folder.is_symlink() or manifest_path.is_symlink():
                raise ValueError("linked_artifact")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict):
                raise ValueError("invalid_manifest_type")
            expected = {"schemaVersion": 1, "country": country, "runId": run_id,
                        "runAttempt": attempt, "headSha": head_sha,
                        "scrapeOutcome": "success", "status": "validated"}
            if any(manifest.get(key) != value for key, value in expected.items()):
                raise ValueError("artifact_identity_or_status_mismatch")
            filename = manifest.get("file", "")
            if not isinstance(filename, str) or Path(filename).name != filename or "\\" in filename:
                raise ValueError("invalid_filename")
            source = folder / filename
            info = inspect_csv(source, country, manifest["startedAt"], manifest["finishedAt"])
            if any(manifest.get(key) != value for key, value in info.items()):
                raise ValueError("artifact_content_mismatch")
            # 只有来自本轮独立 artifact 且完成全部验证的目录能覆盖工作树。
            shutil.copyfile(source, catalog_dir / filename)
            result["markets"][country] = {"status": "validated", **info}
            result["publishFiles"].append(filename)
        except (ValueError, OSError, UnicodeError, csv.Error, KeyError, TypeError):
            result["markets"][country] = {"status": "failed", "reasonCode": "missing_or_invalid_artifact"}
    result["complete"] = len(result["publishFiles"]) == len(COUNTRIES) and scrape_result == "success"
    report_dir.mkdir(parents=True, exist_ok=True)
    write_json(report_dir / "report.json", result)
    (report_dir / "publish-files.txt").write_text(
        "".join(name + "\n" for name in result["publishFiles"]), encoding="utf-8")
    (report_dir / "publish-countries.txt").write_text(
        "".join(country + "\n" for country in COUNTRIES if result["markets"][country]["status"] == "validated"),
        encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--country", choices=COUNTRIES, required=True)
    prep.add_argument("--context", type=Path, required=True)
    prep.add_argument("--catalog", type=Path, required=True)
    stage_parser = sub.add_parser("stage")
    stage_parser.add_argument("--context", type=Path, required=True)
    stage_parser.add_argument("--catalog", type=Path, required=True)
    stage_parser.add_argument("--out", type=Path, required=True)
    stage_parser.add_argument("--outcome", required=True)
    collector = sub.add_parser("collect")
    collector.add_argument("--artifacts", type=Path, required=True)
    collector.add_argument("--catalog", type=Path, required=True)
    collector.add_argument("--report", type=Path, required=True)
    collector.add_argument("--scrape-result", required=True)
    gate = sub.add_parser("gate")
    gate.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "gate":
        report = json.loads(args.report.read_text(encoding="utf-8"))
        print(json.dumps(report, ensure_ascii=False))
        return 0 if report.get("complete") is True else 1
    if args.command == "stage":
        result = stage(args.context, args.catalog, args.out, args.outcome)
    else:
        # run/attempt 必须来自 GitHub 的本次上下文，禁止从旧 manifest 反推。
        identity = (os.environ["GITHUB_RUN_ID"], os.environ["GITHUB_RUN_ATTEMPT"], os.environ["GITHUB_SHA"])
        if args.command == "prepare":
            result = prepare(args.context, args.country, *identity, args.catalog)
        else:
            result = collect(args.artifacts, args.catalog, args.report, *identity, args.scrape_result)
    print(json.dumps(result, ensure_ascii=False))
    # 汇总先保存成功国家，再由独立 gate 步骤呈现全局失败，不能让失败阻断有效数据提交。
    return 1 if args.command == "stage" and result["status"] != "validated" else 0


if __name__ == "__main__":
    raise SystemExit(main())
