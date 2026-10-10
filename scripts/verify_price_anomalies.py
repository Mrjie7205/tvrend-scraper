"""用离线合成页面验收涨跌阈值及真实截图，不访问商店、不写价格主表。"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path

from monitor_prices.core import launch_scraper_browser, new_scraper_context
from price_anomalies import classify_change, record_price_change, summarize


ROOT = Path(__file__).resolve().parent / "price_artifacts" / "verification"


async def main() -> int:
    from playwright.async_api import async_playwright

    now = datetime.now(timezone.utc)
    baseline = {"price": 100, "currency": "GBP", "observed_at": (now - timedelta(days=1)).isoformat(),
                "identity_precision": "synthetic_fixture"}
    cases = (("UP-100", 200, 100), ("DOWN-50", 50, -50))
    reports = []
    run_root = ROOT / now.strftime('%Y%m%dT%H%M%S%fZ')
    async with async_playwright() as playwright:
        browser = await launch_scraper_browser(playwright, headless=True)
        context = await new_scraper_context(browser, country="GB")
        await context.set_offline(True)
        try:
            for redact, name, price, change in (
                (redact, *case) for redact in (False, True) for case in cases
            ):
                mode = 'masked' if redact else 'original'
                os.environ['SCRAPER_SCREENSHOT_REDACT'] = '1' if redact else '0'
                page = await context.new_page()
                try:
                    await page.set_content(
                        "<html><head><title>Synthetic price change test</title></head>"
                        "<body style='font:22px sans-serif;padding:40px'>"
                        "<h1>SYNTHETIC PRICE CHANGE TEST</h1><p>Verification only — not market data.</p>"
                        f"<h2 id='productTitle'>{name}</h2><p>Previous price: GBP 100</p>"
                        f"<p>Observed price: GBP {price}</p><p>Change: {change:+d}%</p>"
                        "<input type='password' value='fixture-do-not-persist'>"
                        "</body></html>"
                    )
                    observation = {"price": price, "currency": "GBP", "observed_at": now.isoformat(),
                                   "platform": "Currys", "country": "GB", "product": name,
                                   "url": f"https://www.currys.co.uk/products/synthetic-{name}.html",
                                   "observation_source": "synthetic_fixture", "ingestion_status": "not_published"}
                    report = await record_price_change(baseline=baseline, observation=observation,
                                                       page=page, output_dir=run_root / mode,
                                                       evidence_source="synthetic_same_page")
                    if report is None:
                        raise RuntimeError(f"{name} 达到边界却未留证")
                    document = json.loads(report.read_text(encoding="utf-8"))
                    if document["change_percent"] != change:
                        raise RuntimeError("涨跌比例错误")
                    if document["screenshot"]["status"] != "saved":
                        raise RuntimeError("有效页面未取得截图")
                    if document['screenshot'].get('redacted') is not redact:
                        raise RuntimeError('波动截图模式记录与配置不一致')
                    screenshot = report.parent / document["screenshot"]["file"]
                    if not screenshot.is_file() or not screenshot.read_bytes().startswith(b"\x89PNG"):
                        raise RuntimeError("截图文件缺失或格式不符")
                    if "fixture-do-not-persist" in report.read_text(encoding="utf-8"):
                        raise RuntimeError("快照 JSON 混入输入框值")
                    reports.append({"case": name, "change_percent": change,
                                    "mode": mode, "redacted": redact,
                                    "report": report.relative_to(ROOT).as_posix(),
                                    "image": screenshot.relative_to(ROOT).as_posix(), "synthetic": True})
                finally:
                    await page.close()
        finally:
            await context.close()
            await browser.close()
    for price in (199.99, 50.01):
        if classify_change(100, price, old_currency="GBP", currency="GBP") is not None:
            raise RuntimeError("阈值之外的普通波动被误报")
    if classify_change(100, 200, old_currency="EUR", currency="GBP") is not None:
        raise RuntimeError("跨币种价格被错误比较")
    if classify_change(0, 200, old_currency="GBP", currency="GBP") is not None:
        raise RuntimeError("零基线被错误比较")
    summary = {mode: summarize(run_root / mode) for mode in ('original', 'masked')}
    result = {"synthetic": True, "market_requests": 0, "writes_price_history": False,
              "inclusive_boundaries_passed": True, "normal_moves_skipped": True,
              "original_and_masked_modes_verified": True,
              "incomparable_baselines_skipped": True, "reports": reports,
              "snapshot_summary": summary}
    (ROOT / "verification-result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
