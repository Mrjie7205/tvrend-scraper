"""临时诊断 v3:点穿「Weiter shoppen」墙,看墙后真实价 + 配送国。跑完删。"""
import asyncio
import sys

sys.path.insert(0, ".")
from playwright.async_api import async_playwright  # noqa: E402
from monitor_prices.core import USER_AGENTS, STEALTH_JS  # noqa: E402
from monitor_prices.adapters import get_adapter  # noqa: E402

URLS = [
    ("55Q6F", "https://www.amazon.de/dp/B0GJTMPY2F"),   # ★德国IP本机实测 = 399.00 EUR
    ("43Q7F", "https://www.amazon.de/dp/B0FFSRHM9L"),
]


async def read_state(page, adapter, tag):
    title = (await page.title())[:60]
    glow = await page.evaluate(
        "()=>{for(const s of ['#glow-ingress-block','#nav-global-location-popover-link','#glow-ingress-line2']){const e=document.querySelector(s); if(e&&(e.textContent||'').trim()) return e.textContent.replace(/\\s+/g,' ').trim();} return '(no glow)';}")
    raws = await page.evaluate(
        "()=>Array.from(document.querySelectorAll('.a-price .a-offscreen')).slice(0,5).map(e=>(e.textContent||'').trim())")
    extracted = await adapter.extract_price(page)
    print(f"   [{tag}] title={title} | 配送国={glow} | 原始价={raws} | ADAPTER={extracted}")


async def main():
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
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                try:
                    await page.click("#sp-cc-accept", timeout=3000)
                except Exception:
                    pass
                await page.wait_for_timeout(1500)
                print(f"\n[{model}] {url}")
                await read_state(page, adapter, "墙前")
                # 点「Weiter shoppen / Continue shopping」继续按钮
                clicked = False
                for sel in ["button:has-text('Weiter shoppen')", "text=Weiter shoppen",
                            "button:has-text('Continue shopping')", "input[type=submit]", ".a-button-input"]:
                    try:
                        await page.click(sel, timeout=3000)
                        clicked = True
                        break
                    except Exception:
                        continue
                print(f"   点穿墙: {'成功 (' + '点了继续按钮)' if clicked else '没找到继续按钮'}")
                if clicked:
                    try:
                        await page.wait_for_selector("#productTitle", timeout=20000)
                    except Exception:
                        pass
                    await page.wait_for_timeout(2500)
                    await read_state(page, adapter, "墙后")
            except Exception as e:
                print(f"\n[{model}] ERROR {str(e)[:150]}")
            await ctx.close()
        await browser.close()


asyncio.run(main())
