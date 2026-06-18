"""Boulanger (boulanger.com / 法国家电零售) 价格提取。

完整 port 自 TV_Price_Monitor/monitor.py 的 get_boulanger_price + dead-link 判断,
4 个月稳定运行(成功率 ~98% Boulanger 那部分)。

提取策略(顺序):
  1) Schema.org meta / JSON-LD(Boulanger 的 Schema 通常很准)
  2) .price__main .price__amount(主价格元素)
  3) 所有 .price__amount(排除划线价)
  4) 通用 .price / span[class*='price']
"""
from __future__ import annotations

from .base import BaseAdapter
from ..core import clean_price, get_price_from_schema


class BoulangerAdapter(BaseAdapter):
    platform_name = "Boulanger"
    locale_override = ("fr-FR", "Europe/Paris")
    wait_selectors = (".price__amount", ".price")

    def is_dead_link(self, page_title: str) -> bool:
        t = (page_title or "").lower()
        if super().is_dead_link(t):
            return True
        # Boulanger 商品下架页特征:"Oups" / "épuisé"(法语:售罄)
        return "oups" in t or "épuisé" in t or "epuise" in t

    async def extract_price(self, page) -> tuple[float, str] | None:
        # 1. Schema/Meta(最准)
        result = await get_price_from_schema(page)
        if result:
            return result

        # 2. .price__main .price__amount(主价格)
        try:
            main = page.locator(".price__main .price__amount").first
            if await main.is_visible(timeout=2000):
                text = await main.inner_text()
                r = clean_price(text.replace("\n", ","))
                if r:
                    return r
        except Exception:
            pass

        # 3. 所有 .price__amount(排除划线价)
        try:
            for el in await page.locator(".price__amount").all():
                if not await el.is_visible():
                    continue
                is_crossed = await el.evaluate(
                    """el => {
                        const style = window.getComputedStyle(el);
                        return style.textDecoration.includes('line-through')
                            || !!el.closest('.price__crossed, .price__old');
                    }"""
                )
                if is_crossed:
                    continue
                text = await el.inner_text()
                r = clean_price(text.replace("\n", ","))
                if r:
                    return r
        except Exception:
            pass

        # 4. 通用兜底
        for sel in (".price", "span[class*='price']"):
            try:
                if await page.is_visible(sel, timeout=1000):
                    text = await page.inner_text(sel)
                    r = clean_price(text)
                    if r:
                        return r
            except Exception:
                pass
        return None
