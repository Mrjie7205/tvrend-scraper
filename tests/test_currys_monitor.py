from __future__ import annotations

import sys
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from monitor_prices.adapters.currys import CurrysAdapter  # noqa: E402
from monitor_prices.adapters.boulanger import BoulangerAdapter  # noqa: E402
from catalog_scrape.adapters.boulanger import (  # noqa: E402
    BoulangerCatalogAdapter,
    _JS_EXTRACT,
)
from monitor_prices.run_daily import (  # noqa: E402
    _batch_price_outlier_keys,
    _batch_prices_pass_history_guard,
)


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


def test_boulanger_catalog_extracts_one_primary_product_per_card() -> None:
    assert "article.product-list__product" in _JS_EXTRACT
    assert "product-list__product-image-link" in _JS_EXTRACT
    assert "document.querySelectorAll('a[href*=\"/ref/\"]')" not in _JS_EXTRACT


def test_boulanger_duplicate_price_tie_does_not_choose_false_low() -> None:
    entries = [
        ("TV Mini LED TCL 75X11L (2026)", "3299,00 €"),
        ("TV Mini LED TCL 75X11L (2026)", "749,00 €"),
    ]

    assert BoulangerCatalogAdapter._pick_price_eur(entries) == 3299.0


def test_batch_single_outlier_is_sent_to_pdp_without_rejecting_batch() -> None:
    adapter = BoulangerAdapter()
    skus = [
        {
            "url": "https://www.boulanger.com/ref/1238818",
            "product_name": "75X11L",
            "country": "FR",
            "platform": "Boulanger",
        },
        {
            "url": "https://www.boulanger.com/ref/1240585",
            "product_name": "55C6KPRO",
            "country": "FR",
            "platform": "Boulanger",
        },
    ]
    prices = {"1238818": (749.0, "EUR"), "1240585": (749.0, "EUR")}
    hist = {"75X11L_FR_Boulanger": 3599.0, "55C6KPRO_FR_Boulanger": 749.0}

    assert _batch_price_outlier_keys(adapter, skus, prices, hist) == {"1238818"}


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
