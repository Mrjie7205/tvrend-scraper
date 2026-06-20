"""Amazon DE monitor adapter(日抓价格)。

关键(2026-06 实测,重要):
- ★ **真德国价必须从德国出口 IP 抓**。Amazon 按【连接 IP】权威判配送国,客户端改不了:实测英国 IP 抓
  amazon.de 配送地=Großbritannien、价=GBP;glow 弹窗(headless 不弹)、address-change AJAX(200 但被
  忽略)、直接写 sp-cdn 国家 cookie(被改回 GB)全无效。i18n-prefs=EUR 只把别国价换算成欧元显示
  (398,35 GBP→459,00 €),非真德国价。
- 故本 adapter **不强制币种,只认页面原生 EUR**:德国 IP/代理抓→原生欧元=真德国价,取;非德 IP 抓→
  原生 GBP/USD≠EUR→返回 None **不存价**(宁可无数据不存错国价)。生产(CI/服务器非德 IP)拿德国价
  = 给 Amazon 走德国出口代理/VPN(见 SKILL §5)。
- Amazon cookie 框 #sp-cc-accept(非 OneTrust);lc-acbde=de_DE 只设德语内容(无害)。
- 价在隐藏 .a-offscreen → textContent 取(别 is_visible);Schema.org 兜底、DOM 主力。
"""
from __future__ import annotations

from .base import BaseAdapter
from ..core import clean_price, get_price_from_schema

# 买价优先级:corePrice 里的 priceToPay 最准 → 通用 .a-price 兜底(避免抓到划线原价/相关商品价)
_PRICE_SELECTORS = (
    "#corePriceDisplay_desktop_feature_div span.priceToPay span.a-offscreen",
    "#corePriceDisplay_desktop_feature_div span.a-price.aok-align-center span.a-offscreen",
    "#corePriceDisplay_desktop_feature_div .a-offscreen",
    "#corePrice_feature_div .a-offscreen",
    "#apex_offerDisplay_desktop_feature_div .a-offscreen",
    ".priceToPay .a-offscreen",
    "#price .a-offscreen",
    ".a-price[data-a-color='base'] .a-offscreen",
    ".a-price .a-offscreen",
)

# 缺货/下架词(德语为主 + 英语兜底)。"derzeit nicht verfügbar" = 暂时无货。
_DEAD_WORDS = (
    "nicht verfügbar", "nicht verfugbar", "derzeit nicht", "ausverkauft",
    "currently unavailable", "out of stock", "seite nicht gefunden",
)


class AmazonAdapter(BaseAdapter):
    platform_name = "Amazon"
    locale_override = ("de-DE", "Europe/Berlin")
    cookie_accept_selectors = ("#sp-cc-accept",)
    wait_selectors = ("#corePriceDisplay_desktop_feature_div", ".a-price .a-offscreen", "#productTitle")
    # 只设德语内容(无害)。★不设 i18n-prefs=EUR:那会把别国价换算成欧元、掩盖真实市场;
    # 让币种保持原生,extract_price 的「只认 EUR」就成了「只认德国市场」的守门(非德 IP→GBP→拒)。
    context_cookies = (
        {"name": "lc-acbde", "value": "de_DE", "domain": ".amazon.de", "path": "/"},
    )

    def is_dead_link(self, page_title: str) -> bool:
        t = (page_title or "").lower()
        if super().is_dead_link(t):
            return True
        return any(w in t for w in _DEAD_WORDS)

    async def extract_price(self, page):
        # 1) Schema.org / JSON-LD 兜底(Amazon /dp 多数没有,但便宜先试)
        r = await get_price_from_schema(page)
        if r and r[1] == "EUR":
            return r
        # 2) DOM 主力:按优先级取第一个非空 .a-offscreen 文本(隐藏元素,用 textContent)
        try:
            txt = await page.evaluate(
                """(sels) => {
                    for (const s of sels) {
                        const e = document.querySelector(s);
                        if (e && e.textContent && e.textContent.trim()) return e.textContent.trim();
                    }
                    return "";
                }""",
                list(_PRICE_SELECTORS),
            )
        except Exception:
            txt = ""
        cp = clean_price(txt)
        # 只认原生 EUR = 德国市场守门:非德 IP 抓到的是别国价(GBP/USD)→ 返回 None 不存(宁可无数据不存错国价)
        if cp and cp[1] == "EUR":
            return cp
        return None
