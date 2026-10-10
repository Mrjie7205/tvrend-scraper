"""Currys (currys.co.uk / 英国家电零售) 价格提取。

Currys 的 Schema.org / JSON-LD 很标准,项目现成的 get_price_from_schema 直接给
(price, 'GBP')(2026-06 实测主路径命中)。DOM 兜底取 PDP 主商品价,避开
"recently viewed" 里的配件价(£39.99 那种)。clean_price 已自动识别 £ → GBP。
"""
from __future__ import annotations

import os
import re
import asyncio
from urllib.parse import parse_qs, urlparse
from collections import Counter
from datetime import datetime, timezone

from .base import BaseAdapter
from ..core import clean_price, close_playwright_resource, get_price_from_schema
from failure_evidence import capture_failure, redact_text, sanitize_url


RE_PRODUCT_ID = re.compile(r"(\d{7,9})(?:\.html)?(?:[?#]|$)", re.IGNORECASE)


_JS_AUTOMATIC_CONNECTION_CHECK = r"""() => {
  const visible = el => {
    const s = getComputedStyle(el), r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && s.display !== 'none' && !['hidden','collapse'].includes(s.visibility)
      && Number(s.opacity) !== 0 && (!el.checkVisibility || el.checkVisibility({checkOpacity:true,checkVisibilityCSS:true}));
  };
  const text = (document.body?.innerText || '').replace(/\s+/g, ' '), title = document.title || '';
  const shell = /bear with us|just a moment/i.test(title + ' ' + text);
  const connection = /checking your connection(?: is secure)?|checking the security of your connection before letting you onto our site|check your connection is secure/i.test(text);
  const hard = /sorry[, ]+you have been blocked|you are unable to access|access denied/i.test(title + ' ' + text);
  const explicitHuman = /enter the characters|verify you are human|not a robot|solve.{0,20}captcha/i.test(text);
  const captchaControls = [...document.querySelectorAll('input[name*=captcha i],input[id*=captcha i],iframe[src*=captcha i],iframe[src*=turnstile i],.cf-turnstile,[class*=g-recaptcha],[class*=h-captcha]')].some(visible);
  // 普通目录筛选、商品比较或套餐checkbox不是人机验证；只在明确检查上下文中升级。
  const contextualCheckbox = (shell || explicitHuman) && [...document.querySelectorAll('input[type=checkbox]')].some(visible);
  const controls = captchaControls || contextualCheckbox;
  return {automatic_check: shell && connection && !hard && !explicitHuman && !controls,
          human_controls: controls || explicitHuman, hard_block: hard, connection_shell: shell,
          dom_available: !!document.body && !!text.trim(), page_title: title};
}"""


def _currys_target_matches(requested_url: str, final_url: str, expected_kind: str) -> bool:
    if not isinstance(requested_url, str) or not isinstance(final_url, str):
        return False
    requested, final = urlparse(requested_url), urlparse(final_url)
    if requested.scheme != 'https' or final.scheme != 'https' or requested.netloc != final.netloc:
        return False
    if expected_kind == 'product':
        left, right = RE_PRODUCT_ID.search(requested.path), RE_PRODUCT_ID.search(final.path)
        return bool('/products/' in final.path and left and right and left.group(1) == right.group(1))
    if expected_kind == 'catalog':
        # 目录端点身份与分页完整性分开；站点规范化分页query不能否决明确商品报价。
        return requested.path.rstrip('/') == final.path.rstrip('/')
    raise ValueError('expected_kind 必须为 product 或 catalog')


def currys_navigation_state(page) -> dict:
    tracker = vars(page).get('_currys_document_tracker') or {}
    return {'navigation_count': tracker.get('count', 0), 'http_status': tracker.get('status'),
            'committed_documents': tracker.get('committed', 0), 'in_flight': tracker.get('in_flight', False)}


