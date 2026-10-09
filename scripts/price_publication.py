"""交接本轮价格观测，并在最新远端版本上合并发布，避免旧快照覆盖。"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
import re
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping, Sequence

from monitor_prices.prices_io import DEFAULT_KEEP_DAYS, PRICES_COLUMNS


CHANNEL_SCOPE = {
    "Boulanger": ("FR", "EUR"),
    "Currys": ("GB", "GBP"),
    "Elkjop": ("NO", "NOK"),
}
PRICE_PATH = "raw/prices.csv"
PAYLOAD_NAME = "prices.csv"
MANIFEST_NAME = "manifest.json"
SCHEMA_VERSION = 1


class PublicationError(RuntimeError):
    """输入或远端状态不满足发布要求；保留原产物供检查和重试。"""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class RunIdentity:
    repository: str
    run_id: str
    run_attempt: str
    source_sha: str
    source_ref: str
    channel: str

    def validate(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repository):
            raise PublicationError("repository 格式不正确")
        if not re.fullmatch(r"[1-9][0-9]*", self.run_id):
            raise PublicationError("run_id 缺失或不正确")
        if not re.fullmatch(r"[1-9][0-9]*", self.run_attempt):
            raise PublicationError("run_attempt 缺失或不正确")
        if not re.fullmatch(r"[0-9a-f]{40}", self.source_sha):
            raise PublicationError("source_sha 必须是完整提交 SHA")
        if (
            not re.fullmatch(r"refs/heads/[A-Za-z0-9][A-Za-z0-9_./-]*", self.source_ref)
            or ".." in self.source_ref
            or "//" in self.source_ref
            or self.source_ref.endswith(("/", ".", ".lock"))
        ):
            raise PublicationError("source_ref 必须是明确的安全分支引用")
        if self.channel not in CHANNEL_SCOPE:
            raise PublicationError("本轮必须明确指定一个受支持渠道")

    @classmethod
    def from_environment(cls, env: Mapping[str, str]) -> "RunIdentity":
        identity = cls(
            repository=env.get("GITHUB_REPOSITORY", ""),
            run_id=env.get("GITHUB_RUN_ID", ""),
            run_attempt=env.get("GITHUB_RUN_ATTEMPT", ""),
            source_sha=env.get("GITHUB_SHA", ""),
            source_ref=env.get("GITHUB_REF", ""),
            channel=env.get("CHANNELS", ""),
        )
        identity.validate()
        return identity


def row_key(row: Mapping[str, object]) -> tuple[str, ...]:
    # 与私库实际去重口径一致，同一 SKU/时间但不同报价仍是不同观测。
    return tuple(str(row.get(column, "")).strip() for column in PRICES_COLUMNS)


def _rows_bytes(rows: Sequence[Mapping[str, object]], *, line_ending: str = "\r\n") -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=PRICES_COLUMNS, lineterminator=line_ending)
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8-sig")


def _read_csv(payload: bytes) -> list[dict[str, str]]:
    try:
        reader = csv.DictReader(io.StringIO(payload.decode("utf-8-sig"), newline=""), strict=True)
        if reader.fieldnames != list(PRICES_COLUMNS):
            raise PublicationError("价格文件的 11 列名称或顺序不符")
        rows = list(reader)
        if any(None in row or any(value is None for value in row.values()) for row in rows):
            raise PublicationError("价格文件存在列数不符的记录")
        return rows
    except (UnicodeError, csv.Error) as exc:
        raise PublicationError(f"价格文件无法完整读取: {exc}") from exc


def _date(value: str) -> date | None:
    try:
        parsed = date.fromisoformat(value)
        return parsed if parsed.isoformat() == value else None
    except ValueError:
        return None


def _validate_observations(rows: Sequence[Mapping[str, object]], identity: RunIdentity,
                           captured_at: datetime) -> None:
    if not rows:
        raise PublicationError("本轮没有有效价格记录，不能生成成功发布产物")
    country, currency = CHANNEL_SCOPE[identity.channel]
    for index, row in enumerate(rows, 1):
        if set(row) != set(PRICES_COLUMNS):
            raise PublicationError(f"第 {index} 行字段不符")
        if (row.get("Platform"), row.get("Country"), row.get("Currency")) != (identity.channel, country, currency):
            raise PublicationError(f"第 {index} 行渠道、国家或币种与运行身份不符")
        if row.get("Status") != "Success" or not str(row.get("Product Name", "")).strip() or not str(row.get("Brand", "")).strip():
            raise PublicationError(f"第 {index} 行不是完整成功记录")
        observed_date = _date(str(row.get("Date", "")))
        observed_time = str(row.get("Time", ""))
        if observed_date is None or abs((observed_date - captured_at.date()).days) > 1:
            raise PublicationError(f"第 {index} 行日期不属于本轮抓取，拒绝旧快照或出界日期")
        try:
            if not re.fullmatch(r"\d{2}:\d{2}:\d{2}", observed_time):
                raise ValueError("Time 格式")
            datetime.strptime(observed_time, "%H:%M:%S")
            price = float(str(row.get("Price", "")))
            if not math.isfinite(price) or price <= 0:
                raise ValueError("Price 必须是有限正数")
        except (TypeError, ValueError) as exc:
            raise PublicationError(f"第 {index} 行时间或价格不合法") from exc


def write_run_artifact(rows: Sequence[Mapping[str, object]], destination: Path,
                       identity: RunIdentity, *, max_skus: int = 0,
                       now: datetime | None = None) -> dict:
    """只写本轮传入观测；manifest 最后落盘，不能拿旧主表充当本轮结果。"""
    identity.validate()
    now = now or _utc_now()
    if type(max_skus) is not int or max_skus < 0:
        raise PublicationError("max_skus 必须是非负整数")
    normalized = [{column: str(row.get(column, "")) for column in PRICES_COLUMNS} for row in rows]
    _validate_observations(normalized, identity, now)
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise PublicationError("产物目录非空，拒绝覆盖前一次运行；请使用新的运行目录")
    payload = _rows_bytes(normalized)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        **asdict(identity),
        "country": CHANNEL_SCOPE[identity.channel][0],
        "columns": list(PRICES_COLUMNS),
        "row_count": len(normalized),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "captured_at": now.isoformat(),
        "max_skus": max_skus,
        "smoke_test": max_skus != 0,
    }
    payload_temp = destination / f"{PAYLOAD_NAME}.tmp"
    payload_temp.write_bytes(payload)
    os.replace(payload_temp, destination / PAYLOAD_NAME)
    manifest_temp = destination / f"{MANIFEST_NAME}.tmp"
    manifest_temp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(manifest_temp, destination / MANIFEST_NAME)
    return manifest


def validate_artifact(directory: Path, expected: RunIdentity, *,
                      now: datetime | None = None) -> tuple[dict, list[dict[str, str]]]:
    expected.validate()
    now = now or _utc_now()
    directory = Path(directory)
    try:
        manifest_path, payload_path = directory / MANIFEST_NAME, directory / PAYLOAD_NAME
        if manifest_path.is_symlink() or payload_path.is_symlink():
            raise PublicationError("发布产物不能使用符号链接")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        payload = payload_path.read_bytes()
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PublicationError(f"本轮产物缺失或损坏: {exc}") from exc
    if not isinstance(manifest, dict):
        raise PublicationError("manifest 必须是对象")
    for name, value in asdict(expected).items():
        if manifest.get(name) != value:
            raise PublicationError(f"产物身份不符: {name}")
    if manifest.get("schema_version") != SCHEMA_VERSION or manifest.get("columns") != list(PRICES_COLUMNS):
        raise PublicationError("产物字段版本不符")
    if manifest.get("country") != CHANNEL_SCOPE[expected.channel][0]:
        raise PublicationError("产物国家不符")
    if manifest.get("sha256") != hashlib.sha256(payload).hexdigest():
        raise PublicationError("产物内容校验和不符")
    if type(manifest.get("max_skus")) is not int or manifest["max_skus"] < 0 or type(manifest.get("smoke_test")) is not bool:
        raise PublicationError("产物烟测标识不合法")
    if manifest["smoke_test"] != (manifest["max_skus"] != 0):
        raise PublicationError("产物烟测标识矛盾")
    try:
        captured_at = datetime.fromisoformat(manifest["captured_at"])
        if captured_at.utcoffset() != timedelta(0):
            raise ValueError("captured_at 必须是 UTC 时间")
    except (KeyError, TypeError, ValueError) as exc:
        raise PublicationError("产物生成时间不合法") from exc
    if captured_at > now + timedelta(minutes=5):
        raise PublicationError("产物生成时间在未来")
    rows = _read_csv(payload)
    if type(manifest.get("row_count")) is not int or manifest["row_count"] != len(rows):
        raise PublicationError("产物记录数量与清单不符")
    _validate_observations(rows, expected, captured_at)
    return manifest, rows


def merge_observations(current: Sequence[dict[str, str]], incoming: Sequence[dict[str, str]], *,
                       today: date, keep_days: int = DEFAULT_KEEP_DAYS) -> tuple[list[dict[str, str]], dict]:
    """最新远端记录与本轮观测做整行并集，再统一执行既有滚动窗口。"""
    if keep_days < 14:
        raise PublicationError("保留窗口不能短于 14 天")
    if not incoming:
        raise PublicationError("没有待发布观测")
    parsed = [(_date(row["Date"]), row) for row in [*current, *incoming]]
    dates = [observed for observed, _ in parsed if observed is not None]
    if any(observed > today + timedelta(days=1) for observed in dates):
        raise PublicationError("存在未来日期，拒绝用异常日期推进历史裁剪边界")
    if not dates:
        raise PublicationError("没有可解析的日期")
    cutoff = max(dates) - timedelta(days=keep_days)
    if any(_date(row["Date"]) is None or _date(row["Date"]) < cutoff for row in incoming):
        raise PublicationError("本轮产物已超出保留窗口，需独立历史恢复，不能标为正常发布")
    current_keys = {row_key(row) for row in current}
    merged_by_key: dict[tuple[str, ...], dict[str, str]] = {}
    trimmed = 0
    for observed, row in parsed:
        if observed is not None and observed < cutoff:
            trimmed += 1
            continue
        merged_by_key.setdefault(row_key(row), row)
    # 稳定排序保留同一观测时间的原顺序，不改变既有的同刻后行优先语义。
    merged = sorted(merged_by_key.values(), key=lambda row: (row["Date"], row["Time"]))
    merged_keys = set(merged_by_key)
    required = {row_key(row) for row in current if _date(row["Date"]) is None or _date(row["Date"]) >= cutoff}
    if not required.issubset(merged_keys) or not {row_key(row) for row in incoming}.issubset(merged_keys):
        raise PublicationError("合并完整性检查失败")
    return merged, {
        "before_rows": len(current),
        "incoming_rows": len(incoming),
        "added_unique_rows": len({row_key(row) for row in incoming} - current_keys),
        "after_rows": len(merged),
        "trimmed_rows": trimmed,
        "deduplicated_rows": len(current) + len(incoming) - trimmed - len(merged),
        "cutoff": cutoff.isoformat(),
    }


def _git(repo: Path, *arguments: str, input_bytes: bytes | None = None,
         env: Mapping[str, str] | None = None, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments], input=input_bytes,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env={**os.environ, **(env or {})},
    )
    if check and result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise PublicationError(f"Git 操作失败 ({arguments[0]}): {detail}")
    return result


def _git_text(repo: Path, *arguments: str) -> str:
    return _git(repo, *arguments).stdout.decode("utf-8").strip()


def _validate_origin(repo: Path, repository: str) -> None:
    allowed = {
        f"https://github.com/{repository}".lower(),
        f"https://github.com/{repository}.git".lower(),
        f"git@github.com:{repository}.git".lower(),
    }
    # 单独配置的 pushurl 也必须核对，不能只验证抓取地址而向另一仓库写入。
    for options in (("--all",), ("--push", "--all")):
        origins = _git_text(repo, "remote", "get-url", *options, "origin").splitlines()
        if len(origins) != 1 or origins[0].lower() not in allowed:
            raise PublicationError("origin 与运行仓库身份不符，拒绝向未知目标发布")


def _fetch_main(repo: Path) -> str:
    _git(repo, "fetch", "--no-tags", "origin", "refs/heads/main")
    sha = _git_text(repo, "rev-parse", "FETCH_HEAD")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise PublicationError("无法确定远端 main 的完整 SHA")
    return sha


def _commit_candidate(repo: Path, base_sha: str, payload: bytes, identity: RunIdentity) -> str:
    # 临时 index 仅生成只改价格文件的提交，不切分支、不清理工作区、不改用户暂存区。
    with tempfile.TemporaryDirectory(prefix="tvrend-price-index-") as scratch:
        Path(scratch).resolve().relative_to(Path(tempfile.gettempdir()).resolve())
        env = {"GIT_INDEX_FILE": str(Path(scratch) / "index")}
        _git(repo, "read-tree", base_sha, env=env)
        blob = _git(repo, "hash-object", "-w", "--stdin", input_bytes=payload).stdout.decode().strip()
        _git(repo, "update-index", "--add", "--cacheinfo", f"100644,{blob},{PRICE_PATH}", env=env)
        tree = _git(repo, "write-tree", env=env).stdout.decode().strip()
        return _git_text(
            repo, "-c", "user.name=tvrend-scraper bot", "-c", "user.email=actions@github.com",
            "-c", "commit.gpgsign=false", "commit-tree", tree, "-p", base_sha,
            "-m", f"chore: publish {identity.channel.lower()} prices {identity.run_id}/{identity.run_attempt} [skip ci]",
        )


def publish_artifact(directory: Path, repo: Path, expected: RunIdentity, *,
                     branch: str = "main", attempts: int = 5,
                     retry_delay: float = 3.0, now: datetime | None = None) -> dict:
    now = now or _utc_now()
    manifest, incoming = validate_artifact(directory, expected, now=now)
    if branch != "main" or expected.source_ref != "refs/heads/main":
        raise PublicationError("手动分支只保留产物，不能发布至 main")
    if manifest["smoke_test"] or manifest["max_skus"] != 0:
        raise PublicationError("烟测产物不得更新正式价格")
    if not 1 <= attempts <= 10 or retry_delay < 0:
        raise PublicationError("发布重试参数不合法")
    repo = Path(repo).resolve()
    if _git_text(repo, "status", "--porcelain"):
        raise PublicationError("发布工作区有未提交改动，拒绝运行")
    _validate_origin(repo, expected.repository)
    base_sha = _fetch_main(repo)
    for attempt in range(1, attempts + 1):
        current_payload = _git(repo, "show", f"{base_sha}:{PRICE_PATH}").stdout
        current = _read_csv(current_payload)
        merged, report = merge_observations(current, incoming, today=now.date())
        # 临时 index 不经过工作树 clean 过滤；沿用远端主表表头的换行格式，
        # 避免仅因 runner 的换行设置产生整份主表无业务意义的差异。
        line_ending = "\r\n" if current_payload.split(b"\n", 1)[0].endswith(b"\r") else "\n"
        payload = _rows_bytes(merged, line_ending=line_ending)
        report.update({"run_id": expected.run_id, "run_attempt": expected.run_attempt, "base_sha": base_sha, "attempt": attempt})
        if payload == current_payload:
            report.update({"status": "already_present", "published_sha": base_sha})
            return report
        candidate = _commit_candidate(repo, base_sha, payload, expected)
        pushed = _git(repo, "push", "origin", f"{candidate}:refs/heads/main", check=False)
        latest_sha = _fetch_main(repo)
        if pushed.returncode == 0:
            published = _read_csv(_git(repo, "show", f"{latest_sha}:{PRICE_PATH}").stdout)
            if not {row_key(row) for row in merged}.issubset({row_key(row) for row in published}):
                raise PublicationError("推送后读回发现记录缺失，不能标为发布成功")
            report.update({"status": "published", "published_sha": candidate, "verified_sha": latest_sha})
            return report
        if latest_sha == base_sha:
            raise PublicationError("推送失败且远端没有竞争更新，停止重试，请检查权限或分支规则")
        print(f"[publish] 第 {attempt} 次推送遇到分支更新，读取最新版本重新合并")
        base_sha = latest_sha
        if attempt < attempts:
            time.sleep(retry_delay * attempt)
    raise PublicationError("发布竞争重试耗尽；本轮产物保留，尚未发布")


def main() -> int:
    parser = argparse.ArgumentParser(description="校验本轮价格产物，并在最新 main 上合并发布")
    subparsers = parser.add_subparsers(dest="command", required=True)
    publish = subparsers.add_parser("publish")
    publish.add_argument("--artifact-dir", required=True, type=Path)
    publish.add_argument("--repo-dir", required=True, type=Path)
    publish.add_argument("--repository", required=True)
    publish.add_argument("--expected-run-id", required=True)
    publish.add_argument("--expected-run-attempt", required=True)
    publish.add_argument("--expected-source-sha", required=True)
    publish.add_argument("--expected-source-ref", required=True)
    publish.add_argument("--expected-channel", required=True, choices=tuple(CHANNEL_SCOPE))
    publish.add_argument("--branch", default="main")
    args = parser.parse_args()
    identity = RunIdentity(args.repository, args.expected_run_id, args.expected_run_attempt,
                           args.expected_source_sha, args.expected_source_ref, args.expected_channel)
    try:
        report = publish_artifact(args.artifact_dir, args.repo_dir, identity, branch=args.branch)
    except PublicationError as exc:
        print(f"[publish] 失败: {exc}")
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
