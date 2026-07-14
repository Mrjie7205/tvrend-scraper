"""Elkjøp 挪威电视类目抓取。

Elkjøp 的签名 key 接口会拦截没有真实页面会话的 HTTP 客户端，因此先用 Chromium
打开首页，再从站内页面环境发起同源请求取得短时效 Algolia key。目录数据仍直接
调用 Elkjøp 前端实际使用的 Algolia 搜索接口，并用 ``nbHits``、``nbPages`` 和唯一
SKU 数做严格完整性校验。

旧的浏览器分页实现保留为可选兜底，仅供本地排障；GitHub Action 的 API-only 是指
目录翻页走 Algolia，不代表签名 key 可以脱离真实浏览器会话。
"""
from __future__ import annotations

import asyncio
from collections import Counter
import json
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
ALGOLIA_KEY_URL = "https://www.elkjop.no/api/algolia/signed-api-key"
ALGOLIA_QUERY_URL = "https://z0fl7r8ubh-dsn.algolia.net/1/indexes/*/queries"
ALGOLIA_APP_ID = "Z0FL7R8UBH"
ALGOLIA_INDEX = "commerce_b2c_OCNOELK"
ALGOLIA_TAXONOMY_FILTER = "productTaxonomy.id:PT351"
MAX_ITEMS = int(os.environ.get("ELKJOP_MAX_ITEMS", "0"))
PAGE_SIZE = int(os.environ.get("ELKJOP_PAGE_SIZE", "48"))
API_ENABLED = os.environ.get("ELKJOP_CATALOG_API", "true").strip().lower() not in {"0", "false", "no"}
PAGE_FALLBACK_ENABLED = os.environ.get(
    "ELKJOP_CATALOG_PAGE_FALLBACK", "false"
).strip().lower() in {"1", "true", "yes"}
MIN_EXPECTED_PER_YEAR = int(os.environ.get("ELKJOP_MIN_EXPECTED_PER_YEAR", "100"))
TARGET_YEARS = (2025, 2026)
FILTER_BRANDS = ("Hisense", "LG", "iFfalcon", "Samsung", "Sony", "TCL")
TARGET_BRANDS = ("Samsung", "LG", "Sony", "TCL", "Hisense", "iFfalcon")
REQUIRED_SOURCE_BRANDS = {"hisense", "lg", "samsung", "sony", "tcl"}
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


def _first(value):
    """Algolia attributes 的值通常是单元素 list；统一取首值。"""
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _api_year(hit: dict) -> int | None:
    value = _first((hit.get("attributes") or {}).get("33627"))
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _api_size(hit: dict) -> float | None:
    value = _first((hit.get("attributes") or {}).get("31323"))
    try:
        size = float(value)
    except (TypeError, ValueError):
        return _size_from_text(str(hit.get("title") or hit.get("name") or ""))
    return size if 24 <= size <= 120 else None


def _api_price(hit: dict) -> float | None:
    price = hit.get("price") or {}
    currency = str(price.get("currency") or "").upper()
    value = price.get("amount")
    if value in (None, ""):
        return None
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    if currency != "NOK":
        raise RuntimeError(
            f"Elkjop Algolia SKU {hit.get('articleNumber')} 币种异常: {currency or '空'}"
        )
    if not 100 <= amount <= 500_000:
        raise RuntimeError(
            f"Elkjop Algolia SKU {hit.get('articleNumber')} 价格异常: {amount} NOK"
        )
    return amount


def _api_raw_text(hit: dict) -> str:
    """保留匹配需要的标题、厂商型号和描述，同时去掉重复片段。"""
    parts = (
        hit.get("title"),
        hit.get("name"),
        hit.get("manufacturerArticleNumber"),
        hit.get("shortDescription"),
    )
    return " ".join(dict.fromkeys(str(p).strip() for p in parts if str(p or "").strip()))


def _api_source_brand(hit: dict) -> str:
    raw = str(hit.get("brand") or "").strip()
    allowed = {b.lower(): b for b in FILTER_BRANDS}
    canonical = allowed.get(raw.lower())
    if not canonical:
        raise RuntimeError(
            f"Elkjop Algolia 返回筛选外品牌: {raw or '空'} "
            f"(SKU={hit.get('articleNumber') or hit.get('objectID')})"
        )
    return "iFFALCON" if canonical.lower() == "iffalcon" else canonical


def _api_business_brand(source_brand: str) -> str:
    return "TCL" if source_brand.lower() == "iffalcon" else source_brand


