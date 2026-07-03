"""通用工具:User-Agent 池、Stealth JS、价格文本清洗、反爬页检测、
国家 → locale/timezone 映射。

这些都不是 adapter 特定的,所有渠道共用一份。
原始来自 TV_Price_Monitor/monitor.py。
"""
from __future__ import annotations

import asyncio
import os
import re


# ============================================================
# 渠道作用域过滤(CHANNELS 环境变量:逗号分隔的渠道白名单)
# ============================================================
def channels_in_scope() -> "set[str] | None":
    """读取 CHANNELS 白名单；未设置或空值表示全部渠道。"""
    raw = os.environ.get("CHANNELS", "")
    names = {part.strip().lower() for part in raw.split(",") if part.strip()}
    return names or None


def platform_in_scope(platform: str, scope: "set[str] | None") -> bool:
    return scope is None or (platform or "").strip().lower() in scope

# ============================================================
# UA 池(每次启动 context 随机选 1 个)
# ============================================================
USER_AGENTS: tuple[str, ...] = (
    # Chrome - Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    # Chrome - Mac
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    # Edge - Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 Edg/122.0.0.0",
    # Edge - Mac
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0",
    # Safari - Mac
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    # Chrome - Linux (CI 环境)
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
)

VIEWPORT_WIDTHS: tuple[int, ...] = (1920, 1366, 1440, 1536)
VIEWPORT_HEIGHTS: tuple[int, ...] = (1080, 768, 900)

# ============================================================
# Stealth JS 注入(屏蔽 Playwright 指纹)
# ============================================================
STEALTH_JS = """
// 1. 屏蔽 webdriver
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

// 2. 伪造 plugins (正常浏览器至少有几个)
Object.defineProperty(navigator, 'plugins', {
    get: () => [
        { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
        { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
        { name: 'Native Client', filename: 'internal-nacl-plugin', description: '' }
    ]
});

// 3. 伪造 languages
Object.defineProperty(navigator, 'languages', { get: () => ['en-GB', 'en-US', 'en'] });
Object.defineProperty(navigator, 'language', { get: () => 'en-GB' });

// 4. 屏蔽 chrome.runtime (Headless Chrome 特征)
if (window.chrome) {
    window.chrome.runtime = undefined;
}

// 5. 伪造 permissions
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications' ?
        Promise.resolve({ state: Notification.permission }) :
        originalQuery(parameters)
);

// 6. 隐藏 Headless 特征
Object.defineProperty(window, 'outerWidth', { get: () => window.innerWidth });
Object.defineProperty(window, 'outerHeight', { get: () => window.innerHeight + 85 });

// 7. iframe 检测防护
const iframeProto = HTMLIFrameElement.prototype;
const origContentWindow = Object.getOwnPropertyDescriptor(iframeProto, 'contentWindow');
if (origContentWindow) {
    Object.defineProperty(iframeProto, 'contentWindow', {
        get: function() {
            const iframe = origContentWindow.get.call(this);
            if (iframe) {
                try { Object.defineProperty(iframe.navigator, 'webdriver', { get: () => undefined }); } catch(e) {}
            }
            return iframe;
        }
    });
}
"""

# ============================================================
# 国家 → locale/timezone 映射
# ============================================================
COUNTRY_LOCALE: dict[str, tuple[str, str]] = {
    "FR": ("fr-FR", "Europe/Paris"),
    "DE": ("de-DE", "Europe/Berlin"),
    "UK": ("en-GB", "Europe/London"),
    "GB": ("en-GB", "Europe/London"),
    "NL": ("nl-NL", "Europe/Amsterdam"),
    "ES": ("es-ES", "Europe/Madrid"),
    "IT": ("it-IT", "Europe/Rome"),
    "NO": ("nb-NO", "Europe/Oslo"),
}

DEFAULT_LOCALE = ("en-GB", "Europe/London")


def locale_for(country: str) -> tuple[str, str]:
    return COUNTRY_LOCALE.get(country.upper(), DEFAULT_LOCALE)


