"""旁路采集不能泄漏会话字段、拖延超时或制造无限截图。"""
from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import failure_evidence as evidence


class FakePage:
    url = "https://www.currys.co.uk/products/test-tv.html?token=PRIVATE#account"

    def __init__(self, *, hang=False, screenshot_error=False, redaction=True):
        self.hang = hang
        self.screenshot_error = screenshot_error
        self.redaction = redaction
        self.screenshots = 0

    def is_closed(self):
        return False

    def locator(self, selector):
        return selector

    async def evaluate(self, script):
        # 第一个浏览器操作开始前，最小摘要必须已持久化。
        reports = [p for p in Path(os.environ["FAILURE_EVIDENCE_DIR"]).glob("*.json") if p.name != "summary.json"]
        assert reports
        if self.hang:
            await asyncio.Event().wait()
        return {"redaction_ready": self.redaction, "title": "Shop", "masked_elements": 3,
                "headings": [{"tag": "h1", "text": "Continue shopping"}],
                "buttons": [{"label": "Continue", "url": "https://www.currys.co.uk/continue?token=PRIVATE"},
                            {"label": "test-person@example.com", "url": "https://relay.test/key?signature=PRIVATE"}],
                "form_count": 1, "input_count": 2, "hidden_values": "NEVER-SAVE"}

    async def screenshot(self, **kwargs):
        self.screenshots += 1
        assert kwargs["full_page"] is False
        assert kwargs["mask"] == [evidence._MASK_SELECTORS]
        assert kwargs["mask_color"] == "#000000"
        if self.screenshot_error:
            raise RuntimeError("token=PRIVATE")
        return b"\x89PNG\r\n\x1a\nsynthetic-test"


class FailureEvidenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, {"FAILURE_EVIDENCE_DIR": str(self.root)})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    async def capture(self, page, **kwargs):
        return await evidence.capture_failure(page, platform="Currys", country="GB", stage="test",
                                             reason="dead_link", **kwargs)

    async def test_minimal_summary_preserves_github_ids_and_redacts_secrets(self):
        with patch.dict(os.environ, {"GITHUB_RUN_ID": "37768037106", "GITHUB_RUN_ATTEMPT": "2", "GITHUB_SHA": "a" * 40}):
            path = await self.capture(None, url="https://www.currys.co.uk/product?session=PRIVATE", error=RuntimeError("token=PRIVATE"))
        report = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual("37768037106", report["run_id"])
        self.assertEqual("2", report["run_attempt"])
        self.assertEqual("a" * 40, report["head_sha"])
        self.assertFalse(report["browser_available"])
        self.assertEqual("page_unavailable", report["screenshot"]["status"])
        self.assertNotIn("PRIVATE", path.read_text(encoding="utf-8"))
        self.assertFalse(list(self.root.glob("*.tmp")))

    async def test_structure_uses_allowlist_and_same_page_deduplicates(self):
        page = FakePage()
        path = await self.capture(page)
        again = await evidence.capture_failure(page, platform="Currys", country="GB", stage="test", reason="dead_link")
        report = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(path, again)
        self.assertEqual(1, page.screenshots)
        self.assertEqual("saved", report["screenshot"]["status"])
        self.assertEqual("https://www.currys.co.uk/continue", report["visible_structure"]["buttons"][0]["url"])
        self.assertIsNone(report["visible_structure"]["buttons"][1]["url"])
        for secret in ("PRIVATE", "NEVER-SAVE", "test-person@example.com", "hidden_values"):
            self.assertNotIn(secret, path.read_text(encoding="utf-8"))

    async def test_same_page_new_navigation_or_search_target_has_new_event(self):
        page = FakePage()
        page.url = "https://www.amazon.es/s?k=brand-one"
        first = await self.capture(page)
        page.url = "https://www.amazon.es/s?k=brand-two"
        second = await self.capture(page)
        page.url = "https://www.amazon.es/dp/B000000001"
        third = await self.capture(page)
        self.assertEqual(3, len({first, second, third}))
        self.assertEqual("omitted_limit", json.loads(third.read_text())["screenshot"]["status"])
        for path in (first, second, third):
            self.assertNotIn("brand-one", path.read_text())
            self.assertNotIn("brand-two", path.read_text())

    async def test_timeout_and_screenshot_error_keep_initial_json(self):
        started = time.monotonic()
        path = await self.capture(FakePage(hang=True), timeout_seconds=0.025)
        self.assertLess(time.monotonic() - started, 0.3)
        self.assertEqual("capture_timeout", json.loads(path.read_text())["screenshot"]["status"])
        path = await self.capture(FakePage(screenshot_error=True))
        report = json.loads(path.read_text())
        self.assertEqual("capture_failed", report["screenshot"]["status"])
        self.assertEqual("RuntimeError", report["capture_error_type"])
        self.assertNotIn("PRIVATE", path.read_text())

    async def test_incomplete_redaction_never_takes_screenshot(self):
        page = FakePage(redaction=False)
        path = await self.capture(page)
        self.assertEqual(0, page.screenshots)
        self.assertEqual("redaction_unavailable", json.loads(path.read_text())["screenshot"]["status"])

    async def test_forty_dead_links_leave_two_images_and_omission_counts(self):
        for _ in range(40):
            await self.capture(FakePage())
        self.assertEqual(2, len(list(self.root.glob("*.png"))))
        summary = json.loads((self.root / "summary.json").read_text())
        self.assertEqual(40, summary["events"])
        self.assertEqual(38, summary["groups"][0]["screenshots_omitted"])

    async def test_write_error_is_best_effort(self):
        with patch.object(evidence, "_atomic_json", side_effect=OSError("disk unavailable")):
            self.assertIsNone(await self.capture(FakePage()))
            self.assertIsNone(evidence.record_failure(platform="Amazon", country="IT", stage="context", reason="failed"))

    async def test_new_critical_reason_keeps_summary_after_repeated_error_cap(self):
        with patch.object(evidence, "MAX_JSON_EVENTS", 2):
            for _ in range(5):
                evidence.record_failure(platform="Currys", country="GB", stage="sku", reason="dead_link")
            path = evidence.record_failure(platform="Currys", country="GB", stage="browser", reason="browser_crashed")
        self.assertIsNotNone(path)
        self.assertEqual("browser_crashed", json.loads(path.read_text())["reason"])
        summary = json.loads((self.root / "summary.json").read_text())
        self.assertEqual(3, summary["groups"][0]["json_omitted"])

    def test_url_allowlist_and_personal_text_redaction(self):
        self.assertIsNone(evidence.sanitize_url("https://relay.test/key?secret=PRIVATE"))
        self.assertIsNone(evidence.sanitize_url("https://amazon.es.evil.test/product"))
        self.assertEqual("https://www.amazon.es/dp/B000000001", evidence.sanitize_url("https://user:pass@www.amazon.es/dp/B000000001?token=PRIVATE#fragment"))
        self.assertEqual("https://www.amazon.es/[redacted]", evidence.sanitize_url("https://www.amazon.es/ap/signin/secret"))
        for value in ("Cookie: PRIVATE", "Authorization: Bearer PRIVATE", "token=PRIVATE", "recipient Alice PRIVATE",
                      "access_token=PRIVATE", "api_key: PRIVATE", "headers: PRIVATE", "storageState=PRIVATE"):
            self.assertNotIn("PRIVATE", evidence.redact_text(value))
        self.assertEqual("https://www.amazon.es/[redacted]", evidence.sanitize_url("https://www.amazon.es/ref/%3Fapi_key=PRIVATE"))

    def test_visible_payload_methods_and_flags_are_strictly_allowlisted(self):
        payload = evidence._visible_payload({"buttons": [
            {"label": "Continue", "url": "https://www.amazon.it/continue?key=PRIVATE",
             "form_action": "https://www.amazon.it/submit?token=PRIVATE", "form_method": "post"},
            {"label": "Unknown", "form_action": "https://relay.test/key?signature=PRIVATE", "form_method": "PUT PRIVATE"},
        ], "normal_page": "true", "captcha_language": True, "hidden_fields": "PRIVATE"})
        self.assertEqual("POST", payload["buttons"][0]["form_method"])
        self.assertEqual("https://www.amazon.it/submit", payload["buttons"][0]["form_action"])
        self.assertIsNone(payload["buttons"][1]["form_method"])
        self.assertIsNone(payload["buttons"][1]["form_action"])
        self.assertFalse(payload["normal_page"])
        self.assertTrue(payload["captcha_language"])
        self.assertNotIn("PRIVATE", json.dumps(payload))

    async def test_real_dom_form_destinations_and_visible_page_flags(self):
        from playwright.async_api import async_playwright
        instruction = "Fai clic sul pulsante qui sotto per continuare a fare acquisti"
        cases = {
            "continue": f"""<p>{instruction}</p>
              <form id="continue-form" method="post" action="/errors/continue?nonce=PRIVATE-FORM#private">
                <input type="hidden" name="captcha-token" value="PRIVATE-HIDDEN">
                <button type="submit">Continue</button>
                <button type="submit" formaction="/override?signature=PRIVATE-OVERRIDE" formmethod="get">Override</button>
                <button type="submit" formaction="https://relay.test/key?signature=PRIVATE-RELAY">External</button>
              </form>
              <button type="submit" form="continue-form">Associated</button>
              <div style="display:none">captcha robot verify you are human</div>""",
            "normal": f"""<nav id="nav-main">Shop</nav><h1 id="productTitle">TCL television</h1>
              <label><input type="checkbox" name="in_stock">In stock</label>
              <div hidden><input name="captcha"><p>captcha robot {instruction}</p></div>
              <script>const hiddenCaptchaToken = 'PRIVATE-SCRIPT';</script>""",
            "challenge": """<h1>Robot check</h1><p>Enter the characters you see below</p>
              <input id="captcha-input" name="captcha" value="PRIVATE-INPUT">
              <input type="hidden" name="csrf" value="PRIVATE-HIDDEN">""",
        }
        async with async_playwright() as playwright:
            try:
                browser = await playwright.chromium.launch(channel="chrome", headless=True)
            except Exception:
                browser = await playwright.chromium.launch(headless=True)
            try:
                reports = {}
                for case, body in cases.items():
                    page = await browser.new_page(viewport={"width": 800, "height": 480})
                    try:
                        html = f"<html><head><title>Public store fixture</title></head><body>{body}</body></html>"
                        await page.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=html))
                        await page.goto(f"https://www.amazon.it/evidence/{case}?session_key=PRIVATE-LOCATION")
                        path = await evidence.capture_failure(page, platform="Amazon", country="IT", stage="structure_test",
                                                              reason=f"synthetic_{case}")
                        content = path.read_text(encoding="utf-8")
                        self.assertNotIn("PRIVATE", content)
                        self.assertNotIn("hiddenCaptchaToken", content)
                        report = json.loads(content)
                        self.assertEqual("saved", report["screenshot"]["status"])
                        reports[case] = report["visible_structure"]
                    finally:
                        await page.close()
                continued = reports["continue"]
                buttons = {button["label"]: button for button in continued["buttons"]}
                for label in ("Continue", "Associated"):
                    self.assertEqual("https://www.amazon.it/errors/continue", buttons[label]["form_action"])
                    self.assertEqual("https://www.amazon.it/errors/continue", buttons[label]["url"])
                    self.assertEqual("POST", buttons[label]["form_method"])
                self.assertEqual("https://www.amazon.it/override", buttons["Override"]["form_action"])
                self.assertEqual("GET", buttons["Override"]["form_method"])
                self.assertIsNone(buttons["External"]["form_action"])
                self.assertIsNone(buttons["External"]["url"])
                self.assertTrue(continued["continue_shopping_instruction"])
                self.assertFalse(continued["normal_page"])
                self.assertFalse(continued["challenge_language"])
                self.assertFalse(continued["visible_challenge_controls"])
                self.assertTrue(reports["normal"]["normal_page"])
                for name in ("captcha_language", "robot_language", "challenge_language",
                             "visible_challenge_controls", "continue_shopping_instruction"):
                    self.assertFalse(reports["normal"][name], name)
                self.assertFalse(reports["challenge"]["normal_page"])
                self.assertFalse(reports["challenge"]["continue_shopping_instruction"])
                for name in ("captcha_language", "robot_language", "challenge_language", "visible_challenge_controls"):
                    self.assertTrue(reports["challenge"][name], name)
                self.assertTrue(all(report["visible_scan_complete"] for report in reports.values()))
            finally:
                await browser.close()

    async def test_real_browser_masks_personal_areas_before_image_capture(self):
        from playwright.async_api import async_playwright
        html = """<html><head><title>Public store fixture</title></head><body>
        <div id="account" style="position:absolute;left:20px;top:30px;width:300px;height:60px;background:red">Alice Example test-person@example.com</div>
        <input value="PRIVATE-INPUT" style="position:absolute;left:20px;top:110px;width:280px;height:35px">
        <input type="hidden" value="PRIVATE-HIDDEN">
        <div id="delivery-address">123 Example Road, W1A 1AA</div>
        <h1 style="position:absolute;left:20px;top:210px">Continue shopping</h1>
        <a href="https://www.currys.co.uk/continue?token=PRIVATE">Continue</a></body></html>"""
        async with async_playwright() as playwright:
            try:
                browser = await playwright.chromium.launch(channel="chrome", headless=True)
            except Exception:
                browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page(viewport={"width": 640, "height": 360})
                await page.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=html))
                await page.goto(FakePage.url)
                path = await self.capture(page)
                report = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual("saved", report["screenshot"]["status"])
                encoded = base64.b64encode((path.parent / report["screenshot"]["file"]).read_bytes()).decode()
                pixels = await page.evaluate("""async encoded => {
                  const image = new Image(); image.src = 'data:image/png;base64,' + encoded; await image.decode();
                  const canvas = document.createElement('canvas'); canvas.width=image.width;canvas.height=image.height;
                  const ctx=canvas.getContext('2d');ctx.drawImage(image,0,0);
                  return [[30,45],[30,125]].map(([x,y]) => [...ctx.getImageData(x,y,1,1).data]);
                }""", encoded)
                self.assertEqual([[0, 0, 0, 255], [0, 0, 0, 255]], pixels)
                for secret in ("PRIVATE", "test-person@example.com", "Alice Example", "123 Example Road"):
                    self.assertNotIn(secret, path.read_text(encoding="utf-8"))
            finally:
                await browser.close()


if __name__ == "__main__":
    unittest.main()
