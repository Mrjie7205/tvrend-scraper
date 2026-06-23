"""诊断 C:测 GitHub Azure runner IP 能否用「设德国邮编 26935」法拿真德国价。

workflow_dispatch 触发(_diag-amazon.yml)。回答的问题:
  - Azure 数据中心 IP 会不会被 amazon.de 的「Weiter shoppen / 继续购物」墙挡?
  - postcode 法(glow address-change AJAX)在 Azure 上灵不灵?

本机(美国 Cogent IP)已验证此法可拿真德国价;本诊断专测 Azure IP 这个变量。
诊断脚本,不改任何生产代码 —— 测通了再去接 adapter。
"""
import asyncio
import json
import re
import sys
import urllib.request

sys.path.insert(0, ".")
from monitor_prices.core import STEALTH_JS, USER_AGENTS  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

# 几台已知德国标准价的电视(德国 IP 实测,周 06-15)
REF = [
    ("Hisense 32E4DS", 169.99, "https://www.amazon.de/dp/B0GT9QKMRM"),
    ("Hisense 32A5DS", 229.99, "https://www.amazon.de/dp/B0GYZMPVXG"),
    ("Hisense 40A4N", 164.03, "https://www.amazon.de/dp/B0CZX3GM3F"),
]
WALL_MARKERS = ("weiter shoppen", "continue shopping", "zur startseite gehen", "dogs of amazon")


def egress_ip() -> str:
    try:
        with urllib.request.urlopen("https://ipinfo.io/json", timeout=10) as r:
            d = json.load(r)
        return f"{d.get('ip')} country={d.get('country')} org={d.get('org')}"
    except Exception as e:
        return f"(查询失败: {e})"


async def set_location(page, zipc="26935") -> dict:
    """逐步诊断版的设地区,每步留痕。"""
    steps = {}
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
        steps["homepage"] = f"FAIL goto: {e}"
        return steps
    title = (await page.title()) or ""
    body = ((await page.content()) or "")[:4000].lower()
    steps["homepage_title"] = title[:70]
    steps["walled"] = any(w in title.lower() or w in body for w in WALL_MARKERS)
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
        steps["token_fetch"] = f"FAIL: {e}"
        return steps
    m = re.search(r'data-toaster-csrfToken="([^"]+)"', html)
    steps["token"] = "got" if m else f"NONE (toaster_len={len(html)})"
    if not m:
        return steps
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
                let j = {}; try { j = await r.json(); } catch (e) {}
                return {status: r.status, updated: j.isAddressUpdated, valid: j.isValidAddress};
            }""",
            {"token": m.group(1), "zip": zipc},
        )
    except Exception as e:
        steps["post"] = f"FAIL: {e}"
        return steps
    steps["post"] = f"status={res.get('status')} isAddressUpdated={res.get('updated')} isValidAddress={res.get('valid')}"
    return steps


async def main() -> int:
    print(f"=== 出口 IP: {egress_ip()} ===\n", flush=True)
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(
                headless=True, channel="chrome",
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage"])
        except Exception:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage"])
        ctx = await browser.new_context(user_agent=USER_AGENTS[0], locale="de-DE", timezone_id="Europe/Berlin")
        await ctx.add_init_script(STEALTH_JS)
        page = await ctx.new_page()

        steps = await set_location(page, "26935")
        print("=== 设德国地区 逐步诊断 ===", flush=True)
        for k, v in steps.items():
            print(f"  {k}: {v}", flush=True)

        print("\n=== 抓价对照德国标准(周06-15)===", flush=True)
        eur_hits = 0
        for model, ref, url in REF:
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(2500)
                info = await page.evaluate(
                    """() => ({
                        core: (document.querySelector('#corePriceDisplay_desktop_feature_div')||{}).innerText || '',
                        deliver: (document.querySelector('#glow-ingress-line2')||{}).textContent || '',
                        title: document.title })"""
                )
            except Exception as e:
                print(f"  {model:18} 抓取异常: {e}", flush=True)
                continue
            block = (info["core"] or "").replace("\n", " ").replace("\xa0", " ")
            deliver = (info["deliver"] or "").strip()
            mm = re.search(r"(\d{1,3}(?:[.,]\d{3})*[.,]\d{2})\s*€", block)
            cur = "EUR" if "€" in block else ("USD" if "$" in block else "?")
            price = mm.group(1) if mm else "无€价"
            if cur == "EUR" and mm:
                eur_hits += 1
            print(f"  {model:18} 标准{ref:>8} | 抓到 {price:>10} {cur} | 配送[{deliver[:18]}] | {info['title'][:28]}", flush=True)

        print("\n=== 判定 ===", flush=True)
        if steps.get("walled"):
            print("⚠ Azure IP 撞「Weiter shoppen / 继续购物」墙(首页)", flush=True)
        print(f"德国 EUR 价命中: {eur_hits}/{len(REF)}", flush=True)
        verdict = eur_hits >= 2 and not steps.get("walled")
        print("✅ Azure IP 也能用 postcode 法拿真德国价 → C 可行(免费 CI 全自动)!"
              if verdict else
              "❌ Azure IP 上 postcode 法不灵(被墙 / 配送美国 / 无价)→ C 需退路(自托管 runner / 本地定时)", flush=True)
        await browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
