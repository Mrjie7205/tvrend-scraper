"""离线验证本轮 artifact 身份、失败状态和部分成功交接，防止同日残留造成假绿。"""

from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import amazon_artifact_gate as gate

# 与 run_weekly 正式输出保持相同的 17 列；包含无价商品，避免简化 fixture 漏掉真实契约。
CATALOG_COLUMNS = (
    "brand_raw", "raw_text", "url", "size_hint_inch", "price_hint_eur",
    "price_local", "currency", "price_eur", "platform", "country", "scraped_at",
    "asin", "elkjop_sku", "model_year", "filter_year", "source_brand", "fx_rate_date",
)


class AmazonArtifactGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.checkout = self.root / "checkout"
        self.checkout.mkdir()
        self.artifacts = self.root / "artifacts"
        self.report = self.root / "report"
        self.start = "2026-09-29T09:00:00+00:00"
        self.fresh = "2026-09-29T09:20:00Z"
        self.finish = "2026-09-29T09:21:00+00:00"

    def csv(self, folder: Path, country: str, *, rows: int = 100, stamp: str | None = None,
            no_quote_rows: int = 0, no_quote_currency: str = "", override: dict | None = None) -> Path:
        folder.mkdir(parents=True, exist_ok=True)
        stamp = stamp or self.fresh
        day = gate.parse_time(stamp).strftime("%Y%m%d")
        path = folder / f"amazon_{country}_{day}.csv"
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CATALOG_COLUMNS)
            writer.writeheader()
            for index in range(rows):
                row = {"brand_raw": "TCL", "platform": "Amazon", "country": country.upper(),
                       "scraped_at": stamp, "url": f"https://www.amazon.{country}/dp/item{index}",
                       "raw_text": 'TV, 55"\n2026', "size_hint_inch": "55.0", "asin": f"B{index:09}",
                       "currency": "GBP" if country == "gb" else "EUR",
                       "price_local": "289.0" if country == "gb" else "369.0",
                       "price_eur": "336.15" if country == "gb" else "369.0",
                       "price_hint_eur": "336.15" if country == "gb" else "369.0"}
                if index < no_quote_rows:
                    row.update({column: "" for column in gate.PRICE_COLUMNS})
                    row["currency"] = no_quote_currency
                if override:
                    row.update(override)
                writer.writerow(row)
        return path

    def stage(self, country: str, outcome: str = "success", *, rows: int = 100, stamp: str | None = None) -> dict:
        source = self.root / f"source-{country}"
        self.csv(source, country, rows=rows, stamp=stamp)
        context = self.root / f"context-{country}.json"
        gate.write_json(context, {"schemaVersion": 1, "country": country, "runId": "12345",
                                 "runAttempt": "2", "headSha": "abc123", "startedAt": self.start})
        with patch.object(gate, "now", return_value=self.finish):
            return gate.stage(context, source, self.artifacts / f"amazon-result-{country}", outcome)

    def collect(self, result: str = "success") -> dict:
        return gate.collect(self.artifacts, self.checkout, self.report, "12345", "2", "abc123", result)

    def test_old_same_day_checkout_cannot_supply_missing_country(self) -> None:
        old = self.csv(self.checkout, "it", stamp="2026-09-29T08:39:04Z")
        before = old.read_bytes()
        for country in ("de", "gb", "es"):
            self.stage(country)
        result = self.collect("failure")
        self.assertFalse(result["complete"])
        self.assertEqual(3, len(result["publishFiles"]))
        self.assertEqual("failed", result["markets"]["it"]["status"])
        self.assertEqual(before, old.read_bytes())
        self.assertNotIn(old.name, (self.report / "publish-files.txt").read_text())

    def test_success_outcome_cannot_upload_old_same_day_csv(self) -> None:
        result = self.stage("it", stamp="2026-09-29T08:39:04Z")
        self.assertEqual("failed", result["status"])
        self.assertEqual([], list((self.artifacts / "amazon-result-it").glob("*.csv")))

    def test_failed_scrape_with_fresh_residual_csv_never_publishes_it(self) -> None:
        result = self.stage("it", "failure")
        self.assertEqual("failed", result["status"])
        self.assertEqual([], list((self.artifacts / "amazon-result-it").glob("*.csv")))
        self.assertEqual([], self.collect("failure")["publishFiles"])

    def test_missing_country_marks_failure_and_preserves_valid_countries(self) -> None:
        for country in ("de", "gb", "it"):
            self.stage(country)
        result = self.collect()
        self.assertFalse(result["complete"])
        self.assertEqual(3, len(list(self.checkout.glob("*.csv"))))
        self.assertEqual("failed", result["markets"]["es"]["status"])

    def test_empty_data_is_not_a_success_artifact(self) -> None:
        self.assertEqual("failed", self.stage("it", rows=0)["status"])
        self.assertFalse(self.collect()["complete"])
        self.assertEqual([], list(self.checkout.glob("*.csv")))

    def test_no_artifacts_does_not_import_any_checkout_file(self) -> None:
        for country in gate.COUNTRIES:
            self.csv(self.checkout, country)
        result = self.collect("failure")
        self.assertFalse(result["complete"])
        self.assertEqual([], result["publishFiles"])
        self.assertEqual("", (self.report / "publish-countries.txt").read_text())

    def test_all_four_successful_artifacts_form_publish_list(self) -> None:
        for country in gate.COUNTRIES:
            self.stage(country)
        result = self.collect()
        self.assertTrue(result["complete"])
        self.assertEqual(4, len(result["publishFiles"]))
        for country in gate.COUNTRIES:
            item = result["markets"][country]
            self.assertEqual(100, item["rowCount"])
            self.assertEqual((self.artifacts / f"amazon-result-{country}" / item["file"]).read_bytes(),
                             (self.checkout / item["file"]).read_bytes())

    def test_failed_upstream_job_cannot_become_green_even_with_four_artifacts(self) -> None:
        for country in gate.COUNTRIES:
            self.stage(country)
        result = self.collect("failure")
        self.assertFalse(result["complete"])
        self.assertEqual(4, len(result["publishFiles"]))

    def test_previous_attempt_manifest_rejected(self) -> None:
        self.stage("it")
        path = self.artifacts / "amazon-result-it" / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest["runAttempt"] = "1"
        gate.write_json(path, manifest)
        self.assertEqual([], self.collect()["publishFiles"])

    def test_modified_csv_hash_rejected(self) -> None:
        self.stage("it")
        path = next((self.artifacts / "amazon-result-it").glob("*.csv"))
        path.write_bytes(path.read_bytes().replace(b"item0", b"other0"))
        self.assertEqual([], self.collect()["publishFiles"])

    def test_wrong_country_in_artifact_rows_rejected(self) -> None:
        self.stage("it")
        path = next((self.artifacts / "amazon-result-it").glob("*.csv"))
        path.write_bytes(path.read_bytes().replace(b"IT", b"DE"))
        self.assertEqual([], self.collect()["publishFiles"])

    def test_real_schema_mixed_priced_and_no_quote_rows_accepts_each_market(self) -> None:
        for country in gate.COUNTRIES:
            for no_quote_currency in ("", "GBP" if country == "gb" else "EUR"):
                with self.subTest(country=country, no_quote_currency=no_quote_currency):
                    path = self.csv(self.root / "mixed", country, no_quote_rows=23,
                                    no_quote_currency=no_quote_currency)
                    info = gate.inspect_csv(path, country, self.start, self.finish)
                    self.assertEqual(100, info["rowCount"])
                    self.assertEqual(77, info["pricedRowCount"])

    def test_price_validation_rejects_invalid_nonempty_values_and_partial_prices(self) -> None:
        cases = [{column: value} for column in gate.PRICE_COLUMNS
                 for value in ("0", "-1", "not-a-price", "NaN", "Infinity", "")]
        cases.extend([{"currency": ""}, {"currency": "GBP"}])
        for override in cases:
            with self.subTest(override=override):
                path = self.csv(self.root / "invalid", "it", override=override)
                with self.assertRaises(ValueError):
                    gate.inspect_csv(path, "it", self.start, self.finish)

    def test_all_no_quote_rows_not_counted_as_prices_and_wrong_currency_still_rejected(self) -> None:
        path = self.csv(self.root / "unpriced", "it", no_quote_rows=100)
        info = gate.inspect_csv(path, "it", self.start, self.finish)
        self.assertEqual(0, info["pricedRowCount"])
        self.assertEqual(100, info["rowCount"])
        path = self.csv(self.root / "unpriced", "it", no_quote_rows=100, no_quote_currency="GBP")
        with self.assertRaisesRegex(ValueError, "wrong_currency"):
            gate.inspect_csv(path, "it", self.start, self.finish)

    def test_malformed_manifest_does_not_discard_other_successful_country(self) -> None:
        for invalid in ([], None, "unexpected"):
            with self.subTest(invalid=invalid):
                self.artifacts.mkdir(exist_ok=True)
                bad_dir = self.artifacts / "amazon-result-it"
                bad_dir.mkdir(exist_ok=True)
                (bad_dir / "manifest.json").write_text(json.dumps(invalid), encoding="utf-8")
                if not (self.artifacts / "amazon-result-de").exists():
                    self.stage("de")
                result = self.collect("failure")
                self.assertEqual(["amazon_de_20260929.csv"], result["publishFiles"])
                self.assertFalse(result["complete"])

    def test_invalid_timestamp_types_fail_only_their_market(self) -> None:
        self.stage("it")
        self.stage("de")
        path = self.artifacts / "amazon-result-it" / "manifest.json"
        manifest = json.loads(path.read_text())
        for invalid in ({}, [], None, 123):
            with self.subTest(invalid=invalid):
                manifest["startedAt"] = invalid
                gate.write_json(path, manifest)
                result = self.collect()
                self.assertEqual(["amazon_de_20260929.csv"], result["publishFiles"])
                self.assertFalse(result["complete"])

    def test_skipped_scrape_never_uploads_checkout_csv(self) -> None:
        result = self.stage("it", "skipped")
        self.assertEqual("failed", result["status"])
        self.assertEqual([], list((self.artifacts / "amazon-result-it").glob("*.csv")))

    def test_second_precision_accepts_new_output_during_start_second(self) -> None:
        self.start = "2026-09-29T09:00:00.500000+00:00"
        self.fresh = "2026-09-29T09:00:00Z"
        self.finish = "2026-09-29T09:00:00.900000+00:00"
        self.assertEqual("validated", self.stage("it")["status"])
        self.assertEqual(["amazon_it_20260929.csv"], self.collect()["publishFiles"])

    def test_same_second_old_file_rejected_by_before_run_hash(self) -> None:
        source = self.root / "source-it"
        self.csv(source, "it", stamp="2026-09-29T09:00:00Z")
        context = self.root / "context-it.json"
        with patch.object(gate, "now", return_value="2026-09-29T09:00:00.500000+00:00"):
            gate.prepare(context, "it", "12345", "2", "abc123", source)
        with patch.object(gate, "now", return_value="2026-09-29T09:00:01+00:00"):
            result = gate.stage(context, source, self.artifacts / "amazon-result-it", "success")
        self.assertEqual("failed", result["status"])
        self.assertNotIn("existingCatalogs", result)
        self.assertEqual([], self.collect()["publishFiles"])

    def test_cross_midnight_completion_uses_actual_scrape_date(self) -> None:
        self.start = "2026-09-29T23:59:00+00:00"
        self.fresh = "2026-09-30T00:20:00Z"
        self.finish = "2026-09-30T00:21:00+00:00"
        result = self.stage("it")
        self.assertEqual("amazon_it_20260930.csv", result["file"])
        self.assertEqual([result["file"]], self.collect()["publishFiles"])

    def test_workflow_keeps_failure_visible_and_uses_isolated_artifacts(self) -> None:
        text = (SCRIPTS.parent / ".github/workflows/daily-amazon.yml").read_text(encoding="utf-8")
        self.assertNotIn("continue-on-error:", text)
        self.assertIn("fail-fast: false", text)
        self.assertIn("path: _amazon_download", text)
        self.assertIn("merge-multiple: false", text)
        self.assertLess(text.index("git commit -m"), text.index("amazon_artifact_gate.py gate"))


if __name__ == "__main__":
    unittest.main()
