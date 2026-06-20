"""临时诊断 v2:坐实 GitHub runner 上 Amazon DE 给的是不是 robot 墙 + runner 在哪。跑完删。"""
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
]
BOT_WORDS = ["captcha", "roboter", "robot", "geben sie die zeichen", "not a robot",
             "automated access", "zur automatisierten", "api-services", "tut uns leid"]


def runner_geo():
    for svc in ("http://ip-api.com/json", "https://ifconfig.co/json"):
        try:
            d = json.load(urllib.request.urlopen(svc, timeout=10))
            return (f"[{svc}] IP={d.get('query') or d.get('ip')} country={d.get('country')} "
                    f"region={d.get('regionName') or d.get('region')} city={d.get('city')} "
                    f"isp={d.get('isp') or d.get('org') or d.get('asn_org')}")
        except Exception as e:
            print(f"  geo {svc} 失败: {str(e)[:60]}")
    return "(all geo lookups failed)"


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
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                status = resp.status if resp else "?"
                try:
                    await page.click("#sp-cc-accept", timeout=3000)
                except Exception:
                    pass
                await page.wait_for_timeout(3000)
                final_url = page.url
                title = (await page.title())[:70]
                body = (await page.evaluate("()=>document.body? (document.body.innerText||'').replace(/\\s+/g,' ').trim():''"))
                low = body.lower()
                hit = [w for w in BOT_WORDS if w in low]
                price_n = await page.evaluate("()=>document.querySelectorAll('.a-price .a-offscreen').length")
                print(f"\n[{model}] {url}")
                print(f"   HTTP status  = {status}")
                print(f"   final URL    = {final_url}")
                print(f"   title        = {title}")
                print(f"   price元素数  = {price_n}")
                print(f"   robot关键词  = {hit if hit else '(无)'}")
                print(f"   正文前300字  = {body[:300]}")
            except Exception as e:
                print(f"\n[{model}] ERROR {str(e)[:150]}")
            await ctx.close()
        await browser.close()


asyncio.run(main())
