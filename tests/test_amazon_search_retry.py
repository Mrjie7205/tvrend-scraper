"""搜索页临时 5xx 仅在同会话、同目标与原发布门禁内重试，不访问网络。"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from catalog_scrape.adapters.amazon import (
    AMAZON_ES, AmazonCatalogAdapter, AmazonCatalogIncomplete,
    _JS_EXTRACT, _JS_SEARCH_STATE,
)
from catalog_scrape.diagnostics import AmazonCatalogDiagnostics
from catalog_scrape.run_weekly import run_one_adapter


class AmazonSearchRetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict('os.environ', {'AMAZON_DIAGNOSTICS_DIR': str(self.root / 'diagnostics')}))
        self.capture = self.enterContext(patch(
            'catalog_scrape.diagnostics.capture_failure', new=AsyncMock(return_value=None),
        ))
        self.sleep = self.enterContext(patch('catalog_scrape.adapters.amazon.asyncio.sleep', new=AsyncMock()))
        for name, value in (
            ('BRAND_QUERIES', ('tcl',)), ('TARGET_YEARS', ()),
            ('EXTRA_SERIES_QUERIES', ()), ('MAX_PAGES', 1), ('EXPAND_VARIANTS', False),
        ):
            self.enterContext(patch(f'catalog_scrape.adapters.amazon.{name}', value))
        self.accept_cookie = self.enterContext(patch('catalog_scrape.adapters.amazon._accept_cookie', new=AsyncMock()))

    def adapter(self):
        adapter = AmazonCatalogAdapter(AMAZON_ES)
        adapter._prepare_market_session = AsyncMock(return_value=True)
        adapter._record_price_observation = AsyncMock()
        adapter._load_price_baseline = lambda *args: {}
        adapter._load_recent_catalog_items = lambda: []
        adapter._series_rescue_queries = lambda items: []
        adapter.diagnostics = AmazonCatalogDiagnostics('ES', self.root / 'diagnostics')
        return adapter

    @staticmethod
    def rows(count=1, price='399,00 €'):
        return [{'asin': f'B{number:09}', 'brand': 'TCL', 'title': 'TCL 55 pulgadas TV',
                 'price': price, 'sponsored': False} for number in range(count)]

    def page(self, steps):
        """每个 step 对应一次 goto，状态和卡片始终属于该次页面。"""
        page = AsyncMock()
        remaining = iter(steps)
        current = {}

        async def navigate(url, **kwargs):
            nonlocal current
            current = next(remaining)
            if isinstance(current, Exception):
                raise current
            page.url = current.get('url', url)
            return SimpleNamespace(status=current.get('status', 200))

        async def evaluate(script):
            if script == _JS_SEARCH_STATE:
                if current.get('state_error'):
                    raise current['state_error']
                if current.get('state_hang'):
                    await asyncio.Event().wait()
                success = current.get('status', 200) == 200
                return {
                    'currentUrl': page.url, 'bodyTextPresent': True,
                    'captcha': False, 'robotCheck': False, 'accessChallengeTarget': False,
                    'visibleChallengeControls': False, 'continueShopping': False,
                    'normalPage': success, 'nextDisabled': True,
                    'deliveryText': 'Madrid 28013' if success else '',
                    **current.get('state', {}),
                }
            if script == _JS_EXTRACT:
                return current.get('rows', self.rows())
            raise AssertionError('不允许请求额外页面或处理访问挑战')

        page.goto.side_effect = navigate
        page.evaluate.side_effect = evaluate
        return page

    async def navigate(self, adapter, page, query_kind='brand'):
        info = {'query': 'tcl', 'page': 2, 'queryKind': query_kind}
        url = f'{AMAZON_ES.base_url}/s?k=tcl+televisor&page=2'
        await adapter._goto_search_page(page, url, info, 45000)
        return info

    @staticmethod
    def report(adapter):
        return json.loads((adapter.diagnostics.path / 'report.json').read_text(encoding='utf-8'))

    async def test_503_then_200_reuses_page_and_exact_url_and_records_attempts(self):
        adapter = self.adapter()
        page = self.page([{'status': 503}, {'status': 200}])
        info = await self.navigate(adapter, page)
        self.assertEqual(2, page.goto.await_count)
        self.assertEqual(page.goto.await_args_list[0], page.goto.await_args_list[1])
        self.assertEqual(200, info['httpStatus'])
        self.sleep.assert_awaited_once_with(2.0)
        page.context.clear_cookies.assert_not_awaited()
        adapter._prepare_market_session.assert_not_awaited()
        attempts = self.report(adapter)['searchNavigation'][0]['attempts']
        self.assertEqual([1, 2], [item['attempt'] for item in attempts])
        self.assertEqual([503, 200], [item['httpStatus'] for item in attempts])
        self.assertEqual('retry_scheduled', attempts[0]['disposition'])
        self.assertEqual('response_received', attempts[1]['result'])
        self.assertEqual('catalog_search_attempt_1', self.capture.await_args.kwargs['stage'])

    async def test_persistent_503_stops_at_three_and_preserves_each_failure(self):
        adapter = self.adapter()
        page = self.page([{'status': 503}] * 3)
        with self.assertRaisesRegex(AmazonCatalogIncomplete, 'attempt=3/3'):
            await self.navigate(adapter, page)
        self.assertEqual(3, page.goto.await_count)
        self.assertEqual([2.0, 5.0], [call.args[0] for call in self.sleep.await_args_list])
        self.assertEqual(3, self.capture.await_count)
        self.assertEqual(
            [f'catalog_search_attempt_{attempt}' for attempt in (1, 2, 3)],
            [call.kwargs['stage'] for call in self.capture.await_args_list],
        )
        report = self.report(adapter)
        self.assertEqual(['http_503'] * 3, [item['reason'] for item in report['pages']])
        self.assertEqual('attempts_exhausted', report['searchNavigation'][0]['attempts'][-1]['disposition'])

    async def test_502_and_504_are_the_only_other_retryable_statuses(self):
        adapter = self.adapter()
        page = self.page([{'status': 502}, {'status': 504}, {'status': 200}])
        await self.navigate(adapter, page)
        attempts = self.report(adapter)['searchNavigation'][0]['attempts']
        self.assertEqual([502, 504, 200], [item['httpStatus'] for item in attempts])
        for status in (400, 403, 404, 408, 429, 500, 501, 505):
            with self.subTest(status=status):
                page = self.page([{'status': status}])
                with self.assertRaisesRegex(AmazonCatalogIncomplete, f'http_{status}'):
                    await self.navigate(self.adapter(), page)
                page.goto.assert_awaited_once()
                page.evaluate.assert_not_awaited()

    async def test_503_challenge_and_continue_interstitial_never_retry(self):
        for flag in ('captcha', 'robotCheck', 'accessChallengeTarget',
                     'visibleChallengeControls', 'continueShopping'):
            with self.subTest(flag=flag):
                adapter = self.adapter()
                page = self.page([{'status': 503, 'state': {flag: True}}])
                with self.assertRaises(AmazonCatalogIncomplete):
                    await self.navigate(adapter, page)
                page.goto.assert_awaited_once()
                page.get_by_role.assert_not_called()
                reason = self.report(adapter)['searchNavigation'][0]['attempts'][0]['reason']
                self.assertEqual('continue_shopping_interstitial' if flag == 'continueShopping'
                                 else 'access_challenge', reason)
        self.sleep.assert_not_awaited()

    async def test_error_page_identity_or_delivery_mismatch_never_retry(self):
        for state in (
            {'currentUrl': 'https://www.amazon.it/s?k=tcl+televisor&page=2'},
            {'currentUrl': AMAZON_ES.base_url + '/s?k=tcl+televisor&page=1'},
            {'currentUrl': AMAZON_ES.base_url + '/s?k=sony+televisor&page=2'},
            {'currentUrl': AMAZON_ES.base_url + '/ap/signin'},
            {'deliveryText': 'Madrid 28001'},
        ):
            with self.subTest(state=state):
                page = self.page([{'status': 503, 'state': state}])
                with self.assertRaises(AmazonCatalogIncomplete):
                    await self.navigate(self.adapter(), page)
                page.goto.assert_awaited_once()
        self.sleep.assert_not_awaited()

    async def test_unreadable_or_empty_503_state_fails_closed(self):
        for step, reason in (
            ({'state_error': RuntimeError('detached page')}, 'search_error_state_unreadable'),
            ({'state': {'bodyTextPresent': False}}, 'search_error_state_unverified'),
        ):
            with self.subTest(step=step):
                page = self.page([{'status': 503, **step}])
                with self.assertRaisesRegex(AmazonCatalogIncomplete, reason):
                    await self.navigate(self.adapter(), page)
                page.goto.assert_awaited_once()
        self.sleep.assert_not_awaited()

    async def test_state_inspection_timeout_is_bounded_and_does_not_retry(self):
        page = self.page([{'status': 503, 'state_hang': True}])
        adapter = self.adapter()
        with patch('catalog_scrape.adapters.amazon._SEARCH_NAVIGATION_BUDGET_SECONDS', 0.02):
            with self.assertRaisesRegex(AmazonCatalogIncomplete, 'search_error_state_unreadable'):
                await asyncio.wait_for(self.navigate(adapter, page), timeout=1)
        page.goto.assert_awaited_once()
        self.sleep.assert_not_awaited()
        attempt = self.report(adapter)['searchNavigation'][0]['attempts'][0]
        self.assertEqual('TimeoutError', attempt['errorType'])

    async def test_total_budget_includes_evidence_and_stops_before_another_navigation(self):
        clock = SimpleNamespace(now=100.0)
        adapter = self.adapter()
        page = self.page([{'status': 503}])

        async def slow_capture(*args, **kwargs):
            clock.now += 89

        self.capture.side_effect = slow_capture
        with patch('catalog_scrape.adapters.amazon.time', SimpleNamespace(monotonic=lambda: clock.now)):
            with self.assertRaises(AmazonCatalogIncomplete):
                await self.navigate(adapter, page)
        page.goto.assert_awaited_once()
        self.sleep.assert_not_awaited()
        self.assertEqual('budget_exhausted', self.report(adapter)['searchNavigation'][0]['attempts'][0]['disposition'])

    async def test_retry_navigation_timeout_is_capped_by_remaining_budget(self):
        clock = SimpleNamespace(now=100.0)
        adapter = self.adapter()
        page = self.page([{'status': 503}, {'status': 200}])

        async def consume_budget(*args, **kwargs):
            clock.now += 49

        self.capture.side_effect = consume_budget
        with patch('catalog_scrape.adapters.amazon.time', SimpleNamespace(monotonic=lambda: clock.now)):
            await self.navigate(adapter, page)
        self.assertEqual([45000, 41000], [call.kwargs['timeout'] for call in page.goto.await_args_list])

    async def test_navigation_exception_is_not_retried_or_reclassified(self):
        error = TimeoutError('original navigation timeout')
        adapter = self.adapter()
        page = self.page([error])
        with self.assertRaises(TimeoutError) as caught:
            await self.navigate(adapter, page)
        self.assertIs(error, caught.exception)
        page.goto.assert_awaited_once()
        self.sleep.assert_not_awaited()
        self.assertEqual('navigation_failed', self.report(adapter)['searchNavigation'][0]['attempts'][0]['result'])

    async def test_recovered_200_still_extracts_only_after_original_delivery_checks(self):
        adapter = self.adapter()
        page = self.page([{'status': 503}, {'status': 200}])
        items = await adapter.fetch_catalog(page)
        self.assertEqual(1, len(items))
        self.assertEqual(399.0, items[0].price_local)
        self.accept_cookie.assert_awaited_once_with(page)
        adapter._prepare_market_session.assert_awaited_once_with(page)
        self.assertEqual([_JS_SEARCH_STATE, _JS_SEARCH_STATE, _JS_EXTRACT],
                         [call.args[0] for call in page.evaluate.await_args_list])

    async def test_recovered_200_wrong_delivery_still_fails_without_extracting(self):
        page = self.page([{'status': 503}, {'status': 200, 'state': {'deliveryText': 'Madrid 28001'}}])
        recovery = AsyncMock(side_effect=AmazonCatalogIncomplete('配送地址未恢复'))
        with patch('catalog_scrape.adapters.amazon.ensure_amazon_page_delivery', recovery):
            with self.assertRaisesRegex(AmazonCatalogIncomplete, '配送地址未恢复'):
                await self.adapter().fetch_catalog(page)
        recovery.assert_awaited_once()
        self.assertNotIn(_JS_EXTRACT, [call.args[0] for call in page.evaluate.await_args_list])

    async def test_recovered_200_wrong_identity_or_challenge_still_fails(self):
        for state in ({'currentUrl': AMAZON_ES.base_url + '/'}, {'captcha': True}):
            with self.subTest(state=state):
                page = self.page([{'status': 503}, {'status': 200, 'state': state}])
                with self.assertRaises(AmazonCatalogIncomplete):
                    await self.adapter().fetch_catalog(page)
                self.assertNotIn(_JS_EXTRACT, [call.args[0] for call in page.evaluate.await_args_list])

    async def test_recovered_200_keeps_original_currency_guard(self):
        page = self.page([{'status': 503}, {'status': 200, 'rows': self.rows(price='£399.00')}])
        items = await self.adapter().fetch_catalog(page)
        self.assertEqual(1, len(items))
        self.assertIsNone(items[0].price_local)
        self.assertIsNone(items[0].price_eur)

    async def test_recovered_200_preserves_evidence_and_captures_final_catalog_rejection(self):
        rejected_accessory = self.rows()
        rejected_accessory[0]['title'] = 'TCL 55 pulgadas TV wall mount'
        for rows, final_reason in ((self.rows(), 'incomplete_catalog'),
                                   (rejected_accessory, 'empty_catalog')):
            with self.subTest(final_reason=final_reason):
                evidence_dir = self.root / final_reason
                evidence_dir.mkdir()

                async def save_evidence(page, **kwargs):
                    path = evidence_dir / f"{kwargs['reason']}.json"
                    path.write_text(json.dumps({'stage': kwargs['stage'], 'reason': kwargs['reason']}),
                                    encoding='utf-8')
                    return path

                self.capture.reset_mock()
                self.capture.side_effect = save_evidence
                adapter = self.adapter()
                page = self.page([{'status': 503}, {'status': 200, 'rows': rows}])
                browser = AsyncMock()
                browser.new_context.return_value.new_page.return_value = page
                with patch('catalog_scrape.run_weekly._catalog_dir', return_value=self.root / 'catalog'):
                    result = await run_one_adapter(browser, adapter)
                self.assertIsNone(result.path)
                self.assertFalse((self.root / 'catalog').exists())
                self.assertEqual(['http_503', final_reason],
                                 [call.kwargs['reason'] for call in self.capture.await_args_list])
                self.assertTrue((evidence_dir / 'http_503.json').is_file())
                final_path = evidence_dir / f'{final_reason}.json'
                self.assertEqual('catalog_validation', json.loads(final_path.read_text())['stage'])
                self.assertEqual(final_path, adapter._failure_evidence_path)
                self.assertEqual(final_path, page._catalog_failure_evidence_path)
                report = self.report(adapter)
                self.assertEqual('rejected', report['status'])
                self.assertEqual('http_503', report['pages'][0]['reason'])
                self.assertEqual([503, 200], [attempt['httpStatus']
                                 for attempt in report['searchNavigation'][0]['attempts']])

    async def test_recovered_200_restores_existing_independent_evidence_state(self):
        adapter = self.adapter()
        page = self.page([{'status': 503}, {'status': 200}])
        previous_path = self.root / 'previous_failure.json'
        previous_path.write_text('{}', encoding='utf-8')
        transient_path = self.root / 'temporary_503.json'
        transient_path.write_text('{}', encoding='utf-8')
        adapter._failure_evidence_captured = True
        adapter._failure_evidence_path = previous_path
        page._catalog_failure_evidence_path = previous_path
        page._amazon_failure_reason = 'previous_independent_failure'
        self.capture.return_value = transient_path
        await self.navigate(adapter, page)
        self.assertTrue(adapter._failure_evidence_captured)
        self.assertEqual(previous_path, adapter._failure_evidence_path)
        self.assertEqual(previous_path, page._catalog_failure_evidence_path)
        self.assertEqual('previous_independent_failure', page._amazon_failure_reason)
        self.assertTrue(previous_path.is_file())
        self.assertTrue(transient_path.is_file())

    async def test_recovered_200_restores_absent_evidence_attributes(self):
        adapter = self.adapter()
        page = self.page([{'status': 503}, {'status': 200}])
        self.capture.return_value = self.root / 'temporary_503.json'
        await self.navigate(adapter, page)
        for owner, keys in (
            (adapter, ('_failure_evidence_captured', '_failure_evidence_path')),
            (page, ('_catalog_failure_evidence_path', '_amazon_failure_reason')),
        ):
            for key in keys:
                self.assertNotIn(key, vars(owner))

    async def test_exhaustion_after_100_candidates_never_publishes_partial_catalog(self):
        adapter = self.adapter()
        page = self.page([{'rows': self.rows(100)}, *[{'status': 503}] * 3])
        browser = AsyncMock()
        browser.new_context.return_value.new_page.return_value = page
        with patch('catalog_scrape.adapters.amazon.MAX_PAGES', 2), \
                patch('catalog_scrape.run_weekly._catalog_dir', return_value=self.root / 'catalog'):
            result = await run_one_adapter(browser, adapter)
        self.assertIsNone(result.path)
        self.assertFalse((self.root / 'catalog').exists())
        report = self.report(adapter)
        self.assertEqual(100, report['candidateCount'])
        self.assertEqual('failed', report['status'])
        self.assertEqual(4, page.goto.await_count)

    async def test_optional_query_http_exhaustion_still_fails_the_catalog(self):
        for kind in ('year', 'series'):
            with self.subTest(kind=kind):
                adapter = self.adapter()
                adapter._series_rescue_queries = lambda items: ['tcl c6k'] if kind == 'series' else []
                page = self.page([{'status': 200}, *[{'status': 503}] * 3])
                with patch('catalog_scrape.adapters.amazon.TARGET_YEARS', ('2026',) if kind == 'year' else ()):
                    with self.assertRaisesRegex(AmazonCatalogIncomplete, 'attempt=3/3'):
                        await adapter.fetch_catalog(page)
                self.assertEqual(4, page.goto.await_count)

    async def test_navigation_exception_preserves_brand_failure_and_optional_query_semantics(self):
        page = self.page([TimeoutError('navigation timeout')])
        with self.assertRaisesRegex(AmazonCatalogIncomplete, '主品牌.*导航失败'):
            await self.adapter().fetch_catalog(page)
        for kind in ('year', 'series'):
            with self.subTest(kind=kind):
                adapter = self.adapter()
                adapter._series_rescue_queries = lambda items: ['tcl c6k'] if kind == 'series' else []
                page = self.page([{'status': 200}, TimeoutError('navigation timeout')])
                with patch('catalog_scrape.adapters.amazon.TARGET_YEARS', ('2026',) if kind == 'year' else ()):
                    items = await adapter.fetch_catalog(page)
                self.assertEqual(1, len(items))
                self.assertEqual(2, page.goto.await_count)


if __name__ == '__main__':
    unittest.main()
