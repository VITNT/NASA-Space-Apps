from datetime import date
import unittest

from fastapi.testclient import TestClient

from firms_pipeline.api import create_app


class CalendarTests(unittest.TestCase):
    def setUp(self):
        app = create_app()
        repo = app.state.repository
        # Use the real DuckDB query with a small relation and no private data.
        class PointsFixture(type(repo)):
            @property
            def points(self):
                return """(
                    SELECT *, CASE dataset WHEN 'modis' THEN 'terra'
                        WHEN 'viirs_snpp' THEN 'snpp' ELSE 'noaa20' END AS platform,
                        5.0 AS frp_mw
                    FROM (VALUES
                        (2025, 2, DATE '2025-02-01', 12.01, 104.01, 'modis', 'high', 0),
                        (2025, 2, DATE '2025-02-01', 12.02, 104.02, 'modis', 'nominal', 0),
                        (2025, 2, DATE '2025-02-01', 12.03, 104.04, 'viirs_snpp', 'nominal', NULL),
                        (2025, 2, DATE '2025-02-01', 13.00, 105.00, 'viirs_noaa20', 'high', 0),
                        (2025, 2, DATE '2025-02-01', 13.20, 106.00, 'modis', 'low', 0),
                        (2025, 2, DATE '2025-02-01', 12.20, 106.00, 'modis', 'high', 2),
                        (2025, 2, DATE '2025-02-02', 12.01, 104.01, 'modis', 'high', 0)
                    ) AS p(year, month, acq_date, latitude, longitude, dataset, confidence_class, hotspot_type)
                )"""

            # Terra and Suomi NPP observed all of February; NOAA-20 did not.
            @property
            def monthly(self):
                return """(
                    SELECT * FROM (VALUES
                        (DATE '2025-02-01', 'modis', 'terra', true, 28, 28),
                        (DATE '2025-02-01', 'viirs_snpp', 'snpp', true, 28, 28),
                        (DATE '2025-02-01', 'viirs_noaa20', 'noaa20', true, 20, 28)
                    ) AS m(month_start, dataset, platform, is_complete_month, active_days, calendar_days)
                )"""

            @property
            def factors(self):
                return """(
                    SELECT * FROM (VALUES
                        (12, 104, 2, 13, 5, 1.5),
                        (-999, -999, 2, 13, 5, 2.0)
                    ) AS f(ry, rx, month, target, source, factor)
                )"""

            def harmonization_target(self, dataset):
                return {None: 13, "modis": 1}.get(dataset)

        app.state.repository = PointsFixture(repo.project_root, repo.profile)
        self.client = TestClient(app)
        self.params = {
            "start_date": date(2025, 2, 1).isoformat(),
            "end_date": date(2025, 2, 28).isoformat(),
            "min_lat": 10, "max_lat": 15, "min_lon": 102, "max_lon": 108,
        }

    def test_deduplicates_within_and_across_sensors_per_day(self):
        response = self.client.get("/v1/calendar", params=self.params)
        self.assertEqual(response.status_code, 200)
        first, second = response.json()["data"]
        self.assertEqual(first, {
            "date": "2025-02-01", "active_cell_days": 2,
            "modis": 2, "viirs_snpp": 1, "viirs_noaa20": 1, "shared_cell_days": 1,
            "harmonized_cell_days": 1.5,
        })
        self.assertEqual(second["active_cell_days"], 1)

    def test_harmonized_counts_full_month_platforms_and_scales_by_region(self):
        # The NOAA-20 cell is dropped because NOAA-20 missed part of the month;
        # the remaining cell is scaled by its region's factor.
        response = self.client.get("/v1/calendar", params={**self.params, "interval": "month"})
        (month,) = response.json()["data"]
        self.assertEqual((month["active_cell_days"], month["harmonized_cell_days"]), (3, 3.0))

        # A lone platform that is its own target needs no scaling.
        response = self.client.get("/v1/calendar", params={**self.params, "interval": "month", "dataset": "modis"})
        self.assertEqual(response.json()["data"][0]["harmonized_cell_days"], 2.0)

    def test_harmonized_is_null_without_full_month_coverage_or_calibration(self):
        response = self.client.get("/v1/calendar", params={**self.params, "dataset": "viirs_noaa20"})
        self.assertIsNone(response.json()["data"][0]["harmonized_cell_days"])

    def test_monthly_totals_keep_cell_days_and_support_sensor_filter(self):
        response = self.client.get("/v1/calendar", params={**self.params, "interval": "month", "dataset": "modis"})
        self.assertEqual(response.status_code, 200)
        (month,) = response.json()["data"]
        self.assertEqual(month["active_cell_days"], 2)
        self.assertEqual(month["modis"], 3)
        self.assertEqual(month["shared_cell_days"], 0)

    def test_rejects_inverted_latitude_bounds(self):
        response = self.client.get("/v1/calendar", params={**self.params, "min_lat": 40})
        self.assertEqual(response.status_code, 422)

    def test_map_grid_combines_detections_and_honors_sparse_days(self):
        response = self.client.get("/v1/spatial/grid", params={
            **self.params, "days": ["2025-02-01", "2025-02-03"],
        })
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["cell_size"], 0.25)
        row = payload["data"][0]
        self.assertAlmostEqual(row.pop("mean_latitude"), 12.02)
        self.assertAlmostEqual(row.pop("mean_longitude"), 104.02333333333333)
        self.assertEqual(row, {
            "west": 104.0, "south": 12.0, "detection_count": 3, "observed_days": 1,
            "platforms": ["snpp", "terra"], "frp_sum_mw": 15.0,
        })

        response = self.client.get("/v1/spatial/grid", params={**self.params, "dataset": "modis"})
        self.assertEqual(response.status_code, 200)
        cell = response.json()["data"][0]
        self.assertEqual((cell["detection_count"], cell["observed_days"], cell["platforms"]), (3, 2, ["terra"]))
