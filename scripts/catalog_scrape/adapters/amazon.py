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
from urllib.parse import quote_plus

from monitor_prices.core import clean_price
from monitor_prices.fx import ECB_RATE_DATE, price_to_eur

from .base import BaseCatalogAdapter, CatalogItem


# 追踪的 5 大品牌。Amazon 搜某品牌仍会混入别牌，品牌以标题为准。
BRAND_QUERIES = tuple(
    q.strip().lower()
    for q in os.environ.get("AMAZON_BRAND_QUERIES", "samsung,lg,tcl,hisense,sony").split(",")
    if q.strip()
)
TARGET_BRAND_ORDER = tuple(q.upper() for q in BRAND_QUERIES)
EXTRA_SERIES_QUERIES = tuple(
    q.strip().lower()
    for q in os.environ.get("AMAZON_EXTRA_SERIES_QUERIES", "").split(",")
    if q.strip()
)
TARGET_YEARS = tuple(
    y.strip()
    for y in os.environ.get("AMAZON_TARGET_YEARS", "2025,2026").split(",")
    if y.strip()
)
MAX_PAGES = int(os.environ.get("AMAZON_MAX_PAGES", "7"))
EXTRA_MAX_PAGES = int(os.environ.get("AMAZON_EXTRA_MAX_PAGES", "3"))
YEAR_MAX_PAGES = int(os.environ.get("AMAZON_YEAR_MAX_PAGES", "2"))
SERIES_RESCUE_MAX_PAGES = int(os.environ.get("AMAZON_SERIES_RESCUE_MAX_PAGES", "1"))
MAX_SERIES_RESCUE_QUERIES = int(os.environ.get("AMAZON_MAX_SERIES_RESCUE_QUERIES", "40"))
EXPAND_VARIANTS = os.environ.get("AMAZON_EXPAND_VARIANTS", "true").lower() != "false"
MAX_VARIANT_SEEDS = int(os.environ.get("AMAZON_MAX_VARIANT_SEEDS", "64"))
MAX_VARIANTS_PER_SEED = int(os.environ.get("AMAZON_MAX_VARIANTS_PER_SEED", "12"))
MAX_SEEDS_PER_SERIES = int(os.environ.get("AMAZON_MAX_SEEDS_PER_SERIES", "2"))
SESSION_PREP_ATTEMPTS = int(os.environ.get("AMAZON_SESSION_PREP_ATTEMPTS", "3"))
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

RE_VARIANT_HINT = re.compile(
    r"\b(?:Options?|Optionen|Opzioni|Opciones)\s*:\s*\d+",
    re.IGNORECASE,
)
RE_CURRENT_YEAR_HINT = re.compile(r"\b(?:2025|2026)\b")
RE_CURRENT_SERIES_HINT = re.compile(
    # Amazon 部分市场不显示 “Options: n sizes”，但标题里有当前年款系列码。
    # 这些型号优先进详情页 twister 补 sibling，避免 S95H 这类新品只抓到一个尺寸。
    r"(?:S9[05]H|S8[05]H|QN\d{2,4}H|QN\d{2,4}F|Q\dF|Q\dFA|U\d{4}F"
    r"|C\d[KL]|P\d[KL]|X11L|QNED\d{2}[AB]|OLED\d{2}[A-Z0-9]*[56]?[A-Z]*)",
    re.IGNORECASE,
)

