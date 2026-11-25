"""失败候选保存不等于发布完整目录，不访问网络。"""
from __future__ import annotations

import csv
import fnmatch
import json
import re
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from catalog_scrape.adapters.amazon import AmazonCatalogAdapter, AMAZON_ES, _JS_SEARCH_STATE, _JS_EXTRACT
from catalog_scrape.diagnostics import AmazonCatalogDiagnostics
from catalog_scrape.run_weekly import run_one_adapter


class AmazonDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch('catalog_scrape.diagnostics.capture_failure',
                                new=AsyncMock(return_value=None)))

    async def _run_second_page_case(self, *, status=200, captcha=False, robot=False, failure=None):
        """第一页足够跨过100行门槛，第二页失败时仍必须阻止正式发布。"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            adapter = AmazonCatalogAdapter(AMAZON_ES)
            adapter._prepare_market_session = AsyncMock(return_value=True)
            rows = [{'asin': f'B{number:09}', 'brand': 'TCL', 'title': 'TCL 55 pulgadas TV',
                     'price': '399,00 €', 'sponsored': False} for number in range(100)]
            request = 0

            async def navigate(*args, **kwargs):
                nonlocal request
                request += 1
                page.url = args[0]
                if request == 2 and failure == 'navigation':
                    raise TimeoutError('bounded navigation timeout')
                return SimpleNamespace(status=status if request >= 2 else 200)

            async def evaluate(script, *args):
                if script == _JS_SEARCH_STATE:
                    return {'currentUrl': page.url,
                            'bodyTextPresent': True,
                            'deliveryText': 'Madrid 28013', 'captcha': captcha and request == 2,
                            'robotCheck': robot and request == 2, 'nextDisabled': request == 2}
                if script == _JS_EXTRACT:
                    if request == 2 and failure == 'extraction':
                        raise RuntimeError('DOM extraction failed')
                    return rows if request == 1 else []
                raise AssertionError('unexpected evaluate')

            page = AsyncMock()
            page.goto.side_effect = navigate
            page.evaluate.side_effect = evaluate
            browser = AsyncMock()
            browser.new_context.return_value.new_page.return_value = page
            with ExitStack() as stack:
                stack.enter_context(patch.dict('os.environ', {'AMAZON_DIAGNOSTICS_DIR': str(root / 'artifacts')}))
                stack.enter_context(patch('catalog_scrape.run_weekly._catalog_dir', return_value=root / 'catalog'))
                stack.enter_context(patch('catalog_scrape.adapters.amazon.BRAND_QUERIES', ('tcl',)))
                stack.enter_context(patch('catalog_scrape.adapters.amazon.TARGET_YEARS', ()))
                stack.enter_context(patch('catalog_scrape.adapters.amazon.EXTRA_SERIES_QUERIES', ()))
                stack.enter_context(patch('catalog_scrape.adapters.amazon.MAX_PAGES', 2))
                stack.enter_context(patch('catalog_scrape.adapters.amazon.EXPAND_VARIANTS', False))
                stack.enter_context(patch('catalog_scrape.adapters.amazon._accept_cookie', new=AsyncMock()))
                stack.enter_context(patch('catalog_scrape.adapters.amazon.asyncio.sleep', new=AsyncMock()))
                stack.enter_context(patch.object(adapter, '_series_rescue_queries', return_value=[]))
                stack.enter_context(patch.object(adapter, '_load_recent_catalog_items', return_value=[]))
                result = await run_one_adapter(browser, adapter)
            report = json.loads((adapter.diagnostics.path / 'report.json').read_text(encoding='utf-8'))
            has_catalog = bool(list((root / 'catalog').glob('*.csv'))) if (root / 'catalog').exists() else False
            return result, report, has_catalog

    async def test_http_error_rejects_catalog_even_after_enough_good_rows(self):
        result, report, has_catalog = await self._run_second_page_case(status=503)
        self.assertIsNone(result.path)
        self.assertFalse(has_catalog)
        self.assertEqual('http_503', report['pages'][-1]['reason'])
        self.assertEqual(100, report['candidateCount'])

    async def test_challenge_rejects_catalog_despite_valid_header_and_enough_rows(self):
        for flag in ('captcha', 'robot'):
            with self.subTest(flag=flag):
                result, report, has_catalog = await self._run_second_page_case(**{flag: True})
                self.assertIsNone(result.path)
                self.assertFalse(has_catalog)
                self.assertEqual('access_challenge', report['pages'][-1]['reason'])

    async def test_primary_brand_navigation_and_extraction_fail_closed(self):
        for failure in ('navigation', 'extraction'):
            with self.subTest(failure=failure):
                result, report, has_catalog = await self._run_second_page_case(failure=failure)
                self.assertIsNone(result.path)
                self.assertFalse(has_catalog)
                self.assertEqual('failed', report['status'])
                self.assertIn('目录不完整', result.failure_reason)

    async def test_valid_empty_last_page_does_not_invalidate_good_catalog(self):
        result, report, has_catalog = await self._run_second_page_case()
        self.assertIsNotNone(result.path)
        self.assertTrue(has_catalog)
        self.assertEqual('validated', report['status'])

    async def _run_detail_stop(self, stage, *, previous_count=0, brands=('tcl',), search_price='399,00 €',
                               failure='identity'):
        from catalog_scrape.adapters.amazon import _JS_DETAIL

        adapter = AmazonCatalogAdapter(AMAZON_ES)
        adapter._prepare_market_session = AsyncMock(return_value=True)
        history_ids = ['B901000001', 'B901000002', 'B901000003']
        historical = [adapter._build_item(asin, 'TCL 65 pulgadas TV', 'TCL', 65, '1,00 €')
                      for asin in history_ids]
        target = history_ids[1] if stage == 'history_recovery' else 'B000000000'
        rows = [{'asin': f'B{number:09}', 'brand': 'TCL', 'title': 'TCL 55 pulgadas TV',
                 'price': search_price, 'variantHint': number < 2} for number in range(100)]
        page = AsyncMock()
        urls = []

        async def navigate(url, **kwargs):
            urls.append(url)
            status = 429 if failure == 'rate_limit' and url.endswith('/' + target) else 200
            return SimpleNamespace(status=status)

        async def evaluate(script, *args):
            url = urls[-1]
            asin = url.rsplit('/', 1)[-1] if '/dp/' in url else ''
            if script == _JS_SEARCH_STATE:
                state = dict(currentUrl=url, normalPage=True, deliveryText='Madrid 28013', productAsin=asin)
                if asin == target:
                    if failure == 'identity':
                        state['productAsin'] = 'B999999999'
                    elif failure == 'captcha':
                        state['captcha'] = True
                    elif failure == 'delivery':
                        state['deliveryText'] = 'Barcelona 08001'
                return state
            if script == _JS_EXTRACT and not asin:
                return rows
            if script == _JS_DETAIL and asin and asin != target:
                return dict(title='TCL 65 pulgadas TV', price='499,00 €', variantRefs=[])
            raise AssertionError('拒绝的页面不得取价')

        page.goto.side_effect = navigate
        page.evaluate.side_effect = evaluate
        browser = AsyncMock()
        browser.new_context.return_value.new_page.return_value = page
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            root = Path(temporary)
            catalog = root / 'catalog'
            catalog.mkdir()
            if previous_count:
                (catalog / 'amazon_es_20000101.csv').write_text('url\n' + 'old\n' * previous_count)
            stack.enter_context(patch.dict('os.environ', {'AMAZON_DIAGNOSTICS_DIR': str(root / 'artifacts'),
                                'AMAZON_MIN_CATALOG_ROWS': '100', 'AMAZON_MIN_PREVIOUS_RATIO': '0.70'}))
            stack.enter_context(patch('catalog_scrape.run_weekly._catalog_dir', return_value=catalog))
            for name, value in (('BRAND_QUERIES', brands), ('TARGET_YEARS', ()), ('EXTRA_SERIES_QUERIES', ()),
                                ('MAX_PAGES', 1), ('EXPAND_VARIANTS', True)):
                stack.enter_context(patch('catalog_scrape.adapters.amazon.' + name, value))
            stack.enter_context(patch('catalog_scrape.adapters.amazon._accept_cookie', new=AsyncMock()))
            stack.enter_context(patch('catalog_scrape.adapters.amazon.asyncio.sleep', new=AsyncMock()))
            stack.enter_context(patch('catalog_scrape.adapters.amazon.set_amazon_market_location',
                                      new=AsyncMock(return_value=False)))
            stack.enter_context(patch.object(adapter, '_series_rescue_queries', return_value=[]))
            stack.enter_context(patch.object(adapter, '_load_price_baseline', return_value={}))
            stack.enter_context(patch.object(adapter, '_load_recent_catalog_items', return_value=historical))
            stack.enter_context(patch.object(adapter, '_select_previous_recovery_items',
                                             return_value=historical if stage == 'history_recovery' else historical[:1]))
            result = await run_one_adapter(browser, adapter)
            records = []
            if result.path:
                with result.path.open(encoding='utf-8-sig', newline='') as handle:
                    records = list(csv.DictReader(handle))
            report = json.loads((adapter.diagnostics.path / 'report.json').read_text(encoding='utf-8'))
        self.assertEqual(target, urls[-1].rsplit('/', 1)[-1])
        return records, report

    async def test_interrupted_detail_publishes_only_fresh_verified_rows_and_stops_access(self):
        for stage, failure in [('history_recovery', 'identity'), ('variant_expansion', 'captcha'),
                               ('history_recovery', 'rate_limit')]:
            with self.subTest(stage=stage, failure=failure):
                rows, report = await self._run_detail_stop(stage, failure=failure)
                self.assertEqual(101, len(rows))
                self.assertEqual({'399.0', '499.0'}, {row['price_local'] for row in rows})
                self.assertNotIn('B901000002', {row['asin'] for row in rows})
                self.assertEqual('validated', report['status'])
                stop = report['detailEnrichmentStopped']
                self.assertEqual((stage, 100, [], True),
                                 (stop['stage'], stop['searchCount'], stop['missingBrands'], stop['hasLocalPrice']))

    async def test_interrupted_detail_cannot_use_extra_rows_to_meet_historical_coverage(self):
        rows, report = await self._run_detail_stop('history_recovery', previous_count=145)
        self.assertEqual([], rows)
        self.assertEqual('rejected', report['status'])
        self.assertIn('100 < 101', report['failureReason'])

    async def test_interrupted_detail_requires_all_brands_and_current_local_price(self):
        for kwargs in [dict(brands=('tcl', 'sony')), dict(search_price='')]:
            with self.subTest(kwargs=kwargs):
                rows, report = await self._run_detail_stop('history_recovery', **kwargs)
                self.assertEqual([], rows)
                self.assertIn('搜索目录无法独立验收', report['failureReason'])

    async def test_optional_delivery_reset_failure_still_rejects_market(self):
        rows, report = await self._run_detail_stop('history_recovery', failure='delivery')
        self.assertEqual([], rows)
        self.assertIn('配送地址恢复失败', report['failureReason'])
        self.assertNotIn('detailEnrichmentStopped', report)

    async def test_partial_candidates_survive_rejection_without_formal_catalog(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            adapter = AmazonCatalogAdapter(AMAZON_ES)
            item = adapter._build_item('B000000001', 'TCL 55 pulgadas TV', 'TCL', 55, '399,00 €')
            adapter.diagnostics = AmazonCatalogDiagnostics('ES', root / 'artifacts')
            adapter.diagnostics.checkpoint([item])
            adapter.fetch_catalog = AsyncMock(return_value=[item])
            browser = AsyncMock()
            with patch('catalog_scrape.run_weekly._catalog_dir', return_value=root / 'catalog'):
                result = await run_one_adapter(browser, adapter)
            self.assertIsNone(result.path)
            self.assertFalse((root / 'catalog').exists())
            evidence = adapter.diagnostics.path
            report = json.loads((evidence / 'report.json').read_text(encoding='utf-8'))
            self.assertEqual('rejected', report['status'])
            self.assertFalse(report['eligibleForIngestion'])
            self.assertIn('绝对下限', report['failureReason'])
            with (evidence / 'partial_candidates.csv').open(encoding='utf-8-sig', newline='') as handle:
                candidate = next(csv.DictReader(handle))
            self.assertEqual('399.0', candidate['price_eur'])
            self.assertEqual('unvalidated_diagnostic_candidate', candidate['validation_status'])

    def test_observation_writer_keeps_only_public_fields(self):
        with tempfile.TemporaryDirectory() as temporary:
            evidence = AmazonCatalogDiagnostics('IT', Path(temporary))
            evidence.record_page({'query': 'tcl', 'page': 1}, [{
                'asin': 'B000000001', 'title': 'TCL TV', 'price': '399 EUR',
                'cookies': 'not-allowed', 'headers': 'not-allowed', 'storageState': 'not-allowed',
            }])
            content = (evidence.path / 'search_observations.jsonl').read_text(encoding='utf-8')
            self.assertNotIn('not-allowed', content)
            self.assertNotIn('cookies', content)
            self.assertIn('TCL TV', content)

    def test_diagnostic_artifacts_are_not_downloaded_as_formal_catalogs(self):
        workflow = (Path(__file__).resolve().parents[1] / '.github/workflows/daily-amazon.yml').read_text(encoding='utf-8')
        self.assertIn('name: amazon-diagnostics-', workflow)
        steps = re.split(r'\n(?=      - name:)', workflow)
        downloads = [step for step in steps if 'uses: actions/download-artifact@' in step]
        self.assertEqual(1, len(downloads))
        download = downloads[0]
        pattern = re.search(r'^\s+pattern:\s*(\S+)\s*$', download, re.MULTILINE).group(1)
        for country in ('de', 'gb', 'it', 'es'):
            self.assertTrue(fnmatch.fnmatch(f'amazon-result-{country}', pattern))
            self.assertFalse(fnmatch.fnmatch(f'amazon-diagnostics-{country}', pattern))
        self.assertIn('path: _amazon_download', download)
        self.assertIn('merge-multiple: false', download)
        # 正式目录必须先通过受限 manifest 与 CSV 内容门禁，不能直接下载到 catalog。
        self.assertIn('amazon_artifact_gate.py collect', workflow)
        self.assertIn('--artifacts _amazon_download --catalog catalog', workflow)


if __name__ == '__main__':
    unittest.main()
