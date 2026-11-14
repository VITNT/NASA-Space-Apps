"""Read-only HTTP API over the validated NASA FIRMS Parquet products."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Annotated, Any, Iterator, Literal

import duckdb
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .pipeline import sql_string


Dataset = Literal["modis", "viirs_noaa20", "viirs_snpp"]
Platform = Literal["aqua", "terra", "noaa20", "snpp"]
ConfidenceClass = Literal["low", "nominal", "high"]
DayNight = Literal["D", "N"]


class ResultPage(BaseModel):
    data: list[dict[str, Any]]
    count: int = Field(ge=0)
    truncated: bool = False


class DataRepository:
    def __init__(self, project_root: Path, profile: str | None = None):
        self.project_root = project_root.resolve()
        data_root = self.project_root / "data"
        requested_profile = profile or os.environ.get("FIRMS_DATA_PROFILE", "merged")
        if requested_profile not in {"merged", "live"}:
            raise RuntimeError("FIRMS_DATA_PROFILE must be 'merged' or 'live'.")
        merged_required = (
            data_root / "catalog" / "merged_manifest.json",
            data_root / "catalog" / "merged_aggregates.json",
            data_root / "catalog" / "merged_spatial.json",
        )
        # A fresh installation can still start before the optional history merge.
        self.profile = "live" if requested_profile == "merged" and not all(p.exists() for p in merged_required) else requested_profile
        if self.profile == "merged":
            self.points_glob = str(data_root / "merged" / "hotspots" / "**" / "*.parquet")
            aggregate_root = data_root / "merged" / "aggregates"
            self.spatial_glob = str(data_root / "merged" / "spatial" / "h3" / "**" / "*.parquet")
            self.point_manifest_path = data_root / "catalog" / "merged_manifest.json"
            self.aggregate_manifest_path = data_root / "catalog" / "merged_aggregates.json"
            self.spatial_manifest_path = data_root / "catalog" / "merged_spatial.json"
        else:
            self.points_glob = str(data_root / "processed" / "hotspots" / "**" / "*.parquet")
            aggregate_root = data_root / "aggregates"
            self.spatial_glob = str(data_root / "spatial" / "h3" / "**" / "*.parquet")
            self.point_manifest_path = data_root / "catalog" / "manifest.json"
            self.aggregate_manifest_path = data_root / "catalog" / "aggregates.json"
            self.spatial_manifest_path = data_root / "catalog" / "spatial.json"
        self.daily_path = aggregate_root / "daily_global.parquet"
        self.monthly_path = aggregate_root / "monthly_global.parquet"
        required = (
            self.daily_path,
            self.monthly_path,
            self.point_manifest_path,
            self.aggregate_manifest_path,
            self.spatial_manifest_path,
        )
        missing = [str(path) for path in required if not path.exists()]
        if missing:
            raise RuntimeError(f"Required validated data products are missing: {missing}")
        self.point_manifest = json.loads(self.point_manifest_path.read_text(encoding="utf-8"))
        self.aggregate_manifest = json.loads(self.aggregate_manifest_path.read_text(encoding="utf-8"))
        self.spatial_manifest = json.loads(self.spatial_manifest_path.read_text(encoding="utf-8"))

    @contextmanager
    def connection(self) -> Iterator[duckdb.DuckDBPyConnection]:
        connection = duckdb.connect()
        connection.execute("SET threads = 2")
        try:
            yield connection
        finally:
            connection.close()

    def rows(self, query: str, parameters: list[Any] | None = None) -> list[dict[str, Any]]:
        with self.connection() as connection:
            cursor = connection.execute(query, parameters or [])
            names = [column[0] for column in cursor.description]
            return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]

    @property
    def points(self) -> str:
        return f"read_parquet({sql_string(self.points_glob)}, hive_partitioning=true)"

    @property
    def daily(self) -> str:
        return f"read_parquet({sql_string(self.daily_path)})"

    @property
    def monthly(self) -> str:
        return f"read_parquet({sql_string(self.monthly_path)})"

    @property
    def spatial(self) -> str:
        return f"read_parquet({sql_string(self.spatial_glob)}, hive_partitioning=true)"


def _date_span(start_date: date, end_date: date, maximum_days: int) -> None:
    if end_date < start_date:
        raise HTTPException(status_code=422, detail="end_date must be on or after start_date")
    if (end_date - start_date).days + 1 > maximum_days:
        raise HTTPException(status_code=422, detail=f"Date range may not exceed {maximum_days} days")


def _bbox_clause(min_lon: float, max_lon: float) -> tuple[str, list[float]]:
    if min_lon <= max_lon:
        return "longitude BETWEEN ? AND ?", [min_lon, max_lon]
    return "(longitude >= ? OR longitude <= ?)", [min_lon, max_lon]


def _repository(request: Request) -> DataRepository:
    return request.app.state.repository


def create_app(project_root: Path | None = None) -> FastAPI:
    configured_root = project_root or Path(os.environ.get("FIRMS_PROJECT_ROOT", Path(__file__).resolve().parents[1]))
    repository = DataRepository(configured_root)
    app = FastAPI(
        title="NASA FIRMS Fire Activity API",
        version="0.1.0",
        description=(
            "Read-only access to validated NASA FIRMS observations and aggregates. "
            "MODIS and VIIRS remain separately identified."
        ),
    )
    app.state.repository = repository
    frontend_root = repository.project_root / "frontend"
    if frontend_root.exists():
        app.mount("/static", StaticFiles(directory=frontend_root), name="static")

        @app.get("/", include_in_schema=False)
        def frontend() -> FileResponse:
            return FileResponse(frontend_root / "index.html")

    @app.get("/health")
    def health(request: Request) -> dict[str, Any]:
        repo = _repository(request)
        return {
            "status": "ok",
            "data_profile": repo.profile,
            "point_run_id": repo.point_manifest["run_id"],
            "aggregate_run_id": repo.aggregate_manifest["run_id"],
            "spatial_run_id": repo.spatial_manifest["run_id"],
            "source_rows": repo.aggregate_manifest["source_rows"],
        }

    @app.get("/v1/coverage", response_model=ResultPage)
    def coverage(request: Request) -> ResultPage:
        repo = _repository(request)
        query = f"""
            SELECT
                dataset,
                platform,
                min(first_observed_date) AS first_date,
                max(last_observed_date) AS last_date,
                sum(detection_count)::BIGINT AS detection_count,
                count_if(is_complete_month)::INTEGER AS complete_months,
                count_if(NOT is_complete_month)::INTEGER AS partial_months
            FROM {repo.monthly}
            GROUP BY dataset, platform
            ORDER BY dataset, platform
        """
        data = repo.rows(query)
        return ResultPage(data=data, count=len(data))

    @app.get("/v1/activity/monthly", response_model=ResultPage)
    def monthly_activity(
        request: Request,
        dataset: Dataset | None = None,
        platform: Platform | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> ResultPage:
        repo = _repository(request)
        conditions: list[str] = []
        parameters: list[Any] = []
        if dataset:
            conditions.append("dataset = ?")
            parameters.append(dataset)
        if platform:
            conditions.append("platform = ?")
            parameters.append(platform)
        if start_date:
            conditions.append("last_observed_date >= ?")
            parameters.append(start_date)
        if end_date:
            conditions.append("first_observed_date <= ?")
            parameters.append(end_date)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        query = f"SELECT * FROM {repo.monthly}{where} ORDER BY dataset, platform, year, month LIMIT ?"
        data = repo.rows(query, [*parameters, limit + 1])
        truncated = len(data) > limit
        data = data[:limit]
        return ResultPage(data=data, count=len(data), truncated=truncated)

    @app.get("/v1/activity/daily", response_model=ResultPage)
    def daily_activity(
        request: Request,
        dataset: Dataset | None = None,
        platform: Platform | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        limit: Annotated[int, Query(ge=1, le=2000)] = 400,
    ) -> ResultPage:
        repo = _repository(request)
        conditions: list[str] = []
        parameters: list[Any] = []
        if dataset:
            conditions.append("dataset = ?")
            parameters.append(dataset)
        if platform:
            conditions.append("platform = ?")
            parameters.append(platform)
        if start_date:
            conditions.append("activity_date >= ?")
            parameters.append(start_date)
        if end_date:
            conditions.append("activity_date <= ?")
            parameters.append(end_date)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        query = f"SELECT * FROM {repo.daily}{where} ORDER BY activity_date, dataset, platform, source_stage LIMIT ?"
        data = repo.rows(query, [*parameters, limit + 1])
        truncated = len(data) > limit
        data = data[:limit]
        return ResultPage(data=data, count=len(data), truncated=truncated)

    @app.get("/v1/hotspots", response_model=ResultPage)
    def hotspots(
        request: Request,
        dataset: Dataset,
        start_date: date,
        end_date: date,
        min_lat: Annotated[float, Query(ge=-90, le=90)],
        max_lat: Annotated[float, Query(ge=-90, le=90)],
        min_lon: Annotated[float, Query(ge=-180, le=180)],
        max_lon: Annotated[float, Query(ge=-180, le=180)],
        platform: Platform | None = None,
        confidence_class: ConfidenceClass | None = None,
        min_frp_mw: Annotated[float | None, Query(ge=0)] = None,
        daynight: DayNight | None = None,
        limit: Annotated[int, Query(ge=1, le=5000)] = 1000,
    ) -> ResultPage:
        _date_span(start_date, end_date, 31)
        if max_lat < min_lat:
            raise HTTPException(status_code=422, detail="max_lat must be greater than or equal to min_lat")
        repo = _repository(request)
        longitude_sql, longitude_parameters = _bbox_clause(min_lon, max_lon)
        conditions = [
            "dataset = ?",
            "acq_date BETWEEN ? AND ?",
            "latitude BETWEEN ? AND ?",
            longitude_sql,
        ]
        parameters: list[Any] = [
            dataset,
            start_date,
            end_date,
            min_lat,
            max_lat,
            *longitude_parameters,
        ]
        if platform:
            conditions.append("platform = ?")
            parameters.append(platform)
        if confidence_class:
            conditions.append("confidence_class = ?")
            parameters.append(confidence_class)
        if min_frp_mw is not None:
            conditions.append("frp_mw >= ?")
            parameters.append(min_frp_mw)
        if daynight:
            conditions.append("daynight = ?")
            parameters.append(daynight)
        query = f"""
            SELECT
                observation_time_utc, latitude, longitude, dataset, platform,
                frp_mw, confidence_class, confidence_raw, daynight,
                hotspot_type, source_stage
            FROM {repo.points}
            WHERE {' AND '.join(conditions)}
            ORDER BY observation_time_utc, latitude, longitude, source_file
            LIMIT ?
        """
        data = repo.rows(query, [*parameters, limit + 1])
        truncated = len(data) > limit
        data = data[:limit]
        return ResultPage(data=data, count=len(data), truncated=truncated)

    @app.get("/v1/summary/bbox", response_model=ResultPage)
    def bbox_summary(
        request: Request,
        start_date: date,
        end_date: date,
        min_lat: Annotated[float, Query(ge=-90, le=90)],
        max_lat: Annotated[float, Query(ge=-90, le=90)],
        min_lon: Annotated[float, Query(ge=-180, le=180)],
        max_lon: Annotated[float, Query(ge=-180, le=180)],
        dataset: Dataset | None = None,
        confidence_class: ConfidenceClass | None = None,
        min_frp_mw: Annotated[float | None, Query(ge=0)] = None,
        daynight: DayNight | None = None,
    ) -> ResultPage:
        _date_span(start_date, end_date, 366)
        if max_lat < min_lat:
            raise HTTPException(status_code=422, detail="max_lat must be greater than or equal to min_lat")
        repo = _repository(request)
        longitude_sql, longitude_parameters = _bbox_clause(min_lon, max_lon)
        conditions = [
            "acq_date BETWEEN ? AND ?",
            "latitude BETWEEN ? AND ?",
            longitude_sql,
        ]
        parameters: list[Any] = [start_date, end_date, min_lat, max_lat, *longitude_parameters]
        if dataset:
            conditions.append("dataset = ?")
            parameters.append(dataset)
        if confidence_class:
            conditions.append("confidence_class = ?")
            parameters.append(confidence_class)
        if min_frp_mw is not None:
            conditions.append("frp_mw >= ?")
            parameters.append(min_frp_mw)
        if daynight:
            conditions.append("daynight = ?")
            parameters.append(daynight)
        query = f"""
            SELECT
                dataset,
                platform,
                count(*)::BIGINT AS detection_count,
                count(DISTINCT acq_date)::INTEGER AS active_days,
                sum(frp_mw)::DOUBLE AS frp_sum_mw,
                avg(frp_mw)::DOUBLE AS frp_mean_mw,
                max(frp_mw)::DOUBLE AS frp_max_mw,
                count_if(confidence_class = 'high')::BIGINT AS high_confidence_count,
                count_if(daynight = 'D')::BIGINT AS day_count,
                count_if(daynight = 'N')::BIGINT AS night_count
            FROM {repo.points}
            WHERE {' AND '.join(conditions)}
            GROUP BY dataset, platform
            ORDER BY dataset, platform
        """
        data = repo.rows(query, parameters)
        return ResultPage(data=data, count=len(data))

    @app.get("/v1/spatial/monthly", response_model=ResultPage)
    def spatial_monthly(
        request: Request,
        resolution: Annotated[int, Query(ge=4, le=5)],
        dataset: Dataset,
        year: Annotated[int, Query(ge=2000, le=2100)],
        month: Annotated[int, Query(ge=1, le=12)],
        platform: Platform | None = None,
        min_detection_count: Annotated[int, Query(ge=1)] = 1,
        min_lat: Annotated[float | None, Query(ge=-90, le=90)] = None,
        max_lat: Annotated[float | None, Query(ge=-90, le=90)] = None,
        min_lon: Annotated[float | None, Query(ge=-180, le=180)] = None,
        max_lon: Annotated[float | None, Query(ge=-180, le=180)] = None,
        limit: Annotated[int, Query(ge=1, le=50000)] = 10000,
    ) -> ResultPage:
        supplied_bbox = [value is not None for value in (min_lat, max_lat, min_lon, max_lon)]
        if any(supplied_bbox) and not all(supplied_bbox):
            raise HTTPException(status_code=422, detail="Supply all four bounding-box values or none")
        if min_lat is not None and max_lat is not None and max_lat < min_lat:
            raise HTTPException(status_code=422, detail="max_lat must be greater than or equal to min_lat")

        repo = _repository(request)
        conditions = [
            "resolution = ?",
            "dataset = ?",
            "year = ?",
            "month = ?",
            "detection_count >= ?",
        ]
        parameters: list[Any] = [resolution, dataset, year, month, min_detection_count]
        if platform:
            conditions.append("platform = ?")
            parameters.append(platform)
        if all(supplied_bbox):
            conditions.append("center_latitude BETWEEN ? AND ?")
            parameters.extend([min_lat, max_lat])
            longitude_sql, longitude_parameters = _bbox_clause(float(min_lon), float(max_lon))
            conditions.append(longitude_sql.replace("longitude", "center_longitude"))
            parameters.extend(longitude_parameters)
        query = f"""
            SELECT
                h3_cell,
                resolution,
                center_latitude,
                center_longitude,
                cell_area_km2,
                dataset,
                platform,
                year,
                month,
                detection_count,
                detections_per_km2,
                active_days,
                frp_sum_mw,
                frp_mw_per_km2,
                frp_mean_mw,
                frp_max_mw,
                low_confidence_count,
                nominal_confidence_count,
                high_confidence_count,
                day_count,
                night_count,
                archive_count,
                nrt_count
            FROM {repo.spatial}
            WHERE {' AND '.join(conditions)}
            ORDER BY detection_count DESC, h3_cell
            LIMIT ?
        """
        data = repo.rows(query, [*parameters, limit + 1])
        truncated = len(data) > limit
        data = data[:limit]
        return ResultPage(data=data, count=len(data), truncated=truncated)

    return app


app = create_app()