def currys_pagination_state(requested_url: str, final_url: str, title: str | None) -> dict:
    """只留下白名单数字分页值和脱敏标题，不保存原始query或令牌。"""
    def numbers(url):
        query = parse_qs(urlparse(url).query)
        result = {}
        for key in ('start', 'sz', 'page'):
            values = query.get(key, [])
            result[key] = int(values[0]) if len(values) == 1 and re.fullmatch(r'[0-9]{1,7}', values[0]) else None
        return result
    wanted, actual = numbers(requested_url), numbers(final_url)
    safe_title = redact_text(title, 200) if isinstance(title, str) else None
    title_page = re.search(r'\bPage\s+([0-9]{1,5})\b', safe_title or '', re.IGNORECASE)
    return {'requested': wanted, 'actual': actual, 'page_title': safe_title,
            'title_page': int(title_page.group(1)) if title_page else None,
            'position_verified': wanted['start'] is not None and wanted['sz'] is not None
                                 and wanted['start'] == actual['start'] and wanted['sz'] == actual['sz']}


def _currys_document_tracker(page, initial_status):
    tracker = vars(page).get('_currys_document_tracker')
    if tracker is not None:
        return tracker
    if not callable(getattr(page, 'on', None)) or asyncio.iscoroutinefunction(page.on):
        raise TypeError('页面没有同步导航事件观察接口')
    seeded = initial_status is not None
    tracker = {'count': int(seeded), 'committed': int(seeded), 'status': initial_status,
               'url': page.url, 'response': None, 'change': asyncio.Event(),
               'in_flight': False, 'active_request': None, 'request_started_at': None}
    def request_started(request):
        try:
            if not request.is_navigation_request() or request.frame != page.main_frame:
                return
            # 301/302链是同一次导航；子资源不占主文档预算。
            if request.redirected_from is None:
                tracker['count'] += 1
                tracker['request_started_at'] = asyncio.get_running_loop().time()
            elif tracker['request_started_at'] is None:
                tracker['request_started_at'] = asyncio.get_running_loop().time()
            tracker.update(in_flight=True, active_request=request)
            tracker['change'].set()
        except Exception:
            pass
    def response_seen(response):
        try:
            if (response.request.is_navigation_request() and response.frame == page.main_frame
                    and not 300 <= response.status < 400):
                tracker['response'] = response
        except Exception:
            pass
    def committed(frame):
        response = tracker.get('response')
        if frame != page.main_frame or response is None:
            return
        try:
            if response.url.split('#', 1)[0] != frame.url.split('#', 1)[0]:
                return
            tracker['committed'] += 1
            tracker.update(response=None, count=max(tracker['count'], tracker['committed']),
                           status=response.status, url=frame.url, in_flight=False, active_request=None)
            tracker['change'].set()
        except Exception:
            pass
    def request_failed(request):
        if request == tracker.get('active_request'):
            tracker.update(in_flight=False, active_request=None, response=None)
            tracker['change'].set()
    def closed(*args):
        page.remove_listener('request', request_started)
        page.remove_listener('requestfailed', request_failed)
        page.remove_listener('response', response_seen)
        page.remove_listener('framenavigated', committed)
        page.remove_listener('close', closed)
    # 请求一开始即占预算；不能等提交事件再决定是否发第二次goto。
    page.on('request', request_started)
    page.on('requestfailed', request_failed)
    page.on('response', response_seen)
    page.on('framenavigated', committed)
    page.on('close', closed)
    vars(page)['_currys_document_tracker'] = tracker
    return tracker


def track_currys_document(page, initial_status=None):
    """首次goto前安装观察器；提交后的补充状态只用于不发事件的测试/兼容页面。"""
    try:
        tracker = _currys_document_tracker(page, initial_status)
        if initial_status is not None and tracker['committed'] == 0 and not tracker['in_flight']:
            tracker.update(count=max(1, tracker['count']), committed=1, status=initial_status, url=page.url)
    except (AttributeError, TypeError):
        pass


