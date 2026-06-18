"""Currys 类目页反向拉:抓 currys.co.uk 电视分类下全量商品。

抓取策略(2026-06 实测确定):
- 入口 = /tv-and-audio/televisions/tvs?start=N&sz=20(总 ~494 台, 每页 20, ~25 页)
- Currys 是客户端渲染,plain requests 拿不到商品 → 必须用 Playwright
- 反爬特性:**同一 browser context 翻第 2 页就 403**(会话级限速),
  但每页换一个全新 context 直开 start=N 就 200。所以这里 **每页开新 context**。
- 翻页 URL 由 Currys 自己生成:?start=0/20/40/...&sz=20。循环到某页无新增或够 total 为止。

DOM 关键点:
- 商品链接 = a[href*='/products/'],/products/<slug> 是 Currys 商品页规范 URL
- 标题已含型号:'SAMSUNG S90F 65" OLED 4K... - QE65S90F' / 'LG C5 ... - OLED65C54LA'
  → 品牌打头,型号在末尾;直接当 raw_text 交给 下游匹配环节(它按品牌正则抠型号)
- 价格在 product card 里,£NNN

匹配的脏活留给 下游匹配环节,这里只交付原始标题 + URL + 价格(英镑)。
"""
from __future__ import annotations

import asyncio
import os
import random
import re
from typing import Sequence

from .base import BaseCatalogAdapter, CatalogItem

LISTING_URL = "https://www.currys.co.uk/tv-and-audio/televisions/tvs"
PAGE_SIZE = 20
# 安全上限:~494/20≈25 页,留余量。测试可用环境变量 CURRYS_MAX_PAGES 调小。
MAX_PAGES = int(os.environ.get("CURRYS_MAX_PAGES", "30"))
COOKIE_ACCEPT_SELECTOR = "#onetrust-accept-btn-handler"

# 品牌识别(取标题第一个词;我们追踪 5 大,其余照样交出去由匹配器判 no_brand)
_KNOWN_BRANDS = {
    "SAMSUNG": "Samsung", "HISENSE": "Hisense", "SONY": "Sony", "TCL": "TCL", "LG": "LG",
    "PHILIPS": "Philips", "PANASONIC": "Panasonic", "TOSHIBA": "Toshiba", "JVC": "JVC",
    "SHARP": "Sharp", "BLAUPUNKT": "Blaupunkt", "HITACHI": "Hitachi", "AMAZON": "Amazon",
}
# 尺寸:65" / 55” / 75-inch(含 ASCII 与花引号)
RE_SIZE = re.compile(r"(\d{2,3})\s*(?:[\"”″'']|-?\s*inch)", re.IGNORECASE)
RE_TOTAL = re.compile(r"of\s+(\d+)", re.IGNORECASE)

# 一次性抓本页全部 (slug, 标题, 价格, href):按 slug 合并(图片链接 text 空,取最长标题)
_JS_EXTRACT = r"""
() => {
  const bySlug = {};
  document.querySelectorAll("a[href*='/products/']").forEach(a => {
    const href = a.getAttribute('href') || '';
    const m = href.match(/\/products\/([^/?#]+)/);
    if (!m) return;
    const slug = m[1];
    const title = (a.innerText || '').trim().replace(/\s+/g, ' ');
    const card = a.closest("article, li, [class*='product'], [data-testid*='product']");
    let price = '';
    if (card) {
      const pm = (card.innerText || '').match(/£[\d.,]+/);
      if (pm) price = pm[0];
    }
    if (!bySlug[slug]) bySlug[slug] = { slug, title: '', price: '', href: href.split('?')[0] };
    if (title.length > bySlug[slug].title.length) bySlug[slug].title = title;
    if (price && !bySlug[slug].price) bySlug[slug].price = price;
  });
  return Object.values(bySlug);
}
"""


def _brand_from_slug(slug: str) -> str:
    """品牌取 slug 第一段(干净):'samsung-s90f-...' → Samsung。
    比标题可靠 —— 标题 innerText 常被 'Save £200' / 'Get it tomorrow' 促销前缀污染。"""
    first = (slug or "").split("-", 1)[0].upper()
    return _KNOWN_BRANDS.get(first, first.title() if first else "")


def _raw_from_slug(slug: str) -> str:
    """从 slug 重建商品名:最干净的型号来源(标题 innerText 常被促销浮层污染)。
    'samsung-s90f-65-oled-...-qe65s90f-102' → 'samsung s90f 65 oled ... qe65s90f'
    (去掉尾部纯数字的商品 id)。匹配器型号正则大小写不敏感,无需还原大小写。"""
    toks = [t for t in (slug or "").split("-") if t]
    while toks and toks[-1].isdigit():  # 尾部商品 id
        toks.pop()
    return " ".join(toks)


def _size_from_slug(slug: str) -> float | None:
    """slug 里第一个落在电视尺寸区间(24-120)的 2-3 位整数(年份是 4 位不会命中)。"""
    for t in (slug or "").split("-"):
        if t.isdigit() and 2 <= len(t) <= 3:
            v = int(t)
            if 24 <= v <= 120:
                return float(v)
    return None


