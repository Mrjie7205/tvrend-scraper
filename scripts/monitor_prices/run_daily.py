"""每日价格抓取主入口。

流程:
  1. 读 channel_links.csv(active=true)→ SKU 清单
  2. 并发(默认 3 路)开 Playwright context,每个 SKU 一个独立指纹
  3. 找到 platform 对应的 adapter,跑 extract_price
  4. 算 price_trend(降价/涨价/持平/新上线)
  5. 批量追加进 raw/prices.csv

GitHub Actions 调用方式:
  python -m monitor_prices.run_daily        (默认 headless)
  HEADLESS_MODE=false python -m monitor_prices.run_daily   (本地调试)

local 调用方式:
  cd 1-Data/Channel-Prices/scripts
  python -m monitor_prices.run_daily
"""
from __future__ import annotations

import asyncio
import os
import random
import re
import statistics
import sys
from datetime import datetime
from pathlib import Path

# 让 `python -m monitor_prices.run_daily` 在 scripts/ 工作目录下可用
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from monitor_prices.core import (  # noqa: E402
    DEFAULT_LOCALE,
    channels_in_scope,
    close_playwright_resource,
    handle_antibot_page,
    platform_in_scope,
)
from monitor_prices.checkpoint import reset_checkpoint, write_checkpoint  # noqa: E402
from monitor_prices.prices_io import (  # noqa: E402
    append_prices,
    compute_price_trend,
    load_active_skus,
    load_latest_historical_prices,
    trim_prices_window,
)
from monitor_prices.adapters import get_adapter, supported_platforms  # noqa: E402
from monitor_prices.adapters.currys import CurrysPdpGuard  # noqa: E402
from failure_evidence import capture_failure, record_failure  # noqa: E402
from failure_evidence import _atomic_json, _bounded  # noqa: E402
from price_anomalies import classify_change, record_price_change, attach_price_verification, update_price_change_status, summarize  # noqa: E402
from monitor_prices.prices_io import FrozenPriceHistory, load_latest_historical_observations  # noqa: E402
from monitor_prices.core import launch_scraper_browser, new_scraper_context  # noqa: E402
from monitor_prices.core import ANTIBOT_TITLE_MARKERS  # noqa: E402
from datetime import timezone
from collections import Counter
import json
from decimal import Decimal, InvalidOperation

CONCURRENCY = int(os.environ.get("MONITOR_CONCURRENCY", "3"))
HEADLESS = os.environ.get("HEADLESS_MODE", "true").lower() != "false"
MAX_SKUS = int(os.environ.get("MONITOR_MAX_SKUS", "0") or "0")
SKU_TIMEOUT_SECONDS = float(os.environ.get("MONITOR_SKU_TIMEOUT_SECONDS", "120") or "120")

