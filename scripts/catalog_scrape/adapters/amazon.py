"""Amazon DE 类目页反向拉:抓 amazon.de 上五大品牌的电视清单。

抓取策略(2026-06 实测确定):
- 入口 = 按品牌搜索 /s?k=<brand>+fernseher&page=N(比泛搜 "fernseher" 干净,品牌归属准、每品牌更深)。
- Amazon 是客户端渲染,plain HTTP 只回 ~2KB stub → 必须用 Playwright(run_weekly 已给好 UA+locale+Stealth)。
- 实测 headless Playwright 直接拿到结果页(28 ASIN/页、无 captcha),单 context 翻页也 OK,无需每页换 context。
- Cookie 同意框:#sp-cc-accept。

DOM 关键点:
- 每个结果 = div[data-component-type='s-search-result'],data-asin 是稳定唯一 ID。
- 标题在 h2 span,含品牌+型号+尺寸(如 'Samsung Crystal UHD 4K U7099F 43 Zoll ...')→ 直接当 raw_text 交匹配器。
- 商品 URL 由 ASIN 直接拼 /dp/<ASIN>(比抓卡片链接稳)。
- 广告位('Vorgestellte Produkte von Amazon-Marken' / 'Gesponsert')要剔。
- ⚠ 搜索页价格随连接的配送地 localize(实测从非德 IP 会显示 GBP),仅作 price_hint 去重用,**真实 EUR 价由
  monitor adapter 在 /dp 页设德国配送地后取**。

匹配脏活留给 match_to_truth,这里只交付原始标题 + /dp URL + 价格数字(hint)。
"""
from __future__ import annotations

import asyncio
import os
import random
import re
from typing import Sequence

from .base import BaseCatalogAdapter, CatalogItem

BASE = "https://www.amazon.de"
# 追踪的 5 大品牌(搜索词)。Amazon 搜某品牌仍会混入别牌,品牌以标题为准。
BRAND_QUERIES = ("samsung", "lg", "tcl", "hisense", "sony")
MAX_PAGES = int(os.environ.get("AMAZON_MAX_PAGES", "7"))   # 每品牌最多翻 7 页(留余量)
COOKIE_ACCEPT_SELECTOR = "#sp-cc-accept"

_KNOWN_BRANDS = {
    "SAMSUNG": "Samsung", "HISENSE": "Hisense", "SONY": "Sony", "TCL": "TCL", "LG": "LG",
    "PHILIPS": "Philips", "PANASONIC": "Panasonic", "TOSHIBA": "Toshiba", "XIAOMI": "Xiaomi",
}
RE_SIZE = re.compile(r"(\d{2,3})\s*(?:Zoll|[\"”″])", re.IGNORECASE)

# 一次性抓本页全部结果(asin/标题/价格/是否广告)
_JS_EXTRACT = r"""
() => {
  const out = [];
  document.querySelectorAll("div[data-component-type='s-search-result']").forEach(el => {
    const asin = el.getAttribute('data-asin') || '';
    if (!asin) return;
    const t = el.querySelector("h2 span, [data-cy='title-recipe'] span, h2 a span");
    const title = t ? (t.textContent || '').trim().replace(/\s+/g, ' ') : '';
    if (!title) return;
    const sponsored = !!el.querySelector(
      "[aria-label*='Gesponsert'], .puis-sponsored-label-text, .s-sponsored-label-text, [data-component-type='sp-sponsored-result']");
    const pr = el.querySelector(".a-price .a-offscreen");
    const price = pr ? (pr.textContent || '').trim() : '';
    out.push({ asin, title, price, sponsored });
  });
  return out;
}
"""


def _brand_from_title(title: str) -> str:
    up = title.upper()
    for k, v in _KNOWN_BRANDS.items():
        if re.search(rf"\b{k}\b", up):
            return v
    return ""


def _size_from_title(title: str) -> float | None:
    m = RE_SIZE.search(title)
    return float(m.group(1)) if m else None


def _price_hint(text: str) -> float | None:
    """从 '1.299,00 €' / '172,70 GBP' 抠数字(德式千分点 + 逗号小数)。仅去重 hint,不分币种。"""
    m = re.search(r"\d[\d.\s]*,\d{2}", text) or re.search(r"\d[\d.\s]*", text)
    if not m:
        return None
    s = m.group(0).replace(" ", "").replace(".", "").replace(",", ".")
    try:
        return round(float(s), 2)
    except ValueError:
        return None


class AmazonCatalogAdapter(BaseCatalogAdapter):
    platform_name = "Amazon"
    country = "DE"
    locale_override = ("de-DE", "Europe/Berlin")

    async def _accept_cookie(self, page) -> None:
        try:
            await page.click(COOKIE_ACCEPT_SELECTOR, timeout=2500)
        except Exception:
            pass

    async def fetch_catalog(self, page) -> Sequence[CatalogItem]:
        by_asin: dict[str, CatalogItem] = {}     # 全品牌跨页按 ASIN 去重
        cookie_done = False
        for q in BRAND_QUERIES:
            for n in range(1, MAX_PAGES + 1):
                url = f"{BASE}/s?k={q}+fernseher&page={n}"
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                except Exception as e:
                    print(f"[catalog/Amazon] {q} p{n} goto 失败: {e}")
                    break
                if not cookie_done:
                    await self._accept_cookie(page)
                    cookie_done = True
                await page.wait_for_timeout(random.randint(2200, 3200))
                try:
                    rows = await page.evaluate(_JS_EXTRACT)
                except Exception as e:
                    print(f"[catalog/Amazon] {q} p{n} extract 失败: {e}")
                    break

                new_real = 0
                for r in rows:
                    if r.get("sponsored"):
                        continue
                    asin = (r.get("asin") or "").strip()
                    title = (r.get("title") or "").strip()
                    if not asin or asin in by_asin or not title:
                        continue
                    brand = _brand_from_title(title)
                    size = _size_from_title(title)
                    if not brand or size is None:      # 要求像「真电视」:有品牌 + 有尺寸
                        continue
                    by_asin[asin] = CatalogItem(
                        brand_raw=brand,
                        raw_text=title,
                        url=f"{BASE}/dp/{asin}",
                        size_hint_inch=size,
                        price_hint_eur=_price_hint(r.get("price") or ""),
                        extra={"asin": asin},
                    )
                    new_real += 1
                print(f"[catalog/Amazon] {q} p{n}: {len(rows)} 结果 / 本页新增真电视 {new_real} / 累计 {len(by_asin)}")
                if new_real == 0:                       # 本页无新真电视 = 该品牌翻到底
                    break
                await asyncio.sleep(random.uniform(1.0, 2.2))
        return list(by_asin.values())
