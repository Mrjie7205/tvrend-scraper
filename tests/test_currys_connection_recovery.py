"""真实Chromium合成页面回归：强制离线，验证实际JS/主文档导航，不模拟检测flags。"""
import asyncio
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import urlsplit

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from monitor_prices.adapters.currys import CurrysAdapter, CurrysPdpGuard, wait_currys_automatic_check
from monitor_prices.prices_io import FrozenPriceHistory
from monitor_prices import run_daily
from catalog_scrape.adapters import currys as catalog_module

PRODUCT = 'https://www.currys.co.uk/products/lg-tv-10284267.html'
CATALOG = 'https://www.currys.co.uk/tv-and-audio/televisions/tvs?start=50&sz=50'
NORMAL_PRODUCT = '''<title>LG television</title><meta property="product:price:amount" content="100.00">
<meta property="product:price:currency" content="GBP"><h1>LG television</h1>
<label><input type="checkbox">Compare this television</label><label><input type="checkbox">Add soundbar bundle</label>'''
NORMAL_CATALOG = '''<title>TVs | Currys</title><h1>TVs</h1><label><input type="checkbox">Brand filter</label>
<div class="product-item-element"><a href="/products/lg-tv-10284267.html">LG 55 television</a>
<span class="price"><span class="sales"><span class="value">£100.00</span></span></span></div>'''


def automatic(script='', extra=''):
    return ("<title>Just a moment...</title><h1>Bear with us!</h1>"
            "<h2>We're just checking the security of your connection before letting you onto our site.</h2>"
            "<p>Please give us up to 5 seconds to complete this check</p>" + extra + script)


async def make_fixture(playwright, mode, *, kind='product'):
    from playwright.async_api import Error
    try:
        browser = await playwright.chromium.launch(headless=True)
    except Error as exc:
        if "Executable doesn't exist" in str(exc):
            pytest.skip('离线真实DOM回归需要预装Chromium；此环境尚未安装')
        raise
    context = await browser.new_context(offline=True, service_workers='block')
    page = await context.new_page()
    url = PRODUCT if kind == 'product' else CATALOG
    normal = NORMAL_PRODUCT if kind == 'product' else NORMAL_CATALOG
    traffic = {'documents': 0, 'subresources': 0, 'external_allowed': 0}

    async def route_handler(route):
        request = route.request
        parsed = urlsplit(request.url)
        if parsed.hostname != 'www.currys.co.uk':
            await route.abort()
            return
        if request.resource_type != 'document':
            traffic['subresources'] += 1
            if parsed.path == '/delay.js':
                await asyncio.sleep(0.3)
            try:
                await route.fulfill(status=200, content_type='text/javascript', body='/* offline fixture */')
            except Error:
                pass  # 原文档已被自然导航替换。
            return
        traffic['documents'] += 1
        number = traffic['documents']
        if mode in {'normal', 'normal_captcha', 'normal_turnstile'} or (number > 1 and mode in {'automatic', 'delayed_dom', 'retry', 'wrong_target'}):
            status, body = 200, normal
            if mode == 'normal_captcha':
                body += '<label>Entry<input name="captcha_code" type="text"></label>'
            if mode == 'normal_turnstile':
                body += '<div class="cf-turnstile" style="width:200px;height:50px">Entry</div>'
        elif mode == 'hard_block':
            status, body = 403, '<title>Attention Required!</title><h1>Sorry, you have been blocked</h1>'
        elif mode == 'manual':
            status, body = 403, automatic(extra='<label><input type="checkbox">Verify you are human</label>')
        elif mode == 'connection_checkbox':
            status, body = 403, automatic(extra='<label><input type="checkbox">Continue</label>')
        elif mode == 'rate_limited':
            status, body = 429, '<title>Too many requests</title>'
        else:
            script = ''
            if mode == 'automatic':
                script = '<script>setTimeout(()=>location.reload(),80)</script>'
            elif mode == 'delayed_dom':
                script = '<script>setTimeout(()=>location.reload(),80)</script><script src="/delay.js"></script>'
            elif mode == 'wrong_target':
                destination = '/products/another-tv-10289999.html' if kind == 'product' else '/tv-and-audio/televisions/tvs?start=0&sz=50'
                script = f'<script>setTimeout(()=>location.replace({destination!r}),80)</script>'
            elif mode == 'dom_only':
                script = '<script>setTimeout(()=>{document.title="LG television";document.body.innerHTML="<h1>LG TV £100</h1>"},50)</script>'
            elif mode == 'subresource_only':
                script = '<script>fetch("/resource").then(()=>{document.title="LG television";document.body.innerHTML="<h1>LG TV £100</h1>"})</script>'
            status, body = 403, automatic(script)
        await route.fulfill(status=status, content_type='text/html; charset=utf-8', body=body)
    await context.route('**/*', route_handler)
    return browser, context, page, url, traffic


