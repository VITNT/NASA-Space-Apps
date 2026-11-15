"""Read-only HTTP API over the validated NASA FIRMS Parquet products."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import shlex
import tempfile
import threading
from calendar import monthrange
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Annotated, Any, Iterator, Literal

import duckdb
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import areas
from .cache import ResponseCache
from .pipeline import sql_string
from .sensitivity import (
    CELL_DAY_FILTER,
    CELL_X_SQL,
    CELL_Y_SQL,
    COMPLETE_PLATFORM_SQL,
    GLOBAL_REGION,
    PLATFORM_BIT_SQL,
)


# DuckDB settings for every request. On a small VM set FIRMS_DUCKDB_MEMORY
# (for example 600MB) so a large query spills to disk instead of failing.
DUCKDB_CONFIG = {"threads": os.environ.get("FIRMS_DUCKDB_THREADS", "2")}
if os.environ.get("FIRMS_DUCKDB_MEMORY"):
    DUCKDB_CONFIG["memory_limit"] = os.environ["FIRMS_DUCKDB_MEMORY"]
if os.environ.get("FIRMS_DUCKDB_TEMP"):
    DUCKDB_CONFIG["temp_directory"] = os.environ["FIRMS_DUCKDB_TEMP"]
# A region-month is flagged when its robust z reaches ANOMALY_Z: about one
# month in a few hundred by chance for a well-behaved region.
ANOMALY_Z = 3.0
ANOMALY_MAD_FLOOR = 0.15   # in log units: no region is treated as steadier than ±15%
ANOMALY_MIN_YEARS = 8
ANOMALY_MIN_CELL_DAYS = 5
QUERY_SLOTS = threading.BoundedSemaphore(max(1, int(os.environ.get("FIRMS_QUERY_SLOTS", "2"))))

Dataset = Literal["modis", "viirs_noaa20", "viirs_snpp"]
Platform = Literal["aqua", "terra", "noaa20", "snpp"]
ConfidenceClass = Literal["low", "nominal", "high"]
DayNight = Literal["D", "N"]
MAP_CELL_SIZES = tuple(2.0 ** exponent for exponent in range(-8, 2))
MAP_POINT_LIMIT = 50_000
MAP_POINT_COLUMNS = (
    "latitude", "longitude", "observed_utc", "dataset", "platform", "frp_mw",
    "brightness_primary_k", "brightness_secondary_k", "scan_km", "track_km",
    "daynight", "confidence_raw", "confidence_class", "hotspot_type",
    "product_version", "source_stage",
)
# The hotspot export is complete or refused, never cut short.
EXPORT_LIMIT = 500_000
# The longest period one hotspot export may cover: the whole archive.
EXPORT_MAX_DAYS = 366 * 40
# One row per detection, labelled for people as well as programs. Brightness
# channels are named by spectral role because MODIS and VIIRS use different bands.
EXPORT_FIELDS = (
    ("acq_date", "strftime(acq_date, '%Y-%m-%d')"),
    ("acq_time_utc", "strftime(observation_time_utc, '%H:%M')"),
    ("latitude", "latitude"),
    ("longitude", "longitude"),
    ("satellite", "CASE platform WHEN 'terra' THEN 'Terra' WHEN 'aqua' THEN 'Aqua' "
                  "WHEN 'snpp' THEN 'Suomi NPP' WHEN 'noaa20' THEN 'NOAA-20' ELSE platform END"),
    ("instrument", "upper(instrument_family)"),
    ("dataset", "dataset"),
    ("nominal_pixel_m", "CASE WHEN dataset = 'modis' THEN 1000 ELSE 375 END"),
    ("scan_km", "scan_km"),
    ("track_km", "track_km"),
    ("day_night", "CASE daynight WHEN 'D' THEN 'day' WHEN 'N' THEN 'night' END"),
    ("confidence_class", "confidence_class"),
    ("confidence_raw", "confidence_raw"),
    ("frp_mw", "frp_mw"),
    ("brightness_mir_k", "brightness_primary_k"),
    ("brightness_tir_k", "brightness_secondary_k"),
    ("hotspot_type", "hotspot_type"),
    ("hotspot_type_label", "CASE hotspot_type WHEN 0 THEN 'presumed vegetation fire' WHEN 1 THEN 'active volcano' "
                           "WHEN 2 THEN 'other static land source' WHEN 3 THEN 'offshore' ELSE 'not yet classified' END"),
    ("counts_toward_calendar", f"CASE WHEN {CELL_DAY_FILTER} THEN 'yes' ELSE 'no' END"),
    ("calendar_cell_lat", f"round({CELL_Y_SQL} / 10, 1)"),
    ("calendar_cell_lon", f"round(({CELL_X_SQL}) / 10, 1)"),
    ("product_version", "product_version"),
    ("source_stage", "source_stage"),
)
EXPORT_COLUMNS = tuple(name for name, _ in EXPORT_FIELDS)
EXPORT_SQL = ", ".join(f"{sql} AS {name}" for name, sql in EXPORT_FIELDS)
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
        # Cell-days per 1° region, month and platform mask, written by calibrate.
        # With the factors it gives every region-month's adjusted activity.
        self.masks_path = data_root / "merged" / "calibration" / "region_month_constellations.parquet"
        self.global_history = None
        if not self.calibration:
            try:
                snapshot = json.loads(GLOBAL_HISTORY.read_text(encoding="utf-8"))
                if snapshot["schema_version"] == 1 and snapshot["point_run_id"] == self.point_manifest["run_id"]:
                    self.global_history = snapshot["series"]
            except (OSError, ValueError, KeyError):
                pass

    @contextmanager
    def connection(self, tables: dict[str, Any] | None = None) -> Iterator[duckdb.DuckDBPyConnection]:
        # A small host runs out of memory long before it runs out of requests:
        # only QUERY_SLOTS queries run at once, the rest wait their turn.
        with QUERY_SLOTS:
            connection = duckdb.connect(config=DUCKDB_CONFIG)
            for name, table in (tables or {}).items():
                connection.register(name, table)
            try:
                yield connection
            finally:
                connection.close()

    def rows(
        self, query: str, parameters: list[Any] | None = None, tables: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        with self.connection(tables) as connection:
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

    @property
    def anomalies_available(self) -> bool:
        return self.calibration is not None and self.masks_path.exists()

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


def _area_shape(
    circle: Annotated[str | None, Query(max_length=80, description="lat,lon,radius_km")] = None,
    polygon: Annotated[str | None, Query(max_length=4000, description="lat,lon pairs of a drawn outline")] = None,
    country: Annotated[str | None, Query(max_length=80, description="A name from /v1/areas/countries")] = None,
) -> areas.Shape:
    """An optional shape inside the bounding box: a circle, a drawn polygon or a country."""
    return areas.shape(circle, polygon, country)


AreaShape = Annotated[areas.Shape, Depends(_area_shape)]


def _map_conditions(
    start_date: date, end_date: date,
    min_lat: float, max_lat: float, min_lon: float, max_lon: float,
    dataset: str | None, days: list[date] | None, viewport: list[float] | None,
    platform: str | None = None,
) -> tuple[list[str], list[Any]]:
    """The detections a map request covers: area, dates, sensor, satellite and viewport."""
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
    if platform is not None:
        conditions.append("platform = ?")
        parameters.append(platform)
    if days:
        conditions.append("acq_date IN (SELECT unnest(?::DATE[]))")
        parameters.append(days)
    if viewport:
        south, north, west, east = viewport
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
    return conditions, parameters


class RevalidatedStaticFiles(StaticFiles):
    """Frontend files that browsers recheck on every load.

    The scripts import each other by plain path, so a browser that kept one
    stale module beside fresh ones would fail to start the page.
    """

    def file_response(self, *args: Any, **kwargs: Any):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


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
    app.add_middleware(GZipMiddleware, minimum_size=4096)
    # Responses depend only on the URL, the published data and this code.
    if os.environ.get("FIRMS_CACHE", "on").lower() not in {"0", "off", "false", "no"}:
        code = hashlib.sha256(b"".join(
            path.read_bytes() for path in sorted(Path(__file__).parent.glob("*.py"))
        )).hexdigest()[:16]
        version = "|".join(str(value) for value in (
            code, repository.profile, repository.point_manifest["run_id"], repository.aggregate_manifest["run_id"],
            (repository.calibration or {}).get("run_id"),
        ))
        cache_dir = os.environ.get("FIRMS_CACHE_DIR")
        app.add_middleware(
            ResponseCache, version=version,
            # Never inside the data folder: the dataset stays exactly as published.
            directory=Path(cache_dir) if cache_dir else Path(tempfile.gettempdir()) / "luminite-cache",
            memory_bytes=int(os.environ.get("FIRMS_CACHE_MEMORY_MB", "48")) * 1024 * 1024,
            disk_bytes=int(os.environ.get("FIRMS_CACHE_DISK_MB", "512")) * 1024 * 1024,
        )
    app.state.repository = repository
    frontend_root = Path(__file__).resolve().parents[1] / "frontend"
    if frontend_root.exists():
        app.mount("/static", RevalidatedStaticFiles(directory=frontend_root), name="static")

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
            "anomalies_available": repo.anomalies_available,
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

    @app.get("/v1/areas/countries", response_model=ResultPage)
    def country_areas() -> ResultPage:
        """Countries that can be used as an area, with the bounding box to query them with."""
        data = areas.countries()
        return ResultPage(data=data, count=len(data))

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
                active_days = calendar_days AS platform_complete,
                active_days, calendar_days
            FROM {repo.monthly}
            ORDER BY month_start, dataset, platform
        """)
        return ResultPage(data=data, count=len(data))

    @app.get("/v1/coverage/daily", response_model=ResultPage)
    def daily_coverage(request: Request, start_date: date, end_date: date) -> ResultPage:
        """The UTC days on which each platform reported anywhere on Earth.

        A working satellite detects fires somewhere every day, so a platform
        missing from a day had no data that day, not merely no fire.
        """
        _date_span(start_date, end_date, 366)
        repo = _repository(request)
        data = repo.rows(f"""
            SELECT DISTINCT activity_date AS date, dataset, platform
            FROM {repo.daily}
            WHERE activity_date BETWEEN ? AND ?
            ORDER BY date, dataset, platform
        """, [start_date, end_date])
        return ResultPage(data=data, count=len(data))

    def export_query(
        repo: DataRepository, start_date: date, end_date: date,
        min_lat: float, max_lat: float, min_lon: float, max_lon: float,
        dataset: str | None, days: list[date] | None, platform: str | None = None,
        shape: areas.Shape = areas.Shape(),
    ) -> tuple[str, list[Any]]:
        _date_span(start_date, end_date, EXPORT_MAX_DAYS)
        if max_lat < min_lat:
            raise HTTPException(status_code=422, detail="Invalid latitude bounds")
        if days and any(day < start_date or day > end_date for day in days):
            raise HTTPException(status_code=422, detail="Selected days must be within the date range")
        conditions, parameters = _map_conditions(
            start_date, end_date, min_lat, max_lat, min_lon, max_lon, dataset, days, None, platform)
        conditions.extend(shape.conditions)
        return f"FROM {repo.points} WHERE {' AND '.join(conditions)}", parameters

    @app.get("/v1/export/hotspots/count")
    def export_hotspot_count(
        request: Request,
        start_date: date,
        end_date: date,
        min_lat: Annotated[float, Query(ge=-90, le=90)],
        max_lat: Annotated[float, Query(ge=-90, le=90)],
        min_lon: Annotated[float, Query(ge=-180, le=180)],
        max_lon: Annotated[float, Query(ge=-180, le=180)],
        dataset: Dataset | None = None,
        platform: Platform | None = None,
        days: Annotated[list[date] | None, Query(max_length=31)] = None,
        shape: AreaShape = areas.Shape(),
    ) -> dict[str, Any]:
        repo = _repository(request)
        source, parameters = export_query(
            repo, start_date, end_date, min_lat, max_lat, min_lon, max_lon, dataset, days, platform, shape)
        count, qualifying = repo.rows(
            f"SELECT count(*) AS n, count_if({CELL_DAY_FILTER}) AS q {source}", parameters, shape.tables)[0].values()
        return {"count": count, "qualifying": qualifying, "limit": EXPORT_LIMIT}

    @app.get("/v1/export/hotspots.csv")
    def export_hotspots(
        request: Request,
        start_date: date,
        end_date: date,
        min_lat: Annotated[float, Query(ge=-90, le=90)],
        max_lat: Annotated[float, Query(ge=-90, le=90)],
        min_lon: Annotated[float, Query(ge=-180, le=180)],
        max_lon: Annotated[float, Query(ge=-180, le=180)],
        dataset: Dataset | None = None,
        platform: Platform | None = None,
        days: Annotated[list[date] | None, Query(max_length=31)] = None,
        shape: AreaShape = areas.Shape(),
    ) -> StreamingResponse:
        """Every detection in the search, one row each, streamed as CSV.

        Unlike /v1/hotspots this export is never silently truncated: a search
        larger than EXPORT_LIMIT is refused so the file is always complete.
        """
        repo = _repository(request)
        source, parameters = export_query(
            repo, start_date, end_date, min_lat, max_lat, min_lon, max_lon, dataset, days, platform, shape)
        count = repo.rows(f"SELECT count(*) AS n {source}", parameters, shape.tables)[0]["n"]
        if count > EXPORT_LIMIT:
            raise HTTPException(
                status_code=413,
                detail=f"{count:,} detections exceed the {EXPORT_LIMIT:,}-row export limit. "
                "Narrow the area or choose a shorter period.",
            )
        query = f"SELECT {EXPORT_SQL} {source} ORDER BY observation_time_utc, dataset, platform, latitude, longitude"

        def rows() -> Iterator[str]:
            buffer = io.StringIO()
            writer = csv.writer(buffer, lineterminator="\r\n")
            writer.writerow(EXPORT_COLUMNS)
            with repo.connection(shape.tables) as connection:
                cursor = connection.execute(query, parameters)
                while batch := cursor.fetchmany(20_000):
                    writer.writerows(batch)
                    yield buffer.getvalue()
                    buffer.seek(0)
                    buffer.truncate()
            yield buffer.getvalue()

        name = f"luminite-hotspots-{start_date.isoformat()}_{end_date.isoformat()}.csv"
        return StreamingResponse(rows(), media_type="text/csv; charset=utf-8", headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "X-Row-Count": str(count),
        })

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
        through_day: Annotated[int | None, Query(ge=1, le=31, description="Count only days 1 to N of each month")] = None,
        platforms: Annotated[list[Platform] | None, Query(description="Count only these satellites")] = None,
        shape: AreaShape = areas.Shape(),
    ) -> ResultPage:
        if max_lat < min_lat:
            raise HTTPException(status_code=422, detail="Invalid latitude bounds")
        repo = _repository(request)
        whole_months = start_date.day == 1 and end_date.day == monthrange(end_date.year, end_date.month)[1]
        if (
            repo.global_history is not None and interval == "month" and whole_months
            and not shape and through_day is None and not platforms
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
        if through_day is not None:
            conditions.append("day(acq_date) <= ?")
            parameters.append(through_day)
        if platforms:
            conditions.append("platform IN (SELECT unnest(?::VARCHAR[]))")
            parameters.append(platforms)
        conditions.extend(shape.conditions)
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
                        count_if(dataset = 'viirs_noaa20') AS viirs_noaa20,
                        sum(frp_mw) FILTER (dataset = 'modis') AS frp_modis,
                        sum(frp_mw) FILTER (dataset = 'viirs_snpp') AS frp_snpp,
                        sum(frp_mw) FILTER (dataset = 'viirs_noaa20') AS frp_noaa20
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
                    NULL::DOUBLE AS harmonized_cell_days,
                    round(coalesce(sum(frp_modis), 0)::DOUBLE, 1) AS modis_frp_mw,
                    round(coalesce(sum(frp_snpp), 0)::DOUBLE, 1) AS viirs_snpp_frp_mw,
                    round(coalesce(sum(frp_noaa20), 0)::DOUBLE, 1) AS viirs_noaa20_frp_mw
                FROM cell_days GROUP BY 1 ORDER BY 1
            """, parameters, shape.tables)
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
                    sum(frp_mw) FILTER (dataset = 'modis') AS frp_modis,
                    sum(frp_mw) FILTER (dataset = 'viirs_snpp') AS frp_snpp,
                    sum(frp_mw) FILTER (dataset = 'viirs_noaa20') AS frp_noaa20,
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
                    sum(c.frp_modis) AS frp_modis, sum(c.frp_snpp) AS frp_snpp, sum(c.frp_noaa20) AS frp_noaa20,
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
                {harmonized} AS harmonized_cell_days,
                round(coalesce(sum(r.frp_modis), 0)::DOUBLE, 1) AS modis_frp_mw,
                round(coalesce(sum(r.frp_snpp), 0)::DOUBLE, 1) AS viirs_snpp_frp_mw,
                round(coalesce(sum(r.frp_noaa20), 0)::DOUBLE, 1) AS viirs_noaa20_frp_mw
            FROM regional r{factor_joins}
            GROUP BY 1 ORDER BY 1
        """
        data = repo.rows(query, parameters, shape.tables)
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
        platform: Platform | None = None,
        days: Annotated[list[date] | None, Query(max_length=31)] = None,
        cell_size: float | None = None,
        viewport: Annotated[list[float] | None, Query(min_length=4, max_length=4)] = None,
        shape: AreaShape = areas.Shape(),
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
        if viewport and any(bound % cell_size for bound in viewport):
            raise HTTPException(status_code=422, detail="Map viewport must align to grid cells")
        conditions, parameters = _map_conditions(
            start_date, end_date, min_lat, max_lat, min_lon, max_lon, dataset, days, viewport, platform)
        conditions.extend(shape.conditions)

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
        """, parameters, shape.tables)
        return MapGrid(data=data, cell_size=cell_size)

    @app.get("/v1/spatial/points")
    def map_points(
        request: Request,
        start_date: date,
        end_date: date,
        min_lat: Annotated[float, Query(ge=-90, le=90)],
        max_lat: Annotated[float, Query(ge=-90, le=90)],
        min_lon: Annotated[float, Query(ge=-180, le=180)],
        max_lon: Annotated[float, Query(ge=-180, le=180)],
        dataset: Dataset | None = None,
        platform: Platform | None = None,
        days: Annotated[list[date] | None, Query(max_length=31)] = None,
        viewport: Annotated[list[float] | None, Query(min_length=4, max_length=4)] = None,
        shape: AreaShape = areas.Shape(),
    ) -> JSONResponse:
        """Individual detections for the map, as rows of MAP_POINT_COLUMNS.

        A view holding more than MAP_POINT_LIMIT detections is reported as
        truncated; the map then shows the grouped grid instead.
        """
        _date_span(start_date, end_date, 31)
        if max_lat < min_lat:
            raise HTTPException(status_code=422, detail="Invalid latitude bounds")
        if days and any(day < start_date or day > end_date for day in days):
            raise HTTPException(status_code=422, detail="Selected days must be within the date range")
        if viewport:
            south, north, west, east = viewport
            if not (-90 <= south <= north <= 90 and -180 <= west <= 180 and -180 <= east <= 180):
                raise HTTPException(status_code=422, detail="Invalid map viewport bounds")
        repo = _repository(request)
        conditions, parameters = _map_conditions(
            start_date, end_date, min_lat, max_lat, min_lon, max_lon, dataset, days, viewport, platform)
        conditions.extend(shape.conditions)
        columns = ", ".join(
            "strftime(observation_time_utc, '%Y-%m-%dT%H:%M:%SZ')" if name == "observed_utc" else name
            for name in MAP_POINT_COLUMNS
        )
        with repo.connection(shape.tables) as connection:
            rows = connection.execute(
                f"SELECT {columns} FROM {repo.points} WHERE {' AND '.join(conditions)} LIMIT ?",
                [*parameters, MAP_POINT_LIMIT + 1],
            ).fetchall()
        truncated = len(rows) > MAP_POINT_LIMIT
        return JSONResponse({
            "columns": MAP_POINT_COLUMNS,
            "rows": [] if truncated else rows,
            "truncated": truncated,
        })

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

    @app.get("/v1/anomalies")
    def region_anomalies(
        request: Request,
        month: Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$", description="YYYY-MM")],
        min_lat: Annotated[float, Query(ge=-90, le=90)] = -90,
        max_lat: Annotated[float, Query(ge=-90, le=90)] = 90,
        min_lon: Annotated[float, Query(ge=-180, le=180)] = -180,
        max_lon: Annotated[float, Query(ge=-180, le=180)] = 180,
    ) -> dict[str, Any]:
        """1° regions whose adjusted activity this month is far from the same month in other years.

        Read from the calibration's cell-days per region, month and platform mask,
        adjusted exactly as /v1/calendar adjusts them. The score is a robust z:
        the distance of log(1 + cell-days) from the median of the other years, in
        units of their median absolute deviation. One extreme year does not hide
        another, and a doubling counts alike in quiet and busy regions.
        """
        repo = _repository(request)
        if not repo.anomalies_available:
            raise HTTPException(status_code=404, detail="Anomalies need the sensor calibration. Run `python -m firms_pipeline calibrate`.")
        year, calendar_month = int(month[:4]), int(month[5:])
        target = repo.harmonization_target(None)
        longitude = "k.rx + .5 BETWEEN ? AND ?" if min_lon <= max_lon else "(k.rx + .5 >= ? OR k.rx + .5 <= ?)"
        data = repo.rows(f"""
            WITH coverage AS (
                SELECT year(month_start) AS year, bit_or({PLATFORM_BIT_SQL}) FILTER ({COMPLETE_PLATFORM_SQL})::INTEGER AS source
                FROM {repo.monthly} WHERE month(month_start) = ? GROUP BY ALL
            ),
            -- A month counts when two or more satellites observed all of it. Terra
            -- alone (2000-2002) needs a tenfold adjustment that was never validated.
            years AS (SELECT year FROM coverage WHERE bit_count(source) >= 2),
            adjusted AS (
                SELECT k.year, k.ry, k.rx,
                    sum(CASE WHEN (k.mask & c.source) = 0 THEN 0 WHEN c.source = {target} THEN k.cell_days
                        ELSE k.cell_days * coalesce(f.factor, g.factor) END) AS value
                FROM read_parquet({sql_string(repo.masks_path)}) k
                JOIN coverage c ON c.year = k.year AND bit_count(c.source) >= 2
                LEFT JOIN {repo.factors} f ON f.ry = k.ry AND f.rx = k.rx AND f.month = k.month
                    AND f.target = {target} AND f.source = c.source
                LEFT JOIN {repo.factors} g ON g.ry = {GLOBAL_REGION} AND g.month = k.month
                    AND g.target = {target} AND g.source = c.source
                WHERE k.month = ? AND k.ry + .5 BETWEEN ? AND ? AND {longitude}
                GROUP BY ALL
            ),
            filled AS (
                SELECT r.ry, r.rx, y.year, coalesce(a.value, 0) AS value
                FROM (SELECT DISTINCT ry, rx FROM adjusted) r CROSS JOIN years y
                LEFT JOIN adjusted a ON a.ry = r.ry AND a.rx = r.rx AND a.year = y.year
            ),
            baseline AS (
                SELECT ry, rx, count(*) AS years, median(value) AS centre, median(ln(1 + value)) AS centre_log
                FROM filled WHERE year != ? GROUP BY ry, rx
            ),
            spread AS (
                SELECT o.ry, o.rx, median(abs(ln(1 + o.value) - b.centre_log)) AS mad
                FROM filled o JOIN baseline b USING (ry, rx) WHERE o.year != ? GROUP BY o.ry, o.rx
            ),
            scored AS (
                SELECT f.ry, f.rx, f.value, b.centre, b.years,
                    (ln(1 + f.value) - b.centre_log) / greatest(1.4826 * s.mad, ?) AS z
                FROM filled f JOIN baseline b USING (ry, rx) JOIN spread s USING (ry, rx)
                WHERE f.year = ? AND b.years >= ? AND greatest(f.value, b.centre) >= ?
            )
            SELECT ry, rx, round(value, 1) AS value, round(centre, 1) AS baseline, years,
                round(CASE WHEN centre > 0 THEN value / centre - 1 END, 3) AS delta, round(z, 2) AS z
            FROM scored WHERE abs(z) >= ?
            ORDER BY abs(z) DESC, ry, rx
        """, [
            calendar_month, calendar_month, min_lat, max_lat, min_lon, max_lon, year, year,
            ANOMALY_MAD_FLOOR, year, ANOMALY_MIN_YEARS, ANOMALY_MIN_CELL_DAYS, ANOMALY_Z,
        ])
        observed = len(repo.rows(
            f"SELECT platform FROM {repo.monthly} WHERE month_start = ? AND {COMPLETE_PLATFORM_SQL}",
            [date(year, calendar_month, 1)])) >= 2
        return {
            "month": month, "available": observed, "threshold": ANOMALY_Z, "data": data if observed else [],
            **({} if observed else {"reason": "Fewer than two satellites observed this month in full, so its adjusted values are not reliable enough to compare."}),
        }

    return app


app = create_app()
