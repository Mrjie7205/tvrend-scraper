"""Elkjop catalog 的浏览器签名 key 回归测试。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from catalog_scrape.adapters.elkjop import ElkjopCatalogAdapter  # noqa: E402


class ElkjopSignedKeyTest(unittest.IsolatedAsyncioTestCase):
    async def test_signed_key_uses_browser_page_fetch(self) -> None:
        adapter = ElkjopCatalogAdapter()
        adapter._open_and_pass_checkpoint = AsyncMock(return_value=True)
        adapter._accept_cookies = AsyncMock()
        page = AsyncMock()
        page.evaluate.return_value = {
            "status": 200,
            "apiKey": "short-lived-key",
            "checkpoint": False,
        }

        key = await adapter._signed_api_key(page)

        self.assertEqual("short-lived-key", key)
        adapter._open_and_pass_checkpoint.assert_awaited_once()
        adapter._accept_cookies.assert_awaited_once_with(page)
        page.evaluate.assert_awaited_once()

    async def test_signed_key_retries_checkpoint_without_leaking_body(self) -> None:
        adapter = ElkjopCatalogAdapter()
        adapter._open_and_pass_checkpoint = AsyncMock(return_value=True)
        adapter._accept_cookies = AsyncMock()
        page = AsyncMock()
        page.evaluate.side_effect = [
            {"status": 429, "apiKey": "", "checkpoint": True},
            {"status": 200, "apiKey": "recovered-key", "checkpoint": False},
        ]

        with patch("catalog_scrape.adapters.elkjop.asyncio.sleep", new=AsyncMock()):
            key = await adapter._signed_api_key(page)

        self.assertEqual("recovered-key", key)
        self.assertEqual(2, page.evaluate.await_count)

    async def test_signed_key_fails_closed_when_home_checkpoint_remains(self) -> None:
        adapter = ElkjopCatalogAdapter()
        adapter._open_and_pass_checkpoint = AsyncMock(return_value=False)
        page = AsyncMock()

        with self.assertRaisesRegex(RuntimeError, "首页安全检查未通过"):
            await adapter._signed_api_key(page)

        page.evaluate.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
