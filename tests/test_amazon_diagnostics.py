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
                if request == 2 and failure == 'navigation':
                    raise TimeoutError('bounded navigation timeout')
                return SimpleNamespace(status=status if request == 2 else 200)

            async def evaluate(script, *args):
                if script == _JS_SEARCH_STATE:
                    return {'deliveryText': 'Madrid 28013', 'captcha': captcha and request == 2,
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