# ============================================================
# 价格文本清洗
# ============================================================
def clean_price(text: str | None) -> tuple[float, str] | None:
    """从带货币符号的文本提取价格 + 币种。

    支持:
      - 货币符号 / 三字码 (€ / £ / $ / kr / EUR / GBP / USD / NOK)
      - 法国千分位空格(1 999,00) / 德国千分位点(1.999,00) / 通用逗号小数
    """
    if not text:
        return None
    text = text.strip()
    currency = "EUR"
    if "£" in text or "GBP" in text:
        currency = "GBP"
    elif "$" in text or "USD" in text:
        currency = "USD"
    elif "NOK" in text.upper() or re.search(r"(?:^|\s)kr(?:\s|$)", text, re.IGNORECASE):
        currency = "NOK"

    s = (
        text.replace("€", "")
        .replace("£", "")
        .replace("$", "")
        .replace("EUR", "")
        .replace("GBP", "")
        .replace("USD", "")
        .replace("NOK", "")
        .replace("nok", "")
        .replace("KR", "")
        .replace("kr", "")
        .replace("\xa0", "")
        .strip()
    )

    try:
        if currency in {"EUR", "NOK"}:
            # 先清除千分位空格,再区分德式(1.999,00)和法式(1 999,00)
            s = s.replace(" ", "")
            if "," in s and "." in s:
                s = s.replace(".", "").replace(",", ".")
            elif currency == "NOK" and "." in s and "," not in s:
                left, right = s.rsplit(".", 1)
                s = left + right if len(right) == 3 else s
            else:
                s = s.replace(",", ".")
        else:
            s = s.replace(",", "").replace(" ", "")
        m = re.search(r"(\d+(\.\d+)?)", s)
        if m:
            return float(m.group(1)), currency
    except (ValueError, AttributeError):
        pass
    return None


# ============================================================
# Schema.org / Meta 通用价格抓取(多数渠道头一招就解决)
# ============================================================
def _num_from_schema(amount, currency: str):
    """解析 schema.org/Meta 的 price 字段。规范 price 应是机读小数(点小数、无千分位)。
    先直试 float(原串);非规范则借 clean_price 的【币种感知】千分位逻辑——避免 '1,499'
    这类千分位逗号被裸 .replace(',','.') 成 1.499 的静默缩 1000 倍错价。无法解析返回 None
    (交回 extract_price 的 DOM 兜底)。
    """
    s = str(amount).strip()
    val = None
    try:
        val = float(s)
    except (TypeError, ValueError):
        sym = {"GBP": "£", "USD": "$"}.get(str(currency).upper(), "")
        r = clean_price(sym + s)
        val = r[0] if r else None
    # 电视价不可能 < 10(欧元 '1,499' 因逗号=小数歧义可能漏解析成 1.499)→ 判可疑,回退 DOM 兜底
    if val is not None and val < 10:
        return None
    return val


async def get_price_from_schema(page) -> tuple[float, str] | None:
    """从 Meta 标签或 JSON-LD 提取价格。

    顺序:
      1) <meta property='product:price:amount'> + currency
      2) <meta itemprop='price'> + priceCurrency
      3) <script type='application/ld+json'> 的 offers.price
    """
    import json

    # 1. Meta
    try:
        amount = await page.get_attribute("meta[property='product:price:amount']", "content", timeout=500)
        if not amount:
            amount = await page.get_attribute("meta[itemprop='price']", "content", timeout=500)
        if amount:
            currency = (
                await page.get_attribute("meta[property='product:price:currency']", "content")
                or await page.get_attribute("meta[itemprop='priceCurrency']", "content")
                or "EUR"
            )
            if currency == amount:
                currency = "EUR"
            val = _num_from_schema(amount, currency)
            if val is not None:
                return val, currency
    except Exception:
        pass

    # 2. JSON-LD
    try:
        scripts = await page.locator("script[type='application/ld+json']").all()
        for script in scripts:
            text = await script.text_content()
            if not text or '"price"' not in text:
                continue
            try:
                data = json.loads(text)
                items = data if isinstance(data, list) else [data]
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    offers = it.get("offers")
                    if not offers and "@graph" in it:
                        for sub in it["@graph"]:
                            if isinstance(sub, dict) and "offers" in sub:
                                offers = sub["offers"]
                                break
                    if not offers:
                        continue
                    offer_list = offers if isinstance(offers, list) else [offers]
                    for offer in offer_list:
                        if isinstance(offer, dict):
                            p = offer.get("price")
                            if p:
                                cur = offer.get("priceCurrency", "EUR")
                                val = _num_from_schema(p, cur)
                                if val is not None:
                                    return val, cur
            except json.JSONDecodeError:
                continue
    except Exception:
        pass
    return None


