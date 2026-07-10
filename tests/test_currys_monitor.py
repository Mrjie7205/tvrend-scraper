from __future__ import annotations

import sys
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from monitor_prices.adapters.currys import CurrysAdapter  # noqa: E402
from monitor_prices.adapters.boulanger import BoulangerAdapter  # noqa: E402
from monitor_prices.run_daily import _batch_prices_pass_history_guard  # noqa: E402


def test_currys_batch_key_uses_stable_product_id() -> None:
    adapter = CurrysAdapter()
    old_slug = "https://www.currys.co.uk/products/old-name-10283600.html"
    new_slug = "https://www.currys.co.uk/products/new-name-10283600.html?src=search"

    assert adapter.batch_price_key(old_slug) == "10283600"
    assert adapter.batch_price_key(new_slug) == "10283600"


def test_currys_category_redirect_is_unavailable() -> None:
    adapter = CurrysAdapter()
    product_url = "https://www.currys.co.uk/products/tv-name-10283600.html"

    assert adapter.is_unavailable_response(
        200,
        product_url,
        "https://www.currys.co.uk/tv-and-audio/televisions/tvs",
    )
    assert not adapter.is_unavailable_response(200, product_url, product_url)


def test_currys_http_404_is_unavailable() -> None:
    adapter = CurrysAdapter()
    product_url = "https://www.currys.co.uk/products/tv-name-10283600.html"

    assert adapter.is_unavailable_response(404, product_url, product_url)


def test_boulanger_batch_key_uses_ref_id() -> None:
    adapter = BoulangerAdapter()

    assert adapter.batch_price_key("https://www.boulanger.com/ref/1240577") == "1240577"
    assert adapter.batch_price_key("https://www.boulanger.com/ref/1240577#avis") == "1240577"


def test_history_guard_rejects_systematic_discount_amounts() -> None:
    adapter = CurrysAdapter()
    skus = []
    prices = {}
    hist = {}
    for i in range(20):
        product_id = str(10280000 + i)
        model = f"55TEST{i}"
        url = f"https://www.currys.co.uk/products/tv-{product_id}.html"
        skus.append({"url": url, "product_name": model, "country": "GB", "platform": "Currys"})
        prices[product_id] = (30.0, "GBP")
        hist[f"{model}_GB_Currys"] = 600.0

    assert not _batch_prices_pass_history_guard(adapter, skus, prices, hist)


def test_history_guard_accepts_normal_price_movement() -> None:
    adapter = CurrysAdapter()
    skus = []
    prices = {}
    hist = {}
    for i in range(20):
        product_id = str(10281000 + i)
        model = f"65TEST{i}"
        url = f"https://www.currys.co.uk/products/tv-{product_id}.html"
        skus.append({"url": url, "product_name": model, "country": "GB", "platform": "Currys"})
        prices[product_id] = (950.0 + i, "GBP")
        hist[f"{model}_GB_Currys"] = 1000.0

    assert _batch_prices_pass_history_guard(adapter, skus, prices, hist)
