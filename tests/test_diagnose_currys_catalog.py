"""分页诊断只读、有限采样和真实离线DOM回归。"""
import asyncio
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import diagnose_currys_catalog as probe
from catalog_scrape.adapters.currys import CurrysCatalogAdapter


@pytest.mark.parametrize('starts', [[], [-1], [100001], [0, 0], [0, 50, 100]])
def test_input_rejects_more_than_two_or_invalid_pages(starts):
    with pytest.raises(ValueError):
        probe.validate_starts(starts)


def test_default_and_two_page_limits():
    assert probe.validate_starts(None) == [400]
    assert probe.validate_starts([400, 0]) == [400, 0]


def test_conflicting_visible_totals_are_not_verifiable_metadata():
    metadata = {'ranges': [], 'total_candidates': [
        {'total': 401, 'source': {'tag': 'p'}}, {'total': 495, 'source': {'tag': 'p'}},
    ], 'controls': []}
    assert probe.assess_metadata(metadata)['metadata_available'] is False
    assert probe.assess_metadata(metadata)['total_conflict'] is True


@pytest.mark.parametrize('case,exit_code', [('tail', 0), ('shown', 0), ('shown_conflict', 2), ('no_metadata', 2), ('blocked', 2)])
def test_real_offline_probe_keeps_safe_evidence_and_truthful_exit(tmp_path, monkeypatch, case, exit_code):
    from playwright.async_api import Error
    traffic = []
    monkeypatch.setenv('SCRAPER_BROWSER_PROFILE', 'current')
    monkeypatch.setenv('FAILURE_EVIDENCE_DIR', str(tmp_path / 'initial'))
    monkeypatch.setenv('GITHUB_RUN_ID', '123456')
    monkeypatch.setenv('GITHUB_RUN_ATTEMPT', '1')
    monkeypatch.setenv('GITHUB_SHA', 'a' * 40)
    body = '''<title>TVs | Currys - Page 9</title><h1>TVs</h1>
        <div class="product-item-element"><a href="/products/lg-tv-10284267.html">LG 55 television</a>
        <span class="price"><span class="value">£100.00</span></span></div>
        <input type="hidden" value="SECRET-HIDDEN-CANARY"><div style="display:none">SECRET-ADDRESS-CANARY</div>'''
    if case in {'tail', 'shown', 'shown_conflict'}:
        body += '''<p id="results-count" class="results-count">Showing 401 - 401 of 401 results</p>
            <nav class="pagination" aria-label="Pagination"><a href="?page=8&amp;token=SECRET-QUERY-CANARY">8</a>
            <span class="current" aria-current="page">9</span><button disabled aria-label="Next page">›</button>
            <a href="?page=9&amp;token=SECRET-QUERY-CANARY">Last page</a>
            <button style="display:none" aria-label="Next page">Hidden</button></nav>'''
        if case in {'shown', 'shown_conflict'}:
            body = body.replace('Showing 401 - 401 of 401 results',
                                ('495 items' if case == 'shown_conflict' else '401 items') + '<br>Showing 401 of 401')
    if case == 'blocked':
        body = '<title>Access denied</title><h1>Sorry, you have been blocked</h1>'

    async def launch(playwright, **kwargs):
        try:
            browser = await playwright.chromium.launch(headless=True)
        except Error as exc:
            if "Executable doesn't exist" in str(exc):
                pytest.skip('此环境未预装离线DOM测试所需Chromium')
            raise
        browser._tvrend_browser_profile = 'current'
        return browser

    async def new_context(self, browser):
        context = await browser.new_context(offline=True, service_workers='block')
        async def route(route):
            assert route.request.url.startswith('https://www.currys.co.uk/')
            if route.request.resource_type == 'document':
                traffic.append(route.request.url)
            await route.fulfill(status=403 if case == 'blocked' else 200, content_type='text/html; charset=utf-8', body=body)
        await context.route('**/*', route)
        return context

    monkeypatch.setattr(probe, 'launch_scraper_browser', launch)
    monkeypatch.setattr(CurrysCatalogAdapter, '_new_context', new_context)
    output = tmp_path / 'evidence'
    assert asyncio.run(probe.run_probe([400], output)) == exit_code
    report = json.loads((output / 'report.json').read_text(encoding='utf-8'))
    assert len(traffic) == 1 and len(report['pages']) == 1
    assert report['publishes_prices'] is False and report['catalog_complete'] is None
    assert report['run_id'] == '123456' and report['head_sha'] == 'a' * 40
    row = report['pages'][0]
    assert row['navigation_count'] == 1
    assert row['screenshot']['status'] == 'saved'
    assert (output / row['screenshot']['file']).is_file()
    content = (output / 'report.json').read_text(encoding='utf-8')
    assert 'SECRET-' not in content
    assert not list(output.rglob('*.csv')) and not list(output.rglob('*.html'))
    if case in {'tail', 'shown'}:
        assert row['assessment']['total'] == 401
        if case == 'tail':
            assert row['assessment']['showing_range'] == [401, 401, 401]
        else:
            assert row['assessment']['shown_count'] == 401
            assert row['assessment']['showing_range'] is None
            assert row['metadata']['shown_counts'][0]['shown'] == row['metadata']['shown_counts'][0]['total'] == 401
        assert row['assessment']['current_page'] == row['assessment']['explicit_last_page'] == 9
        assert row['assessment']['next_disabled'] is True
    else:
        assert report['status'] == 'incomplete'
        assert row['status'] == ('blocked' if case == 'blocked' else 'metadata_unavailable')
        if case == 'shown_conflict':
            assert row['assessment']['total_conflict'] is True


def test_observer_error_does_not_replace_normal_catalog_result(monkeypatch):
    adapter = CurrysCatalogAdapter()
    card = {'slug': 'lg-tv-10284267', 'title': 'LG 55 television', 'price': '£100', 'href': '/products/lg-tv-10284267.html'}
    page = SimpleNamespace(url='https://www.currys.co.uk/tv-and-audio/televisions/tvs?start=0&sz=50',
        on=MagicMock(), goto=AsyncMock(return_value=SimpleNamespace(status=200)), wait_for_load_state=AsyncMock(),
        evaluate=AsyncMock(side_effect=[{'automatic_check': False}, {'challenge': False}, [card]]),
        wait_for_timeout=AsyncMock(), is_visible=AsyncMock(return_value=False))
    context = SimpleNamespace(new_page=AsyncMock(return_value=page), close=AsyncMock())
    monkeypatch.setattr(adapter, '_new_context', AsyncMock(return_value=context))
    adapter.page_observation_callback = AsyncMock(side_effect=RuntimeError('diagnostic failed'))
    assert asyncio.run(adapter._scrape_page(object(), 0)) == (200, [card])
    assert adapter._last_page_info['observation_error_type'] == 'RuntimeError'
    context.close.assert_awaited_once()
