from __future__ import annotations

import unittest
from fastapi.testclient import TestClient

from firms_pipeline.api import create_app


class ApiIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(create_app())

    def test_health_uses_validated_manifest(self) -> None:
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data_profile"], "merged")
        self.assertEqual(response.json()["source_rows"], 568_609_549)

    def test_demo_frontend_is_served(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("<title>Luminite</title>", response.text)
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

    def test_viewport_grid_keeps_counts_stable_when_panning(self) -> None:
        params = dict(
            dataset="viirs_noaa20", start_date="2025-11-13", end_date="2025-11-15",
            min_lat=-90, max_lat=90, min_lon=-180, max_lon=180, cell_size=0.125,
            days=["2025-11-13", "2025-11-15"],
        )
        left = self.client.get("/v1/spatial/grid", params={**params, "viewport": [8, 16, 96, 108]})
        right = self.client.get("/v1/spatial/grid", params={**params, "viewport": [8, 16, 100, 112]})
        self.assertEqual(left.status_code, 200)
        self.assertEqual(right.status_code, 200)
        self.assertEqual(left.json()["cell_size"], 0.125)
        shared_left = [row for row in left.json()["data"] if row["west"] >= 100]
        shared_right = [row for row in right.json()["data"] if row["west"] < 108]
        self.assertTrue(shared_left)
        self.assertEqual(shared_left, shared_right)
        self.assertTrue(all(row["observed_days"] <= 2 for row in shared_left))

    def test_hotspot_export_is_complete_and_matches_its_count(self) -> None:
        params = dict(start_date="2025-02-01", end_date="2025-02-28",
                      min_lat=10.35, max_lat=14.8, min_lon=102.3, max_lon=107.75)
        count = self.client.get("/v1/export/hotspots/count", params=params).json()
        response = self.client.get("/v1/export/hotspots.csv", params=params)
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response.headers["content-disposition"])
        lines = response.text.splitlines()
        self.assertEqual(len(lines) - 1, count["count"])
        self.assertGreater(count["count"], 15_000)  # more than the old 5,000-per-sensor cap
        header = lines[0].split(",")
        self.assertIn("counts_toward_calendar", header)
        flags = [line.split(",")[header.index("counts_toward_calendar")] for line in lines[1:]]
        self.assertEqual(flags.count("yes"), count["qualifying"])

    def test_hotspot_export_honors_selected_days(self) -> None:
        response = self.client.get("/v1/export/hotspots.csv", params=dict(
            start_date="2025-02-03", end_date="2025-02-07", days=["2025-02-03", "2025-02-07"],
            min_lat=10.35, max_lat=14.8, min_lon=102.3, max_lon=107.75))
        self.assertEqual({line.split(",")[0] for line in response.text.splitlines()[1:]}, {"2025-02-03", "2025-02-07"})

    def test_hotspot_export_refuses_rather_than_truncates(self) -> None:
        response = self.client.get("/v1/export/hotspots.csv", params=dict(
            start_date="2025-02-01", end_date="2025-02-28", min_lat=-90, max_lat=90, min_lon=-180, max_lon=180))
        self.assertEqual(response.status_code, 413)

    def test_daily_coverage_lists_reporting_platforms(self) -> None:
        rows = self.client.get("/v1/coverage/daily", params=dict(
            start_date="2025-02-01", end_date="2025-02-01")).json()["data"]
        self.assertEqual({row["platform"] for row in rows}, {"terra", "aqua", "snpp", "noaa20"})

    def test_map_points_return_each_detection_in_the_viewport(self) -> None:
        params = dict(
            dataset="viirs_noaa20", start_date="2025-11-13", end_date="2025-11-13",
            min_lat=-90, max_lat=90, min_lon=-180, max_lon=180, viewport=[10, 15, 102, 108],
        )
        points = self.client.get("/v1/spatial/points", params=params)
        grid = self.client.get("/v1/spatial/grid", params={**params, "cell_size": 1})
        self.assertEqual(points.status_code, 200)
        body = points.json()
        self.assertFalse(body["truncated"])
        rows = [dict(zip(body["columns"], row)) for row in body["rows"]]
        self.assertEqual(len(rows), sum(cell["detection_count"] for cell in grid.json()["data"]))
        self.assertTrue(rows)
        for row in rows:
            self.assertTrue(10 <= row["latitude"] < 15 and 102 <= row["longitude"] < 108)
            self.assertEqual(row["platform"], "noaa20")
            self.assertTrue(row["observed_utc"].startswith("2025-11-13T"))
            self.assertIn(row["daynight"], ("D", "N"))

    def test_grid_by_platform_partitions_the_sensor_grid(self) -> None:
        params = dict(
            dataset="modis", start_date="2025-02-01", end_date="2025-02-28",
            min_lat=-90, max_lat=90, min_lon=-180, max_lon=180, viewport=[0, 30, 90, 120], cell_size=1,
        )
        def counts(**extra: str) -> dict[tuple[float, float], int]:
            response = self.client.get("/v1/spatial/grid", params={**params, **extra})
            self.assertEqual(response.status_code, 200)
            return {(cell["south"], cell["west"]): cell["detection_count"] for cell in response.json()["data"]}

        combined = counts()
        terra = self.client.get("/v1/spatial/grid", params={**params, "platform": "terra"}).json()["data"]
        aqua = counts(platform="aqua")
        self.assertTrue(terra and aqua)
        self.assertTrue(all(cell["platforms"] == ["terra"] for cell in terra))
        merged = {(cell["south"], cell["west"]): cell["detection_count"] for cell in terra}
        for key, count in aqua.items():
            merged[key] = merged.get(key, 0) + count
        self.assertEqual(merged, combined)
        self.assertEqual(counts(dataset="viirs_snpp", platform="terra"), {})

    def test_map_points_report_a_view_with_too_many_detections(self) -> None:
        body = self.client.get("/v1/spatial/points", params=dict(
            start_date="2025-02-01", end_date="2025-02-28",
            min_lat=-90, max_lat=90, min_lon=-180, max_lon=180,
        )).json()
        self.assertTrue(body["truncated"])
        self.assertEqual(body["rows"], [])

    def test_grid_rejects_a_worldwide_detail_request(self) -> None:
        response = self.client.get("/v1/spatial/grid", params=dict(
            start_date="2025-11-13", end_date="2025-11-13",
            min_lat=-90, max_lat=90, min_lon=-180, max_lon=180, cell_size=0.125,
        ))
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

    CAMBODIA = dict(min_lat=10.35, max_lat=14.8, min_lon=102.3, max_lon=107.75)

    def _month(self, **extra) -> dict:
        params = dict(start_date="2025-02-01", end_date="2025-02-28", interval="month", **self.CAMBODIA, **extra)
        response = self.client.get("/v1/calendar", params=params)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["data"][0]

    def test_shapes_narrow_the_bounding_box(self) -> None:
        box = self._month()["active_cell_days"]
        country = self._month(country="Cambodia")["active_cell_days"]
        circle = self._month(circle="12.5,105,50")["active_cell_days"]
        # A polygon that traces the whole box counts exactly what the box does.
        whole = self._month(polygon="10.35,102.3,10.35,107.75,14.8,107.75,14.8,102.3")["active_cell_days"]
        half = self._month(polygon="10.35,102.3,10.35,105,14.8,105,14.8,102.3")["active_cell_days"]
        self.assertLess(circle, country)
        self.assertLess(country, box)
        self.assertEqual(whole, box)
        self.assertLess(0, half)
        self.assertLess(half, box)

    def test_circle_points_lie_within_the_radius(self) -> None:
        from math import asin, cos, radians, sin, sqrt

        payload = self.client.get("/v1/spatial/points", params=dict(
            start_date="2025-02-01", end_date="2025-02-03", circle="12.5,105,30", **self.CAMBODIA)).json()
        latitude, longitude = payload["columns"].index("latitude"), payload["columns"].index("longitude")
        self.assertGreater(len(payload["rows"]), 0)
        for row in payload["rows"]:
            a = sin(radians(row[latitude] - 12.5) / 2) ** 2 + cos(radians(12.5)) * cos(radians(row[latitude])) * sin(
                radians(row[longitude] - 105) / 2) ** 2
            self.assertLessEqual(2 * 6371.0088 * asin(sqrt(a)), 30.001)

    def test_shapes_are_validated(self) -> None:
        for extra in (dict(circle="12.5,105"), dict(circle="12.5,105,99999"), dict(polygon="1,2,3,4"),
                      dict(polygon="1,2,3,4,x,6"), dict(country="Atlantis"), dict(circle="12.5,105,50", country="Cambodia")):
            response = self.client.get("/v1/calendar", params=dict(
                start_date="2025-02-01", end_date="2025-02-28", **self.CAMBODIA, **extra))
            self.assertEqual(response.status_code, 422, extra)

    def test_countries_list_bounds_that_cross_the_antimeridian(self) -> None:
        rows = {row["name"]: row for row in self.client.get("/v1/areas/countries").json()["data"]}
        self.assertGreater(len(rows), 200)
        self.assertLess(rows["Cambodia"]["min_lon"], rows["Cambodia"]["max_lon"])
        self.assertGreater(rows["New Zealand"]["min_lon"], rows["New Zealand"]["max_lon"])

    def test_month_to_date_counts_only_the_first_days(self) -> None:
        whole = self._month()["active_cell_days"]
        partial = self._month(through_day=10)["active_cell_days"]
        days = self.client.get("/v1/calendar", params=dict(
            start_date="2025-02-01", end_date="2025-02-10", **self.CAMBODIA)).json()["data"]
        self.assertEqual(partial, sum(row["active_cell_days"] for row in days))
        self.assertLess(partial, whole)

    def test_platform_filter_counts_like_for_like(self) -> None:
        every = self._month()
        modis = self._month(platforms=["terra", "aqua"])
        self.assertEqual(modis["viirs_snpp"], 0)
        self.assertEqual(modis["modis"], every["modis"])
        self.assertLess(modis["active_cell_days"], every["active_cell_days"])

    def test_calendar_reports_fire_radiative_power_per_record(self) -> None:
        row = self._month()
        summary = self.client.get("/v1/summary/bbox", params=dict(
            start_date="2025-02-01", end_date="2025-02-28", **self.CAMBODIA)).json()["data"]
        # The calendar sums only qualifying detections, so it cannot exceed the record's total.
        modis_total = sum(item["frp_sum_mw"] for item in summary if item["dataset"] == "modis")
        self.assertGreater(row["modis_frp_mw"], 0)
        self.assertLessEqual(row["modis_frp_mw"], modis_total + 1)

    def test_hotspot_export_covers_more_than_a_month(self) -> None:
        params = dict(start_date="2025-01-01", end_date="2025-03-31", circle="12.5,105,20", **self.CAMBODIA)
        count = self.client.get("/v1/export/hotspots/count", params=params).json()
        response = self.client.get("/v1/export/hotspots.csv", params=params)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.text.splitlines()) - 1, count["count"])
        self.assertEqual({line[:7] for line in response.text.splitlines()[1:]}, {"2025-01", "2025-02", "2025-03"})


