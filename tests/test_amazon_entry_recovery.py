"""Amazon 继续入口只允许明确单按钮导航一次；真实验证和错误配送仍拒绝。"""
from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from catalog_scrape.adapters.amazon import (
    AMAZON_DE, AMAZON_GB, AMAZON_IT, AmazonCatalogAdapter, AmazonCatalogIncomplete,
    _JS_SEARCH_STATE, _JS_CONTINUE_PAGE_INSPECTION,
    _checked_page_state, _plain_continue_entry_control,
    set_amazon_market_location, set_amazon_location_via_popup,
)


def inspection(market):
    labels = {'DE': 'Weiter shoppen', 'GB': 'Continue shopping', 'IT': 'Continua con gli acquisti'}
    return {
        'current': {'origin': market.base_url, 'path': '/'},
        'normalPage': False, 'visibleChallengeControls': False, 'challengeLanguage': False,
        'continueShoppingInstruction': True, 'formCount': 1,
        'visibleInputCount': 0, 'visibleFrameCount': 0,
        'controls': [{
            'tag': 'button', 'type': 'submit', 'label': labels[market.code],
            'href': None, 'formMethod': 'get',
            'formAction': {'origin': market.base_url, 'path': '/errors_page/validateCaptcha'},
        }],
    }


def entry_state(market):
    return {'currentUrl': market.base_url + '/', 'normalPage': False,
            'continueShopping': True, 'accessChallengeTarget': True}


def normal_state(market, **kwargs):
    return {'currentUrl': market.base_url + '/', 'normalPage': True, **kwargs}


class ContinueEntryShapeTests(unittest.TestCase):
    def test_three_observed_market_buttons_are_allowlisted(self):
        for market in (AMAZON_DE, AMAZON_GB, AMAZON_IT):
            self.assertIsNotNone(_plain_continue_entry_control(inspection(market), market))

    def test_unknown_or_human_verification_shapes_are_rejected(self):
        base = inspection(AMAZON_GB)
        changes = [
            {'visibleChallengeControls': True}, {'challengeLanguage': True},
            {'visibleInputCount': 1}, {'visibleFrameCount': 1},
            {'formCount': 2}, {'continueShoppingInstruction': False}, {'normalPage': True},
            {'current': {'origin': 'https://www.amazon.it', 'path': '/'}},
            {'controls': base['controls'] * 2},
        ]
        for change in changes:
            with self.subTest(change=change):
                self.assertIsNone(_plain_continue_entry_control({**deepcopy(base), **change}, AMAZON_GB))
        for change in [
            {'label': 'Verify I am human'}, {'formMethod': 'post'},
            {'formAction': {'origin': 'https://outside.invalid', 'path': '/errors_page/validateCaptcha'}},
            {'formAction': {'origin': AMAZON_GB.base_url, 'path': '/ap/signin'}},
            {'href': {'origin': AMAZON_GB.base_url, 'path': '/'}},
        ]:
            candidate = deepcopy(base)
            candidate['controls'][0].update(change)
            self.assertIsNone(_plain_continue_entry_control(candidate, AMAZON_GB))


class ContinueEntryNavigationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch('catalog_scrape.diagnostics.capture_failure', new=AsyncMock(return_value=None)))

    def page(self, market, states, inspected=None):
        page = AsyncMock()
        page.url = market.base_url + '/'
        states = list(states)
        inspected = inspected if inspected is not None else inspection(market)
        async def evaluate(script, *args):
            if script == _JS_SEARCH_STATE:
                return states.pop(0)
            if script == _JS_CONTINUE_PAGE_INSPECTION:
                return inspected
            raise AssertionError('不允许读取隐藏字段或直接提交表单 token')
        page.evaluate.side_effect = evaluate
        button = SimpleNamespace(click=AsyncMock())
        page.get_by_role = Mock(return_value=button)
        future = asyncio.get_running_loop().create_future()
        future.set_result(SimpleNamespace(status=200))
        navigation = MagicMock()
        navigation.__aenter__ = AsyncMock(return_value=SimpleNamespace(value=future))
        navigation.__aexit__ = AsyncMock(return_value=False)
        page.expect_navigation = Mock(return_value=navigation)
        return page, button

    async def test_one_visible_click_returns_verified_normal_market_page(self):
        for market in (AMAZON_DE, AMAZON_GB, AMAZON_IT):
            page, button = self.page(market, [entry_state(market), normal_state(market)])
            result = await _checked_page_state(page, market, 200, stage='location_home')
            self.assertTrue(result['normalPage'])
            button.click.assert_awaited_once()
            page.get_by_role.assert_called_once_with(
                'button', name=inspection(market)['controls'][0]['label'], exact=True,
            )
            page.goto.assert_not_awaited()
            page.context.clear_cookies.assert_not_awaited()

    async def test_real_captcha_stops_without_inspection_or_click(self):
        page, button = self.page(AMAZON_GB, [entry_state(AMAZON_GB) | {'captcha': True}])
        with self.assertRaises(AmazonCatalogIncomplete):
            await _checked_page_state(page, AMAZON_GB, 200)
        page.evaluate.assert_awaited_once_with(_JS_SEARCH_STATE)
        button.click.assert_not_awaited()

    async def test_unrecognized_entry_is_never_clicked(self):
        page, button = self.page(
            AMAZON_GB, [entry_state(AMAZON_GB)], inspection(AMAZON_GB) | {'visibleInputCount': 1},
        )
        with self.assertRaises(AmazonCatalogIncomplete):
            await _checked_page_state(page, AMAZON_GB, 200)
        button.click.assert_not_awaited()

    async def test_entry_without_visible_action_control_is_not_clicked(self):
        # opacity:0/collapse 的按钮会被只读 DOM 检查剔除，不能再盲点同名控件。
        page, button = self.page(
            AMAZON_GB, [entry_state(AMAZON_GB)], inspection(AMAZON_GB) | {'controls': []},
        )
        with self.assertRaises(AmazonCatalogIncomplete):
            await _checked_page_state(page, AMAZON_GB, 200)
        page.get_by_role.assert_not_called()
        button.click.assert_not_awaited()

    async def test_entry_loop_is_rejected_after_one_click(self):
        page, button = self.page(AMAZON_GB, [entry_state(AMAZON_GB), entry_state(AMAZON_GB)])
        with self.assertRaises(AmazonCatalogIncomplete):
            await _checked_page_state(page, AMAZON_GB, 200)
        button.click.assert_awaited_once()

    async def test_later_second_entry_does_not_get_second_click(self):
        page, button = self.page(AMAZON_GB, [
            entry_state(AMAZON_GB), normal_state(AMAZON_GB), entry_state(AMAZON_GB),
        ])
        await _checked_page_state(page, AMAZON_GB, 200)
        with self.assertRaisesRegex(AmazonCatalogIncomplete, '重复'):
            await _checked_page_state(page, AMAZON_GB, 200)
        button.click.assert_awaited_once()

    async def test_cross_market_or_captcha_after_click_is_rejected(self):
        for after in (normal_state(AMAZON_IT), normal_state(AMAZON_GB, captcha=True)):
            page, button = self.page(AMAZON_GB, [entry_state(AMAZON_GB), after])
            with self.assertRaises(AmazonCatalogIncomplete):
                await _checked_page_state(page, AMAZON_GB, 200)
            button.click.assert_awaited_once()

    async def test_button_timeout_does_not_retry_or_replace_original_failure(self):
        page, button = self.page(AMAZON_GB, [entry_state(AMAZON_GB)])
        original = TimeoutError('button navigation timeout')
        button.click.side_effect = original
        with self.assertRaises(AmazonCatalogIncomplete) as caught:
            await _checked_page_state(page, AMAZON_GB, 200)
        self.assertIs(original, caught.exception.__cause__)
        button.click.assert_awaited_once()

    async def test_error_http_never_enters_visible_button_navigation(self):
        page, button = self.page(AMAZON_GB, [entry_state(AMAZON_GB)])
        with self.assertRaises(AmazonCatalogIncomplete):
            await _checked_page_state(page, AMAZON_GB, 503)
        button.click.assert_not_awaited()

    async def test_session_records_safe_success_and_failure_summary(self):
        for after, expected in (
            (normal_state(AMAZON_GB), 'normal_page_restored'),
            (entry_state(AMAZON_GB), 'rejected'),
        ):
            page, _ = self.page(AMAZON_GB, [entry_state(AMAZON_GB), after])
            adapter = AmazonCatalogAdapter(AMAZON_GB)
            adapter.diagnostics = SimpleNamespace(report={}, _save_report=Mock())
            async def prepare(_):
                await _checked_page_state(page, AMAZON_GB, 200)
                return True
            adapter._prepare_market_session_impl = prepare
            if expected == 'rejected':
                with self.assertRaises(AmazonCatalogIncomplete):
                    await adapter._prepare_market_session(page)
            else:
                self.assertTrue(await adapter._prepare_market_session(page))
            summary = adapter.diagnostics.report['continueNavigation']
            self.assertEqual(1, summary['attempts'])
            self.assertEqual(expected, summary['result'])
            self.assertEqual(expected != 'rejected', summary['verified_normal_page'])
            self.assertNotIn('validateCaptcha', json.dumps(summary))
            self.assertNotIn('http', json.dumps(summary))


class ExistingHomePopupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch('catalog_scrape.diagnostics.capture_failure', new=AsyncMock(return_value=None)))
        self.enterContext(patch('catalog_scrape.adapters.amazon._accept_cookie', new=AsyncMock()))

    def popup_page(self, market):
        page = AsyncMock()
        page.url = market.base_url + '/'
        element = Mock()
        element.first = element
        element.count = AsyncMock(return_value=1)
        element.click = AsyncMock()
        element.fill = AsyncMock()
        page.locator = Mock(return_value=element)
        return page, element

    async def test_normal_it_home_with_missing_token_is_not_reloaded_before_popup(self):
        page, element = self.popup_page(AMAZON_IT)
        page.goto.return_value = SimpleNamespace(status=200)
        page.evaluate.side_effect = [normal_state(AMAZON_IT), '', normal_state(AMAZON_IT)]
        with patch('catalog_scrape.adapters.amazon.verify_amazon_delivery_location',
                   new=AsyncMock(return_value=True)) as verify:
            self.assertTrue(await set_amazon_market_location(page, AMAZON_IT))
        page.goto.assert_awaited_once_with(AMAZON_IT.base_url + '/', wait_until='domcontentloaded', timeout=45000)
        element.fill.assert_awaited_once_with(AMAZON_IT.postcode, timeout=5000)
        verify.assert_awaited_once_with(page, AMAZON_IT, refresh=True)

    async def test_reusing_home_still_rejects_unconfirmed_postcode(self):
        page, _ = self.popup_page(AMAZON_IT)
        page.evaluate.return_value = normal_state(AMAZON_IT)
        with patch('catalog_scrape.adapters.amazon.verify_amazon_delivery_location',
                   new=AsyncMock(return_value=False)):
            self.assertFalse(await set_amazon_location_via_popup(page, AMAZON_IT, reuse_current_page=True))
        page.goto.assert_not_awaited()

    async def test_cross_market_page_cannot_be_reused_for_location_popup(self):
        page, element = self.popup_page(AMAZON_IT)
        page.evaluate.return_value = normal_state(AMAZON_GB)
        with self.assertRaises(AmazonCatalogIncomplete):
            await set_amazon_location_via_popup(page, AMAZON_IT, reuse_current_page=True)
        element.click.assert_not_awaited()
        page.goto.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