@pytest.mark.parametrize('kind,mode,verified,documents', [
    ('product', 'automatic', True, 2), ('product', 'delayed_dom', True, 2),
    ('product', 'dom_only', False, 1), ('product', 'subresource_only', False, 1),
    ('product', 'wrong_target', False, 2), ('product', 'manual', False, 1),
    ('product', 'hard_block', False, 1), ('product', 'rate_limited', False, 1),
    ('product', 'normal', True, 1), ('product', 'connection_checkbox', False, 1),
    ('product', 'normal_captcha', False, 1), ('product', 'normal_turnstile', False, 1),
    ('catalog', 'automatic', True, 2), ('catalog', 'wrong_target', False, 2),
    ('catalog', 'normal', True, 1), ('catalog', 'connection_checkbox', False, 1),
])
def test_real_dom_requires_new_main_200_and_exact_target(tmp_path, monkeypatch, kind, mode, verified, documents):
    from playwright.async_api import async_playwright
    monkeypatch.setenv('FAILURE_EVIDENCE_DIR', str(tmp_path / 'failures'))
    async def exercise():
        async with async_playwright() as playwright:
            browser, context, page, url, traffic = await make_fixture(playwright, mode, kind=kind)
            try:
                response = await page.goto(url, wait_until='commit')
                report = await wait_currys_automatic_check(page, requested_url=url, initial_status=response.status,
                                                          expected_kind=kind, timeout_seconds=0.6, remaining_navigations=1)
                assert report['target_verified'] is verified
                assert report['navigation_count'] == documents
                assert traffic['documents'] == documents and traffic['external_allowed'] == 0
                assert await page.evaluate('navigator.onLine') is False
                if verified:
                    assert report['final_status'] == 200
                if mode in {'dom_only', 'subresource_only'}:
                    assert report['final_status'] == 403
                if mode in {'manual', 'hard_block', 'rate_limited'}:
                    assert not report['retry_allowed']
                if mode == 'normal':
                    assert report['human_controls'] is False
                if mode in {'normal_captcha', 'normal_turnstile', 'connection_checkbox'}:
                    assert report['human_controls'] is True and not report['retry_allowed']
                if mode == 'automatic':
                    assert report['automatic_check']
                    assert report['outcome'] == 'recovered'
                    initial = Path(report['initial_evidence_path'])
                    assert initial.is_relative_to(tmp_path / 'failures')
                    detail = json.loads(initial.read_text(encoding='utf-8'))
                    assert detail['stage'] == 'initial_connection_check'
                    assert detail['reason'] == 'automatic_connection_check_pending'
            finally:
                await browser.close()
    asyncio.run(exercise())


@pytest.mark.parametrize('mode,success,documents', [('automatic', True, 2), ('retry', True, 2),
    ('never', False, 2), ('manual', False, 1), ('hard_block', False, 1), ('wrong_target', False, 2)])
def test_real_pdp_recovery_counts_only_final_sku_refusal(tmp_path, monkeypatch, mode, success, documents):
    from playwright.async_api import async_playwright
    monkeypatch.setenv('FAILURE_EVIDENCE_DIR', str(tmp_path / 'failures'))
    monkeypatch.setenv('PRICE_ARTIFACTS_DIR', str(tmp_path / 'prices'))
    monkeypatch.setattr(run_daily.random, 'uniform', lambda *args: 0)
    real_wait = wait_currys_automatic_check
    async def short_wait(*args, **kwargs):
        return await real_wait(*args, **{**kwargs, 'timeout_seconds': 0.4})
    monkeypatch.setattr(run_daily, 'wait_currys_automatic_check', short_wait)
    adapter = CurrysAdapter()
    adapter.wait_selectors, adapter.cookie_accept_selectors = (), ()
    monkeypatch.setattr(run_daily, 'get_adapter', lambda _: adapter)
    async def exercise():
        async with async_playwright() as playwright:
            browser, context, page, url, traffic = await make_fixture(playwright, mode)
            async def existing_context(*args, **kwargs):
                # process_sku使用自己的新page；关闭fixture预建页避免多余浏览器资源。
                return context
            monkeypatch.setattr(run_daily, '_new_context', existing_context)
            try:
                await page.close()
                hist = FrozenPriceHistory({}, {})
                hist.currys_pdp_guard = CurrysPdpGuard()
                for index in range(5):
                    hist.currys_pdp_guard.observe({'product_name': f'prior{index}', 'country': 'GB'}, http_status=403, reason='access_blocked')
                sku = {'brand': 'LG', 'product_name': '55TEST', 'country': 'GB', 'platform': 'Currys', 'url': url}
                result = await run_daily.process_sku(asyncio.Semaphore(1), browser, sku, hist)
                assert (result['Status'] == 'Success') is success
                assert traffic['documents'] == documents
                if success:
                    assert result['Price'] == 100.0
                    assert not hist.currys_pdp_guard.stopped
                    assert hist.currys_pdp_guard.report()['connection_recovery']['restored'] == 1
                    assert hist.currys_pdp_guard.report()['connection_recovery']['entries'][-1]['outcome'] == 'recovered'
                elif mode == 'wrong_target':
                    assert result['Status'] == 'Failed: redirect_unverified'
                    assert not hist.currys_pdp_guard.stopped
                else:
                    assert hist.currys_pdp_guard.stopped
                    assert hist.currys_pdp_guard.report()['outcomes']['access_blocked'] == 6
            finally:
                await browser.close()
    asyncio.run(exercise())


