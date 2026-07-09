"""Amazon 多国家 catalog 抓取。

当前生产策略：
- DE 继续沿用已验证的德国邮编 + 固定 ASIN canary，保持原有稳定性。
- GB 先接入 amazon.co.uk，配送邮编使用用户指定的 Warwick 邮编 CV4 7ES。
  GB 阶段使用「地址设置成功 + 搜索页原生 GBP 合理价格」作为 fail-closed 守门；
  等 GitHub smoke 跑出稳定样本后，再把 GB 升级为固定 ASIN canary。
- IT/ES 复用 GB 已验证的搜索卡片结构化品牌/尺寸抽取策略，先用地址设置
  成功 + 搜索页 EUR 合理价格作为 fail-closed 守门。

输出仍保持 platform=Amazon，用 country 区分市场：
  catalog/amazon_de_YYYYMMDD.csv
  catalog/amazon_gb_YYYYMMDD.csv
  catalog/amazon_it_YYYYMMDD.csv
  catalog/amazon_es_YYYYMMDD.csv

价格口径：
- price_local/currency 保存渠道原币；
- price_eur 保存换算欧元；
- price_hint_eur 继续给下游 matcher 使用统一 EUR hint。
"""
from __future__ import annotations

import asyncio
import os
import random
import re
from dataclasses import dataclass
from typing import Sequence

from monitor_prices.core import clean_price
from monitor_prices.fx import ECB_RATE_DATE, price_to_eur

from .base import BaseCatalogAdapter, CatalogItem


# 追踪的 5 大品牌。Amazon 搜某品牌仍会混入别牌，品牌以标题为准。
BRAND_QUERIES = ("samsung", "lg", "tcl", "hisense", "sony")
MAX_PAGES = int(os.environ.get("AMAZON_MAX_PAGES", "7"))
COOKIE_ACCEPT_SELECTOR = "#sp-cc-accept"

_KNOWN_BRANDS = {
    "SAMSUNG": "Samsung",
    "HISENSE": "Hisense",
    "SONY": "Sony",
    "TCL": "TCL",
    "LG": "LG",
    "PHILIPS": "Philips",
    "PANASONIC": "Panasonic",
    "TOSHIBA": "Toshiba",
    "XIAOMI": "Xiaomi",
}

RE_SIZE = re.compile(
    r"(\d{2,3})\s*(?:[- ]?\s*(?:Zoll|inch(?:es)?|pollici|pulgadas)|[\"'”″])",
    re.IGNORECASE,
)

# 护栏：Amazon 按品牌搜会混入投影、商显、支架、保护膜、遥控、电源等非电视本体。
RE_NON_TV = re.compile(
    r"projektor|projector|projecteur|proiettore|proyector|laser\s*tv|beamer"
    r"|\bstanbyme\b|\bmonitor\b|moniteur"
    r"|business\s*display|professional\s*display|signage"
    r"|displayschutz|bildschirmschutz|schutzfolie|displayfolie|screen\s*protector|panzerglas"
    r"|wandhalterung|wall\s*mount|tv[- ]?halterung|supporto|soporte"
    r"|fußständer|tv[- ]?ständer|tv[- ]?stand|tv[- ]?beine|netzteil|fernbedienung|remote\s*control",
    re.IGNORECASE,
)

