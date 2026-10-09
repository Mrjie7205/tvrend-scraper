"""目录失败现场在实际页面关闭前采集，且不会改变抓取结果；全部离线。"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from catalog_scrape.adapters.amazon import (
    AMAZON_GB, AmazonCatalogAdapter, AmazonCatalogIncomplete,
)
from catalog_scrape.adapters.boulanger import BoulangerCatalogAdapter
from catalog_scrape.adapters.currys import CurrysCatalogAdapter
from catalog_scrape.adapters.elkjop import ElkjopCatalogAdapter
from catalog_scrape.diagnostics import AmazonCatalogDiagnostics, capture_catalog_failure
from catalog_scrape.run_weekly import run_one_adapter
import diagnose_amazon_catalog as diagnose


class CatalogFailureEvidenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.events = []

        async def capture(page, **kwargs):
            self.events.append(('capture', page, kwargs))
            return Path('safe-evidence.json')

        self.capture = self.enterContext(patch(
            'catalog_scrape.diagnostics.capture_failure', new=AsyncMock(side_effect=capture),
        ))

    def browser(self):
        page = AsyncMock()
        page.url = 'https://www.amazon.co.uk/'
        page.is_visible.return_value = False
        context = AsyncMock()
        context.new_page.return_value = page
        context.close.side_effect = lambda: self.events.append(('close', context))
        browser = AsyncMock()
        browser.new_context.return_value = context
        return browser, context, page

    async def test_amazon_home_challenge_captured_once_without_retry(self):
        adapter = AmazonCatalogAdapter(AMAZON_GB)
        _, _, page = self.browser()
        page.goto.return_value = SimpleNamespace(status=200)
        page.evaluate.return_value = {'continueShopping': True}
        with self.assertRaisesRegex(AmazonCatalogIncomplete, 'continue_shopping_interstitial'):
            await adapter._prepare_market_session(page)
        self.capture.assert_awaited_once()
        self.assertEqual('location_home', self.capture.await_args.kwargs['stage'])
        self.assertEqual('continue_shopping_interstitial', self.capture.await_args.kwargs['reason'])
        page.goto.assert_awaited_once()
        page.context.clear_cookies.assert_not_awaited()

    async def test_amazon_location_false_is_saved_before_existing_retry(self):
        adapter = AmazonCatalogAdapter(AMAZON_GB)
        _, _, page = self.browser()
        page.context.clear_cookies.side_effect = lambda: self.events.append(('reset',))
        with patch('catalog_scrape.adapters.amazon.SESSION_PREP_ATTEMPTS', 2), patch(
            'catalog_scrape.adapters.amazon.set_amazon_market_location', new=AsyncMock(return_value=False),
        ), patch('catalog_scrape.adapters.amazon.asyncio.sleep', new=AsyncMock()):
            self.assertFalse(await adapter._prepare_market_session(page))
        self.assertEqual(['capture', 'reset', 'capture'], [event[0] for event in self.events])
        self.assertEqual('delivery_location_unverified', self.capture.await_args.kwargs['reason'])

    async def test_amazon_canary_false_is_saved_and_still_rejected(self):
        adapter = AmazonCatalogAdapter(AMAZON_GB)
        _, _, page = self.browser()
        with patch('catalog_scrape.adapters.amazon.SESSION_PREP_ATTEMPTS', 1), patch(
            'catalog_scrape.adapters.amazon.set_amazon_market_location', new=AsyncMock(return_value=True),
        ), patch('catalog_scrape.adapters.amazon.verify_amazon_detail_canary', new=AsyncMock(return_value=False)):
            self.assertFalse(await adapter._prepare_market_session(page))
        self.capture.assert_awaited_once()
        self.assertEqual('canary_rejected', self.capture.await_args.kwargs['reason'])

    async def test_capture_error_never_replaces_original_amazon_error(self):
        adapter = AmazonCatalogAdapter(AMAZON_GB)
        _, _, page = self.browser()
        original = AmazonCatalogIncomplete('access_challenge')
        self.capture.side_effect = OSError('diagnostic disk unavailable')
        with patch('catalog_scrape.adapters.amazon.set_amazon_market_location',
                   new=AsyncMock(side_effect=original)):
            with self.assertRaises(AmazonCatalogIncomplete) as caught:
                await adapter._prepare_market_session(page)
        self.assertIs(original, caught.exception)

    async def test_boulanger_http_error_records_before_empty_page_is_returned(self):
        adapter = BoulangerCatalogAdapter()
        _, _, page = self.browser()
        page.goto.return_value = SimpleNamespace(status=503)
        page.evaluate.return_value = []
        with patch.object(adapter, '_scroll_to_load', new=AsyncMock()), patch(
            'catalog_scrape.adapters.boulanger.asyncio.sleep', new=AsyncMock(),
        ):
            self.assertEqual(0, await adapter._scrape_page(page, 'https://www.boulanger.com/c/televiseur', {}))
        self.assertEqual(503, self.capture.await_args.kwargs['http_status'])
        self.assertIs(page, self.capture.await_args.args[0])

    async def test_boulanger_swallowed_navigation_and_extraction_are_captured(self):
        for failure in ('navigation', 'extraction'):
            with self.subTest(failure=failure):
                adapter = BoulangerCatalogAdapter()
                _, _, page = self.browser()
                if failure == 'navigation':
                    page.goto.side_effect = TimeoutError('navigation timeout')
                else:
                    page.evaluate.side_effect = ValueError('broken product DOM')
                with patch.object(adapter, '_scroll_to_load', new=AsyncMock()), patch(
                    'catalog_scrape.adapters.boulanger.asyncio.sleep', new=AsyncMock(),
                ):
                    self.assertEqual(0, await adapter._scrape_page(page, 'https://www.boulanger.com/c/televiseur', {}))
                self.assertIs(page, self.capture.await_args.args[0])
                self.assertEqual(f'catalog_{failure}', self.capture.await_args.kwargs['stage'])

    async def test_currys_captures_actual_inner_page_before_context_close(self):
        for failure in ('http', 'navigation', 'extraction'):
            with self.subTest(failure=failure):
                self.events.clear()
                adapter = CurrysCatalogAdapter()
                browser, context, page = self.browser()
                page.url = 'https://www.currys.co.uk/tv-and-audio/televisions/tvs'
                page.goto.return_value = SimpleNamespace(status=503 if failure == 'http' else 200)
                if failure == 'navigation':
                    page.goto.side_effect = TimeoutError('navigation timeout')
                if failure == 'extraction':
                    page.evaluate.side_effect = ValueError('broken product DOM')
                with patch.object(adapter, '_new_context', new=AsyncMock(return_value=context)):
                    status, rows = await adapter._scrape_page(browser, 0)
                self.assertEqual([], rows)
                self.assertEqual(503 if failure == 'http' else 0, status)
                self.assertEqual(['capture', 'close'], [event[0] for event in self.events])
                self.assertIs(page, self.events[0][1])

    async def test_elkjop_home_challenge_survives_all_layers_without_duplicate(self):
        adapter = ElkjopCatalogAdapter()
        adapter._open_and_pass_checkpoint = AsyncMock(return_value=False)
        browser, _, page = self.browser()
        with patch('catalog_scrape.adapters.elkjop.KEY_RELAY_URL', ''), patch(
            'catalog_scrape.adapters.elkjop.API_ENABLED', True,
        ), patch('catalog_scrape.adapters.elkjop.PAGE_FALLBACK_ENABLED', False):
            result = await run_one_adapter(browser, adapter)
        self.assertIsNone(result.path)
        self.assertIn('首页安全检查未通过', result.failure_reason)
        self.capture.assert_awaited_once()
        self.assertIs(page, self.capture.await_args.args[0])
        self.assertEqual(['capture', 'close'], [event[0] for event in self.events])

    async def test_elkjop_relay_failure_has_no_fake_browser_or_secret(self):
        adapter = ElkjopCatalogAdapter()
        adapter._signed_api_key_from_relay = AsyncMock(side_effect=RuntimeError('opaque-relay-secret'))
        _, _, page = self.browser()
        with patch('catalog_scrape.adapters.elkjop.KEY_RELAY_URL', 'https://private-relay.invalid/key'):
            with self.assertRaisesRegex(RuntimeError, 'opaque-relay-secret'):
                await adapter._signed_api_key(page)
        self.assertIsNone(self.capture.await_args.args[0])
        self.assertNotIn('opaque-relay-secret', str(self.capture.await_args.kwargs))
        self.assertNotIn('private-relay', str(self.capture.await_args.kwargs))

    async def test_runner_fallback_precedes_close_and_preserves_business_error(self):
        adapter = BoulangerCatalogAdapter()
        adapter.fetch_catalog = AsyncMock(side_effect=RuntimeError('real failure'))
        browser, _, page = self.browser()
        result = await run_one_adapter(browser, adapter)
        self.assertIn('real failure', result.failure_reason)
        self.assertEqual(['capture', 'close'], [event[0] for event in self.events])
        self.assertIs(page, self.capture.await_args.args[0])

    async def test_runner_bootstrap_failure_records_without_page(self):
        adapter = BoulangerCatalogAdapter()
        browser = AsyncMock()
        browser.new_context.side_effect = RuntimeError('browser unavailable')
        result = await run_one_adapter(browser, adapter)
        self.assertIn('browser unavailable', result.failure_reason)
        self.assertIsNone(self.capture.await_args.args[0])

    async def test_wrapped_error_reuses_inner_evidence(self):
        page = AsyncMock()
        original = ValueError('original')
        await capture_catalog_failure(page, platform='Amazon', country='GB',
                                      stage='inner', reason='extraction_error', error=original)
        try:
            raise RuntimeError('wrapped') from original
        except RuntimeError as wrapped:
            path = await capture_catalog_failure(page, platform='Amazon', country='GB',
                                                stage='outer', reason='catalog_exception', error=wrapped)
        self.assertEqual(Path('safe-evidence.json'), path)
        self.capture.assert_awaited_once()

    def test_existing_amazon_report_drops_private_fields_and_url_queries(self):
        with tempfile.TemporaryDirectory() as temp:
            evidence = AmazonCatalogDiagnostics('GB', Path(temp))
            evidence.record_page({
                'currentUrl': 'https://www.amazon.co.uk/s?token=do-not-save',
                'reason': 'navigation_error', 'deliveryText': 'Private Customer Address',
                'cookies': 'session-secret', 'headers': {'Authorization': 'secret'},
            })
            raw = (evidence.path / 'report.json').read_text(encoding='utf-8')
            for secret in ('do-not-save', 'Private Customer Address', 'session-secret', 'Authorization'):
                self.assertNotIn(secret, raw)


class StandaloneAmazonDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.enterContext(patch.dict(os.environ, {'FAILURE_EVIDENCE_DIR': str(self.root / 'evidence')}))
        self.events = []
        self.page = AsyncMock()
        self.page.url = 'https://www.amazon.co.uk/s?token=do-not-save'
        self.page.goto.return_value = SimpleNamespace(status=200)
        self.page.evaluate.return_value = {}
        self.context = AsyncMock()
        self.context.new_page.return_value = self.page
        self.context.close.side_effect = lambda: self.events.append('close')
        self.browser = AsyncMock()
        self.browser.new_context.return_value = self.context
        playwright = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=self.browser)))
        self.playwright = playwright
        manager = MagicMock()
        manager.__aenter__ = AsyncMock(return_value=playwright)
        manager.__aexit__ = AsyncMock(return_value=None)
        self.enterContext(patch('playwright.async_api.async_playwright', return_value=manager))
        self.adapter = AmazonCatalogAdapter(AMAZON_GB)
        self.adapter._prepare_market_session = AsyncMock(return_value=True)
        self.enterContext(patch.object(diagnose, 'AmazonCatalogAdapter', return_value=self.adapter))

        async def capture(page, **kwargs):
            self.events.append('capture')
            return self.root / 'evidence' / 'safe.json'
        self.capture = self.enterContext(patch(
            'catalog_scrape.diagnostics.capture_failure', new=AsyncMock(side_effect=capture),
        ))
        self.args = argparse.Namespace(country='GB', query='tcl', pages=[1, 2],
                                       output=self.root / 'diagnostic', entry_only=False)

    def summary(self):
        return json.loads((self.args.output / 'summary.json').read_text(encoding='utf-8'))

    async def test_prepare_false_and_exception_both_capture_before_close(self):
        for error in (None, AmazonCatalogIncomplete('continue_shopping_interstitial')):
            with self.subTest(error=type(error).__name__):
                self.events.clear()
                self.capture.reset_mock()
                self.adapter._failure_evidence_path = None
                self.adapter._prepare_market_session = AsyncMock(return_value=False, side_effect=error)
                self.assertEqual(1, await diagnose.run(self.args))
                self.capture.assert_awaited_once()
                self.assertEqual(['capture', 'close'], self.events)
                self.assertFalse(self.summary()['sessionPrepared'])
                self.assertFalse(list(self.args.output.rglob('*.html')))

    async def test_entry_only_stops_before_brand_pages(self):
        self.args.entry_only = True
        self.assertEqual(0, await diagnose.run(self.args))
        self.page.goto.assert_not_awaited()
        self.capture.assert_not_awaited()
        self.assertTrue(self.summary()['entryOnly'])
        self.assertEqual([], self.summary()['pages'])

    async def test_challenge_stops_diagnostic_before_next_page(self):
        self.page.evaluate.return_value = {'continueShopping': True}
        self.assertEqual(1, await diagnose.run(self.args))
        self.page.goto.assert_awaited_once()
        self.capture.assert_awaited_once()
        raw = json.dumps(self.summary())
        self.assertNotIn('do-not-save', raw)
        self.assertEqual(1, len(self.summary()['pages']))

    async def test_capture_error_does_not_replace_entry_rejection(self):
        self.adapter._prepare_market_session.side_effect = AmazonCatalogIncomplete('access_challenge')
        self.capture.side_effect = OSError('screenshot failed')
        self.assertEqual(1, await diagnose.run(self.args))
        self.assertEqual('AmazonCatalogIncomplete', self.summary()['errorType'])
        self.assertEqual('access_challenge', self.summary()['error'])

    async def test_browser_start_failure_still_leaves_no_page_summary(self):
        self.playwright.chromium.launch.side_effect = RuntimeError('browser unavailable')
        self.assertEqual(1, await diagnose.run(self.args))
        self.capture.assert_awaited_once()
        self.assertIsNone(self.capture.await_args.args[0])
        self.assertEqual('RuntimeError', self.summary()['errorType'])
        self.assertEqual('browser unavailable', self.summary()['error'])

    def test_sample_rows_never_include_hidden_or_session_fields(self):
        result = diagnose._safe_rows([{
            'asin': 'B000000001', 'title': 'TCL 55 TV',
            'cookies': 'session-secret', 'headers': 'private',
            'storageState': 'private', 'html': '<input value="secret">',
        }])
        self.assertEqual([{'asin': 'B000000001', 'title': 'TCL 55 TV'}], result)


if __name__ == '__main__':
    unittest.main()
