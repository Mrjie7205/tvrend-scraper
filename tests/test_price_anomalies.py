"""价格阈值、币种基线和真实报价不被旁路留证改变的行为契约。"""
import asyncio
import csv
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import price_anomalies as anomalies
from monitor_prices import prices_io, run_daily
from monitor_prices.adapters.currys import CurrysAdapter


@pytest.mark.parametrize("old,new,expected", [(100, 200, 100), (100, 50, -50), (100, "199.99", None),
    (100, "50.01", None), (0, 200, None), (None, 200, None), (-1, 200, None),
    (float("nan"), 200, None), (float("inf"), 200, None), (100, -1, None),
    (100, float("nan"), None), (100, None, None), (100, 0, -100)])
def test_exact_thresholds_and_invalid_prices(old, new, expected):
    result = anomalies.classify_change(old, new, old_currency="GBP", currency="GBP")
    assert (result["change_percent"] if result else None) == expected
    if new == 0 and result:
        assert result["valid_quote"] is False


def test_different_or_missing_currency_never_compares():
    assert anomalies.classify_change(100, 500, old_currency="EUR", currency="GBP") is None
    assert anomalies.classify_change(100, 500, old_currency=None, currency="GBP") is None


def test_float_boundary_representation_does_not_hide_exact_alert():
    assert anomalies.classify_change(0.1 + 0.2, 0.6, old_currency="GBP", currency="GBP")["change_percent"] == 100
    assert anomalies.classify_change(0.6, (0.1 + 0.2), old_currency="GBP", currency="GBP")["change_percent"] == -50
    assert anomalies.classify_change("100", "199.99999999999999", old_currency="GBP", currency="GBP") is None


class Page:
    url = "https://www.currys.co.uk/products/tv-10280001.html?token=PRIVATE"

    def __init__(self, broken=False):
        self.broken, self.count = broken, 0
        self.goto = AsyncMock(return_value=SimpleNamespace(status=200))
        self.title = AsyncMock(return_value="LG TV")
        self.close = AsyncMock()
        self.wait_for_load_state = AsyncMock()

    def is_closed(self):
        return False

    def locator(self, selector):
        return selector

    async def evaluate(self, script):
        return {"redaction_ready": True, "title": "TV", "headings": [], "buttons": [], "private": "NEVER_SAVE"}

    async def screenshot(self, **kwargs):
        self.count += 1
        assert kwargs["mask"] and kwargs["mask_color"] == "#000000"
        assert not kwargs["full_page"]
        if self.broken:
            raise RuntimeError("capture unavailable")
        return b"\x89PNG\r\n\x1a\nunit-test"


BASELINE = {"price": 100, "currency": "GBP", "observed_at": "2026-10-08T12:00:00+00:00",
            "identity_precision": "model_country_platform_currency", "source_file": "prices.csv"}
OBSERVATION = {"price": 200, "currency": "GBP", "observed_at": "2026-10-09T12:00:00+00:00",
               "platform": "Currys", "country": "GB", "product": "55TV", "url": Page.url,
               "listing_id": "10280001", "observation_source": "product_page", "ingestion_status": "accepted"}


@pytest.fixture(autouse=True)
def temporary_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("PRICE_ARTIFACTS_DIR", str(tmp_path / "prices"))
    monkeypatch.setenv("FAILURE_EVIDENCE_DIR", str(tmp_path / "failures"))
    monkeypatch.delenv("PRICE_SCREENSHOT_LIMIT", raising=False)
    monkeypatch.setattr(anomalies, "_WRITE_FAILURES", 0)


