"""Catalog 主入口的跨平台输出和失败原因回归测试，不访问网络。"""

from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from catalog_scrape.run_weekly import (  # noqa: E402
    AdapterRunResult,
    _amazon_catalog_size_failure,
    _summarize_results,
    run_one_adapter,
)


class CatalogRunnerOutputTest(unittest.TestCase):
    def test_amazon_size_guard_rejects_partial_history_recovery(self) -> None:
        failure = _amazon_catalog_size_failure(21, [755, 769, 770, 761, 758])
        self.assertIsNotNone(failure)
        self.assertIn("21 <", failure or "")

    def test_amazon_size_guard_accepts_normal_daily_variation(self) -> None:
        self.assertIsNone(
            _amazon_catalog_size_failure(669, [634, 646, 656, 669, 670])
        )

    def test_amazon_size_guard_has_absolute_floor_without_history(self) -> None:
        failure = _amazon_catalog_size_failure(40, [])
        self.assertIn("绝对下限", failure or "")

    def test_summary_uses_ascii_status_markers(self) -> None:
        output = io.StringIO()
        with patch("catalog_scrape.run_weekly.sys.stdout", output):
            code = _summarize_results([
                ("amazon_de:Amazon/DE", AdapterRunResult(Path("amazon.csv"))),
            ])

        self.assertEqual(0, code)
        self.assertIn("[OK] amazon_de:Amazon/DE: amazon.csv", output.getvalue())
        self.assertNotIn("✓", output.getvalue())
        self.assertNotIn("✗", output.getvalue())

    def test_gbk_summary_preserves_original_adapter_failure(self) -> None:
        raw = io.BytesIO()
        output = io.TextIOWrapper(raw, encoding="gbk", errors="strict")
        with patch("catalog_scrape.run_weekly.sys.stdout", output):
            code = _summarize_results([
                (
                    "amazon_de:Amazon/DE",
                    AdapterRunResult(
                        path=None,
                        failure_reason="抓取异常 RuntimeError: checkpoint ✗ blocked",
                    ),
                ),
            ])
            output.flush()

        rendered = raw.getvalue().decode("gbk")
        self.assertEqual(1, code)
        self.assertIn("[FAIL] amazon_de:Amazon/DE", rendered)
        self.assertIn("RuntimeError: checkpoint", rendered)
        self.assertIn(r"\u2717", rendered)


class CatalogRunnerAdapterTest(unittest.IsolatedAsyncioTestCase):
    async def test_adapter_exception_is_kept_for_final_summary(self) -> None:
        adapter = type(
            "AmazonAdapter",
            (),
            {
                "platform_name": "Amazon",
                "country": "DE",
                "locale_override": ("de-DE", "Europe/Berlin"),
                "native_browser_identity": False,
                "fetch_catalog": AsyncMock(
                    side_effect=RuntimeError("response 503 ✗ security checkpoint")
                ),
            },
        )()
        context = AsyncMock()
        context.new_page.return_value = AsyncMock()
        browser = AsyncMock()
        browser.new_context.return_value = context

        with patch("catalog_scrape.run_weekly.sys.stdout", io.StringIO()):
            result = await run_one_adapter(browser, adapter)

        self.assertIsNone(result.path)
        self.assertEqual(
            "抓取异常 RuntimeError: response 503 ✗ security checkpoint",
            result.failure_reason,
        )
        context.close.assert_awaited_once()

    async def test_zero_items_keeps_explicit_failure_reason(self) -> None:
        adapter = type(
            "ElkjopAdapter",
            (),
            {
                "platform_name": "Elkjop",
                "country": "NO",
                "locale_override": ("nb-NO", "Europe/Oslo"),
                "native_browser_identity": False,
                "fetch_catalog": AsyncMock(return_value=[]),
            },
        )()
        context = AsyncMock()
        context.new_page.return_value = AsyncMock()
        browser = AsyncMock()
        browser.new_context.return_value = context

        with patch("catalog_scrape.run_weekly.sys.stdout", io.StringIO()):
            result = await run_one_adapter(browser, adapter)

        self.assertIsNone(result.path)
        self.assertEqual("0 条记录，不写文件", result.failure_reason)
        context.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
