"""每日价格抓取主入口。

流程:
  1. 读 channel_links.csv(active=true)→ SKU 清单
  2. 并发(默认 3 路)开 Playwright context,每个 SKU 一个独立指纹
  3. 找到 platform 对应的 adapter,跑 extract_price
  4. 算 price_trend(降价/涨价/持平/新上线)
  5. 批量追加进 raw/prices.csv

GitHub Actions 调用方式:
  python -m monitor_prices.run_daily        (默认 headless)
  HEADLESS_MODE=false python -m monitor_prices.run_daily   (本地调试)

local 调用方式:
  cd 1-Data/Channel-Prices/scripts
  python -m monitor_prices.run_daily
"""
from __future__ import annotations

import asyncio
import os
import random
import re
import sys
from datetime import datetime
from pathlib import Path

# 让 `python -m monitor_prices.run_daily` 在 scripts/ 工作目录下可用
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from monitor_prices.core import (  # noqa: E402
    DEFAULT_LOCALE,
    STEALTH_JS,
    USER_AGENTS,
    VIEWPORT_HEIGHTS,
    VIEWPORT_WIDTHS,
    handle_antibot_page,
    locale_for,
)
from monitor_prices.prices_io import (  # noqa: E402
    append_prices,
    compute_price_trend,
    load_active_skus,
    load_latest_historical_prices,
)
from monitor_prices.adapters import get_adapter, supported_platforms  # noqa: E402

CONCURRENCY = int(os.environ.get("MONITOR_CONCURRENCY", "3"))
HEADLESS = os.environ.get("HEADLESS_MODE", "true").lower() != "false"

# Playwright 启动参数(降低指纹)
BROWSER_ARGS = (
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-infobars",
    "--ignore-certificate-errors",
    "--disable-dev-shm-usage",
)


def _safe_filename(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", s or "unknown")[:64]


async def process_sku(sem, browser, sku: dict, hist: dict) -> dict:
    """抓一个 SKU 的价格,返回结果 dict(供 append_prices 写入)。"""
    async with sem:
        url = sku["url"]
        name = sku["product_name"]
        brand = sku["brand"]
        platform = sku["platform"]
        country = sku["country"]

        result = {
            "Brand": brand,
            "Product Name": name,
            "Country": country,
            "Platform": platform,
            "Price": None,
            "Currency": None,
            "Page Title": "",
            "Status": "Pending",
            "Price_Trend": "-",
        }

        adapter = get_adapter(platform)
        if adapter is None:
            # 不支持的渠道暂时跳过(不写日志,避免 prices.csv 灌入大量 Failed)
            print(f"  [skip] {platform} 暂未实现 adapter ({name})")
            result["Status"] = "Skipped: Unsupported Platform"
            return result

        print(f"\n→ [{country}] {name} ({platform})")

        ctx = None
        try:
            # 独立 context + 随机指纹
            ua = random.choice(USER_AGENTS)
            locale, tz = adapter.locale_override or locale_for(country)
            ctx = await browser.new_context(
                user_agent=ua,
                viewport={
                    "width": random.choice(VIEWPORT_WIDTHS),
                    "height": random.choice(VIEWPORT_HEIGHTS),
                },
                locale=locale,
                timezone_id=tz,
            )
            await ctx.add_init_script(STEALTH_JS)
            page = await ctx.new_page()

            # 导航(2 次重试 + 反爬等待)
            MAX_RETRIES = 2
            price_data = None
            for attempt in range(MAX_RETRIES):
                try:
                    await asyncio.sleep(random.uniform(1.0, 3.0))
                    timeout_ms = 40000 if attempt == 0 else 60000
                    await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                    await handle_antibot_page(page, name)
                except Exception as e:
                    print(f"  [{name}] 导航异常 ({attempt + 1}/{MAX_RETRIES}): {str(e)[:80]}")
                    if attempt < MAX_RETRIES - 1:
                        continue
                    result["Status"] = "Failed: Navigation Error"
                    break

                # 死链 / 缺货
                page_title = (await page.title()) or ""
                if adapter.is_dead_link(page_title):
                    print(f"  [{name}] 死链/下架: {page_title[:80]}")
                    result["Status"] = "Failed: Dead Link"
                    result["Page Title"] = page_title
                    break

                # cookie 弹窗(轻量)
                for sel in adapter.cookie_accept_selectors:
                    try:
                        if await page.is_visible(sel, timeout=1500):
                            await page.click(sel)
                    except Exception:
                        pass

                # 等待价格元素
                for sel in adapter.wait_selectors:
                    try:
                        await page.wait_for_selector(sel, timeout=5000)
                        break
                    except Exception:
                        continue

                # 价格提取
                price_data = await adapter.extract_price(page)
                if price_data:
                    new_price, currency = price_data
                    result["Price"] = new_price
                    result["Currency"] = currency
                    result["Status"] = "Success"
                    result["Page Title"] = (await page.title()) or ""
                    result["Price_Trend"] = compute_price_trend(name, country, platform, new_price, hist)
                    print(f"  [ok] {currency} {new_price} ({result['Price_Trend']})")
                    break
                else:
                    if attempt < MAX_RETRIES - 1:
                        await asyncio.sleep(2)
                        continue
                    result["Status"] = "Failed: Price Not Found"
                    print(f"  [{name}] 价格未找到")

        except Exception as e:
            print(f"  [{name}] 严重异常: {str(e)[:120]}")
            result["Status"] = f"Failed: Critical {str(e)[:50]}"
        finally:
            if ctx:
                try:
                    await ctx.close()
                except Exception:
                    pass

        return result


async def run() -> int:
    print(f"[monitor] supported adapters: {supported_platforms()}")
    skus = load_active_skus()
    if not skus:
        print("[monitor] 无 active SKU,退出")
        return 0

    # 过滤掉无 adapter 的渠道(避免开浏览器后再 skip)
    runnable = [s for s in skus if get_adapter(s["platform"]) is not None]
    skipped = len(skus) - len(runnable)
    if skipped:
        skipped_platforms = sorted({s["platform"] for s in skus if get_adapter(s["platform"]) is None})
        print(f"[monitor] 跳过 {skipped} 个 SKU(未实现 adapter 的渠道:{skipped_platforms})")
    if not runnable:
        print("[monitor] 全部 SKU 渠道都没 adapter,退出")
        return 0

    print(f"[monitor] 抓取 {len(runnable)} SKU · headless={HEADLESS} · concurrency={CONCURRENCY}")
    hist = load_latest_historical_prices()

    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(headless=HEADLESS, channel="chrome", args=list(BROWSER_ARGS))
        except Exception:
            browser = await p.chromium.launch(headless=HEADLESS, args=list(BROWSER_ARGS))

        sem = asyncio.Semaphore(CONCURRENCY)
        results = await asyncio.gather(*[process_sku(sem, browser, s, hist) for s in runnable])
        await browser.close()

    # 批量追加进 prices.csv(给每行打时间戳)
    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M:%S")
    for r in results:
        r["Date"] = date_str
        r["Time"] = time_str
    append_prices(results)

    n_ok = sum(1 for r in results if r["Status"] == "Success")
    n_fail = len(results) - n_ok
    print(f"\n[monitor] 完成 · 成功 {n_ok} / 失败 {n_fail} (共 {len(results)})")
    return 0


def main() -> int:
    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
