from __future__ import annotations

import asyncio
import csv
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from monitor_prices.checkpoint import reset_checkpoint, write_checkpoint  # noqa: E402
from monitor_prices.core import close_playwright_resource  # noqa: E402
from monitor_prices import run_daily  # noqa: E402


def test_close_playwright_resource_times_out_without_raising() -> None:
    class HangingResource:
        async def close(self):
            await asyncio.Event().wait()

    async def exercise():
        return await asyncio.wait_for(
            close_playwright_resource(HangingResource(), "test", timeout_seconds=0.01),
            timeout=0.2,
        )

    assert asyncio.run(exercise()) is False


def test_process_sku_timeout_releases_concurrency_slot() -> None:
    sku = {
        "brand": "Test",
        "product_name": "TEST55",
        "country": "FR",
        "platform": "Boulanger",
        "url": "https://example.test/item",
    }

    async def never_finishes(*args, **kwargs):
        await asyncio.Event().wait()

    async def exercise():
        sem = asyncio.Semaphore(1)
        with patch.object(run_daily, "SKU_TIMEOUT_SECONDS", 0.02), patch.object(
            run_daily, "process_sku", new=AsyncMock(side_effect=never_finishes)
        ):
            result = await asyncio.wait_for(
                run_daily.process_sku_bounded(sem, object(), sku, {}), timeout=0.3
            )
            await asyncio.wait_for(sem.acquire(), timeout=0.1)
            return result, sem

    result, sem = asyncio.run(exercise())
    assert result["Status"] == "Failed: SKU Timeout"
    assert sem.locked()


def test_checkpoint_is_atomic_and_contains_completed_rows(tmp_path: Path) -> None:
    target = tmp_path / "partial_prices.csv"
    reset_checkpoint(target)
    row = {
        "Date": "2026-08-05",
        "Time": "05:00:00",
        "Brand": "TCL",
        "Product Name": "55P7K",
        "Country": "FR",
        "Platform": "Boulanger",
        "Price": 499.0,
        "Currency": "EUR",
        "Page Title": "Batch category snapshot",
        "Status": "Success",
        "Price_Trend": "降价",
    }
    write_checkpoint([row], target)

    with target.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1
    assert rows[0]["Product Name"] == "55P7K"
    assert rows[0]["Status"] == "Success"
    assert not target.with_suffix(".csv.tmp").exists()
