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
import statistics
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
    channels_in_scope,
    handle_antibot_page,
    locale_for,
    platform_in_scope,
)
from monitor_prices.prices_io import (  # noqa: E402
    append_prices,
    compute_price_trend,
    load_active_skus,
    load_latest_historical_prices,
    trim_prices_window,
)
from monitor_prices.adapters import get_adapter, supported_platforms  # noqa: E402

CONCURRENCY = int(os.environ.get("MONITOR_CONCURRENCY", "3"))
HEADLESS = os.environ.get("HEADLESS_MODE", "true").lower() != "false"
MAX_SKUS = int(os.environ.get("MONITOR_MAX_SKUS", "0") or "0")

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


def _batch_prices_pass_history_guard(adapter, skus: list[dict], prices: dict, hist: dict) -> bool:
    """用最近一次成功价拦截系统性错位（优惠额、月供被当售价等）。"""
    ratios: list[float] = []
    for sku in skus:
        price_data = prices.get(adapter.batch_price_key(sku["url"]))
        old = hist.get(f"{sku['product_name']}_{sku['country']}_{sku['platform']}")
        if not price_data or not old or old <= 0:
            continue
        ratios.append(float(price_data[0]) / float(old))
    if len(ratios) < 20:
        print(f"[monitor/{adapter.platform_name}] 历史价守门样本 {len(ratios)} 条，不足 20，跳过比对")
        return True

    median_ratio = statistics.median(ratios)
    extreme_share = sum(r < 0.4 or r > 2.5 for r in ratios) / len(ratios)
    passed = 0.75 <= median_ratio <= 1.35 and extreme_share <= 0.05
    print(
        f"[monitor/{adapter.platform_name}] 历史价守门: n={len(ratios)} "
        f"median={median_ratio:.3f}, extreme={extreme_share:.1%}, "
        f"{'通过' if passed else '拒绝'}"
    )
    return passed


async def _new_context(browser, adapter, country: str):
    """按渠道地区创建浏览器会话，供单 SKU 或共享会话渠道复用。"""
    locale, tz = adapter.locale_override or locale_for(country)
    ctx = await browser.new_context(
        user_agent=random.choice(USER_AGENTS),
        viewport={
            "width": random.choice(VIEWPORT_WIDTHS),
            "height": random.choice(VIEWPORT_HEIGHTS),
        },
        locale=locale,
        timezone_id=tz,
    )
    await ctx.add_init_script(STEALTH_JS)
    if getattr(adapter, "context_cookies", ()):
        try:
            await ctx.add_cookies(list(adapter.context_cookies))
        except Exception as e:
            print(f"  [{adapter.platform_name}] 注入 context_cookies 失败: {str(e)[:80]}")
    return ctx


async def process_sku(
    sem,
    browser,
    sku: dict,
    hist: dict,
    shared_context=None,
    batch_prices: dict[str, tuple[float, str]] | None = None,
) -> dict:
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

        # 类目价格快照命中时，无需创建 context 或打开 PDP。
        batch_key = adapter.batch_price_key(url)
        if batch_prices and batch_key in batch_prices:
            new_price, currency = batch_prices[batch_key]
            result["Price"] = new_price
            result["Currency"] = currency
            result["Status"] = "Success"
            result["Page Title"] = "Batch category snapshot"
            result["Price_Trend"] = compute_price_trend(name, country, platform, new_price, hist)
            print(f"  [ok/batch] {currency} {new_price} ({result['Price_Trend']})")
            return result

        ctx = shared_context
        owns_context = shared_context is None
        page = None
        try:
            if owns_context:
                ctx = await _new_context(browser, adapter, country)

            if getattr(adapter, "direct_price_enabled", False):
                try:
                    price_data = await adapter.extract_price_direct(url, ctx.request)
                except Exception as e:
                    print(f"  [{name}] direct API 异常，回退页面抓取: {str(e)[:100]}")
                    price_data = None
                if price_data:
                    new_price, currency = price_data
                    result["Price"] = new_price
                    result["Currency"] = currency
                    result["Status"] = "Success"
                    result["Page Title"] = "Direct API"
                    result["Price_Trend"] = compute_price_trend(name, country, platform, new_price, hist)
                    print(f"  [ok/api] {currency} {new_price} ({result['Price_Trend']})")
                    return result
                print(f"  [{name}] direct API 无价，回退页面抓取")

            page = await ctx.new_page()

            # 导航(2 次重试 + 反爬等待)
            MAX_RETRIES = 2
            price_data = None
            for attempt in range(MAX_RETRIES):
                try:
                    await asyncio.sleep(random.uniform(1.0, 3.0))
                    timeout_ms = 40000 if attempt == 0 else 60000
                    wait_until = getattr(adapter, "navigation_wait_until", "domcontentloaded")
                    response = await page.goto(url, wait_until=wait_until, timeout=timeout_ms)
                    status = response.status if response else 0
                    if adapter.is_unavailable_response(status, url, page.url):
                        result["Status"] = "Failed: Dead Link"
                        result["Page Title"] = f"HTTP {status} → {page.url}"
                        print(f"  [{name}] 死链/下架: HTTP {status} → {page.url[:100]}")
                        break
                    if wait_until == "commit":
                        try:
                            await page.wait_for_load_state(
                                "domcontentloaded",
                                timeout=getattr(adapter, "post_commit_timeout_ms", 15000),
                            )
                        except Exception:
                            # HTTP 状态和最终 URL 已拿到；重页面继续由反爬检测和价格选择器判断。
                            pass
                    passed = await handle_antibot_page(
                        page,
                        name,
                        max_waits=getattr(adapter, "antibot_max_waits", 4),
                        wait_seconds=getattr(adapter, "antibot_wait_seconds", 5.0),
                    )
                    if not passed:
                        raise RuntimeError("反爬验证等待超时")
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
            if page:
                try:
                    await page.close()
                except Exception:
                    pass
            if owns_context and ctx:
                try:
                    await ctx.close()
                except Exception:
                    pass

        return result


