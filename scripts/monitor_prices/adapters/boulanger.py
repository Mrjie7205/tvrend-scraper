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

import os
import random
import re

from .base import BaseAdapter
from ..core import (
    STEALTH_JS,
    USER_AGENTS,
    VIEWPORT_HEIGHTS,
    VIEWPORT_WIDTHS,
    clean_price,
    get_price_from_schema,
)


RE_REF_ID = re.compile(r"/ref/(\d+)", re.IGNORECASE)


class BoulangerAdapter(BaseAdapter):
    platform_name = "Boulanger"
    locale_override = ("fr-FR", "Europe/Paris")
    wait_selectors = (".price__amount", ".price")
    navigation_wait_until = "commit"
    batch_price_enabled = True

    def batch_price_key(self, url: str) -> str:
        match = RE_REF_ID.search(url or "")
        return match.group(1) if match else super().batch_price_key(url)

    async def prepare_batch_prices(self, browser, skus: list[dict]) -> dict[str, tuple[float, str]]:
        """按五大品牌 facet 批量取价，未命中的少量链接再回退 PDP。"""
        from catalog_scrape.adapters.boulanger import BoulangerCatalogAdapter

        ctx = await browser.new_context(
            user_agent=random.choice(USER_AGENTS),
            viewport={
                "width": random.choice(VIEWPORT_WIDTHS),
                "height": random.choice(VIEWPORT_HEIGHTS),
            },
            locale="fr-FR",
            timezone_id="Europe/Paris",
        )
        await ctx.add_init_script(STEALTH_JS)
        page = await ctx.new_page()
        try:
            items = await BoulangerCatalogAdapter().fetch_catalog(page)
        finally:
            await ctx.close()

        price_map: dict[str, tuple[float, str]] = {}
        for item in items:
            if item.price_hint_eur is None:
                continue
            key = self.batch_price_key(item.url)
            if key:
                price_map[key] = (float(item.price_hint_eur), "EUR")

        requested_keys = {self.batch_price_key(s["url"]) for s in skus}
        matched = sum(1 for key in requested_keys if key in price_map)
        coverage = matched / len(requested_keys) if requested_keys else 0.0
        min_items = int(os.environ.get("BOULANGER_BATCH_MIN_ITEMS", "180"))
        min_coverage = float(os.environ.get("BOULANGER_BATCH_MIN_COVERAGE", "0.50"))
        print(
            f"[monitor/Boulanger] 类目价格快照 {len(price_map)} 条，"
            f"命中 {matched}/{len(requested_keys)} ({coverage:.1%})"
        )
        if len(price_map) < min_items or coverage < min_coverage:
            print(
                f"[monitor/Boulanger] 快照未通过完整性闸门 "
                f"(items>={min_items}, coverage>={min_coverage:.0%})，回退 PDP"
            )
            return {}
        return price_map

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
