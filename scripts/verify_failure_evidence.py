"""用真实浏览器验证合成失败现场；不访问商店、不生成价格或正式目录。"""
from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timezone
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
    run_root = ROOT / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            shops = (
                ("Amazon", "GB", "https://www.amazon.co.uk"),
                ("Boulanger", "FR", "https://www.boulanger.com"),
                ("Currys", "GB", "https://www.currys.co.uk"),
                ("Elkjop", "NO", "https://www.elkjop.no"),
            )
            for redact, platform, country, origin in (
                (redact, *shop) for redact in (False, True) for shop in shops
            ):
                # 两种模式都是真实截图；仅使用离线合成信息，不访问商店。
                mode = 'masked' if redact else 'original'
                os.environ['SCRAPER_SCREENSHOT_REDACT'] = '1' if redact else '0'
                os.environ['FAILURE_EVIDENCE_DIR'] = str(run_root / mode)
                page = await browser.new_page(viewport={"width": 960, "height": 540})
                await page.context.set_offline(True)
                try:
                    await page.set_content(
                        "<html><head><title>Synthetic failure evidence verification</title></head>"
                        "<body style='font:20px sans-serif;padding:32px'>"
                        "<h1>SYNTHETIC TEST — not a real shop incident</h1>"
                        f"<h2>{platform} / {country}</h2>"
                        "<p>Navigation failed: this controlled fixture validates diagnostics.</p>"
                        "<div id='account-info' style='background:#dbeafe;width:420px;padding:16px'>fixture@example.invalid</div>"
                        "<label>Password <input type='password' value='fixture-password-7fd03'></label>"
                        "<input type='hidden' name='session_token' value='fixture-cookie-82a6'>"
                        "<button>Continue shopping</button>"
                        "</body></html>"
                    )
                    account_box = await page.locator('#account-info').bounding_box()
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
                    if screenshot.get('redacted') is not redact:
                        raise RuntimeError(f'{platform} 截图模式记录与配置不一致')
                    # 在浏览器中只读PNG像素，不增加图像库依赖，也不改写图片。
                    pixel = await page.evaluate('''async ({png, x, y}) => {
                        const bytes = Uint8Array.from(atob(png), c => c.charCodeAt(0));
                        const bitmap = await createImageBitmap(new Blob([bytes], {type:'image/png'}));
                        const canvas = document.createElement('canvas');
                        canvas.width = bitmap.width; canvas.height = bitmap.height;
                        const ctx = canvas.getContext('2d'); ctx.drawImage(bitmap, 0, 0);
                        const scale = bitmap.width / innerWidth;
                        return [...ctx.getImageData(Math.floor(x*scale), Math.floor(y*scale), 1, 1).data];
                    }''', {'png': base64.b64encode(screenshot_path.read_bytes()).decode('ascii'),
                           'x': account_box['x'] + 8, 'y': account_box['y'] + 8})
                    expected_pixel = [0, 0, 0, 255] if redact else [219, 234, 254, 255]
                    if pixel != expected_pixel:
                        raise RuntimeError(f'{platform} {mode} 真实PNG像素不符合预期：{pixel}')
                    results.append({
                        "platform": platform, "country": country, "synthetic": True,
                        "mode": mode, "redacted": redact, "pixel_verified": True,
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
        "original_and_masked_modes_verified": True,
        "results": results,
    }
    (ROOT / "verification-result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(verify()))
