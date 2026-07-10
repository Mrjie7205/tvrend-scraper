"""Boulanger 类目页反向拉:抓 boulanger.com 电视分类下 5 大品牌的全量商品。

抓取策略(2026-06 实测确定):
- 入口 = 品牌 facet `/c/televiseur/brand~<brand>`(总类目只显示精选 ~47,不全)
- Boulanger 是纯客户端渲染(CSR),plain requests 拿不到商品 → 必须用 Playwright
- 每个 facet 用 `?numPage=N` 分页:每页 ~40-49 个商品,Samsung 这种大类有 ~155 个
  共 4-5 页。循环到某页"无新增 ref"为止(带安全上限 MAX_PAGES)
- 每页内先滚到底触发懒加载,再用一段 JS 一次性抓 (href, 商品名文本, 价格)

DOM 关键点:
- 商品链接 = `a[href*='/ref/']`,/ref/<id> 是 Boulanger 商品页规范 URL
- 同一商品有多个 <a/ref/>:图片(空 text)、标题(商品名)、评分(#avis 锚点,"4,6/5 (16)")
  → 按 URL 去 fragment 合并,从候选文本里挑"最像商品名"的(TV 开头优先)
- 价格在父 product card 里,JS 用 closest() 找

匹配的脏活留给 下游匹配环节,这里只交付原始文本 + URL + 价格。
"""
from __future__ import annotations

import asyncio
import re
from typing import Sequence

from .base import BaseCatalogAdapter, CatalogItem

# 品牌 facet 入口(我们关心的 5 大品牌)
BRAND_FACETS = ("samsung", "tcl", "lg", "hisense", "sony")

def _facet_url(brand: str, page: int) -> str:
    base = f"https://www.boulanger.com/c/televiseur/brand~{brand}"
    return base if page <= 1 else f"{base}?numPage={page}"

COOKIE_ACCEPT_SELECTOR = "#onetrust-accept-btn-handler"
PRODUCT_LINK_SELECTOR = "a[href*='/ref/']"

MAX_PAGES = 8          # 每品牌最多翻 8 页(~155 个商品 ≈ 4 页,留余量)
MAX_SCROLL_ROUNDS = 12  # 每页滚动触发懒加载的最大轮数

# 噪声过滤
RE_HAS_MODEL_LIKE = re.compile(r"[A-Z]\d|\d[A-Z]")  # 字母数字相邻 → 像 model name
RE_RATING_NOISE = re.compile(r"^\d[,.]\d/5\s*\(")    # "4,6/5 (16)"

# spec 描述前缀(这些不是商品名,是 hover tooltip)
SPEC_DESC_PREFIXES = (
    "fréquence", "frequence", "résolution", "resolution", "consommation",
    "rétroéclair", "retroeclair", "balayage", "luminosité", "luminosite", "native",
)

# 品牌识别白名单(从商品名里抠;TCL/LG 全大写,其他首字母大写)
BRAND_HINTS = (
    "SAMSUNG", "HISENSE", "SONY", "TCL", "LG",
    "PHILIPS", "PANASONIC", "GRUNDIG", "THOMSON", "SHARP", "BLAUPUNKT",
)

RE_INCH_POUCES = re.compile(r"(\d{2,3})\s*pouces?\b", re.IGNORECASE)
RE_INCH_QUOTE = re.compile(r"(\d{2,3})\s*[\"'']")
RE_CM = re.compile(r"(\d{2,3})\s*cm\b", re.IGNORECASE)


def _extract_brand(text: str) -> str:
    up = text.upper()
    for b in BRAND_HINTS:
        if b in up:
            return b if b in ("TCL", "LG") else b.title()
    return ""


def _extract_size_inch(text: str) -> float | None:
    m = RE_INCH_POUCES.search(text)
    if m:
        v = int(m.group(1))
        if 17 <= v <= 150:
            return float(v)
    m = RE_INCH_QUOTE.search(text)
    if m:
        v = int(m.group(1))
        if 17 <= v <= 150:
            return float(v)
    m = RE_CM.search(text)
    if m:
        v = int(m.group(1)) / 2.54
        if 17 <= v <= 150:
            return round(v)
    return None


# 同一 product card 一次性抓 (href, text, price) 的 JS
_JS_EXTRACT = r"""
() => {
  const out = [];
  document.querySelectorAll('a[href*="/ref/"]').forEach(a => {
    if (a.offsetParent === null) return;
    const href = a.getAttribute('href');
    const text = (a.innerText || '').trim().replace(/\n/g, ' ');
    const card = a.closest(
      'article, [class*="product-card"], [class*="ProductCard"], ' +
      '[data-test*="product"], [class*="product-item"], li[class*="product"]'
    );
    let price = '';
    if (card) {
      // 旧实现直接取第一个 .price__amount，促销卡常会先遇到划线原价。
      // 与 PDP adapter 对齐：优先 price__main，并排除 crossed/old 与 line-through。
      const isCurrent = el => {
        const style = window.getComputedStyle(el);
        return !style.textDecoration.includes('line-through') &&
          !el.closest('.price__crossed, .price__old, [class*="crossed"], [class*="old-price"]');
      };
      const mainCandidates = [...card.querySelectorAll('.price__main .price__amount')];
      const fallbackCandidates = [...card.querySelectorAll(
        '.price__amount, [class*="-price__amount"], [class*="price-amount"], ' +
        'span[class*="Price"], [data-test*="price"]'
      )];
      const priceEl = mainCandidates.find(isCurrent) ||
        fallbackCandidates.find(el => isCurrent(el) && el.children.length === 0);
      if (priceEl) price = (priceEl.innerText || '').trim().replace(/\n/g, ' ');
    }
    out.push({href, text, price});
  });
  return out;
}
"""


