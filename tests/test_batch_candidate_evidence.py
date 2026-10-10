"""完整日抓链路：被门禁丢弃的类目候选仍留证，复用既有PDP访问而不发布未验收价。"""
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
from monitor_prices.adapters.currys import CurrysAdapter
from monitor_prices.adapters.boulanger import BoulangerAdapter
from price_publication import PublicationError
import price_anomalies


class Page:
    def __init__(self, calls, status):
        self.url, self.closed, self.calls, self.status = "about:blank", False, calls, status

    async def goto(self, url, **kwargs):
        self.calls.append(url)
        self.url = url
        return SimpleNamespace(status=self.status)

    async def title(self):
        return "LG television"

    async def wait_for_load_state(self, *args, **kwargs):
        pass

    async def close(self):
        self.closed = True

    def is_closed(self):
        return self.closed

    def locator(self, selector):
        return selector

    async def evaluate(self, script):
        return {"redaction_ready": True, "title": "TV", "headings": [], "buttons": []}

    async def screenshot(self, **kwargs):
        assert not self.closed, "截图必须发生在既有PDP关闭之前"
        assert kwargs["mask"] and kwargs["mask_color"] == "#000000"
        return b"\x89PNG\r\n\x1a\nunit-test-candidate"


@pytest.mark.parametrize("guard,outcome", [
    ("single", "403"), ("single", "missing"), ("single", "different"),
    ("single", "same"), ("single", "different_anomaly"), ("single", "below_threshold"),
    ("history", "403"), ("completeness_currys", "403"), ("completeness_boulanger", "403"),
])
def test_rejected_batch_candidate_survives_full_monitor_chain(tmp_path, monkeypatch, guard, outcome):
    import playwright.async_api
    from catalog_scrape.adapters.currys import CurrysCatalogAdapter
    from catalog_scrape.adapters.boulanger import BoulangerCatalogAdapter
    from monitor_prices.adapters import boulanger as boulanger_module

    channel = "Boulanger" if guard.endswith("boulanger") else "Currys"
    country, currency = ("FR", "EUR") if channel == "Boulanger" else ("GB", "GBP")
    adapter = BoulangerAdapter() if channel == "Boulanger" else CurrysAdapter()
    adapter.cookie_accept_selectors = ()
    adapter.wait_selectors = ()
    count = 20 if guard == "history" else 1
    candidate_price = 180.0 if outcome == "below_threshold" else 200.0
    final_price = {"different": 100.0, "different_anomaly": 250.0, "below_threshold": 180.0}.get(outcome, 200.0)
    status = 403 if outcome == "403" else 200
    calls = []

    class Context:
        async def new_page(self):
            return Page(calls, status)
        async def close(self):
            pass

    skus = [{"brand": "LG", "product_name": f"55TV{i}", "country": country, "platform": channel,
             "url": f"https://www.boulanger.com/ref/{1200000 + i}" if channel == "Boulanger" else f"https://www.currys.co.uk/products/tv-{10280000 + i}.html"}
            for i in range(count)]
    prices = {adapter.batch_price_key(sku["url"]): (candidate_price, currency) for sku in skus}
    legacy = {f"{sku['product_name']}_{country}_{channel}": 100.0 for sku in skus}
    baselines = {(sku["product_name"], country, channel, currency):
                 {"price": 100.0, "currency": currency, "observed_at": "2026-10-08T12:00:00+00:00",
                  "identity_precision": "model_country_platform_currency"} for sku in skus}

    if guard.startswith("completeness"):
        async def catalogue(self, target):
            self.catalog_report = {"blocked": False, "complete": True}
            return [SimpleNamespace(url=sku["url"], price_hint_eur=candidate_price) for sku in skus]
        monkeypatch.setattr(CurrysCatalogAdapter, "fetch_catalog_from_browser", catalogue)
        monkeypatch.setattr(BoulangerCatalogAdapter, "fetch_catalog", catalogue)
        monkeypatch.setattr(boulanger_module, "new_scraper_context", AsyncMock(return_value=Context()))
        # 使用真实适配器的既有数量门禁：1条远低于默认300/180，正式返回仍为空。
        monkeypatch.delenv("CURRYS_BATCH_MIN_ITEMS", raising=False)
        monkeypatch.delenv("BOULANGER_BATCH_MIN_ITEMS", raising=False)
    else:
        async def prepare(browser, group):
            adapter.batch_candidate_prices = dict(prices)
            return dict(prices)
        monkeypatch.setattr(adapter, "prepare_batch_prices", prepare)

    adapter.extract_price = AsyncMock(return_value=None if outcome == "missing" else (final_price, currency))
    monkeypatch.setattr(run_daily, "_new_context", AsyncMock(side_effect=lambda *a, **k: Context()))
    monkeypatch.setattr(run_daily, "get_adapter", lambda _: adapter)
    monkeypatch.setattr(run_daily, "load_active_skus", lambda: skus)
    monkeypatch.setattr(run_daily, "channels_in_scope", lambda: {channel.lower()})
    monkeypatch.setattr(run_daily, "load_latest_historical_prices", lambda: legacy)
    monkeypatch.setattr(run_daily, "load_latest_historical_observations", lambda: baselines)
    monkeypatch.setattr(run_daily, "reset_checkpoint", lambda: tmp_path / "partial.csv")
    monkeypatch.setattr(run_daily, "MAX_SKUS", 0)
    monkeypatch.setattr(run_daily, "CONCURRENCY", 1)
    monkeypatch.setattr(run_daily, "handle_antibot_page", AsyncMock(return_value=True))
    monkeypatch.setattr(run_daily.asyncio, "sleep", AsyncMock())
    browser = SimpleNamespace(close=AsyncMock())
    monkeypatch.setattr(run_daily, "launch_scraper_browser", AsyncMock(return_value=browser))
    manager = AsyncMock()
    manager.__aenter__.return_value = object()
    monkeypatch.setattr(playwright.async_api, "async_playwright", lambda: manager)
    for key, value in {"PRICE_PUBLICATION_DIR": str(tmp_path / "publication"), "PRICE_ARTIFACTS_DIR": str(tmp_path / "price-evidence"),
                       "FAILURE_EVIDENCE_DIR": str(tmp_path / "failures"), "MONITOR_MAX_SKUS": "0", "CHANNELS": channel,
                       "GITHUB_REPOSITORY": "Mrjie7205/tvrend-scraper", "GITHUB_RUN_ID": "123456", "GITHUB_RUN_ATTEMPT": "1",
                       "GITHUB_SHA": "a" * 40, "GITHUB_REF": "refs/heads/main"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("PRICE_SCREENSHOT_LIMIT", raising=False)
    monkeypatch.setattr(price_anomalies, "_WRITE_FAILURES", 0)

    if outcome in {"403", "missing"}:
        with pytest.raises(PublicationError, match="没有有效价格"):
            asyncio.run(run_daily.run())
        assert not (tmp_path / "publication" / "manifest.json").exists()
    else:
        assert asyncio.run(run_daily.run()) == 0
        with (tmp_path / "publication" / "prices.csv").open(encoding="utf-8-sig", newline="") as handle:
            published = list(csv.DictReader(handle))
        assert [float(row["Price"]) for row in published] == [final_price]

    # 无价时只保留基线已有的两次PDP尝试；其它场景一次，不能由留证额外打开页面。
    expected_pdp = min(count, 6) if channel == "Currys" and outcome == "403" else count
    assert len(calls) == expected_pdp * (2 if outcome == "missing" else 1)
    events = [json.loads(path.read_text(encoding="utf-8")) for path in (tmp_path / "price-evidence").glob("price_change_*.json")]
    if outcome == "below_threshold":
        assert events == []
        return
    candidates = [event for event in events if event["observation_source"] == "batch_catalog_candidate"]
    assert len(candidates) == count
    for event in candidates:
        assert event["old_price"] == 100 and event["new_price"] == 200
        suspended = event["verification"]["status"] == "pdp_access_suspended"
        assert event["screenshot_page_source"] == ("existing_pdp_unavailable" if suspended else "existing_pdp_verification")
        assert event["screenshot"]["status"] == ("page_unavailable" if suspended else "saved")
        assert event["ingestion_status"] == ("accepted" if outcome == "same" else "rejected")
        assert event["validation_state"] == ("validated" if outcome == "same" else "rejected")
        assert event["verification"]["guard"] == ("batch_history_guard" if guard == "history" else "batch_completeness_guard" if guard.startswith("completeness") else "batch_single_guard")
        if outcome == "403":
            if not suspended:
                assert event["verification"]["http_status"] == 403
        elif outcome == "missing":
            assert event["verification"]["status"] == "price_not_found"
        else:
            assert event["verification"]["price"] == final_price
    if outcome == "different_anomaly":
        final_events = [event for event in events if event["observation_source"] == "product_page"]
        assert len(final_events) == 1
        assert final_events[0]["new_price"] == 250 and final_events[0]["ingestion_status"] == "accepted"
    else:
        assert len(events) == count