_JS_EXTRACT = r"""
() => {
  const out = [];
  const clean = (s) => (s || '').trim().replace(/\s+/g, ' ');
  const firstText = (el, selectors) => {
    for (const sel of selectors) {
      const node = el.querySelector(sel);
      const txt = clean(node ? node.textContent : '');
      if (txt) return txt;
    }
    return '';
  };
  document.querySelectorAll("div[data-component-type='s-search-result']").forEach(el => {
    const asin = el.getAttribute('data-asin') || '';
    if (!asin) return;
    // Amazon UK/DE 的搜索卡片常把品牌作为标题上方的独立粗体行展示。
    // 这比从标题或型号里猜品牌可靠，尤其适合 Hisense/TCL 这类标题经常省略品牌的结果。
    const brand = firstText(el, [
      "[data-cy='title-recipe'] h2.a-size-mini span.a-size-medium.a-color-base",
      "[data-cy='title-recipe'] .a-row.a-color-secondary span.a-size-medium.a-color-base",
      "h2.a-size-mini span.a-size-medium.a-color-base"
    ]);
    const candidates = [];
    [
      "[data-cy='title-recipe'] h2.a-size-medium.a-spacing-none.a-color-base.a-text-normal span",
      "[data-cy='title-recipe'] a.a-link-normal.s-line-clamp-2 span",
      "h2.a-size-medium.a-spacing-none.a-color-base.a-text-normal span",
      "img.s-image"
    ].forEach(sel => {
      el.querySelectorAll(sel).forEach(node => {
        const txt = clean(node.textContent || node.getAttribute('alt') || '');
        if (txt) candidates.push(txt);
      });
    });
    candidates.sort((a, b) => b.length - a.length);
    let title = candidates[0] || '';
    if (brand && title && !title.toLowerCase().startsWith(brand.toLowerCase())) {
      title = `${brand} ${title}`;
    }
    if (!title) return;
    const sponsored = !!el.querySelector(
      "[aria-label*='Gesponsert'], [aria-label*='Sponsored'], .puis-sponsored-label-text, .s-sponsored-label-text, [data-component-type='sp-sponsored-result']");
    const pr = el.querySelector(".a-price .a-offscreen");
    const price = pr ? (pr.textContent || '').trim() : '';
    const cardText = clean(el.textContent);
    const sizeMatch = cardText.match(
      /(?:Display Size|Screen Size|Bildschirmgr[oöß]?[sß]e|Displaygr[oöß]?[sß]e|Dimensione schermo|Tama[nñ]o de pantalla)\s*:?\s*(\d{2,3})\s*(?:inches?|Zoll|pollici|pulgadas|["”″])/i
    );
    const sizeText = sizeMatch ? `${sizeMatch[1]} inches` : '';
    out.push({ asin, brand, title, price, sponsored, sizeText });
  });
  return out;
}
"""

_AMZ_PRICE_SELECTORS = (
    "#corePriceDisplay_desktop_feature_div span.priceToPay span.a-offscreen",
    "#corePriceDisplay_desktop_feature_div .a-offscreen",
    ".priceToPay .a-offscreen",
    ".a-price .a-offscreen",
)

_CANARY_LO, _CANARY_HI = 0.5, 1.5


@dataclass(frozen=True)
class AmazonMarket:
    code: str
    base_url: str
    cookie_domain: str
    locale: str
    timezone: str
    search_word: str
    postcode: str
    currency: str
    language_cookie_name: str
    language_cookie_value: str
    location_required: bool = True
    de_canary: tuple[tuple[str, float], ...] = ()


AMAZON_DE = AmazonMarket(
    code="DE",
    base_url="https://www.amazon.de",
    cookie_domain=".amazon.de",
    locale="de-DE",
    timezone="Europe/Berlin",
    search_word="fernseher",
    postcode=os.environ.get("AMAZON_DE_ZIP", "26935"),
    currency="EUR",
    language_cookie_name="lc-acbde",
    language_cookie_value="de_DE",
    de_canary=(
        ("B0GYZMPVXG", 229.99),  # Hisense 32A5DS
        ("B0GT9QKMRM", 169.99),  # Hisense 32E4DS
    ),
)

AMAZON_GB = AmazonMarket(
    code="GB",
    base_url="https://www.amazon.co.uk",
    cookie_domain=".amazon.co.uk",
    locale="en-GB",
    timezone="Europe/London",
    search_word="tv",
    postcode=os.environ.get("AMAZON_GB_POSTCODE", "CV4 7ES"),
    currency="GBP",
    language_cookie_name="lc-acbuk",
    language_cookie_value="en_GB",
)

AMAZON_IT = AmazonMarket(
    code="IT",
    base_url="https://www.amazon.it",
    cookie_domain=".amazon.it",
    locale="it-IT",
    timezone="Europe/Rome",
    search_word="televisore",
    postcode=os.environ.get("AMAZON_IT_POSTCODE", "20121"),
    currency="EUR",
    language_cookie_name="lc-acbit",
    language_cookie_value="it_IT",
    location_required=False,
)

AMAZON_ES = AmazonMarket(
    code="ES",
    base_url="https://www.amazon.es",
    cookie_domain=".amazon.es",
    locale="es-ES",
    timezone="Europe/Madrid",
    search_word="televisor",
    postcode=os.environ.get("AMAZON_ES_POSTCODE", "28013"),
    currency="EUR",
    language_cookie_name="lc-acbes",
    language_cookie_value="es_ES",
    location_required=False,
)


