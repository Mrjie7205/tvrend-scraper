"""通用工具:User-Agent 池、Stealth JS、价格文本清洗、反爬页检测、
国家 → locale/timezone 映射。

这些都不是 adapter 特定的,所有渠道共用一份。
原始来自 TV_Price_Monitor/monitor.py。
"""
from __future__ import annotations

import asyncio
import re

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
      - 货币符号 / 三字码 (€ / £ / $ / EUR / GBP / USD)
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

    s = (
        text.replace("€", "")
        .replace("£", "")
        .replace("$", "")
        .replace("EUR", "")
        .replace("GBP", "")
        .replace("USD", "")
        .replace("\xa0", "")
        .strip()
    )

    try:
        if currency == "EUR":
            # 先清除千分位空格,再区分德式(1.999,00)和法式(1 999,00)
            s = s.replace(" ", "")
            if "," in s and "." in s:
                s = s.replace(".", "").replace(",", ".")
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
