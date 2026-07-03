"""Elkjøp 挪威电视类目抓取。

站点是客户端渲染并带 Vercel Security Checkpoint。策略是一个 context 内先访问
首页取得验证会话，然后使用站点自身的筛选 URL 按年份分页抓取。

这里刻意不用“无限滚动猜测加载完成”，因为 Elkjøp 的电视列表实际是分页形态；
类目页会显示类似 ``1–48 av 231 produkter`` 的范围文本，可以作为每页与全年份
总量校验依据，避免只抓到第一页就误判完成。
"""
from __future__ import annotations

import asyncio
import os
import re
from typing import Sequence
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit

from .base import BaseCatalogAdapter, CatalogItem
from monitor_prices.core import clean_price, handle_antibot_page
from monitor_prices.fx import ECB_RATE_DATE, price_to_eur
from monitor_prices.adapters.elkjop import ElkjopAdapter

LISTING_URL = "https://www.elkjop.no/tv-lyd-og-smarte-hjem/tv-og-tilbehor/tv"
HOME_URL = "https://www.elkjop.no/"
MAX_ITEMS = int(os.environ.get("ELKJOP_MAX_ITEMS", "0"))
PAGE_SIZE = int(os.environ.get("ELKJOP_PAGE_SIZE", "48"))
TARGET_YEARS = (2025, 2026)
FILTER_BRANDS = ("Hisense", "LG", "iFfalcon", "Samsung", "Sony", "TCL")
TARGET_BRANDS = ("Samsung", "LG", "Sony", "TCL", "Hisense", "iFfalcon")
BRAND_PARAM = "1|brand[]"
YEAR_PARAM = "1|attributes.33627[]"

RE_SIZE = re.compile(r"(?<!\d)(\d{2,3})\s*(?:[\"”″]|tommer|inch)", re.IGNORECASE)
RE_SKU = re.compile(r"/(\d{5,9})(?:[/?#]|$)")
RE_YEAR = re.compile(r"\b(2025|2026)\b")
RE_RANGE = re.compile(r"(\d+)\s*[–-]\s*(\d+)\s+av\s+(\d+)\s+produkter", re.IGNORECASE)

_JS_EXTRACT = r"""
() => {
  const result = {};
  document.querySelectorAll(
    "a[data-testid='product-card'][href*='/product/tv-lyd-og-smarte-hjem/tv-og-tilbehor/tv/']"
  ).forEach(a => {
    const href = a.href || a.getAttribute("href") || "";
    const m = href.match(/\/(\d{5,9})(?:[/?#]|$)/);
    if (!m) return;
    const card = a;
    const text = ((card && card.innerText) || a.innerText || a.getAttribute("aria-label") || "")
      .trim().replace(/\s+/g, " ");
    const heading = card && card.querySelector("h2, h3, [data-testid*='title']");
    const title = (
      (heading && heading.innerText) ||
      a.getAttribute("title") ||
      a.getAttribute("aria-label") ||
      a.innerText ||
      ""
    )
      .trim().replace(/\s+/g, " ");
    const priceNode = card && card.querySelector(
      ".row-span-2 .inc-vat, span.font-headline.inc-vat"
    );
    const currentPrice = (priceNode && priceNode.innerText || "").trim();
    if (!result[m[1]] || text.length > result[m[1]].text.length) {
      result[m[1]] = {sku: m[1], href, title, text, currentPrice};
    } else if (currentPrice && !result[m[1]].currentPrice) {
      result[m[1]].currentPrice = currentPrice;
    }
  });
  return Object.values(result);
}
"""

_JS_RANGE_TEXT = r"""
() => {
  const pattern = /\d+\s*[–-]\s*\d+\s+av\s+\d+\s+produkter/i;
  const candidates = Array.from(document.querySelectorAll("main *"))
    .map(el => (el.innerText || "").trim().replace(/\s+/g, " "))
    .filter(Boolean);
  return candidates.find(text => pattern.test(text)) || "";
}
"""


def _query_for_year(year: int) -> str:
    """生成 Elkjøp 筛选查询串，保留站点要求的参数名结构。"""
    pairs = [(BRAND_PARAM, brand) for brand in FILTER_BRANDS]
    pairs.append((YEAR_PARAM, str(year)))
    return urlencode(pairs)


def _page_url(year: int, page_no: int = 1) -> str:
    suffix = "" if page_no <= 1 else f"/page-{page_no}"
    return f"{LISTING_URL}{suffix}?{_query_for_year(year)}"


