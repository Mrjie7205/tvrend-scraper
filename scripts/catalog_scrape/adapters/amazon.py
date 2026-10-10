"""Amazon 多国家 catalog 抓取。

当前生产策略：
- 四国均须确认本地配送邮编，再验证固定 ASIN canary。
- EUR/GBP 币种不能替代配送地验证；配送国外可能仍显示 EUR，却已扣除本地税。
- 配送地不能确认或目录不完整时只保存隔离诊断，拒绝写正式 catalog。

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
import csv
import hashlib
import os
import random
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Sequence
from urllib.parse import parse_qs, quote_plus, urlparse

from monitor_prices.core import clean_price
from monitor_prices.fx import ECB_RATE_DATE, price_to_eur

from .base import BaseCatalogAdapter, CatalogItem
from catalog_scrape.diagnostics import AmazonCatalogDiagnostics, capture_catalog_failure


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
YEAR_MAX_PAGES = int(os.environ.get("AMAZON_YEAR_MAX_PAGES", "1"))
SERIES_RESCUE_MAX_PAGES = int(os.environ.get("AMAZON_SERIES_RESCUE_MAX_PAGES", "1"))
MAX_SERIES_RESCUE_QUERIES = int(os.environ.get("AMAZON_MAX_SERIES_RESCUE_QUERIES", "25"))
EXPAND_VARIANTS = os.environ.get("AMAZON_EXPAND_VARIANTS", "true").lower() != "false"
MAX_VARIANT_SEEDS = int(os.environ.get("AMAZON_MAX_VARIANT_SEEDS", "40"))
MAX_VARIANTS_PER_SEED = int(os.environ.get("AMAZON_MAX_VARIANTS_PER_SEED", "10"))
MAX_SEEDS_PER_SERIES = int(os.environ.get("AMAZON_MAX_SEEDS_PER_SERIES", "2"))
SESSION_PREP_ATTEMPTS = int(os.environ.get("AMAZON_SESSION_PREP_ATTEMPTS", "3"))
PREVIOUS_CATALOG_LOOKBACK = int(os.environ.get("AMAZON_PREVIOUS_CATALOG_LOOKBACK", "7"))
MAX_PREVIOUS_RECOVERY_ITEMS = int(os.environ.get("AMAZON_MAX_PREVIOUS_RECOVERY_ITEMS", "40"))
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
    r"|fußständer|tv[- ]?ständer|tv[- ]?stand|tv[- ]?beine|netzteil|fernbedienung"
    r"|mounting\s+screws?|vesa\s+screws?|ladegerät|power\s*(?:supply|adapter)|tv\s+power\s+adapter",
    re.IGNORECASE,
)

# `remote` 不能整体禁用：LG 等电视本体标题会正常出现 "Magic Remote"。
# 这里只拦截明确描述“替换/兼容遥控器”的短语，补住 Amazon GB 曾把
# `WKOLF Replace Remote suit for ... T6C ... TV` 当成电视本体的缺口。
RE_REMOTE_ACCESSORY = re.compile(
    r"\breplace(?:ment)?\s+remote\b"
    r"|\bremote\s+(?:suit(?:able)?\s+for|compatible\s+with|for)\b"
    r"|\buniversal\s+(?:tv\s+)?remote(?:\s+control)?\b"
    r"|\btelecomando\s+(?:sostitutivo|di\s+ricambio|compatibile)\b"
    r"|\bmando\s+(?:de\s+reemplazo|sustituto|compatible)\b",
    re.IGNORECASE,
)


def is_non_tv_title(title: str) -> bool:
    """判断 Amazon 标题是否明确属于配件/非电视本体。"""
    text = str(title or "")
    return bool(RE_NON_TV.search(text) or RE_REMOTE_ACCESSORY.search(text))

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

_JS_SEARCH_STATE = r"""() => {
  const text = document.body?.innerText || '';
  return {
    currentUrl: location.href,
    productAsin: (document.querySelector('#ASIN')?.value
      || document.querySelector('input[name="ASIN"]')?.value || '').trim().toUpperCase(),
    cardCount: document.querySelectorAll("div[data-component-type='s-search-result']").length,
    nextPresent: !!document.querySelector('a.s-pagination-next'),
    nextDisabled: !!document.querySelector('.s-pagination-next.s-pagination-disabled'),
    deliveryText: (document.querySelector('#glow-ingress-line2')?.textContent
      || document.querySelector('#glow-ingress-block')?.textContent || '').trim().replace(/\s+/g, ' '),
    captcha: /captcha|enter the characters you see below|api-services-support@amazon.com/i.test(text),
    robotCheck: /robot check|not a robot|automated access|unusual traffic|access denied|accesso negato|verify you are human|security check/i.test(text),
    normalPage: Array.from(document.querySelectorAll('#nav-main, #glow-ingress-block, #productTitle, [data-component-type=s-search-result]')).some(el => el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden'),
    continueShopping: /Fai clic sul pulsante qui sotto per continuare a fare acquisti|Click the button below to continue shopping|Klicke auf die Schaltfläche unten, um mit dem Einkauf(?:en)? fortzufahren|Haz clic en el botón de abajo para seguir comprando/i.test(text)
    ,accessChallengeTarget: Array.from(document.querySelectorAll('form, button, input[type=submit]')).some(el => {
      if (!el.getClientRects().length) return false;
      const raw = el.getAttribute('action') || el.getAttribute('formaction') || el.form?.getAttribute('action');
      try { return /\/validatecaptcha\/?$/i.test(new URL(raw || '', location.href).pathname); } catch { return false; }
    })
  };
}"""

_JS_CONTINUE_PAGE_INSPECTION = r"""() => {
  const visible = el => {
    if (!el || !el.getClientRects().length) return false;
    const box = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return box.width > 0 && box.height > 0 && style.display !== 'none' &&
      style.visibility !== 'hidden' && style.visibility !== 'collapse' && Number(style.opacity) > 0 &&
      (!el.checkVisibility || el.checkVisibility({checkOpacity: true, checkVisibilityCSS: true}));
  };
  const clean = s => (s || '').trim().replace(/\s+/g, ' ');
  const destination = raw => {
    if (!raw) return null;
    try { const u = new URL(raw, location.href); return {origin: u.origin, path: u.pathname}; }
    catch { return {invalid: true}; }
  };
  const text = clean(document.body?.innerText || '');
  const name = el => {
    const refs = (el.getAttribute('aria-labelledby') || '').trim().split(/\s+/).filter(Boolean);
    const labelled = clean(refs.map(id => document.getElementById(id)?.textContent || '').join(' '));
    if (labelled) return {label: labelled, source: 'aria-labelledby'};
    const aria = clean(el.getAttribute('aria-label'));
    if (aria) return {label: aria, source: 'aria-label'};
    return {label: clean(el.innerText || (el.matches('input[type=submit],input[type=button]') ? el.getAttribute('value') : '')),
            source: refs.length ? 'unresolved-reference' : 'visible-text'};
  };
  const nodes = Array.from(document.querySelectorAll('button,input[type=submit],input[type=button],[role=button]'));
  const submits = Array.from(document.querySelectorAll('button,input[type=submit]')).filter(el => el.type === 'submit');
  const forms = Array.from(document.forms);
  const controls = nodes.filter(visible).map(el => {
    const direct = submits.includes(el);
    const widget = !direct && ['SPAN','DIV'].includes(el.tagName) && el.getAttribute('role') === 'button'
      ? el.closest('.a-button') : null;
    const contained = widget ? submits.filter(candidate => widget.contains(candidate)) : [];
    const native = direct ? el : contained.length === 1 ? contained[0] : null;
    const form = native?.form;
    const label = name(el);
    return {
      tag: el.tagName.toLowerCase(), type: el.getAttribute('type') || '',
      label: label.label, labelSource: label.source,
      href: destination(el.getAttribute('href')),
      formMethod: native?.getAttribute('formmethod') || form?.method || '',
      formAction: destination(native?.getAttribute('formaction') || form?.getAttribute('action')),
      formTarget: native?.getAttribute('formtarget') || form?.target || '',
      hasAriaLabelledBy: !!el.getAttribute('aria-labelledby'),
      nativeSubmit: !!native, nativeIndex: submits.indexOf(native), formIndex: forms.indexOf(form),
      surfaceIndex: nodes.indexOf(el), kind: direct ? 'native_submit' : native ? 'aui_wrapper' : 'other',
      namedSubmit: !!native?.getAttribute('name'),
      disabled: !!native?.disabled || el.getAttribute('aria-disabled') === 'true',
    };
  });
  return {
    current: {origin: location.origin, path: location.pathname},
    normalPage: Array.from(document.querySelectorAll('#nav-main, #glow-ingress-block, #productTitle, [data-component-type=s-search-result]')).some(visible),
    visibleChallengeControls: Array.from(document.querySelectorAll('input[name*=captcha i], input[id*=captcha i], input[type=checkbox], iframe[src*=captcha i], [class*=g-recaptcha], [class*=h-captcha]')).some(visible),
    challengeLanguage: /captcha|robot|unusual traffic|automated access|access denied|accesso negato|verify you are human|security check/i.test(text),
    continueShoppingInstruction: /Fai clic sul pulsante qui sotto per continuare a fare acquisti|Click the button below to continue shopping|Klicke auf die Schaltfläche unten, um mit dem Einkauf(?:en)? fortzufahren|Haz clic en el botón de abajo para seguir comprando/i.test(text),
    formCount: document.forms.length,
    visibleInputCount: Array.from(document.querySelectorAll('input, textarea, select, [contenteditable=true]')).filter(el =>
      visible(el) && !['hidden','submit','button'].includes((el.type || '').toLowerCase())).length,
    visibleFrameCount: Array.from(document.querySelectorAll('iframe')).filter(visible).length,
    controls,
  };
}"""


async def inspect_amazon_continue_page(page) -> dict:
    """只读核对中间页的导航目标；不读取隐藏字段、不点击、不保存会话值。"""
    return await page.evaluate(_JS_CONTINUE_PAGE_INSPECTION)

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
    detail_canary: tuple[tuple[str, float], ...] = ()


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
    detail_canary=(
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
    detail_canary=(
        ("B0F7WHLF6M", 149.0),  # Hisense 32A4QTUK
        ("B0F9PP6BKT", 138.0),  # TCL 32SF560-UK
    ),
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
    detail_canary=(
        ("B0GM1HFTNL", 159.0),  # Hisense 32E4ST
        ("B0F54B34TH", 159.0),  # TCL 32V4C
    ),
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
    detail_canary=(
        ("B0GJFWX36Y", 154.9),  # Hisense 32A4S
        ("B0F9PVTQL2", 149.0),  # TCL 32SF560
    ),
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


def _delivery_postcode_matches(text: str, market: AmazonMarket) -> bool:
    """只认配送栏，不从页面其他位置或刚输入的表单推断配送地。"""
    visible_text = re.sub(r'[\u200b-\u200f\ufeff]', '', str(text or ''))
    normalized = re.sub(r'\s+', ' ', visible_text).strip().upper()
    parts = market.postcode.upper().split()
    expected = r'\s*'.join(re.escape(part) for part in parts)
    return bool(expected and re.search(r'(?<![A-Z0-9])' + expected + r'(?![A-Z0-9])', normalized))


class AmazonCatalogIncomplete(RuntimeError):
    """本轮存在明确错误或访问挑战，已有候选只能保留在隔离诊断中。"""


async def _capture_amazon_failure(page, market, *, stage, reason, adapter=None, **kwargs):
    if page is not None:
        vars(page)['_amazon_failure_reason'] = reason
    return await capture_catalog_failure(
        page, platform='Amazon', country=market.code,
        stage=stage, reason=reason, adapter=adapter, **kwargs,
    )


def _page_rejection_reason(http_status: int | None, state: dict) -> str | None:
    if state.get('captcha') or state.get('robotCheck') or state.get('accessChallengeTarget'):
        return 'access_challenge'
    if state.get('continueShopping'):
        return 'continue_shopping_interstitial'
    if isinstance(http_status, int) and http_status >= 400:
        return f'http_{http_status}'
    return None


_CONTINUE_LABELS = {
    'DE': 'Weiter shoppen', 'GB': 'Continue shopping',
    'IT': 'Continua con gli acquisti', 'ES': 'Seguir comprando',
}


def _plain_continue_entry_control(inspection: dict, market: AmazonMarket) -> dict | None:
    """仅接受已见过的单按钮入口，不读取/拼接隐藏参数，也不处理人机输入。"""
    if not isinstance(inspection, dict):
        return None
    current = inspection.get('current') or {}
    if not isinstance(current, dict):
        return None
    if (
        current.get('origin') != market.base_url or current.get('path') not in ('', '/')
        or inspection.get('normalPage') is not False
        or inspection.get('visibleChallengeControls') is not False
        or inspection.get('challengeLanguage') is not False
        or inspection.get('continueShoppingInstruction') is not True
        or inspection.get('formCount') != 1
        or inspection.get('visibleInputCount') != 0
        or inspection.get('visibleFrameCount') != 0
    ):
        return None
    controls = inspection.get('controls') or []
    if not controls:
        return None
    for control in controls:
        if not isinstance(control, dict) or (
            control.get('kind') not in ('native_submit', 'aui_wrapper')
            or control.get('nativeSubmit') is not True
            or control.get('disabled') is not False
            or not isinstance(control.get('surfaceIndex'), int) or control['surfaceIndex'] < 0
            or not isinstance(control.get('nativeIndex'), int) or control['nativeIndex'] < 0
            or not isinstance(control.get('formIndex'), int) or control['formIndex'] < 0
            or control.get('label') != _CONTINUE_LABELS.get(market.code)
            or str(control.get('formMethod') or '').lower() != 'get'
            or control.get('formAction') != {'origin': market.base_url, 'path': '/errors_page/validateCaptcha'}
            or control.get('formTarget', '') not in ('', '_self')
            or control.get('href') is not None
        ):
            return None
    if len({control['formIndex'] for control in controls}) != 1:
        return None
    # 同一个 native submit 的AUI外层/内层是一个操作；不同submit只有不带各自name值才等价。
    if len({control['nativeIndex'] for control in controls}) != 1 and any(
        control.get('namedSubmit') for control in controls
    ):
        return None
    return min(controls, key=lambda control: (control['kind'] != 'native_submit', control['surfaceIndex']))


def _continue_inspection_summary(inspection, market: AmazonMarket) -> dict:
    """拒绝原因仅留枚举、计数和已知按钮名，不记录 URL、引用 ID 或隐藏值。"""
    if not isinstance(inspection, dict):
        return {'rejection_reason': 'invalid_inspection'}
    current = inspection.get('current') or {}
    controls = inspection.get('controls') or []
    safe_controls = []
    for control in controls[:8]:
        if not isinstance(control, dict):
            continue
        action = control.get('formAction') or {}
        safe_controls.append({
            'tag': control.get('tag') if control.get('tag') in ('button', 'input', 'a', 'span', 'div') else 'other',
            'type': control.get('type') if control.get('type') in ('', 'button', 'submit') else 'other',
            'known_label': control.get('label') in _CONTINUE_LABELS.values(),
            'label': control.get('label') if control.get('label') in _CONTINUE_LABELS.values() else '[unrecognized]',
            'has_aria_labelledby': control.get('hasAriaLabelledBy') is True,
            'label_source': control.get('labelSource') if control.get('labelSource') in (
                'aria-labelledby', 'aria-label', 'unresolved-reference', 'visible-text',
            ) else 'unknown',
            'native_submit': control.get('nativeSubmit') is True,
            'kind': control.get('kind') if control.get('kind') in ('native_submit', 'aui_wrapper') else 'other',
            'disabled': control.get('disabled') is True,
            'method_get': str(control.get('formMethod') or '').lower() == 'get',
            'same_market_action': action == {'origin': market.base_url, 'path': '/errors_page/validateCaptcha'},
        })
    same_market = isinstance(current, dict) and current.get('origin') == market.base_url
    root_path = isinstance(current, dict) and current.get('path') in ('', '/')
    reason = 'allowed'
    checks = [
        (not same_market or not root_path, 'wrong_market_or_path'),
        (inspection.get('normalPage') is not False, 'not_entry_page'),
        (inspection.get('visibleChallengeControls') is not False or inspection.get('challengeLanguage') is not False, 'visible_challenge'),
        (inspection.get('continueShoppingInstruction') is not True, 'instruction_unrecognized'),
        (inspection.get('formCount') != 1, 'form_count_mismatch'),
        (inspection.get('visibleInputCount') != 0 or inspection.get('visibleFrameCount') != 0, 'visible_input_or_frame'),
        (not controls, 'no_visible_control'),
    ]
    for failed, code in checks:
        if failed:
            reason = code
            break
    if reason == 'allowed' and _plain_continue_entry_control(inspection, market) is None:
        control = next((control for control in controls if isinstance(control, dict)
                        and control.get('label') != _CONTINUE_LABELS.get(market.code)), {})
        if control:
            reason = 'label_reference_unresolved' if control.get('hasAriaLabelledBy') else 'label_unrecognized'
        elif len(controls) > 1:
            reason = 'multiple_actions_or_unsupported_controls'
        else:
            reason = 'action_or_method_rejected'
    indexes = [control.get('formIndex') for control in controls if isinstance(control, dict)]
    return {
        'rejection_reason': reason, 'control_count': len(controls),
        'form_count': inspection.get('formCount'), 'visible_input_count': inspection.get('visibleInputCount'),
        'visible_frame_count': inspection.get('visibleFrameCount'), 'same_market': same_market,
        'root_path': root_path, 'same_form': bool(indexes and all(index == indexes[0] and isinstance(index, int) and index >= 0 for index in indexes)),
        'unique_allowed_operation': reason == 'allowed',
        'duplicate_representations': reason == 'allowed' and len(controls) > 1,
        'controls': safe_controls,
    }


def _continue_structure_may_settle(inspection: dict, market: AmazonMarket) -> bool:
    """只在已明确识别的纯继续页上短等结构，不等待或处理人工验证。"""
    current = inspection.get('current') or {}
    for control in inspection.get('controls') or []:
        if not isinstance(control, dict) or (
            control.get('formAction') not in (None, {'origin': market.base_url, 'path': '/errors_page/validateCaptcha'})
            or str(control.get('formMethod') or '').lower() not in ('', 'get')
            or control.get('href') is not None
        ):
            return False
    return bool(
        isinstance(current, dict) and current.get('origin') == market.base_url
        and current.get('path') in ('', '/') and inspection.get('normalPage') is False
        and inspection.get('continueShoppingInstruction') is True
        and inspection.get('visibleChallengeControls') is False
        and inspection.get('challengeLanguage') is False
        and inspection.get('visibleInputCount') == 0 and inspection.get('visibleFrameCount') == 0
        and inspection.get('formCount') in (0, 1)
    )


def _normal_market_page(state: dict, market: AmazonMarket) -> bool:
    if not isinstance(state, dict):
        return False
    current = urlparse(str(state.get('currentUrl') or ''))
    expected = urlparse(market.base_url)
    return bool(
        state.get('normalPage') is True and current.scheme == 'https'
        and current.netloc == expected.netloc
        and not current.path.startswith(('/errors', '/ap/'))
        and _page_rejection_reason(None, state) is None
    )


async def _follow_plain_continue_entry(page, market: AmazonMarket, http_status: int | None):
    if http_status != 200:
        return None
    try:
        inspection = await inspect_amazon_continue_page(page)
        control = _plain_continue_entry_control(inspection, market)
        safe_inspection = _continue_inspection_summary(inspection, market)
        safe_inspection.update(checks=1, waited_for_structure=False)
        if control is None and safe_inspection['rejection_reason'] in {
            'no_visible_control', 'label_reference_unresolved', 'form_count_mismatch',
        } and _continue_structure_may_settle(inspection, market):
            initial_reason = safe_inspection['rejection_reason']
            await page.wait_for_timeout(1200)
            inspection = await inspect_amazon_continue_page(page)
            control = _plain_continue_entry_control(inspection, market)
            safe_inspection = _continue_inspection_summary(inspection, market)
            safe_inspection.update(checks=2, waited_for_structure=True, initial_rejection_reason=initial_reason)
    except Exception:
        return None
    existing = vars(page).get('_amazon_continue_navigation_summary') or {'attempts': 0, 'result': 'not_attempted'}
    existing['inspection'] = safe_inspection
    vars(page)['_amazon_continue_navigation_summary'] = existing
    if control is None:
        return None
    if vars(page).get('_amazon_continue_navigation_used'):
        summary = vars(page).get('_amazon_continue_navigation_summary')
        if isinstance(summary, dict):
            summary['repeat_rejected'] = True
        raise AmazonCatalogIncomplete(f'Amazon {market.code} 继续入口重复出现，本轮停止')
    # 限额在点击前扣除；超时或再次出现中间页都不能再点第二次。
    vars(page)['_amazon_continue_navigation_used'] = True
    summary = {
        'attempts': 1, 'result': 'attempted', 'verified_normal_page': False,
        'verified_same_market': False, 'repeat_rejected': False,
        'inspection': safe_inspection,
    }
    vars(page)['_amazon_continue_navigation_summary'] = summary
    try:
        async with page.expect_navigation(wait_until='domcontentloaded', timeout=30000) as navigation:
            approved = page.locator('button,input[type=submit],input[type=button],[role=button]').nth(control['surfaceIndex'])
            await page.get_by_role('button', name=control['label'], exact=True).and_(approved).click(timeout=5000)
        response = await navigation.value
        status = response.status if response else None
        state = await page.evaluate(_JS_SEARCH_STATE)
        if status == 200 and not _page_rejection_reason(status, state) and not state.get('normalPage'):
            await page.wait_for_timeout(1200)
            state = await page.evaluate(_JS_SEARCH_STATE)
        if status != 200 or not _normal_market_page(state, market):
            raise AmazonCatalogIncomplete(f'Amazon {market.code} 继续入口后未得到同市场正常商店页面')
        summary.update(result='normal_page_restored', verified_normal_page=True, verified_same_market=True)
        print(f'[catalog/Amazon/{market.code}] 已完成一次站点继续入口，恢复正常页后仍须配送与价格校验')
        return state
    except Exception as error:
        summary.update(result='rejected', error_type=type(error).__name__)
        await _capture_amazon_failure(
            page, market, stage='continue_entry', reason='continue_navigation_rejected', error=error,
        )
        if isinstance(error, AmazonCatalogIncomplete):
            raise
        raise AmazonCatalogIncomplete(f'Amazon {market.code} 继续入口导航失败，本轮停止') from error


async def _checked_page_state(page, market: AmazonMarket, http_status: int | None = None,
                              *, stage: str = 'delivery') -> dict:
    """恢复配送会话前后都先排除错误页，不通过重试绕过访问验证。"""
    state = await page.evaluate(_JS_SEARCH_STATE)
    if not isinstance(state, dict):
        raise AmazonCatalogIncomplete(f'Amazon {market.code} 无法读取页面状态，停止采集')
    reason = _page_rejection_reason(http_status, state)
    if reason and not state.get('captcha') and not state.get('robotCheck'):
        resumed = await _follow_plain_continue_entry(page, market, http_status)
        if resumed is not None:
            return resumed
    if reason:
        error = AmazonCatalogIncomplete(f'Amazon {market.code} 页面不可用于配送恢复 ({reason})')
        error.retryable = reason.startswith('http_5')
        await _capture_amazon_failure(
            page, market, stage=stage, reason=reason, http_status=http_status, error=error,
        )
        raise error
    return state


def _recovery_page_matches(state: dict, target_url: str, *, asin: str = '', require_asin: bool = True) -> bool:
    """地址设置会离开商品页；必须回到原站点和原商品/搜索条件才准许取价。"""
    actual = urlparse(str(state.get('currentUrl') or ''))
    expected = urlparse(target_url)
    if actual.scheme != 'https' or actual.netloc != expected.netloc:
        return False
    if asin:
        match = re.search(r'/(?:dp|gp/product)/([A-Z0-9]{10})(?:/|$)', actual.path, re.I)
        if not match or match.group(1).upper() != asin.upper():
            return False
        observed = str(state.get('productAsin') or '').upper()
        return observed == asin.upper() if require_asin or observed else True
    actual_query, expected_query = parse_qs(actual.query), parse_qs(expected.query)
    return actual.path == expected.path and all(
        actual_query.get(key) == expected_query.get(key) for key in ('k', 'page')
    )


async def ensure_amazon_page_delivery(
    page, market: AmazonMarket, target_url: str, *, asin: str = '',
    http_status: int | None = None, state: dict | None = None,
) -> dict:
    try:
        return await _ensure_amazon_page_delivery(
            page, market, target_url, asin=asin, http_status=http_status, state=state,
        )
    except Exception as error:
        await _capture_amazon_failure(
            page, market, stage='delivery_recovery', reason='delivery_recovery_rejected',
            url=target_url, http_status=http_status, product=asin or None, error=error,
        )
        raise


async def _ensure_amazon_page_delivery(
    page, market: AmazonMarket, target_url: str, *, asin: str = '',
    http_status: int | None = None, state: dict | None = None,
) -> dict:
    """配送栏迟到时短等一次；仍不符只重设一次地址，失败则整轮拒绝。

    此处不清 cookie、不切换 IP；只有结构白名单确认的普通继续入口可整轮导航一次。
    真实人机验证或重复中间页仍立即停止。地址恢复成功后重新
    导航并核对目标页面，调用方只能抽取返回后页面，不能复用旧行或旧价格。
    """
    state = state if state is not None else await _checked_page_state(page, market, http_status)
    reason = _page_rejection_reason(http_status, state)
    if reason:
        raise AmazonCatalogIncomplete(f'Amazon {market.code} 页面不可用于配送恢复 ({reason})')
    if _delivery_postcode_matches(state.get('deliveryText', ''), market):
        return state
    if not _recovery_page_matches(state, target_url, asin=asin, require_asin=False):
        raise AmazonCatalogIncomplete(f'Amazon {market.code} 配送复核页面身份不符，停止采集')
    print(f'[catalog/Amazon/{market.code}] 配送栏未确认，等待后重新核对原页面')
    await page.wait_for_timeout(1500)
    state = await _checked_page_state(page, market, http_status)
    if not _recovery_page_matches(state, target_url, asin=asin):
        raise AmazonCatalogIncomplete(f'Amazon {market.code} 等待后页面身份不符，停止采集')
    if _delivery_postcode_matches(state.get('deliveryText', ''), market):
        return state
    print(f'[catalog/Amazon/{market.code}] 配送栏仍未确认，仅恢复一次本地配送地址')
    if not await set_amazon_market_location(page, market):
        raise AmazonCatalogIncomplete(f'Amazon {market.code} 配送地址恢复失败，拒绝本轮目录')
    try:
        response = await page.goto(target_url, wait_until='domcontentloaded', timeout=30000)
        status = response.status if response else None
        await _checked_page_state(page, market, status)
        await page.wait_for_timeout(1500)
        state = await _checked_page_state(page, market, status)
    except AmazonCatalogIncomplete:
        raise
    except Exception as exc:
        raise AmazonCatalogIncomplete(f'Amazon {market.code} 恢复后原页面加载失败，拒绝本轮目录') from exc
    if not _recovery_page_matches(state, target_url, asin=asin):
        raise AmazonCatalogIncomplete(f'Amazon {market.code} 恢复后页面身份不符，停止采集')
    if not _delivery_postcode_matches(state.get('deliveryText', ''), market):
        raise AmazonCatalogIncomplete(f'Amazon {market.code} 恢复后配送地仍未确认，拒绝混入境外配送价格')
    print(f'[catalog/Amazon/{market.code}] 原页面配送恢复成功，重新抽取当前数据')
    return state


async def verify_amazon_delivery_location(page, market: AmazonMarket, *, refresh: bool = False) -> bool:
    if refresh:
        try:
            response = await page.goto(f'{market.base_url}/', wait_until='domcontentloaded', timeout=45000)
            await _checked_page_state(page, market, response.status if response else None)
            await page.wait_for_timeout(1200)
        except AmazonCatalogIncomplete:
            raise
        except Exception as exc:
            await _capture_amazon_failure(
                page, market, stage='delivery_verification', reason='navigation_error', error=exc,
            )
            print(f'  [set-loc/{market.code}] 配送地复核加载失败: {type(exc).__name__}')
            return False
    try:
        state = await page.evaluate(_JS_SEARCH_STATE)
    except Exception as exc:
        await _capture_amazon_failure(
            page, market, stage='delivery_verification', reason='state_read_error', error=exc,
        )
        return False
    reason = _page_rejection_reason(None, state)
    if reason:
        error = AmazonCatalogIncomplete(f'Amazon {market.code} 页面出现访问挑战，停止本轮采集 ({reason})')
        await _capture_amazon_failure(page, market, stage='delivery_verification', reason=reason, error=error)
        raise error
    text = state.get('deliveryText') or ''
    ok = _delivery_postcode_matches(text, market)
    if not ok:
        await _capture_amazon_failure(
            page, market, stage='delivery_verification', reason='delivery_location_unverified',
        )
    print(f'  [set-loc/{market.code}] 配送栏复核 {text[:120]!r} -> {market.postcode}: {"OK" if ok else "FAIL"}')
    return ok


async def set_amazon_location_via_popup(page, market: AmazonMarket, *, reuse_current_page: bool = False) -> bool:
    """旧 glow toaster 接口为空时，用顶部配送地弹窗填邮编作为 fallback。"""
    try:
        if reuse_current_page:
            state = await _checked_page_state(page, market, stage='location_popup')
            if not _normal_market_page(state, market):
                raise AmazonCatalogIncomplete(f'Amazon {market.code} 无法确认可复用的同市场正常配送页面')
        else:
            response = await page.goto(f"{market.base_url}/", wait_until="domcontentloaded", timeout=45000)
            await _checked_page_state(page, market, response.status if response else None, stage='location_popup')
        await _accept_cookie(page)
        location_entry = page.locator(
            "#nav-global-location-popover-link, #glow-ingress-block, "
            "#glow-ingress-line1, #glow-ingress-line2"
        ).first
        try:
            await location_entry.click(timeout=8000)
        except Exception:
            # Amazon 新版顶部入口在无障碍树中是 button，部分页面不再保留旧 id。
            await page.get_by_role(
                "button",
                name=re.compile(r"deliver to|entregar en|invia a|enviar a|location", re.I),
            ).first.click(timeout=8000)
        await page.wait_for_timeout(1200)

        # Cookie 弹窗有时在首次点击配送地后才延迟出现，会遮住地址弹窗。
        await _accept_cookie(page)
        inp = page.locator(
            "#GLUXZipUpdateInput, [data-action='GLUXPostalInputAction'], "
            "input[autocomplete='postal-code']"
        ).first
        if await inp.count() == 0:
            # 新版先显示“当前配送到其他国家”的中间提示，需要再点 Change Address。
            try:
                await page.get_by_role(
                    "button",
                    name=re.compile(r"change address|change location|cambiar dirección", re.I),
                ).first.click(timeout=2500)
                await page.wait_for_timeout(800)
            except Exception:
                pass
            inp = page.locator(
                "#GLUXZipUpdateInput, [data-action='GLUXPostalInputAction'], "
                "input[autocomplete='postal-code']"
            ).first
        if await inp.count() == 0:
            # Cookie 层关闭后，原始点击可能没有真正打开地址弹窗；再尝试一次。
            try:
                await location_entry.click(timeout=3000)
                await page.wait_for_timeout(800)
            except Exception:
                pass
            inp = page.locator(
                "#GLUXZipUpdateInput, [data-action='GLUXPostalInputAction'], "
                "input[autocomplete='postal-code']"
            ).first
        if await inp.count() == 0:
            print(f"  [set-loc/{market.code}] 弹窗未出现邮编输入框")
            await _capture_amazon_failure(
                page, market, stage='location_popup', reason='postcode_input_missing',
            )
            return False
        await inp.fill(market.postcode, timeout=5000)
        submit = page.locator(
            "#GLUXZipUpdate, input[aria-labelledby='GLUXZipUpdate-announce'], "
            "[data-action='GLUXPostalUpdateAction']"
        ).first
        if await submit.count():
            await submit.click(timeout=5000)
        else:
            await page.get_by_role(
                "button",
                name=re.compile(r"apply|aplicar|usa questo indirizzo|utiliser", re.I),
            ).first.click(timeout=5000)
        await page.wait_for_timeout(2500)
        for sel in ("#GLUXConfirmClose", "input[name='glowDoneButton']", ".a-popover-footer .a-button-input"):
            try:
                await page.click(sel, timeout=1500)
                break
            except Exception:
                pass
        return await verify_amazon_delivery_location(page, market, refresh=True)
    except AmazonCatalogIncomplete:
        raise
    except Exception as e:
        await _capture_amazon_failure(
            page, market, stage='location_popup', reason='location_popup_error', error=e,
        )
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
        response = await page.goto(f"{market.base_url}/", wait_until="domcontentloaded", timeout=45000)
        await _checked_page_state(page, market, response.status if response else None, stage='location_home')
    except AmazonCatalogIncomplete:
        raise
    except Exception as e:
        await _capture_amazon_failure(
            page, market, stage='location_home', reason='navigation_error', error=e,
        )
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
        await _capture_amazon_failure(
            page, market, stage='location_api', reason='location_token_request_error', error=e,
        )
        print(f"  [set-loc/{market.code}] 取 CSRF token 失败: {e}")
        return False

    m = re.search(r'data-toaster-csrfToken="([^"]+)"', html)
    if not m:
        await _capture_amazon_failure(
            page, market, stage='location_api', reason='location_token_missing',
        )
        print(f"  [set-loc/{market.code}] 没找到 CSRF token，Amazon glow 可能改版")
        ok = await set_amazon_location_via_popup(page, market, reuse_current_page=True)
        if ok:
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
        await _capture_amazon_failure(
            page, market, stage='location_api', reason='address_request_error', error=e,
        )
        print(f"  [set-loc/{market.code}] POST address-change 失败: {e}")
        return False

    failure = _page_rejection_reason(res.get('status'), {})
    if failure:
        error = AmazonCatalogIncomplete(f'Amazon {market.code} 地址设置请求失败 ({failure})')
        await _capture_amazon_failure(
            page, market, stage='location_api', reason=failure,
            http_status=res.get('status'), error=error,
        )
        raise error
    ok = bool(res.get("updated"))
    print(
        f"  [set-loc/{market.code}] 配送地 -> {market.postcode}:"
        f"{'OK isAddressUpdated:1' if ok else 'FAIL 未生效'} (status={res.get('status')})"
    )
    if ok:
        ok = await verify_amazon_delivery_location(page, market, refresh=True)
    if not ok:
        # glow POST 经常返回 200 但 isAddressUpdated=0。此时仍应尝试可见弹窗，
        # 特别是 GB 必须设置本地邮编后才能验证原生 GBP。
        popup_ok = await set_amazon_location_via_popup(page, market, reuse_current_page=True)
        if popup_ok:
            return True
    return ok


async def verify_amazon_detail_canary(page, market: AmazonMarket) -> bool:
    ok_any = False
    for asin, known in market.detail_canary:
        try:
            response = await page.goto(f"{market.base_url}/dp/{asin}", wait_until="domcontentloaded", timeout=45000)
            state = await page.evaluate(_JS_SEARCH_STATE)
            if _page_rejection_reason(None, state):
                raise AmazonCatalogIncomplete(f'Amazon {market.code} canary 页面出现访问挑战，停止采集')
            if _page_rejection_reason(response.status if response else None, {}):
                await _capture_amazon_failure(
                    page, market, stage='canary', reason='canary_http_error',
                    http_status=response.status if response else None, product=asin,
                )
                continue
            await page.wait_for_timeout(1500)
            if not await verify_amazon_delivery_location(page, market):
                return False
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
        except AmazonCatalogIncomplete as e:
            await _capture_amazon_failure(
                page, market, stage='canary', reason='canary_rejected', product=asin, error=e,
            )
            raise
        except Exception as e:
            await _capture_amazon_failure(
                page, market, stage='canary', reason='canary_request_error', product=asin, error=e,
            )
            print(f"  [canary/{market.code}] {asin} 抓取异常: {str(e)[:80]}")
            continue
        local_price, currency, _eur = _price_pair(txt, market.currency)
        lo, hi = _CANARY_LO * known, _CANARY_HI * known
        if local_price is not None and lo <= local_price <= hi:
            print(f"  [canary/{market.code}] {asin} {local_price} {currency} in [{lo:.0f},{hi:.0f}] OK")
            ok_any = True
        else:
            await _capture_amazon_failure(
                page, market, stage='canary', reason='canary_price_invalid', product=asin,
            )
            print(
                f"  [canary/{market.code}] {asin} raw={txt[:24]!r} 不符"
                f"(要 {market.currency} 且 ∈[{lo:.0f},{hi:.0f}])"
            )
    if not ok_any:
        print(f"  [canary/{market.code}] FAIL 所有锚点不符 -> abort")
    return ok_any


async def verify_amazon_search_currency(page, market: AmazonMarket) -> bool:
    """GB 初期守门：确认设置地址后搜索页给的是原生 GBP，且价格在电视合理区间。"""
    url = f"{market.base_url}/s?k=hisense+{market.search_word}&page=1"
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(2000)
        rows = await page.evaluate(_JS_EXTRACT)
    except Exception as e:
        await _capture_amazon_failure(
            page, market, stage='search_canary', reason='canary_request_error', url=url, error=e,
        )
        print(f"  [canary/{market.code}] 搜索页校验失败: {e}")
        return False
    for r in rows:
        if r.get("sponsored"):
            continue
        title = (r.get("title") or "").strip()
        if is_non_tv_title(title):
            continue
        price, currency, _eur = _price_pair(r.get("price") or "", market.currency)
        if price is not None and 50 <= price <= 10000:
            print(f"  [canary/{market.code}] 搜索页 {price} {currency} 合理 OK")
            return True
    print(f"  [canary/{market.code}] 搜索页未找到合理 {market.currency} 电视价 -> abort")
    await _capture_amazon_failure(
        page, market, stage='search_canary', reason='canary_price_invalid', url=url,
    )
    return False


class AmazonCatalogAdapter(BaseCatalogAdapter):
    """Amazon 单市场 adapter。registry 用 amazon_de / amazon_gb 区分实例。"""

    platform_name = "Amazon"

    def __init__(self, market: AmazonMarket):
        self.market = market
        self.country = market.code
        self.locale_override = (market.locale, market.timezone)
        self.diagnostics: AmazonCatalogDiagnostics | None = None
        self._price_baseline = {}
        self._price_change_paths = []
        self.continue_navigation_summary = {'attempts': 0, 'result': 'not_attempted'}

    def _load_price_baseline(self, started_at: datetime, catalog_dir: Path | None = None) -> dict:
        """只从开始前最新合格正式目录取同市场 ASIN 原币价，不回退到诊断候选。"""
        from amazon_artifact_gate import inspect_csv, parse_time
        catalog_dir = catalog_dir or Path(__file__).resolve().parents[3] / 'catalog'
        country = self.country.lower()
        for path in sorted(catalog_dir.glob(f'amazon_{country}_*.csv'), reverse=True):
            try:
                info = inspect_csv(path, country, '1970-01-01T00:00:00Z', started_at.isoformat())
                payload = path.read_bytes()
                if hashlib.sha256(payload).hexdigest() != info['sha256']:
                    continue
                rows = list(csv.DictReader(payload.decode('utf-8-sig').splitlines()))
                if any(parse_time(row['scraped_at']) >= started_at for row in rows):
                    continue
            except (OSError, UnicodeError, ValueError, csv.Error):
                continue
            result, ambiguous = {}, set()
            for row in rows:
                asin = (row.get('asin') or '').strip().upper()
                if not re.fullmatch(r'[A-Z0-9]{10}', asin):
                    continue
                if row.get('currency') != self.market.currency or not row.get('price_local'):
                    continue
                parsed = urlparse(row.get('url') or '')
                if parsed.netloc != urlparse(self.market.base_url).netloc or not re.search(
                    rf'/(?:dp|gp/product)/{re.escape(asin)}(?:/|$)', parsed.path, re.I,
                ):
                    continue
                baseline = {
                    'price': row['price_local'], 'currency': row['currency'],
                    'observed_at': row['scraped_at'], 'source_file': path.name,
                    'identity_precision': 'country+ASIN+currency',
                }
                if asin in result and result[asin]['price'] != baseline['price']:
                    ambiguous.add(asin)
                else:
                    result[asin] = baseline
            return {asin: row for asin, row in result.items() if asin not in ambiguous}
        return {}

    async def _record_price_observation(self, page, item: CatalogItem, *, source: str,
                                        query=None, page_number=None) -> None:
        """在原观测页还打开时旁路取证，不为截图重新取价或改写正式价格。"""
        try:
            from price_anomalies import classify_change, record_price_change
            asin = item.extra.get('asin')
            baseline = self._price_baseline.get(asin)
            if not baseline or not classify_change(
                baseline['price'], item.price_local,
                old_currency=baseline['currency'], currency=item.currency,
            ):
                return
            evidence_page = page
            evidence_source = 'same_product_page'
            source_page_url = getattr(page, 'url', None)
            if source == 'catalog_search':
                evidence_source = 'same_search_page_asin_card'
                try:
                    card = page.locator(
                        f"[data-component-type='s-search-result'][data-asin='{asin}']",
                    ).first
                    if not await card.count():
                        raise ValueError('asin_card_missing')
                    await card.scroll_into_view_if_needed(timeout=2000)
                    if not await card.is_visible():
                        raise ValueError('asin_card_not_visible')
                except Exception:
                    evidence_page = None
                    evidence_source = 'search_observation_asin_card_unavailable'
            path = await record_price_change(
                baseline=baseline,
                observation={
                    'platform': 'Amazon', 'country': self.country, 'product': asin, 'asin': asin,
                    'price': item.price_local, 'currency': item.currency, 'url': item.url,
                    'observed_at': datetime.now(UTC).isoformat(), 'observation_source': source,
                    'source_page_url': source_page_url, 'source_query': query, 'source_page': page_number,
                    'ingestion_status': 'pending', 'validation_state': 'unvalidated',
                },
                page=evidence_page, evidence_source=evidence_source,
            )
            if path is not None:
                self._price_change_paths.append(path)
        except Exception:
            # 旁路证据出错不改变已观测报价或当前目录门禁。
            pass

    def finalize_price_observations(self, status: str, reason: str | None = None) -> None:
        from price_anomalies import update_price_change_status
        for path in self._price_change_paths:
            try:
                update_price_change_status(
                    path, ingestion_status='accepted' if status == 'validated' else 'rejected',
                    reason=reason,
                )
            except Exception:
                pass

    async def _prepare_market_session(self, page) -> bool:
        try:
            return await self._prepare_market_session_impl(page)
        finally:
            # 只写内部产生的次数/结果/布尔标志；不把 URL、表单或隐藏值带入摘要。
            raw = vars(page).get('_amazon_continue_navigation_summary') or {}
            keys = ('attempts', 'result', 'verified_normal_page', 'verified_same_market',
                    'repeat_rejected', 'error_type', 'inspection')
            self.continue_navigation_summary = {
                key: raw[key] for key in keys if key in raw
            } or {'attempts': 0, 'result': 'not_attempted'}
            if self.diagnostics is not None:
                self.diagnostics.report['continueNavigation'] = dict(self.continue_navigation_summary)
                try:
                    self.diagnostics._save_report()
                except Exception:
                    pass

    async def _prepare_market_session_impl(self, page) -> bool:
        """仅传输暂错可在同一会话退避；配送/币种拒绝或访问挑战不能靠重置身份恢复。"""
        market = self.market
        for attempt in range(1, SESSION_PREP_ATTEMPTS + 1):
            # 每次准备只复用本次下层留下的现场，不能误用上一次页面。
            vars(page).pop('_catalog_failure_evidence_path', None)
            vars(page).pop('_amazon_failure_reason', None)
            stage = 'session_location'
            try:
                location_ok = await set_amazon_market_location(page, market)
                canary_ok = False
                if location_ok:
                    stage = 'session_canary'
                    if market.detail_canary:
                        canary_ok = await verify_amazon_detail_canary(page, market)
                    else:
                        canary_ok = await verify_amazon_search_currency(page, market)
            except Exception as error:
                await _capture_amazon_failure(
                    page, market, stage=stage, reason='session_preparation_error',
                    error=error, adapter=self,
                )
                if getattr(error, 'retryable', False) and attempt < SESSION_PREP_ATTEMPTS:
                    await asyncio.sleep(3.0 * attempt)
                    continue
                raise
            if location_ok and canary_ok:
                if attempt > 1:
                    print(f"[catalog/Amazon/{market.code}] 会话守门第 {attempt} 次成功 OK")
                return True
            # 必须在下一次清状态/导航之前保存这一次真实页面。
            failure_reason = vars(page).get('_amazon_failure_reason')
            previous = vars(page).get('_catalog_failure_evidence_path')
            if previous is not None:
                self._failure_evidence_captured = True
                self._failure_evidence_path = previous
            else:
                await _capture_amazon_failure(
                    page, market, stage=stage,
                    reason='delivery_location_unverified' if not location_ok else 'canary_rejected',
                    adapter=self,
                )
            retryable = failure_reason in {
                'navigation_error', 'location_token_request_error', 'address_request_error',
                'canary_request_error', 'state_read_error',
            }
            if not retryable:
                return False
            if attempt < SESSION_PREP_ATTEMPTS:
                print(
                    f"[catalog/Amazon/{market.code}] 会话守门第 {attempt} 次失败 "
                    f"(location={location_ok}, canary={canary_ok})，重试…"
                )
                await asyncio.sleep(random.uniform(3.0, 6.0))
        print(
            f"[catalog/Amazon/{market.code}] FAIL 会话守门连续 {SESSION_PREP_ATTEMPTS} 次失败 -> abort"
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
        if is_non_tv_title(title):
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

    def _load_recent_catalog_items(self) -> list[CatalogItem]:
        """读取公库最近几天的同市场 catalog，给搜索结果抖动提供可验证的回查候选。"""
        catalog_dir = Path(__file__).resolve().parents[3] / "catalog"
        paths = sorted(catalog_dir.glob(f"amazon_{self.market.code.lower()}_*.csv"))
        by_asin: dict[str, CatalogItem] = {}
        for path in paths[-PREVIOUS_CATALOG_LOOKBACK:]:
            try:
                with path.open("r", encoding="utf-8-sig", newline="") as handle:
                    rows = list(csv.DictReader(handle))
            except Exception as exc:
                print(f"[catalog/Amazon/{self.market.code}] 读取历史 catalog {path.name} 失败: {exc}")
                continue
            for row in rows:
                asin = (row.get("asin") or "").strip().upper()
                title = (row.get("raw_text") or "").strip()
                brand = (row.get("brand_raw") or "").strip()
                try:
                    size = float(row.get("size_hint_inch") or 0)
                except (TypeError, ValueError):
                    size = 0
                if not asin or not title or not brand or not size:
                    continue
                item = self._build_item(asin, title, brand, size, row.get("price_local") or "")
                if not self._series_hint(item):
                    continue
                item.extra["history_catalog"] = path.name
                by_asin[asin] = item
        return list(by_asin.values())

    @classmethod
    def _select_previous_recovery_items(
        cls,
        historical: Sequence[CatalogItem],
        current: Sequence[CatalogItem],
    ) -> list[CatalogItem]:
        """公平选择今天缺失的历史系列尺寸；优先标题明确为 2026 的系列。"""
        current_keys = {
            ((item.brand_raw or "").upper(), cls._series_hint(item), int(item.size_hint_inch or 0))
            for item in current
            if cls._series_hint(item)
        }
        series_sizes: dict[tuple[str, str], set[int]] = {}
        candidates: dict[str, list[CatalogItem]] = {}
        seen_asins: set[str] = set()
        for item in historical:
            brand = (item.brand_raw or "").upper()
            series = cls._series_hint(item)
            size = int(item.size_hint_inch or 0)
            asin = (item.extra.get("asin") or "").upper()
            if not series or not size or not asin or (brand, series, size) in current_keys or asin in seen_asins:
                continue
            seen_asins.add(asin)
            series_sizes.setdefault((brand, series), set()).add(size)
            candidates.setdefault(brand, []).append(item)

        def priority(item: CatalogItem) -> tuple[int, int, str, int]:
            brand = (item.brand_raw or "").upper()
            series = cls._series_hint(item)
            series_2026 = (
                (brand == "SAMSUNG" and series.endswith("H"))
                or (brand == "TCL" and series.endswith("L"))
                or (brand == "LG" and (series.endswith("6") or series.endswith("B")))
                or (brand == "HISENSE" and series.endswith("S"))
            )
            explicit_2026 = 0 if (re.search(r"\b2026\b", item.raw_text or "") or series_2026) else 1
            family_width = -len(series_sizes.get((brand, series), set()))
            return explicit_2026, family_width, series, int(item.size_hint_inch or 0)

        for brand in candidates:
            candidates[brand].sort(key=priority)
        brand_order = list(TARGET_BRAND_ORDER) + sorted(set(candidates) - set(TARGET_BRAND_ORDER))
        selected: list[CatalogItem] = []
        while len(selected) < MAX_PREVIOUS_RECOVERY_ITEMS and any(candidates.get(b) for b in brand_order):
            for brand in brand_order:
                queue = candidates.get(brand) or []
                if queue:
                    selected.append(queue.pop(0))
                if len(selected) >= MAX_PREVIOUS_RECOVERY_ITEMS:
                    break
        return selected

    async def _detail_item(self, page, asin: str, fallback_brand: str, fallback_variant_text: str = "") -> CatalogItem | None:
        market = self.market
        try:
            response = await page.goto(f"{market.base_url}/dp/{asin}", wait_until="domcontentloaded", timeout=30000)
            state = await page.evaluate(_JS_SEARCH_STATE)
            if _page_rejection_reason(None, state):
                raise AmazonCatalogIncomplete(f'Amazon {market.code} 详情页出现访问挑战，停止采集')
            failure = _page_rejection_reason(response.status if response else None, {})
            if failure:
                if self.diagnostics:
                    self.diagnostics.record_page({'queryKind': 'detail', 'asin': asin, 'reason': failure})
                await _capture_amazon_failure(
                    page, market, stage='catalog_detail', reason=failure,
                    http_status=response.status if response else None, product=asin, adapter=self,
                )
                return None
            await page.wait_for_timeout(random.randint(1300, 2200))
            await ensure_amazon_page_delivery(
                page, market, f'{market.base_url}/dp/{asin}', asin=asin,
                http_status=response.status if response else None,
            )
            detail = await page.evaluate(_JS_DETAIL, list(_AMZ_DETAIL_PRICE_SELECTORS))
        except AmazonCatalogIncomplete as e:
            await _capture_amazon_failure(
                page, market, stage='catalog_detail', reason='detail_rejected',
                product=asin, error=e, adapter=self,
            )
            raise
        except Exception as e:
            await _capture_amazon_failure(
                page, market, stage='catalog_detail', reason='detail_error',
                product=asin, error=e, adapter=self,
            )
            print(f"[catalog/Amazon/{market.code}] detail {asin} 失败: {str(e)[:100]}")
            return None
        title = (detail.get("title") or "").strip()
        if not title:
            await _capture_amazon_failure(
                page, market, stage='catalog_detail', reason='missing_product_title',
                product=asin, adapter=self,
            )
            return None
        brand = _brand_from_title(title) or fallback_brand
        size = _size_from_title(title) or _size_from_title(fallback_variant_text)
        if not brand or size is None:
            return None
        if is_non_tv_title(title):
            return None
        item = self._build_item(asin, title, brand, size, detail.get("price") or "")
        await self._record_price_observation(page, item, source='catalog_detail')
        return item

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
            response = await page.goto(seed.url, wait_until="domcontentloaded", timeout=30000)
            state = await page.evaluate(_JS_SEARCH_STATE)
            if _page_rejection_reason(None, state):
                raise AmazonCatalogIncomplete(f'Amazon {market.code} 变体页出现访问挑战，停止采集')
            failure = _page_rejection_reason(response.status if response else None, {})
            if failure:
                if self.diagnostics:
                    self.diagnostics.record_page({'queryKind': 'variant', 'asin': seed_asin, 'reason': failure})
                await _capture_amazon_failure(
                    page, market, stage='catalog_variant', reason=failure,
                    http_status=response.status if response else None, product=seed_asin, adapter=self,
                )
                return -1
            await page.wait_for_timeout(random.randint(1300, 2200))
            await ensure_amazon_page_delivery(
                page, market, seed.url, asin=seed_asin,
                http_status=response.status if response else None,
            )
            detail = await page.evaluate(_JS_DETAIL, list(_AMZ_DETAIL_PRICE_SELECTORS))
        except AmazonCatalogIncomplete as e:
            await _capture_amazon_failure(
                page, market, stage='catalog_variant', reason='variant_rejected',
                product=seed_asin, error=e, adapter=self,
            )
            raise
        except Exception as e:
            await _capture_amazon_failure(
                page, market, stage='catalog_variant', reason='variant_error',
                product=seed_asin, error=e, adapter=self,
            )
            print(f"[catalog/Amazon/{market.code}] variants {seed_asin} 失败: {str(e)[:100]}")
            return -1

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
        self._price_change_paths = []
        self._price_baseline = self._load_price_baseline(datetime.now(UTC))
        self.diagnostics = AmazonCatalogDiagnostics(market.code)
        if not await self._prepare_market_session(page):
            self.diagnostics.finish(status='rejected', reason='会话地址或价格 canary 守门失败')
            return []

        by_asin: dict[str, CatalogItem] = {}
        variant_seeds: dict[str, CatalogItem] = {}
        cookie_done = False

        async def scrape_query(q: str, max_pages: int, query_kind: str) -> bool:
            nonlocal cookie_done
            consecutive_empty = 0
            saw_rows = False
            for n in range(1, max_pages + 1):
                page_info = {'query': q, 'page': n, 'queryKind': query_kind}
                search_text = f"{q} {market.search_word}"
                url = f"{market.base_url}/s?k={quote_plus(search_text)}&page={n}"
                try:
                    timeout_ms = 20_000 if query_kind in {"year", "series"} else 45_000
                    response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                    page_info['httpStatus'] = response.status if response else None
                except Exception as e:
                    page_info.update(reason='navigation_failed', errorType=type(e).__name__)
                    self.diagnostics.record_page(page_info)
                    await _capture_amazon_failure(
                        page, market, stage='catalog_search', reason='navigation_error',
                        url=url, error=e, adapter=self,
                    )
                    print(f"[catalog/Amazon/{market.code}] {q} p{n} goto 失败: {e}")
                    if query_kind == 'brand':
                        raise AmazonCatalogIncomplete(f'Amazon {market.code} 主品牌 {q} 第 {n} 页导航失败，本轮目录不完整') from e
                    return False
                failure = _page_rejection_reason(page_info.get('httpStatus'), {})
                if failure:
                    page_info['reason'] = failure
                    self.diagnostics.record_page(page_info)
                    error = AmazonCatalogIncomplete(f'Amazon {market.code} 搜索页请求失败 ({failure})')
                    await _capture_amazon_failure(
                        page, market, stage='catalog_search', reason=failure,
                        url=url, http_status=page_info.get('httpStatus'), error=error, adapter=self,
                    )
                    raise error
                if not cookie_done:
                    await _accept_cookie(page)
                    cookie_done = True
                await page.wait_for_timeout(random.randint(2200, 3200))
                try:
                    page_info.update(await page.evaluate(_JS_SEARCH_STATE))
                    failure = _page_rejection_reason(page_info.get('httpStatus'), page_info)
                    if failure:
                        page_info['reason'] = failure
                        self.diagnostics.record_page(page_info)
                        raise AmazonCatalogIncomplete(f'Amazon {market.code} 搜索页访问受阻 ({failure})')
                    if not _delivery_postcode_matches(page_info.get('deliveryText', ''), market):
                        page_info['deliveryRecoveryAttempted'] = True
                        try:
                            restored = await ensure_amazon_page_delivery(
                                page, market, url, http_status=page_info.get('httpStatus'), state=page_info,
                            )
                        except AmazonCatalogIncomplete:
                            page_info['reason'] = 'delivery_recovery_rejected'
                            self.diagnostics.record_page(page_info)
                            raise
                        page_info.update(restored)
                    # 地址恢复可能重载页面；只读取复核成功后的当前搜索卡片。
                    rows = await page.evaluate(_JS_EXTRACT)
                except AmazonCatalogIncomplete as e:
                    await _capture_amazon_failure(
                        page, market, stage='catalog_search', reason=failure or 'search_page_rejected',
                        url=url, http_status=page_info.get('httpStatus'), error=e, adapter=self,
                    )
                    raise
                except Exception as e:
                    page_info.update(reason='extraction_failed', errorType=type(e).__name__)
                    self.diagnostics.record_page(page_info)
                    await _capture_amazon_failure(
                        page, market, stage='catalog_search', reason='extraction_error',
                        url=url, error=e, adapter=self,
                    )
                    print(f"[catalog/Amazon/{market.code}] {q} p{n} extract 失败: {e}")
                    if query_kind == 'brand':
                        raise AmazonCatalogIncomplete(f'Amazon {market.code} 主品牌 {q} 第 {n} 页抽取失败，本轮目录不完整') from e
                    return False
                if not _delivery_postcode_matches(page_info.get('deliveryText', ''), market):
                    page_info['reason'] = 'delivery_location_unverified'
                    self.diagnostics.record_page(page_info, rows)
                    raise AmazonCatalogIncomplete(f'Amazon {market.code} 搜索页配送地未确认，拒绝混入境外配送价格')
                saw_rows = saw_rows or bool(rows)

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
                    await self._record_price_observation(
                        page, item, source='catalog_search', query=q, page_number=n,
                    )
                    by_asin[asin] = item
                    if self._should_expand_variants(r, item):
                        variant_seeds[asin] = item
                    new_real += 1
                print(
                    f"[catalog/Amazon/{market.code}] {query_kind}:{q} p{n}: {len(rows)} 结果 / "
                    f"本页新增真电视 {new_real} / 累计 {len(by_asin)} / 过滤 {filtered}"
                )
                page_info.update(extractedRows=len(rows), acceptedNew=new_real,
                                 candidateCount=len(by_asin), filtered=filtered,
                                 reason='rows' if rows else 'empty_search_response')
                self.diagnostics.record_page(page_info, rows)
                if not rows and n == 1 and query_kind == 'brand':
                    await _capture_amazon_failure(
                        page, market, stage='catalog_search', reason='empty_search_response',
                        url=url, adapter=self,
                    )
                self.diagnostics.checkpoint(by_asin.values())
                if new_real == 0:
                    consecutive_empty += 1
                    if consecutive_empty >= 2:
                        break
                else:
                    consecutive_empty = 0
                await asyncio.sleep(random.uniform(1.0, 2.2))
            return saw_rows

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
        consecutive_rescue_failures = 0
        for q in rescue_queries:
            normalized_query = re.sub(r"\s+", " ", q.strip().lower())
            if normalized_query in completed_queries:
                continue
            completed_queries.add(normalized_query)
            if await scrape_query(q, SERIES_RESCUE_MAX_PAGES, "series"):
                consecutive_rescue_failures = 0
            else:
                consecutive_rescue_failures += 1
                if consecutive_rescue_failures >= 2:
                    print(
                        f"[catalog/Amazon/{market.code}] 系列精确补抓连续失败 2 次，"
                        "触发熔断并保留已抓结果"
                    )
                    break

        historical = self._load_recent_catalog_items()
        recovery_items = self._select_previous_recovery_items(historical, list(by_asin.values()))
        print(
            f"[catalog/Amazon/{market.code}] 历史缺尺寸回查 candidates={len(recovery_items)} "
            f"(lookback={PREVIOUS_CATALOG_LOOKBACK})…"
        )
        recovered = 0
        for previous in recovery_items:
            asin = (previous.extra.get("asin") or "").upper()
            if not asin or asin in by_asin:
                continue
            item = await self._detail_item(page, asin, previous.brand_raw)
            if item is None:
                continue
            item.extra["recovered_from_history"] = previous.extra.get("history_catalog", "")
            by_asin[asin] = item
            self.diagnostics.checkpoint(by_asin.values())
            if self._should_expand_variants({}, item):
                variant_seeds[asin] = item
            recovered += 1
            await asyncio.sleep(random.uniform(0.4, 0.8))
        print(f"[catalog/Amazon/{market.code}] 历史缺尺寸回查恢复 {recovered} 条")

        if variant_seeds:
            added_total = 0
            consecutive_detail_failures = 0
            selected_seeds = self._select_variant_seeds(list(variant_seeds.values()))
            print(
                f"[catalog/Amazon/{market.code}] 多尺寸补全 candidates={len(variant_seeds)} / "
                f"selected={len(selected_seeds)} "
                f"(max_per_seed={MAX_VARIANTS_PER_SEED})…"
            )
            for i, seed in enumerate(selected_seeds, 1):
                added = await self._expand_variants_from_seed(page, seed, by_asin)
                if added < 0:
                    consecutive_detail_failures += 1
                    if consecutive_detail_failures >= 2:
                        print(
                            f"[catalog/Amazon/{market.code}] 详情页变体连续失败 2 次，"
                            "触发熔断并保留已抓结果"
                        )
                        break
                    continue
                consecutive_detail_failures = 0
                added_total += added
                self.diagnostics.checkpoint(by_asin.values())
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