def currys_recovery_failure_reason(report: dict) -> str:
    """未验证不等于被拒绝；读取/观察器故障不得触发全渠道访问保护。"""
    phase = report.get('phase')
    if phase == 'target_mismatch':
        return 'redirect_unverified'
    if phase == 'dom_unavailable':
        return 'dom_read_error'
    if phase == 'response_observer_unavailable':
        return 'navigation_observer_error'
    if phase == 'navigation_in_progress_timeout':
        return 'navigation_timeout'
    if report.get('human_controls') or report.get('hard_block') or report.get('automatic_check'):
        return 'challenge_unresolved'
    return 'navigation_unverified'


async def wait_currys_automatic_check(page, *, requested_url: str, initial_status: int | None,
                                     expected_kind: str = 'product', timeout_seconds: float = 8.0,
                                     remaining_navigations: int = 1, normal_ready_timeout_seconds: float = 15.0) -> dict:
    """只等已知自动连接检查完成，不点击、不主动导航；成功须真实新主文档200及原目标身份。"""
    report = {'initial_status': initial_status, 'final_status': initial_status,
              'automatic_check': False, 'human_controls': False, 'hard_block': False,
              'rate_limited': initial_status == 429, 'followup_navigation_count': 0,
              'target_verified': False, 'retry_allowed': False, 'phase': 'not_automatic', 'navigation_count': 1,
              'initial_evidence_path': None, 'outcome': 'pending',
              'requested_url': sanitize_url(requested_url), 'final_url': sanitize_url(page.url)}
    remaining_navigations = max(0, int(remaining_navigations))
    try:
        tracker = _currys_document_tracker(page, initial_status)
    except (AttributeError, TypeError) as exc:
        report.update(phase='response_observer_unavailable', error_type=type(exc).__name__, outcome='unresolved')
        return report
    start_count = tracker['count']
    maximum_count = min(2, start_count + remaining_navigations)
    change = tracker['change']
    loop = asyncio.get_running_loop()
    check_deadline = loop.time() + max(0.05, float(timeout_seconds))
    ready_deadline = None
    ready_document = None
    ready_budget = max(0.05, float(normal_ready_timeout_seconds))

    def refresh():
        report.update(navigation_count=tracker['count'], committed_documents=tracker['committed'],
                      navigation_in_flight=tracker['in_flight'], final_status=tracker['status'],
                      followup_navigation_count=max(0, tracker['count'] - start_count), final_url=sanitize_url(tracker['url']))

    async def changed_or_tick(deadline):
        try:
            await asyncio.wait_for(change.wait(), timeout=max(0.01, min(0.1, deadline - loop.time())))
        except asyncio.TimeoutError:
            pass

    try:
        while True:
            refresh()
            if tracker['count'] > maximum_count:
                report['phase'] = 'navigation_budget_exhausted'
                break
            # 已开始的主文档请求先等提交，不能读取旧403或发goto争抢同一页面。
            if tracker['in_flight']:
                deadline = (tracker['request_started_at'] or loop.time()) + ready_budget
                if loop.time() >= deadline:
                    report.update(phase='navigation_in_progress_timeout', error_type='TimeoutError')
                    break
                change.clear()
                await changed_or_tick(deadline)
                continue
            # 自动检查前段8秒与真实200后的正常DOM就绪观察各自有界。
            if tracker['status'] == 200 and ready_document != tracker['committed']:
                ready_document = tracker['committed']
                ready_deadline = loop.time() + ready_budget
            deadline = ready_deadline if tracker['status'] == 200 and ready_deadline is not None else check_deadline
            left = deadline - loop.time()
            if left <= 0:
                report['phase'] = 'automatic_check_timeout' if tracker['status'] != 200 and report['automatic_check'] else 'dom_unavailable'
                if report['phase'] == 'dom_unavailable':
                    report['error_type'] = 'TimeoutError'
                break
            change.clear()
            try:
                try:
                    await page.wait_for_load_state('domcontentloaded', timeout=max(1, min(200, int(left * 1000))))
                except Exception as exc:
                    if 'Timeout' not in type(exc).__name__:
                        raise
                # 正常页可在DOMContentLoaded事件前已有商品；观察可读DOM，不以全页事件为硬门禁。
                flags = await asyncio.wait_for(page.evaluate(_JS_AUTOMATIC_CONNECTION_CHECK), timeout=max(0.01, min(1.0, deadline - loop.time())))
            except Exception as exc:
                if tracker['in_flight'] or (tracker['status'] == 200 and ready_document != tracker['committed']):
                    continue
                if (report['automatic_check'] or tracker['count'] > start_count or 'Timeout' in type(exc).__name__) and loop.time() < deadline:
                    await changed_or_tick(deadline)
                    continue
                report.update(phase='dom_unavailable', error_type=type(exc).__name__)
                break
            report['human_controls'] = flags.get('human_controls') is True
            report['hard_block'] = flags.get('hard_block') is True
            report['automatic_check'] = report['automatic_check'] or flags.get('automatic_check') is True
            if flags.get('automatic_check') and not tracker.get('automatic_recorded'):
                tracker['automatic_recorded'] = True
                capture_started = loop.time()
                try:
                    evidence_path = await capture_failure(
                        page, platform='Currys', country='GB', stage='initial_connection_check',
                        reason='automatic_connection_check_pending', url=requested_url,
                        http_status=initial_status, timeout_seconds=4.0,
                    )
                    tracker['initial_evidence_path'] = str(evidence_path) if evidence_path else None
                except (Exception, asyncio.CancelledError) as exc:
                    tracker['initial_evidence_error'] = type(exc).__name__
                # 留证是独立有界旁路，不消耗原有恢复等待预算，也不据旧DOM flags决定新文档结果。
                check_deadline += loop.time() - capture_started
                continue
            refresh()
            if tracker['in_flight']:
                continue
            report['rate_limited'] = report['final_status'] == 429
            if report['rate_limited'] or report['human_controls'] or report['hard_block']:
                report['phase'] = 'rate_limited' if report['rate_limited'] else 'human_challenge' if report['human_controls'] else 'hard_block'
                break
            if tracker['count'] > maximum_count:
                report['phase'] = 'navigation_budget_exhausted'
                break
            target = _currys_target_matches(requested_url, page.url, expected_kind)
            fresh_200 = (report['final_status'] == 200 and
                         (initial_status == 200 and not report['automatic_check'] or tracker['committed'] > 1))
            if expected_kind == 'catalog':
                report['pagination'] = currys_pagination_state(requested_url, page.url, flags.get('page_title'))
            if fresh_200 and flags.get('dom_available') is False:
                await changed_or_tick(deadline)
                continue
            if fresh_200 and not flags.get('automatic_check') and not flags.get('connection_shell'):
                report['target_verified'] = target
                report['phase'] = 'normal_page_restored' if target else 'target_mismatch'
                break
            if not report['automatic_check']:
                report['phase'] = 'not_automatic'
                break
            if tracker['count'] >= maximum_count:
                report['phase'] = 'navigation_budget_exhausted'
                break
            await changed_or_tick(deadline)
        report['retry_allowed'] = bool(report['automatic_check'] and report['final_status'] == 403
                                       and not report['human_controls'] and not report['hard_block']
                                       and not report['rate_limited']
                                       and not tracker['in_flight']
                                       and tracker['count'] < maximum_count
                                       and report['phase'] in {'automatic_check_timeout', 'dom_unavailable'})
        if report['phase'] == 'navigation_budget_exhausted' and report['automatic_check']:
            # 用尽预算的自动页不能在后续截图等待期间继续自行刷新；最小现场已在识别时保存。
            await close_playwright_resource(page, 'Currys exhausted navigation page', timeout_seconds=1)
            report['page_closed_for_budget'] = True
        report['outcome'] = 'recovered' if report['target_verified'] and (tracker.get('automatic_recorded') or initial_status == 403) else 'normal' if report['target_verified'] else 'unresolved'
        return report
    finally:
        refresh()
        if report['phase'] == 'navigation_in_progress_timeout':
            report['last_committed_status'] = report['final_status']
            report['final_status'] = None
        report['initial_evidence_path'] = tracker.get('initial_evidence_path')
        if tracker.get('initial_evidence_error'):
            report['initial_evidence_error'] = tracker['initial_evidence_error']


