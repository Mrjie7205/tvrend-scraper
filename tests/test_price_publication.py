from __future__ import annotations

import csv
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import price_publication as publication
from monitor_prices import prices_io


NOW = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
IDENTITY = publication.RunIdentity(
    "Mrjie7205/tvrend-scraper", "123456", "1", "a" * 40, "refs/heads/main", "Currys",
)


def observation(name="65G5", *, channel="Currys", price="1899.0", days=0, at="11:30:00"):
    country, currency = publication.CHANNEL_SCOPE[channel]
    values = (
        (NOW.date() + timedelta(days=days)).isoformat(), at, "LG", name,
        country, channel, price, currency, "电视价格观测", "Success", "持平",
    )
    return dict(zip(prices_io.PRICES_COLUMNS, values))


def write_csv(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=prices_io.PRICES_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def artifact(path: Path, rows=None, *, identity=IDENTITY, **kwargs):
    publication.write_run_artifact(rows if rows is not None else [observation()], path, identity, now=NOW, **kwargs)
    return path


def change_manifest(path: Path, **changes) -> None:
    target = path / publication.MANIFEST_NAME
    manifest = json.loads(target.read_text(encoding="utf-8"))
    manifest.update(changes)
    target.write_text(json.dumps(manifest), encoding="utf-8")


@pytest.mark.parametrize("field,value", [
    ("run_id", "654321"), ("run_attempt", "2"), ("source_sha", "b" * 40),
    ("source_ref", "refs/heads/codex/test"), ("repository", "other/repo"), ("channel", "Boulanger"),
])
def test_artifact_wrong_identity_is_rejected(tmp_path, field, value):
    directory = artifact(tmp_path / "artifact")
    with pytest.raises(publication.PublicationError, match="身份不符"):
        publication.validate_artifact(directory, replace(IDENTITY, **{field: value}), now=NOW)


@pytest.mark.parametrize("missing", [publication.MANIFEST_NAME, publication.PAYLOAD_NAME])
def test_missing_artifact_file_fails_closed(tmp_path, missing):
    directory = artifact(tmp_path / "artifact")
    (directory / missing).rename(directory / f"{missing}.saved")
    with pytest.raises(publication.PublicationError, match="缺失或损坏"):
        publication.validate_artifact(directory, IDENTITY, now=NOW)


def test_payload_damage_and_manifest_count_are_rejected(tmp_path):
    directory = artifact(tmp_path / "artifact")
    path = directory / publication.PAYLOAD_NAME
    path.write_bytes(path.read_bytes().replace(b"1899.0", b"1839.0"))
    with pytest.raises(publication.PublicationError, match="校验和"):
        publication.validate_artifact(directory, IDENTITY, now=NOW)
    change_manifest(directory, sha256=hashlib.sha256(path.read_bytes()).hexdigest(), row_count=2)
    with pytest.raises(publication.PublicationError, match="数量"):
        publication.validate_artifact(directory, IDENTITY, now=NOW)


@pytest.mark.parametrize("rows", [[], [observation(days=-2)], [observation(days=2)],
                                 [observation(price="nan")], [observation(price="inf")],
                                 [observation(at="25:00:00")], [observation(channel="Boulanger")]])
def test_invalid_or_old_snapshot_observations_do_not_create_manifest(tmp_path, rows):
    directory = tmp_path / "artifact"
    with pytest.raises(publication.PublicationError):
        artifact(directory, rows)
    assert not (directory / publication.MANIFEST_NAME).exists()


def test_empty_or_partial_csv_cannot_claim_success(tmp_path):
    directory = artifact(tmp_path / "artifact")
    payload = directory / publication.PAYLOAD_NAME
    payload.write_bytes(b"Date,Price\n2026-10-09,1899\n")
    change_manifest(directory, sha256=hashlib.sha256(payload.read_bytes()).hexdigest())
    with pytest.raises(publication.PublicationError, match="11 列"):
        publication.validate_artifact(directory, IDENTITY, now=NOW)


def test_artifact_cannot_overwrite_previous_run(tmp_path):
    directory = artifact(tmp_path / "artifact")
    original = (directory / publication.PAYLOAD_NAME).read_bytes()
    with pytest.raises(publication.PublicationError, match="非空"):
        artifact(directory, [observation(price="100.0")])
    assert (directory / publication.PAYLOAD_NAME).read_bytes() == original


@pytest.mark.parametrize("smoke,source_ref", [(1, "refs/heads/main"), (0, "refs/heads/codex/test")])
def test_smoke_and_manual_branches_never_reach_git(tmp_path, monkeypatch, smoke, source_ref):
    identity = replace(IDENTITY, source_ref=source_ref)
    directory = artifact(tmp_path / "artifact", identity=identity, max_skus=smoke)
    monkeypatch.setattr(publication, "_git", lambda *a, **k: pytest.fail("不应执行 Git 操作"))
    with pytest.raises(publication.PublicationError, match="烟测|手动分支"):
        publication.publish_artifact(directory, tmp_path, identity, now=NOW)


def test_merge_preserves_different_quotes_and_only_trims_outside_window():
    boundary = observation("BOUNDARY", days=-45)
    expired = observation("EXPIRED", days=-46)
    unknown_date = dict(observation("BAD_DATE"), Date="legacy-unparsed")
    same_time_other_price = observation(price="1839.0")
    current = [boundary, expired, unknown_date, observation()]
    incoming = [same_time_other_price, same_time_other_price]
    merged, report = publication.merge_observations(current, incoming, today=NOW.date())
    keys = {publication.row_key(row) for row in merged}
    assert publication.row_key(boundary) in keys
    assert publication.row_key(unknown_date) in keys
    assert publication.row_key(expired) not in keys
    assert {row["Price"] for row in merged if row["Product Name"] == "65G5"} == {"1839.0", "1899.0"}
    assert len(merged) == 4
    assert report["trimmed_rows"] == 1
    assert report["deduplicated_rows"] == 1
    assert report["added_unique_rows"] == 1


def test_future_date_cannot_advance_retention_boundary():
    with pytest.raises(publication.PublicationError, match="未来日期"):
        publication.merge_observations([observation(days=2)], [observation()], today=NOW.date())
    with pytest.raises(publication.PublicationError, match="超出保留窗口"):
        publication.merge_observations([observation()], [observation(days=-46)], today=NOW.date())


def test_same_timestamp_keeps_original_order_and_last_observation_priority(tmp_path, monkeypatch):
    # 若改按整行/价格排序，1839 会移到1899之前，改变既有同刻后行优先口径。
    first = observation(price="1899.0")
    last = observation(price="1839.0")
    merged, _ = publication.merge_observations([first], [last], today=NOW.date())
    assert [row["Price"] for row in merged] == ["1899.0", "1839.0"]
    write_csv(tmp_path / "raw/prices.csv", merged)
    monkeypatch.setattr(prices_io, "_root", lambda: tmp_path)
    assert prices_io.load_latest_historical_prices()["65G5_GB_Currys"] == 1839.0


def test_late_history_does_not_replace_newest_price(tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    write_csv(raw / "prices.csv", [observation(price="599.0", at="12:00:00"), observation(price="649.0", at="11:00:00")])
    write_csv(raw / "prices_old.csv", [observation(price="999.0", days=-1)])
    monkeypatch.setattr(prices_io, "_root", lambda: tmp_path)
    assert prices_io.load_latest_historical_prices()["65G5_GB_Currys"] == 599.0
    assert len(list(csv.DictReader((raw / "prices.csv").open(encoding="utf-8-sig")))) == 2


def test_monitor_artifact_mode_never_rewrites_or_trims_checkout(tmp_path, monkeypatch):
    raw = tmp_path / "raw" / "prices.csv"
    write_csv(raw, [observation("EXPIRED_BUT_REMOTE_OWNED", days=-60)])
    original = raw.read_bytes()
    destination = tmp_path / "publication"
    monkeypatch.setattr(prices_io, "_root", lambda: tmp_path)
    monkeypatch.setattr(publication, "_utc_now", lambda: NOW)
    for name, value in {
        "PRICE_PUBLICATION_DIR": str(destination), "MONITOR_MAX_SKUS": "0", "CHANNELS": "Currys",
        "GITHUB_REPOSITORY": IDENTITY.repository, "GITHUB_RUN_ID": IDENTITY.run_id,
        "GITHUB_RUN_ATTEMPT": IDENTITY.run_attempt, "GITHUB_SHA": IDENTITY.source_sha,
        "GITHUB_REF": IDENTITY.source_ref,
    }.items():
        monkeypatch.setenv(name, value)
    prices_io.append_prices([observation()])
    prices_io.trim_prices_window()
    assert raw.read_bytes() == original
    _, rows = publication.validate_artifact(destination, IDENTITY, now=NOW)
    assert [row["Product Name"] for row in rows] == ["65G5"]
    with pytest.raises(publication.PublicationError, match="没有有效价格"):
        prices_io.append_prices([])


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false", *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
    )
    return result.stdout.decode("utf-8-sig").strip()


def make_git_repositories(tmp_path, line_ending="\r\n"):
    remote = tmp_path / "remote.git"
    remote.mkdir()
    git(remote, "init", "--bare", "--initial-branch=main")
    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "--initial-branch=main")
    git(seed, "config", "user.name", "Test")
    git(seed, "config", "user.email", "test@example.invalid")
    write_csv(seed / "raw/prices.csv", [observation("BASE", days=-1)])
    if line_ending == "\n":
        path = seed / "raw/prices.csv"
        path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n"))
    (seed / "unrelated.txt").write_text("保留其他提交文件", encoding="utf-8")
    git(seed, "add", "--", "raw/prices.csv", "unrelated.txt")
    git(seed, "commit", "-m", "baseline")
    git(seed, "remote", "add", "origin", str(remote))
    git(seed, "push", "origin", "main")
    for name in ("a", "b"):
        git(tmp_path, "clone", str(remote), name)
    return remote, tmp_path / "a", tmp_path / "b"


