"""Read-only HTTP API over the validated NASA FIRMS Parquet products."""

from __future__ import annotations

import json
import os
import shlex
from calendar import monthrange
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Annotated, Any, Iterator, Literal

import duckdb
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .pipeline import sql_string
from .sensitivity import (
    CELL_DAY_FILTER,
    CELL_X_SQL,
    CELL_Y_SQL,
    COMPLETE_PLATFORM_SQL,
    GLOBAL_REGION,
    PLATFORM_BIT_SQL,
)


Dataset = Literal["modis", "viirs_noaa20", "viirs_snpp"]
Platform = Literal["aqua", "terra", "noaa20", "snpp"]
ConfidenceClass = Literal["low", "nominal", "high"]
DayNight = Literal["D", "N"]
MAP_CELL_SIZES = tuple(2.0 ** exponent for exponent in range(-8, 2))
GLOBAL_HISTORY = Path(__file__).with_name("resources") / "global_history.json"
MAPS_KEY_ENV = "FIRMS_GOOGLE_MAPS_BROWSER_KEY"


class ResultPage(BaseModel):
    data: list[dict[str, Any]]
    count: int = Field(ge=0)
    truncated: bool = False


class MapGrid(BaseModel):
    data: list[dict[str, Any]]
    cell_size: float


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
        # Harmonization is optional: without validated factors the calendar
        # still serves observed cell-days.
        calibration_path = data_root / "catalog" / "calibration.json"
        self.factors_path = data_root / "merged" / "calibration" / "factors.parquet"
        self.calibration = None
        if self.profile == "merged" and calibration_path.exists() and self.factors_path.exists():
            try:
                calibration_data = json.loads(calibration_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                calibration_data = {}
            if (
                calibration_data.get("status") == "validated"
                and calibration_data.get("schema_version") == 2
                and calibration_data.get("source_run_id") == self.point_manifest["run_id"]
            ):
                self.calibration = calibration_data
        self.global_history = None
        if not self.calibration:
            try:
                snapshot = json.loads(GLOBAL_HISTORY.read_text(encoding="utf-8"))
                if snapshot["schema_version"] == 1 and snapshot["point_run_id"] == self.point_manifest["run_id"]:
                    self.global_history = snapshot["series"]
            except (OSError, ValueError, KeyError):
                pass

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
    def factors(self) -> str:
        return f"read_parquet({sql_string(self.factors_path)})"

    def harmonization_target(self, dataset: str | None) -> int | None:
        """The constellation that harmonized cell-days are expressed in."""
        if not self.calibration:
            return None
        return self.calibration["targets"].get(dataset or "all")

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


def _local_maps_key(path: Path) -> str:
    """Read the Git-ignored local key as data, without sourcing shell code."""
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            name, separator, value = line.strip().removeprefix("export ").partition("=")
            if separator and name.strip() == MAPS_KEY_ENV:
                values = shlex.split(value, comments=True)
                return values[0] if len(values) == 1 else ""
    except (OSError, ValueError):
        pass
    return ""


def create_app(project_root: Path | None = None) -> FastAPI:
    source_root = Path(__file__).resolve().parents[1]
    local_archive = source_root / ".local" / "archive"
    default_root = local_archive if local_archive.is_symlink() else source_root
    configured_root = project_root or Path(os.environ.get("FIRMS_PROJECT_ROOT", default_root))
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
    frontend_root = Path(__file__).resolve().parents[1] / "frontend"
    if frontend_root.exists():
        app.mount("/static", StaticFiles(directory=frontend_root), name="static")

        @app.get("/", include_in_schema=False)
        def frontend() -> FileResponse:
            return FileResponse(frontend_root / "index.html")

        # Only expose a browser key configured specifically for this frontend.
        google_maps_key = os.environ.get(MAPS_KEY_ENV, "").strip()
        local_maps_key = _local_maps_key(source_root / ".local" / "google-maps.env")

        @app.get("/map-config", include_in_schema=False)
        def map_config(request: Request) -> JSONResponse:
            loopback = {"127.0.0.1", "::1", "localhost"}
            is_local = request.url.hostname in loopback and request.client and request.client.host in loopback
            return JSONResponse(
                {"googleMapsKey": google_maps_key or (local_maps_key if is_local else None) or None},
                headers={"Cache-Control": "no-store"},
            )

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
            "area_label": repo.point_manifest.get("area_label"),
            "calibration_available": repo.calibration is not None,
            "global_history_available": repo.global_history is not None,
        }

    @app.get("/v1/calibration")
    def calibration_info(request: Request) -> dict[str, Any]:
        repo = _repository(request)
        if not repo.calibration:
            return {
                "available": False,
                "message": "No validated harmonization for this data. Run `python -m firms_pipeline calibrate`. "
                "Until then, months with different satellites are not comparable.",
            }
        return {"available": True, **repo.calibration}

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

    @app.get("/v1/coverage/monthly", response_model=ResultPage)
    def monthly_coverage(request: Request) -> ResultPage:
        repo = _repository(request)
        data = repo.rows(f"""
            SELECT month_start, dataset, platform, is_complete_month,
                active_days = calendar_days AS platform_complete
            FROM {repo.monthly}
            ORDER BY month_start, dataset, platform
        """)
        return ResultPage(data=data, count=len(data))

    @app.get("/v1/calendar", response_model=ResultPage)
    def burning_calendar(
        request: Request,
        start_date: date,
        end_date: date,
        min_lat: Annotated[float, Query(ge=-90, le=90)],
        max_lat: Annotated[float, Query(ge=-90, le=90)],
        min_lon: Annotated[float, Query(ge=-180, le=180)],
        max_lon: Annotated[float, Query(ge=-180, le=180)],
        dataset: Dataset | None = None,
        interval: Literal["day", "month"] = "day",
        month: Annotated[int | None, Query(ge=1, le=12)] = None,
    ) -> ResultPage:
        if max_lat < min_lat:
            raise HTTPException(status_code=422, detail="Invalid latitude bounds")
        repo = _repository(request)
        whole_months = start_date.day == 1 and end_date.day == monthrange(end_date.year, end_date.month)[1]
        if (
            repo.global_history is not None and interval == "month" and whole_months
            and (min_lat, max_lat, min_lon, max_lon) == (-90, 90, -180, 180)
        ):
            _date_span(start_date, end_date, 366 * 100)
            start, end = start_date.isoformat(), end_date.isoformat()
            data = [
                row for row in repo.global_history[dataset or "all"]
                if start <= row["date"] <= end and (month is None or int(row["date"][5:7]) == month)
            ]
            return ResultPage(data=data, count=len(data))
        _date_span(start_date, end_date, 366 if month is None else 366 * 6)
        longitude_sql, longitude_parameters = _bbox_clause(min_lon, max_lon)
        conditions = [
            "year BETWEEN ? AND ?", "acq_date BETWEEN ? AND ?",
            "latitude BETWEEN ? AND ?", longitude_sql,
            CELL_DAY_FILTER,
        ]
        parameters = [start_date.year, end_date.year, start_date, end_date, min_lat, max_lat, *longitude_parameters]
        if dataset is not None:
            conditions.append("dataset = ?")
            parameters.append(dataset)
        if month is not None:
            conditions.append("month = ?")
            parameters.append(month)
        elif start_date.year == end_date.year:
            conditions.append("month BETWEEN ? AND ?")
            parameters.extend([start_date.month, end_date.month])

        # UTC cell-days give each occupied 0.1° cell the same weight across
        # instruments and repeat passes. This is an activity index, not an event count.
        #
        # harmonized_cell_days restates each month in the cell-days the full
        # constellation would have reported. Only platforms that observed the
        # whole month count, and each 1° region is scaled by the factor measured
        # for those platforms. It is null when no platform covered the month.
        target = repo.harmonization_target(dataset)
        if target is None:
            data = repo.rows(f"""
                WITH cell_days AS (
                    SELECT acq_date, {CELL_Y_SQL} AS y, {CELL_X_SQL} AS x,
                        count_if(dataset = 'modis') AS modis,
                        count_if(dataset = 'viirs_snpp') AS viirs_snpp,
                        count_if(dataset = 'viirs_noaa20') AS viirs_noaa20
                    FROM {repo.points}
                    WHERE {' AND '.join(conditions)}
                    GROUP BY acq_date, y, x
                )
                SELECT date_trunc('{interval}', acq_date)::DATE AS date,
                    count(*) AS active_cell_days,
                    sum(modis)::BIGINT AS modis,
                    sum(viirs_snpp)::BIGINT AS viirs_snpp,
                    sum(viirs_noaa20)::BIGINT AS viirs_noaa20,
                    count_if(modis > 0 AND (viirs_snpp > 0 OR viirs_noaa20 > 0)) AS shared_cell_days,
                    NULL::DOUBLE AS harmonized_cell_days
                FROM cell_days GROUP BY 1 ORDER BY 1
            """, parameters)
            return ResultPage(data=data, count=len(data))
        else:
            factor = f"CASE WHEN r.source = {target} THEN 1 ELSE coalesce(f.factor, g.factor) END"
            harmonized = f"""CASE WHEN bool_or(r.source IS NULL OR (r.usable > 0 AND {factor} IS NULL)) THEN NULL
                    ELSE round(sum(r.usable * {factor})::DOUBLE, 1) END"""
            factor_joins = f"""
                LEFT JOIN {repo.factors} f ON f.ry = r.ry AND f.rx = r.rx AND f.month = month(r.acq_date)
                    AND f.target = {target} AND f.source = r.source
                LEFT JOIN {repo.factors} g ON g.ry = {GLOBAL_REGION} AND g.month = month(r.acq_date)
                    AND g.target = {target} AND g.source = r.source"""
        query = f"""
            WITH coverage AS (
                SELECT month_start, bit_or({PLATFORM_BIT_SQL})::INTEGER AS source
                FROM {repo.monthly}
                WHERE {COMPLETE_PLATFORM_SQL}{f" AND dataset = {sql_string(dataset)}" if dataset else ""}
                GROUP BY month_start
            ),
            cell_days AS (
                SELECT acq_date, {CELL_Y_SQL} AS y, {CELL_X_SQL} AS x,
                    count_if(dataset = 'modis') AS modis,
                    count_if(dataset = 'viirs_snpp') AS viirs_snpp,
                    count_if(dataset = 'viirs_noaa20') AS viirs_noaa20,
                    bit_or({PLATFORM_BIT_SQL})::INTEGER AS mask
                FROM {repo.points}
                WHERE {' AND '.join(conditions)}
                GROUP BY acq_date, y, x
            ),
            regional AS (
                SELECT c.acq_date, floor(c.y / 10)::INTEGER AS ry, floor(c.x / 10)::INTEGER AS rx, v.source,
                    count(*) AS cell_days,
                    count_if((c.mask & v.source) != 0) AS usable,
                    sum(c.modis) AS modis, sum(c.viirs_snpp) AS viirs_snpp, sum(c.viirs_noaa20) AS viirs_noaa20,
                    count_if(c.modis > 0 AND (c.viirs_snpp > 0 OR c.viirs_noaa20 > 0)) AS shared
                FROM cell_days c
                LEFT JOIN coverage v ON v.month_start = CAST(date_trunc('month', c.acq_date) AS DATE)
                GROUP BY ALL
            )
            SELECT CAST(date_trunc('{interval}', r.acq_date) AS DATE) AS date,
                sum(r.cell_days)::BIGINT AS active_cell_days,
                sum(r.modis)::BIGINT AS modis,
                sum(r.viirs_snpp)::BIGINT AS viirs_snpp,
                sum(r.viirs_noaa20)::BIGINT AS viirs_noaa20,
                sum(r.shared)::BIGINT AS shared_cell_days,
                {harmonized} AS harmonized_cell_days
            FROM regional r{factor_joins}
            GROUP BY 1 ORDER BY 1
        """
        data = repo.rows(query, parameters)
        return ResultPage(data=data, count=len(data))

    @app.get("/v1/spatial/grid", response_model=MapGrid)
    def map_grid(
        request: Request,
        start_date: date,
        end_date: date,
        min_lat: Annotated[float, Query(ge=-90, le=90)],
        max_lat: Annotated[float, Query(ge=-90, le=90)],
        min_lon: Annotated[float, Query(ge=-180, le=180)],
        max_lon: Annotated[float, Query(ge=-180, le=180)],
        dataset: Dataset | None = None,
        days: Annotated[list[date] | None, Query(max_length=31)] = None,
        cell_size: float | None = None,
        viewport: Annotated[list[float] | None, Query(min_length=4, max_length=4)] = None,
    ) -> MapGrid:
        _date_span(start_date, end_date, 31)
        if max_lat < min_lat:
            raise HTTPException(status_code=422, detail="Invalid latitude bounds")
        if days and any(day < start_date or day > end_date for day in days):
            raise HTTPException(status_code=422, detail="Selected days must be within the date range")
        south, north, west, east = viewport or [min_lat, max_lat, min_lon, max_lon]
        if not (-90 <= south <= north <= 90 and -180 <= west <= 180 and -180 <= east <= 180):
            raise HTTPException(status_code=422, detail="Invalid map viewport bounds")
        latitude_span = north - south
        longitude_span = east - west
        if longitude_span < 0:
            longitude_span += 360
        if cell_size is None:
            span = max(latitude_span, longitude_span)
            cell_size = next(size for threshold, size in ((12, .25), (60, .5), (100, 1), (360, 2)) if span <= threshold)
        elif cell_size not in MAP_CELL_SIZES:
            raise HTTPException(status_code=422, detail="Unsupported map cell size")
        if (latitude_span / cell_size + 1) * (longitude_span / cell_size + 1) > 70_000:
            raise HTTPException(status_code=422, detail="Map viewport is too large for this cell size")
        repo = _repository(request)
        longitude_sql, longitude_parameters = _bbox_clause(min_lon, max_lon)
        conditions = [
            "year BETWEEN ? AND ?", "acq_date BETWEEN ? AND ?",
            "latitude BETWEEN ? AND ?", longitude_sql,
        ]
        parameters: list[Any] = [start_date.year, end_date.year, start_date, end_date, min_lat, max_lat, *longitude_parameters]
        if start_date.year == end_date.year:
            conditions.append("month BETWEEN ? AND ?")
            parameters.extend([start_date.month, end_date.month])
        if dataset is not None:
            conditions.append("dataset = ?")
            parameters.append(dataset)
        if days:
            conditions.append("acq_date IN (SELECT unnest(?::DATE[]))")
            parameters.append(days)
        if viewport:
            if any(bound % cell_size for bound in viewport):
                raise HTTPException(status_code=422, detail="Map viewport must align to grid cells")
            # Viewport edges fall between cells. Keep the lower edge inclusive
            # so panning never changes the observations counted in a given cell.
            longitude = "(CASE WHEN longitude = 180 THEN -180 ELSE longitude END)"
            join = "AND" if west <= east else "OR"
            conditions.extend([
                "latitude >= ?",
                "latitude <= ?" if north == 90 else "latitude < ?",
                f"({longitude} >= ? {join} {longitude} < ?)",
            ])
            parameters.extend([south, north, west, east])

        data = repo.rows(f"""
            WITH observations AS (
                SELECT floor(CASE WHEN longitude = 180 THEN -180 ELSE longitude END / {cell_size}) AS x,
                    least({90 / cell_size - 1}, floor(latitude / {cell_size})) AS y,
                    latitude, CASE WHEN longitude = 180 THEN -180 ELSE longitude END AS point_longitude,
                    acq_date, platform, frp_mw
                FROM {repo.points}
                WHERE {' AND '.join(conditions)}
            )
            SELECT x * {cell_size} AS west, y * {cell_size} AS south,
                avg(latitude) AS mean_latitude, avg(point_longitude) AS mean_longitude,
                count(*) AS detection_count, count(DISTINCT acq_date) AS observed_days,
                list(DISTINCT platform ORDER BY platform) AS platforms, sum(frp_mw)::DOUBLE AS frp_sum_mw
            FROM observations
            GROUP BY x, y ORDER BY y, x
        """, parameters)
        return MapGrid(data=data, cell_size=cell_size)

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