class BoulangerCatalogAdapter(BaseCatalogAdapter):
    platform_name = "Boulanger"
    country = "FR"
    locale_override = ("fr-FR", "Europe/Paris")

    async def _scroll_to_load(self, page) -> int:
        """滚到底直到 /ref/ 链接数稳定。返回最终链接数。"""
        prev = -1
        stable = 0
        for _ in range(MAX_SCROLL_ROUNDS):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(1.5)
            cur = await page.locator(PRODUCT_LINK_SELECTOR).count()
            if cur == prev:
                stable += 1
                if stable >= 3:
                    break
            else:
                stable = 0
            prev = cur
        return prev

    async def _scrape_page(self, page, url: str, by_url: dict) -> int:
        """抓单个 numPage 页,累加 (canonical_url → list[(text, price)]) 到 by_url。
        返回这一页新增的 URL 数。"""
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print(f"    goto 失败: {str(e)[:80]}")
            return 0
        await asyncio.sleep(1.5)

        # cookie(只第一次有)
        try:
            if await page.is_visible(COOKIE_ACCEPT_SELECTOR, timeout=1500):
                await page.click(COOKIE_ACCEPT_SELECTOR)
                await asyncio.sleep(0.8)
        except Exception:
            pass

        await self._scroll_to_load(page)

        try:
            extracted = await page.evaluate(_JS_EXTRACT)
        except Exception as e:
            print(f"    JS 抓取失败: {str(e)[:80]}")
            return 0

        n_added = 0
        for item in extracted:
            href = item.get("href") or ""
            if not href:
                continue
            if not href.startswith("http"):
                href = "https://www.boulanger.com" + href
            canonical = href.split("#", 1)[0]
            text = (item.get("text") or "").strip()
            price = (item.get("price") or "").strip()
            if canonical not in by_url:
                n_added += 1
            by_url.setdefault(canonical, []).append((text, price))
        return n_added

    async def fetch_catalog(self, page) -> Sequence[CatalogItem]:
        by_url: dict[str, list[tuple[str, str]]] = {}
        for brand in BRAND_FACETS:
            print(f"[catalog/Boulanger] === brand={brand} ===")
            for pg in range(1, MAX_PAGES + 1):
                url = _facet_url(brand, pg)
                n_added = await self._scrape_page(page, url, by_url)
                print(f"    numPage={pg}: 新增 {n_added} URL(累计 {len(by_url)})")
                if n_added == 0 and pg > 1:
                    break  # 这一页没新增 → 该品牌翻完了
        print(f"[catalog/Boulanger] 5 品牌 × 分页跑完,共 {len(by_url)} 个 URL")

        return self._build_items(by_url)

    # ---------- 候选文本 → CatalogItem ----------
    @staticmethod
    def _pick_product_name(entries: list[tuple[str, str]]) -> str | None:
        best, best_score = None, -1
        for t, _ in entries:
            if len(t) < 8 or len(t) > 200:
                continue
            tl = t.lower()
            if any(tl.startswith(p) for p in SPEC_DESC_PREFIXES):
                continue
            if RE_RATING_NOISE.match(t):
                continue
            if not RE_HAS_MODEL_LIKE.search(t.upper()):
                continue
            score = 0
            if tl.startswith("tv "):
                score += 100
            if "tv" in tl[:10]:
                score += 30
            if 25 <= len(t) <= 100:
                score += 20
            if score > best_score:
                best, best_score = t, score
        return best

    @staticmethod
    def _pick_price_eur(entries: list[tuple[str, str]]) -> float | None:
        from collections import Counter
        prices = []
        for _, p in entries:
            if not p:
                continue
            cleaned = (p.replace("\xa0", " ").replace("€", "").replace("EUR", "")
                       .replace(" ", "").replace(",", ".").strip())
            m = re.search(r"\d+(?:\.\d+)?", cleaned)
            if not m:
                continue
            try:
                val = float(m.group(0))
                if 50 <= val <= 50000:
                    prices.append(val)
            except ValueError:
                pass
        if not prices:
            return None
        # 同一 card 里通常有划线原价 + 现价,取最小(现价)
        return min(prices)

    def _build_items(self, by_url: dict[str, list[tuple[str, str]]]) -> list[CatalogItem]:
        items: list[CatalogItem] = []
        n_no_name = 0
        for url, entries in by_url.items():
            name = self._pick_product_name(entries)
            if not name:
                n_no_name += 1
                continue
            items.append(CatalogItem(
                brand_raw=_extract_brand(name),
                raw_text=name,
                url=url,
                size_hint_inch=_extract_size_inch(name),
                price_hint_eur=self._pick_price_eur(entries),
            ))
        n_priced = sum(1 for it in items if it.price_hint_eur is not None)
        print(f"[catalog/Boulanger] {len(by_url)} URL · {len(items)} 商品 "
              f"({n_priced} 带价格 / 跳过 {n_no_name} 无名 URL)")
        return items
