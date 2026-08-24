"""隔离 Boulanger 商品卡串价造成的已确认错误数据。

这不是按价格高低做猜测，而是只处理已经逐页核实过的“型号 + 日期 + 错误价”组合。
活动数据中删除错误日价即代表该日留空；原始行完整保存在 quarantine 目录，便于审计和回滚。
"""

from __future__ import annotations

import argparse
import csv
import os
from pathlib import Path
from typing import Iterable


REASON = "boulanger_product_card_price_leak_confirmed_2026-08-24"


def _dates(*values: str) -> set[str]:
    return set(values)


# 经 PDP 与商品列表逐项核实的错误日价。未列入这里的异常值不会自动删除。
BAD_PRICE_ROWS: dict[tuple[str, str], dict[str, set[float]]] = {
    ("TCL", "75X11L"): {
        **{d: {699.0} for d in _dates("2026-08-11", "2026-08-15", "2026-08-16", "2026-08-17", "2026-08-18", "2026-08-19")},
        **{d: {749.0} for d in _dates("2026-08-20", "2026-08-21", "2026-08-22", "2026-08-24")},
        "2026-08-23": {1099.0},
    },
    ("TCL", "65C8L"): {
        **{d: {699.0} for d in _dates("2026-08-11", "2026-08-15", "2026-08-16", "2026-08-17", "2026-08-18", "2026-08-19")},
        **{d: {749.0} for d in _dates("2026-08-20", "2026-08-21", "2026-08-22", "2026-08-24")},
        "2026-08-23": {1099.0},
    },
    ("TCL", "65C7L"): {
        **{d: {699.0} for d in _dates("2026-08-11", "2026-08-15", "2026-08-16", "2026-08-17", "2026-08-18", "2026-08-19")},
        **{d: {749.0} for d in _dates("2026-08-20", "2026-08-21", "2026-08-22", "2026-08-24")},
    },
    ("TCL", "55C8L"): {
        **{d: {699.0} for d in _dates("2026-08-11", "2026-08-15", "2026-08-16", "2026-08-17", "2026-08-18", "2026-08-19")},
        **{d: {749.0} for d in _dates("2026-08-20", "2026-08-21", "2026-08-22", "2026-08-24")},
    },
    ("LG", "48C6"): {
        **{d: {399.0} for d in _dates("2026-08-12", "2026-08-13", "2026-08-14", "2026-08-15", "2026-08-16", "2026-08-17", "2026-08-19")},
        **{d: {649.0} for d in _dates("2026-08-18", "2026-08-21", "2026-08-22")},
    },
    ("LG", "50QNED85B"): {
        **{d: {399.0} for d in _dates("2026-08-12", "2026-08-13", "2026-08-14", "2026-08-15", "2026-08-16", "2026-08-17", "2026-08-19")},
    },
    ("LG", "55MRGB86B6"): {
        **{d: {399.0} for d in _dates("2026-08-12", "2026-08-13", "2026-08-14", "2026-08-15", "2026-08-16", "2026-08-17", "2026-08-19")},
        **{d: {649.0} for d in _dates("2026-08-18", "2026-08-21", "2026-08-22")},
    },
    ("LG", "65G6"): {
        **{d: {399.0} for d in _dates("2026-08-12", "2026-08-13", "2026-08-14", "2026-08-15", "2026-08-16", "2026-08-17", "2026-08-19")},
        **{d: {649.0} for d in _dates("2026-08-18", "2026-08-21", "2026-08-22")},
    },
}


# 周目录中同一问题留下的价格提示。只清空价格，不删除商品条目。
BAD_CATALOG_HINTS: dict[str, dict[str, set[float]]] = {
    "boulanger_fr_20260817.csv": {
        **{ref: {699.0} for ref in ("1238818", "1238872", "1238873", "1238877")},
        **{ref: {399.0} for ref in ("1239790", "1239830", "1239936", "1240876")},
    },
    "boulanger_fr_20260824.csv": {
        **{ref: {749.0} for ref in ("1238818", "1238872", "1238873", "1238877")},
    },
}


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), list(reader)