def _brand_from_title(title: str) -> str:
    up = title.upper()
    for k, v in _KNOWN_BRANDS.items():
        if re.search(rf"\b{k}\b", up):
            return v
    return ""


def _size_from_title(title: str) -> float | None:
    m = RE_SIZE.search(title)
    return float(m.group(1)) if m else None


def _price_pair(text: str, expected_currency: str) -> tuple[float | None, str, float | None]:
    """返回 (本币价, 币种, 欧元价)。币种不符时返回空，避免错国价进入数据。"""
    parsed = clean_price(text)
    if not parsed:
        return None, "", None
    price, currency = parsed
    currency = currency.upper()
    if currency != expected_currency:
        return None, currency, None
    return round(price, 2), currency, price_to_eur(price, currency)


async def _accept_cookie(page) -> None:
    for sel in (
        COOKIE_ACCEPT_SELECTOR,
        f"{COOKIE_ACCEPT_SELECTOR} input",
        "#sp-cc-rejectall-link",
        "input#sp-cc-accept",
        "input[name='accept']",
        "button[name='accept']",
    ):
        try:
            await page.click(sel, timeout=2500)
            return
        except Exception:
            pass
    for pat in ("Accetta", "Accetta tutto", "Aceptar", "Aceptar todo", "Accept", "Reject", "Rifiuta", "Rechazar"):
        try:
            await page.get_by_text(pat, exact=False).first.click(timeout=1200)
            return
        except Exception:
            pass


async def set_amazon_location_via_popup(page, market: AmazonMarket) -> bool:
    """旧 glow toaster 接口为空时，用顶部配送地弹窗填邮编作为 fallback。"""
    try:
        await page.goto(f"{market.base_url}/", wait_until="domcontentloaded", timeout=45000)
        await _accept_cookie(page)
        await page.click("#nav-global-location-popover-link, #glow-ingress-block", timeout=8000)
        await page.wait_for_timeout(1500)
        inp = page.locator("#GLUXZipUpdateInput").first
        if await inp.count() == 0:
            print(f"  [set-loc/{market.code}] 弹窗未出现邮编输入框")
            return False
        await inp.fill(market.postcode, timeout=5000)
        await page.click("#GLUXZipUpdate", timeout=5000)
        await page.wait_for_timeout(2500)
        for sel in ("#GLUXConfirmClose", "input[name='glowDoneButton']", ".a-popover-footer .a-button-input"):
            try:
                await page.click(sel, timeout=1500)
                break
            except Exception:
                pass
        print(f"  [set-loc/{market.code}] 配送地弹窗 → {market.postcode}:✓")
        return True
    except Exception as e:
        print(f"  [set-loc/{market.code}] 配送地弹窗失败: {str(e)[:120]}")
        return False


