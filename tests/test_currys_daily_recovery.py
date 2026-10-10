"""目录失败不能冒充PDP全站拒绝；只在明确、连续的不同型号访问拒绝时有界停止。"""
import asyncio
import csv
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from monitor_prices import run_daily
from monitor_prices.prices_io import FrozenPriceHistory
from monitor_prices.adapters.currys import CurrysAdapter, CurrysPdpGuard


def worker(monkeypatch, outcomes, *, same_model=False, concurrent=False):
    adapter = CurrysAdapter()
    adapter.cookie_accept_selectors, adapter.wait_selectors = (), ()
    adapter.catalog_report = {"blocked": True, "rate_limited": False, "complete": False}
    hist = FrozenPriceHistory({}, {})
    hist.currys_pdp_guard = CurrysPdpGuard()
    calls = []
    actual_sleep = asyncio.sleep
    skus = [{"brand": "LG", "product_name": "55TV" if same_model else f"55TV{i}", "country": "GB",
             "platform": "Currys", "url": f"https://www.currys.co.uk/products/tv-{10280000+i}.html"}
            for i in range(len(outcomes))]

    class Page:
        url = "about:blank"
        async def goto(self, url, **kwargs):
            index = int(adapter.batch_price_key(url)) - 10280000
            assert not hist.currys_pdp_guard.stopped, "保护触发之后不得再发新的PDP请求"
            calls.append(index)
            self.outcome = outcomes[index]
            self.url = "https://www.currys.co.uk/tv-and-audio/televisions/tvs" if self.outcome == "redirect" else url
            if concurrent:
                await actual_sleep(0)
            return SimpleNamespace(status=int(self.outcome) if self.outcome.isdigit() else 200)
        async def title(self):
            return "Just a moment..." if self.outcome == "challenge" else "LG TV"
        async def wait_for_load_state(self, *args, **kwargs):
            pass
        async def close(self):
            pass

    class Context:
        async def new_page(self):
            return Page()
        async def close(self):
            pass

    async def extract(page):
        return None if page.outcome == "missing" else (100.0, "GBP")
    adapter.extract_price = AsyncMock(side_effect=extract)
    monkeypatch.setattr(run_daily, "_new_context", AsyncMock(side_effect=lambda *a, **kw: Context()))
    monkeypatch.setattr(run_daily, "get_adapter", lambda _: adapter)
    monkeypatch.setattr(run_daily, "handle_antibot_page", AsyncMock(return_value=True))
    monkeypatch.setattr(run_daily, "capture_failure", AsyncMock())
    monkeypatch.setattr(run_daily, "record_failure", lambda **kw: None)
    monkeypatch.setattr(run_daily.asyncio, "sleep", AsyncMock())
    return adapter, hist, skus, calls


def process_all(hist, skus, *, concurrent=False, batch=None):
    async def run():
        sem = asyncio.Semaphore(3 if concurrent else 1)
        if concurrent:
            return await asyncio.gather(*(run_daily.process_sku(sem, object(), sku, hist, batch_prices=batch) for sku in skus))
        return [await run_daily.process_sku(sem, object(), sku, hist, batch_prices=batch) for sku in skus]
    return asyncio.run(run())


@pytest.mark.parametrize("denial", ["403", "401", "challenge"])
def test_six_different_pdp_denials_stop_remaining_not_first_catalog_denial(monkeypatch, denial):
    _, hist, skus, calls = worker(monkeypatch, [denial] * 8)
    results = process_all(hist, skus)
    assert len(calls) == 6
    assert [r["Status"] for r in results[-2:]] == ["Failed: pdp_access_suspended"] * 2
    report = hist.currys_pdp_guard.report()
    assert report["stop_reason"] == "consecutive_pdp_access_denials"
    assert report["maximum_consecutive_distinct_skus"] == 6
    assert report["skipped"] == {"price_lookup": 2}
    assert len(report["trigger"]["consecutive_skus"]) == 6


def test_same_model_multiple_listing_denials_are_not_six_different_skus(monkeypatch):
    _, hist, skus, calls = worker(monkeypatch, ["403"] * 8, same_model=True)
    results = process_all(hist, skus)
    assert len(calls) == 8
    assert all(r["Status"] == "Failed: access_blocked" for r in results)
    assert not hist.currys_pdp_guard.stopped
    assert hist.currys_pdp_guard.report()["maximum_consecutive_distinct_skus"] == 1


@pytest.mark.parametrize("reachable", ["ok", "404", "410", "redirect", "missing"])
def test_reachable_or_non_denial_outcome_breaks_consecutive_block_sequence(monkeypatch, reachable):
    _, hist, skus, calls = worker(monkeypatch, ["403"] * 5 + [reachable] + ["403"] * 5)
    process_all(hist, skus)
    assert len(calls) == 11 + (reachable == "missing")
    report = hist.currys_pdp_guard.report()
    assert not report["stopped"] and report["maximum_consecutive_distinct_skus"] == 5
    assert report["reachable_resets"] == 1


def test_pdp_429_stops_extra_requests_but_keeps_validated_batch(monkeypatch):
    adapter, hist, skus, calls = worker(monkeypatch, ["429", "ok", "ok"])
    batch = {adapter.batch_price_key(skus[1]["url"]): (100, "GBP")}
    results = process_all(hist, skus, batch=batch)
    assert calls == [0]
    assert [r["Status"] for r in results] == ["Failed: rate_limited", "Success", "Failed: pdp_rate_limited"]
    assert hist.currys_pdp_guard.report()["stop_reason"] == "pdp_rate_limited"


