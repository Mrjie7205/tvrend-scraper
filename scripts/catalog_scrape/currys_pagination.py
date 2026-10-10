"""Currys 可见分页证据与严格完整性证明；诊断和正式目录共用，不保存原始查询或隐藏值。"""
from __future__ import annotations

import re
from urllib.parse import urlparse

JS_PAGINATION_METADATA = r"""() => {
  const visible = el => {const s=getComputedStyle(el),r=el.getBoundingClientRect();
    return r.width>0&&r.height>0&&s.display!=='none'&&!['hidden','collapse'].includes(s.visibility)
      &&Number(s.opacity)!==0&&(!el.checkVisibility||el.checkVisibility({checkOpacity:true,checkVisibilityCSS:true}));};
  const ident = el => ({tag:el.tagName.toLowerCase(),id:/^[a-zA-Z][\w-]{0,80}$/.test(el.id)?el.id:null,
    classes:[...el.classList].filter(c=>/^[a-zA-Z][\w-]{0,80}$/.test(c)).slice(0,4)});
  const number = value => /^\d{1,7}$/.test(value||'')?Number(value):null;
  const linkNumbers = el => {try{const u=new URL(el.getAttribute('href'),location.href),out={
    same_endpoint:u.origin===location.origin&&u.pathname.replace(/\/$/,'')===location.pathname.replace(/\/$/,''),
    bare_endpoint:!u.search};
    for(const key of ['start','sz','page']){const vs=u.searchParams.getAll(key);out[key]=vs.length===1?number(vs[0]):null;}return out;}catch(e){return {};}};
  const resultNodes=[...document.querySelectorAll('[class*="result-count" i],[class*="results-count" i],[class*="results-number" i],[class*="results-hits" i],[id*="result-count" i]')].filter(visible).slice(0,12);
  const ranges=[],shownCounts=[],totals=[],perPage=[];
  for(const el of [...resultNodes,document.body]){
    if(!el)continue;
    const text=(el.innerText||'').replace(/\s+/g,' '),source=el===document.body?{tag:'body',id:null,classes:[]}:ident(el);
    for(const m of text.matchAll(/\b(?:showing\s+|viewing\s+)?([\d,]+)\s*(?:-|–|to)\s*([\d,]+)\s*(?:of|out of)\s*([\d,]+)\s*(?:results|products|items)\b/gi)){
      const [start,end,total]=m.slice(1,4).map(v=>Number(v.replaceAll(',','')));
      if(Number.isSafeInteger(total)&&total<=1e7)ranges.push({start,end,total,source});
      if(ranges.length>=12)break;
    }
    for(const m of text.matchAll(/\bshowing\s*([\d,]+)\s*of\s*([\d,]+)\b/gi)){
      const [shown,total]=m.slice(1,3).map(v=>Number(v.replaceAll(',','')));
      if(Number.isSafeInteger(shown)&&Number.isSafeInteger(total)&&shown<=1e7&&total<=1e7)shownCounts.push({shown,total,source});
      if(shownCounts.length>=12)break;
    }
    for(const m of text.matchAll(/\b([\d,]+)\s+(results|products|items)\b/gi)){
      const total=Number(m[1].replaceAll(',',''));
      if(Number.isSafeInteger(total)&&total<=1e7){
        if(/^\s+per\s+page\b/i.test(text.slice(m.index+m[0].length)))perPage.push({count:total,source});
        else totals.push({total,label:m[2].toLowerCase(),source,
          authoritative:el.classList?.contains('page-result-count')&&m[2].toLowerCase()==='items'});
      }
      if(totals.length>=12)break;
    }
  }
  const containers=[...document.querySelectorAll('[class*="pagination" i],[class*="paging" i],nav[aria-label*="page" i],nav[aria-label*="pagination" i]')].filter(visible).slice(0,10);
  const controls=[];
  for(const container of containers){
    for(const el of container.querySelectorAll('a,button,[aria-current],.current,.current-page,.active')){
      if(!visible(el)||controls.length>=40)continue;
      const raw=(el.innerText||'').trim().replace(/\s+/g,' ');
      const text=/[a-z0-9]/i.test(raw)?raw:(el.getAttribute('aria-label')||raw).trim();
      const parsed=text.match(/^(?:(?:go to\s+)?page\s+)?([0-9]{1,5})$/i);
      const direction=text.match(/^(?:go to\s+)?(next|previous|prev|first|last)(?:\s+page)?$/i)
        || (['next','prev'].includes(el.getAttribute('rel'))?[null,el.getAttribute('rel')]:null);
      if(!parsed&&!direction)continue;
      controls.push({source:ident(el),number:parsed?Number(parsed[1]):null,direction:direction?direction[1].toLowerCase():null,
        current:['page','true'].includes(el.getAttribute('aria-current'))||/(?:^|\s)(?:active|current|current-page)(?:\s|$)/i.test(el.className||''),
        disabled:el.disabled===true||el.getAttribute('aria-disabled')==='true'||/(?:^|\s)disabled(?:\s|$)/i.test(el.className||''),
        link_numbers:el.hasAttribute('href')?linkNumbers(el):{}});
    }
  }
  return {ranges:ranges.slice(0,12),shown_counts:shownCounts.slice(0,12),total_candidates:totals.slice(0,12),per_page_counts:perPage.slice(0,12),pagination_containers:containers.map(ident),controls};
}"""