def _extract_size_inch(title: str) -> float | None:
    m = RE_SIZE.search(title or "")
    if m:
        v = int(m.group(1))
        if 17 <= v <= 150:
            return float(v)
    return None


def _clean_price_gbp(text: str) -> float | None:
    if not text:
        return None
    cleaned = text.replace("£", "").replace(",", "").strip()
    m = re.search(r"\d+(?:\.\d+)?", cleaned)
    if not m:
        return None
    try:
        v = float(m.group(0))
        return v if 50 <= v <= 50000 else None
    except ValueError:
        return None


class CurrysCatalogAdapter(BaseCatalogAdapter):
    platform_name = "Currys"
    country = "GB"
    locale_override = ("en-GB", "Europe/London")

    async def _new_context(self, browser):
        """每页一个全新 context(干净 cookies/指纹)以规避 Currys 会话级 403。"""
        # 复用项目的 stealth + UA 池;懒导入避免 import 期依赖 sys.path
        from monitor_prices.core import STEALTH_JS, USER_AGENTS, VIEWPORT_HEIGHTS, VIEWPORT_WIDTHS
        ctx = await browser.new_context(
            user_agent=random.choice(USER_AGENTS),
            viewport={"width": random.choice(VIEWPORT_WIDTHS), "height": random.choice(VIEWPORT_HEIGHTS)},
            locale="en-GB",
            timezone_id="Europe/London",
        )
        await ctx.add_init_script(STEALTH_JS)
        return ctx

    async def _scrape_page(self, browser, start: int) -> tuple[int, list[dict]]:
        """开新 context 抓一页;返回 (http_status, [card dict])。"""
        url = f"{LISTING_URL}?start={start}&sz={PAGE_SIZE}"
        ctx = await self._new_context(browser)
        page = await ctx.new_page()
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=50000)
            status = resp.status if resp else 0
            await page.wait_for_timeout(2800)
            # cookie 弹窗(OneTrust);失败无所谓
            try:
                if await page.is_visible(COOKIE_ACCEPT_SELECTOR, timeout=1200):
                    await page.click(COOKIE_ACCEPT_SELECTOR)
                    await page.wait_for_timeout(500)
            except Exception:
                pass
            if status != 200:
                return status, []
            cards = await page.evaluate(_JS_EXTRACT)
            return status, cards or []
        except Exception as e:
            print(f"    [Currys] start={start} 异常: {str(e)[:90]}")
            return 0, []
        finally:
            await ctx.close()

    async def fetch_catalog(self, page) -> Sequence[CatalogItem]:
        browser = page.context.browser
        by_slug: dict[str, dict] = {}
        consecutive_fail = 0

        for pi in range(MAX_PAGES):
            start = pi * PAGE_SIZE
            status, cards = await self._scrape_page(browser, start)
            # crash / 瞬时失败 → 重试一次(Chromium 偶发 "Page crashed")
            if status != 200:
                await asyncio.sleep(2.5)
                status, cards = await self._scrape_page(browser, start)

            new = 0
            for c in cards:
                slug = c.get("slug")
                if not slug or slug in by_slug:
                    continue
                if len((c.get("title") or "").strip()) < 8:  # 纯图片链接,无标题
                    continue
                by_slug[slug] = c
                new += 1
            print(f"    [Currys] start={start}: status={status} 新增 {new}(累计 {len(by_slug)})")

            if status != 200:
                # 容忍单页失败(跳过试下一页);连续 2 页失败才认为到底/被封
                consecutive_fail += 1
                if consecutive_fail >= 2:
                    break
                await asyncio.sleep(random.uniform(1.5, 3.0))
                continue
            consecutive_fail = 0
            if new == 0 and pi > 0:
                break  # 翻到底
            # 礼貌延时,降低 IP 级限速风险
            await asyncio.sleep(random.uniform(1.2, 2.6))

        print(f"[catalog/Currys] 翻页跑完,共 {len(by_slug)} 个商品")
        return self._build_items(by_slug)

    def _build_items(self, by_slug: dict[str, dict]) -> list[CatalogItem]:
        items: list[CatalogItem] = []
        for slug, c in by_slug.items():
            brand = _brand_from_slug(slug)
            raw = _raw_from_slug(slug)  # slug 重建,干净且含型号
            size = _size_from_slug(slug) or _extract_size_inch(c.get("title") or "")
            href = c.get("href") or ""
            if not href.startswith("http"):
                href = "https://www.currys.co.uk" + href
            items.append(CatalogItem(
                brand_raw=brand,
                raw_text=raw,
                url=href,
                size_hint_inch=size,
                price_hint_eur=_clean_price_gbp(c.get("price") or ""),  # 注:Currys 是 GBP,字段名沿用 schema
            ))
        n_priced = sum(1 for it in items if it.price_hint_eur is not None)
        print(f"[catalog/Currys] {len(items)} 商品({n_priced} 带价格)")
        return items
