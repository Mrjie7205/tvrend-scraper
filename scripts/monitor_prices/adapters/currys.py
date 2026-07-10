"""Currys (currys.co.uk / 英国家电零售) 价格提取。

Currys 的 Schema.org / JSON-LD 很标准,项目现成的 get_price_from_schema 直接给
(price, 'GBP')(2026-06 实测主路径命中)。DOM 兜底取 PDP 主商品价,避开
"recently viewed" 里的配件价(£39.99 那种)。clean_price 已自动识别 £ → GBP。
"""
from __future__ import annotations

import os
import re

from .base import BaseAdapter
from ..core import clean_price, get_price_from_schema


RE_PRODUCT_ID = re.compile(r"(\d{7,9})(?:\.html)?(?:[?#]|$)", re.IGNORECASE)


class CurrysAdapter(BaseAdapter):
    platform_name = "Currys"
    locale_override = ("en-GB", "Europe/London")
    wait_selectors = ("[class*='product-price']", ".price")
    batch_price_enabled = True
    navigation_wait_until = "commit"

    def batch_price_key(self, url: str) -> str:
        """Currys URL 的末尾商品 ID 稳定，slug 改名也不会影响关联。"""
        match = RE_PRODUCT_ID.search(url or "")
        return match.group(1) if match else super().batch_price_key(url)

    async def prepare_batch_prices(self, browser, skus: list[dict]) -> dict[str, tuple[float, str]]:
        """先扫电视类目页建立价格快照，减少数百次 PDP 访问和反爬触发。"""
        # 复用 weekly catalog 的“每页新 context”策略，这是目前 GitHub Actions
        # 环境里对 Currys 最稳定的访问方式。
        from catalog_scrape.adapters.currys import CurrysCatalogAdapter

        items = await CurrysCatalogAdapter().fetch_catalog_from_browser(browser)
        price_map: dict[str, tuple[float, str]] = {}
        for item in items:
            if item.price_hint_eur is None:
                continue
            key = self.batch_price_key(item.url)
            if key:
                price_map[key] = (float(item.price_hint_eur), "GBP")

        requested_keys = {self.batch_price_key(s["url"]) for s in skus}
        matched = sum(1 for key in requested_keys if key in price_map)
        coverage = matched / len(requested_keys) if requested_keys else 0.0
        min_items = int(os.environ.get("CURRYS_BATCH_MIN_ITEMS", "300"))
        min_coverage = float(os.environ.get("CURRYS_BATCH_MIN_COVERAGE", "0.55"))
        print(
            f"[monitor/Currys] 类目价格快照 {len(price_map)} 条，"
            f"命中 {matched}/{len(requested_keys)} ({coverage:.1%})"
        )

        # 完整性闸门：分类页异常、只加载出首屏或价格选择器失效时，整批作废，
        # 自动回退原有 PDP 抓取，避免把不完整快照误当成正常结果。
        if len(price_map) < min_items or coverage < min_coverage:
            print(
                f"[monitor/Currys] 快照未通过完整性闸门 "
                f"(items>={min_items}, coverage>={min_coverage:.0%})，回退 PDP"
            )
            return {}
        return price_map

    def is_unavailable_response(self, status: int, requested_url: str, final_url: str) -> bool:
        if super().is_unavailable_response(status, requested_url, final_url):
            return True
        # Currys 的下架 PDP 常返回 200，但最终跳到电视分类页。原逻辑会把它
        # 当成反爬页等待两轮；这里按稳定商品 ID 识别为已下架并快速结束。
        requested_key = self.batch_price_key(requested_url)
        final_key = self.batch_price_key(final_url)
        return "/products/" in (requested_url or "") and (
            "/products/" not in (final_url or "") or final_key != requested_key
        )

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
