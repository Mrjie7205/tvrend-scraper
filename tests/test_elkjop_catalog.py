"""Elkjop catalog 的浏览器签名 key 回归测试。"""

from __future__ import annotations

import sys
import base64
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from catalog_scrape.adapters.elkjop import (  # noqa: E402
    ElkjopCatalogAdapter,
    _signed_key_expiry,
)


class ElkjopSignedKeyTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def signed_key(valid_until: int) -> str:
        raw = (
            "a" * 64
            + "restrictIndices=commerce_*%2Ccontent_*%2CstoreIndex"
            + f"&validUntil={valid_until}"
        )
        return base64.b64encode(raw.encode()).decode()

    def test_signed_key_expiry_validates_scope_and_lifetime(self) -> None:
        key = self.signed_key(1600)
        self.assertEqual(1600, _signed_key_expiry(key, now=1000))
        self.assertIsNone(_signed_key_expiry(key, now=1590))
        self.assertIsNone(_signed_key_expiry("not-a-signed-key", now=1000))

    async def test_relay_uses_bearer_token_and_validates_signed_key(self) -> None:
        adapter = ElkjopCatalogAdapter()
        response = AsyncMock()
        response.ok = True
        response.json.return_value = {"apiKey": self.signed_key(1600)}
        request_context = AsyncMock()
        request_context.get.return_value = response

        with (
            patch("catalog_scrape.adapters.elkjop.KEY_RELAY_URL", "https://relay.test/key"),
            patch("catalog_scrape.adapters.elkjop.KEY_RELAY_TOKEN", "relay-token"),
            patch("catalog_scrape.adapters.elkjop.time.time", return_value=1000),
        ):
            key = await adapter._signed_api_key_from_relay(request_context)

        self.assertEqual(self.signed_key(1600), key)
        headers = request_context.get.await_args.kwargs["headers"]
        self.assertEqual("Bearer relay-token", headers["authorization"])
        self.assertEqual(0, request_context.get.await_args.kwargs["max_redirects"])
        self.assertEqual(180_000, request_context.get.await_args.kwargs["timeout"])

    async def test_relay_rejects_plain_http_before_sending_token(self) -> None:
        adapter = ElkjopCatalogAdapter()
        request_context = AsyncMock()
        with (
            patch("catalog_scrape.adapters.elkjop.KEY_RELAY_URL", "http://relay.test/key"),
            patch("catalog_scrape.adapters.elkjop.KEY_RELAY_TOKEN", "relay-token"),
        ):
            with self.assertRaisesRegex(RuntimeError, "必须是无 userinfo 的 HTTPS"):
                await adapter._signed_api_key_from_relay(request_context)
        request_context.get.assert_not_awaited()

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

        key = await adapter._signed_api_key_from_browser(page)

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
            key = await adapter._signed_api_key_from_browser(page)

        self.assertEqual("recovered-key", key)
        self.assertEqual(2, page.evaluate.await_count)

    async def test_signed_key_fails_closed_when_home_checkpoint_remains(self) -> None:
        adapter = ElkjopCatalogAdapter()
        adapter._open_and_pass_checkpoint = AsyncMock(return_value=False)
        page = AsyncMock()

        with self.assertRaisesRegex(RuntimeError, "首页安全检查未通过"):
            await adapter._signed_api_key_from_browser(page)

        page.evaluate.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
