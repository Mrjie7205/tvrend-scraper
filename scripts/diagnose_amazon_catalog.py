"""有限采样 Amazon 搜索页，用于诊断；永不写入正式 catalog 或价格表。"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from catalog_scrape.adapters.amazon import (
    AMAZON_DE, AMAZON_ES, AMAZON_GB, AMAZON_IT,
    AmazonCatalogAdapter, _JS_EXTRACT, inspect_amazon_continue_page,
)
from catalog_scrape.run_weekly import BROWSER_ARGS
from monitor_prices.core import STEALTH_JS, USER_AGENTS
from urllib.parse import quote_plus


MARKETS = {market.code: market for market in (AMAZON_DE, AMAZON_GB, AMAZON_IT, AMAZON_ES)}
PAGE_STATE = r"""() => {
  const text = document.body?.innerText || '';
  const next = document.querySelector('a.s-pagination-next');
  return {
    title: document.title,
    cardCount: document.querySelectorAll("div[data-component-type='s-search-result']").length,
    nextPresent: !!next,
    nextDisabled: !!document.querySelector('.s-pagination-next.s-pagination-disabled'),
    nextHref: next ? next.getAttribute('href') : '',
    deliveryText: (document.querySelector('#glow-ingress-line2')?.textContent
      || document.querySelector('#glow-ingress-block')?.textContent || '').trim().replace(/\s+/g, ' '),
    captcha: /captcha|enter the characters you see below|api-services-support@amazon.com/i.test(text),
    robotCheck: /robot check|not a robot|automated access|unusual traffic/i.test(text)
  };
}"""
CARD_TEXT = r"""() => Object.fromEntries(Array.from(document.querySelectorAll(
  "div[data-component-type='s-search-result']")).map(el => [
    el.getAttribute('data-asin'), (el.textContent || '').trim().replace(/\s+/g, ' ')
  ]))"""
SANITIZED_DOM = r"""() => {
  const clone = document.documentElement.cloneNode(true);
  clone.querySelectorAll('script, input[type=hidden], meta[name*=csrf]').forEach(el => el.remove());
  clone.querySelectorAll('*').forEach(el => {
    Array.from(el.attributes).forEach(attr => {
      if (/token|csrf|nonce|cookie|authorization/i.test(attr.name)) el.removeAttribute(attr.name);
    });
    if (el.tagName === 'INPUT') el.removeAttribute('value');
  });
  return '<!doctype html>\n' + clone.outerHTML;
}"""


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--country', choices=tuple(MARKETS), required=True)
    parser.add_argument('--query', choices=('samsung', 'lg', 'tcl', 'hisense', 'sony'), required=True)
    parser.add_argument('--pages', default='1,2')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r'[1-3](?:,[1-3]){0,2}', args.pages):
        parser.error('--pages 仅允许 1 至 3 页、至多三个不重复页码')
    args.pages = [int(value) for value in args.pages.split(',')]
    if len(set(args.pages)) != len(args.pages):
        parser.error('--pages 不允许重复页码')
    args.output = args.output.resolve()
    formal = Path(__file__).resolve().parents[1] / 'catalog'
    if args.output == formal or formal in args.output.parents:
        parser.error('诊断输出不能写入正式 catalog 目录')
    return args


async def run(args: argparse.Namespace) -> int:
    from playwright.async_api import async_playwright
    market = MARKETS[args.country]
    adapter = AmazonCatalogAdapter(market)
    args.output.mkdir(parents=True, exist_ok=True)
    summary = {'country': market.code, 'query': args.query,
               'observedAt': datetime.now(UTC).isoformat(), 'diagnosticOnly': True, 'pages': []}
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(headless=True, channel='chrome', args=list(BROWSER_ARGS))
        except Exception:
            browser = await p.chromium.launch(headless=True, args=list(BROWSER_ARGS))
        context = await browser.new_context(
            locale=market.locale, timezone_id=market.timezone,
            viewport={'width': 1366, 'height': 900}, user_agent=random.choice(USER_AGENTS),
        )
        await context.add_init_script(STEALTH_JS)
        page = await context.new_page()
        try:
            summary['sessionPrepared'] = await adapter._prepare_market_session(page)
            if not summary['sessionPrepared']:
                summary['sessionFailureState'] = await page.evaluate(PAGE_STATE)
                summary['continuePageInspection'] = await inspect_amazon_continue_page(page)
                await page.screenshot(path=str(args.output / 'session-failed.png'))
                (args.output / 'session-failed.html').write_text(await page.evaluate(SANITIZED_DOM), encoding='utf-8')
                return 1
            for number in args.pages:
                url = f'{market.base_url}/s?k={quote_plus(args.query + " " + market.search_word)}&page={number}'
                record = {'page': number, 'requestedUrl': url}
                prefix = args.output / f'{market.code.lower()}-{args.query}-p{number}'
                try:
                    response = await page.goto(url, wait_until='domcontentloaded', timeout=45000)
                    await page.wait_for_timeout(3000)
                    record.update({'httpStatus': response.status if response else None,
                                   'finalUrl': page.url, 'state': await page.evaluate(PAGE_STATE)})
                    rows = await page.evaluate(_JS_EXTRACT)
                    texts = await page.evaluate(CARD_TEXT)
                    filtered = Counter({'sponsored': 0, 'no_brand': 0, 'no_size': 0,
                                        'non_tv': 0, 'duplicate': 0})
                    accepted = 0
                    seen = set()
                    for row in rows:
                        row['cardText'] = texts.get(row.get('asin'), '')
                        before = filtered.copy()
                        if row.get('sponsored'):
                            filtered['sponsored'] += 1
                        elif row.get('asin') in seen:
                            filtered['duplicate'] += 1
                        else:
                            item = adapter._item_from_search_row(row, filtered)
                            if item is not None:
                                accepted += 1
                                seen.add(row.get('asin'))
                                row['acceptedSize'] = item.size_hint_inch
                        reasons = [key for key in filtered if filtered[key] > before[key]]
                        row['filterReason'] = reasons[0] if reasons else 'accepted'
                    record.update({'extractedRows': len(rows), 'acceptedRows': accepted,
                                   'filtered': dict(filtered)})
                    prefix.with_suffix('.json').write_text(json.dumps({'metadata': record, 'rows': rows}, ensure_ascii=False, indent=2), encoding='utf-8')
                    prefix.with_suffix('.html').write_text(await page.evaluate(SANITIZED_DOM), encoding='utf-8')
                    await page.screenshot(path=str(prefix.with_suffix('.png')), full_page=True)
                except Exception as exc:
                    record.update({'errorType': type(exc).__name__, 'error': str(exc)[:500], 'finalUrl': page.url})
                summary['pages'].append(record)
                print(json.dumps(record, ensure_ascii=True))
                await page.wait_for_timeout(2000)
        finally:
            (args.output / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
            await context.close()
            await browser.close()
    return 0 if summary.get('sessionPrepared') and all('errorType' not in p for p in summary['pages']) else 1


if __name__ == '__main__':
    raise SystemExit(asyncio.run(run(_arguments())))
