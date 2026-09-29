"""Amazon catalog 的纯函数回归测试，不访问网络。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from catalog_scrape.adapters.amazon import (  # noqa: E402
    AMAZON_GB,
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
                page.evaluate.return_value = ''
                with patch('catalog_scrape.adapters.amazon._accept_cookie', new=AsyncMock()), patch(
                    'catalog_scrape.adapters.amazon.set_amazon_location_via_popup',
                    new=AsyncMock(return_value=False),
                ):
                    self.assertFalse(await set_amazon_market_location(page, market))

    async def test_api_updated_true_is_not_enough_without_delivery_header(self) -> None:
        page = AsyncMock()
        page.evaluate.side_effect = [
            'data-toaster-csrfToken="token-value"',
            {'status': 200, 'updated': True},
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
        verify.assert_awaited_once_with(page, AMAZON_ES, refresh=True)

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
        popup.assert_awaited_once_with(page, AMAZON_GB)


if __name__ == "__main__":
    unittest.main()
