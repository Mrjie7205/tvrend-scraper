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
    AMAZON_DE, AMAZON_GB, AMAZON_IT, AMAZON_ES, AmazonCatalogAdapter, AmazonCatalogIncomplete,
    _JS_SEARCH_STATE, _JS_CONTINUE_PAGE_INSPECTION, _JS_DETAIL,
    _checked_page_state, _plain_continue_entry_control, _continue_inspection_summary,
    set_amazon_market_location, set_amazon_location_via_popup,
)


def inspection(market):
    labels = {'DE': 'Weiter shoppen', 'GB': 'Continue shopping', 'IT': 'Continua con gli acquisti', 'ES': 'Seguir comprando'}
    return {
        'current': {'origin': market.base_url, 'path': '/'},
        'normalPage': False, 'visibleChallengeControls': False, 'challengeLanguage': False,
        'continueShoppingInstruction': True, 'formCount': 1,
        'visibleInputCount': 0, 'visibleFrameCount': 0,
        'controls': [{
            'tag': 'button', 'type': 'submit', 'label': labels[market.code],
            'href': None, 'formMethod': 'get',
            'formAction': {'origin': market.base_url, 'path': '/errors_page/validateCaptcha'},
            'nativeSubmit': True, 'nativeIndex': 0, 'formIndex': 0, 'surfaceIndex': 0,
            'kind': 'native_submit', 'disabled': False, 'namedSubmit': False,
            'labelSource': 'visible-text',
        }],
    }


def entry_state(market):
    return {'currentUrl': market.base_url + '/', 'normalPage': False,
            'continueShopping': True, 'accessChallengeTarget': True}


def normal_state(market, **kwargs):
    return {'currentUrl': market.base_url + '/', 'normalPage': True, **kwargs}