def assess_metadata(metadata: dict) -> dict:
    ranges = {(item['start'], item['end'], item['total']) for item in metadata.get('ranges', [])
              if 0 < item['start'] <= item['end'] <= item['total']}
    shown = {(item['shown'], item['total']) for item in metadata.get('shown_counts', [])
             if 0 <= item['shown'] <= item['total']}
    authoritative = {item['total'] for item in metadata.get('total_candidates', []) if item.get('authoritative') is True}
    scoped = {item['total'] for item in metadata.get('total_candidates', []) if item['source']['tag'] != 'body'}
    totals = {value[2] for value in ranges} | {value[1] for value in shown} | (authoritative or scoped or {item['total'] for item in metadata.get('total_candidates', [])})
    current = {item['number'] for item in metadata.get('controls', []) if item['current'] and item['number'] is not None}
    next_controls = [item for item in metadata.get('controls', []) if item['direction'] == 'next']
    last_controls = [item for item in metadata.get('controls', []) if item['direction'] == 'last']
    return {'metadata_available': len(totals) == 1 and (bool(ranges) or bool(shown) or bool(metadata.get('total_candidates'))),
            'total': next(iter(totals)) if len(totals) == 1 else None, 'total_conflict': len(totals) > 1,
            'trusted_total': next(iter(authoritative)) if len(authoritative) == 1 and len(totals) == 1 else None,
            'showing_range': list(next(iter(ranges))) if len(ranges) == 1 else None,
            'shown_count': next(iter(shown))[0] if len(shown) == 1 else None,
            'current_page': next(iter(current)) if len(current) == 1 else None,
            'next_control_observed': bool(next_controls),
            'next_disabled': all(item['disabled'] for item in next_controls) if next_controls else None,
            'explicit_last_page': next((item['link_numbers'].get('page') for item in last_controls if item['link_numbers'].get('page') is not None), None)}


def verify_page_position(start: int, size: int, pagination: dict, metadata: dict, *, document_verified: bool) -> dict:
    """可见当前页和同目录前页链接证明顺序；query消失不等于错页，明确冲突不能忽略。"""
    assessment = assess_metadata(metadata)
    expected = start // size + 1
    current = assessment['current_page']
    actual = pagination.get('actual') or {}
    reasons = []
    if not document_verified:
        reasons.append('document_not_verified')
    if start % size or current != expected:
        reasons.append('current_page_mismatch')
    if pagination.get('title_page') is not None and pagination['title_page'] != expected:
        reasons.append('title_page_mismatch')
    if any(actual.get(key) is not None and actual[key] != value for key, value in [('start', start), ('sz', size), ('page', expected)]):
        reasons.append('query_page_mismatch')
    previous_confirmed = start == 0
    for control in metadata.get('controls', []):
        link = control.get('link_numbers') or {}
        if not link.get('same_endpoint') or control.get('disabled'):
            continue
        if control.get('direction') not in {'prev', 'previous'} and control.get('number') != expected - 1:
            continue
        if link.get('start') == start - size and link.get('sz') == size:
            previous_confirmed = True
        elif start == size and link.get('bare_endpoint'):
            # 明确上一页/1号页链接指向同目录裸入口，且本轮0页必须已单独验证。
            previous_confirmed = True
    if not previous_confirmed:
        reasons.append('previous_page_unverified')
    total = assessment['trusted_total']
    if total is None or total <= 0 or assessment['total_conflict']:
        reasons.append('trusted_total_unavailable')
    if total is not None:
        expected_end = min(start + size, total)
        if any(item['total'] != total or item['shown'] != expected_end for item in metadata.get('shown_counts', [])):
            reasons.append('shown_count_mismatch')
        if any((item['start'], item['end'], item['total']) != (start + 1, expected_end, total) for item in metadata.get('ranges', [])):
            reasons.append('showing_range_mismatch')
    return {'verified': not reasons, 'reasons': reasons, 'expected_page': expected, 'current_page': current,
            'previous_confirmed': previous_confirmed, 'trusted_total': total,
            'shown_present': bool(metadata.get('shown_counts') or metadata.get('ranges'))}


def completion_proof(pages: dict, cards: dict, *, current_start: int, size: int) -> dict:
    """只有可信总数、连续正确页序和真实产品ID全集三者吻合才接受末页。"""
    expected_starts = list(range(0, current_start + 1, size))
    unresolved = []
    totals = set()
    for start in expected_starts:
        row = pages.get(str(start), {})
        evidence = row.get('pagination_evidence') or {}
        if row.get('status') != 200 or row.get('missing') or not evidence.get('verified'):
            unresolved.append(start)
        if evidence.get('trusted_total') is not None:
            totals.add(evidence['trusted_total'])
    unique_ids, invalid = set(), 0
    for card in cards.values():
        href = card.get('href') or ''
        try:
            parsed = urlparse(href)
        except (TypeError, ValueError):
            invalid += 1
            continue
        match = re.search(r'(?<!\d)(\d{7,9})\.html$', parsed.path, re.IGNORECASE)
        if '/products/' in parsed.path and match and (not parsed.netloc or parsed.netloc == 'www.currys.co.uk'):
            unique_ids.add(match.group(1))
        else:
            invalid += 1
    total = next(iter(totals)) if len(totals) == 1 else None
    covers_last = total is not None and current_start < total <= current_start + size
    reasons = []
    if unresolved:
        reasons.append('missing_or_unverified_pages')
    if total is None:
        reasons.append('total_missing_or_drift')
    if not covers_last:
        reasons.append('not_last_window')
    if invalid or len(unique_ids) != total:
        reasons.append('unique_product_count_mismatch')
    return {'complete': not reasons, 'reasons': reasons, 'trusted_total': total, 'total_values': sorted(totals),
            'unique_product_ids': len(unique_ids), 'invalid_product_id_observations': invalid,
            'last_start': current_start, 'last_window_contains_total': covers_last,
            'expected_pages': len(expected_starts), 'unresolved_pages': unresolved}