def _safe_filename(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", s or "unknown")[:64]


def _batch_prices_pass_history_guard(adapter, skus: list[dict], prices: dict, hist: dict) -> bool:
    """用最近一次成功价拦截系统性错位（优惠额、月供被当售价等）。"""
    ratios: list[float] = []
    for sku in skus:
        price_data = prices.get(adapter.batch_price_key(sku["url"]))
        old = hist.get(f"{sku['product_name']}_{sku['country']}_{sku['platform']}")
        if not price_data or not old or old <= 0:
            continue
        ratios.append(float(price_data[0]) / float(old))
    if len(ratios) < 20:
        print(f"[monitor/{adapter.platform_name}] 历史价守门样本 {len(ratios)} 条，不足 20，跳过比对")
        return True

    median_ratio = statistics.median(ratios)
    extreme_share = sum(r < 0.4 or r > 2.5 for r in ratios) / len(ratios)
    passed = 0.75 <= median_ratio <= 1.35 and extreme_share <= 0.05
    print(
        f"[monitor/{adapter.platform_name}] 历史价守门: n={len(ratios)} "
        f"median={median_ratio:.3f}, extreme={extreme_share:.1%}, "
        f"{'通过' if passed else '拒绝'}"
    )
    return passed


def _batch_price_outlier_keys(adapter, skus: list[dict], prices: dict, hist: dict) -> set[str]:
    """找出需要改走商品详情页复核的单品。

    整批守门只能发现大面积错位；少数其他尺寸/替代型号串价可能不超过
    5% 占比。这里不删除真实大促，只把跳幅过大的 batch 价从快速映射中移除，
    后续流程会自动打开该 SKU 详情页，以 JSON-LD/主价格复核。
    """
    lower = float(os.environ.get("BATCH_SINGLE_LOWER_RATIO", "0.65"))
    upper = float(os.environ.get("BATCH_SINGLE_UPPER_RATIO", "1.75"))
    outliers: set[str] = set()
    for sku in skus:
        key = adapter.batch_price_key(sku["url"])
        price_data = prices.get(key)
        old = hist.get(f"{sku['product_name']}_{sku['country']}_{sku['platform']}")
        if not key or not price_data or not old or old <= 0:
            continue
        ratio = float(price_data[0]) / float(old)
        if ratio < lower or ratio > upper:
            outliers.add(key)
            print(
                f"[monitor/{adapter.platform_name}] 单品价格跳变转 PDP 复核: "
                f"{sku['product_name']} old={old:g}, batch={float(price_data[0]):g}, "
                f"ratio={ratio:.3f}"
            )
    return outliers


async def _new_context(browser, adapter, country: str):
    """按渠道地区创建浏览器会话，供单 SKU 或共享会话渠道复用。"""
    ctx = await new_scraper_context(browser, country=country, locale_override=adapter.locale_override)
    if getattr(adapter, "context_cookies", ()):
        try:
            await ctx.add_cookies(list(adapter.context_cookies))
        except Exception as e:
            print(f"  [{adapter.platform_name}] 注入 context_cookies 失败: {str(e)[:80]}")
    return ctx


def _candidate_key(sku):
    return tuple(sku[key] for key in ("product_name", "country", "platform", "url"))


def _currys_pdp_guard(hist, sku):
    return getattr(hist, "currys_pdp_guard", None) if sku["platform"].lower() == "currys" else None


def _catalog_blocks_pdp(adapter, sku):
    # Currys目录失败只说明目录阶段；其PDP由独立的真实访问结果保护。
    report = getattr(adapter, "catalog_report", {})
    return bool(report.get("rate_limited")) if sku["platform"].lower() == "currys" else bool(report.get("blocked"))


def _same_candidate_price(candidate, result):
    try:
        return (result.get("Status") == "Success" and result.get("Currency") == candidate["currency"]
                and Decimal(str(result.get("Price"))) > 0
                and Decimal(str(result["Price"])) == Decimal(str(candidate["price"])))
    except (InvalidOperation, ValueError, TypeError):
        return False


async def _retain_batch_candidates(adapter, skus, prices, hist, *, guard):
    """门禁弃用前只留重大波动候选，不改变正式价格映射，也不再访问站点。"""
    pending = getattr(hist, "pending_candidates", None)
    if pending is None:
        hist.pending_candidates = pending = {}
    observed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for sku in skus:
        price_data = prices.get(adapter.batch_price_key(sku["url"]))
        if not price_data or _candidate_key(sku) in pending:
            continue
        price, currency = price_data
        baseline = getattr(hist, "observations", {}).get((sku["product_name"], sku["country"], sku["platform"], str(currency).upper()))
        if not baseline or not classify_change(baseline.get("price"), price, old_currency=baseline.get("currency"), currency=currency):
            continue
        path = await record_price_change(
            baseline=baseline,
            observation={"price": price, "currency": currency, "observed_at": observed_at,
                         "platform": sku["platform"], "country": sku["country"], "product": sku["product_name"],
                         "url": sku["url"], "listing_id": adapter.batch_price_key(sku["url"]),
                         "observation_source": "batch_catalog_candidate", "ingestion_status": "pending",
                         "validation_state": "unvalidated"},
            evidence_source="batch_candidate_awaiting_existing_pdp",
            verification={"status": "pending_existing_pdp", "guard": guard},
        )
        if path is not None:
            pending[_candidate_key(sku)] = {"path": path, "price": price, "currency": currency, "guard": guard}


async def _finalize_batch_candidate(sku, hist, result, *, page=None, status=None, reason=None):
    candidate = getattr(hist, "pending_candidates", {}).get(_candidate_key(sku))
    if not candidate or candidate.get("accepted"):
        return
    try:
        matched = _same_candidate_price(candidate, result)
        success = result.get("Status") == "Success"
        verification = {"status": "matching_pdp_price" if matched else "different_pdp_price" if success else reason or result.get("Status", "pdp_unavailable"),
                        "url": getattr(page, "url", None), "http_status": status, "guard": candidate["guard"],
                        "observed_at": datetime.now(timezone.utc).isoformat()}
        if success:
            verification.update(price=result.get("Price"), currency=result.get("Currency"))
        # 复用已经访问过、尚未关闭的PDP，不能为一个候选再发第二次访问。
        await attach_price_verification(candidate["path"], page=page, verification=verification,
                                        evidence_source="existing_pdp_verification" if page is not None else "existing_pdp_unavailable")
        update_price_change_status(candidate["path"], ingestion_status="accepted" if matched else "rejected",
                                   reason="pdp_confirms_candidate" if matched else "pdp_differs_from_candidate" if success else "pdp_verification_unavailable")
        candidate["accepted"] = matched
    except (Exception, asyncio.CancelledError) as exc:
        update_price_change_status(candidate["path"], ingestion_status="not_published", reason="candidate_verification_interrupted")
        print(json.dumps({"event": "batch_candidate_evidence_unavailable", "error_type": type(exc).__name__}))


async def _observe_success(result, sku, hist, adapter, browser, *, page=None, context=None, source="product_page"):
    """最终成功价先固定时间；价格留证旁路失败不得把已取得的报价改成失败。"""
    stamp = datetime.now(timezone.utc)
    result["Date"], result["Time"] = stamp.strftime("%Y-%m-%d"), stamp.strftime("%H:%M:%S")
    candidate = getattr(hist, "pending_candidates", {}).get(_candidate_key(sku))
    if page is not None and candidate and _same_candidate_price(candidate, result):
        return  # 同价PDP通过同一个候选事件确认，finally会留同页截图及独立复核时间。
    baseline = getattr(hist, "observations", {}).get((sku["product_name"], sku["country"], sku["platform"], str(result["Currency"]).upper()))
    if not baseline or not classify_change(baseline["price"], result["Price"], old_currency=baseline["currency"], currency=result["Currency"]):
        return
    observation = {"price": result["Price"], "currency": result["Currency"],
                   "observed_at": f"{result['Date']}T{result['Time']}+00:00",
                   "platform": sku["platform"], "country": sku["country"], "product": sku["product_name"],
                   "url": sku["url"], "listing_id": adapter.batch_price_key(sku["url"]),
                   "source_page_url": getattr(page, "url", None) if page is not None else None,
                   "observation_source": source, "ingestion_status": "accepted"}
    verification_page, verification_context = None, None
    event_path = None
    try:
        event_path = await record_price_change(baseline=baseline, observation=observation, page=page,
                                               evidence_source="same_product_page" if page is not None else "pdp_verification_pending")
        if page is not None or event_path is None:
            return
        if json.loads(event_path.read_text(encoding="utf-8")).get("verification") is not None:
            return  # 同一观测已复核，不重复打开页面。
        guard = _currys_pdp_guard(hist, sku)
        if _catalog_blocks_pdp(adapter, sku) or (guard is not None and guard.stopped):
            skip_reason = (guard.record_skip("price_verification") if guard is not None and guard.stopped
                           else "not_attempted_catalog_rate_limited" if sku["platform"].lower() == "currys" else "not_attempted_channel_blocked")
            await attach_price_verification(event_path, verification={"status": skip_reason},
                                            evidence_source="pdp_verification_not_started")
            return
        verification = {"status": "not_attempted", "url": sku["url"]}

        async def verify_once():
            nonlocal verification_page, verification_context, verification
            ctx = context
            if ctx is None:
                verification_context = await _new_context(browser, adapter, sku["country"])
                ctx = verification_context
            verification_page = await ctx.new_page()
            if guard is not None and not guard.start_request("price_verification"):
                verification = {"status": guard.record_skip("price_verification"), "url": sku["url"]}
                return
            response = await verification_page.goto(sku["url"], wait_until="domcontentloaded", timeout=10000)
            status = response.status if response else 0
            if sku["platform"].lower() == "currys" and status >= 400:
                reason = adapter.classify_response(status, sku["url"], verification_page.url) or "http_error"
                if guard is not None:
                    guard.observe(sku, http_status=status, reason=reason)
                verification = {"status": reason, "http_status": status, "url": verification_page.url}
                return
            title = await verification_page.title()
            reason = adapter.classify_response(status, sku["url"], verification_page.url, title) if hasattr(adapter, "classify_response") else None
            if not reason and any(marker in title.lower() for marker in ANTIBOT_TITLE_MARKERS):
                reason = "challenge_unresolved"
            if status in {403, 429} or reason:
                if guard is not None:
                    guard.observe(sku, http_status=status, reason=reason or "access_blocked")
                verification = {"status": reason or "access_blocked", "http_status": status, "url": verification_page.url}
                return
            if status != 200 or adapter.is_unavailable_response(status, sku["url"], verification_page.url):
                if guard is not None:
                    guard.observe(sku, http_status=status, reason="page_unavailable")
                verification = {"status": "page_unavailable", "http_status": status, "url": verification_page.url}
                return
            if guard is not None:
                guard.observe(sku, http_status=status)
            price = await adapter.extract_price(verification_page)
            verification = {"status": "price_observed" if price else "price_not_found", "http_status": status,
                            "url": verification_page.url, "observed_at": datetime.now(timezone.utc).isoformat()}
            if price:
                verification.update(price=float(price[0]), currency=price[1])
        try:
            await _bounded(verify_once(), 12.0)
        except (Exception, asyncio.CancelledError) as exc:
            if guard is not None:
                guard.observe(sku, reason="verification_unavailable")
            verification = {"status": "verification_timeout" if isinstance(exc, (asyncio.TimeoutError, asyncio.CancelledError)) else "verification_failed",
                            "error_type": type(exc).__name__, "url": sku["url"]}
        await attach_price_verification(event_path, page=verification_page, verification=verification)
    except (Exception, asyncio.CancelledError) as exc:
        print(json.dumps({"event": "price_evidence_unavailable", "error_type": type(exc).__name__, "price_preserved": True}))
    finally:
        for resource, label in ((verification_page, "price verification page"), (verification_context, "price verification context")):
            if resource is not None:
                try:
                    await close_playwright_resource(resource, label, timeout_seconds=1)
                except (Exception, asyncio.CancelledError) as exc:
                    # SKU预算恰在取证清理时耗尽，也不能把已经获得的有效API/batch价变成空价。
                    print(json.dumps({"event": "price_evidence_cleanup_failed", "error_type": type(exc).__name__, "price_preserved": True}))


async def process_sku(
    sem,
    browser,
    sku: dict,
    hist: dict,
    shared_context=None,
    batch_prices: dict[str, tuple[float, str]] | None = None,
) -> dict:
    """抓一个 SKU 的价格,返回结果 dict(供 append_prices 写入)。"""
    async with sem:
        url = sku["url"]
        name = sku["product_name"]
        brand = sku["brand"]
        platform = sku["platform"]
        country = sku["country"]

        result = {
            "Brand": brand,
            "Product Name": name,
            "Country": country,
            "Platform": platform,
            "Price": None,
            "Currency": None,
            "Page Title": "",
            "Status": "Pending",
            "Price_Trend": "-",
        }

        adapter = get_adapter(platform)
        if adapter is None:
            # 不支持的渠道暂时跳过(不写日志,避免 prices.csv 灌入大量 Failed)
            print(f"  [skip] {platform} 暂未实现 adapter ({name})")
            result["Status"] = "Skipped: Unsupported Platform"
            return result

        print(f"\n→ [{country}] {name} ({platform})")

        # 类目价格快照命中时，无需创建 context 或打开 PDP。
        batch_key = adapter.batch_price_key(url)
        if batch_prices and batch_key in batch_prices:
            new_price, currency = batch_prices[batch_key]
            result["Price"] = new_price
            result["Currency"] = currency
            result["Status"] = "Success"
            result["Page Title"] = "Batch category snapshot"
            result["Price_Trend"] = compute_price_trend(name, country, platform, new_price, hist)
            print(f"  [ok/batch] {currency} {new_price} ({result['Price_Trend']})")
            await _observe_success(result, sku, hist, adapter, browser, source="batch_catalog")
            return result

        guard = _currys_pdp_guard(hist, sku)
        if _catalog_blocks_pdp(adapter, sku) or (guard is not None and guard.stopped):
            skip_reason = (guard.record_skip() if guard is not None and guard.stopped
                           else "pdp_rate_limited" if platform.lower() == "currys" else "channel_access_blocked")
            result["Status"] = f"Failed: {skip_reason}"
            record_failure(platform=platform, country=country, stage="blocked_before_pdp",
                           reason=skip_reason, url=url, product=name)
            await _finalize_batch_candidate(sku, hist, result, reason=skip_reason)
            return result

        ctx = shared_context
        owns_context = shared_context is None
        page = None
        failure_reason = None
        failure_error = None
        stage = "create_context"
        status = None
        try:
            if owns_context:
                ctx = await _new_context(browser, adapter, country)

            if getattr(adapter, "direct_price_enabled", False):
                try:
                    price_data = await adapter.extract_price_direct(url, ctx.request)
                except Exception as e:
                    print(f"  [{name}] direct API 异常，回退页面抓取: {str(e)[:100]}")
                    record_failure(platform=platform, country=country, stage="direct_api",
                                   reason="direct_api_error", url=url, product=name, error=e,
                                   browser_available=False)
                    price_data = None
                if price_data:
                    new_price, currency = price_data
                    result["Price"] = new_price
                    result["Currency"] = currency
                    result["Status"] = "Success"
                    result["Page Title"] = "Direct API"
                    result["Price_Trend"] = compute_price_trend(name, country, platform, new_price, hist)
                    print(f"  [ok/api] {currency} {new_price} ({result['Price_Trend']})")
                    await _observe_success(result, sku, hist, adapter, browser, context=ctx, source="direct_api")
                    return result
                print(f"  [{name}] direct API 无价，回退页面抓取")

            stage = "create_page"
            page = await ctx.new_page()

            # 导航(2 次重试 + 反爬等待)
            MAX_RETRIES = 2
            price_data = None
            for attempt in range(MAX_RETRIES):
                try:
                    stage = "navigate"
                    await asyncio.sleep(random.uniform(1.0, 3.0))
                    if guard is not None and not guard.start_request():
                        failure_reason = guard.record_skip()
                        result["Status"] = f"Failed: {failure_reason}"
                        break
                    timeout_ms = 40000 if attempt == 0 else 60000
                    wait_until = getattr(adapter, "navigation_wait_until", "domcontentloaded")
                    response = await page.goto(url, wait_until=wait_until, timeout=timeout_ms)
                    status = response.status if response else 0
                    if status >= 400:
                        # 所有渠道的错误HTTP先归类；错误页面不能继续进入价格选择器。
                        failure_reason = ({403: "access_blocked", 429: "rate_limited", 404: "dead_link", 410: "dead_link"}
                                          .get(status, "http_error"))
                        if platform.lower() == "currys" and status == 401:
                            failure_reason = "access_blocked"
                        result["Status"] = f"Failed: {failure_reason}"
                        result["Page Title"] = f"HTTP {status} → {page.url}"
                        if guard is not None:
                            guard.observe(sku, http_status=status, reason=failure_reason)
                        break
                    if platform.lower() != "currys" and adapter.is_unavailable_response(status, url, page.url):
                        result["Status"] = "Failed: Dead Link"
                        result["Page Title"] = f"HTTP {status} → {page.url}"
                        print(f"  [{name}] 死链/下架: HTTP {status} → {page.url[:100]}")
                        failure_reason = "dead_link"
                        break
                    if wait_until == "commit":
                        try:
                            await page.wait_for_load_state(
                                "domcontentloaded",
                                timeout=getattr(adapter, "post_commit_timeout_ms", 15000),
                            )
                        except Exception:
                            # HTTP 状态和最终 URL 已拿到；重页面继续由反爬检测和价格选择器判断。
                            pass
                    if platform.lower() == "currys":
                        reason = adapter.classify_response(status, url, page.url, await page.title())
                        if reason:
                            result["Status"] = f"Failed: {reason}"
                            failure_reason = reason
                            if guard is not None:
                                guard.observe(sku, http_status=status, reason=reason)
                            break
                    passed = await handle_antibot_page(
                        page,
                        name,
                        max_waits=getattr(adapter, "antibot_max_waits", 4),
                        wait_seconds=getattr(adapter, "antibot_wait_seconds", 5.0),
                    )
                    if not passed:
                        if platform.lower() == "currys":
                            result["Status"] = "Failed: challenge_unresolved"
                            failure_reason = "challenge_unresolved"
                            if guard is not None:
                                guard.observe(sku, http_status=status, reason=failure_reason)
                            break
                        raise RuntimeError("反爬验证等待超时")
                    if guard is not None:
                        guard.observe(sku, http_status=status)
                except Exception as e:
                    if guard is not None:
                        guard.observe(sku, reason="navigation_unavailable")
                    print(f"  [{name}] 导航异常 ({attempt + 1}/{MAX_RETRIES}): {str(e)[:80]}")
                    if attempt < MAX_RETRIES - 1:
                        continue
                    result["Status"] = "Failed: Navigation Error"
                    failure_reason = "navigation_timeout" if isinstance(e, (asyncio.TimeoutError, TimeoutError)) or "Timeout" in type(e).__name__ else "navigation_error"
                    if platform.lower() == "currys":
                        result["Status"] = f"Failed: {failure_reason}"
                    failure_error = e
                    break

                # 死链 / 缺货
                stage = "page_identity"
                page_title = (await page.title()) or ""
                if adapter.is_dead_link(page_title):
                    print(f"  [{name}] 死链/下架: {page_title[:80]}")
                    result["Status"] = "Failed: Dead Link"
                    result["Page Title"] = page_title
                    failure_reason = "dead_link"
                    break

                # cookie 弹窗(轻量)
                for sel in adapter.cookie_accept_selectors:
                    try:
                        if await page.is_visible(sel, timeout=1500):
                            await page.click(sel)
                    except Exception:
                        pass

                # 等待价格元素
                for sel in adapter.wait_selectors:
                    try:
                        await page.wait_for_selector(sel, timeout=5000)
                        break
                    except Exception:
                        continue

                # 价格提取
                stage = "extract_price"
                price_data = await adapter.extract_price(page)
                if price_data:
                    new_price, currency = price_data
                    result["Price"] = new_price
                    result["Currency"] = currency
                    result["Status"] = "Success"
                    result["Page Title"] = (await page.title()) or ""
                    result["Price_Trend"] = compute_price_trend(name, country, platform, new_price, hist)
                    print(f"  [ok] {currency} {new_price} ({result['Price_Trend']})")
                    await _observe_success(result, sku, hist, adapter, browser, page=page, context=ctx)
                    break
                else:
                    if attempt < MAX_RETRIES - 1:
                        await asyncio.sleep(2)
                        continue
                    result["Status"] = "Failed: Price Not Found"
                    failure_reason = "price_not_found"
                    print(f"  [{name}] 价格未找到")

        except asyncio.CancelledError as e:
            failure_reason = "sku_cancelled"
            failure_error = e
            raise
        except Exception as e:
            print(f"  [{name}] 严重异常: {str(e)[:120]}")
            result["Status"] = f"Failed: Critical {str(e)[:50]}"
            failure_reason = "critical_error"
            failure_error = e
        finally:
            await _finalize_batch_candidate(sku, hist, result, page=page, status=status, reason=failure_reason)
            try:
                if failure_reason:
                    await capture_failure(page, platform=platform, country=country, stage=stage,
                                          reason=failure_reason, url=url, http_status=status,
                                          product=name, error=failure_error,
                                          listing_id=adapter.batch_price_key(url),
                                          timeout_seconds=1.0 if failure_reason == "sku_cancelled" else None)
            except (Exception, asyncio.CancelledError):
                # 即使诊断被取消，仍先清理资源，再由原始异常/状态决定抓取结果。
                pass
            if page:
                await close_playwright_resource(page, f"{name} page")
            if owns_context and ctx:
                await close_playwright_resource(ctx, f"{name} context")

        return result


async def process_sku_bounded(
    sem,
    browser,
    sku: dict,
    hist: dict,
    shared_context=None,
    batch_prices: dict[str, tuple[float, str]] | None = None,
) -> dict:
    """等待并发位后，再给单个 SKU 的完整抓取设置硬时限。

    时限不包含排队时间，避免 SKU 数量多时还没轮到执行就被误判超时。
    内层传入独立 semaphore，是为了复用原有 process_sku 逻辑而不二次占用并发位。
    """
    async with sem:
        try:
            return await asyncio.wait_for(
                process_sku(
                    asyncio.Semaphore(1),
                    browser,
                    sku,
                    hist,
                    shared_context=shared_context,
                    batch_prices=batch_prices,
                ),
                timeout=max(0.01, SKU_TIMEOUT_SECONDS),
            )
        except asyncio.TimeoutError:
            print(
                f"  [{sku['product_name']}] 单 SKU 总耗时超过 "
                f"{SKU_TIMEOUT_SECONDS:g}s，释放并发位并继续"
            )
            record_failure(platform=sku["platform"], country=sku["country"], stage="sku_timeout",
                           reason="sku_timeout", url=sku["url"], product=sku["product_name"])
            return {
                "Brand": sku["brand"],
                "Product Name": sku["product_name"],
                "Country": sku["country"],
                "Platform": sku["platform"],
                "Price": None,
                "Currency": None,
                "Page Title": "",
                "Status": "Failed: SKU Timeout",
                "Price_Trend": "-",
            }
        except asyncio.CancelledError as exc:
            record_failure(platform=sku["platform"], country=sku["country"], stage="sku_task",
                           reason="task_cancelled", url=sku["url"], product=sku["product_name"], error=exc)
            raise


async def process_shared_group(browser, adapter, skus: list[dict], hist: dict) -> list[dict]:
    """同一渠道串行复用 context，保留 Vercel 等验证产生的会话状态。"""
    ctx = None
    country = skus[0]["country"]
    try:
        ctx = await _new_context(browser, adapter, country)
        if adapter.warmup_url:
            page = None
            warmup_reason = None
            warmup_error = None
            try:
                page = await ctx.new_page()
                print(f"\n[monitor/{adapter.platform_name}] 预热共享会话: {adapter.warmup_url}")
                try:
                    await page.goto(adapter.warmup_url, wait_until="domcontentloaded", timeout=120000)
                except Exception as exc:
                    print(f"[monitor/{adapter.platform_name}] 预热导航提示: {str(exc)[:100]}")
                    warmup_reason, warmup_error = "warmup_navigation_error", exc
                passed = await handle_antibot_page(
                    page,
                    f"{adapter.platform_name} warmup",
                    max_waits=adapter.antibot_max_waits,
                    wait_seconds=adapter.antibot_wait_seconds,
                )
                if not passed:
                    print(f"[monitor/{adapter.platform_name}] 预热验证未通过，仍继续商品页测试")
                    warmup_reason = "warmup_antibot_failed"
            except (Exception, asyncio.CancelledError) as exc:
                warmup_reason, warmup_error = "warmup_failed", exc
                raise
            finally:
                try:
                    if warmup_reason:
                        await capture_failure(page, platform=adapter.platform_name, country=country,
                                              stage="shared_warmup", reason=warmup_reason,
                                              url=adapter.warmup_url, error=warmup_error)
                except (Exception, asyncio.CancelledError):
                    pass
                if page:
                    await close_playwright_resource(page, f"{adapter.platform_name} warmup page")

        serial_sem = asyncio.Semaphore(1)
        results = []
        for sku in skus:
            results.append(
                await process_sku_bounded(serial_sem, browser, sku, hist, shared_context=ctx)
            )
        return results
    except (Exception, asyncio.CancelledError) as exc:
        record_failure(platform=adapter.platform_name, country=country, stage="shared_context",
                       reason="shared_group_failed", error=exc, browser_available=ctx is not None)
        raise
    finally:
        if ctx:
            await close_playwright_resource(ctx, f"{adapter.platform_name} shared context")


async def run() -> int:
    checkpoint_path = reset_checkpoint()
    print(f"[monitor] supported adapters: {supported_platforms()}")
    print(f"[monitor] 增量检查点: {checkpoint_path}")
    skus = load_active_skus()
    if not skus:
        print("[monitor] 无 active SKU,退出")
        return 0

    # CHANNELS 白名单过滤(不设/空 = 全跑)。自动 action 用它排除 Amazon。
    scope = channels_in_scope()
    if scope is not None:
        before = len(skus)
        skus = [s for s in skus if platform_in_scope(s["platform"], scope)]
        print(f"[monitor] CHANNELS={sorted(scope)} → {len(skus)}/{before} SKU 入选")
        if not skus:
            print("[monitor] CHANNELS 白名单下无匹配 SKU,退出")
            return 0

    # 过滤掉无 adapter 的渠道(避免开浏览器后再 skip)
    runnable = [s for s in skus if get_adapter(s["platform"]) is not None]
    skipped = len(skus) - len(runnable)
    if skipped:
        skipped_platforms = sorted({s["platform"] for s in skus if get_adapter(s["platform"]) is None})
        print(f"[monitor] 跳过 {skipped} 个 SKU(未实现 adapter 的渠道:{skipped_platforms})")
    if not runnable:
        print("[monitor] 全部 SKU 渠道都没 adapter,退出")
        return 0

    if MAX_SKUS > 0 and len(runnable) > MAX_SKUS:
        print(f"[monitor] MONITOR_MAX_SKUS={MAX_SKUS} → {MAX_SKUS}/{len(runnable)} SKU 入选")
        runnable = runnable[:MAX_SKUS]

    print(f"[monitor] 抓取 {len(runnable)} SKU · headless={HEADLESS} · concurrency={CONCURRENCY}")
    hist = FrozenPriceHistory(load_latest_historical_prices(), load_latest_historical_observations())
    if any(s["platform"].lower() == "currys" for s in runnable):
        hist.currys_pdp_guard = CurrysPdpGuard()

    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        try:
            browser = await launch_scraper_browser(p, headless=HEADLESS)
        except (Exception, asyncio.CancelledError) as exc:
            for platform, country in sorted({(s["platform"], s["country"]) for s in runnable}):
                record_failure(platform=platform, country=country, stage="browser_launch", reason="browser_unavailable", error=exc)
            raise

        sem = asyncio.Semaphore(CONCURRENCY)
        batch_price_maps: dict[str, dict[str, tuple[float, str]]] = {}
        for platform in sorted({s["platform"] for s in runnable}):
            adapter = get_adapter(platform)
            if not getattr(adapter, "batch_price_enabled", False):
                continue
            group = [s for s in runnable if s["platform"].lower() == platform.lower()]
            adapter.batch_candidate_prices = {}
            try:
                prepared = await adapter.prepare_batch_prices(browser, group)
                if platform.lower() == "currys" and getattr(adapter, "catalog_report", {}).get("rate_limited"):
                    hist.currys_pdp_guard.catalog_rate_limited()
                if not prepared:
                    await _retain_batch_candidates(adapter, group, getattr(adapter, "batch_candidate_prices", {}), hist, guard="batch_completeness_guard")
                    for country in sorted({s["country"] for s in group}):
                        record_failure(platform=adapter.platform_name, country=country, stage="prepare_batch",
                                       reason="batch_prices_unavailable", browser_available=True)
                if prepared and not _batch_prices_pass_history_guard(adapter, group, prepared, hist):
                    print(f"[monitor/{adapter.platform_name}] 批量价格疑似系统性错位，整批回退 PDP")
                    for country in sorted({s["country"] for s in group}):
                        record_failure(platform=adapter.platform_name, country=country, stage="batch_history_guard",
                                       reason="batch_history_guard_rejected", browser_available=True)
                    await _retain_batch_candidates(adapter, group, prepared, hist, guard="batch_history_guard")
                    prepared = {}
                if prepared:
                    outlier_keys = _batch_price_outlier_keys(adapter, group, prepared, hist)
                    await _retain_batch_candidates(adapter, group, {key: prepared[key] for key in outlier_keys}, hist, guard="batch_single_guard")
                    for key in outlier_keys:
                        prepared.pop(key, None)
                    if outlier_keys:
                        print(
                            f"[monitor/{adapter.platform_name}] {len(outlier_keys)} 个异常跳变 SKU "
                            "已从 batch 价移除，后续走 PDP 真值复核"
                        )
                        for sku in group:
                            if adapter.batch_price_key(sku["url"]) in outlier_keys:
                                record_failure(platform=adapter.platform_name, country=sku["country"],
                                               stage="batch_single_guard", reason="batch_price_outlier",
                                               product=sku["product_name"], url=sku["url"], browser_available=True)
                batch_price_maps[adapter.platform_name.lower()] = prepared
            except asyncio.CancelledError as exc:
                for country in sorted({s["country"] for s in group}):
                    record_failure(platform=adapter.platform_name, country=country, stage="prepare_batch",
                                   reason="batch_preparation_cancelled", error=exc, browser_available=True)
                await close_playwright_resource(browser, "cancelled batch browser")
                raise
            except Exception as exc:
                if platform.lower() == "currys" and getattr(adapter, "catalog_report", {}).get("rate_limited"):
                    hist.currys_pdp_guard.catalog_rate_limited()
                print(f"[monitor/{adapter.platform_name}] 批量价格准备失败，回退 PDP: {str(exc)[:120]}")
                for country in sorted({s["country"] for s in group}):
                    record_failure(platform=adapter.platform_name, country=country, stage="prepare_batch",
                                   reason="batch_preparation_failed", error=exc, browser_available=True)
                batch_price_maps[adapter.platform_name.lower()] = {}

        normal = [s for s in runnable if not get_adapter(s["platform"]).shared_context]
        shared: dict[str, list[dict]] = {}
        for sku in runnable:
            adapter = get_adapter(sku["platform"])
            if adapter.shared_context:
                shared.setdefault(adapter.platform_name.lower(), []).append(sku)

        jobs = [
            process_sku_bounded(
                sem,
                browser,
                s,
                hist,
                batch_prices=batch_price_maps.get(get_adapter(s["platform"]).platform_name.lower()),
            )
            for s in normal
        ]
        jobs.extend(
            process_shared_group(browser, get_adapter(name), group, hist)
            for name, group in shared.items()
        )
        results = []
        completed = 0
        fatal_errors: list[str] = []
        tasks = [asyncio.create_task(job) for job in jobs]
        try:
            for future in asyncio.as_completed(tasks):
                try:
                    batch = await future
                except Exception as exc:
                    message = f"未预期的任务异常: {type(exc).__name__}: {str(exc)[:160]}"
                    print(f"[monitor] {message}")
                    fatal_errors.append(message)
                    record_failure(platform="multi", country="multi", stage="worker_result",
                                   reason="worker_failed", error=exc)
                    continue
                batch_rows = batch if isinstance(batch, list) else [batch]
                now = datetime.now()
                for row in batch_rows:
                    row.setdefault("Date", now.strftime("%Y-%m-%d"))
                    row.setdefault("Time", now.strftime("%H:%M:%S"))
                results.extend(batch_rows)
                completed += len(batch_rows)
                try:
                    write_checkpoint(results, checkpoint_path)
                    print(f"[monitor] 检查点已更新: {completed}/{len(runnable)} 条完成")
                except OSError as exc:
                    # 检查点只是中断恢复辅助文件；不能反过来关闭浏览器并毁掉主抓取。
                    print(
                        f"[monitor] 检查点暂时无法写入，继续主抓取: "
                        f"{type(exc).__name__}: {str(exc)[:120]}"
                    )
        finally:
            await close_playwright_resource(browser, "browser")
        if fatal_errors:
            raise RuntimeError("；".join(fatal_errors))

    n_ok = sum(1 for r in results if r["Status"] == "Success")
    n_fail = len(results) - n_ok
    n_batch = sum(1 for r in results if r["Page Title"] == "Batch category snapshot")
    try:
        evidence = summarize()
    except Exception as exc:
        print(json.dumps({"event": "price_evidence_summary_failed", "error_type": type(exc).__name__}))
        evidence = {"events": None, "screenshots_saved": None, "write_failures_this_process": None,
                    "screenshot_statuses": {}, "summary_status": "unavailable"}
    quality = {"attempted": len(runnable), "success": n_ok, "failed": n_fail,
               "failed_by_category": dict(Counter(r["Status"] for r in results if r["Status"] != "Success")),
               "latest_date": max((r["Date"] for r in results if r["Status"] == "Success"), default=None),
               "price_anomalies": evidence["events"], "price_screenshots": evidence["screenshots_saved"],
               "price_evidence_write_failures": evidence["write_failures_this_process"],
               "price_screenshot_statuses": evidence["screenshot_statuses"],
               "price_evidence": {"status": evidence.get("status", evidence.get("summary_status", "unavailable")),
                                  "events": evidence["events"], "screenshots_saved": evidence["screenshots_saved"],
                                  "write_failures": evidence.get("write_failures", evidence["write_failures_this_process"]),
                                  "screenshot_statuses": evidence["screenshot_statuses"],
                                  "evidence_complete": evidence.get("evidence_complete")},
               "catalog_reports": {platform: getattr(get_adapter(platform), "catalog_report", {}) for platform in sorted({s["platform"] for s in runnable})},
               "currys_pdp_guard": hist.currys_pdp_guard.report() if hasattr(hist, "currys_pdp_guard") else None,
               "collection_status": "empty" if not n_ok else "partial" if n_fail else "full",
               "artifact_status": "preparing", "publication_status": "not_started"}

    def save_quality():
        try:
            _atomic_json(checkpoint_path.parent / "quality.json", quality)
        except Exception as exc:
            # 摘要是旁路。尤其不能用写盘异常盖掉下方真实的产物准备失败。
            print(json.dumps({"event": "monitor_quality_write_failed", "error_type": type(exc).__name__}))

    # 先记录采集结果：零成功也必须留下 typed failures，不能等发布产物准备完才写。
    save_quality()
    try:
        # 每条观测已进入检查点；继续沿用成功价格和正价门禁，失败行不进入正式产物。
        append_prices([r for r in results if r["Status"] == "Success"])
        trim_prices_window()
    except (Exception, asyncio.CancelledError) as exc:
        quality.update(artifact_status="artifact_failed", publication_status="not_published",
                       artifact_error={"type": type(exc).__name__,
                                       "reason": "no_successful_observations" if not n_ok else "artifact_preparation_failed"})
        save_quality()
        raise
    quality.update(artifact_status="prepared", publication_status="awaiting_publish_job")
    save_quality()
    print(
        f"\n[monitor] 完成 · 成功 {n_ok} / 失败 {n_fail} (共 {len(results)})"
        f" · 类目快照命中 {n_batch}"
    )
    return 0


def main() -> int:
    try:
        return asyncio.run(run())
    except (Exception, asyncio.CancelledError, KeyboardInterrupt) as exc:
        record_failure(platform=os.environ.get("CHANNELS", "multi"), country="multi",
                       stage="monitor_main", reason="monitor_interrupted" if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt))
                       else "monitor_failed", error=exc)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
