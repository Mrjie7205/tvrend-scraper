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
- 搜索页价格随连接的配送地 localize → **fetch_catalog 开头先 set_amazon_de_location(设德国邮编,纯 AJAX、
  任意 IP)+ canary 守门**,此后搜索页 price_hint_eur 即真·德国 EUR 价。set-loc/canary 失败 → fail-closed
  返回空(绝不用默认美国地址抓→错国价)。
- 配件/投影/商显在源头剔(护栏3 RE_NON_TV,与私库 matcher 同口径 + 补 ü/Fußständer/Netzteil/TV-Beine 漏网)。

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

# 护栏3:非电视/配件——Amazon 按品牌搜会混入投影/激光电视/便携屏/商显/屏保/支架/电源/遥控。
# 与私库 matcher RE_NON_TV 同口径,并补其漏网:ü 变体(Standfüße)、Fußständer、Netzteil、TV-Beine、商显。
# 锚定"TV-配件"措辞(tv-ständer/tv-beine),避免误杀"mit Standfuß"的真电视。在 catalog 源头剔。
RE_NON_TV = re.compile(
    r"projektor|projector|projecteur|laser\s*tv|beamer"
    r"|\bstanbyme\b|\bmonitor\b|moniteur"
    r"|business\s*display|professional\s*display|signage"
    r"|displayschutz|bildschirmschutz|schutzfolie|displayfolie|screen\s*protector|panzerglas"
    r"|wandhalterung|tv[- ]?halterung"
    r"|fußständer|tv[- ]?ständer|tv[- ]?beine|netzteil|fernbedienung",
    re.IGNORECASE,
)

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
        from monitor_prices.core import set_amazon_de_location, verify_amazon_de_canary
        # ★ fail-closed:先把会话配送地设成德国邮编(任意 IP 可用)。失败 → 返回空,绝不用默认美国地址抓(错国价)。
        if not await set_amazon_de_location(page):
            print("[catalog/Amazon] ✗ 设德国地区失败 → abort(返回空,不写错国价)")
            return []
        # canary 守门:确认真拿到德国价(防 glow 静默坏掉吐错国价)
        if not await verify_amazon_de_canary(page):
            print("[catalog/Amazon] ✗ canary 不过 → abort(返回空)")
            return []

        by_asin: dict[str, CatalogItem] = {}     # 全品牌跨页按 ASIN 去重
        cookie_done = False                       # set-loc 已点过 cookie,首搜索页再点一次兜底
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
                    if RE_NON_TV.search(title):        # 护栏3:配件/投影/商显 在源头剔
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