class ElkjopCatalogAdapter(BaseCatalogAdapter):
    platform_name = "Elkjop"
    country = "NO"
    locale_override = ("nb-NO", "Europe/Oslo")
    # Vercel 会联合检查 UA、Client Hints 与平台；随机 Windows/Safari UA 配 Linux
    # Chromium 反而是明显异常，因此 Elkjop 使用浏览器原生一致身份。
    native_browser_identity = True

    async def _signed_api_key(self, page) -> str:
        """在真实首页会话中取短时效搜索 key。

        Vercel 会对 ``APIRequestContext`` 直连接口返回 Security Checkpoint。页面内的
        ``fetch`` 会携带真实 Chromium 指纹、同源 Referer 和站点 Cookie，与 Elkjøp
        前端自己的调用方式一致。
        """
        print("    [Elkjop/api] 预热首页并建立签名 key 会话")
        if not await self._open_and_pass_checkpoint(page, HOME_URL, "Elkjop key warmup"):
            raise RuntimeError("Elkjop 首页安全检查未通过，无法获取 Algolia signed key")
        await self._accept_cookies(page)

        last_error = ""
        for attempt in range(1, 4):
            try:
                result = await page.evaluate(
                    """async (url) => {
                        const response = await fetch(url, {
                            method: "GET",
                            credentials: "include",
                            headers: {"accept": "application/json"},
                        });
                        const text = await response.text();
                        let apiKey = "";
                        try {
                            const payload = JSON.parse(text);
                            apiKey = String(payload.apiKey || "").trim();
                        } catch (_) {}
                        return {
                            status: response.status,
                            apiKey,
                            checkpoint: text.includes("Vercel Security Checkpoint"),
                        };
                    }""",
                    ALGOLIA_KEY_URL,
                )
                key = str(result.get("apiKey") or "").strip()
                if int(result.get("status") or 0) == 200 and key:
                    print("    [Elkjop/api] signed key 获取成功")
                    return key
                suffix = " Vercel Security Checkpoint" if result.get("checkpoint") else ""
                last_error = f"HTTP {result.get('status')}{suffix}"
            except Exception as exc:
                last_error = str(exc)[:120]
            if attempt < 3:
                # 短时波动时给 Vercel/页面会话留出恢复时间，不连续轰击 key 接口。
                await asyncio.sleep(2 ** attempt)
        raise RuntimeError(f"Elkjop Algolia signed key 获取失败: {last_error}")

    @staticmethod
    def _algolia_payload(year: int, page_index: int) -> dict:
        return {
            "requests": [{
                "indexName": ALGOLIA_INDEX,
                "query": "",
                "facetFilters": [
                    [f"attributes.33627:{year}"],
                    [f"brand:{brand}" for brand in FILTER_BRANDS],
                ],
                "filters": ALGOLIA_TAXONOMY_FILTER,
                "attributesToRetrieve": [
                    "articleNumber",
                    "objectID",
                    "brand",
                    "title",
                    "name",
                    "manufacturerArticleNumber",
                    "productUrl",
                    "urlB2C",
                    "price",
                    "attributes",
                    "shortDescription",
                    "isOnline",
                    "isBuyableOnline",
                    "onlineSalesStatus",
                ],
                "hitsPerPage": PAGE_SIZE,
                "page": page_index,
                "analytics": False,
                "clickAnalytics": False,
            }],
        }

    async def _algolia_page(
        self,
        request_context,
        *,
        api_key: str,
        year: int,
        page_index: int,
    ) -> dict:
        last_error = ""
        for attempt in range(1, 4):
            try:
                response = await request_context.post(
                    ALGOLIA_QUERY_URL,
                    headers={
                        "content-type": "application/json",
                        "x-algolia-application-id": ALGOLIA_APP_ID,
                        "x-algolia-api-key": api_key,
                    },
                    data=json.dumps(self._algolia_payload(year, page_index)),
                    timeout=30_000,
                )
                if response.ok:
                    payload = await response.json()
                    results = payload.get("results") or []
                    if results and isinstance(results[0], dict):
                        return results[0]
                    last_error = "响应缺少 results[0]"
                else:
                    last_error = f"HTTP {response.status}"
            except Exception as exc:
                last_error = str(exc)[:120]
            if attempt < 3:
                await asyncio.sleep(attempt)
        raise RuntimeError(
            f"Elkjop Algolia {year} page={page_index} 获取失败: {last_error}"
        )

    async def _fetch_catalog_api(self, page) -> Sequence[CatalogItem]:
        api_key = await self._signed_api_key(page)
        request_context = page.context.request
        by_year_sku: dict[int, dict[str, dict]] = {}
        source_brand_counts: Counter[str] = Counter()
        total_with_price = 0

        for year in TARGET_YEARS:
            first = await self._algolia_page(
                request_context, api_key=api_key, year=year, page_index=0
            )
            expected = int(first.get("nbHits") or 0)
            total_pages = int(first.get("nbPages") or 0)
            if expected < MIN_EXPECTED_PER_YEAR or total_pages < 1:
                raise RuntimeError(
                    f"Elkjop Algolia {year} 总量异常: nbHits={expected}, nbPages={total_pages}"
                )

            hits_by_sku: dict[str, dict] = {}
            for page_index in range(total_pages):
                result = first if page_index == 0 else await self._algolia_page(
                    request_context,
                    api_key=api_key,
                    year=year,
                    page_index=page_index,
                )
                if int(result.get("nbHits") or 0) != expected:
                    raise RuntimeError(
                        f"Elkjop Algolia {year} 分页总量漂移: "
                        f"page={page_index}, {result.get('nbHits')} != {expected}"
                    )
                if int(result.get("nbPages") or 0) != total_pages:
                    raise RuntimeError(
                        f"Elkjop Algolia {year} 页数漂移: "
                        f"page={page_index}, {result.get('nbPages')} != {total_pages}"
                    )
                hits = result.get("hits") or []
                for hit in hits:
                    sku = str(hit.get("articleNumber") or hit.get("objectID") or "").strip()
                    if not sku.isdigit():
                        raise RuntimeError(f"Elkjop Algolia {year} 返回无效 SKU: {sku!r}")
                    if _api_year(hit) != year:
                        raise RuntimeError(
                            f"Elkjop Algolia SKU {sku} 年份异常: {_api_year(hit)} != {year}"
                        )
                    url = _canonical_url(str(hit.get("productUrl") or hit.get("urlB2C") or ""))
                    if not RE_SKU.search(url) or sku not in url:
                        raise RuntimeError(f"Elkjop Algolia SKU {sku} URL 异常: {url}")
                    if sku in hits_by_sku:
                        raise RuntimeError(f"Elkjop Algolia {year} 分页重复 SKU: {sku}")
                    source_brand = _api_source_brand(hit)
                    source_brand_counts[source_brand] += 1
                    hit["_catalog_url"] = url
                    hit["_source_brand"] = source_brand
                    hits_by_sku[sku] = hit
                print(
                    f"    [Elkjop/api] {year} page {page_index + 1}/{total_pages}: "
                    f"{len(hits)} 条，累计 {len(hits_by_sku)}/{expected}"
                )

            if len(hits_by_sku) != expected:
                raise RuntimeError(
                    f"Elkjop Algolia {year} 完整性失败: 唯一 SKU {len(hits_by_sku)} != nbHits {expected}"
                )
            by_year_sku[year] = hits_by_sku
            print(f"    [Elkjop/api] {year} 年抓取完成: {len(hits_by_sku)}/{expected}")

        sku_years: dict[str, set[int]] = {}
        for year, hits in by_year_sku.items():
            for sku in hits:
                sku_years.setdefault(sku, set()).add(year)
        cross_year = {sku: years for sku, years in sku_years.items() if len(years) > 1}
        if cross_year:
            sample_sku, sample_years = next(iter(cross_year.items()))
            raise RuntimeError(
                f"Elkjop Algolia 跨年份重复 SKU {len(cross_year)} 条，"
                f"示例 {sample_sku}:{sorted(sample_years)}"
            )

        seen_required = {brand.lower() for brand in source_brand_counts}
        missing_brands = sorted(REQUIRED_SOURCE_BRANDS - seen_required)
        if missing_brands:
            raise RuntimeError(f"Elkjop Algolia 品牌覆盖缺失: {missing_brands}")

        items: list[CatalogItem] = []
        for year in TARGET_YEARS:
            for sku, hit in by_year_sku[year].items():
                source_brand = hit["_source_brand"]
                price_nok = _api_price(hit)
                if price_nok is not None:
                    total_with_price += 1
                price_eur = price_to_eur(price_nok, "NOK")
                items.append(CatalogItem(
                    brand_raw=_api_business_brand(source_brand),
                    raw_text=_api_raw_text(hit),
                    url=hit["_catalog_url"],
                    size_hint_inch=_api_size(hit),
                    price_hint_eur=price_eur,
                    price_local=price_nok,
                    currency="NOK",
                    price_eur=price_eur,
                    extra={
                        "elkjop_sku": sku,
                        "model_year": year,
                        "filter_year": year,
                        "source_brand": source_brand,
                        "fx_rate_date": ECB_RATE_DATE,
                    },
                ))

        print(
            f"[catalog/Elkjop/api] 完整性通过: {len(items)} 唯一 SKU，"
            f"{total_with_price} 条带价，品牌={dict(sorted(source_brand_counts.items()))}"
        )
        return items[:MAX_ITEMS] if MAX_ITEMS else items

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

    async def _fetch_catalog_page(self, page) -> Sequence[CatalogItem]:
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

    async def fetch_catalog(self, page) -> Sequence[CatalogItem]:
        if API_ENABLED:
            try:
                return await self._fetch_catalog_api(page)
            except Exception as exc:
                print(f"[catalog/Elkjop/api] 抓取失败: {str(exc)[:200]}")
                if not PAGE_FALLBACK_ENABLED:
                    raise
                print("[catalog/Elkjop] 启用网页分页兜底")
        return await self._fetch_catalog_page(page)