class CurrysPdpGuard:
    """每轮独立的商品页保护；目录403不计数，只有连续不同SKU的确认拒绝触发停止。"""
    def __init__(self, limit: int = 6):
        self.limit = limit
        self.consecutive_skus: dict[tuple[str, str], dict] = {}
        self.maximum_consecutive = 0
        self.outcomes = Counter()
        self.requests = Counter()
        self.skipped = Counter()
        self.stop_reason = None
        self.trigger = None
        self.resets = 0
        self.connection_recoveries = []

    @property
    def stopped(self):
        return self.stop_reason is not None

    def start_request(self, purpose="price_lookup") -> bool:
        # 无await的检查和计数使并发任务不能在确认停止后又发出新请求。
        if self.stopped:
            return False
        self.requests[purpose] += 1
        return True

    def record_skip(self, purpose="price_lookup") -> str:
        self.skipped[purpose] += 1
        return "pdp_rate_limited" if "rate_limited" in (self.stop_reason or "") else "pdp_access_suspended"

    def _stop(self, reason, trigger):
        if not self.stopped:
            self.stop_reason = reason
            self.trigger = {**trigger, "observed_at": datetime.now(timezone.utc).isoformat(),
                            "consecutive_skus": list(self.consecutive_skus.values())}

    def catalog_rate_limited(self):
        # 目录明确429是限流指令；与目录403的可恢复局部失败分开处理。
        self._stop("catalog_rate_limited", {"source": "catalog", "http_status": 429})

    def observe(self, sku, *, http_status=None, reason=None):
        kind = reason or "reachable_product"
        self.outcomes[kind] += 1
        if http_status == 429 or reason == "rate_limited":
            self._stop("pdp_rate_limited", {"source": "pdp", "http_status": http_status,
                                           "reason": kind, "product": sku["product_name"]})
            return
        if http_status in {401, 403} or reason in {"access_blocked", "challenge_unresolved"}:
            identity = (sku["product_name"], sku["country"])
            self.consecutive_skus.setdefault(identity, {"product": identity[0], "country": identity[1]})
            self.maximum_consecutive = max(self.maximum_consecutive, len(self.consecutive_skus))
            if len(self.consecutive_skus) >= self.limit:
                self._stop("consecutive_pdp_access_denials", {"source": "pdp", "http_status": http_status,
                                                            "reason": kind, "product": sku["product_name"]})
            return
        # 可达目标200/404、重定向、无价或其它非拒绝结果都不能累积为连续封禁证据。
        if self.consecutive_skus:
            self.resets += 1
        self.consecutive_skus.clear()
        # 一旦触发，本轮保持停止。允许已开始请求收尾，但不会据此重新放开队列。

    def record_recovery(self, sku, summary):
        self.connection_recoveries.append({'product': sku['product_name'], 'country': sku['country'], **summary})

    def report(self):
        return {"scope": "currys_pdp", "limit": self.limit, "stopped": self.stopped,
                "stop_reason": self.stop_reason, "trigger": self.trigger,
                "consecutive_distinct_skus": len(self.consecutive_skus),
                "maximum_consecutive_distinct_skus": self.maximum_consecutive,
                "reachable_resets": self.resets, "outcomes": dict(self.outcomes),
                "requests_started": dict(self.requests), "skipped": dict(self.skipped),
                "connection_recovery": {"observations": len(self.connection_recoveries),
                                        "restored": sum(bool(row.get('target_verified')) for row in self.connection_recoveries),
                                        "entries": self.connection_recoveries}}


