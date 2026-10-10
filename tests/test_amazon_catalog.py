"""Amazon catalog 的纯函数回归测试，不访问网络。"""

from __future__ import annotations

import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, Mock, patch
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from catalog_scrape.adapters.amazon import (  # noqa: E402
    AMAZON_GB,
    AMAZON_DE,
    AMAZON_IT,
    AMAZON_ES,
    AmazonCatalogAdapter,
    AmazonDeCatalogAdapter,
    is_non_tv_title,
    set_amazon_market_location,
    set_amazon_location_via_popup,
    _delivery_postcode_matches,
    verify_amazon_delivery_location,
    verify_amazon_detail_canary,
    AmazonCatalogIncomplete,
    ensure_amazon_page_delivery,
    _JS_SEARCH_STATE,
    _JS_CONTINUE_PAGE_INSPECTION,
    _JS_DETAIL,
    _JS_EXTRACT,
)


class AmazonCatalogSeriesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = AmazonDeCatalogAdapter()

    def item(self, brand: str, title: str, asin: str = "B000000000"):
        return self.adapter._build_item(asin, title, brand, 55, "")

    def test_extracts_current_series_from_market_skus(self) -> None:
        cases = (
            ("Samsung", "Samsung QE48S95HATXXU OLED TV 2026", "S95H"),
            ("Samsung", "Samsung GQ65QN90FATXZG Neo QLED", "QN90F"),
            ("LG", "LG OLED55C6ELA OLED evo TV", "C6"),
            ("LG", "LG 55QNED85A6A TV", "QNED85A"),
            ("TCL", "TCL 75C8L Premium QD Mini LED", "C8L"),
            ("TCL", "TCL 65C6K PRO Mini LED", "C6KPRO"),
            ("Hisense", "Hisense 65U7Q PRO Mini LED", "U7QPRO"),
            ("Sony", "Sony K-55XR80M2 BRAVIA 8 II", "BRAVIA8II"),
        )
        for brand, title, expected in cases:
            with self.subTest(title=title):
                self.assertEqual(expected, self.adapter._series_hint(self.item(brand, title)))

    def test_variant_seed_selection_keeps_two_fallbacks_per_series(self) -> None:
        seeds = []
        for index, size in enumerate((48, 55, 65, 77)):
            item = self.item("Samsung", f"Samsung QE{size}S95HATXXU OLED TV 2026", f"B00000000{index}")
            item.size_hint_inch = size
            item.extra["variant_hint"] = True
            seeds.append(item)
        selected = self.adapter._select_variant_seeds(seeds)
        self.assertEqual(2, len(selected))

    def test_series_rescue_prioritizes_series_with_fewer_seen_sizes(self) -> None:
        items = [
            self.item("Samsung", "Samsung QE55S95HATXXU OLED TV 2026", "B000000001"),
            self.item("Samsung", "Samsung GQ55QN90FATXZG Neo QLED", "B000000002"),
            self.item("Samsung", "Samsung GQ65QN90FATXZG Neo QLED", "B000000003"),
        ]
        items[0].size_hint_inch = 55
        items[1].size_hint_inch = 55
        items[2].size_hint_inch = 65
        queries = self.adapter._series_rescue_queries(items)
        self.assertEqual("samsung s95h", queries[0])

    def test_history_recovery_only_selects_sizes_missing_today(self) -> None:
        current = [self.item("Samsung", "Samsung QE48S95HATXXU OLED TV 2026", "B000000001")]
        current[0].size_hint_inch = 48
        historical = [
            self.item("Samsung", "Samsung QE48S95HATXXU OLED TV 2026", "B000000001"),
            self.item("Samsung", "Samsung QE55S95HATXXU OLED TV 2026", "B000000002"),
            self.item("Samsung", "Samsung QE65S95HATXXU OLED TV 2026", "B000000003"),
        ]
        for item, size in zip(historical, (48, 55, 65)):
            item.size_hint_inch = size
        selected = self.adapter._select_previous_recovery_items(historical, current)
        self.assertEqual({55, 65}, {int(item.size_hint_inch or 0) for item in selected})

    def test_rejects_remote_and_power_accessories_without_blocking_real_tv(self) -> None:
        rejected = (
            'WKOLF Replace Remote suit for TCL 43" 50/55PF650K,50/60/75 T6C TV',
            'TV Mounting Screws Kit 100pcs - Universal VESA Screws for Samsung TVs',
            '120W Ladegerät für Sony Bravia TV 55W755C',
        )
        for title in rejected:
            with self.subTest(title=title):
                self.assertTrue(is_non_tv_title(title))

        accepted = (
            'LG OLED55C6 Smart TV with AI Magic Remote and Dolby Vision',
            'Samsung 55S95H OLED TV with One Remote Control',
        )
        for title in accepted:
            with self.subTest(title=title):
                self.assertFalse(is_non_tv_title(title))


class AmazonLocationFallbackTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # 本组只验证业务门禁；真实采集及顺序由失败现场专门测试覆盖。
        self.enterContext(patch('catalog_scrape.diagnostics.capture_failure',
                                new=AsyncMock(return_value=None)))
        self.enterContext(patch('catalog_scrape.adapters.amazon._complete_amazon_location_popup', new=AsyncMock(return_value=True)))

    def test_delivery_postcode_requires_real_header_match(self) -> None:
        self.assertTrue(_delivery_postcode_matches('Madrid 28013\u200c', AMAZON_ES))
        self.assertTrue(_delivery_postcode_matches('Milano 20121', AMAZON_IT))
        self.assertTrue(_delivery_postcode_matches('Coventry CV4 7ES', AMAZON_GB))
        self.assertTrue(_delivery_postcode_matches('Coventry CV47ES', AMAZON_GB))
        self.assertTrue(_delivery_postcode_matches('Coventry CV47ES\u200c', AMAZON_GB))
        self.assertTrue(_delivery_postcode_matches('Coventry CV4\u200c7ES', AMAZON_GB))
        self.assertFalse(_delivery_postcode_matches('Coventry CV4 1AA', AMAZON_GB))
        self.assertFalse(_delivery_postcode_matches('Coventry CV4', AMAZON_GB))
        for text in ('Hong Kong', 'Spain', '', 'Madrid 128013', 'Madrid 280130'):
            with self.subTest(text=text):
                self.assertFalse(_delivery_postcode_matches(text, AMAZON_ES))

    async def test_eur_market_does_not_accept_unconfirmed_delivery(self) -> None:
        for market in (AMAZON_IT, AMAZON_ES):
            with self.subTest(country=market.code):
                page = AsyncMock()
                page.evaluate.side_effect = [{}, '']
                with patch('catalog_scrape.adapters.amazon._accept_cookie', new=AsyncMock()), patch(
                    'catalog_scrape.adapters.amazon.set_amazon_location_via_popup',
                    new=AsyncMock(return_value=False),
                ):
                    self.assertFalse(await set_amazon_market_location(page, market))

    async def test_api_updated_true_is_not_enough_without_delivery_header(self) -> None:
        page = AsyncMock()
        page.evaluate.side_effect = [
            {},
            'data-toaster-csrfToken="token-value"',
            {'status': 200, 'updated': True},
            {'deliveryText': 'Hong Kong'},
            {'deliveryText': 'Hong Kong'},
        ]
        with patch('catalog_scrape.adapters.amazon._accept_cookie', new=AsyncMock()), patch(
            'catalog_scrape.adapters.amazon.set_amazon_location_via_popup',
            new=AsyncMock(return_value=False),
        ) as popup:
            self.assertFalse(await set_amazon_market_location(page, AMAZON_IT))
        popup.assert_awaited_once()

    async def test_popup_click_success_does_not_prove_postcode_changed(self) -> None:
        page = AsyncMock()
        page.evaluate.return_value = {}
        element = Mock()
        element.first = element
        element.count = AsyncMock(return_value=1)
        element.click = AsyncMock()
        element.fill = AsyncMock()
        page.locator = Mock(return_value=element)
        with patch('catalog_scrape.adapters.amazon._accept_cookie', new=AsyncMock()), patch(
            'catalog_scrape.adapters.amazon.verify_amazon_delivery_location',
            new=AsyncMock(return_value=False),
        ) as verify:
            self.assertFalse(await set_amazon_location_via_popup(page, AMAZON_ES))
        verify.assert_awaited_once_with(page, AMAZON_ES, after_popup=True)

    async def test_session_does_not_use_canary_as_delivery_substitute(self) -> None:
        adapter = AmazonCatalogAdapter(AMAZON_IT)
        page = AsyncMock()
        with patch('catalog_scrape.adapters.amazon.set_amazon_market_location', new=AsyncMock(return_value=False)), patch(
            'catalog_scrape.adapters.amazon.verify_amazon_detail_canary', new=AsyncMock(return_value=True),
        ) as canary, patch('catalog_scrape.adapters.amazon.asyncio.sleep', new=AsyncMock()):
            self.assertFalse(await adapter._prepare_market_session(page))
        canary.assert_not_awaited()

    async def test_popup_propagates_explicit_challenge(self) -> None:
        page = AsyncMock()
        page.evaluate.return_value = {}
        element = Mock()
        element.first = element
        element.count = AsyncMock(return_value=1)
        element.click = AsyncMock()
        element.fill = AsyncMock()
        page.locator = Mock(return_value=element)
        with patch('catalog_scrape.adapters.amazon._accept_cookie', new=AsyncMock()), patch(
            'catalog_scrape.adapters.amazon.verify_amazon_delivery_location',
            new=AsyncMock(side_effect=AmazonCatalogIncomplete('access_challenge')),
        ):
            with self.assertRaises(AmazonCatalogIncomplete):
                await set_amazon_location_via_popup(page, AMAZON_ES)

    async def test_canary_challenge_stops_before_second_anchor(self) -> None:
        page = AsyncMock()
        page.evaluate.return_value = {'captcha': True, 'deliveryText': 'Madrid 28013'}
        with self.assertRaises(AmazonCatalogIncomplete):
            await verify_amazon_detail_canary(page, AMAZON_ES)
        page.goto.assert_awaited_once()
        self.assertIn(AMAZON_ES.detail_canary[0][0], page.goto.await_args.args[0])

    async def test_session_does_not_retry_after_explicit_challenge(self) -> None:
        adapter = AmazonCatalogAdapter(AMAZON_IT)
        page = AsyncMock()
        with patch('catalog_scrape.adapters.amazon.set_amazon_market_location', new=AsyncMock(return_value=True)) as location, patch(
            'catalog_scrape.adapters.amazon.verify_amazon_detail_canary',
            new=AsyncMock(side_effect=AmazonCatalogIncomplete('access_challenge')),
        ) as canary, patch('catalog_scrape.adapters.amazon.asyncio.sleep', new=AsyncMock()) as sleep:
            with self.assertRaises(AmazonCatalogIncomplete):
                await adapter._prepare_market_session(page)
        location.assert_awaited_once()
        canary.assert_awaited_once()
        sleep.assert_not_awaited()
        page.context.clear_cookies.assert_not_awaited()

    async def test_post_updated_false_uses_visible_popup_fallback(self) -> None:
        page = AsyncMock()
        page.context = AsyncMock()
        page.evaluate.side_effect = [
            {},
            'data-toaster-csrfToken="token-value"',
            {"status": 200, "updated": False},
        ]
        with patch(
            "catalog_scrape.adapters.amazon._accept_cookie",
            new=AsyncMock(),
        ), patch(
            "catalog_scrape.adapters.amazon.set_amazon_location_via_popup",
            new=AsyncMock(return_value=True),
        ) as popup:
            result = await set_amazon_market_location(page, AMAZON_GB)

        self.assertTrue(result)
        popup.assert_awaited_once_with(page, AMAZON_GB, reuse_current_page=True)


class AmazonDeliveryRecoveryTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch('catalog_scrape.diagnostics.capture_failure',
                                new=AsyncMock(return_value=None)))
        self.enterContext(patch('catalog_scrape.adapters.amazon._DELIVERY_OBSERVATION_DELAYS_MS', (0, 1500)))

    """模拟短时配送栏缺失；任何页面/配送/挑战校验失败都不能输出价格。"""

    ASIN = 'B000000001'

    def state(self, delivery='', *, market=AMAZON_IT, **extra):
        return {
            'currentUrl': f'{market.base_url}/dp/{self.ASIN}',
            'productAsin': self.ASIN,
            'normalPage': True,
            'deliveryText': delivery,
            **extra,
        }

    async def test_healthy_market_headers_do_not_reset_session(self):
        for market in (AMAZON_DE, AMAZON_GB, AMAZON_IT, AMAZON_ES):
            with self.subTest(market=market.code):
                page = AsyncMock()
                state = self.state(market.postcode, market=market)
                page.evaluate.return_value = state
                with patch('catalog_scrape.adapters.amazon.set_amazon_market_location', new=AsyncMock()) as reset:
                    result = await ensure_amazon_page_delivery(page, market, state['currentUrl'], asin=self.ASIN)
                self.assertEqual(state, result)
                reset.assert_not_awaited()
                page.goto.assert_not_awaited()
                page.wait_for_timeout.assert_not_awaited()

    async def test_delayed_header_only_waits_without_address_reset(self):
        page = AsyncMock()
        state = self.state('Milano 20121')
        page.evaluate.side_effect = [self.state(), state]
        with patch('catalog_scrape.adapters.amazon.set_amazon_market_location', new=AsyncMock()) as reset:
            self.assertEqual(state, await ensure_amazon_page_delivery(page, AMAZON_IT, state['currentUrl'], asin=self.ASIN))
        reset.assert_not_awaited()
        page.goto.assert_not_awaited()
        page.wait_for_timeout.assert_awaited_once_with(1500)

    async def test_one_reset_reopens_original_asin_and_revalidates(self):
        page = AsyncMock()
        page.goto.return_value = SimpleNamespace(status=200)
        good = self.state('Milano 20121')
        page.evaluate.side_effect = [self.state(), self.state(), good, good]
        with patch('catalog_scrape.adapters.amazon.set_amazon_market_location', new=AsyncMock(return_value=True)) as reset:
            self.assertEqual(good, await ensure_amazon_page_delivery(page, AMAZON_IT, good['currentUrl'], asin=self.ASIN))
        reset.assert_awaited_once_with(page, AMAZON_IT)
        page.goto.assert_awaited_once_with(good['currentUrl'], wait_until='domcontentloaded', timeout=30000)
        page.context.clear_cookies.assert_not_awaited()

    async def test_unconfirmed_restored_header_rejects_without_second_reset(self):
        page = AsyncMock()
        page.goto.return_value = SimpleNamespace(status=200)
        page.evaluate.return_value = self.state('Hong Kong')
        with patch('catalog_scrape.adapters.amazon.set_amazon_market_location', new=AsyncMock(return_value=True)) as reset:
            with self.assertRaisesRegex(AmazonCatalogIncomplete, '恢复后配送地仍未确认'):
                await ensure_amazon_page_delivery(page, AMAZON_IT, self.state()['currentUrl'], asin=self.ASIN)
        reset.assert_awaited_once()
        page.goto.assert_awaited_once()

    async def test_failed_address_reset_rejects_without_reopening_detail(self):
        page = AsyncMock()
        page.evaluate.return_value = self.state()
        with patch('catalog_scrape.adapters.amazon.set_amazon_market_location', new=AsyncMock(return_value=False)) as reset:
            with self.assertRaisesRegex(AmazonCatalogIncomplete, '配送地址恢复失败'):
                await ensure_amazon_page_delivery(page, AMAZON_IT, self.state()['currentUrl'], asin=self.ASIN)
        reset.assert_awaited_once()
        page.goto.assert_not_awaited()

    async def test_challenge_interstitial_and_http_error_never_trigger_recovery(self):
        for state_extra, status in (({'captcha': True}, 200), ({'robotCheck': True}, 200),
                                    ({'continueShopping': True}, 200), ({}, 503)):
            with self.subTest(state_extra=state_extra, status=status):
                page = AsyncMock()
                page.evaluate.return_value = self.state(**state_extra)
                with patch('catalog_scrape.adapters.amazon.set_amazon_market_location', new=AsyncMock()) as reset:
                    with self.assertRaises(AmazonCatalogIncomplete):
                        await ensure_amazon_page_delivery(page, AMAZON_IT, self.state()['currentUrl'], asin=self.ASIN, http_status=status)
                reset.assert_not_awaited()
                page.goto.assert_not_awaited()
                page.wait_for_timeout.assert_not_awaited()

    async def test_challenge_during_wait_is_not_treated_as_lost_postcode(self):
        page = AsyncMock()
        page.evaluate.side_effect = [self.state(), self.state(robotCheck=True)]
        with patch('catalog_scrape.adapters.amazon.set_amazon_market_location', new=AsyncMock()) as reset:
            with self.assertRaises(AmazonCatalogIncomplete):
                await ensure_amazon_page_delivery(page, AMAZON_IT, self.state()['currentUrl'], asin=self.ASIN)
        reset.assert_not_awaited()

    async def test_reopened_error_or_challenge_cannot_pass_on_good_postcode(self):
        for extra, status in (({'captcha': True}, 200), ({'continueShopping': True}, 200), ({}, 429)):
            with self.subTest(extra=extra, status=status):
                page = AsyncMock()
                page.goto.return_value = SimpleNamespace(status=status)
                page.evaluate.side_effect = [self.state(), self.state(), self.state('Milano 20121', **extra)]
                with patch('catalog_scrape.adapters.amazon.set_amazon_market_location', new=AsyncMock(return_value=True)) as reset:
                    with self.assertRaises(AmazonCatalogIncomplete):
                        await ensure_amazon_page_delivery(page, AMAZON_IT, self.state()['currentUrl'], asin=self.ASIN)
                reset.assert_awaited_once()
                page.goto.assert_awaited_once()
                page.wait_for_timeout.assert_awaited_once()

    async def test_recovery_rejects_wrong_country_asin_and_old_dom(self):
        bad_pages = [
            self.state('Milano 20121', currentUrl=f'{AMAZON_DE.base_url}/dp/{self.ASIN}'),
            self.state('Milano 20121', currentUrl=f'{AMAZON_IT.base_url}/dp/B000000002'),
            self.state('Milano 20121', productAsin='B000000002'),
            self.state('Milano 20121', productAsin=''),
        ]
        for bad in bad_pages:
            with self.subTest(bad=bad):
                page = AsyncMock()
                page.goto.return_value = SimpleNamespace(status=200)
                page.evaluate.side_effect = [self.state(), self.state(), bad, bad]
                with patch('catalog_scrape.adapters.amazon.set_amazon_market_location', new=AsyncMock(return_value=True)) as reset:
                    with self.assertRaisesRegex(AmazonCatalogIncomplete, '恢复后页面身份不符'):
                        await ensure_amazon_page_delivery(page, AMAZON_IT, self.state()['currentUrl'], asin=self.ASIN)
                reset.assert_awaited_once()

    async def test_search_recovery_requires_original_query_and_page(self):
        target = f'{AMAZON_IT.base_url}/s?k=tcl+televisore&page=2'
        blank = {'currentUrl': target, 'deliveryText': '', 'normalPage': True}
        wrong = {'currentUrl': target.replace('page=2', 'page=1'), 'deliveryText': 'Milano 20121'}
        page = AsyncMock()
        page.goto.return_value = SimpleNamespace(status=200)
        page.evaluate.side_effect = [blank, blank, wrong, wrong]
        with patch('catalog_scrape.adapters.amazon.set_amazon_market_location', new=AsyncMock(return_value=True)):
            with self.assertRaisesRegex(AmazonCatalogIncomplete, '恢复后页面身份不符'):
                await ensure_amazon_page_delivery(page, AMAZON_IT, target)

    async def test_detail_and_variant_extract_only_after_return_to_original_page(self):
        for kind in ('detail', 'variant'):
            with self.subTest(kind=kind):
                adapter = AmazonCatalogAdapter(AMAZON_IT)
                page = AsyncMock()
                restored = False
                detail_calls = 0

                async def reset(*args):
                    nonlocal restored
                    restored = True
                    return True

                async def evaluate(script, *args):
                    nonlocal detail_calls
                    if script == _JS_SEARCH_STATE:
                        return self.state('Milano 20121' if restored else '')
                    if script == _JS_DETAIL:
                        self.assertTrue(restored, '不能读取配送恢复前页面的价格或尺寸')
                        self.assertEqual(2, page.goto.await_count)
                        detail_calls += 1
                        return {'title': 'TCL 55 pollici TV', 'price': '499,00 €', 'variantRefs': []}
                    raise AssertionError('unexpected evaluate')

                page.goto.return_value = SimpleNamespace(status=200)
                page.evaluate.side_effect = evaluate
                with patch('catalog_scrape.adapters.amazon.set_amazon_market_location', new=AsyncMock(side_effect=reset)):
                    if kind == 'detail':
                        item = await adapter._detail_item(page, self.ASIN, 'TCL')
                        self.assertEqual(499.0, item.price_eur)
                    else:
                        seed = adapter._build_item(self.ASIN, 'TCL 55 pollici TV', 'TCL', 55, '399,00 €')
                        self.assertEqual(0, await adapter._expand_variants_from_seed(page, seed, {}))
                self.assertEqual(1, detail_calls)

    async def test_location_home_challenge_stops_before_popup_or_address_api(self):
        for setter in (set_amazon_market_location, set_amazon_location_via_popup):
            with self.subTest(setter=setter.__name__):
                page = AsyncMock()
                page.goto.return_value = SimpleNamespace(status=200)
                page.evaluate.return_value = self.state(continueShopping=True)
                with patch('catalog_scrape.adapters.amazon._accept_cookie', new=AsyncMock()) as cookie:
                    with self.assertRaises(AmazonCatalogIncomplete):
                        await setter(page, AMAZON_IT)
                cookie.assert_not_awaited()
                self.assertEqual(2, page.evaluate.await_count)
                self.assertEqual(
                    [_JS_SEARCH_STATE, _JS_CONTINUE_PAGE_INSPECTION],
                    [call.args[0] for call in page.evaluate.await_args_list],
                )

    async def test_search_extracts_fresh_cards_only_after_delivery_recovery(self):
        adapter = AmazonCatalogAdapter(AMAZON_IT)
        adapter._prepare_market_session = AsyncMock(return_value=True)
        page = AsyncMock()
        current_url = ''
        restored = False
        extractions = 0

        async def goto(url, **kwargs):
            nonlocal current_url
            current_url = url
            return SimpleNamespace(status=200)

        async def reset(*args):
            nonlocal restored
            restored = True
            return True

        async def evaluate(script, *args):
            nonlocal extractions
            if script == _JS_SEARCH_STATE:
                return {'currentUrl': current_url, 'normalPage': True, 'deliveryText': 'Milano 20121' if restored else ''}
            if script == _JS_EXTRACT:
                self.assertTrue(restored, '不能保存恢复前搜索页报价')
                self.assertEqual(2, page.goto.await_count)
                extractions += 1
                return [{'asin': self.ASIN, 'title': 'TCL 55 pollici TV', 'price': '499,00 €'}]
            raise AssertionError('unexpected evaluate')

        page.goto.side_effect = goto
        page.evaluate.side_effect = evaluate
        with TemporaryDirectory() as temporary, ExitStack() as stack:
            stack.enter_context(patch.dict('os.environ', {'AMAZON_DIAGNOSTICS_DIR': temporary}))
            stack.enter_context(patch('catalog_scrape.adapters.amazon.BRAND_QUERIES', ('tcl',)))
            stack.enter_context(patch('catalog_scrape.adapters.amazon.TARGET_YEARS', ()))
            stack.enter_context(patch('catalog_scrape.adapters.amazon.EXTRA_SERIES_QUERIES', ()))
            stack.enter_context(patch('catalog_scrape.adapters.amazon.MAX_PAGES', 1))
            stack.enter_context(patch('catalog_scrape.adapters.amazon.EXPAND_VARIANTS', False))
            stack.enter_context(patch('catalog_scrape.adapters.amazon._accept_cookie', new=AsyncMock()))
            stack.enter_context(patch('catalog_scrape.adapters.amazon.asyncio.sleep', new=AsyncMock()))
            stack.enter_context(patch('catalog_scrape.adapters.amazon.set_amazon_market_location', new=AsyncMock(side_effect=reset)))
            stack.enter_context(patch.object(adapter, '_series_rescue_queries', return_value=[]))
            stack.enter_context(patch.object(adapter, '_load_recent_catalog_items', return_value=[]))
            items = await adapter.fetch_catalog(page)
        self.assertEqual(1, extractions)
        self.assertEqual([499.0], [item.price_eur for item in items])

    async def test_optional_detail_404_remains_no_quote_without_address_recovery(self):
        adapter = AmazonCatalogAdapter(AMAZON_IT)
        page = AsyncMock()
        page.goto.return_value = SimpleNamespace(status=404)
        page.evaluate.return_value = self.state()
        with patch('catalog_scrape.adapters.amazon.set_amazon_market_location', new=AsyncMock()) as reset:
            self.assertIsNone(await adapter._detail_item(page, self.ASIN, 'TCL'))
        reset.assert_not_awaited()
        page.evaluate.assert_awaited_once_with(_JS_SEARCH_STATE)

    async def test_address_api_http_error_stops_without_popup_retry(self):
        page = AsyncMock()
        page.goto.return_value = SimpleNamespace(status=200)
        page.evaluate.side_effect = [{}, 'data-toaster-csrfToken="test"', {'status': 503, 'updated': False}]
        with patch('catalog_scrape.adapters.amazon._accept_cookie', new=AsyncMock()), patch(
            'catalog_scrape.adapters.amazon.set_amazon_location_via_popup', new=AsyncMock(),
        ) as popup:
            with self.assertRaisesRegex(AmazonCatalogIncomplete, 'http_503'):
                await set_amazon_market_location(page, AMAZON_IT)
        popup.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