# ============================================================
# 反爬页通用检测(Cloudflare / Datadome / Akamai / Just-a-moment)
# ============================================================
ANTIBOT_TITLE_MARKERS: tuple[str, ...] = (
    "bear with us",
    "just a moment",
    "ein moment",
    "access denied",
    "attention required",
    "cloudflare",
    "security checkpoint",
)

ANTIBOT_CONTENT_MARKERS: tuple[str, ...] = (
    "checking your connection",
    "verify you are human",
)


async def handle_antibot_page(page, label: str = "", max_waits: int = 4, wait_seconds: float = 5.0) -> bool:
    """如果当前页是反爬挑战页,原地多等几轮 5s;通过返回 True。"""
    try:
        for _ in range(max_waits):
            content = (await page.content()).lower()
            title = (await page.title()).lower()
            is_bot_page = any(m in title for m in ANTIBOT_TITLE_MARKERS) or any(
                m in content for m in ANTIBOT_CONTENT_MARKERS
            )
            if not is_bot_page:
                return True
            print(f"  [{label}] ⚠ 反爬拦截页,等 {wait_seconds:.0f}s 再试")
            await asyncio.sleep(wait_seconds)
        return False
    except Exception:
        return True


# ============================================================
# Amazon.de:把会话配送地设成德国邮编(任意 IP 可用,纯 AJAX、无需弹窗)+ canary 守门
# ============================================================
# 一个普通德国邮编(26935 = Stadland,下萨克森)。用 LOCATION_INPUT 设邮编,
# Amazon 据此判配送国=德国,搜索页 + /dp 页随之给真·德国 EUR 价。
AMAZON_DE_ZIP = os.environ.get("AMAZON_DE_ZIP", "26935")

# canary 锚点:已知德国价的稳定型号(逐台核过)。set 完地区后抓它们验"真拿到德国价"。
# (asin, 已知德国标价 EUR)。价带 0.5–1.5×:容促销/小涨,逮错国价/0/垃圾。
AMAZON_DE_CANARY = (
    ("B0GYZMPVXG", 229.99),   # Hisense 32A5DS
    ("B0GT9QKMRM", 169.99),   # Hisense 32E4DS
)
_CANARY_LO, _CANARY_HI = 0.5, 1.5

_AMZ_PRICE_SELECTORS = (
    "#corePriceDisplay_desktop_feature_div span.priceToPay span.a-offscreen",
    "#corePriceDisplay_desktop_feature_div .a-offscreen",
    ".priceToPay .a-offscreen",
    ".a-price .a-offscreen",
)