async def set_amazon_market_location(page, market: AmazonMarket) -> bool:
    """用 Amazon glow 地址接口设置配送地。失败必须 fail-closed。"""
    try:
        await page.context.add_cookies([
            {"name": "i18n-prefs", "value": market.currency, "domain": market.cookie_domain, "path": "/"},
            {
                "name": market.language_cookie_name,
                "value": market.language_cookie_value,
                "domain": market.cookie_domain,
                "path": "/",
            },
        ])
    except Exception:
        pass

    try:
        await page.goto(f"{market.base_url}/", wait_until="domcontentloaded", timeout=45000)
    except Exception as e:
        print(f"  [set-loc/{market.code}] 进首页失败: {e}")
        return False

    await _accept_cookie(page)

    try:
        html = await page.evaluate(
            """async (baseUrl) => {
                const url = baseUrl + "/portal-migration/hz/glow/get-rendered-toaster"
                    + "?pageType=Gateway&aisTransitionState=null&rancorLocationSource=IP_GEOLOCATION&isB2B=false";
                const r = await fetch(url, {credentials: "include"});
                return await r.text();
            }""",
            market.base_url,
        )
    except Exception as e:
        print(f"  [set-loc/{market.code}] 取 CSRF token 失败: {e}")
        return False

    m = re.search(r'data-toaster-csrfToken="([^"]+)"', html)
    if not m:
        print(f"  [set-loc/{market.code}] 没找到 CSRF token，Amazon glow 可能改版")
        ok = await set_amazon_location_via_popup(page, market)
        if ok:
            return True
        if not market.location_required:
            print(f"  [set-loc/{market.code}] 邮编未确认；该 EUR 市场继续交给搜索页币种 canary 守门")
            return True
        return False

    token = m.group(1)
    try:
        res = await page.evaluate(
            """async ({baseUrl, token, zip}) => {
                const r = await fetch(baseUrl + "/portal-migration/hz/glow/address-change?actionSource=glow", {
                    method: "POST",
                    headers: {"anti-csrftoken-a2z": token, "content-type": "application/json"},
                    credentials: "include",
                    body: JSON.stringify({
                        locationType: "LOCATION_INPUT",
                        zipCode: zip,
                        deviceType: "web",
                        storeContext: "generic",
                        pageType: "Gateway",
                        actionSource: "glow"
                    })
                });
                let updated = false;
                try { updated = (await r.json()).isAddressUpdated === 1; } catch (e) {}
                return {status: r.status, updated};
            }""",
            {"baseUrl": market.base_url, "token": token, "zip": market.postcode},
        )
    except Exception as e:
        print(f"  [set-loc/{market.code}] POST address-change 失败: {e}")
        return False

    ok = bool(res.get("updated"))
    print(
        f"  [set-loc/{market.code}] 配送地 → {market.postcode}:"
        f"{'✓ isAddressUpdated:1' if ok else '✗ 未生效'} (status={res.get('status')})"
    )
    if not ok and not market.location_required:
        print(f"  [set-loc/{market.code}] 邮编未确认；该 EUR 市场继续交给搜索页币种 canary 守门")
        return True
    return ok


