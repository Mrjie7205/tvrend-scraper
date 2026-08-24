"""每日价格任务的增量检查点。

主 CSV 仍只在整轮成功后提交；检查点则在每个任务完成后原子更新并作为 Action
artifact 上传。这样即使 runner 被取消，已完成的真实结果仍可审计和恢复。
"""
from __future__ import annotations

import csv
import os
import time
from pathlib import Path
from typing import Iterable

from monitor_prices.prices_io import PRICES_COLUMNS


def default_checkpoint_path() -> Path:
    return Path(__file__).resolve().parents[1] / "monitor_artifacts" / "partial_prices.csv"


def reset_checkpoint(path: Path | None = None) -> Path:
    target = path or default_checkpoint_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    _write_atomic([], target)
    return target


def write_checkpoint(rows: Iterable[dict], path: Path | None = None) -> Path:
    """将当前全部已完成结果原子写入，避免中断时留下半个 CSV。"""
    target = path or default_checkpoint_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    _write_atomic(list(rows), target)
    return target


def _write_atomic(rows: list[dict], target: Path) -> None:
    tmp = target.with_suffix(target.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=PRICES_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in PRICES_COLUMNS})
    # Windows Defender、文件索引器或 artifact 读取可能极短暂占用目标文件。
    # 检查点是恢复辅助文件，不应因一次瞬时锁定让整轮真实价格抓取失败。
    last_error: PermissionError | None = None
    for attempt in range(6):
        try:
            os.replace(tmp, target)
            return
        except PermissionError as exc:
            last_error = exc
            time.sleep(0.05 * (attempt + 1))
    assert last_error is not None
    raise last_error
