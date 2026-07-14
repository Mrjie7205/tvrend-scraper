"""Elkjop Algolia 短时签名 key 的最小中转服务。

用途：GitHub hosted runner 无法通过 Elkjop/Vercel Challenge；在允许真实 Chromium
会话的机器上运行本服务，只向持有 Bearer Token 的调用方返回短时搜索 key。
Catalog 抓取、清洗和提交仍由 GitHub Action 完成。

启动示例（必须放在 HTTPS 反向代理之后）：
    ELKJOP_KEY_RELAY_TOKEN=<随机长密钥> \
      python -m catalog_scrape.elkjop_key_relay
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

from playwright.async_api import async_playwright

from .adapters.elkjop import ElkjopCatalogAdapter, _signed_key_expiry
from .run_weekly import BROWSER_ARGS


BIND = os.environ.get("ELKJOP_KEY_RELAY_BIND", "127.0.0.1").strip()
PORT = int(os.environ.get("ELKJOP_KEY_RELAY_PORT", "8765"))
TOKEN = os.environ.get("ELKJOP_KEY_RELAY_TOKEN", "").strip()
KEY_PATH = "/v1/elkjop/signed-key"

_CACHE_LOCK = threading.Lock()
_CACHE: tuple[str, int] | None = None


async def _fetch_key_in_browser() -> tuple[str, int]:
    """使用与本地 Catalog 相同的真实页面会话取得并校验 key。"""
    async with async_playwright() as playwright:
        browser = None
        context = None
        try:
            try:
                browser = await playwright.chromium.launch(
                    headless=True,
                    channel="chrome",
                    args=list(BROWSER_ARGS),
                )
            except Exception:
                browser = await playwright.chromium.launch(
                    headless=True,
                    args=list(BROWSER_ARGS),
                )
            context = await browser.new_context(
                locale="nb-NO",
                timezone_id="Europe/Oslo",
                viewport={"width": 1440, "height": 900},
            )
            page = await context.new_page()
            key = await ElkjopCatalogAdapter()._signed_api_key_from_browser(page)
            expiry = _signed_key_expiry(key)
            if not expiry:
                raise RuntimeError("Elkjop 返回的 signed key 未通过范围/有效期校验")
            return key, expiry
        finally:
            if context is not None:
                await context.close()
            if browser is not None:
                await browser.close()


def _get_key() -> tuple[str, int]:
    """有效期剩余超过两分钟时复用内存缓存，避免重复触发站点挑战。"""
    global _CACHE
    with _CACHE_LOCK:
        now = int(time.time())
        if _CACHE and _CACHE[1] > now + 120:
            return _CACHE
        _CACHE = asyncio.run(_fetch_key_in_browser())
        return _CACHE


def _health_payload() -> dict[str, Any]:
    now = int(time.time())
    expiry = _CACHE[1] if _CACHE else 0
    return {
        "ok": True,
        "keyCached": bool(_CACHE and expiry > now + 60),
        "expiresIn": max(0, expiry - now),
    }


class RelayHandler(BaseHTTPRequestHandler):
    server_version = "TvREND-Elkjop-Key-Relay/1.0"

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("cache-control", "no-store")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 接口名固定
        path = urlsplit(self.path).path
        if path == "/health":
            self._send_json(200, _health_payload())
            return
        if path != KEY_PATH:
            self._send_json(404, {"error": "not_found"})
            return

        expected = f"Bearer {TOKEN}"
        received = self.headers.get("authorization", "")
        if not TOKEN or not secrets.compare_digest(received, expected):
            self._send_json(401, {"error": "unauthorized"})
            return

        try:
            key, expiry = _get_key()
        except Exception as exc:
            # 只记录异常类别和截断信息；不记录请求头、Token 或 key。
            print(f"[elkjop-key-relay] 获取失败: {type(exc).__name__}: {str(exc)[:160]}")
            self._send_json(503, {"error": "key_unavailable"})
            return
        self._send_json(200, {"apiKey": key, "validUntil": expiry})

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[elkjop-key-relay] {self.address_string()} {fmt % args}")


def main() -> int:
    if len(TOKEN) < 24:
        raise SystemExit("ELKJOP_KEY_RELAY_TOKEN 必须配置且至少 24 个字符")
    server = ThreadingHTTPServer((BIND, PORT), RelayHandler)
    print(f"[elkjop-key-relay] listening http://{BIND}:{PORT}{KEY_PATH}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