class CurrysAdapter(BaseAdapter):
    platform_name = "Currys"
    locale_override = ("en-GB", "Europe/London")
    wait_selectors = ("[class*='product-price']", ".price")
    batch_price_enabled = True
    navigation_wait_until = "commit"

    def batch_price_key(self, url: str) -> str:
        """Currys URL 的末尾商品 ID 稳定，slug 改名也不会影响关联。"""
        match = RE_PRODUCT_ID.search(url or "")
        return match.group(1) if match else super().batch_price_key(url)

    async def prepare_batch_prices(self, browser, skus: list[dict]) -> dict[str, tuple[float, str]]:
        """先扫电视类目页建立价格快照，减少数百次 PDP 访问和反爬触发。"""
        # 复用 weekly catalog 的“每页新 context”策略，这是目前 GitHub Actions
        # 环境里对 Currys 最稳定的访问方式。
        from catalog_scrape.adapters.currys import CurrysCatalogAdapter

        self.batch_candidate_prices = {}
        catalog_adapter = CurrysCatalogAdapter()
        self.catalog_report = {}
        try:
            items = await catalog_adapter.fetch_catalog_from_browser(browser)
        finally:
            self.catalog_report = dict(getattr(catalog_adapter, "catalog_report", {}) or {})
        price_map: dict[str, tuple[float, str]] = {}
        for item in items:
            if item.price_hint_eur is None:
                continue
            key = self.batch_price_key(item.url)
            if key:
                price_map[key] = (float(item.price_hint_eur), "GBP")

        self.batch_candidate_prices = dict(price_map)
        requested_keys = {self.batch_price_key(s["url"]) for s in skus}
        matched = sum(1 for key in requested_keys if key in price_map)
        coverage = matched / len(requested_keys) if requested_keys else 0.0
        min_items = int(os.environ.get("CURRYS_BATCH_MIN_ITEMS", "300"))
        min_coverage = float(os.environ.get("CURRYS_BATCH_MIN_COVERAGE", "0.55"))
        print(
            f"[monitor/Currys] 类目价格快照 {len(price_map)} 条，"
            f"命中 {matched}/{len(requested_keys)} ({coverage:.1%})"
        )

        # 完整性闸门：分类页异常、只加载出首屏或价格选择器失效时，整批作废，
        # 自动回退原有 PDP 抓取，避免把不完整快照误当成正常结果。
        if len(price_map) < min_items or coverage < min_coverage:
            print(
                f"[monitor/Currys] 快照未通过完整性闸门 "
                f"(items>={min_items}, coverage>={min_coverage:.0%})，回退 PDP"
            )
            return {}
        return price_map

    def is_unavailable_response(self, status: int, requested_url: str, final_url: str) -> bool:
        # 只有明确404/410可确认为链接不可用；重定向与访问拒绝不能推出型号下架。
        return super().is_unavailable_response(status, requested_url, final_url)

    def classify_response(self, status: int, requested_url: str, final_url: str, title: str = "") -> str | None:
        if status in {401, 403}:
            return "access_blocked"
        if status == 429:
            return "rate_limited"
        lowered = (title or "").lower()
        if any(marker in lowered for marker in ("attention required", "access denied", "cloudflare")):
            return "access_blocked"
        if any(marker in lowered for marker in ("just a moment", "bear with us", "security check")):
            return "challenge_unresolved"
        if status in {404, 410}:
            return "dead_link"
        if status >= 400:
            return "http_error"
        requested_key = self.batch_price_key(requested_url)
        final_key = self.batch_price_key(final_url)
        if status == 200 and "/products/" in (requested_url or "") and (
            "/products/" not in (final_url or "") or final_key != requested_key
        ):
            return "redirect_unverified"
        return None

    def is_dead_link(self, page_title: str) -> bool:
        t = (page_title or "").lower()
        if super().is_dead_link(t):
            return True
        # Currys 下架 / 缺货页标题特征
        return (
            "no longer available" in t
            or "out of stock" in t
            or "can't find" in t
            or "cannot find" in t
        )

    async def extract_price(self, page) -> tuple[float, str] | None:
        # 1) Schema / JSON-LD(Currys 标准,直接给 GBP)
        result = await get_price_from_schema(page)
        if result:
            return result

        # 2) DOM 兜底:PDP 主商品价(clean_price 自动识别 £→GBP)
        for sel in (
            "[class*='pdp-component'][class*='product-price']",
            "[data-testid*='product-price']",
            ".product-price",
        ):
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=1500):
                    text = await el.inner_text()
                    r = clean_price(text.replace("\n", " "))
                    if r:
                        return r
            except Exception:
                pass
        return None