def _canonical_url(url: str) -> str:
    absolute = urljoin(HOME_URL, url or "")
    parts = urlsplit(absolute)
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def _brand_from_text(text: str) -> str:
    if re.search(r"\biF+ALCON\b|\biFfalcon\b", text or "", re.IGNORECASE):
        # iFFALCON 是 TCL 旗下/并列筛选项，业务上归入 TCL 口径，同时在 extra 保留来源品牌。
        return "TCL"
    for brand in TARGET_BRANDS:
        if brand.lower() == "iffalcon":
            continue
        if re.search(rf"\b{re.escape(brand)}\b", text or "", re.IGNORECASE):
            return brand
    return ""


def _source_brand_from_text(text: str) -> str:
    """保留页面原始品牌，便于后续检查 iFFALCON 与 TCL 的归并。"""
    if re.search(r"\biF+ALCON\b|\biFfalcon\b", text or "", re.IGNORECASE):
        return "iFFALCON"
    return _brand_from_text(text)


def _size_from_text(text: str) -> float | None:
    m = RE_SIZE.search(text or "")
    if not m:
        return None
    size = int(m.group(1))
    return float(size) if 24 <= size <= 120 else None


def _year_from_text(text: str) -> int | None:
    m = RE_YEAR.search(text or "")
    return int(m.group(1)) if m else None


def _parse_range(text: str) -> tuple[int, int, int] | None:
    m = RE_RANGE.search(text or "")
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def _price_nok(text: str) -> float | None:
    """解析商品卡含税当前价，如 24 990.- / 24990,-；不读取 Før/Outlet 价格。"""
    if not text:
        return None
    parsed = clean_price(f"{text} NOK")
    if not parsed:
        return None
    value = parsed[0]
    return value if 100 <= value <= 500_000 else None


