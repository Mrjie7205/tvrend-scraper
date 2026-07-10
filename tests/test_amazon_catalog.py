"""Amazon catalog 的纯函数回归测试，不访问网络。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from catalog_scrape.adapters.amazon import AmazonDeCatalogAdapter  # noqa: E402


class AmazonCatalogSeriesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = AmazonDeCatalogAdapter()

    def item(self, brand: str, title: str, asin: str = "B000000000"):
        return self.adapter._build_item(asin, title, brand, 55, "")

    def test_extracts_current_series_from_market_skus(self) -> None:
        cases = (
            ("Samsung", "Samsung QE48S95HATXXU OLED TV 2026", "S95H"),
            ("Samsung", "Samsung GQ65QN90FATXZG Neo QLED", "QN90F"),
            ("LG", "LG OLED55C6ELA OLED evo TV", "C6"),
            ("LG", "LG 55QNED85A6A TV", "QNED85A"),
            ("TCL", "TCL 75C8L Premium QD Mini LED", "C8L"),
            ("TCL", "TCL 65C6K PRO Mini LED", "C6KPRO"),
            ("Hisense", "Hisense 65U7Q PRO Mini LED", "U7QPRO"),
            ("Sony", "Sony K-55XR80M2 BRAVIA 8 II", "BRAVIA8II"),
        )
        for brand, title, expected in cases:
            with self.subTest(title=title):
                self.assertEqual(expected, self.adapter._series_hint(self.item(brand, title)))

    def test_variant_seed_selection_keeps_two_fallbacks_per_series(self) -> None:
        seeds = []
        for index, size in enumerate((48, 55, 65, 77)):
            item = self.item("Samsung", f"Samsung QE{size}S95HATXXU OLED TV 2026", f"B00000000{index}")
            item.size_hint_inch = size
            item.extra["variant_hint"] = True
            seeds.append(item)
        selected = self.adapter._select_variant_seeds(seeds)
        self.assertEqual(2, len(selected))

    def test_series_rescue_prioritizes_series_with_fewer_seen_sizes(self) -> None:
        items = [
            self.item("Samsung", "Samsung QE55S95HATXXU OLED TV 2026", "B000000001"),
            self.item("Samsung", "Samsung GQ55QN90FATXZG Neo QLED", "B000000002"),
            self.item("Samsung", "Samsung GQ65QN90FATXZG Neo QLED", "B000000003"),
        ]
        items[0].size_hint_inch = 55
        items[1].size_hint_inch = 55
        items[2].size_hint_inch = 65
        queries = self.adapter._series_rescue_queries(items)
        self.assertEqual("samsung s95h", queries[0])


if __name__ == "__main__":
    unittest.main()
