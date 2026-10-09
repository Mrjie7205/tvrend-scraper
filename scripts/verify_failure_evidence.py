"""用真实浏览器验证合成失败现场；不访问商店、不生成价格或正式目录。"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parent / "failure_artifacts" / "verification"
SECRETS = ("fixture-password-7fd03", "fixture-cookie-82a6", "fixture@example.invalid")


async def verify() -> int:
    # 先指定隔离目录，再导入采集器，避免验证材料进入实际失败现场目录。
    os.environ["FAILURE_EVIDENCE_DIR"] = str(ROOT)
    ROOT.mkdir(parents=True, exist_ok=True)
    from failure_evidence import capture_failure, record_failure
    from playwright.async_api import async_playwright

    results = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            for platform, country, origin in (
                ("Amazon", "GB", "https://www.amazon.co.uk"),
                ("Boulanger", "FR", "https://www.boulanger.com"),
                ("Currys", "GB", "https://www.currys.co.uk"),
                ("Elkjop", "NO", "https://www.elkjop.no"),
            ):
                page = await browser.new_page(viewport={"width": 960, "height": 540})
                try:
                    await page.set_content(
                        "<html><head><title>Synthetic failure evidence verification</title></head>"
                        "<body style='font:20px sans-serif;padding:32px'>"
                        "<h1>SYNTHETIC TEST — not a real shop incident</h1>"
                        f"<h2>{platform} / {country}</h2>"
                        "<p>Navigation failed: this controlled fixture validates diagnostics.</p>"
                        "<div id='account-info'>fixture@example.invalid</div>"
                        "<label>Password <input type='password' value='fixture-password-7fd03'></label>"
                        "<input type='hidden' name='session_token' value='fixture-cookie-82a6'>"
                        "<button>Continue shopping</button>"
                        "</body></html>"
                    )
                    report = await capture_failure(
                        page, platform=platform, country=country,
                        stage="synthetic_verification", reason="Synthetic navigation failure",
                        url=f"{origin}/?token=fixture-cookie-82a6", http_status=503,
                        product="SYNTHETIC-NOT-A-PRODUCT",
                    )
                    if report is None or not Path(report).is_file():
                        raise RuntimeError(f"{platform} 未保存最小失败 JSON")
                    report = Path(report)
                    text = report.read_text(encoding="utf-8")
                    if any(secret in text for secret in SECRETS):
                        raise RuntimeError(f"{platform} 诊断 JSON 包含测试敏感值")
                    document = json.loads(text)
                    screenshot = document.get("screenshot", {})
                    filename = screenshot.get("file")
                    if screenshot.get("status") != "saved" or not filename:
                        raise RuntimeError(f"{platform} 浏览器可用却没有留下截图：{report.name}")
                    screenshot_path = report.parent / filename
                    if not screenshot_path.is_file() or screenshot_path.suffix != ".png":
                        raise RuntimeError(f"{platform} JSON 指向的截图文件不存在")
                    results.append({
                        "platform": platform, "country": country, "synthetic": True,
                        "report": str(report.relative_to(ROOT)).replace("\\", "/"),
                        "screenshots": [str(screenshot_path.relative_to(ROOT)).replace("\\", "/")],
                        "json_sensitive_values_absent": True,
                    })
                finally:
                    await page.close()
            report = record_failure(
                platform="Amazon", country="IT", stage="synthetic_browser_unavailable",
                reason="Synthetic browser connection closed", browser_available=False,
            )
            if report is None or not Path(report).is_file():
                raise RuntimeError("浏览器不可用时没有留下失败摘要")
        finally:
            await browser.close()
    result = {
        "synthetic": True, "published_prices": False, "real_shop_requests": 0,
        "verified_browser_snapshots": len(results), "browser_unavailable_summary": True,
        "results": results,
    }
    (ROOT / "verification-result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(verify()))
