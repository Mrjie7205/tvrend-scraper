from __future__ import annotations

import csv
from pathlib import Path

from scripts.recovery.quarantine_boulanger_card_price_leaks import (
    quarantine_catalogs,
    quarantine_prices,
)


def _write(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _read(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def test_quarantine_only_removes_exact_confirmed_price_rows(tmp_path: Path) -> None:
    fields = ["Date", "Time", "Brand", "Product Name", "Country", "Platform", "Price", "Currency"]
    prices = tmp_path / "raw" / "prices.csv"
    rows = [
        {"Date": "2026-08-24", "Time": "01:00:00", "Brand": "TCL", "Product Name": "75X11L", "Country": "FR", "Platform": "Boulanger", "Price": "749.0", "Currency": "EUR"},
        {"Date": "2026-08-24", "Time": "02:00:00", "Brand": "TCL", "Product Name": "75X11L", "Country": "FR", "Platform": "Boulanger", "Price": "3299.0", "Currency": "EUR"},
        {"Date": "2026-08-24", "Time": "03:00:00", "Brand": "TCL", "Product Name": "75X11L", "Country": "FR", "Platform": "Amazon", "Price": "749.0", "Currency": "EUR"},
    ]
    _write(prices, fields, rows)
    quarantine = prices.parent / "quarantine" / "audit.csv"

    assert quarantine_prices(prices, quarantine, apply=True) == 1
    assert [row["Price"] for row in _read(prices)] == ["3299.0", "749.0"]
    assert _read(quarantine)[0]["Quarantine Reason"].startswith("boulanger_product_card_price_leak")
    assert quarantine_prices(prices, quarantine, apply=True) == 0


def test_catalog_keeps_product_and_blanks_only_bad_hint(tmp_path: Path) -> None:
    fields = ["brand_raw", "raw_text", "url", "price_hint_eur", "price_local", "price_eur"]
    catalog_dir = tmp_path / "catalog"
    path = catalog_dir / "boulanger_fr_20260817.csv"
    _write(path, fields, [
        {"brand_raw": "TCL", "raw_text": "TV TCL 75X11L", "url": "https://www.boulanger.com/ref/1238818", "price_hint_eur": "699.0", "price_local": "", "price_eur": ""},
        {"brand_raw": "Samsung", "raw_text": "TV Samsung", "url": "https://www.boulanger.com/ref/9999999", "price_hint_eur": "699.0", "price_local": "", "price_eur": ""},
    ])
    quarantine = catalog_dir / "quarantine" / "audit.csv"

    assert quarantine_catalogs(catalog_dir, quarantine, apply=True) == 1
    rows = _read(path)
    assert len(rows) == 2
    assert rows[0]["price_hint_eur"] == ""
    assert rows[1]["price_hint_eur"] == "699.0"