# 这里只用于生成“系列精确搜索”以及对详情页种子去重，不承担商品品牌判断或最终型号匹配。
# 最终品牌仍来自 Amazon 搜索卡片/标题，最终 base_model 仍由私库 matcher 决定。
_SERIES_PATTERNS = {
    "SAMSUNG": (
        re.compile(r"(?:GQ|GU|QE|TQ|TU)?\d{2,3}(S(?:85|90|95|99)[FH])", re.IGNORECASE),
        re.compile(r"(?:GQ|QE|TQ)?\d{2,3}(QN(?:70|80|85|90|900|990)[FH])", re.IGNORECASE),
        re.compile(r"\b(S(?:85|90|95|99)[FH]|QN(?:70|80|85|90|900|990)[FH])\b", re.IGNORECASE),
        re.compile(r"\b(Q[678]F|LS03(?:FW|H)|M[78]0H|R\d{2}H|U\d{4}F)\b", re.IGNORECASE),
    ),
    "LG": (
        re.compile(r"OLED\s*\d{2,3}\s*([BCG][56])", re.IGNORECASE),
        re.compile(r"(?:^|\D)\d{2,3}(QNED\d{2}[AB]|UA\d{2}|NU\d{2})", re.IGNORECASE),
        re.compile(r"\b(QNED\d{2}[AB]|UA\d{2}|NU\d{2})\b", re.IGNORECASE),
        re.compile(r"\b([BCG][56])\b", re.IGNORECASE),
    ),
    "TCL": (
        re.compile(
            r"(?:^|\D)\d{2,3}(X11L|C\d[KL](?:\s*PRO|S)?|P\d[KL]|Q\dC|T6C|S[45][KL]?|A\d{3}(?:U|W|\s*PRO)?)\b",
            re.IGNORECASE,
        ),
        re.compile(
            r"\b(X11L|C\d[KL](?:\s*PRO|S)?|P\d[KL]|Q\dC|T6C|S[45][KL]?|A\d{3}(?:U|W|\s*PRO)?)\b",
            re.IGNORECASE,
        ),
    ),
    "HISENSE": (
        re.compile(
            r"(?:^|\D)\d{2,3}(A[4567][QS]|E[678][QS](?:\s*PRO)?|U[789][QS](?:\s*(?:PRO|E))?|UR[89]S|S5Q)\b",
            re.IGNORECASE,
        ),
        re.compile(
            r"\b(A[4567][QS]|E[678][QS](?:\s*PRO)?|U[789][QS](?:\s*(?:PRO|E))?|UR[89]S|S5Q)\b",
            re.IGNORECASE,
        ),
    ),
    "SONY": (
        re.compile(r"\b(BRAVIA\s*[23589](?:\s*II)?)\b", re.IGNORECASE),
        re.compile(r"\b(XR\d{2}(?:M2)?)\b", re.IGNORECASE),
    ),
}

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
    const variantHint = /(?:Options?|Optionen|Opzioni|Opciones)\s*:\s*\d+/i.test(cardText);
    out.push({ asin, brand, title, price, sponsored, sizeText, variantHint });
  });
  return out;
}
"""

_JS_DETAIL = r"""
(priceSelectors) => {
  const clean = (s) => (s || '').trim().replace(/\s+/g, ' ');
  const firstText = (selectors) => {
    for (const sel of selectors) {
      const node = document.querySelector(sel);
      const txt = clean(node ? node.textContent : '');
      if (txt) return txt;
    }
    return '';
  };
  let price = '';
  for (const sel of priceSelectors) {
    const node = document.querySelector(sel);
    const txt = clean(node ? node.textContent : '');
    if (txt) {
      price = txt;
      break;
    }
  }
  const variantRefs = [];
  const seen = new Set();
  const add = (el) => {
    const asin = clean(
      el.getAttribute('data-asin')
      || el.getAttribute('data-defaultasin')
      || el.getAttribute('data-csa-c-item-id')
      || ''
    ).replace(/^asin\./i, '');
    const dp = el.getAttribute('data-dp-url') || el.getAttribute('href') || el.getAttribute('value') || '';
    const m = dp.match(/\/dp\/([A-Z0-9]{10})/i);
    const finalAsin = (asin && /^[A-Z0-9]{10}$/i.test(asin)) ? asin.toUpperCase() : (m ? m[1].toUpperCase() : '');
    if (!finalAsin || seen.has(finalAsin)) return;
    const text = clean(
      el.textContent
      || el.getAttribute('title')
      || el.getAttribute('aria-label')
      || el.getAttribute('data-a-html-content')
      || ''
    );
    seen.add(finalAsin);
    variantRefs.push({ asin: finalAsin, text });
  };
  document.querySelectorAll([
    '#twister [data-asin]',
    '#twister [data-defaultasin]',
    '#twister [data-dp-url]',
    '#variation_size_name [data-asin]',
    '#variation_size_name [data-defaultasin]',
    '#variation_size_name li',
    '#variation_size_name option',
    '.twister-plus-inline-twister-container [data-asin]',
    '.twister-plus-inline-twister-container [data-defaultasin]',
    '.inline-twister-swatch[data-asin]',
    '.inline-twister-swatch[data-defaultasin]',
    '[class*="twister"][data-asin]',
    '[class*="twister"][data-defaultasin]',
    '[class*="swatch"][data-asin]',
    '[class*="swatch"][data-defaultasin]'
  ].join(',')).forEach(add);
  return {
    title: firstText(['#productTitle', 'span#productTitle']),
    price,
    variantRefs,
  };
}
"""

_AMZ_PRICE_SELECTORS = (
    "#corePriceDisplay_desktop_feature_div span.priceToPay span.a-offscreen",
    "#corePriceDisplay_desktop_feature_div .a-offscreen",
    ".priceToPay .a-offscreen",
    ".a-price .a-offscreen",
)
_AMZ_DETAIL_PRICE_SELECTORS = (
    "#corePriceDisplay_desktop_feature_div span.priceToPay span.a-offscreen",
    "#corePriceDisplay_desktop_feature_div .priceToPay .a-offscreen",
    "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen",
    "#corePrice_feature_div .a-price .a-offscreen",
    "#apex_desktop .a-price .a-offscreen",
    "#priceblock_ourprice",
    "#priceblock_dealprice",
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

    async def _prepare_market_session(self, page) -> bool:
        """地址或币种守门遇到 Amazon 短时波动时，重建 cookie 状态后有限重试。"""
        market = self.market
        for attempt in range(1, SESSION_PREP_ATTEMPTS + 1):
            if attempt > 1:
                try:
                    await page.context.clear_cookies()
                    await page.goto("about:blank")
                except Exception:
                    pass
            location_ok = await set_amazon_market_location(page, market)
            canary_ok = False
            if location_ok:
                if market.de_canary:
                    canary_ok = await verify_amazon_de_canary(page, market)
                else:
                    canary_ok = await verify_amazon_search_currency(page, market)
            if location_ok and canary_ok:
                if attempt > 1:
                    print(f"[catalog/Amazon/{market.code}] 会话守门第 {attempt} 次成功 ✓")
                return True
            if attempt < SESSION_PREP_ATTEMPTS:
                print(
                    f"[catalog/Amazon/{market.code}] 会话守门第 {attempt} 次失败 "
                    f"(location={location_ok}, canary={canary_ok})，重试…"
                )
                await asyncio.sleep(random.uniform(3.0, 6.0))
        print(
            f"[catalog/Amazon/{market.code}] ✗ 会话守门连续 {SESSION_PREP_ATTEMPTS} 次失败 → abort"
        )
        return False

    def _build_item(
        self,
        asin: str,
        title: str,
        brand: str,
        size: float,
        price_text: str,
    ) -> CatalogItem:
        market = self.market
        price_local, currency, price_eur = _price_pair(price_text, market.currency)
        return CatalogItem(
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

    def _item_from_search_row(self, row: dict, filtered: dict[str, int]) -> CatalogItem | None:
        asin = (row.get("asin") or "").strip()
        title = (row.get("title") or "").strip()
        if not asin or not title:
            return None
        card_brand = (row.get("brand") or "").strip()
        # 如果搜索卡片明确给了品牌行，以它为准；未知品牌直接丢弃。
        # 只有卡片没有品牌行时，才回退到标题识别，避免把 “Samsung Tizen OS”
        # 这类功能描述误判成商品品牌。
        brand = _brand_from_title(card_brand) if card_brand else _brand_from_title(title)
        size = _size_from_title(title) or _size_from_title(row.get("sizeText") or "")
        if not brand or size is None:
            filtered["no_brand" if not brand else "no_size"] += 1
            return None
        if RE_NON_TV.search(title):
            filtered["non_tv"] += 1
            return None
        return self._build_item(asin, title, brand, size, row.get("price") or "")

    def _should_expand_variants(self, row: dict, item: CatalogItem) -> bool:
        if not EXPAND_VARIANTS:
            return False
        if row.get("variantHint"):
            return True
        if RE_CURRENT_YEAR_HINT.search(item.raw_text or ""):
            return True
        if RE_CURRENT_SERIES_HINT.search(item.raw_text or ""):
            return True
        # 某些市场/版式没有把 “Options: n sizes” 暴露到稳定节点；
        # 标题里有多尺寸提示时也允许进入详情页，但仍由详情页 twister 限定 sibling ASIN。
        return bool(RE_VARIANT_HINT.search(row.get("title") or item.raw_text))

    @staticmethod
    def _series_hint(item: CatalogItem) -> str:
        """从标题提取系列搜索词，仅用于补抓，不作为最终 matcher 结论。"""
        brand = (item.brand_raw or "").upper()
        text = item.raw_text or ""
        for candidate in (text, re.sub(r"\s+", "", text)):
            for pattern in _SERIES_PATTERNS.get(brand, ()):
                match = pattern.search(candidate)
                if match:
                    return re.sub(r"\s+", "", match.group(1).upper()).strip()
        return ""

    @classmethod
    def _variant_seed_priority(cls, item: CatalogItem) -> tuple[int, int, int, int]:
        """有明确多尺寸入口的新品优先，避免详情页预算被低价值种子占满。"""
        text = item.raw_text or ""
        variant_hint = 0 if item.extra.get("variant_hint") else 1
        current_hit = 0 if (RE_CURRENT_YEAR_HINT.search(text) or RE_CURRENT_SERIES_HINT.search(text)) else 1
        series_hit = 0 if cls._series_hint(item) else 1
        priced = 0 if item.price_local is not None else 1
        return variant_hint, current_hit, series_hit, priced

    @classmethod
    def _select_variant_seeds(cls, items: Sequence[CatalogItem]) -> list[CatalogItem]:
        """每个已识别系列最多保留少量入口，兼顾效率与详情页偶发缺失的回退。"""
        selected: list[CatalogItem] = []
        per_series: dict[tuple[str, str], int] = {}
        queues: dict[str, list[CatalogItem]] = {}
        for item in sorted(items, key=cls._variant_seed_priority):
            queues.setdefault((item.brand_raw or "").upper(), []).append(item)
        brand_order = list(TARGET_BRAND_ORDER) + sorted(set(queues) - set(TARGET_BRAND_ORDER))
        while len(selected) < MAX_VARIANT_SEEDS and any(queues.get(brand) for brand in brand_order):
            for brand in brand_order:
                queue = queues.get(brand) or []
                while queue:
                    item = queue.pop(0)
                    series = cls._series_hint(item)
                    if series:
                        key = (brand, series)
                        used = per_series.get(key, 0)
                        if used >= MAX_SEEDS_PER_SERIES:
                            continue
                        per_series[key] = used + 1
                    selected.append(item)
                    break
                if len(selected) >= MAX_VARIANT_SEEDS:
                    break
        return selected

    @classmethod
    def _series_rescue_queries(cls, items: Sequence[CatalogItem]) -> list[str]:
        """对宽泛品牌搜索已发现的新品系列再做一次精确搜索，找回独立 ASIN 尺寸。"""
        stats: dict[tuple[str, str], set[int]] = {}
        for item in items:
            series = cls._series_hint(item)
            if not series:
                continue
            size = int(item.size_hint_inch or 0)
            key = ((item.brand_raw or "").upper(), series)
            stats.setdefault(key, set()).add(size)
        queues: dict[str, list[tuple[str, str]]] = {}
        for key in stats:
            queues.setdefault(key[0], []).append(key)
        for brand in queues:
            queues[brand].sort(key=lambda key: (len(stats[key]), key[1]))
        brand_order = list(TARGET_BRAND_ORDER) + sorted(set(queues) - set(TARGET_BRAND_ORDER))
        ranked: list[tuple[str, str]] = []
        while len(ranked) < MAX_SERIES_RESCUE_QUERIES and any(queues.get(brand) for brand in brand_order):
            for brand in brand_order:
                queue = queues.get(brand) or []
                if queue:
                    ranked.append(queue.pop(0))
                if len(ranked) >= MAX_SERIES_RESCUE_QUERIES:
                    break
        return [f"{brand.lower()} {series.lower()}" for brand, series in ranked]

    async def _detail_item(self, page, asin: str, fallback_brand: str, fallback_variant_text: str = "") -> CatalogItem | None:
        market = self.market
        try:
            await page.goto(f"{market.base_url}/dp/{asin}", wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(random.randint(1300, 2200))
            detail = await page.evaluate(_JS_DETAIL, list(_AMZ_DETAIL_PRICE_SELECTORS))
        except Exception as e:
            print(f"[catalog/Amazon/{market.code}] detail {asin} 失败: {str(e)[:100]}")
            return None
        title = (detail.get("title") or "").strip()
        if not title:
            return None
        brand = _brand_from_title(title) or fallback_brand
        size = _size_from_title(title) or _size_from_title(fallback_variant_text)
        if not brand or size is None:
            return None
        if RE_NON_TV.search(title):
            return None
        return self._build_item(asin, title, brand, size, detail.get("price") or "")

    async def _expand_variants_from_seed(
        self,
        page,
        seed: CatalogItem,
        by_asin: dict[str, CatalogItem],
    ) -> int:
        """从一个已确认电视卡片进入详情页，只抽 twister 中的同款尺寸 ASIN。

        注意：只读 #twister / #variation_size_name，故不会把详情页广告推荐里的
        壁挂架、耳机、显示器等 ASIN 当成 sibling。
        """
        market = self.market
        seed_asin = seed.extra.get("asin") or ""
        if not seed_asin:
            return 0
        try:
            await page.goto(seed.url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(random.randint(1300, 2200))
            detail = await page.evaluate(_JS_DETAIL, list(_AMZ_DETAIL_PRICE_SELECTORS))
        except Exception as e:
            print(f"[catalog/Amazon/{market.code}] variants {seed_asin} 失败: {str(e)[:100]}")
            return 0

        refs = detail.get("variantRefs") or []
        # 某些详情页只渲染一个“另一个尺寸”，不能把这种情况误判为无变体。
        if not refs:
            return 0
        added = 0
        for ref in refs[:MAX_VARIANTS_PER_SEED]:
            asin = (ref.get("asin") or "").strip().upper()
            if not asin or asin in by_asin:
                continue
            item = await self._detail_item(page, asin, seed.brand_raw, ref.get("text") or "")
            if item is None:
                continue
            by_asin[asin] = item
            added += 1
            await asyncio.sleep(random.uniform(0.6, 1.2))
        return added

    async def fetch_catalog(self, page) -> Sequence[CatalogItem]:
        market = self.market
        if not await self._prepare_market_session(page):
            return []

        by_asin: dict[str, CatalogItem] = {}
        variant_seeds: dict[str, CatalogItem] = {}
        cookie_done = False

        async def scrape_query(q: str, max_pages: int, query_kind: str) -> None:
            nonlocal cookie_done
            consecutive_empty = 0
            for n in range(1, max_pages + 1):
                search_text = f"{q} {market.search_word}"
                url = f"{market.base_url}/s?k={quote_plus(search_text)}&page={n}"
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
                    asin = (r.get("asin") or "").strip().upper()
                    if asin in by_asin:
                        filtered["duplicate"] += 1
                        existing = by_asin[asin]
                        if r.get("variantHint"):
                            existing.extra["variant_hint"] = True
                        if self._should_expand_variants(r, existing):
                            variant_seeds[asin] = existing
                        continue
                    item = self._item_from_search_row(r, filtered)
                    if item is None:
                        continue
                    item.extra["variant_hint"] = bool(r.get("variantHint"))
                    item.extra["search_kind"] = query_kind
                    by_asin[asin] = item
                    if self._should_expand_variants(r, item):
                        variant_seeds[asin] = item
                    new_real += 1
                print(
                    f"[catalog/Amazon/{market.code}] {query_kind}:{q} p{n}: {len(rows)} 结果 / "
                    f"本页新增真电视 {new_real} / 累计 {len(by_asin)} / 过滤 {filtered}"
                )
                if new_real == 0:
                    consecutive_empty += 1
                    if consecutive_empty >= 2:
                        break
                else:
                    consecutive_empty = 0
                await asyncio.sleep(random.uniform(1.0, 2.2))

        query_plan = (
            [(q, MAX_PAGES, "brand") for q in BRAND_QUERIES]
            + [
                (f"{q} {year}", min(MAX_PAGES, YEAR_MAX_PAGES), "year")
                for q in BRAND_QUERIES
                for year in TARGET_YEARS
            ]
            + [
                (q, min(MAX_PAGES, EXTRA_MAX_PAGES), "extra")
                for q in EXTRA_SERIES_QUERIES
            ]
        )
        completed_queries: set[str] = set()
        for q, max_pages, query_kind in query_plan:
            normalized_query = re.sub(r"\s+", " ", q.strip().lower())
            if not normalized_query or normalized_query in completed_queries:
                continue
            completed_queries.add(normalized_query)
            await scrape_query(q, max_pages, query_kind)

        # Amazon 有些尺寸是完全独立的 ASIN，详情页没有 twister sibling。
        # 用宽搜中识别出的系列做一页精确搜索，补上这类“页面之间互不相连”的尺寸。
        rescue_queries = self._series_rescue_queries(list(by_asin.values()))
        print(
            f"[catalog/Amazon/{market.code}] 系列精确补抓 queries={len(rescue_queries)} "
            f"(max_pages={SERIES_RESCUE_MAX_PAGES})…"
        )
        for q in rescue_queries:
            normalized_query = re.sub(r"\s+", " ", q.strip().lower())
            if normalized_query in completed_queries:
                continue
            completed_queries.add(normalized_query)
            await scrape_query(q, SERIES_RESCUE_MAX_PAGES, "series")

        if variant_seeds:
            added_total = 0
            selected_seeds = self._select_variant_seeds(list(variant_seeds.values()))
            print(
                f"[catalog/Amazon/{market.code}] 多尺寸补全 candidates={len(variant_seeds)} / "
                f"selected={len(selected_seeds)} "
                f"(max_per_seed={MAX_VARIANTS_PER_SEED})…"
            )
            for i, seed in enumerate(selected_seeds, 1):
                added = await self._expand_variants_from_seed(page, seed, by_asin)
                added_total += added
                if added:
                    print(
                        f"[catalog/Amazon/{market.code}] variant {i}/{len(selected_seeds)} "
                        f"{seed.extra.get('asin')} +{added} / 累计 {len(by_asin)}"
                    )
                await asyncio.sleep(random.uniform(0.8, 1.5))
            print(f"[catalog/Amazon/{market.code}] 多尺寸补全新增 {added_total} 条 / 总计 {len(by_asin)}")
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