def test_each_listing_gets_json_and_snapshot_without_failure_two_image_limit(tmp_path):
    async def run():
        page = Page()
        paths = []
        for index in range(4):
            observation = dict(OBSERVATION, listing_id=f"1028000{index}", url=f"https://www.currys.co.uk/products/tv-1028000{index}.html")
            paths.append(await anomalies.record_price_change(baseline=BASELINE, observation=observation, page=page))
        repeated = await anomalies.record_price_change(baseline=BASELINE, observation=observation, page=page)
        assert repeated == paths[-1] and page.count == 4
        assert len(set(paths)) == 4
        document = json.loads(paths[0].read_text(encoding="utf-8"))
        assert document["baseline_identity_precision"] == "model_country_platform_currency"
        assert document["baseline_source_file"] == "prices.csv"
        assert document["old_observed_at"] == BASELINE["observed_at"]
        assert document["screenshot"]["status"] == "saved"
        assert "PRIVATE" not in paths[0].read_text(encoding="utf-8")
        assert "NEVER_SAVE" not in paths[0].read_text(encoding="utf-8")
        assert not (tmp_path / "failures").exists()
    asyncio.run(run())


def test_explicit_limit_is_visible_and_never_drops_json(monkeypatch):
    monkeypatch.setenv("PRICE_SCREENSHOT_LIMIT", "1")
    async def run():
        for index in range(2):
            await anomalies.record_price_change(baseline=BASELINE, observation=dict(OBSERVATION, listing_id=str(index)), page=Page())
    asyncio.run(run())
    summary = anomalies.summarize()
    assert summary["events"] == 2 and summary["screenshots_saved"] == 1
    assert summary["screenshot_statuses"]["omitted_explicit_limit"] == 1
    assert not summary["evidence_complete"]


def test_missing_page_broken_capture_and_write_failure_are_visible(monkeypatch, capsys):
    async def run():
        missing = await anomalies.record_price_change(baseline=BASELINE, observation=OBSERVATION)
        broken = await anomalies.record_price_change(baseline=BASELINE, observation=dict(OBSERVATION, listing_id="other"), page=Page(broken=True))
        assert json.loads(missing.read_text())["screenshot"]["status"] == "page_unavailable"
        assert json.loads(broken.read_text())["screenshot"]["status"] == "capture_failed"
        monkeypatch.setattr(anomalies, "_atomic_json", lambda *args: (_ for _ in ()).throw(OSError("disk full")))
        assert await anomalies.record_price_change(baseline=BASELINE, observation=dict(OBSERVATION, listing_id="new")) is None
        anomalies.update_price_change_status(missing, ingestion_status="accepted")
    asyncio.run(run())
    assert anomalies._WRITE_FAILURES == 2
    assert "price_evidence_write_failed" in capsys.readouterr().out


def test_no_baseline_and_no_alerts_summary_are_normal():
    assert asyncio.run(anomalies.record_price_change(baseline=None, observation=OBSERVATION)) is None
    summary = anomalies.summarize()
    assert summary["status"] == "no_alerts" and summary["evidence_complete"]


def test_zero_candidate_remains_rejected_even_if_status_update_claims_accepted():
    path = asyncio.run(anomalies.record_price_change(baseline=BASELINE, observation=dict(OBSERVATION, price=0)))
    anomalies.update_price_change_status(path, ingestion_status="accepted")
    document = json.loads(path.read_text())
    assert document["change_percent"] == -100 and not document["valid_quote"]
    assert document["ingestion_status"] == "rejected"