class ElkjopCatalogAdapter(BaseCatalogAdapter):
    platform_name = "Elkjop"
    country = "NO"
    locale_override = ("nb-NO", "Europe/Oslo")

    async def _open_and_pass_checkpoint(self, page, url: str, label: str) -> bool:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=120000)
        except Exception as exc:
            print(f"    [Elkjop] {label} 导航异常: {str(exc)[:100]}")
        return await handle_antibot_page(page, label, max_waits=24, wait_seconds=5.0)

    async def _accept_cookies(self, page) -> None:
        for sel in ("#onetrust-accept-btn-handler", "button:has-text('Godta alle')"):
            try:
                if await page.is_visible(sel, timeout=1500):
                    await page.click(sel)
                    break
            except Exception:
                pass

    async def _wait_for_listing_page(
        self,
        page,
        *,
        year: int,
        page_no: int,
        attempts: int = 90,
    ) -> tuple[tuple[int, int, int] | None, int]:
        """等待商品卡和页码范围文本进入稳定状态。"""
        expected_start = (page_no - 1) * PAGE_SIZE + 1
        last_range: tuple[int, int, int] | None = None
        last_count = 0
        for _ in range(attempts):
            range_text = await page.evaluate(_JS_RANGE_TEXT)
            last_range = _parse_range(range_text)
            last_count = await page.locator("a[data-testid='product-card']").count()
            if last_count > 0 and last_range and last_range[0] == expected_start:
                return last_range, last_count
            await asyncio.sleep(1.0)
        print(
            f"    [Elkjop] {year} 第 {page_no} 页等待超时: "
            f"range={last_range}, cards={last_count}"
        )
        return last_range, last_count

    async def _scrape_listing_page(
        self,
        page,
        *,
        year: int,
        page_no: int,
    ) -> tuple[list[dict], tuple[int, int, int] | None]:
        url = _page_url(year, page_no)
        if not await self._open_and_pass_checkpoint(page, url, f"Elkjop {year} page {page_no}"):
            raise RuntimeError(f"Elkjop {year} 第 {page_no} 页安全检查未通过")
        await self._accept_cookies(page)

        page_range, card_count = await self._wait_for_listing_page(page, year=year, page_no=page_no)
        cards = await page.evaluate(_JS_EXTRACT)
        cards = cards or []
        expected_count = None
        if page_range:
            expected_count = page_range[1] - page_range[0] + 1
        if expected_count is not None and len(cards) != expected_count:
            # 客户端渲染偶尔先出现范围文本、后补齐卡片；再等一轮降低误报。
            await asyncio.sleep(2.5)
            cards = await page.evaluate(_JS_EXTRACT) or []
        if expected_count is not None and len(cards) != expected_count:
            raise RuntimeError(
                f"Elkjop {year} 第 {page_no} 页数量不一致: "
                f"范围要求 {expected_count}，DOM 卡片 {len(cards)} / locator {card_count}"
            )
        print(
            f"    [Elkjop] {year} 第 {page_no} 页: "
            f"range={page_range or '未知'}，商品卡 {len(cards)}"
        )
        return cards, page_range

    async def _fill_missing_prices(self, page, cards: Sequence[dict]) -> None:
        """对目标年份/品牌且缺少卡片价的商品，逐个打开 PDP 补当前价格。"""
        missing_price_cards = []
        for card in cards:
            raw = " ".join(filter(None, (card.get("title"), card.get("text")))).strip()
            if (
                _brand_from_text(raw)
                and (card.get("filter_year") or _year_from_text(raw)) in TARGET_YEARS
                and not _price_nok(card.get("currentPrice") or "")
            ):
                missing_price_cards.append(card)

        if missing_price_cards:
            print(f"    [Elkjop] 发现 {len(missing_price_cards)} 条缺少卡片价，开始 PDP 补价")
        for idx, card in enumerate(missing_price_cards, start=1):
            detail = await page.context.new_page()
            try:
                url = _canonical_url(card.get("href") or "")
                await detail.goto(url, wait_until="domcontentloaded", timeout=120000)
                await handle_antibot_page(detail, "Elkjop PDP", max_waits=24, wait_seconds=5.0)
                result = await ElkjopAdapter().extract_price(detail)
                if result and result[1] == "NOK":
                    card["currentPrice"] = f"{result[0]} kr"
                print(f"    [Elkjop] PDP 补价 {idx}/{len(missing_price_cards)}: {card.get('sku')}")
            except Exception as exc:
                print(f"    [Elkjop] PDP 补价失败: {str(exc)[:100]}")
            finally:
                await detail.close()

    async def fetch_catalog(self, page) -> Sequence[CatalogItem]:
        print("    [Elkjop] 预热首页并等待安全检查")
        if not await self._open_and_pass_checkpoint(page, HOME_URL, "Elkjop warmup"):
            print("    [Elkjop] 首页安全检查未通过")
            return []

        by_year_sku: dict[int, dict[str, dict]] = {year: {} for year in TARGET_YEARS}
        expected_totals: dict[int, int] = {}
        for year in TARGET_YEARS:
            page_no = 1
            total_pages = None
            while True:
                cards, page_range = await self._scrape_listing_page(page, year=year, page_no=page_no)
                if page_range:
                    expected_totals[year] = page_range[2]
                    total_pages = (page_range[2] + PAGE_SIZE - 1) // PAGE_SIZE
                for card in cards:
                    sku = card.get("sku")
                    if not sku:
                        continue
                    card["filter_year"] = year
                    by_year_sku[year][sku] = card
                if MAX_ITEMS and sum(len(v) for v in by_year_sku.values()) >= MAX_ITEMS:
                    break
                if total_pages is None:
                    raise RuntimeError(f"Elkjop {year} 未识别到分页总量，停止以避免半量数据")
                if page_no >= total_pages:
                    break
                page_no += 1
            actual = len(by_year_sku[year])
            expected = expected_totals.get(year)
            if expected is not None and not MAX_ITEMS and actual != expected:
                raise RuntimeError(f"Elkjop {year} 总量校验失败: 页面标称 {expected}，实际唯一 SKU {actual}")
            print(f"    [Elkjop] {year} 年抓取完成: {actual}/{expected or '未知'}")

        by_sku_year: dict[tuple[int, str], dict] = {}
        for year, cards_by_sku in by_year_sku.items():
            for sku, card in cards_by_sku.items():
                by_sku_year[(year, sku)] = card

        await self._fill_missing_prices(page, list(by_sku_year.values()))

        items: list[CatalogItem] = []
        for (filter_year, sku), card in by_sku_year.items():
            raw = " ".join(filter(None, (card.get("title"), card.get("text")))).strip()
            brand = _brand_from_text(raw)
            if not brand:
                continue
            model_year = _year_from_text(raw) or filter_year
            if model_year not in TARGET_YEARS:
                continue
            url = _canonical_url(card.get("href") or "")
            if not RE_SKU.search(url):
                continue
            price_nok = _price_nok(card.get("currentPrice") or "")
            price_eur = price_to_eur(price_nok, "NOK")
            items.append(CatalogItem(
                brand_raw=brand,
                raw_text=raw,
                url=url,
                size_hint_inch=_size_from_text(raw),
                price_hint_eur=price_eur,
                price_local=price_nok,
                currency="NOK",
                price_eur=price_eur,
                extra={
                    "elkjop_sku": sku,
                    "model_year": model_year,
                    "filter_year": filter_year,
                    "source_brand": _source_brand_from_text(raw),
                    "fx_rate_date": ECB_RATE_DATE,
                },
            ))
            if MAX_ITEMS and len(items) >= MAX_ITEMS:
                break
        print(f"[catalog/Elkjop] 提取 {len(items)} 个目标品牌电视")
        return items
