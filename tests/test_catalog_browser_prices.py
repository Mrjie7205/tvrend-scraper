"""真实浏览器配置、Amazon 可比原币快照和 Currys 分页完整性；全部离线。"""
from __future__ import annotations

import csv
import asyncio
from collections import Counter
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from monitor_prices.core import (
    CURRENT_BROWSER_ARGS, STEALTH_JS, USER_AGENTS,
    get_browser_profile, launch_scraper_browser, new_scraper_context,
)
from catalog_scrape.adapters.amazon import AMAZON_GB, AmazonCatalogAdapter, _page_rejection_reason
from catalog_scrape.adapters.currys import CurrysCatalogAdapter, CurrysCatalogIncomplete
from monitor_prices.adapters.boulanger import BoulangerAdapter


class BrowserFactoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {'SCRAPER_BROWSER_PROFILE': 'native'}))

    async def test_fixed_chromium_without_identity_or_certificate_overrides(self):
        launch = AsyncMock()
        playwright = SimpleNamespace(chromium=SimpleNamespace(launch=launch))
        await launch_scraper_browser(playwright)
        options = launch.await_args.kwargs
        self.assertNotIn('channel', options)
        self.assertNotIn('executable_path', options)
        self.assertTrue(options['headless'])
        self.assertFalse(any('AutomationControlled' in arg or 'ignore-certificate' in arg for arg in options['args']))
        launch.side_effect = RuntimeError('browser missing')
        with self.assertRaises(RuntimeError):
            await launch_scraper_browser(playwright)
        self.assertEqual(2, launch.await_count, '每次只启动一次，失败不能换浏览器身份')

    async def test_all_market_contexts_keep_real_locale_and_native_ua(self):
        browser = AsyncMock()
        for country, locale in [('GB', 'en-GB'), ('IT', 'it-IT'), ('DE', 'de-DE'),
                                ('ES', 'es-ES'), ('FR', 'fr-FR'), ('NO', 'nb-NO')]:
            context = await new_scraper_context(browser, country=country)
            self.assertEqual(locale, browser.new_context.await_args.kwargs['locale'])
            self.assertNotIn('user_agent', browser.new_context.await_args.kwargs)
            context.add_init_script.assert_not_awaited()

    async def test_boulanger_batch_also_uses_native_french_context(self):
        browser = AsyncMock()
        with patch('catalog_scrape.adapters.boulanger.BoulangerCatalogAdapter.fetch_catalog',
                   new=AsyncMock(return_value=[])):
            self.assertEqual({}, await BoulangerAdapter().prepare_batch_prices(browser, []))
        self.assertEqual('fr-FR', browser.new_context.await_args.kwargs['locale'])
        self.assertNotIn('user_agent', browser.new_context.await_args.kwargs)
        browser.new_context.return_value.add_init_script.assert_not_awaited()


class CurrentBrowserProfileTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {'SCRAPER_BROWSER_PROFILE': 'current'}))

    async def test_default_current_preserves_baseline_launch_and_context(self):
        browser = AsyncMock()
        launch = AsyncMock(return_value=browser)
        playwright = SimpleNamespace(chromium=SimpleNamespace(launch=launch))
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual('current', get_browser_profile())
            self.assertIs(browser, await launch_scraper_browser(playwright))
            context = await new_scraper_context(browser, country='IT')
        self.assertEqual('chrome', launch.await_args.kwargs['channel'])
        self.assertEqual(list(CURRENT_BROWSER_ARGS), launch.await_args.kwargs['args'])
        self.assertIn(browser.new_context.await_args.kwargs['user_agent'], USER_AGENTS)
        self.assertEqual('it-IT', browser.new_context.await_args.kwargs['locale'])
        context.add_init_script.assert_awaited_once_with(STEALTH_JS)

    async def test_profile_is_frozen_for_whole_browser_run(self):
        browser = AsyncMock()
        launch = AsyncMock(return_value=browser)
        await launch_scraper_browser(SimpleNamespace(chromium=SimpleNamespace(launch=launch)))
        with patch.dict(os.environ, {'SCRAPER_BROWSER_PROFILE': 'native'}):
            self.assertEqual('current', get_browser_profile(browser))
            context = await new_scraper_context(browser, country='GB')
        self.assertIn('user_agent', browser.new_context.await_args.kwargs)
        context.add_init_script.assert_awaited_once()
        launch.assert_awaited_once()

    async def test_current_startup_fallback_keeps_current_profile(self):
        browser = AsyncMock()
        launch = AsyncMock(side_effect=[RuntimeError('system Chrome missing'), browser])
        await launch_scraper_browser(SimpleNamespace(chromium=SimpleNamespace(launch=launch)))
        self.assertEqual('chrome', launch.await_args_list[0].kwargs['channel'])
        self.assertNotIn('channel', launch.await_args_list[1].kwargs)
        self.assertEqual(list(CURRENT_BROWSER_ARGS), launch.await_args_list[1].kwargs['args'])
        self.assertEqual('current', get_browser_profile(browser))

    async def test_current_keeps_existing_native_identity_exception(self):
        browser = AsyncMock()
        context = await new_scraper_context(browser, country='NO', native_identity=True)
        self.assertNotIn('user_agent', browser.new_context.await_args.kwargs)
        self.assertEqual('nb-NO', browser.new_context.await_args.kwargs['locale'])
        context.add_init_script.assert_not_awaited()

    async def test_initialization_error_and_cancellation_close_context_then_reraise(self):
        for original in (RuntimeError('initialization failed'), asyncio.CancelledError()):
            with self.subTest(error=type(original).__name__):
                browser = AsyncMock()
                context = browser.new_context.return_value
                context.add_init_script.side_effect = original
                with self.assertRaises(type(original)) as caught:
                    await new_scraper_context(browser, country='GB')
                self.assertIs(original, caught.exception)
                context.close.assert_awaited_once()
                self.assertEqual('current', get_browser_profile(browser))

    async def test_cleanup_error_does_not_replace_initialization_error(self):
        browser = AsyncMock()
        original = RuntimeError('original initialization failure')
        browser.new_context.return_value.add_init_script.side_effect = original
        with patch('monitor_prices.core.close_playwright_resource',
                   new=AsyncMock(side_effect=RuntimeError('cleanup failed'))):
            with self.assertRaises(RuntimeError) as caught:
                await new_scraper_context(browser, country='GB')
        self.assertIs(original, caught.exception)

    async def test_elkjop_catalog_passes_native_identity_to_factory(self):
        from catalog_scrape.adapters.elkjop import ElkjopCatalogAdapter
        from catalog_scrape.run_weekly import run_one_adapter
        adapter = ElkjopCatalogAdapter()
        adapter.fetch_catalog = AsyncMock(return_value=[])
        context = AsyncMock()
        with patch('catalog_scrape.run_weekly.new_scraper_context', new=AsyncMock(return_value=context)) as factory, patch(
            'catalog_scrape.diagnostics.capture_failure', new=AsyncMock(return_value=None),
        ):
            await run_one_adapter(AsyncMock(), adapter)
        self.assertTrue(factory.await_args.kwargs['native_identity'])

    def test_unknown_profile_is_rejected_before_launch(self):
        with patch.dict(os.environ, {'SCRAPER_BROWSER_PROFILE': 'retry-alternate'}):
            with self.assertRaises(ValueError):
                get_browser_profile()


class AmazonPriceBaselineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.adapter = AmazonCatalogAdapter(AMAZON_GB)
        self.started = datetime(2026, 10, 9, 10, tzinfo=UTC)

    def catalog(self, day, price='100', *, country='GB', currency='GBP', target=True):
        path = self.root / f'amazon_{country.lower()}_202610{day:02}.csv'
        fields = ['platform', 'country', 'scraped_at', 'url', 'raw_text', 'currency',
                  'price_local', 'price_eur', 'price_hint_eur', 'asin']
        with path.open('w', newline='', encoding='utf-8') as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for index in range(100):
                asin = f'B{index + 1:09}'
                if index == 0 and not target:
                    asin = 'B999999999'
                writer.writerow({
                    'platform': 'Amazon', 'country': country, 'scraped_at': f'2026-10-{day:02}T09:00:00Z',
                    'url': f'https://www.amazon.co.uk/dp/{asin}', 'raw_text': 'TCL 55 TV', 'asin': asin,
                    'currency': currency, 'price_local': price, 'price_eur': price, 'price_hint_eur': price,
                })
        return path

    def test_latest_valid_formal_catalog_is_frozen_before_run(self):
        self.catalog(7, '100')
        self.catalog(8, '120')
        self.catalog(10, '999')
        baseline = self.adapter._load_price_baseline(self.started, self.root)
        self.assertEqual('120', baseline['B000000001']['price'])
        self.assertEqual('amazon_gb_20261008.csv', baseline['B000000001']['source_file'])
        self.catalog(8, '130')
        self.assertEqual('120', baseline['B000000001']['price'], '冻结字典不能被后续文件变更污染')

    def test_invalid_currency_or_nonfinite_latest_file_cannot_be_baseline(self):
        self.catalog(7, '100')
        for price, currency in [('120', 'EUR'), ('NaN', 'GBP'), ('0', 'GBP')]:
            self.catalog(8, price, currency=currency)
            result = self.adapter._load_price_baseline(self.started, self.root)
            self.assertEqual('100', result['B000000001']['price'])

    def test_missing_asin_in_latest_catalog_does_not_borrow_older_price(self):
        self.catalog(7, '100')
        self.catalog(8, '120', target=False)
        self.assertNotIn('B000000001', self.adapter._load_price_baseline(self.started, self.root))

    def test_partial_and_diagnostic_candidates_are_not_qualified_catalogs(self):
        path = self.catalog(8)
        path.write_text('\n'.join(path.read_text().splitlines()[:3]) + '\n')
        self.assertEqual({}, self.adapter._load_price_baseline(self.started, self.root))

    def test_visible_validate_captcha_target_is_access_challenge(self):
        self.assertEqual('access_challenge', _page_rejection_reason(200, {'accessChallengeTarget': True}))


class AmazonObservationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.adapter = AmazonCatalogAdapter(AMAZON_GB)
        self.adapter._price_baseline = {'B000000001': {
            'price': '100', 'currency': 'GBP', 'observed_at': '2026-10-08T00:00:00Z',
        }}
        self.capture = self.enterContext(patch('price_anomalies.record_price_change',
                                              new=AsyncMock(return_value=Path('event.json'))))

    def item(self, value):
        return self.adapter._build_item('B000000001', 'TCL 55 TV', 'TCL', 55, f'£{value}')

    async def test_threshold_boundaries_capture_original_page_and_pending_state(self):
        page = AsyncMock()
        page.url = 'https://www.amazon.co.uk/dp/B000000001'
        for value in ('200', '50'):
            await self.adapter._record_price_observation(page, self.item(value), source='catalog_detail')
            self.assertIs(page, self.capture.await_args.kwargs['page'])
            observation = self.capture.await_args.kwargs['observation']
            self.assertEqual('pending', observation['ingestion_status'])
            self.assertEqual('unvalidated', observation['validation_state'])
            self.assertEqual('B000000001', observation['asin'])
        self.assertEqual(2, self.capture.await_count)

    async def test_nontrigger_and_currency_mismatch_do_not_capture(self):
        page = AsyncMock()
        for value in ('199.99', '50.01'):
            await self.adapter._record_price_observation(page, self.item(value), source='catalog_detail')
        self.adapter._price_baseline['B000000001']['currency'] = 'EUR'
        await self.adapter._record_price_observation(page, self.item('200'), source='catalog_detail')
        self.capture.assert_not_awaited()

    async def test_search_snapshot_requires_visible_corresponding_asin(self):
        page = MagicMock()
        page.url = 'https://www.amazon.co.uk/s?k=tcl&page=1'
        card = MagicMock()
        card.count = AsyncMock(return_value=1)
        card.is_visible = AsyncMock(return_value=True)
        card.scroll_into_view_if_needed = AsyncMock()
        page.locator.return_value.first = card
        await self.adapter._record_price_observation(page, self.item('200'),
                                                    source='catalog_search', query='tcl', page_number=1)
        self.assertIs(page, self.capture.await_args.kwargs['page'])
        card.scroll_into_view_if_needed.assert_awaited_once()
        self.assertIn('B000000001', page.locator.call_args.args[0])
        card.count.return_value = 0
        await self.adapter._record_price_observation(page, self.item('50'), source='catalog_search')
        self.assertIsNone(self.capture.await_args.kwargs['page'])
        self.assertEqual('search_observation_asin_card_unavailable', self.capture.await_args.kwargs['evidence_source'])

    async def test_capture_failure_leaves_quote_and_business_result_unchanged(self):
        item = self.item('200')
        self.capture.side_effect = OSError('evidence unavailable')
        await self.adapter._record_price_observation(AsyncMock(), item, source='catalog_detail')
        self.assertEqual(200.0, item.price_local)
        self.assertEqual([], self.adapter._price_change_paths)

    def test_rejected_catalog_marks_all_events_rejected(self):
        self.adapter._price_change_paths = [Path('event.json')]
        with patch('price_anomalies.update_price_change_status') as update:
            self.adapter.finalize_price_observations('rejected', 'incomplete catalog')
        self.assertEqual('rejected', update.call_args.kwargs['ingestion_status'])

    async def test_transport_retry_reuses_session_without_clearing_cookies(self):
        page = AsyncMock()
        calls = 0
        async def location(*args):
            nonlocal calls
            calls += 1
            if calls == 1:
                vars(page)['_amazon_failure_reason'] = 'navigation_error'
                return False
            return True
        with patch('catalog_scrape.adapters.amazon.set_amazon_market_location', side_effect=location), patch(
            'catalog_scrape.adapters.amazon.verify_amazon_detail_canary', new=AsyncMock(return_value=True),
        ), patch('catalog_scrape.adapters.amazon.asyncio.sleep', new=AsyncMock()), patch(
            'catalog_scrape.diagnostics.capture_failure', new=AsyncMock(return_value=None),
        ):
            self.assertTrue(await self.adapter._prepare_market_session(page))
        self.assertEqual(2, calls)
        page.context.clear_cookies.assert_not_awaited()
        page.goto.assert_not_awaited()


class CurrysPaginationLedgerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.enterContext(patch.dict(os.environ, {
            'CURRYS_CATALOG_DIAGNOSTICS_DIR': self.temp.name,
            'SCRAPER_BROWSER_PROFILE': 'native',
        }))
        self.sleep = self.enterContext(patch('catalog_scrape.adapters.currys.asyncio.sleep', new=AsyncMock()))
        self.enterContext(patch('catalog_scrape.diagnostics.capture_failure', new=AsyncMock(return_value=Path('evidence.json'))))
        self.adapter = CurrysCatalogAdapter()
        self.context = AsyncMock()
        self.adapter._new_context = AsyncMock(return_value=self.context)

    @staticmethod
    def card(number):
        product_id = 10280000 + number
        return {'slug': f'tcl-55-tv-{product_id}', 'title': f'TCL 55 TV MODEL {number}',
                'price': '£300', 'href': f'/products/tcl-55-tv-{product_id}.html'}

    async def test_transient_page_is_recorded_and_retried_at_most_once(self):
        seen = Counter()
        async def page_result(browser, start, **kwargs):
            seen[start] += 1
            if start == 50 and seen[start] == 1:
                return 503, []
            self.adapter._last_page_info = {'pagination_evidence': {'verified': True, 'trusted_total': 101}}
            return 200, [self.card(index) for index in range(start, min(start + 50, 101))]
        self.adapter._scrape_page = AsyncMock(side_effect=page_result)
        await self.adapter.fetch_catalog_from_browser(AsyncMock())
        starts = [call.args[1] for call in self.adapter._scrape_page.await_args_list]
        self.assertEqual([0, 50, 50, 100], starts)
        self.assertEqual([2, 2, 1, 2], [call.kwargs['navigation_budget'] for call in self.adapter._scrape_page.await_args_list])
        self.assertTrue(self.adapter.catalog_report['complete'])
        self.assertEqual([], self.adapter.catalog_report['missing_pages'])
        self.assertEqual(2, len(self.adapter.catalog_report['pages']['50']['attempts']))
        self.adapter._new_context.assert_awaited_once()
        self.context.close.assert_awaited_once()
        self.assertTrue(list(Path(self.temp.name).rglob('report.json')))

    async def test_helper_natural_navigation_consumes_outer_retry_budget(self):
        calls = []
        async def page_result(browser, start, *, navigation_budget):
            calls.append((start, navigation_budget))
            if start == 0:
                self.adapter._last_page_info = {'navigation_count': 2, 'http_status': 403, 'blocked': True, 'retryable': True}
                return 403, []
            self.adapter._last_page_info = {'navigation_count': 1, 'http_status': 200}
            return 200, []
        self.adapter._scrape_page = page_result
        await self.adapter.fetch_catalog_from_browser(AsyncMock())
        self.assertEqual([(0, 2), (50, 2)], calls)
        self.assertEqual(2, self.adapter.catalog_report['pages']['0']['navigation_count'])
        self.assertEqual(1, len(self.adapter.catalog_report['pages']['0']['attempts']))
        self.assertFalse(self.adapter.catalog_report['complete'])

    async def test_unrecovered_gap_keeps_daily_observations_but_rejects_weekly_catalog(self):
        self.adapter._scrape_page = AsyncMock(side_effect=[
            (200, [self.card(1)]), (503, []), (503, []), (200, []),
        ])
        page = AsyncMock()
        with self.assertRaises(CurrysCatalogIncomplete):
            await self.adapter.fetch_catalog(page)
        self.assertEqual([50], self.adapter.catalog_report['missing_pages'])
        self.assertEqual(1, self.adapter.catalog_report['observed_items'])
        self.assertFalse(self.adapter.catalog_report['complete'])

    async def test_unverified_page_position_keeps_prices_and_later_pages_but_not_complete_weekly(self):
        async def page_result(browser, start, **kwargs):
            self.adapter._last_page_info = {'http_status': 200, 'navigation_count': 1,
                                           'pagination_evidence': {'verified': start != 50, 'trusted_total': 151}}
            return 200, [self.card(start + 1)] if start < 150 else []
        self.adapter._scrape_page = AsyncMock(side_effect=page_result)
        items = await self.adapter.fetch_catalog_from_browser(AsyncMock())
        self.assertEqual(3, len(items))
        self.assertEqual([0, 50, 100, 150], [call.args[1] for call in self.adapter._scrape_page.await_args_list])
        self.assertEqual([], self.adapter.catalog_report['missing_pages'])
        self.assertEqual([50], self.adapter.catalog_report['pagination_unverified_pages'])
        self.assertFalse(self.adapter.catalog_report['end_observed'], '空页不能代替显式完整性证明')
        self.assertFalse(self.adapter.catalog_report['complete'])
        self.assertFalse(self.adapter.catalog_report['blocked'])
        with self.assertRaises(CurrysCatalogIncomplete):
            await self.adapter.fetch_catalog(AsyncMock())

    async def test_old_successful_sequence_keeps_later_pages_after_isolated_403(self):
        serial = 0
        def cards(count):
            nonlocal serial
            rows = [self.card(index) for index in range(serial + 1, serial + count + 1)]
            serial += count
            return rows
        # 回放 37875492135 的目录轨迹：start=50 两次403；start=250 首次403后恢复。
        self.adapter._scrape_page = AsyncMock(side_effect=[
            (200, cards(50)), (403, []), (403, []),
            (200, cards(50)), (200, cards(50)), (200, cards(45)),
            (403, []), (200, cards(50)), (200, cards(50)),
            (200, cards(48)), (200, cards(1)), (200, cards(6)),
            (200, cards(2)), (200, []),
        ])
        items = await self.adapter.fetch_catalog_from_browser(AsyncMock())
        self.assertEqual(352, len(items))
        self.assertEqual([50], self.adapter.catalog_report['missing_pages'])
        self.assertTrue(self.adapter.catalog_report['had_access_denials'])
        self.assertFalse(self.adapter.catalog_report['blocked'])
        self.assertFalse(self.adapter.catalog_report['complete'])
        self.assertEqual(200, self.adapter.catalog_report['pages']['250']['status'])
        self.assertTrue(all(len(row['attempts']) <= 2 for row in self.adapter.catalog_report['pages'].values()))

    async def test_first_403_recovers_and_does_not_poison_complete_catalog(self):
        calls = 0
        async def page_result(browser, start, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return 403, []
            self.adapter._last_page_info = {'pagination_evidence': {'verified': True, 'trusted_total': 1}}
            return 200, [self.card(1)]
        self.adapter._scrape_page = AsyncMock(side_effect=page_result)
        items = await self.adapter.fetch_catalog(AsyncMock())
        self.assertEqual(1, len(items))
        self.assertTrue(self.adapter.catalog_report['had_access_denials'])
        self.assertFalse(self.adapter.catalog_report['blocked'])
        self.assertTrue(self.adapter.catalog_report['complete'])
        self.assertEqual([], self.adapter.catalog_report['missing_pages'])
        self.assertEqual(2, len(self.adapter.catalog_report['pages']['0']['attempts']))

    async def test_two_consecutive_final_403_pages_stop_with_four_total_attempts(self):
        self.adapter._scrape_page = AsyncMock(return_value=(403, []))
        items = await self.adapter.fetch_catalog_from_browser(AsyncMock())
        self.assertEqual([], items)
        self.assertEqual([0, 0, 50, 50], [call.args[1] for call in self.adapter._scrape_page.await_args_list])
        self.assertTrue(self.adapter.catalog_report['blocked'])
        self.assertTrue(self.adapter.catalog_report['had_access_denials'])
        self.assertEqual('consecutive_access_denials', self.adapter.catalog_report['termination'])
        self.assertFalse(self.adapter.catalog_report['complete'])

    async def test_429_stops_immediately_without_retrying_or_scanning_next_page(self):
        self.adapter._scrape_page = AsyncMock(return_value=(429, []))
        await self.adapter.fetch_catalog_from_browser(AsyncMock())
        self.adapter._scrape_page.assert_awaited_once()
        self.sleep.assert_not_awaited()
        self.assertTrue(self.adapter.catalog_report['rate_limited'])
        self.assertFalse(self.adapter.catalog_report['blocked'])
        self.assertFalse(self.adapter.catalog_report['had_access_denials'])
        self.assertEqual('rate_limited', self.adapter.catalog_report['termination'])

    async def test_429_on_retry_never_causes_third_attempt(self):
        self.adapter._scrape_page = AsyncMock(side_effect=[(403, []), (429, [])])
        await self.adapter.fetch_catalog_from_browser(AsyncMock())
        self.assertEqual(2, self.adapter._scrape_page.await_count)
        self.assertEqual(1, self.sleep.await_count)
        self.assertTrue(self.adapter.catalog_report['rate_limited'])
        self.assertTrue(self.adapter.catalog_report['had_access_denials'])
        self.assertFalse(self.adapter.catalog_report['blocked'])

    async def test_mixed_consecutive_failures_do_not_claim_persistent_access_denial(self):
        self.adapter._scrape_page = AsyncMock(side_effect=[
            (403, []), (403, []), (503, []), (503, []),
        ])
        await self.adapter.fetch_catalog_from_browser(AsyncMock())
        self.assertEqual(4, self.adapter._scrape_page.await_count)
        self.assertFalse(self.adapter.catalog_report['blocked'])
        self.assertTrue(self.adapter.catalog_report['had_access_denials'])
        self.assertEqual('consecutive_failed_pages', self.adapter.catalog_report['termination'])

    async def test_http_403_is_classified_even_when_dom_unreadable(self):
        page = AsyncMock()
        page.on = MagicMock()
        page.url = 'https://www.currys.co.uk/tv-and-audio/televisions/tvs?start=0&sz=50'
        page.goto.return_value = SimpleNamespace(status=403)
        page.evaluate.side_effect = RuntimeError('DOM unreadable')
        self.context.new_page.return_value = page
        status, cards = await self.adapter._scrape_page(AsyncMock(), 0)
        self.assertEqual(403, status)
        self.assertEqual([], cards)
        self.assertTrue(self.adapter._last_page_info['blocked'])
        self.assertFalse(self.adapter._last_page_info['retryable'])
        page.evaluate.assert_awaited_once()

    async def test_current_keeps_separate_contexts_for_bounded_page_attempts(self):
        context1, context2, context3 = AsyncMock(), AsyncMock(), AsyncMock()
        page1, page2, page3 = AsyncMock(), AsyncMock(), AsyncMock()
        for index, page in enumerate((page1, page2, page3)):
            page.on = MagicMock()
            page.url = f'https://www.currys.co.uk/tv-and-audio/televisions/tvs?start={50 if index == 2 else 0}&sz=50'
        context1.new_page.return_value = page1
        context2.new_page.return_value = page2
        context3.new_page.return_value = page3
        page1.goto.return_value = SimpleNamespace(status=503)
        page2.goto.return_value = SimpleNamespace(status=200)
        page2.is_visible.return_value = False
        page2.evaluate.side_effect = [{}, {}, [self.card(1)]]
        page3.goto.return_value = SimpleNamespace(status=200)
        page3.is_visible.return_value = False
        page3.evaluate.side_effect = [{}, {}, [], []]
        self.adapter._new_context = AsyncMock(side_effect=[context1, context2, context3])
        with patch.dict(os.environ, {'SCRAPER_BROWSER_PROFILE': 'current'}):
            items = await self.adapter.fetch_catalog_from_browser(AsyncMock())
        self.assertEqual(1, len(items))
        self.assertEqual(3, self.adapter._new_context.await_count)
        context1.close.assert_awaited_once()
        context2.close.assert_awaited_once()
        context3.close.assert_awaited_once()
        self.assertFalse(self.adapter.catalog_report['blocked'])
        self.assertFalse(self.adapter.catalog_report['had_access_denials'])
        self.assertFalse(self.adapter.catalog_report['complete'], '该fixture只有空页，没有可信总数/页序证据')

    async def test_page_limit_cannot_be_called_complete(self):
        self.adapter._scrape_page = AsyncMock(side_effect=[
            (200, [self.card(1)]), (200, [self.card(2)]),
        ])
        with patch('catalog_scrape.adapters.currys.MAX_PAGES', 2):
            await self.adapter.fetch_catalog_from_browser(AsyncMock())
        self.assertFalse(self.adapter.catalog_report['complete'])
        self.assertFalse(self.adapter.catalog_report['end_observed'])
        self.assertEqual('page_limit', self.adapter.catalog_report['termination'])

    async def test_repeated_page_stops_without_claiming_normal_end(self):
        self.adapter._scrape_page = AsyncMock(return_value=(200, [self.card(1)]))
        await self.adapter.fetch_catalog_from_browser(AsyncMock())
        self.assertEqual(2, self.adapter._scrape_page.await_count)
        self.assertEqual('repeated_page', self.adapter.catalog_report['termination'])
        self.assertFalse(self.adapter.catalog_report['complete'])
        self.assertFalse(self.adapter.catalog_report['end_observed'])


if __name__ == '__main__':
    unittest.main()
