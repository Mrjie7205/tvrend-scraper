"""Elkjøp（挪威）商品详情页价格提取。

Elkjøp 由 Vercel Security Checkpoint 保护，验证结果绑定浏览器会话。因此该
adapter 要求同渠道 SKU 串行复用一个 context，并先访问首页完成预热。
"""
from __future__ import annotations

import os

from .base import BaseAdapter
from ..core import clean_price, get_price_from_schema


class ElkjopAdapter(BaseAdapter):
    platform_name = "Elkjop"
    locale_override = ("nb-NO", "Europe/Oslo")
    # 默认沿用稳妥的共享会话串行模式；本地首次导入/回填可显式关闭以加速：
    #   ELKJOP_SHARED_CONTEXT=false MONITOR_CONCURRENCY=3 python -m monitor_prices.run_daily
    shared_context = os.environ.get("ELKJOP_SHARED_CONTEXT", "true").strip().lower() not in {"0", "false", "no"}
    warmup_url = "https://www.elkjop.no/"
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
