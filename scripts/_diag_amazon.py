"""临时诊断:看 GitHub runner 上 Amazon DE 到底给什么(IP/地理 + 配送国 + 原始价文本 + 币种 + adapter取值)。跑完删。"""
import asyncio
import json
import sys
import urllib.request

sys.path.insert(0, ".")
from playwright.async_api import async_playwright  # noqa: E402
from monitor_prices.core import USER_AGENTS, STEALTH_JS  # noqa: E402
from monitor_prices.adapters import get_adapter  # noqa: E402

URLS = [
    ("55Q6F", "https://www.amazon.de/dp/B0GJTMPY2F"),   # ★德国IP本机实测 = 399.00 EUR(对照锚点)
    ("43Q7F", "https://www.amazon.de/dp/B0FFSRHM9L"),
    ("65Q8F", "https://www.amazon.de/dp/B0FCXFKMPZ"),
]


def runner_geo():
    try:
        d = json.load(urllib.request.urlopen("https://ipinfo.io/json", timeout=10))
        return f"IP={d.get('ip')} country={d.get('country')} region={d.get('region')} city={d.get('city')} org={d.get('org')}"
    except Exception as e:
        return f"(geo lookup failed: {e})"


async def main():
    print("=" * 70)
    print("RUNNER GEO:", runner_geo())
    print("=" * 70)
    adapter = get_adapter("Amazon")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        for model, url in URLS:
            locale, tz = adapter.locale_override
            ctx = await browser.new_context(
                user_agent=USER_AGENTS[0], locale=locale, timezone_id=tz,
                viewport={"width": 1366, "height": 900})
            await ctx.add_init_script(STEALTH_JS)
            await ctx.add_cookies(list(adapter.context_cookies))
            page = await ctx.new_page()
            try:
                await page.goto(url, wait_until="commit", timeout=60000)
                try:
                    await page.click("#sp-cc-accept", timeout=3000)
                except Exception:
                    pass
                try:
                    await page.wait_for_selector("#productTitle", timeout=30000)
                except Exception:
                    pass
                await page.wait_for_timeout(2500)
                title = (await page.title())[:60]
                raws = await page.evaluate(
                    "()=>Array.from(document.querySelectorAll('.a-price .a-offscreen')).slice(0,6).map(e=>(e.textContent||'').trim())")
                glow = await page.evaluate(
                    "()=>{for(const s of ['#glow-ingress-block','#nav-global-location-popover-link','#glow-ingress-line2','#contextualIngressPtLabel_deliveryShortLine']){const e=document.querySelector(s); if(e&&(e.textContent||'').trim()) return s+': '+e.textContent.replace(/\\s+/g,' ').trim();} return '(no glow widget)';}")
                extracted = await adapter.extract_price(page)
                print(f"\n[{model}] {url}")
                print(f"   title        = {title}")
                print(f"   配送国 glow  = {glow}")
                print(f"   原始价文本   = {raws}")
                print(f"   ADAPTER 取价 = {extracted}")
            except Exception as e:
                print(f"\n[{model}] ERROR {str(e)[:150]}")
            await ctx.close()
        await browser.close()


asyncio.run(main())