@pytest.mark.parametrize("line_ending", ["\n", "\r\n"])
def test_real_push_race_rebuilds_union_and_repeat_is_idempotent(tmp_path, monkeypatch, line_ending):
    remote, repo_a, repo_b = make_git_repositories(tmp_path, line_ending=line_ending)
    # 唯一替换的是网络目标白名单，竞争本身使用真实 Git fetch / commit / push。
    monkeypatch.setattr(publication, "_validate_origin", lambda *args: None)
    identity_a = replace(IDENTITY, channel="Boulanger", run_id="654321")
    artifact_a = artifact(tmp_path / "artifact-a", [observation("A", channel="Boulanger")], identity=identity_a)
    artifact_b = artifact(tmp_path / "artifact-b", [observation("B")])
    local_head_before = git(repo_b, "rev-parse", "HEAD")
    original_git = publication._git
    raced = False

    def racing_git(repo, *arguments, **kwargs):
        nonlocal raced
        if Path(repo) == repo_b and arguments[0] == "push" and not raced:
            raced = True
            publication.publish_artifact(artifact_a, repo_a, identity_a, now=NOW, retry_delay=0)
        return original_git(repo, *arguments, **kwargs)

    monkeypatch.setattr(publication, "_git", racing_git)
    result = publication.publish_artifact(artifact_b, repo_b, IDENTITY, now=NOW, retry_delay=0)
    assert result["attempt"] == 2
    assert result["after_rows"] == 3
    remote_csv = git(remote, "show", "main:raw/prices.csv")
    assert ",A,FR,Boulanger," in remote_csv and ",B,GB,Currys," in remote_csv and ",BASE," in remote_csv
    assert git(repo_b, "rev-parse", "HEAD") == local_head_before
    assert git(repo_b, "status", "--porcelain") == ""
    assert git(remote, "show", "main:unrelated.txt") == "保留其他提交文件"
    assert git(remote, "diff-tree", "--no-commit-id", "--name-only", "-r", result["published_sha"]) == "raw/prices.csv"
    published_head = git(remote, "rev-parse", "main")
    raw_blob = subprocess.check_output(["git", "-C", str(remote), "show", "main:raw/prices.csv"])
    assert (b"\r\n" in raw_blob) == (line_ending == "\r\n")
    assert raw_blob.startswith(b"\xef\xbb\xbf")
    again = publication.publish_artifact(artifact_b, repo_b, IDENTITY, now=NOW, retry_delay=0)
    assert again["status"] == "already_present"
    assert git(remote, "rev-parse", "main") == published_head