def test_out_of_order_history_keeps_latest_same_currency(tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    raw.mkdir()
    base = {column: "" for column in prices_io.PRICES_COLUMNS}
    base.update({"Date": "2026-10-09", "Time": "12:00:00", "Brand": "LG", "Product Name": "55TV", "Country": "GB", "Platform": "Currys", "Currency": "GBP", "Price": "100", "Status": "Success"})
    with (raw / "prices.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=prices_io.PRICES_COLUMNS)
        writer.writeheader()
        writer.writerows([base, dict(base, Time="13:00:00", Currency="EUR", Price="500"), dict(base, Time="11:00:00", Price="90")])
    monkeypatch.setattr(prices_io, "_root", lambda: tmp_path)
    history = prices_io.load_latest_historical_observations()
    assert history[("55TV", "GB", "Currys", "GBP")]["price"] == 100
    assert history[("55TV", "GB", "Currys", "EUR")]["price"] == 500


def test_latest_zero_history_does_not_fall_back_to_older_positive(tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    raw.mkdir()
    base = dict(zip(prices_io.PRICES_COLUMNS, ["2026-10-09", "12:00:00", "LG", "55TV", "GB", "Currys", "100", "GBP", "TV", "Success", "持平"]))
    with (raw / "prices.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=prices_io.PRICES_COLUMNS)
        writer.writeheader()
        writer.writerows([base, dict(base, Time="13:00:00", Price="0")])
    monkeypatch.setattr(prices_io, "_root", lambda: tmp_path)
    baseline = prices_io.load_latest_historical_observations()[("55TV", "GB", "Currys", "GBP")]
    assert baseline["price"] == 0
    assert anomalies.classify_change(baseline["price"], 300, old_currency="GBP", currency="GBP") is None


def test_github_summary_supports_zero_events(tmp_path):
    summary = anomalies.summarize()
    target = tmp_path / "github-summary.md"
    anomalies.write_github_summary(target, summary)
    assert "已留存事件 0" in target.read_text(encoding="utf-8")


@pytest.mark.parametrize("direct,broken", [(False, False), (False, True), (True, False)])
def test_final_success_price_preserved_and_page_source_disclosed(monkeypatch, direct, broken):
    page = Page(broken)
    page.url = OBSERVATION["url"].split("?")[0]
    context = SimpleNamespace(new_page=AsyncMock(return_value=page), close=AsyncMock(), request=object())
    adapter = CurrysAdapter()
    adapter.direct_price_enabled = direct
    adapter.extract_price_direct = AsyncMock(return_value=(200, "GBP"))
    adapter.extract_price = AsyncMock(return_value=(190 if direct else 200, "GBP"))
    adapter.wait_selectors = ()
    adapter.cookie_accept_selectors = ()
    hist = prices_io.FrozenPriceHistory({"55TV_GB_Currys": 100}, {("55TV", "GB", "Currys", "GBP"): BASELINE})
    sku = {"brand": "LG", "product_name": "55TV", "country": "GB", "platform": "Currys", "url": page.url}
    monkeypatch.setattr(run_daily, "_new_context", AsyncMock(return_value=context))
    monkeypatch.setattr(run_daily, "get_adapter", lambda _: adapter)
    monkeypatch.setattr(run_daily, "handle_antibot_page", AsyncMock(return_value=True))
    monkeypatch.setattr(run_daily.asyncio, "sleep", AsyncMock())
    result = asyncio.run(run_daily.process_sku(asyncio.Semaphore(1), object(), sku, hist))
    assert result["Status"] == "Success" and result["Price"] == 200
    document = json.loads(next(anomalies.default_output_dir().glob("price_change_*.json")).read_text())
    if direct:
        assert document["observation_source"] == "direct_api"
        assert document["screenshot_page_source"] == "pdp_verification"
        assert document["verification"]["price"] == 190
        assert document["new_price"] == 200
        assert page.goto.await_count == 1
    else:
        assert document["screenshot_page_source"] == "same_product_page"
        assert document["verification"] is None
    assert document["screenshot"]["status"] == ("capture_failed" if broken else "saved")


@pytest.mark.parametrize("status,title,expected", [(403, "", "access_blocked"), (200, "Just a moment", "challenge_unresolved"), (200, "TVs", "redirect_unverified")])
def test_currys_classification_stops_before_price_or_repeated_block_wait(monkeypatch, status, title, expected):
    page = Page()
    page.url = "https://www.currys.co.uk/tv-and-audio/televisions/tvs"
    page.goto.return_value.status = status
    page.title.return_value = title
    context = SimpleNamespace(new_page=AsyncMock(return_value=page), close=AsyncMock())
    adapter = CurrysAdapter()
    adapter.extract_price = AsyncMock()
    monkeypatch.setattr(run_daily, "_new_context", AsyncMock(return_value=context))
    monkeypatch.setattr(run_daily, "get_adapter", lambda _: adapter)
    wait = AsyncMock()
    monkeypatch.setattr(run_daily, "handle_antibot_page", wait)
    monkeypatch.setattr(run_daily.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(run_daily, "capture_failure", AsyncMock())
    sku = {"brand": "LG", "product_name": "55TV", "country": "GB", "platform": "Currys", "url": OBSERVATION["url"]}
    result = asyncio.run(run_daily.process_sku(asyncio.Semaphore(1), object(), sku, {}))
    assert result["Status"] == f"Failed: {expected}"
    assert page.goto.await_count == 1
    wait.assert_not_awaited()
    adapter.extract_price.assert_not_awaited()


def test_blocked_catalog_cannot_trigger_uncovered_pdp_or_anomaly_verification(monkeypatch):
    adapter = CurrysAdapter()
    adapter.catalog_report = {"blocked": True, "complete": False}
    context = AsyncMock()
    monkeypatch.setattr(run_daily, "_new_context", context)
    monkeypatch.setattr(run_daily, "get_adapter", lambda _: adapter)
    monkeypatch.setattr(run_daily, "record_failure", lambda **kw: None)
    sku = {"brand": "LG", "product_name": "55TV", "country": "GB", "platform": "Currys", "url": OBSERVATION["url"]}
    hist = prices_io.FrozenPriceHistory({}, {("55TV", "GB", "Currys", "GBP"): BASELINE})
    result = asyncio.run(run_daily.process_sku(asyncio.Semaphore(1), object(), sku, hist))
    assert result["Status"] == "Failed: channel_access_blocked"
    result = asyncio.run(run_daily.process_sku(asyncio.Semaphore(1), object(), sku, hist, batch_prices={adapter.batch_price_key(sku['url']): (200, "GBP")}))
    assert result["Status"] == "Success" and result["Price"] == 200
    context.assert_not_awaited()
    document = json.loads(next(anomalies.default_output_dir().glob("price_change_*.json")).read_text())
    assert document["verification"]["status"] == "not_attempted_channel_blocked"


def test_direct_api_verification_timeout_keeps_original_success(monkeypatch):
    page = Page()
    page.goto.side_effect = asyncio.TimeoutError()
    context = SimpleNamespace(new_page=AsyncMock(return_value=page), close=AsyncMock(), request=object())
    adapter = CurrysAdapter()
    adapter.direct_price_enabled = True
    adapter.extract_price_direct = AsyncMock(return_value=(200, "GBP"))
    monkeypatch.setattr(run_daily, "_new_context", AsyncMock(return_value=context))
    monkeypatch.setattr(run_daily, "get_adapter", lambda _: adapter)
    hist = prices_io.FrozenPriceHistory({}, {("55TV", "GB", "Currys", "GBP"): BASELINE})
    sku = {"brand": "LG", "product_name": "55TV", "country": "GB", "platform": "Currys", "url": OBSERVATION["url"]}
    result = asyncio.run(run_daily.process_sku(asyncio.Semaphore(1), object(), sku, hist))
    assert result["Status"] == "Success" and result["Price"] == 200
    document = json.loads(next(anomalies.default_output_dir().glob("price_change_*.json")).read_text())
    assert document["verification"]["status"] == "verification_timeout"
    assert document["new_price"] == 200
    assert page.goto.await_count == 1


def test_body_challenge_is_not_retried_as_generic_navigation_error(monkeypatch):
    page = Page()
    context = SimpleNamespace(new_page=AsyncMock(return_value=page), close=AsyncMock())
    adapter = CurrysAdapter()
    monkeypatch.setattr(run_daily, "_new_context", AsyncMock(return_value=context))
    monkeypatch.setattr(run_daily, "get_adapter", lambda _: adapter)
    monkeypatch.setattr(run_daily, "handle_antibot_page", AsyncMock(return_value=False))
    monkeypatch.setattr(run_daily.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(run_daily, "capture_failure", AsyncMock())
    sku = {"brand": "LG", "product_name": "55TV", "country": "GB", "platform": "Currys", "url": page.url}
    result = asyncio.run(run_daily.process_sku(asyncio.Semaphore(1), object(), sku, {}))
    assert result["Status"] == "Failed: challenge_unresolved"
    assert page.goto.await_count == 1
