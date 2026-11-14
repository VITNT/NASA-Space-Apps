from __future__ import annotations

import unittest
import json
from pathlib import Path

import duckdb

from firms_pipeline.merge import ARCHIVE_CUTOFF_YEAR, merge_selection_sql


class MergePolicyTests(unittest.TestCase):
    def test_complete_archives_win_through_2025(self) -> None:
        sql = merge_selection_sql("history/**/*.parquet", "current/**/*.parquet")
        self.assertIn(f"year <= {ARCHIVE_CUTOFF_YEAR}", sql)
        self.assertIn(f"year > {ARCHIVE_CUTOFF_YEAR}", sql)
        self.assertEqual(sql.count("UNION ALL BY NAME"), 1)

    def test_snpp_is_an_independent_dataset_and_platform(self) -> None:
        from typing import get_args

        from firms_pipeline.api import Dataset, Platform

        self.assertIn("viirs_snpp", get_args(Dataset))
        self.assertIn("snpp", get_args(Platform))


class MergedDataIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]
        cls.manifest = json.loads(
            (cls.root / "data" / "catalog" / "merged_manifest.json").read_text(encoding="utf-8")
        )
        cls.monthly = cls.root / "data" / "merged" / "aggregates" / "monthly_global.parquet"
        cls.daily = cls.root / "data" / "merged" / "aggregates" / "daily_global.parquet"

    def test_overlap_is_prevented_and_row_counts_reconcile(self) -> None:
        validation = self.manifest["validation"]
        self.assertEqual(validation["overlap_rows"], 0)
        self.assertEqual(validation["output_rows"], validation["expected_rows"])
        self.assertEqual(self.manifest["precedence"]["annual_archives_through_year"], 2025)
        self.assertEqual(self.manifest["precedence"]["current_archive_nrt_from_year"], 2026)

    def test_historical_platform_coverage_includes_snpp(self) -> None:
        rows = duckdb.connect().execute(
            "SELECT dataset, platform, min(activity_date), max(activity_date) "
            "FROM read_parquet(?) GROUP BY dataset, platform ORDER BY dataset, platform",
            [str(self.daily)],
        ).fetchall()
        coverage = {(dataset, platform): (str(first), str(last)) for dataset, platform, first, last in rows}
        self.assertEqual(coverage[("modis", "terra")][0], "2000-11-01")
        self.assertEqual(coverage[("modis", "aqua")][0], "2002-07-04")
        self.assertEqual(coverage[("viirs_snpp", "snpp")][0], "2012-01-20")
        self.assertEqual(coverage[("viirs_noaa20", "noaa20")][0], "2018-04-01")
        self.assertEqual(coverage[("viirs_snpp", "snpp")][1], "2025-12-31")


if __name__ == "__main__":
    unittest.main()
