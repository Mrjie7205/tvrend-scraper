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
from failure_evidence import _atomic_json, capture_redacted_snapshot, sanitize_url
from monitor_prices.adapters.currys import currys_pagination_state
from monitor_prices.core import close_playwright_resource, get_browser_profile, launch_scraper_browser


JS_PAGINATION_METADATA = r"""() => {
  const visible = el => {const s=getComputedStyle(el),r=el.getBoundingClientRect();
    return r.width>0&&r.height>0&&s.display!=='none'&&!['hidden','collapse'].includes(s.visibility)
      &&Number(s.opacity)!==0&&(!el.checkVisibility||el.checkVisibility({checkOpacity:true,checkVisibilityCSS:true}));};
  const ident = el => ({tag:el.tagName.toLowerCase(),id:/^[a-zA-Z][\w-]{0,80}$/.test(el.id)?el.id:null,
    classes:[...el.classList].filter(c=>/^[a-zA-Z][\w-]{0,80}$/.test(c)).slice(0,4)});
  const number = value => /^\d{1,7}$/.test(value||'')?Number(value):null;
  const linkNumbers = el => {try{const u=new URL(el.getAttribute('href'),location.href),out={};
    for(const key of ['start','sz','page']){const vs=u.searchParams.getAll(key);out[key]=vs.length===1?number(vs[0]):null;}return out;}catch(e){return {};}};
  const resultNodes=[...document.querySelectorAll('[class*="result-count" i],[class*="results-count" i],[class*="results-number" i],[class*="results-hits" i],[id*="result-count" i]')].filter(visible).slice(0,12);
  const ranges=[],shownCounts=[],totals=[];
  for(const el of [...resultNodes,document.body]){
    if(!el)continue;
    const text=(el.innerText||'').replace(/\s+/g,' '),source=el===document.body?{tag:'body',id:null,classes:[]}:ident(el);
    for(const m of text.matchAll(/\b(?:showing\s+|viewing\s+)?([\d,]+)\s*(?:-|–|to)\s*([\d,]+)\s*(?:of|out of)\s*([\d,]+)\s*(?:results|products|items)\b/gi)){
      const [start,end,total]=m.slice(1,4).map(v=>Number(v.replaceAll(',','')));
      if(Number.isSafeInteger(total)&&total<=1e7)ranges.push({start,end,total,source});
      if(ranges.length>=12)break;
    }
    for(const m of text.matchAll(/\bshowing\s+([\d,]+)\s+of\s+([\d,]+)\b/gi)){
      const [shown,total]=m.slice(1,3).map(v=>Number(v.replaceAll(',','')));
      if(Number.isSafeInteger(shown)&&Number.isSafeInteger(total)&&shown<=1e7&&total<=1e7)shownCounts.push({shown,total,source});
      if(shownCounts.length>=12)break;
    }
    for(const m of text.matchAll(/\b([\d,]+)\s+(results|products|items)\b/gi)){
      const total=Number(m[1].replaceAll(',',''));
      if(Number.isSafeInteger(total)&&total<=1e7)totals.push({total,label:m[2].toLowerCase(),source});
      if(totals.length>=12)break;
    }
  }
  const containers=[...document.querySelectorAll('[class*="pagination" i],[class*="paging" i],nav[aria-label*="page" i],nav[aria-label*="pagination" i]')].filter(visible).slice(0,10);
  const controls=[];
  for(const container of containers){
    for(const el of container.querySelectorAll('a,button,[aria-current],.current,.active')){
      if(!visible(el)||controls.length>=40)continue;
      const raw=(el.innerText||'').trim().replace(/\s+/g,' ');
      const text=/[a-z0-9]/i.test(raw)?raw:(el.getAttribute('aria-label')||raw).trim();
      const parsed=text.match(/^(?:(?:go to\s+)?page\s+)?([0-9]{1,5})$/i);
      const direction=text.match(/^(?:go to\s+)?(next|previous|prev|first|last)(?:\s+page)?$/i)
        || (['next','prev'].includes(el.getAttribute('rel'))?[null,el.getAttribute('rel')]:null);
      if(!parsed&&!direction)continue;
      controls.push({source:ident(el),number:parsed?Number(parsed[1]):null,direction:direction?direction[1].toLowerCase():null,
        current:['page','true'].includes(el.getAttribute('aria-current'))||/(?:^|\s)(?:active|current)(?:\s|$)/i.test(el.className||''),
        disabled:el.disabled===true||el.getAttribute('aria-disabled')==='true'||/(?:^|\s)disabled(?:\s|$)/i.test(el.className||''),
        link_numbers:el.hasAttribute('href')?linkNumbers(el):{}});
    }
  }
  return {ranges:ranges.slice(0,12),shown_counts:shownCounts.slice(0,12),total_candidates:totals.slice(0,12),pagination_containers:containers.map(ident),controls};
}"""


def validate_starts(starts: list[int] | None) -> list[int]:
    starts = [400] if starts is None else starts
    if not 1 <= len(starts) <= 2 or len(set(starts)) != len(starts) or any(value < 0 or value > 100000 for value in starts):
        raise ValueError('只允许一至两个不重复的 start，范围为 0 至 100000')
    return starts


def assess_metadata(metadata: dict) -> dict:
    ranges = {(item['start'], item['end'], item['total']) for item in metadata.get('ranges', [])
              if 0 < item['start'] <= item['end'] <= item['total']}
    shown = {(item['shown'], item['total']) for item in metadata.get('shown_counts', [])
             if 0 <= item['shown'] <= item['total']}
    scoped = {item['total'] for item in metadata.get('total_candidates', []) if item['source']['tag'] != 'body'}
    totals = {value[2] for value in ranges} | {value[1] for value in shown} | (scoped or {item['total'] for item in metadata.get('total_candidates', [])})
    current = {item['number'] for item in metadata.get('controls', []) if item['current'] and item['number'] is not None}
    next_controls = [item for item in metadata.get('controls', []) if item['direction'] == 'next']
    last_controls = [item for item in metadata.get('controls', []) if item['direction'] == 'last']
    return {'metadata_available': len(totals) == 1 and (bool(ranges) or bool(shown) or bool(metadata.get('total_candidates'))),
            'total': next(iter(totals)) if len(totals) == 1 else None, 'total_conflict': len(totals) > 1,
            'showing_range': list(next(iter(ranges))) if len(ranges) == 1 else None,
            'shown_count': next(iter(shown))[0] if len(shown) == 1 else None,
            'current_page': next(iter(current)) if len(current) == 1 else None,
            'next_control_observed': bool(next_controls),
            'next_disabled': all(item['disabled'] for item in next_controls) if next_controls else None,
            'explicit_last_page': next((item['link_numbers'].get('page') for item in last_controls if item['link_numbers'].get('page') is not None), None)}


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
            current['pagination_after_render'] = currys_pagination_state(url, page.url, await asyncio.wait_for(page.title(), timeout=1))
            current['final_url'] = sanitize_url(page.url)
            if info.get('http_status') == 200 and (info.get('connection_recovery') or {}).get('target_verified'):
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
