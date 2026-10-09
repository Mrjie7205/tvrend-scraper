"""日抓在页面关闭前留证；旁路失败与超时不改变原始业务结果。"""
from __future__ import annotations

import asyncio
from contextlib import ExitStack
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from monitor_prices import run_daily
import failure_evidence
ORIGINAL_NEW_CONTEXT = run_daily._new_context


SKU = {"brand": "TCL", "product_name": "55TEST", "country": "FR", "platform": "Boulanger",
       "url": "https://www.boulanger.com/ref/1000001"}


def adapter(price=None):
    return SimpleNamespace(platform_name="Boulanger", batch_price_key=lambda url: url,
                           direct_price_enabled=False, navigation_wait_until="domcontentloaded",
                           is_unavailable_response=lambda status, url, final: status == 404,
                           is_dead_link=lambda title: False, cookie_accept_selectors=(), wait_selectors=(),
                           extract_price=AsyncMock(return_value=price), shared_context=False,
                           batch_price_enabled=False, locale_override=None)


class MonitorEvidenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.stack = ExitStack()
        self.stack.enter_context(patch.dict(os.environ, {"FAILURE_EVIDENCE_DIR": self.temporary.name}))
        self.stack.enter_context(patch.object(run_daily.asyncio, "sleep", new=AsyncMock()))
        self.events = []
        self.page = SimpleNamespace(url=SKU["url"], goto=AsyncMock(return_value=SimpleNamespace(status=200)),
                                    title=AsyncMock(return_value="TCL TV"),
                                    close=AsyncMock(side_effect=lambda: self.events.append("close_page")))
        self.ctx = SimpleNamespace(new_page=AsyncMock(return_value=self.page),
                                   close=AsyncMock(side_effect=lambda: self.events.append("close_context")))
        self.stack.enter_context(patch.object(run_daily, "_new_context", new=AsyncMock(return_value=self.ctx)))
        self.stack.enter_context(patch.object(run_daily, "handle_antibot_page", new=AsyncMock(return_value=True)))

    def tearDown(self):
        self.stack.close()
        self.temporary.cleanup()

    async def run_sku(self, chosen):
        self.stack.enter_context(patch.object(run_daily, "get_adapter", return_value=chosen))
        return await run_daily.process_sku(asyncio.Semaphore(1), object(), SKU, {})

    async def test_dead_link_capture_precedes_cleanup_and_preserves_status(self):
        self.page.goto.return_value.status = 404
        capture = self.stack.enter_context(patch.object(run_daily, "capture_failure", new=AsyncMock(side_effect=lambda *a, **kw: self.events.append("capture"))))
        result = await self.run_sku(adapter())
        self.assertEqual("Failed: Dead Link", result["Status"])
        self.assertEqual(["capture", "close_page", "close_context"], self.events)
        self.assertIs(self.page, capture.await_args.args[0])
        self.assertEqual(404, capture.await_args.kwargs["http_status"])

    async def test_evidence_exception_cannot_replace_original_failure(self):
        self.page.goto.return_value.status = 404
        self.stack.enter_context(patch.object(run_daily, "capture_failure", new=AsyncMock(side_effect=OSError("disk"))))
        result = await self.run_sku(adapter())
        self.assertEqual("Failed: Dead Link", result["Status"])
        self.assertEqual(["close_page", "close_context"], self.events)

    async def test_success_has_no_screenshot(self):
        capture = self.stack.enter_context(patch.object(run_daily, "capture_failure", new=AsyncMock()))
        result = await self.run_sku(adapter((499.0, "EUR")))
        self.assertEqual("Success", result["Status"])
        capture.assert_not_awaited()

    async def test_page_creation_failure_has_summary_and_context_cleanup(self):
        self.ctx.new_page.side_effect = RuntimeError("page unavailable")
        capture = self.stack.enter_context(patch.object(run_daily, "capture_failure", new=AsyncMock()))
        result = await self.run_sku(adapter())
        self.assertTrue(result["Status"].startswith("Failed: Critical"))
        self.assertIsNone(capture.await_args.args[0])
        self.assertEqual("create_page", capture.await_args.kwargs["stage"])
        self.assertEqual(["close_context"], self.events)

    async def test_context_creation_failure_has_no_page_summary(self):
        run_daily._new_context.side_effect = RuntimeError("context unavailable")
        capture = self.stack.enter_context(patch.object(run_daily, "capture_failure", new=AsyncMock()))
        result = await self.run_sku(adapter())
        self.assertTrue(result["Status"].startswith("Failed: Critical"))
        self.assertIsNone(capture.await_args.args[0])
        self.assertEqual("create_context", capture.await_args.kwargs["stage"])

    async def test_context_initialization_failure_closes_unreturned_context(self):
        self.ctx.add_init_script = AsyncMock(side_effect=RuntimeError("init unavailable"))
        browser = SimpleNamespace(new_context=AsyncMock(return_value=self.ctx))
        with self.assertRaisesRegex(RuntimeError, "init unavailable"):
            await ORIGINAL_NEW_CONTEXT(browser, adapter(), "FR")
        self.ctx.close.assert_awaited_once()

    async def test_shared_warmup_page_creation_failure_still_has_summary(self):
        chosen = adapter()
        chosen.warmup_url = "https://www.boulanger.com/c/televiseur"
        self.ctx.new_page.side_effect = RuntimeError("page unavailable")
        capture = self.stack.enter_context(patch.object(run_daily, "capture_failure", new=AsyncMock()))
        with self.assertRaisesRegex(RuntimeError, "page unavailable"):
            await run_daily.process_shared_group(object(), chosen, [SKU], {})
        self.assertIsNone(capture.await_args.args[0])
        self.assertEqual("shared_warmup", capture.await_args.kwargs["stage"])
        self.ctx.close.assert_awaited_once()

    async def test_navigation_and_price_missing_keep_existing_error_codes(self):
        capture = self.stack.enter_context(patch.object(run_daily, "capture_failure", new=AsyncMock()))
        self.page.goto.side_effect = TimeoutError("navigation timed out")
        result = await self.run_sku(adapter())
        self.assertEqual("Failed: Navigation Error", result["Status"])
        self.assertEqual("navigation_error", capture.await_args.kwargs["reason"])
        self.page.goto.side_effect = None
        result = await self.run_sku(adapter())
        self.assertEqual("Failed: Price Not Found", result["Status"])
        self.assertEqual("price_not_found", capture.await_args.kwargs["reason"])

    async def test_hard_timeout_with_hanging_evidence_releases_slot(self):
        async def hang(*args, **kwargs):
            await asyncio.Event().wait()
        chosen = adapter()
        chosen.extract_price.side_effect = hang
        self.page.evaluate = AsyncMock(side_effect=hang)
        self.page.is_closed = lambda: False
        self.stack.enter_context(patch.object(run_daily, "get_adapter", return_value=chosen))
        self.stack.enter_context(patch.object(run_daily, "SKU_TIMEOUT_SECONDS", 0.02))
        self.stack.enter_context(patch.object(failure_evidence, "CAPTURE_TIMEOUT_SECONDS", 0.02))
        sem = asyncio.Semaphore(1)
        started = time.monotonic()
        result = await asyncio.wait_for(run_daily.process_sku_bounded(sem, object(), SKU, {}), timeout=0.4)
        self.assertLess(time.monotonic() - started, 0.35)
        self.assertEqual("Failed: SKU Timeout", result["Status"])
        self.assertEqual(["close_page", "close_context"], self.events)
        await asyncio.wait_for(sem.acquire(), timeout=0.05)
        self.assertTrue(list(Path(self.temporary.name).glob("*.json")))

    async def test_external_cancellation_is_propagated_after_capture_and_cleanup(self):
        entered = asyncio.Event()
        async def hang(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        chosen = adapter()
        chosen.extract_price.side_effect = hang
        self.stack.enter_context(patch.object(run_daily, "get_adapter", return_value=chosen))
        capture = self.stack.enter_context(patch.object(run_daily, "capture_failure", new=AsyncMock(side_effect=lambda *a, **kw: self.events.append("capture"))))
        task = asyncio.create_task(run_daily.process_sku(asyncio.Semaphore(1), object(), SKU, {}))
        await asyncio.wait_for(entered.wait(), timeout=0.1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=0.2)
        self.assertEqual("sku_cancelled", capture.await_args.kwargs["reason"])
        self.assertEqual(["capture", "close_page", "close_context"], self.events)

    async def test_batch_exception_records_summary_and_keeps_pdp_fallback(self):
        chosen = adapter()
        chosen.batch_price_enabled = True
        chosen.prepare_batch_prices = AsyncMock(side_effect=RuntimeError("batch unavailable"))
        browser = SimpleNamespace(close=AsyncMock())
        player = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)))
        manager = AsyncMock()
        manager.__aenter__.return_value = player
        success = {"Brand": "TCL", "Product Name": "55TEST", "Country": "FR", "Platform": "Boulanger",
                   "Price": 499.0, "Currency": "EUR", "Page Title": "TCL TV", "Status": "Success", "Price_Trend": "新上线"}
        import playwright.async_api
        for name, value in (("reset_checkpoint", Path(self.temporary.name) / "partial.csv"),
                            ("load_active_skus", [SKU]), ("channels_in_scope", None),
                            ("load_latest_historical_prices", {}), ("get_adapter", chosen)):
            self.stack.enter_context(patch.object(run_daily, name, return_value=value))
        self.stack.enter_context(patch.object(playwright.async_api, "async_playwright", return_value=manager))
        self.stack.enter_context(patch.object(run_daily, "process_sku_bounded", new=AsyncMock(return_value=success)))
        self.stack.enter_context(patch.object(run_daily, "write_checkpoint"))
        append = self.stack.enter_context(patch.object(run_daily, "append_prices"))
        self.stack.enter_context(patch.object(run_daily, "trim_prices_window"))
        summary = self.stack.enter_context(patch.object(run_daily, "record_failure", wraps=failure_evidence.record_failure))
        self.assertEqual(0, await run_daily.run())
        self.assertEqual("batch_preparation_failed", summary.call_args.kwargs["reason"])
        self.assertEqual(1, len(append.call_args.args[0]))
        browser.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
