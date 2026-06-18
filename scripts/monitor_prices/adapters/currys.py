"""Currys (currys.co.uk / 英国家电零售) 价格提取。

Currys 的 Schema.org / JSON-LD 很标准,项目现成的 get_price_from_schema 直接给
(price, 'GBP')(2026-06 实测主路径命中)。DOM 兜底取 PDP 主商品价,避开
"recently viewed" 里的配件价(£39.99 那种)。clean_price 已自动识别 £ → GBP。
"""
from __future__ import annotations

from .base import BaseAdapter
from ..core import clean_price, get_price_from_schema


class CurrysAdapter(BaseAdapter):
    platform_name = "Currys"
    locale_override = ("en-GB", "Europe/London")
    wait_selectors = ("[class*='product-price']", ".price")

    def is_dead_link(self, page_title: str) -> bool:
        t = (page_title or "").lower()
        if super().is_dead_link(t):
            return True
        # Currys 下架 / 缺货页标题特征
        return (
            "no longer available" in t
            or "out of stock" in t
            or "can't find" in t
            or "cannot find" in t
        )

    async def extract_price(self, page) -> tuple[float, str] | None:
        # 1) Schema / JSON-LD(Currys 标准,直接给 GBP)
        result = await get_price_from_schema(page)
        if result:
            return result

        # 2) DOM 兜底:PDP 主商品价(clean_price 自动识别 £→GBP)
        for sel in (
            "[class*='pdp-component'][class*='product-price']",
            "[data-testid*='product-price']",
            ".product-price",
        ):
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=1500):
                    text = await el.inner_text()
                    r = clean_price(text.replace("\n", " "))
                    if r:
                        return r
            except Exception:
                pass
        return None