async def verify_amazon_de_canary(page, market: AmazonMarket) -> bool:
    ok_any = False
    for asin, known in market.de_canary:
        try:
            await page.goto(f"{market.base_url}/dp/{asin}", wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(1500)
            txt = await page.evaluate(
                """(sels) => {
                    for (const s of sels) {
                        const e = document.querySelector(s);
                        if (e && e.textContent && e.textContent.trim()) return e.textContent.trim();
                    }
                    return "";
                }""",
                list(_AMZ_PRICE_SELECTORS),
            )
        except Exception as e:
            print(f"  [canary/{market.code}] {asin} 抓取异常: {str(e)[:80]}")
            continue
        local_price, currency, _eur = _price_pair(txt, market.currency)
        lo, hi = _CANARY_LO * known, _CANARY_HI * known
        if local_price is not None and lo <= local_price <= hi:
            print(f"  [canary/{market.code}] {asin} {local_price} {currency} ∈ [{lo:.0f},{hi:.0f}] ✓")
            ok_any = True
        else:
            print(
                f"  [canary/{market.code}] {asin} raw={txt[:24]!r} 不符"
                f"(要 {market.currency} 且 ∈[{lo:.0f},{hi:.0f}])"
            )
    if not ok_any:
        print(f"  [canary/{market.code}] ✗ 所有锚点不符 → abort")
    return ok_any


async def verify_amazon_search_currency(page, market: AmazonMarket) -> bool:
    """GB 初期守门：确认设置地址后搜索页给的是原生 GBP，且价格在电视合理区间。"""
    url = f"{market.base_url}/s?k=hisense+{market.search_word}&page=1"
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(2000)
        rows = await page.evaluate(_JS_EXTRACT)
    except Exception as e:
        print(f"  [canary/{market.code}] 搜索页校验失败: {e}")
        return False
    for r in rows:
        if r.get("sponsored"):
            continue
        title = (r.get("title") or "").strip()
        if RE_NON_TV.search(title):
            continue
        price, currency, _eur = _price_pair(r.get("price") or "", market.currency)
        if price is not None and 50 <= price <= 10000:
            print(f"  [canary/{market.code}] 搜索页 {price} {currency} 合理 ✓")
            return True
    print(f"  [canary/{market.code}] 搜索页未找到合理 {market.currency} 电视价 → abort")
    return False


class AmazonCatalogAdapter(BaseCatalogAdapter):
    """Amazon 单市场 adapter。registry 用 amazon_de / amazon_gb 区分实例。"""

    platform_name = "Amazon"

    def __init__(self, market: AmazonMarket):
        self.market = market
        self.country = market.code
        self.locale_override = (market.locale, market.timezone)

    async def fetch_catalog(self, page) -> Sequence[CatalogItem]:
        market = self.market
        if not await set_amazon_market_location(page, market):
            print(f"[catalog/Amazon/{market.code}] ✗ 设置配送地失败 → abort")
            return []
        if market.de_canary:
            if not await verify_amazon_de_canary(page, market):
                print(f"[catalog/Amazon/{market.code}] ✗ canary 不过 → abort")
                return []
        elif not await verify_amazon_search_currency(page, market):
            print(f"[catalog/Amazon/{market.code}] ✗ 搜索页币种守门不过 → abort")
            return []

        by_asin: dict[str, CatalogItem] = {}
        cookie_done = False
        for q in BRAND_QUERIES:
            consecutive_empty = 0
            for n in range(1, MAX_PAGES + 1):
                url = f"{market.base_url}/s?k={q}+{market.search_word}&page={n}"
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                except Exception as e:
                    print(f"[catalog/Amazon/{market.code}] {q} p{n} goto 失败: {e}")
                    break
                if not cookie_done:
                    await _accept_cookie(page)
                    cookie_done = True
                await page.wait_for_timeout(random.randint(2200, 3200))
                try:
                    rows = await page.evaluate(_JS_EXTRACT)
                except Exception as e:
                    print(f"[catalog/Amazon/{market.code}] {q} p{n} extract 失败: {e}")
                    break

                new_real = 0
                filtered = {"sponsored": 0, "no_brand": 0, "no_size": 0, "non_tv": 0, "duplicate": 0}
                for r in rows:
                    if r.get("sponsored"):
                        filtered["sponsored"] += 1
                        continue
                    asin = (r.get("asin") or "").strip()
                    title = (r.get("title") or "").strip()
                    if not asin or not title:
                        continue
                    if asin in by_asin:
                        filtered["duplicate"] += 1
                        continue
                    card_brand = (r.get("brand") or "").strip()
                    # 如果搜索卡片明确给了品牌行，以它为准；未知品牌直接丢弃。
                    # 只有卡片没有品牌行时，才回退到标题识别，避免把 “Samsung Tizen OS”
                    # 这类功能描述误判成商品品牌。
                    brand = _brand_from_title(card_brand) if card_brand else _brand_from_title(title)
                    size = _size_from_title(title) or _size_from_title(r.get("sizeText") or "")
                    if not brand or size is None:
                        filtered["no_brand" if not brand else "no_size"] += 1
                        continue
                    if RE_NON_TV.search(title):
                        filtered["non_tv"] += 1
                        continue
                    price_local, currency, price_eur = _price_pair(r.get("price") or "", market.currency)
                    by_asin[asin] = CatalogItem(
                        brand_raw=brand,
                        raw_text=title,
                        url=f"{market.base_url}/dp/{asin}",
                        size_hint_inch=size,
                        price_hint_eur=price_eur,
                        price_local=price_local,
                        currency=currency,
                        price_eur=price_eur,
                        extra={
                            "asin": asin,
                            "fx_rate_date": ECB_RATE_DATE if currency and currency != "EUR" else "",
                        },
                    )
                    new_real += 1
                print(
                    f"[catalog/Amazon/{market.code}] {q} p{n}: {len(rows)} 结果 / "
                    f"本页新增真电视 {new_real} / 累计 {len(by_asin)} / 过滤 {filtered}"
                )
                if new_real == 0:
                    consecutive_empty += 1
                    if consecutive_empty >= 2:
                        break
                else:
                    consecutive_empty = 0
                await asyncio.sleep(random.uniform(1.0, 2.2))
        return list(by_asin.values())


class AmazonDeCatalogAdapter(AmazonCatalogAdapter):
    def __init__(self):
        super().__init__(AMAZON_DE)


class AmazonGbCatalogAdapter(AmazonCatalogAdapter):
    def __init__(self):
        super().__init__(AMAZON_GB)


class AmazonItCatalogAdapter(AmazonCatalogAdapter):
    def __init__(self):
        super().__init__(AMAZON_IT)


class AmazonEsCatalogAdapter(AmazonCatalogAdapter):
    def __init__(self):
        super().__init__(AMAZON_ES)