class ContinueEntryShapeTests(unittest.TestCase):
    def test_four_observed_market_buttons_are_allowlisted(self):
        for market in (AMAZON_DE, AMAZON_GB, AMAZON_IT, AMAZON_ES):
            self.assertIsNotNone(_plain_continue_entry_control(inspection(market), market))

    def test_unknown_or_human_verification_shapes_are_rejected(self):
        base = inspection(AMAZON_GB)
        changes = [
            {'visibleChallengeControls': True}, {'challengeLanguage': True},
            {'visibleInputCount': 1}, {'visibleFrameCount': 1},
            {'formCount': 2}, {'continueShoppingInstruction': False}, {'normalPage': True},
            {'current': {'origin': 'https://www.amazon.it', 'path': '/'}},
            {'controls': [base['controls'][0], base['controls'][0] | {
                'nativeIndex': 1, 'surfaceIndex': 1, 'namedSubmit': True,
            }]},
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

    def test_same_submit_aui_wrappers_are_one_operation(self):
        candidate = inspection(AMAZON_IT)
        native = candidate['controls'][0] | {'surfaceIndex': 2, 'hasAriaLabelledBy': True, 'labelSource': 'aria-labelledby'}
        wrapper = native | {'tag': 'span', 'type': '', 'surfaceIndex': 1, 'kind': 'aui_wrapper'}
        candidate['controls'] = [wrapper, native]
        selected = _plain_continue_entry_control(candidate, AMAZON_IT)
        self.assertEqual(2, selected['surfaceIndex'])
        self.assertEqual('native_submit', selected['kind'])
        summary = _continue_inspection_summary(candidate, AMAZON_IT)
        self.assertTrue(summary['unique_allowed_operation'])
        self.assertTrue(summary['duplicate_representations'])
        self.assertTrue(summary['same_form'])

    def test_same_form_anonymous_duplicate_submits_are_equivalent(self):
        candidate = inspection(AMAZON_GB)
        candidate['controls'].append(candidate['controls'][0] | {'nativeIndex': 1, 'surfaceIndex': 1})
        self.assertIsNotNone(_plain_continue_entry_control(candidate, AMAZON_GB))
        candidate['controls'][1]['formIndex'] = 1
        self.assertIsNone(_plain_continue_entry_control(candidate, AMAZON_GB))

    def test_rejection_summary_keeps_no_unknown_label_url_or_reference_value(self):
        candidate = inspection(AMAZON_IT)
        candidate['controls'][0].update(label='private-person@example.test', hasAriaLabelledBy=True, formIndex=0)
        candidate['controls'][0]['formAction'] = {'origin': 'https://unknown.test', 'path': '/secret-token'}
        summary = _continue_inspection_summary(candidate, AMAZON_IT)
        self.assertEqual('label_reference_unresolved', summary['rejection_reason'])
        raw = json.dumps(summary)
        self.assertNotIn('private-person', raw)
        self.assertNotIn('unknown.test', raw)
        self.assertNotIn('secret-token', raw)
        self.assertTrue(summary['controls'][0]['has_aria_labelledby'])


class ContinueEntryNavigationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch('catalog_scrape.diagnostics.capture_failure', new=AsyncMock(return_value=None)))

    def page(self, market, states, inspected=None, detail=None):
        page = AsyncMock()
        page.url = market.base_url + '/'
        states = list(states)
        inspected = inspected if inspected is not None else inspection(market)
        async def evaluate(script, *args):
            if script == _JS_SEARCH_STATE:
                return states.pop(0)
            if script == _JS_CONTINUE_PAGE_INSPECTION:
                return inspected.pop(0) if isinstance(inspected, list) else inspected
            if script == _JS_DETAIL:
                return detail if detail is not None else {'price': '£199.00' if market.code == 'GB' else '199,00 €'}
            raise AssertionError('不允许读取隐藏字段或直接提交表单 token')
        page.evaluate.side_effect = evaluate
        button = MagicMock()
        button.click = AsyncMock()
        button.and_.return_value = button
        page.get_by_role = Mock(return_value=button)
        page.locator = Mock()
        future = asyncio.get_running_loop().create_future()
        future.set_result(SimpleNamespace(status=200))
        navigation = MagicMock()
        navigation.__aenter__ = AsyncMock(return_value=SimpleNamespace(value=future))
        navigation.__aexit__ = AsyncMock(return_value=False)
        page.expect_navigation = Mock(return_value=navigation)
        return page, button

    async def test_one_visible_click_returns_verified_normal_market_page(self):
        for market in (AMAZON_DE, AMAZON_GB, AMAZON_IT, AMAZON_ES):
            page, button = self.page(market, [entry_state(market), normal_state(market)])
            result = await _checked_page_state(page, market, 200, stage='location_home')
            self.assertTrue(result['normalPage'])
            button.click.assert_awaited_once()
            page.get_by_role.assert_called_once_with(
                'button', name=inspection(market)['controls'][0]['label'], exact=True,
            )
            page.goto.assert_not_awaited()
            page.context.clear_cookies.assert_not_awaited()
            page.locator.return_value.nth.assert_called_once_with(0)

    async def test_home_and_one_target_product_have_separate_single_use_budgets(self):
        asin = 'B0GXZQL9W2'
        url = AMAZON_DE.base_url + '/dp/' + asin
        inspected_product = inspection(AMAZON_DE)
        inspected_product['current']['path'] = '/dp/' + asin
        entry = entry_state(AMAZON_DE) | {'currentUrl': url}
        normal = normal_state(AMAZON_DE, currentUrl=url, productAsin=asin, deliveryText='26935')
        page, button = self.page(AMAZON_DE, [entry_state(AMAZON_DE), normal_state(AMAZON_DE),
                                           entry, normal, entry],
                                 [inspection(AMAZON_DE), inspected_product, inspected_product])
        await _checked_page_state(page, AMAZON_DE, 200)
        await _checked_page_state(page, AMAZON_DE, 200, target_url=url, asin=asin)
        summary = vars(page)['_amazon_continue_navigation_summary']
        self.assertEqual((2, 1, 1), (summary['attempts'], summary['home_attempts'], summary['product_attempts']))
        self.assertTrue(summary['verified_product_identity'])
        self.assertTrue(summary['verified_delivery'])
        self.assertTrue(summary['verified_currency'])
        with self.assertRaisesRegex(AmazonCatalogIncomplete, '重复'):
            await _checked_page_state(page, AMAZON_DE, 200, target_url=url, asin=asin)
        self.assertEqual(2, button.click.await_count)

    async def test_second_distinct_product_entry_cannot_spend_another_budget(self):
        first, second = 'B0GXZQL9W2', 'B000000002'
        urls = [AMAZON_DE.base_url + '/dp/' + asin for asin in (first, second)]
        inspections = []
        for asin in (first, second):
            inspected = inspection(AMAZON_DE)
            inspected['current']['path'] = '/dp/' + asin
            inspections.append(inspected)
        page, button = self.page(AMAZON_DE, [entry_state(AMAZON_DE) | {'currentUrl': urls[0]},
            normal_state(AMAZON_DE, currentUrl=urls[0], productAsin=first, deliveryText='26935'),
            entry_state(AMAZON_DE) | {'currentUrl': urls[1]}], inspections)
        await _checked_page_state(page, AMAZON_DE, 200, target_url=urls[0], asin=first)
        with self.assertRaisesRegex(AmazonCatalogIncomplete, '重复'):
            await _checked_page_state(page, AMAZON_DE, 200, target_url=urls[1], asin=second)
        button.click.assert_awaited_once()

    async def test_product_return_requires_asin_postcode_and_original_currency(self):
        asin = 'B0GXZQL9W2'
        url = AMAZON_DE.base_url + '/dp/' + asin
        inspected = inspection(AMAZON_DE)
        inspected['current']['path'] = '/dp/' + asin
        good = normal_state(AMAZON_DE, currentUrl=url, productAsin=asin, deliveryText='26935')
        for after, price in ((good | {'productAsin': 'B000000002'}, '199,00 €'),
                             (good | {'productAsin': ''}, '199,00 €'),
                             (good | {'currentUrl': AMAZON_DE.base_url + '/'}, '199,00 €'),
                             (good | {'deliveryText': '10115'}, '199,00 €'),
                             (good, '£199.00'),
                             (entry_state(AMAZON_DE) | {'currentUrl': url}, '199,00 €')):
            with self.subTest(after=after, price=price):
                page, button = self.page(AMAZON_DE, [entry_state(AMAZON_DE) | {'currentUrl': url}] + [after] * 8,
                                         inspected, detail={'price': price})
                with self.assertRaises(AmazonCatalogIncomplete):
                    await _checked_page_state(page, AMAZON_DE, 200, target_url=url, asin=asin)
                button.click.assert_awaited_once()

    async def test_product_scope_keeps_form_and_human_control_restrictions(self):
        asin = 'B0GXZQL9W2'
        url = AMAZON_DE.base_url + '/dp/' + asin
        base = inspection(AMAZON_DE)
        base['current']['path'] = '/dp/' + asin
        for mutate in ('wrong_asin', 'search_path', 'post', 'cross_action', 'captcha'):
            candidate = deepcopy(base)
            if mutate == 'wrong_asin':
                candidate['current']['path'] = '/dp/B000000002'
            elif mutate == 'search_path':
                candidate['current']['path'] = '/s'
            elif mutate == 'post':
                candidate['controls'][0]['formMethod'] = 'post'
            elif mutate == 'cross_action':
                candidate['controls'][0]['formAction']['origin'] = AMAZON_IT.base_url
            else:
                candidate['visibleChallengeControls'] = True
            page, button = self.page(AMAZON_DE, [entry_state(AMAZON_DE) | {'currentUrl': url}], candidate)
            with self.assertRaises(AmazonCatalogIncomplete):
                await _checked_page_state(page, AMAZON_DE, 200, target_url=url, asin=asin)
            button.click.assert_not_awaited()

    async def test_variant_and_optional_detail_reach_checked_product_continue(self):
        asin = 'B0GXZQL9W2'
        url = AMAZON_DE.base_url + '/dp/' + asin
        inspected = inspection(AMAZON_DE)
        inspected['current']['path'] = '/dp/' + asin
        normal = normal_state(AMAZON_DE, currentUrl=url, productAsin=asin, deliveryText='26935')
        for kind in ('variant', 'detail'):
            with self.subTest(kind=kind):
                page, button = self.page(AMAZON_DE,
                    [entry_state(AMAZON_DE) | {'currentUrl': url}, normal, normal], inspected,
                    detail={'title': 'Sony 55 Zoll TV', 'price': '199,00 €', 'variantRefs': []})
                page.goto.return_value = SimpleNamespace(status=200)
                adapter = AmazonCatalogAdapter(AMAZON_DE)
                adapter._record_price_observation = AsyncMock()
                seed = adapter._build_item(asin, 'Sony 55 Zoll TV', 'Sony', 55, '199,00 €')
                if kind == 'variant':
                    self.assertEqual(0, await adapter._expand_variants_from_seed(page, seed, {asin: seed}))
                else:
                    self.assertEqual(199, (await adapter._detail_item(page, asin, 'Sony')).price_local)
                button.click.assert_awaited_once()
                self.assertEqual('product', adapter.continue_navigation_summary['scope'])
                self.assertTrue(adapter.continue_navigation_summary['verified_currency'])

    async def test_pure_entry_waits_once_for_accessible_label_to_settle(self):
        initial = inspection(AMAZON_IT)
        initial['controls'][0].update(label='', hasAriaLabelledBy=True, labelSource='unresolved-reference')
        ready = inspection(AMAZON_IT)
        ready['controls'][0].update(hasAriaLabelledBy=True, labelSource='aria-labelledby')
        page, button = self.page(
            AMAZON_IT, [entry_state(AMAZON_IT), normal_state(AMAZON_IT)], [initial, ready],
        )
        self.assertTrue((await _checked_page_state(page, AMAZON_IT, 200))['normalPage'])
        page.wait_for_timeout.assert_awaited_once_with(1200)
        button.click.assert_awaited_once()
        summary = vars(page)['_amazon_continue_navigation_summary']['inspection']
        self.assertEqual(2, summary['checks'])
        self.assertTrue(summary['waited_for_structure'])
        self.assertEqual('label_reference_unresolved', summary['initial_rejection_reason'])

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
        page.wait_for_timeout.assert_not_awaited()

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
        self.enterContext(patch('catalog_scrape.adapters.amazon._complete_amazon_location_popup', new=AsyncMock(return_value=True)))

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
        verify.assert_awaited_once_with(page, AMAZON_IT, after_popup=True)

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
