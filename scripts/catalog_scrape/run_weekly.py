"""每周一次 catalog 反向拉主入口。

流程:
  1. 遍历所有已注册的 catalog adapter(目前只有 Boulanger)
  2. 每个 adapter 开独立 Playwright context + 随机 UA + Stealth
  3. 调 adapter.fetch_catalog 抓全量列表
  4. 输出 catalog/<platform>_<country>_<YYYYMMDD>.csv

调用:
  python -m catalog_scrape.run_weekly                # 抓所有注册的渠道
  python -m catalog_scrape.run_weekly --only Boulanger  # 只抓一个

输出 schema:
  brand_raw, raw_text, url, size_hint_inch, price_hint_eur, platform, country, scraped_at
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import os
import random
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from monitor_prices.core import (  # noqa: E402
    STEALTH_JS,
    USER_AGENTS,
    VIEWPORT_HEIGHTS,
    VIEWPORT_WIDTHS,
    locale_for,
)
from catalog_scrape import REGISTRY, supported_catalogs  # noqa: E402

HEADLESS = os.environ.get("HEADLESS_MODE", "true").lower() != "false"
BROWSER_ARGS = (
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-infobars",
    "--ignore-certificate-errors",
    "--disable-dev-shm-usage",
)

OUTPUT_COLUMNS = (
    "brand_raw",
    "raw_text",
    "url",
    "size_hint_inch",
    "price_hint_eur",
    "platform",
    "country",
    "scraped_at",
)


def _catalog_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "catalog"


async def run_one_adapter(browser, adapter) -> Path | None:
    """跑一个 adapter,产出 catalog/<platform>_<country>_<date>.csv,返回路径。"""
    ua = random.choice(USER_AGENTS)
    locale, tz = adapter.locale_override or locale_for(adapter.country)
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

    try:
        items = await adapter.fetch_catalog(page)
    except Exception as e:
        print(f"[catalog/{adapter.platform_name}] 抓取异常: {e}")
        await ctx.close()
        return None
    finally:
        await ctx.close()

    if not items:
        print(f"[catalog/{adapter.platform_name}] 0 条记录,不写文件")
        return None

    now = datetime.utcnow()
    date_tag = now.strftime("%Y%m%d")
    scraped_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    out_dir = _catalog_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{adapter.platform_name.lower()}_{adapter.country.lower()}_{date_tag}.csv"

    with out_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        for it in items:
            writer.writerow({
                "brand_raw": it.brand_raw,
                "raw_text": it.raw_text,
                "url": it.url,
                "size_hint_inch": it.size_hint_inch if it.size_hint_inch is not None else "",
                "price_hint_eur": it.price_hint_eur if it.price_hint_eur is not None else "",
                "platform": adapter.platform_name,
                "country": adapter.country,
                "scraped_at": scraped_at,
            })
    print(f"[catalog/{adapter.platform_name}] → {out_path.relative_to(_catalog_dir().parent.parent)}")
    return out_path


async def run(only: str | None = None) -> int:
    selected = REGISTRY.items() if not only else [(k, a) for k, a in REGISTRY.items() if k == only.lower()]
    # CHANNELS 白名单(逗号分隔):自动 Action 用它排除 Amazon。--only 优先,再叠加 CHANNELS(交集)。
    _chs = os.environ.get("CHANNELS", "").strip()
    if _chs:
        allow = {c.strip().lower() for c in _chs.split(",") if c.strip()}
        selected = [(k, a) for k, a in selected if k in allow]
        print(f"[catalog] CHANNELS={_chs} → 跑 {[a.platform_name for k, a in selected]}")
    targets = [a for k, a in selected]
    if not targets:
        print(f"[catalog] no adapter for only={only!r} CHANNELS={_chs!r}. Supported: {supported_catalogs()}")
        return 1

    print(f"[catalog] supported: {supported_catalogs()} · headless={HEADLESS}")

    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(headless=HEADLESS, channel="chrome", args=list(BROWSER_ARGS))
        except Exception:
            browser = await p.chromium.launch(headless=HEADLESS, args=list(BROWSER_ARGS))
        results = []
        for adapter in targets:
            r = await run_one_adapter(browser, adapter)
            results.append((adapter.platform_name, r))
        await browser.close()

    print()
    for name, path in results:
        if path:
            print(f"  ✓ {name}: {path.name}")
        else:
            print(f"  ✗ {name}: failed")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="只跑指定 platform(如 Boulanger)")
    args = ap.parse_args()
    return asyncio.run(run(only=args.only))


if __name__ == "__main__":
    raise SystemExit(main())