def test_failed_push_does_not_claim_success_or_consume_artifact(tmp_path, monkeypatch):
    remote, repo, _ = make_git_repositories(tmp_path)
    directory = artifact(tmp_path / "artifact")
    monkeypatch.setattr(publication, "_validate_origin", lambda *args: None)
    original_git = publication._git
    before_sha = git(remote, "rev-parse", "main")

    def rejected_git(repo, *arguments, **kwargs):
        if arguments[0] == "push":
            return subprocess.CompletedProcess(arguments, 1, b"", b"permission denied")
        return original_git(repo, *arguments, **kwargs)

    monkeypatch.setattr(publication, "_git", rejected_git)
    with pytest.raises(publication.PublicationError, match="远端没有竞争更新"):
        publication.publish_artifact(directory, repo, IDENTITY, now=NOW, retry_delay=0)
    assert git(remote, "rev-parse", "main") == before_sha
    publication.validate_artifact(directory, IDENTITY, now=NOW)


def test_dirty_worktree_or_unexpected_remote_never_publishes(tmp_path, monkeypatch):
    _, repo, _ = make_git_repositories(tmp_path)
    directory = artifact(tmp_path / "artifact")
    with pytest.raises(publication.PublicationError, match="origin"):
        publication.publish_artifact(directory, repo, IDENTITY, now=NOW)
    (repo / "unrelated.txt").write_text("用户未提交改动", encoding="utf-8")
    monkeypatch.setattr(publication, "_validate_origin", lambda *args: pytest.fail("应先拒绝脏工作区"))
    with pytest.raises(publication.PublicationError, match="未提交改动"):
        publication.publish_artifact(directory, repo, IDENTITY, now=NOW)
    assert (repo / "unrelated.txt").read_text(encoding="utf-8") == "用户未提交改动"
