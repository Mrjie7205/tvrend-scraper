"""Currys 分页只读诊断：最多两页，只落安全元数据和脱敏图片，不发布或判定完整目录。"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re

from catalog_scrape.adapters.currys import CurrysCatalogAdapter, LISTING_URL, PAGE_SIZE
from catalog_scrape.currys_pagination import JS_PAGINATION_METADATA, assess_metadata
from failure_evidence import _atomic_json, capture_redacted_snapshot, sanitize_url
from monitor_prices.adapters.currys import currys_pagination_state
from monitor_prices.core import close_playwright_resource, get_browser_profile, launch_scraper_browser



def validate_starts(starts: list[int] | None) -> list[int]:
    starts = [400] if starts is None else starts
    if not 1 <= len(starts) <= 2 or len(set(starts)) != len(starts) or any(value < 0 or value > 100000 for value in starts):
        raise ValueError('只允许一至两个不重复的 start，范围为 0 至 100000')
    return starts




async def run_probe(starts: list[int] | None, output: Path) -> int:
    starts = validate_starts(starts)
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if get_browser_profile() != 'current':
        raise ValueError('Currys 此诊断只允许 current 固定配置')
    started = datetime.now(timezone.utc).isoformat()
    report = {'schema_version': 1, 'probe': 'currys_catalog_pagination', 'started_at': started,
              'status': 'incomplete', 'pages': [], 'max_logical_pages': 2, 'profile': 'current',
              'publishes_prices': False, 'catalog_complete': None,
              'notice': '这里只采分页证据；metadata_captured不代表完整目录或价格发布成功。'}
    for field, env, pattern in [('run_id','GITHUB_RUN_ID',r'[0-9]+'),('run_attempt','GITHUB_RUN_ATTEMPT',r'[0-9]+'),('head_sha','GITHUB_SHA',r'[0-9a-fA-F]{40}')]:
        value = os.environ.get(env, '')
        report[field] = value if re.fullmatch(pattern, value) else None
    browser = context = None
    current = None

    async def observe(page, info):
        current.update(http_status=info.get('http_status'), navigation_count=info.get('navigation_count'),
                       reason=info.get('reason'), connection_recovery=info.get('connection_recovery'))
        try:
            url = f'{LISTING_URL}?start={current["requested_start"]}&sz={PAGE_SIZE}'
            current['pagination_after_render'] = info.get('pagination_after_render') or currys_pagination_state(url, page.url, await asyncio.wait_for(page.title(), timeout=1))
            current['final_url'] = sanitize_url(page.url)
            if info.get('http_status') == 200 and (info.get('connection_recovery') or {}).get('target_verified'):
                metadata = info.get('pagination_metadata')
                if not isinstance(metadata, dict):
                    metadata = await asyncio.wait_for(page.evaluate(JS_PAGINATION_METADATA), timeout=3)
                current['metadata'] = metadata
                current['assessment'] = assess_metadata(metadata)
        except Exception as exc:
            current['observation_error_type'] = type(exc).__name__
        try:
            event_id = hashlib.sha256(f'{started}:{current["requested_start"]}'.encode()).hexdigest()[:24]
            snapshot = await capture_redacted_snapshot(page, output_dir=output, event_type='currys_catalog_probe',
                event_id=event_id, screenshot_limit=2, timeout_seconds=4)
            current['screenshot'] = {key: snapshot[key] for key in ('status','file','redacted','error_type') if key in snapshot}
        except Exception as exc:
            current['screenshot'] = {'status': 'capture_failed', 'error_type': type(exc).__name__}
        finally:
            _atomic_json(output / f'page-{current["requested_start"]}.json', current)
            await close_playwright_resource(page, 'Currys probe page', timeout_seconds=1)

    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as playwright:
            browser = await launch_scraper_browser(playwright, headless=True)
            report.update(browser_source=getattr(browser, '_tvrend_browser_source', None), browser_version=browser.version)
            adapter = CurrysCatalogAdapter()
            context = await adapter._new_context(browser)
            adapter._catalog_context = context
            adapter.page_observation_callback = observe
            for start in starts:
                current = {'requested_start': start, 'requested_sz': PAGE_SIZE, 'observed_at': datetime.now(timezone.utc).isoformat()}
                report['pages'].append(current)
                status, cards = await adapter._scrape_page(browser, start, navigation_budget=2)
                current.update(logical_status=status, observed_card_count=len(cards))
                if status != 200:
                    current['status'] = 'blocked' if status in {401,403,429} else 'not_catalog'
                else:
                    current['status'] = 'metadata_captured' if current.get('assessment', {}).get('metadata_available') else 'metadata_unavailable'
                _atomic_json(output / f'page-{start}.json', current)
                _atomic_json(output / 'report.json', report)
                if status != 200:
                    break
            await close_playwright_resource(context, 'Currys probe context')
            await close_playwright_resource(browser, 'Currys probe browser')
            context = browser = None
    except Exception as exc:
        report['error_type'] = type(exc).__name__
    finally:
        await close_playwright_resource(context, 'Currys probe context')
        await close_playwright_resource(browser, 'Currys probe browser')
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        ok = len(report['pages']) == len(starts) and all(page.get('status') == 'metadata_captured' for page in report['pages']) and not report.get('error_type')
        report['status'] = 'metadata_captured' if ok else 'incomplete'
        _atomic_json(output / 'report.json', report)
    print(json.dumps({'status':report['status'],'pages':len(report['pages']),'output':str(output)}, ensure_ascii=False))
    return 0 if ok else 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start', type=int, action='append', help='分页起点，可重复指定，最多两页；默认400')
    parser.add_argument('--output', type=Path, default=Path(__file__).resolve().parent / 'catalog_artifacts/currys-tail')
    args = parser.parse_args()
    try:
        validate_starts(args.start)
    except ValueError as exc:
        parser.error(str(exc))
    return asyncio.run(run_probe(args.start, args.output))


if __name__ == '__main__':
    raise SystemExit(main())