def _write_csv(path: Path, fieldnames: list[str], rows: Iterable[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temp_path, path)


def _as_float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def is_confirmed_bad_price(row: dict[str, str]) -> bool:
    if (row.get("Platform") or "").strip().lower() != "boulanger":
        return False
    key = ((row.get("Brand") or "").strip(), (row.get("Product Name") or "").strip())
    price = _as_float(row.get("Price") or "")
    return price in BAD_PRICE_ROWS.get(key, {}).get((row.get("Date") or "").strip(), set())


def quarantine_prices(prices_path: Path, quarantine_path: Path, *, apply: bool) -> int:
    fieldnames, rows = _read_csv(prices_path)
    removed = [row for row in rows if is_confirmed_bad_price(row)]
    kept = [row for row in rows if not is_confirmed_bad_price(row)]
    print(f"[prices] {prices_path}: confirmed_bad={len(removed)}, kept={len(kept)}")
    if not apply or not removed:
        return len(removed)

    audit_fields = fieldnames + ["Quarantine Reason"]
    existing: list[dict[str, str]] = []
    if quarantine_path.exists():
        _, existing = _read_csv(quarantine_path)
    combined: dict[tuple[str, ...], dict[str, str]] = {}
    for row in [*existing, *removed]:
        audited = {**row, "Quarantine Reason": row.get("Quarantine Reason") or REASON}
        key = tuple(audited.get(name, "") for name in fieldnames)
        combined[key] = audited
    _write_csv(quarantine_path, audit_fields, combined.values())
    _write_csv(prices_path, fieldnames, kept)
    return len(removed)


def _catalog_ref(row: dict[str, str]) -> str:
    return (row.get("url") or "").rstrip("/").split("/")[-1]


def quarantine_catalogs(catalog_dir: Path, quarantine_path: Path, *, apply: bool) -> int:
    audited: list[dict[str, str]] = []
    audit_fields: list[str] = []
    total = 0
    for filename, refs in BAD_CATALOG_HINTS.items():
        path = catalog_dir / filename
        if not path.exists():
            continue
        fieldnames, rows = _read_csv(path)
        if not audit_fields:
            audit_fields = ["catalog_snapshot", *fieldnames, "Quarantine Reason"]
        changed = 0
        for row in rows:
            price = _as_float(row.get("price_hint_eur") or "")
            if price not in refs.get(_catalog_ref(row), set()):
                continue
            audited.append({"catalog_snapshot": filename, **row, "Quarantine Reason": REASON})
            if apply:
                row["price_hint_eur"] = ""
                row["price_local"] = ""
                row["price_eur"] = ""
            changed += 1
        print(f"[catalog] {filename}: confirmed_bad={changed}")
        total += changed
        if apply and changed:
            _write_csv(path, fieldnames, rows)

    if apply and audited:
        existing: list[dict[str, str]] = []
        if quarantine_path.exists():
            _, existing = _read_csv(quarantine_path)
        combined: dict[tuple[str, ...], dict[str, str]] = {}
        keys = [name for name in audit_fields if name != "Quarantine Reason"]
        for row in [*existing, *audited]:
            combined[tuple(row.get(name, "") for name in keys)] = row
        _write_csv(quarantine_path, audit_fields, combined.values())
    return total


def main() -> int:
    repo_root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prices", type=Path, default=repo_root / "raw" / "prices.csv")
    parser.add_argument("--catalog-dir", type=Path, default=repo_root / "catalog")
    parser.add_argument("--skip-catalog", action="store_true")
    parser.add_argument("--apply", action="store_true", help="真正写入；默认只报告命中数")
    args = parser.parse_args()

    prices_quarantine = args.prices.parent / "quarantine" / "boulanger_card_price_leaks_20260811_20260824.csv"
    catalog_quarantine = args.catalog_dir / "quarantine" / "boulanger_card_price_leaks_20260817_20260824.csv"
    removed = quarantine_prices(args.prices, prices_quarantine, apply=args.apply)
    catalog_changed = 0
    if not args.skip_catalog:
        catalog_changed = quarantine_catalogs(args.catalog_dir, catalog_quarantine, apply=args.apply)
    mode = "applied" if args.apply else "dry-run"
    print(f"[{mode}] price_rows={removed}, catalog_hints={catalog_changed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