if __name__ == "__main__":
    unittest.main()


class AnomalyAndCacheTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(create_app())

    def test_anomalies_flag_regions_far_from_their_usual_month(self) -> None:
        response = self.client.get("/v1/anomalies", params={"month": "2019-09"})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["available"])
        self.assertTrue(payload["data"])
        for row in payload["data"]:
            self.assertGreaterEqual(abs(row["z"]), payload["threshold"])
            self.assertGreaterEqual(row["years"], 8)
        scores = [abs(row["z"]) for row in payload["data"]]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_anomalies_honor_the_box_and_validate_the_month(self) -> None:
        box = {"month": "2019-09", "min_lat": -12, "max_lat": 29, "min_lon": 92, "max_lon": 143}
        rows = self.client.get("/v1/anomalies", params=box).json()["data"]
        self.assertTrue(all(-12 <= row["ry"] + .5 <= 29 and 92 <= row["rx"] + .5 <= 143 for row in rows))
        self.assertEqual(self.client.get("/v1/anomalies", params={"month": "2019-13"}).status_code, 422)

    def test_month_in_progress_has_no_anomalies_yet(self) -> None:
        payload = self.client.get("/v1/anomalies", params={"month": "2026-11"}).json()
        self.assertFalse(payload["available"])
        self.assertEqual(payload["data"], [])

    def test_repeat_requests_are_served_from_the_cache(self) -> None:
        params = {"month": "2018-08"}
        first = self.client.get("/v1/anomalies", params=params)
        second = self.client.get("/v1/anomalies", params=params)
        self.assertEqual(second.headers["x-cache"], "hit")
        self.assertEqual(first.json(), second.json())
        unchanged = self.client.get("/v1/anomalies", params=params, headers={"If-None-Match": first.headers["etag"]})
        self.assertEqual(unchanged.status_code, 304)