def test_catalog_natural_recovery_consumes_second_navigation_budget(tmp_path, monkeypatch):
    from playwright.async_api import async_playwright
    monkeypatch.setenv('FAILURE_EVIDENCE_DIR', str(tmp_path / 'failures'))
    adapter = catalog_module.CurrysCatalogAdapter()
    real_wait = wait_currys_automatic_check
    async def short_wait(*args, **kwargs):
        return await real_wait(*args, **{**kwargs, 'timeout_seconds': 0.6})
    monkeypatch.setattr(catalog_module, 'wait_currys_automatic_check', short_wait)
    async def exercise():
        async with async_playwright() as playwright:
            browser, context, page, _, traffic = await make_fixture(playwright, 'automatic', kind='catalog')
            try:
                await page.close()
                adapter._catalog_context = context
                status, cards = await adapter._scrape_page(browser, 50, navigation_budget=2)
                assert status == 200 and len(cards) == 1
                assert traffic['documents'] == 2
                assert adapter._last_page_info['initial_http_status'] == 403
                assert adapter._last_page_info['http_status'] == 200
                assert adapter._last_page_info['navigation_count'] == 2
                assert not adapter._last_page_info['retryable']
            finally:
                await browser.close()
    asyncio.run(exercise())


def test_catalog_final_budget_keeps_initial_evidence_before_close(tmp_path, monkeypatch):
    from playwright.async_api import async_playwright
    monkeypatch.setenv('FAILURE_EVIDENCE_DIR', str(tmp_path / 'failures'))
    adapter = catalog_module.CurrysCatalogAdapter()
    async def exercise():
        async with async_playwright() as playwright:
            browser, context, page, _, traffic = await make_fixture(playwright, 'never', kind='catalog')
            try:
                await page.close()
                adapter._catalog_context = context
                status, cards = await adapter._scrape_page(browser, 50, navigation_budget=1)
                assert status == 403 and cards == []
                assert traffic['documents'] == 1
                assert adapter._last_page_info['navigation_count'] == 1
                assert adapter._last_page_info['connection_recovery']['page_closed_for_budget']
                recovery = adapter._last_page_info['connection_recovery']
                assert recovery['outcome'] == 'unresolved'
                initial = Path(recovery['initial_evidence_path'])
                assert initial.is_relative_to(tmp_path / 'failures')
                document = json.loads(initial.read_text(encoding='utf-8'))
                assert document['stage'] == 'initial_connection_check'
                assert document['screenshot']['status'] == 'saved'
                assert (initial.parent / document['screenshot']['file']).exists()
            finally:
                await browser.close()
    asyncio.run(exercise())


@pytest.mark.parametrize('observer_available,reason,error_type', [
    (True, 'dom_read_error', 'ValueError'),
    (False, 'navigation_observer_error', 'TypeError'),
])
def test_catalog_200_read_failure_keeps_http_status_without_claiming_challenge(monkeypatch, observer_available, reason, error_type):
    adapter = catalog_module.CurrysCatalogAdapter()
    page = SimpleNamespace(
        url=CATALOG, on=MagicMock() if observer_available else AsyncMock(),
        goto=AsyncMock(return_value=SimpleNamespace(status=200)),
        wait_for_load_state=AsyncMock(), evaluate=AsyncMock(side_effect=ValueError('broken product DOM')),
        close=AsyncMock(),
    )
    context = SimpleNamespace(new_page=AsyncMock(return_value=page), close=AsyncMock())
    monkeypatch.setattr(adapter, '_new_context', AsyncMock(return_value=context))
    capture = AsyncMock()
    monkeypatch.setattr(catalog_module, 'capture_catalog_failure', capture)
    status, rows = asyncio.run(adapter._scrape_page(object(), 50))
    assert status == 0 and rows == []
    info = adapter._last_page_info
    assert info['http_status'] == 200 and not info['blocked']
    assert info['reason'] == reason and info['error_type'] == error_type
    assert info['connection_recovery']['outcome'] == 'unresolved'
    assert capture.await_args.kwargs['http_status'] == 200
    assert capture.await_args.kwargs['reason'] == reason
    context.close.assert_awaited_once()
