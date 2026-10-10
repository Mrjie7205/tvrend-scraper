"""Currys 类目页反向拉:抓 currys.co.uk 电视分类下全量商品。

抓取策略(2026-06 实测确定):
- 入口 = /tv-and-audio/televisions/tvs?start=N&sz=50(总 ~500 台, 每页 50, 约 11 页)
- Currys 是客户端渲染,plain requests 拿不到商品 → 必须用 Playwright
- current 保留基线每页 context，native 才整轮共用；配置在运行开始时冻结。
- 单页最多尝试两次；孤立失败隔离后继续，连续两页最终失败才停止。
- 429 立即停止批量；始终不在失败后切换配置或操作 CAPTCHA。
- 翻页 URL 由 Currys 自己生成:?start=0/50/100/...&sz=50。循环到某页无新增或够 total 为止。

DOM 关键点:
- 商品链接 = a[href*='/products/'],/products/<slug> 是 Currys 商品页规范 URL
- 标题已含型号:'SAMSUNG S90F 65" OLED 4K... - QE65S90F' / 'LG C5 ... - OLED65C54LA'
  → 品牌打头,型号在末尾;直接当 raw_text 交给 下游匹配环节(它按品牌正则抠型号)
- 价格在 product card 里,£NNN

匹配的脏活留给 下游匹配环节,这里只交付原始标题 + URL + 价格(英镑)。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4
from typing import Sequence

from .base import BaseCatalogAdapter, CatalogItem
from catalog_scrape.diagnostics import capture_catalog_failure
from monitor_prices.core import close_playwright_resource, get_browser_profile, new_scraper_context

LISTING_URL = "https://www.currys.co.uk/tv-and-audio/televisions/tvs"
PAGE_SIZE = 50
# 安全上限:~500/50≈10 页,留余量。测试可用环境变量 CURRYS_MAX_PAGES 调小。
MAX_PAGES = int(os.environ.get("CURRYS_MAX_PAGES", "15"))
COOKIE_ACCEPT_SELECTOR = "#onetrust-accept-btn-handler"

_JS_PAGE_STATUS = r"""() => {
  const title = (document.title || '').toLowerCase();
  const text = (document.body?.innerText || '').toLowerCase();
  const controls = [...document.querySelectorAll('input[name*="captcha" i],iframe[src*="captcha" i]')]
    .some(el => el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden');
  return {challenge: controls ||
    /access denied|just a moment|security checkpoint|robot check|captcha/.test(title) ||
    /access denied|verify you are human|security checkpoint|unusual traffic|robot check|complete (?:the )?captcha|enter the characters/.test(text)};
}"""


class CurrysCatalogIncomplete(RuntimeError):
    """分页缺口或访问拒绝只能保留观测，不代表完整周目录。"""

# 品牌识别(取标题第一个词;我们追踪 5 大,其余照样交出去由匹配器判 no_brand)
_KNOWN_BRANDS = {
    "SAMSUNG": "Samsung", "HISENSE": "Hisense", "SONY": "Sony", "TCL": "TCL", "LG": "LG",
    "PHILIPS": "Philips", "PANASONIC": "Panasonic", "TOSHIBA": "Toshiba", "JVC": "JVC",
    "SHARP": "Sharp", "BLAUPUNKT": "Blaupunkt", "HITACHI": "Hitachi", "AMAZON": "Amazon",
}
# 尺寸:65" / 55” / 75-inch(含 ASCII 与花引号)
RE_SIZE = re.compile(r"(\d{2,3})\s*(?:[\"”″'']|-?\s*inch)", re.IGNORECASE)
RE_TOTAL = re.compile(r"of\s+(\d+)", re.IGNORECASE)

# 一次性抓本页全部 (slug, 标题, 价格, href):按 slug 合并(图片链接 text 空,取最长标题)
_JS_EXTRACT = r"""
() => {
  const bySlug = {};
  document.querySelectorAll("a[href*='/products/']").forEach(a => {
    const href = a.getAttribute('href') || '';
    const m = href.match(/\/products\/([^/?#]+)/);
    if (!m) return;
    const slug = m[1];
    const title = (a.innerText || '').trim().replace(/\s+/g, ' ');
    // 不使用宽泛的 [class*='product']：它会先命中促销文案容器，把
    // “Get £30 off” 误当成售价。Currys 的完整商品卡稳定使用以下两层。
    const card = a.closest(".product-item-element, .product");
    let price = '';
    if (card) {
      const priceEl = card.querySelector(
        ".price .sales .value, .price .value, .inner-price .sales .value"
      );
      if (priceEl) price = (priceEl.textContent || '').trim();
    }
    if (!bySlug[slug]) bySlug[slug] = { slug, title: '', price: '', href: href.split('?')[0] };
    if (title.length > bySlug[slug].title.length) bySlug[slug].title = title;
    if (price && !bySlug[slug].price) bySlug[slug].price = price;
  });
  return Object.values(bySlug);
}
"""


def _brand_from_slug(slug: str) -> str:
    """品牌取 slug 第一段(干净):'samsung-s90f-...' → Samsung。
    比标题可靠 —— 标题 innerText 常被 'Save £200' / 'Get it tomorrow' 促销前缀污染。"""
    first = (slug or "").split("-", 1)[0].upper()
    return _KNOWN_BRANDS.get(first, first.title() if first else "")


def _raw_from_slug(slug: str) -> str:
    """从 slug 重建商品名:最干净的型号来源(标题 innerText 常被促销浮层污染)。
    'samsung-s90f-65-oled-...-qe65s90f-102' → 'samsung s90f 65 oled ... qe65s90f'
    (去掉尾部纯数字的商品 id)。匹配器型号正则大小写不敏感,无需还原大小写。"""
    toks = [t for t in (slug or "").split("-") if t]
    while toks and toks[-1].isdigit():  # 尾部商品 id
        toks.pop()
    return " ".join(toks)


def _size_from_slug(slug: str) -> float | None:
    """slug 里第一个落在电视尺寸区间(24-120)的 2-3 位整数(年份是 4 位不会命中)。"""
    for t in (slug or "").split("-"):
        if t.isdigit() and 2 <= len(t) <= 3:
            v = int(t)
            if 24 <= v <= 120:
                return float(v)
    return None


def _extract_size_inch(title: str) -> float | None:
    m = RE_SIZE.search(title or "")
    if m:
        v = int(m.group(1))
        if 17 <= v <= 150:
            return float(v)
    return None


def _clean_price_gbp(text: str) -> float | None:
    if not text:
        return None
    cleaned = text.replace("£", "").replace(",", "").strip()
    m = re.search(r"\d+(?:\.\d+)?", cleaned)
    if not m:
        return None
    try:
        v = float(m.group(0))
        return v if 50 <= v <= 50000 else None
    except ValueError:
        return None


class CurrysCatalogAdapter(BaseCatalogAdapter):
    platform_name = "Currys"
    country = "GB"
    locale_override = ("en-GB", "Europe/London")

    async def _new_context(self, browser):
        """配置在运行开始时冻结；current 延续每页 context，native 才整轮共用。"""
        return await new_scraper_context(browser, country=self.country, locale_override=self.locale_override)

    async def _scrape_page(self, browser, start: int) -> tuple[int, list[dict]]:
        """返回 HTTP/逻辑状态及卡片；留证发生在真正分页页面关闭之前。"""
        url = f"{LISTING_URL}?start={start}&sz={PAGE_SIZE}"
        ctx = getattr(self, '_catalog_context', None)
        owns_context = ctx is None
        page = None
        self._last_page_info = {'http_status': None, 'blocked': False, 'retryable': False}
        try:
            if owns_context:
                ctx = await self._new_context(browser)
            page = await ctx.new_page()
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=50000)
            status = resp.status if resp else 0
            self._last_page_info['http_status'] = status
            if status != 200:
                blocked = status in {401, 403, 429}
                reason = 'rate_limited' if status == 429 else 'access_challenge' if blocked else 'http_error'
                self._last_page_info.update(blocked=blocked, reason=reason,
                                            retryable=(status in {0, 403, 408} or status >= 500))
                await capture_catalog_failure(
                    page, platform=self.platform_name, country=self.country,
                    stage='catalog_page', reason=reason, url=url,
                    http_status=status, adapter=self,
                )
                return status, []
            await page.wait_for_timeout(2800)
            state = await page.evaluate(_JS_PAGE_STATUS)
            blocked = status in {401, 403, 429} or (isinstance(state, dict) and state.get('challenge') is True)
            if blocked:
                self._last_page_info.update(blocked=True, retryable=True, reason='access_challenge')
                await capture_catalog_failure(
                    page, platform=self.platform_name, country=self.country,
                    stage='catalog_page', reason=self._last_page_info['reason'],
                    url=url, http_status=status, adapter=self,
                )
                return status if status in {401, 403, 429} else 403, []
            try:
                if await page.is_visible(COOKIE_ACCEPT_SELECTOR, timeout=1200):
                    await page.click(COOKIE_ACCEPT_SELECTOR)
                    await page.wait_for_timeout(500)
            except Exception:
                pass
            cards = await page.evaluate(_JS_EXTRACT)
            if not cards:
                # 正常页一次短等只处理渲染延迟，不导航或清会话。
                await page.wait_for_timeout(1200)
                cards = await page.evaluate(_JS_EXTRACT)
            return status, cards or []
        except Exception as error:
            self._last_page_info.update(reason='navigation_or_extraction_error',
                                        error_type=type(error).__name__, retryable=isinstance(error, TimeoutError) or 'Timeout' in type(error).__name__)
            # 网络连接异常同样允许最终补抓一次；程序/解析错误不盲目重试。
            if 'net::ERR_' in str(error):
                self._last_page_info['retryable'] = True
            await capture_catalog_failure(
                page, platform=self.platform_name, country=self.country,
                stage='catalog_page', reason='navigation_or_extraction_error',
                url=url, error=error, adapter=self,
            )
            print(f"    [Currys] start={start} 异常: {type(error).__name__}")
            return 0, []
        finally:
            if owns_context:
                await close_playwright_resource(ctx, f"Currys catalog page {start} context")
            else:
                await close_playwright_resource(page, f"Currys catalog page {start}")

    def _save_catalog_report(self):
        """分页账本隔离保存，不进入正式目录和价格历史。"""
        try:
            self._catalog_report_path.parent.mkdir(parents=True, exist_ok=True)
            temp = self._catalog_report_path.with_suffix('.tmp')
            temp.write_text(json.dumps(self.catalog_report, ensure_ascii=False, indent=2), encoding='utf-8')
            temp.replace(self._catalog_report_path)
        except OSError:
            pass

    async def fetch_catalog_from_browser(self, browser) -> Sequence[CatalogItem]:
        """返回明确商品观测；日价仍须自己的数量/历史门禁，周目录另验 complete。"""
        by_slug = {}
        now = datetime.now(UTC)
        root = Path(os.environ.get('CURRYS_CATALOG_DIAGNOSTICS_DIR',
                                  str(Path(__file__).resolve().parents[2] / 'catalog_artifacts')))
        run_name = now.strftime('%Y%m%dT%H%M%SZ') + '-' + uuid4().hex[:8]
        self._catalog_report_path = root / 'currys_gb' / run_name / 'report.json'
        self.catalog_report = {
            'schema_version': 1, 'platform': self.platform_name, 'country': self.country,
            'started_at': now.isoformat(), 'complete': False, 'blocked': False,
            'had_access_denials': False, 'rate_limited': False, 'max_attempts_per_page': 2,
            'pages': {}, 'missing_pages': [], 'end_observed': False,
            'notice': '明确商品观测不等于完整目录；失败/缺页数据不得冒充完整周目录。',
        }
        self._save_catalog_report()

        seen_page_signatures = set()

        async def attempt(start):
            row = self.catalog_report['pages'].setdefault(str(start), {'start': start, 'attempts': []})
            if len(row['attempts']) >= 2:
                raise RuntimeError(f'Currys start={start} 已达到本轮两次尝试上限')
            self._last_page_info = {}
            status, cards = await self._scrape_page(browser, start)
            info = dict(self._last_page_info)
            rate_limited = status == 429 or info.get('reason') == 'rate_limited'
            denied = not rate_limited and (bool(info.get('blocked')) or status in {401, 403})
            retryable = not rate_limited and (status in {0, 403, 408} or status >= 500)
            event = {'attempt': len(row['attempts']) + 1, 'status': status,
                     'http_status': info.get('http_status', status), 'card_count': len(cards),
                     'blocked': denied, 'rate_limited': rate_limited,
                     'retryable': retryable, 'reason': info.get('reason'),
                     'error_type': info.get('error_type'), 'observed_at': datetime.now(UTC).isoformat()}
            row['attempts'].append(event)
            row.update(status=status, missing=status != 200, access_denied=denied,
                       retryable=retryable and len(row['attempts']) < 2)
            for card in cards if status == 200 else []:
                slug = card.get('slug')
                if slug and len((card.get('title') or '').strip()) >= 8 and slug not in by_slug:
                    by_slug[slug] = card
            self.catalog_report['had_access_denials'] |= denied
            self.catalog_report['rate_limited'] |= rate_limited
            self._save_catalog_report()
            return status, cards, row

        self._catalog_context = None
        try:
            if get_browser_profile(browser) == 'native':
                self._catalog_context = await self._new_context(browser)
            consecutive_failed = consecutive_denied = 0
            for index in range(MAX_PAGES):
                start = index * PAGE_SIZE
                status, cards, row = await attempt(start)
                # 恢复旧的有界页内重试：每页总共至多两次，不再追加第三次补抓。
                if row['retryable'] and not self.catalog_report['rate_limited']:
                    await asyncio.sleep(2.5)
                    status, cards, row = await attempt(start)
                if self.catalog_report['rate_limited']:
                    self.catalog_report['termination'] = 'rate_limited'
                    break
                if status == 200:
                    consecutive_failed = consecutive_denied = 0
                    if not cards:
                        self.catalog_report['end_observed'] = start > 0
                        self.catalog_report['termination'] = 'empty_page_after_bounded_wait'
                        break
                    signature = tuple(sorted({card.get('slug') for card in cards if card.get('slug')}))
                    if signature and signature in seen_page_signatures:
                        # 重复返回旧页不能证明已经遍历完整目录。
                        row.update(missing=True, retryable=False, reason='repeated_page')
                        self.catalog_report['termination'] = 'repeated_page'
                        break
                    seen_page_signatures.add(signature)
                else:
                    consecutive_failed += 1
                    consecutive_denied = consecutive_denied + 1 if row['access_denied'] else 0
                    if consecutive_failed >= 2:
                        self.catalog_report['blocked'] = consecutive_denied >= 2
                        self.catalog_report['termination'] = (
                            'consecutive_access_denials' if self.catalog_report['blocked']
                            else 'consecutive_failed_pages'
                        )
                        break
                await asyncio.sleep(1.5)
            else:
                self.catalog_report['termination'] = 'page_limit'
        finally:
            await close_playwright_resource(self._catalog_context, 'Currys catalog shared context')
            self._catalog_context = None
            self.catalog_report['missing_pages'] = sorted(
                row['start'] for row in self.catalog_report['pages'].values() if row['missing']
            )
            self.catalog_report['complete'] = bool(
                self.catalog_report['end_observed'] and not self.catalog_report['missing_pages']
                and not self.catalog_report['blocked'] and not self.catalog_report['rate_limited']
            )
            self.catalog_report['finished_at'] = datetime.now(UTC).isoformat()
            self.catalog_report['observed_items'] = len(by_slug)
            self._save_catalog_report()
        return self._build_items(by_slug)

    async def fetch_catalog(self, page) -> Sequence[CatalogItem]:
        items = await self.fetch_catalog_from_browser(page.context.browser)
        if not self.catalog_report['complete']:
            error = CurrysCatalogIncomplete(
                f"Currys 目录不完整: missing_pages={self.catalog_report['missing_pages']}, "
                f"blocked={self.catalog_report['blocked']}, end_observed={self.catalog_report['end_observed']}"
            )
            previous = getattr(self, '_failure_evidence_path', None)
            if previous is not None:
                error._catalog_evidence_path = previous
            else:
                await capture_catalog_failure(
                    None, platform=self.platform_name, country=self.country,
                    stage='catalog_completeness', reason='incomplete_catalog', error=error, adapter=self,
                )
            raise error
        return items

    def _build_items(self, by_slug: dict[str, dict]) -> list[CatalogItem]:
        items: list[CatalogItem] = []
        for slug, c in by_slug.items():
            brand = _brand_from_slug(slug)
            raw = _raw_from_slug(slug)  # slug 重建,干净且含型号
            size = _size_from_slug(slug) or _extract_size_inch(c.get("title") or "")
            href = c.get("href") or ""
            if not href.startswith("http"):
                href = "https://www.currys.co.uk" + href
            items.append(CatalogItem(
                brand_raw=brand,
                raw_text=raw,
                url=href,
                size_hint_inch=size,
                price_hint_eur=_clean_price_gbp(c.get("price") or ""),  # 注:Currys 是 GBP,字段名沿用 schema
            ))
        n_priced = sum(1 for it in items if it.price_hint_eur is not None)
        print(f"[catalog/Currys] {len(items)} 商品({n_priced} 带价格)")
        return items
