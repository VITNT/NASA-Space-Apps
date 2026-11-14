from __future__ import annotations

import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from firms_pipeline.api import create_app


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ApiIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(create_app(PROJECT_ROOT))

    def test_health_uses_validated_manifest(self) -> None:
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data_profile"], "merged")
        self.assertEqual(response.json()["source_rows"], 568_609_549)

    def test_demo_frontend_is_served(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("FIRMS Fire Activity Explorer", response.text)
        self.assertEqual(self.client.get("/static/styles.css").status_code, 200)

    def test_coverage_keeps_sensors_and_platforms_separate(self) -> None:
        response = self.client.get("/v1/coverage")
        self.assertEqual(response.status_code, 200)
        keys = {(row["dataset"], row["platform"]) for row in response.json()["data"]}
        self.assertEqual(
            keys,
            {
                ("modis", "aqua"),
                ("modis", "terra"),
                ("viirs_noaa20", "noaa20"),
                ("viirs_snpp", "snpp"),
            },
        )

    def test_monthly_endpoint_exposes_partial_months(self) -> None:
        response = self.client.get(
            "/v1/activity/monthly",
            params={
                "dataset": "viirs_noaa20",
                "platform": "noaa20",
                "start_date": "2026-01-01",
                "end_date": "2026-12-31",
                "limit": 20,
            },
        )
        self.assertEqual(response.status_code, 200)
        rows = response.json()["data"]
        partial = {(row["year"], row["month"], row["period_status"]) for row in rows if not row["is_complete_month"]}
        self.assertEqual(partial, {(2026, 11, "partial_end")})

    def test_hotspot_query_reads_real_points_and_honors_limit(self) -> None:
        response = self.client.get(
            "/v1/hotspots",
            params={
                "dataset": "viirs_noaa20",
                "start_date": "2025-11-13",
                "end_date": "2025-11-13",
                "min_lat": -90,
                "max_lat": 90,
                "min_lon": -180,
                "max_lon": 180,
                "limit": 2,
            },
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 2)
        self.assertTrue(payload["truncated"])
        self.assertEqual({row["dataset"] for row in payload["data"]}, {"viirs_noaa20"})

    def test_historical_snpp_points_are_queryable(self) -> None:
        response = self.client.get(
            "/v1/hotspots",
            params={
                "dataset": "viirs_snpp",
                "platform": "snpp",
                "start_date": "2012-01-20",
                "end_date": "2012-01-20",
                "min_lat": -90,
                "max_lat": 90,
                "min_lon": -180,
                "max_lon": 180,
                "limit": 1,
            },
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["data"][0]["dataset"], "viirs_snpp")
        self.assertEqual(payload["data"][0]["platform"], "snpp")

    def test_point_query_rejects_more_than_31_days(self) -> None:
        response = self.client.get(
            "/v1/hotspots",
            params={
                "dataset": "modis",
                "start_date": "2026-01-01",
                "end_date": "2026-02-01",
                "min_lat": -90,
                "max_lat": 90,
                "min_lon": -180,
                "max_lon": 180,
            },
        )
        self.assertEqual(response.status_code, 422)

    def test_bbox_summary_reconciles_real_daily_world_count(self) -> None:
        response = self.client.get(
            "/v1/summary/bbox",
            params={
                "dataset": "modis",
                "start_date": "2026-06-01",
                "end_date": "2026-06-01",
                "min_lat": -90,
                "max_lat": 90,
                "min_lon": -180,
                "max_lon": 180,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual({row["platform"] for row in response.json()["data"]}, {"aqua", "terra"})

    def test_spatial_monthly_returns_area_normalized_real_cells(self) -> None:
        response = self.client.get(
            "/v1/spatial/monthly",
            params={
                "resolution": 4,
                "dataset": "modis",
                "year": 2026,
                "month": 6,
                "min_lat": 10,
                "max_lat": 15,
                "min_lon": 100,
                "max_lon": 110,
                "limit": 2,
            },
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 2)
        self.assertTrue(payload["truncated"])
        for row in payload["data"]:
            self.assertEqual(row["resolution"], 4)
            self.assertGreater(row["cell_area_km2"], 0)
            self.assertAlmostEqual(
                row["detections_per_km2"], row["detection_count"] / row["cell_area_km2"], places=10
            )

    def test_spatial_endpoint_rejects_unsupported_resolution(self) -> None:
        response = self.client.get(
            "/v1/spatial/monthly",
            params={"resolution": 3, "dataset": "modis", "year": 2026, "month": 6},
        )
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
