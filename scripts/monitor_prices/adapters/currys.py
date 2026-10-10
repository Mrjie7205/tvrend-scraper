"""Currys (currys.co.uk / 英国家电零售) 价格提取。

Currys 的 Schema.org / JSON-LD 很标准,项目现成的 get_price_from_schema 直接给
(price, 'GBP')(2026-06 实测主路径命中)。DOM 兜底取 PDP 主商品价,避开
"recently viewed" 里的配件价(£39.99 那种)。clean_price 已自动识别 £ → GBP。
"""
from __future__ import annotations

import os
import re
from collections import Counter
from datetime import datetime, timezone

from .base import BaseAdapter
from ..core import clean_price, get_price_from_schema


RE_PRODUCT_ID = re.compile(r"(\d{7,9})(?:\.html)?(?:[?#]|$)", re.IGNORECASE)


class CurrysPdpGuard:
    """每轮独立的商品页保护；目录403不计数，只有连续不同SKU的确认拒绝触发停止。"""
    def __init__(self, limit: int = 6):
        self.limit = limit
        self.consecutive_skus: dict[tuple[str, str], dict] = {}
        self.maximum_consecutive = 0
        self.outcomes = Counter()
        self.requests = Counter()
        self.skipped = Counter()
        self.stop_reason = None
        self.trigger = None
        self.resets = 0

    @property
    def stopped(self):
        return self.stop_reason is not None

    def start_request(self, purpose="price_lookup") -> bool:
        # 无await的检查和计数使并发任务不能在确认停止后又发出新请求。
        if self.stopped:
            return False
        self.requests[purpose] += 1
        return True

    def record_skip(self, purpose="price_lookup") -> str:
        self.skipped[purpose] += 1
        return "pdp_rate_limited" if "rate_limited" in (self.stop_reason or "") else "pdp_access_suspended"

    def _stop(self, reason, trigger):
        if not self.stopped:
            self.stop_reason = reason
            self.trigger = {**trigger, "observed_at": datetime.now(timezone.utc).isoformat(),
                            "consecutive_skus": list(self.consecutive_skus.values())}

    def catalog_rate_limited(self):
        # 目录明确429是限流指令；与目录403的可恢复局部失败分开处理。
        self._stop("catalog_rate_limited", {"source": "catalog", "http_status": 429})

    def observe(self, sku, *, http_status=None, reason=None):
        kind = reason or "reachable_product"
        self.outcomes[kind] += 1
        if http_status == 429 or reason == "rate_limited":
            self._stop("pdp_rate_limited", {"source": "pdp", "http_status": http_status,
                                           "reason": kind, "product": sku["product_name"]})
            return
        if http_status in {401, 403} or reason in {"access_blocked", "challenge_unresolved"}:
            identity = (sku["product_name"], sku["country"])
            self.consecutive_skus.setdefault(identity, {"product": identity[0], "country": identity[1]})
            self.maximum_consecutive = max(self.maximum_consecutive, len(self.consecutive_skus))
            if len(self.consecutive_skus) >= self.limit:
                self._stop("consecutive_pdp_access_denials", {"source": "pdp", "http_status": http_status,
                                                            "reason": kind, "product": sku["product_name"]})
            return
        # 可达目标200/404、重定向、无价或其它非拒绝结果都不能累积为连续封禁证据。
        if self.consecutive_skus:
            self.resets += 1
        self.consecutive_skus.clear()
        # 一旦触发，本轮保持停止。允许已开始请求收尾，但不会据此重新放开队列。

    def report(self):
        return {"scope": "currys_pdp", "limit": self.limit, "stopped": self.stopped,
                "stop_reason": self.stop_reason, "trigger": self.trigger,
                "consecutive_distinct_skus": len(self.consecutive_skus),
                "maximum_consecutive_distinct_skus": self.maximum_consecutive,
                "reachable_resets": self.resets, "outcomes": dict(self.outcomes),
                "requests_started": dict(self.requests), "skipped": dict(self.skipped)}


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

        self.batch_candidate_prices = {}
        catalog_adapter = CurrysCatalogAdapter()
        self.catalog_report = {}
        try:
            items = await catalog_adapter.fetch_catalog_from_browser(browser)
        finally:
            self.catalog_report = dict(getattr(catalog_adapter, "catalog_report", {}) or {})
        price_map: dict[str, tuple[float, str]] = {}
        for item in items:
            if item.price_hint_eur is None:
                continue
            key = self.batch_price_key(item.url)
            if key:
                price_map[key] = (float(item.price_hint_eur), "GBP")

        self.batch_candidate_prices = dict(price_map)
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
        # 只有明确404/410可确认为链接不可用；重定向与访问拒绝不能推出型号下架。
        return super().is_unavailable_response(status, requested_url, final_url)

    def classify_response(self, status: int, requested_url: str, final_url: str, title: str = "") -> str | None:
        if status in {401, 403}:
            return "access_blocked"
        if status == 429:
            return "rate_limited"
        lowered = (title or "").lower()
        if any(marker in lowered for marker in ("attention required", "access denied", "cloudflare")):
            return "access_blocked"
        if any(marker in lowered for marker in ("just a moment", "bear with us", "security check")):
            return "challenge_unresolved"
        if status in {404, 410}:
            return "dead_link"
        if status >= 400:
            return "http_error"
        requested_key = self.batch_price_key(requested_url)
        final_key = self.batch_price_key(final_url)
        if status == 200 and "/products/" in (requested_url or "") and (
            "/products/" not in (final_url or "") or final_key != requested_key
        ):
            return "redirect_unverified"
        return None

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