def test_concurrent_requests_do_not_start_after_guard_has_tripped(monkeypatch):
    _, hist, skus, calls = worker(monkeypatch, ["403"] * 12, concurrent=True)
    results = process_all(hist, skus, concurrent=True)
    assert 6 <= len(calls) <= 8  # 阈值外最多只有已开始的两个请求允许收尾。
    assert sum(r["Status"] == "Failed: pdp_access_suspended" for r in results) == 12 - len(calls)
    assert hist.currys_pdp_guard.stopped


@pytest.mark.parametrize('adopted_document', [False, True])
@pytest.mark.parametrize('phase,reason', [('dom_unavailable', 'dom_read_error'),
                                        ('response_observer_unavailable', 'navigation_observer_error')])
def test_recovery_200_software_failure_does_not_count_as_sixth_pdp_denial(monkeypatch, adopted_document, phase, reason):
    _, hist, skus, _ = worker(monkeypatch, ['403'])
    for index in range(5):
        hist.currys_pdp_guard.observe({'product_name': f'prior{index}', 'country': 'GB'},
                                     http_status=403, reason='access_blocked')
    monkeypatch.setattr(run_daily, 'currys_navigation_state', lambda page: {
        'navigation_count': 2 if adopted_document else 1, 'http_status': 200 if adopted_document else 403,
    })
    monkeypatch.setattr(run_daily, 'wait_currys_automatic_check', AsyncMock(return_value={
        'initial_status': 403, 'final_status': 200, 'target_verified': False, 'retry_allowed': False,
        'automatic_check': True, 'human_controls': False, 'hard_block': False,
        'phase': phase, 'error_type': 'ValueError', 'outcome': 'unresolved',
    }))
    result = process_all(hist, skus)[0]
    assert result['Status'] == f'Failed: {reason}' and result['Price'] is None
    report = hist.currys_pdp_guard.report()
    assert not report['stopped'] and report['maximum_consecutive_distinct_skus'] == 5
    assert report['outcomes']['access_blocked'] == 5
    assert run_daily.capture_failure.await_args.kwargs['http_status'] == 200
    assert run_daily.capture_failure.await_args.kwargs['reason'] == reason


@pytest.mark.parametrize("rate_limited,keep_batch", [(False, False), (False, True), (True, True)])
def test_full_daily_pipeline_separates_catalog_report_from_pdp_guard(tmp_path, monkeypatch, rate_limited, keep_batch):
    import playwright.async_api
    adapter, _, skus, calls = worker(monkeypatch, ["ok", "ok"])
    # worker的Page断言用自身guard；真实run另建每轮guard，在此只验证目录不会阻断PDP。
    async def prepare(browser, group):
        adapter.catalog_report = {"blocked": True, "rate_limited": rate_limited, "complete": False}
        adapter.batch_candidate_prices = {}
        return {adapter.batch_price_key(skus[0]["url"]): (100.0, "GBP")} if keep_batch else {}
    monkeypatch.setattr(adapter, "prepare_batch_prices", prepare)
    monkeypatch.setattr(run_daily, "load_active_skus", lambda: skus)
    monkeypatch.setattr(run_daily, "channels_in_scope", lambda: {"currys"})
    monkeypatch.setattr(run_daily, "load_latest_historical_prices", lambda: {})
    monkeypatch.setattr(run_daily, "load_latest_historical_observations", lambda: {})
    monkeypatch.setattr(run_daily, "reset_checkpoint", lambda: tmp_path / "partial.csv")
    monkeypatch.setattr(run_daily, "MAX_SKUS", 0)
    monkeypatch.setattr(run_daily, "CONCURRENCY", 1)
    monkeypatch.setattr(run_daily, "launch_scraper_browser", AsyncMock(return_value=SimpleNamespace(close=AsyncMock())))
    manager = AsyncMock()
    manager.__aenter__.return_value = object()
    monkeypatch.setattr(playwright.async_api, "async_playwright", lambda: manager)
    for key, value in {"PRICE_PUBLICATION_DIR": str(tmp_path / "publication"), "PRICE_ARTIFACTS_DIR": str(tmp_path / "price-evidence"),
                       "FAILURE_EVIDENCE_DIR": str(tmp_path / "failures"), "MONITOR_MAX_SKUS": "0", "CHANNELS": "Currys",
                       "GITHUB_REPOSITORY": "Mrjie7205/tvrend-scraper", "GITHUB_RUN_ID": "123456", "GITHUB_RUN_ATTEMPT": "1",
                       "GITHUB_SHA": "a" * 40, "GITHUB_REF": "refs/heads/main"}.items():
        monkeypatch.setenv(key, value)
    assert asyncio.run(run_daily.run()) == 0
    quality = json.loads((tmp_path / "quality.json").read_text(encoding="utf-8"))
    assert quality["catalog_reports"]["Currys"]["blocked"] is True
    report = quality["currys_pdp_guard"]
    if rate_limited:
        assert quality["success"] == 1 and calls == []
        assert report["stop_reason"] == "catalog_rate_limited"
        assert report["consecutive_distinct_skus"] == 0
    else:
        assert quality["success"] == 2
        assert len(calls) == (1 if keep_batch else 2)
        assert not report["stopped"] and report["consecutive_distinct_skus"] == 0
    with (tmp_path / "publication" / "prices.csv").open(encoding="utf-8-sig", newline="") as handle:
        published = list(csv.DictReader(handle))
    assert len(published) == quality["success"]
