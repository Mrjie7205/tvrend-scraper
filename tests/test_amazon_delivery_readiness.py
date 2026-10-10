"""配送弹窗迟到 DOM 和可选商品白页只能有界恢复，不能代替身份与价格门禁。"""
from __future__ import annotations

from contextlib import ExitStack
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from catalog_scrape.adapters import amazon
from catalog_scrape.diagnostics import AmazonCatalogDiagnostics


ASIN = 'B0FCL379WH'


def state(market=amazon.AMAZON_IT, *, text='', asin='', normal=True, **extra):
    return {
        'currentUrl': market.base_url + (f'/dp/{ASIN}' if market.code == 'GB' else '/'),
        'normalPage': normal, 'productAsin': asin, 'deliveryText': text,
        'bodyTextPresent': normal,
        'deliveryHeader': {'nodePresent': bool(text), 'visibleNodePresent': bool(text),
                           'textPresent': bool(text), 'source': 'glow_line2' if text else 'none'},
        **extra,
    }


class AmazonDeliveryReadinessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.capture = self.enterContext(patch('catalog_scrape.diagnostics.capture_failure',
                                               new=AsyncMock(return_value=None)))

    def page(self):
        page = AsyncMock()
        page.goto.return_value = SimpleNamespace(status=200)
        return page

    async def test_delayed_current_header_needs_no_refresh(self):
        page = self.page()
        page.evaluate.side_effect = [state(), state(deliveryHeader={
            'nodePresent': True, 'visibleNodePresent': True, 'source': 'none'}), state(text='Milano 20121')]
        self.assertTrue(await amazon.verify_amazon_delivery_location(page, amazon.AMAZON_IT, after_popup=True))
        page.goto.assert_not_awaited()
        report = vars(page)['_amazon_popup_delivery_summary']
        self.assertEqual('verified_current_page', report['result'])
        self.assertEqual(3, len(report['observations']))
        self.assertFalse(report['observations'][0]['node_present'])
        self.assertTrue(report['observations'][1]['visible_node_present'])
        self.assertFalse(report['observations'][1]['text_present'])

    async def test_one_refresh_also_waits_for_late_header(self):
        page = self.page()
        count = len(amazon._DELIVERY_OBSERVATION_DELAYS_MS)
        page.evaluate.side_effect = [state()] * (count + 2) + [state(text='Milano 20121')]
        self.assertTrue(await amazon.verify_amazon_delivery_location(page, amazon.AMAZON_IT, after_popup=True))
        page.goto.assert_awaited_once()
        report = vars(page)['_amazon_popup_delivery_summary']
        self.assertEqual('verified_after_refresh', report['result'])
        self.assertEqual(1, report['refresh_count'])

    async def test_missing_header_stops_after_one_refresh(self):
        page = self.page()
        page.evaluate.return_value = state()
        self.assertFalse(await amazon.verify_amazon_delivery_location(page, amazon.AMAZON_IT, after_popup=True))
        page.goto.assert_awaited_once()
        self.assertEqual(2 * len(amazon._DELIVERY_OBSERVATION_DELAYS_MS), page.evaluate.await_count)
        self.assertEqual('header_unconfirmed', vars(page)['_amazon_popup_delivery_summary']['result'])

    async def test_wrong_postcode_is_rejected_without_refresh(self):
        page = self.page()
        page.evaluate.return_value = state(text='Milano 20122')
        self.assertFalse(await amazon.verify_amazon_delivery_location(page, amazon.AMAZON_IT, after_popup=True))
        page.goto.assert_not_awaited()
        self.assertEqual('postcode_mismatch', vars(page)['_amazon_popup_delivery_summary']['result'])

    async def test_refresh_interstitial_is_not_clicked(self):
        page = self.page()
        count = len(amazon._DELIVERY_OBSERVATION_DELAYS_MS)
        page.evaluate.side_effect = [state()] * count + [state(normal=False, continueShopping=True)]
        with self.assertRaises(amazon.AmazonCatalogIncomplete):
            await amazon.verify_amazon_delivery_location(page, amazon.AMAZON_IT, after_popup=True)
        page.goto.assert_awaited_once()
        page.get_by_role.assert_not_called()
        self.assertEqual('page_rejected', vars(page)['_amazon_popup_delivery_summary']['result'])

    async def test_popup_failure_does_not_repeat_session_address_submission(self):
        page = self.page()
        page.evaluate.return_value = state()
        adapter = amazon.AmazonCatalogAdapter(amazon.AMAZON_IT)
        async def location(*args):
            return await amazon.verify_amazon_delivery_location(page, amazon.AMAZON_IT, after_popup=True)
        with patch.object(amazon, 'set_amazon_market_location', new=AsyncMock(side_effect=location)) as submit, \
             patch.object(amazon, 'verify_amazon_detail_canary', new=AsyncMock()) as canary:
            self.assertFalse(await adapter._prepare_market_session(page))
        submit.assert_awaited_once()
        page.goto.assert_awaited_once()
        canary.assert_not_awaited()

    async def test_popup_safe_diagnostics_survive_failure(self):
        page = self.page()
        page.evaluate.return_value = state(text='Private Address Milano 20122')
        adapter = amazon.AmazonCatalogAdapter(amazon.AMAZON_IT)
        async def location(*args):
            return await amazon.verify_amazon_delivery_location(page, amazon.AMAZON_IT, after_popup=True)
        with TemporaryDirectory() as temporary:
            adapter.diagnostics = AmazonCatalogDiagnostics('IT', Path(temporary))
            with patch.object(amazon, 'set_amazon_market_location', new=AsyncMock(side_effect=location)):
                self.assertFalse(await adapter._prepare_market_session(page))
            report = json.loads((adapter.diagnostics.path / 'report.json').read_text(encoding='utf-8'))
        payload = json.dumps(report['popupDeliveryVerification'])
        for secret in ('Private', '20122', 'http', 'cookie', 'token'):
            self.assertNotIn(secret, payload)
        self.assertEqual('postcode_mismatch', report['popupDeliveryVerification']['result'])

    async def test_delayed_asin_and_header_require_both_before_success(self):
        page = self.page()
        good = state(amazon.AMAZON_GB, text='Coventry CV4 7ES', asin=ASIN)
        page.evaluate.side_effect = [state(amazon.AMAZON_GB, normal=False),
                                     state(amazon.AMAZON_GB, text='Coventry CV4 7ES'), good]
        with patch.object(amazon, 'set_amazon_market_location', new=AsyncMock()) as reset:
            result = await amazon.ensure_amazon_page_delivery(page, amazon.AMAZON_GB, good['currentUrl'],
                                                              asin=ASIN, http_status=200)
        self.assertEqual(good, result)
        self.assertEqual(3, page.evaluate.await_count)
        reset.assert_not_awaited()
        page.goto.assert_not_awaited()

    async def test_normal_correct_delivery_without_asin_control_keeps_existing_fast_path(self):
        page = self.page()
        normal = state(amazon.AMAZON_GB, text='Coventry CV4 7ES', asin='')
        page.evaluate.return_value = normal
        with patch.object(amazon, 'set_amazon_market_location', new=AsyncMock()) as reset:
            self.assertEqual(normal, await amazon.ensure_amazon_page_delivery(
                page, amazon.AMAZON_GB, normal['currentUrl'], asin=ASIN, http_status=200))
        reset.assert_not_awaited()
        page.goto.assert_not_awaited()
        page.wait_for_timeout.assert_not_awaited()
        self.assertNotIn('_amazon_target_readiness_summary', vars(page))

    async def test_correct_postcode_cannot_override_wrong_asin(self):
        page = self.page()
        wrong = state(amazon.AMAZON_GB, text='Coventry CV4 7ES', asin='B000000002')
        page.evaluate.return_value = wrong
        with self.assertRaises(amazon.AmazonCatalogIncomplete):
            await amazon.ensure_amazon_page_delivery(page, amazon.AMAZON_GB, wrong['currentUrl'], asin=ASIN, http_status=200)
        page.wait_for_timeout.assert_not_awaited()
        self.assertEqual('target_mismatch', vars(page)['_amazon_target_readiness_summary']['result'])

    async def test_blank_optional_detail_has_no_price_and_is_skipped(self):
        page = self.page()
        page.evaluate.return_value = state(amazon.AMAZON_GB, normal=False)
        adapter = amazon.AmazonCatalogAdapter(amazon.AMAZON_GB)
        adapter._record_price_observation = AsyncMock()
        with patch.object(amazon, 'set_amazon_market_location', new=AsyncMock()) as reset:
            self.assertIsNone(await adapter._detail_item(page, ASIN, 'Sony'))
        reset.assert_not_awaited()
        page.goto.assert_awaited_once()
        adapter._record_price_observation.assert_not_awaited()
        self.assertEqual('blank_product_dom', vars(page)['_amazon_target_readiness_summary']['result'])
        self.assertTrue(any(call.kwargs['reason'] == 'product_dom_unready' for call in self.capture.await_args_list))

    async def test_blank_optional_variant_preserves_verified_seed_and_adds_no_price(self):
        page = self.page()
        page.evaluate.return_value = state(amazon.AMAZON_GB, normal=False)
        adapter = amazon.AmazonCatalogAdapter(amazon.AMAZON_GB)
        adapter._record_price_observation = AsyncMock()
        seed = adapter._build_item(ASIN, 'Sony 55 inch television', 'Sony', 55, '£499.00')
        verified = {ASIN: seed}
        self.assertEqual(-1, await adapter._expand_variants_from_seed(page, seed, verified))
        self.assertEqual({ASIN: seed}, verified)
        self.assertEqual(499, verified[ASIN].price_local)
        adapter._record_price_observation.assert_not_awaited()
        self.assertTrue(any(call.kwargs['stage'] == 'catalog_variant' and call.kwargs['reason'] == 'product_dom_unready'
                            for call in self.capture.await_args_list))

    async def test_missing_dom_after_wrong_delivery_is_not_classified_as_optional_blank(self):
        page = self.page()
        page.evaluate.side_effect = [state(amazon.AMAZON_GB, text='London SW1A 1AA', asin=ASIN)] + [
            state(amazon.AMAZON_GB, normal=False)] * len(amazon._DELIVERY_OBSERVATION_DELAYS_MS)
        with self.assertRaises(amazon.AmazonCatalogIncomplete) as raised:
            await amazon.ensure_amazon_page_delivery(page, amazon.AMAZON_GB,
                amazon.AMAZON_GB.base_url + f'/dp/{ASIN}', asin=ASIN, http_status=200)
        self.assertNotIsInstance(raised.exception, amazon.AmazonProductPageUnready)

    async def test_only_200_blank_same_asin_is_optional(self):
        for change in ({'productAsin': 'B000000002'}, {'robotCheck': True},
                       {'currentUrl': amazon.AMAZON_IT.base_url + f'/dp/{ASIN}'},
                       {'bodyTextPresent': True}):
            with self.subTest(change=change):
                page = self.page()
                page.evaluate.return_value = state(amazon.AMAZON_GB, normal=False, **change)
                adapter = amazon.AmazonCatalogAdapter(amazon.AMAZON_GB)
                with self.assertRaises(amazon.AmazonCatalogIncomplete) as raised:
                    await adapter._detail_item(page, ASIN, 'Sony')
                self.assertNotIsInstance(raised.exception, amazon.AmazonProductPageUnready)
        for status in (403, 429):
            with self.subTest(status=status):
                page = self.page()
                page.goto.return_value = SimpleNamespace(status=status)
                page.evaluate.return_value = state(amazon.AMAZON_GB, normal=False)
                self.assertIsNone(await amazon.AmazonCatalogAdapter(amazon.AMAZON_GB)._detail_item(page, ASIN, 'Sony'))
                self.assertNotIn('_amazon_target_readiness_summary', vars(page))

    async def test_challenge_during_target_wait_is_not_optional(self):
        page = self.page()
        page.evaluate.side_effect = [state(amazon.AMAZON_GB, normal=False),
                                     state(amazon.AMAZON_GB, normal=False, captcha=True)]
        with self.assertRaises(amazon.AmazonCatalogIncomplete) as raised:
            await amazon.ensure_amazon_page_delivery(page, amazon.AMAZON_GB,
                                                     amazon.AMAZON_GB.base_url + f'/dp/{ASIN}', asin=ASIN, http_status=200)
        self.assertNotIsInstance(raised.exception, amazon.AmazonProductPageUnready)
        page.goto.assert_not_awaited()

    async def test_returned_product_after_address_reset_can_settle_without_second_reset(self):
        page = self.page()
        count = len(amazon._DELIVERY_OBSERVATION_DELAYS_MS)
        initial = state(amazon.AMAZON_GB, asin=ASIN)
        good = state(amazon.AMAZON_GB, text='Coventry CV4 7ES', asin=ASIN)
        page.evaluate.side_effect = [initial] * count + [state(amazon.AMAZON_GB, normal=False), good]
        with patch.object(amazon, 'set_amazon_market_location', new=AsyncMock(return_value=True)) as reset, \
             patch.object(amazon, '_checked_page_state', wraps=amazon._checked_page_state) as checked:
            self.assertEqual(good, await amazon.ensure_amazon_page_delivery(
                page, amazon.AMAZON_GB, good['currentUrl'], asin=ASIN, http_status=200))
        reset.assert_awaited_once()
        page.goto.assert_awaited_once()
        checked.assert_any_await(page, amazon.AMAZON_GB, 200, target_url=good['currentUrl'], asin=ASIN)

    async def test_one_blank_history_pdp_keeps_543_current_verified_candidates(self):
        page = self.page()
        adapter = amazon.AmazonCatalogAdapter(amazon.AMAZON_GB)
        adapter._prepare_market_session = AsyncMock(return_value=True)
        adapter._record_price_observation = AsyncMock()
        current_url = ''
        rows = [{'asin': f'B{i:09d}', 'title': 'TCL 55 inch television', 'price': '£499.00'}
                for i in range(543)]
        async def goto(url, **kwargs):
            nonlocal current_url
            current_url = url
            return SimpleNamespace(status=200)
        async def evaluate(script, *args):
            if script == amazon._JS_SEARCH_STATE:
                if '/dp/' in current_url:
                    return state(amazon.AMAZON_GB, normal=False)
                return state(amazon.AMAZON_GB, text='Coventry CV4 7ES', currentUrl=current_url)
            if script == amazon._JS_EXTRACT:
                return rows
            raise AssertionError('白页不得进入详情价格提取')
        page.goto.side_effect = goto
        page.evaluate.side_effect = evaluate
        previous = adapter._build_item(ASIN, 'Sony 55 inch television', 'Sony', 55, '£999.00')
        with TemporaryDirectory() as temporary, ExitStack() as stack:
            stack.enter_context(patch.dict('os.environ', {'AMAZON_DIAGNOSTICS_DIR': temporary}))
            for key, value in (('BRAND_QUERIES', ('tcl',)), ('TARGET_YEARS', ()),
                               ('EXTRA_SERIES_QUERIES', ()), ('MAX_PAGES', 1), ('EXPAND_VARIANTS', False)):
                stack.enter_context(patch.object(amazon, key, value))
            stack.enter_context(patch.object(amazon, '_accept_cookie', new=AsyncMock()))
            stack.enter_context(patch.object(amazon.asyncio, 'sleep', new=AsyncMock()))
            stack.enter_context(patch.object(adapter, '_series_rescue_queries', return_value=[]))
            stack.enter_context(patch.object(adapter, '_load_recent_catalog_items', return_value=[previous]))
            stack.enter_context(patch.object(adapter, '_select_previous_recovery_items', return_value=[previous]))
            items = await adapter.fetch_catalog(page)
            self.assertEqual('blank_product_dom', adapter.diagnostics.report['targetReadiness']['result'])
            self.assertTrue(any(row.get('reason') == 'product_dom_unready' for row in adapter.diagnostics.report['pages']))
        self.assertEqual(543, len(items))
        self.assertNotIn(ASIN, {item.extra['asin'] for item in items})
        self.assertTrue(all(item.price_local == 499 for item in items))
        self.assertEqual(543, adapter._record_price_observation.await_count)


if __name__ == '__main__':
    unittest.main()