async def process_shared_group(browser, adapter, skus: list[dict], hist: dict) -> list[dict]:
    """同一渠道串行复用 context，保留 Vercel 等验证产生的会话状态。"""
    ctx = await _new_context(browser, adapter, skus[0]["country"])
    try:
        if adapter.warmup_url:
            page = await ctx.new_page()
            try:
                print(f"\n[monitor/{adapter.platform_name}] 预热共享会话: {adapter.warmup_url}")
                try:
                    await page.goto(adapter.warmup_url, wait_until="domcontentloaded", timeout=120000)
                except Exception as exc:
                    print(f"[monitor/{adapter.platform_name}] 预热导航提示: {str(exc)[:100]}")
                passed = await handle_antibot_page(
                    page,
                    f"{adapter.platform_name} warmup",
                    max_waits=adapter.antibot_max_waits,
                    wait_seconds=adapter.antibot_wait_seconds,
                )
                if not passed:
                    print(f"[monitor/{adapter.platform_name}] 预热验证未通过，仍继续商品页测试")
            finally:
                await page.close()

        serial_sem = asyncio.Semaphore(1)
        results = []
        for sku in skus:
            results.append(await process_sku(serial_sem, browser, sku, hist, shared_context=ctx))
        return results
    finally:
        await ctx.close()


async def run() -> int:
    print(f"[monitor] supported adapters: {supported_platforms()}")
    skus = load_active_skus()
    if not skus:
        print("[monitor] 无 active SKU,退出")
        return 0

    # CHANNELS 白名单过滤(不设/空 = 全跑)。自动 action 用它排除 Amazon。
    scope = channels_in_scope()
    if scope is not None:
        before = len(skus)
        skus = [s for s in skus if platform_in_scope(s["platform"], scope)]
        print(f"[monitor] CHANNELS={sorted(scope)} → {len(skus)}/{before} SKU 入选")
        if not skus:
            print("[monitor] CHANNELS 白名单下无匹配 SKU,退出")
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

    if MAX_SKUS > 0 and len(runnable) > MAX_SKUS:
        print(f"[monitor] MONITOR_MAX_SKUS={MAX_SKUS} → {MAX_SKUS}/{len(runnable)} SKU 入选")
        runnable = runnable[:MAX_SKUS]

    print(f"[monitor] 抓取 {len(runnable)} SKU · headless={HEADLESS} · concurrency={CONCURRENCY}")
    hist = load_latest_historical_prices()

    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(headless=HEADLESS, channel="chrome", args=list(BROWSER_ARGS))
        except Exception:
            browser = await p.chromium.launch(headless=HEADLESS, args=list(BROWSER_ARGS))

        sem = asyncio.Semaphore(CONCURRENCY)
        batch_price_maps: dict[str, dict[str, tuple[float, str]]] = {}
        for platform in sorted({s["platform"] for s in runnable}):
            adapter = get_adapter(platform)
            if not getattr(adapter, "batch_price_enabled", False):
                continue
            group = [s for s in runnable if s["platform"].lower() == platform.lower()]
            try:
                prepared = await adapter.prepare_batch_prices(browser, group)
                if prepared and not _batch_prices_pass_history_guard(adapter, group, prepared, hist):
                    print(f"[monitor/{adapter.platform_name}] 批量价格疑似系统性错位，整批回退 PDP")
                    prepared = {}
                batch_price_maps[adapter.platform_name.lower()] = prepared
            except Exception as exc:
                print(f"[monitor/{adapter.platform_name}] 批量价格准备失败，回退 PDP: {str(exc)[:120]}")
                batch_price_maps[adapter.platform_name.lower()] = {}

        normal = [s for s in runnable if not get_adapter(s["platform"]).shared_context]
        shared: dict[str, list[dict]] = {}
        for sku in runnable:
            adapter = get_adapter(sku["platform"])
            if adapter.shared_context:
                shared.setdefault(adapter.platform_name.lower(), []).append(sku)

        jobs = [
            process_sku(
                sem,
                browser,
                s,
                hist,
                batch_prices=batch_price_maps.get(get_adapter(s["platform"]).platform_name.lower()),
            )
            for s in normal
        ]
        jobs.extend(
            process_shared_group(browser, get_adapter(name), group, hist)
            for name, group in shared.items()
        )
        batches = await asyncio.gather(*jobs)
        results = []
        for batch in batches:
            results.extend(batch if isinstance(batch, list) else [batch])
        await browser.close()

    # 批量追加进 prices.csv(给每行打时间戳)
    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M:%S")
    for r in results:
        r["Date"] = date_str
        r["Time"] = time_str
    # 只把成功行写进 prices.csv:失败行有 stdout 日志 + debug 截图可查,后端也只读 Success;
    # 失败空价行进库会污染近窗,并可能在 trim 把唯一 Success 滚出窗口后误判"新上线"。
    append_prices([r for r in results if r["Status"] == "Success"])
    # public 只留近窗(默认 45 天,PRICES_KEEP_DAYS 可调);完整历史由私库 enrich 留存
    trim_prices_window()

    n_ok = sum(1 for r in results if r["Status"] == "Success")
    n_fail = len(results) - n_ok
    n_batch = sum(1 for r in results if r["Page Title"] == "Batch category snapshot")
    print(
        f"\n[monitor] 完成 · 成功 {n_ok} / 失败 {n_fail} (共 {len(results)})"
        f" · 类目快照命中 {n_batch}"
    )
    return 0


def main() -> int:
    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
