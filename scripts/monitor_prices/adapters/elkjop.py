"""Elkjøp（挪威）商品详情页价格提取。

Elkjøp 由 Vercel Security Checkpoint 保护，验证结果绑定浏览器会话。因此该
adapter 要求同渠道 SKU 串行复用一个 context，并先访问首页完成预热。
"""
from __future__ import annotations

import json
import os
import re
from urllib.parse import quote

from .base import BaseAdapter
from ..core import clean_price, get_price_from_schema

RE_SKU = re.compile(r"/(\d{5,9})(?:[/?#]|$)")
TRPC_DYNAMIC_URL = "https://www.elkjop.no/api/trpc/product.getDynamicProductData"


class ElkjopAdapter(BaseAdapter):
    platform_name = "Elkjop"
    locale_override = ("nb-NO", "Europe/Oslo")
    # GitHub hosted runner 打开 Elkjøp 页面会被 Vercel checkpoint 拦住；日抓价格优先走
    # 前端同源 tRPC 动态商品接口。若接口临时不可用，再回退原 PDP 页面解析逻辑。
    direct_price_enabled = os.environ.get("ELKJOP_DIRECT_PRICE", "true").strip().lower() not in {"0", "false", "no"}
    shared_context = False
    warmup_url = None
    antibot_max_waits = 24
    antibot_wait_seconds = 5.0
    wait_selectors = (
        "meta[property='product:price:amount']",
        "meta[itemprop='price']",
        "script[type='application/ld+json']",
    )

    def is_dead_link(self, page_title: str) -> bool:
        t = (page_title or "").lower()
        return super().is_dead_link(t) or "siden finnes ikke" in t

    @staticmethod
    def _sku_from_url(url: str) -> str | None:
        m = RE_SKU.search(url or "")
        return m.group(1) if m else None

    async def extract_price_direct(self, url: str, request_context=None) -> tuple[float, str] | None:
        """优先从 Elkjøp tRPC 动态商品接口取当前含税 NOK 价。

        PDP 页面在 GitHub hosted runner 上容易被 Vercel Security Checkpoint 拦住；
        这个接口是前端页面加载后获取价格/库存的数据源。price.current[0] 是含税价，
        price.current[1] 是不含税价；我们只入库含税 NOK。
        """
        if not self.direct_price_enabled or request_context is None:
            return None
        sku = self._sku_from_url(url)
        if not sku:
            return None

        input_payload = json.dumps({"0": {"sku": sku}}, separators=(",", ":"))
        api_url = f"{TRPC_DYNAMIC_URL}?batch=1&input={quote(input_payload, safe='')}"
        response = await request_context.get(
            api_url,
            headers={
                "accept": "application/json",
                "referer": url,
            },
            timeout=30000,
        )
        if not response.ok:
            print(f"  [Elkjop/api] {sku} HTTP {response.status}")
            return None

        try:
            payload = await response.json()
            data = payload[0]["result"]["data"]
            price = data.get("price") or {}
            currency = str(price.get("currency") or "").upper()
            current = price.get("current") or []
            value = current[0] if isinstance(current, list) and current else None
            sellability = data.get("sellability") or {}
        except Exception as exc:
            print(f"  [Elkjop/api] {sku} JSON 解析失败: {str(exc)[:80]}")
            return None

        try:
            value = float(value)
        except (TypeError, ValueError):
            return None
        if currency != "NOK" or not (100 <= value <= 500_000):
            print(f"  [Elkjop/api] {sku} 价格异常: {value} {currency}")
            return None
        if sellability.get("isDisabled") or sellability.get("isDiscontinued"):
            print(f"  [Elkjop/api] {sku} 已禁售/停产，跳过")
            return None

        print(f"  [Elkjop/api] {sku} → NOK {value}")
        return value, "NOK"

    async def extract_price(self, page) -> tuple[float, str] | None:
        result = await get_price_from_schema(page)
        if result:
            price, currency = result
            if str(currency).upper() == "NOK":
                return price, "NOK"

        # 页面改版时的保守兜底：只接受明确带 kr/NOK 的可见主价格。
        for sel in (
            "[data-testid*='price']",
            "[class*='sales-price']",
            "[class*='current-price']",
            "[class*='price']",
        ):
            try:
                for el in await page.locator(sel).all():
                    if not await el.is_visible():
                        continue
                    text = await el.inner_text()
                    if "kr" not in text.lower() and "nok" not in text.lower():
                        continue
                    parsed = clean_price(text)
                    if parsed and parsed[1] == "NOK":
                        return parsed
            except Exception:
                continue
        return None