async def set_amazon_de_location(page, zipcode: "str | None" = None) -> bool:
    """从【任意 IP】把 amazon.de 当前会话的配送地设成德国邮编 + 锁币种 EUR。

    背景(2026-06 实测推翻旧结论):amazon.de 按"连接 IP"猜配送国 → 非德 IP(GitHub Azure / 本机 VPN)
    默认配送美国、给美元价。但 Amazon glow「地址变更」AJAX **接受客户端设邮编**(响应 isAddressUpdated:1),
    设成德国邮编后搜索页 + /dp 页都给真·德国 EUR 价(逐台对照德国标准核验;Azure runner 实测 3/3)。
    **无需德国 IP、无需 glow 弹窗(headless 弹窗不弹也无妨)、纯 AJAX。**

    步骤:① 锁 i18n-prefs=EUR(防别国币种/换算显示)② 进首页拿会话 ③ get-rendered-toaster 响应里取
    data-toaster-csrfToken ④ POST address-change 设邮编。

    ★ fail-closed:返回 False(没拿到 isAddressUpdated:1)时,调用方必须 abort 整个 Amazon 抓取,
    **绝不 fall through 到默认美国地址的价**(否则吐错国价、污染数据=踩红线)。
    """
    zipc = zipcode or AMAZON_DE_ZIP
    try:
        await page.context.add_cookies([
            {"name": "i18n-prefs", "value": "EUR", "domain": ".amazon.de", "path": "/"},
            {"name": "lc-acbde", "value": "de_DE", "domain": ".amazon.de", "path": "/"},
        ])
    except Exception:
        pass
    try:
        await page.goto("https://www.amazon.de/", wait_until="domcontentloaded", timeout=45000)
    except Exception as e:
        print(f"  [set-loc] 进 amazon.de 首页失败: {e}")
        return False
    for sel in ("#sp-cc-accept", "#sp-cc-accept input"):
        try:
            await page.click(sel, timeout=2500)
            break
        except Exception:
            pass
    try:
        html = await page.evaluate(
            """async () => {
                const url = "https://www.amazon.de/portal-migration/hz/glow/get-rendered-toaster"
                    + "?pageType=Gateway&aisTransitionState=null&rancorLocationSource=IP_GEOLOCATION&isB2B=false";
                const r = await fetch(url, {credentials: "include"});
                return await r.text();
            }"""
        )
    except Exception as e:
        print(f"  [set-loc] 取 CSRF token 失败: {e}")
        return False
    m = re.search(r'data-toaster-csrfToken="([^"]+)"', html)
    if not m:
        print("  [set-loc] 没找到 CSRF token(amazon.de glow 可能改版,需排查)")
        return False
    token = m.group(1)
    try:
        res = await page.evaluate(
            """async ({token, zip}) => {
                const r = await fetch("https://www.amazon.de/portal-migration/hz/glow/address-change?actionSource=glow", {
                    method: "POST",
                    headers: {"anti-csrftoken-a2z": token, "content-type": "application/json"},
                    credentials: "include",
                    body: JSON.stringify({locationType: "LOCATION_INPUT", zipCode: zip, deviceType: "web",
                                          storeContext: "generic", pageType: "Gateway", actionSource: "glow"})
                });
                let updated = false;
                try { updated = (await r.json()).isAddressUpdated === 1; } catch (e) {}
                return {status: r.status, updated};
            }""",
            {"token": token, "zip": zipc},
        )
    except Exception as e:
        print(f"  [set-loc] POST address-change 失败: {e}")
        return False
    ok = bool(res.get("updated"))
    print(f"  [set-loc] amazon.de 配送地 → 德国 {zipc}:{'✓ isAddressUpdated:1' if ok else '✗ 未生效'}"
          f" (status={res.get('status')})")
    return ok


async def verify_amazon_de_canary(page) -> bool:
    """canary 守门:抓已知德国价锚点,断言原生 EUR + 价落在合理带。**任一通过即 True**(防单款下架误杀);
    全部不符 → False(说明地区没设成 / glow 静默坏掉吐错国价)→ 调用方 abort 整轮 Amazon、不提交。

    这是防"无人值守 CI 悄悄坏 → 污染数据"最有效的一招(主开发护栏 2)。
    """
    ok_any = False
    for asin, known in AMAZON_DE_CANARY:
        try:
            await page.goto(f"https://www.amazon.de/dp/{asin}", wait_until="domcontentloaded", timeout=45000)
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
            print(f"  [canary] {asin} 抓取异常: {str(e)[:60]}")
            continue
        cp = clean_price(txt)
        if not cp:
            print(f"  [canary] {asin} 无价(raw={txt[:16]!r})")
            continue
        val, cur = cp
        lo, hi = _CANARY_LO * known, _CANARY_HI * known
        if cur == "EUR" and lo <= val <= hi:
            print(f"  [canary] {asin} {val}€ ∈ [{lo:.0f},{hi:.0f}] vs 已知 {known} ✓")
            ok_any = True
        else:
            print(f"  [canary] {asin} {val} {cur} 不符(要 EUR 且 ∈[{lo:.0f},{hi:.0f}])⚠")
    if not ok_any:
        print("  [canary] ✗ 所有锚点不符 → 地区没设成/吐错国价 → 整轮 Amazon abort、不提交")
    return ok_any
