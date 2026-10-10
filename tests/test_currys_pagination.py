"""严格末页证明：可信总数、真实pager页序、连续完整观测缺一不可。"""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sys
from urllib.parse import parse_qs, urlparse

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from catalog_scrape import currys_pagination as pagination
from catalog_scrape.adapters import currys as catalog


def metadata(start=4, size=2, total=5):
    page = start // size + 1
    controls = [{'number': page, 'direction': None, 'current': True, 'disabled': False, 'link_numbers': {}}]
    if start:
        controls.append({'number': None, 'direction': 'previous', 'current': False, 'disabled': False,
                         'link_numbers': {'start': start-size, 'sz': size, 'page': None, 'same_endpoint': True, 'bare_endpoint': False}})
    return {'ranges': [], 'shown_counts': [], 'total_candidates': [
        {'total': total, 'label': 'items', 'authoritative': True, 'source': {'tag': 'div', 'classes': ['page-result-count']}}],
        'controls': controls, 'pagination_containers': [{'tag': 'ul', 'classes': ['pagination']}]}


def position(start=4, size=2, total=5):
    return pagination.verify_page_position(start, size, {'actual': {'start': None, 'sz': None, 'page': None},
        'title_page': start // size + 1}, metadata(start,size,total), document_verified=True)


def complete_fixture():
    pages = {str(start): {'status': 200, 'missing': False, 'pagination_evidence': position(start)} for start in (0,2,4)}
    cards = {str(index): {'href': f'/products/lg-tv-{10280000+index}.html'} for index in range(5)}
    return pages, cards


def test_empty_query_is_verified_by_current_page_and_previous_link_without_shown():
    result = position()
    assert result['verified'] and result['previous_confirmed'] and not result['shown_present']


@pytest.mark.parametrize('fault', ['current', 'previous', 'origin', 'title', 'query', 'document', 'total', 'shown'])
def test_wrong_or_missing_page_evidence_is_rejected(fault):
    value = metadata()
    page = {'actual': {'start': None, 'sz': None, 'page': None}, 'title_page': 3}
    document = True
    if fault == 'current': value['controls'][0]['number'] = 2
    if fault == 'previous': value['controls'][1]['link_numbers']['start'] = 0
    if fault == 'origin': value['controls'][1]['link_numbers']['same_endpoint'] = False
    if fault == 'title': page['title_page'] = 2
    if fault == 'query': page['actual']['start'] = 2
    if fault == 'document': document = False
    if fault == 'total': value['total_candidates'][0]['authoritative'] = False
    if fault == 'shown': value['shown_counts'] = [{'shown': 4, 'total': 5}]
    assert not pagination.verify_page_position(4, 2, page, value, document_verified=document)['verified']


def test_exact_total_and_contiguous_verified_pages_prove_last_window():
    pages, cards = complete_fixture()
    result = pagination.completion_proof(pages, cards, current_start=4, size=2)
    assert result['complete'] and result['trusted_total'] == result['unique_product_ids'] == 5


@pytest.mark.parametrize('fault', ['gap', 'drift', 'short', 'extra', 'duplicate_id', 'position', 'no_metadata', 'early_window'])
def test_partial_or_ambiguous_catalog_never_becomes_complete(fault):
    pages, cards = complete_fixture()
    last = 4
    if fault == 'gap': del pages['2']
    if fault == 'drift': pages['2']['pagination_evidence']['trusted_total'] = 6
    if fault == 'short': cards.pop('4')
    if fault == 'extra': cards['5'] = {'href': '/products/lg-tv-10280005.html'}
    if fault == 'duplicate_id': cards['4'] = {'href': '/products/alternate-name-10280003.html'}
    if fault == 'position': pages['2']['pagination_evidence']['verified'] = False
    if fault == 'no_metadata': pages['4']['pagination_evidence'] = {}
    if fault == 'early_window': last = 2
    assert not pagination.completion_proof(pages, cards, current_start=last, size=2)['complete']


def test_real_offline_catalog_stops_at_proven_last_page_without_requesting_overflow(tmp_path, monkeypatch):
    from playwright.async_api import Error, async_playwright
    monkeypatch.setenv('SCRAPER_BROWSER_PROFILE', 'current')
    monkeypatch.setenv('CURRYS_CATALOG_DIAGNOSTICS_DIR', str(tmp_path / 'ledger'))
    monkeypatch.setenv('FAILURE_EVIDENCE_DIR', str(tmp_path / 'failures'))
    monkeypatch.setattr(catalog, 'PAGE_SIZE', 2)
    starts = []
    async def new_context(self, browser):
        context = await browser.new_context(offline=True, service_workers='block')
        async def route(route):
            assert route.request.url.startswith('https://www.currys.co.uk/')
            start = int(parse_qs(urlparse(route.request.url).query).get('start', ['0'])[0])
            starts.append(start)
            page = start // 2 + 1
            body = f'<title>TVs | Currys - Page {page}</title><h1>TVs</h1><div class="result-count">2 products per page</div><div class="page-result-count">5 items</div>'
            body += '<ul class="pagination">' + f'<li class="current-page" aria-current="page">{page}</li>'
            if start:
                previous = '/tv-and-audio/televisions/tvs' if start == 2 else f'?start={start-2}&sz=2'
                body += f'<li><a aria-label="Previous page" href="{previous}">‹</a></li>'
            body += '</ul>'
            for index in range(start, min(start+2, 5)):
                body += f'<div class="product-item-element"><a href="/products/lg-55-tv-{10280000+index}.html">LG 55 television {index}</a><span class="price"><span class="value">£500</span></span></div>'
            body += '<script>history.replaceState({},"",location.pathname)</script>'
            await route.fulfill(status=200, content_type='text/html; charset=utf-8', body=body)
        await context.route('**/*', route)
        return context
    monkeypatch.setattr(catalog.CurrysCatalogAdapter, '_new_context', new_context)
    async def exercise():
        async with async_playwright() as playwright:
            try:
                browser = await playwright.chromium.launch(headless=True)
            except Error as exc:
                if "Executable doesn't exist" in str(exc): pytest.skip('未预装离线Chromium')
                raise
            try:
                adapter = catalog.CurrysCatalogAdapter()
                items = await adapter.fetch_catalog_from_browser(browser)
                assert len(items) == 5 and starts == [0,2,4]
                assert adapter.catalog_report['complete'] and adapter.catalog_report['end_observed']
                assert adapter.catalog_report['termination'] == 'verified_last_window'
                assert adapter.catalog_report['pagination_unverified_pages'] == []
                assert adapter.catalog_report['completion_proof']['unique_product_ids'] == 5
                assert adapter.catalog_report['pages']['4']['attempts'][0]['pagination_metadata']['per_page_counts']
                (tmp_path / 'synthetic-completion.json').write_text(json.dumps({'synthetic': True, 'external_allowed': 0,
                    'requested_starts': starts, 'report': adapter.catalog_report}, ensure_ascii=False, indent=2), encoding='utf-8')
            finally:
                await browser.close()
    asyncio.run(exercise())
