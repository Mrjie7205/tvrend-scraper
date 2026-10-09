"""有限采样 Amazon 公开商品字段；失败现场统一脱敏，永不写正式目录或价格。"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote_plus

from catalog_scrape.adapters.amazon import (
    AMAZON_DE, AMAZON_ES, AMAZON_GB, AMAZON_IT,
    AmazonCatalogAdapter, AmazonCatalogIncomplete, _JS_EXTRACT, _page_rejection_reason,
)
from catalog_scrape.diagnostics import capture_catalog_failure
from failure_evidence import redact_text, sanitize_url
from monitor_prices.core import (
    close_playwright_resource, get_browser_profile, launch_scraper_browser, new_scraper_context,
)

MARKETS = {market.code: market for market in (AMAZON_DE, AMAZON_GB, AMAZON_IT, AMAZON_ES)}
PAGE_STATE = r"""() => {
  const text = document.body?.innerText || '';
  return {
    cardCount: document.querySelectorAll("div[data-component-type='s-search-result']").length,
    nextPresent: !!document.querySelector('a.s-pagination-next'),
    nextDisabled: !!document.querySelector('.s-pagination-next.s-pagination-disabled'),
    captcha: /captcha|enter the characters you see below|api-services-support@amazon.com/i.test(text),
    robotCheck: /robot check|not a robot|automated access|unusual traffic|access denied|accesso negato|verify you are human|security check/i.test(text),
    continueShopping: /Fai clic sul pulsante qui sotto per continuare a fare acquisti|Click the button below to continue shopping|Klicke auf die Schaltfläche unten, um mit dem Einkaufen fortzufahren/i.test(text),
    accessChallengeTarget: Array.from(document.querySelectorAll('form, button, input[type=submit]')).some(el => {
      if (!el.getClientRects().length) return false;
      const raw = el.getAttribute('action') || el.getAttribute('formaction') || el.form?.getAttribute('action');
      try { return /\/validatecaptcha\/?$/i.test(new URL(raw || '', location.href).pathname); } catch { return false; }
    })
  };
}"""


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--country', choices=tuple(MARKETS), required=True)
    parser.add_argument('--query', choices=('samsung', 'lg', 'tcl', 'hisense', 'sony'))
    parser.add_argument('--pages', default='1,2')
    parser.add_argument('--entry-only', action='store_true',
                        help='只检查首页、配送地及既有锚点，不进入品牌分页')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not args.entry_only and not args.query:
        parser.error('非 --entry-only 模式必须提供 --query')
    args.query = args.query or 'hisense'
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


def _safe_rows(rows):
    """只保留列明的公开商品字段，不落卡片全文、HTML 或浏览器状态。"""
    fields = ('asin', 'brand', 'title', 'price', 'sizeText', 'sponsored',
              'variantHint', 'acceptedSize', 'filterReason')
    return [{key: redact_text(row[key], 500) if isinstance(row[key], str) else row[key]
             for key in fields if key in row} for row in rows]


async def run(args: argparse.Namespace) -> int:
    from playwright.async_api import async_playwright
    market = MARKETS[args.country]
    adapter = AmazonCatalogAdapter(market)
    args.output.mkdir(parents=True, exist_ok=True)
    summary = {'country': market.code, 'query': args.query,
               'observedAt': datetime.now(UTC).isoformat(), 'diagnosticOnly': True,
               'entryOnly': bool(getattr(args, 'entry_only', False)), 'pages': [],
               'browser_profile': get_browser_profile(),
               'browser_version': 'not_started', 'browser_source': 'not_started'}
    browser = context = page = None
    old_root = os.environ.get('FAILURE_EVIDENCE_DIR')
    if old_root is None:
        os.environ['FAILURE_EVIDENCE_DIR'] = str(args.output / 'failure_artifacts')

    async def capture(reason, error=None, **kwargs):
        path = await capture_catalog_failure(
            page, platform='Amazon', country=market.code,
            stage='diagnostic_entry' if not summary.get('sessionPrepared') else 'diagnostic_search',
            reason=reason, adapter=adapter, error=error, **kwargs,
        )
        if path is not None:
            summary['failureEvidenceFile'] = path.name
        return path

    try:
        async with async_playwright() as playwright:
            browser = await launch_scraper_browser(playwright, headless=True)
            version = getattr(browser, 'version', None)
            source = vars(browser).get('_tvrend_browser_source')
            summary.update(
                browser_profile=get_browser_profile(browser),
                browser_version=version if isinstance(version, str) else 'unknown',
                browser_source=source if isinstance(source, str) else 'unknown',
            )
            context = await new_scraper_context(
                browser, country=market.code, locale_override=(market.locale, market.timezone),
                viewport={'width': 1366, 'height': 900},
            )
            page = await context.new_page()
            try:
                summary['sessionPrepared'] = await adapter._prepare_market_session(page)
            except Exception as error:
                summary.update(sessionPrepared=False, errorType=type(error).__name__,
                               error=redact_text(str(error)))
                await capture('session_preparation_error', error)
                return 1
            if not summary['sessionPrepared']:
                previous = getattr(adapter, '_failure_evidence_path', None)
                if isinstance(previous, Path):
                    summary['failureEvidenceFile'] = previous.name
                else:
                    await capture('session_preparation_rejected')
                return 1
            if summary['entryOnly']:
                return 0
            for number in args.pages:
                url = f'{market.base_url}/s?k={quote_plus(args.query + " " + market.search_word)}&page={number}'
                record = {'page': number, 'requestedUrl': sanitize_url(url)}
                prefix = args.output / f'{market.code.lower()}-{args.query}-p{number}'
                stop = False
                try:
                    response = await page.goto(url, wait_until='domcontentloaded', timeout=45000)
                    await page.wait_for_timeout(3000)
                    status = response.status if response else None
                    state = await page.evaluate(PAGE_STATE)
                    record.update(httpStatus=status, finalUrl=sanitize_url(page.url), state=state)
                    rejection = _page_rejection_reason(status, state)
                    if rejection:
                        raise AmazonCatalogIncomplete(rejection)
                    rows = await page.evaluate(_JS_EXTRACT)
                    filtered = Counter({'sponsored': 0, 'no_brand': 0, 'no_size': 0,
                                        'non_tv': 0, 'duplicate': 0})
                    accepted, seen = 0, set()
                    for row in rows:
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
                    record.update(extractedRows=len(rows), acceptedRows=accepted, filtered=dict(filtered))
                    prefix.with_suffix('.json').write_text(
                        json.dumps({'metadata': record, 'rows': _safe_rows(rows)}, ensure_ascii=False, indent=2),
                        encoding='utf-8',
                    )
                except Exception as error:
                    record.update(errorType=type(error).__name__, error=redact_text(str(error)),
                                  finalUrl=sanitize_url(page.url))
                    await capture('diagnostic_page_error', error, url=url,
                                  http_status=record.get('httpStatus'))
                    # 诊断也不应在访问挑战后继续请求后续页面。
                    stop = isinstance(error, AmazonCatalogIncomplete)
                summary['pages'].append(record)
                print(json.dumps(record, ensure_ascii=True))
                if stop:
                    break
                await page.wait_for_timeout(2000)
            return 0 if all('errorType' not in row for row in summary['pages']) else 1
    except Exception as error:
        summary.update(errorType=type(error).__name__, error=redact_text(str(error)))
        await capture('diagnostic_runner_error', error)
        return 1
    finally:
        try:
            (args.output / 'summary.json').write_text(
                json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8',
            )
        except OSError:
            pass
        for resource in (context, browser):
            if resource is not None:
                await close_playwright_resource(resource, 'Amazon diagnostic resource')
        if old_root is None:
            os.environ.pop('FAILURE_EVIDENCE_DIR', None)


if __name__ == '__main__':
    raise SystemExit(asyncio.run(run(_arguments())))
