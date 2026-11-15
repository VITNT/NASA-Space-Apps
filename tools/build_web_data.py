"""Build the Parquet products that Burnline reads directly from a browser.

The merged archive is ordered by time, so a browser would have to download a
whole month to answer one map view. This writes a second copy that is sorted by
location and pre-aggregated, so DuckDB-WASM can fetch only the few row groups
that a query touches. Nothing here invents or alters observations.

    python -m tools.build_web_data            # writes data/web
    python -m tools.build_web_data --year 2024

Layout under the output folder:

    meta.json                     coverage, calibration and the list of months
    points/YYYY/MM.parquet        every detection, sorted by 2° tile
    map/d16/YYYY/MM.parquet       daily 1/16° grid per platform, sorted by 8° tile
    map/d4/YYYY/MM.parquet        daily 1/4° grid per platform, sorted by day
    map/m4/YYYY/MM.parquet        monthly 1/4° grid per platform
    cal/cells/YYYY.parquet        0.1° active cell-days, sorted by 5° tile
    cal/region_day/YYYY.parquet   cell-days per 1° region, day and platform mask
    cal/region_month/YYYY.parquet the same per month
    cal/factors.parquet           harmonization factors for the served targets
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import duckdb

from firms_pipeline.api import DataRepository
from firms_pipeline.pipeline import sql_string
from firms_pipeline.sensitivity import CELL_DAY_FILTER, CELL_X_SQL, CELL_Y_SQL, PLATFORM_BIT_SQL

SCHEMA_VERSION = 1
PARQUET = "FORMAT PARQUET, COMPRESSION ZSTD"
LONGITUDE = "CASE WHEN longitude = 180 THEN -180 ELSE longitude END"


def copy(connection: duckdb.DuckDBPyConnection, query: str, path: Path, row_group_size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(".partial")
    connection.execute(f"COPY ({query}) TO {sql_string(str(partial))} ({PARQUET}, ROW_GROUP_SIZE {row_group_size})")
    partial.replace(path)


def build_month(connection: duckdb.DuckDBPyConnection, files: list[Path], out: Path, year: int, month: int) -> int:
    sources = "[" + ", ".join(sql_string(str(path)) for path in files) + "]"
    connection.execute(f"""
        CREATE OR REPLACE TEMP TABLE p AS
        SELECT acq_date,
            (hour(observation_time_utc) * 60 + minute(observation_time_utc))::SMALLINT AS minute,
            round(latitude * 1e5)::INTEGER AS lat_e5,
            round(longitude * 1e5)::INTEGER AS lon_e5,
            platform,
            round(frp_mw * 100)::INTEGER AS frp_c,
            confidence_class, confidence_raw, daynight, hotspot_type, source_stage,
            least(1439, floor(latitude * 16))::SMALLINT AS gy,
            floor({LONGITUDE} * 16)::SMALLINT AS gx,
            ({CELL_Y_SQL})::SMALLINT AS cy,
            ({CELL_X_SQL})::SMALLINT AS cx,
            ({PLATFORM_BIT_SQL})::UTINYINT AS bit,
            dataset,
            coalesce({CELL_DAY_FILTER}, false) AS qualifies,
            latitude, {LONGITUDE} AS point_longitude, frp_mw
        FROM read_parquet({sources}, hive_partitioning=true)
    """)
    rows, inexact, misdated = connection.execute("""
        SELECT count(*),
            count_if(lat_e5 / 1e5 != latitude OR lon_e5 / 1e5 != (CASE WHEN point_longitude = -180 AND lon_e5 = 18000000 THEN 180 ELSE point_longitude END)
                OR (frp_mw IS NOT NULL AND frp_c / 100.0 != frp_mw)),
            count_if(bit = 0 OR acq_date IS NULL OR year(acq_date) != ? OR month(acq_date) != ?)
        FROM p
    """, [year, month]).fetchone()
    if inexact or misdated:
        raise SystemExit(f"{year}-{month:02d}: {inexact} values would lose precision and {misdated} rows are misfiled. Nothing was written for this month.")

    name = f"{year}/{month:02d}.parquet"
    copy(connection, """
        SELECT acq_date, minute, lat_e5, lon_e5, platform, frp_c,
            confidence_class, confidence_raw, daynight, hotspot_type, source_stage
        FROM p
        ORDER BY floor(lat_e5 / 200000), floor(lon_e5 / 200000), acq_date, minute, lat_e5, lon_e5, platform
    """, out / "points" / name, 50_000)
    copy(connection, """
        SELECT * FROM (
            SELECT acq_date, platform, gy AS y, gx AS x, count(*)::INTEGER AS n, sum(frp_mw)::FLOAT AS frp
            FROM p GROUP BY ALL
        ) ORDER BY floor(y / 128), floor(x / 128), acq_date, y, x, platform
    """, out / "map" / "d16" / name, 40_000)
    copy(connection, """
        SELECT * FROM (
            SELECT acq_date, platform, floor(gy / 4)::SMALLINT AS y, floor(gx / 4)::SMALLINT AS x, count(*)::INTEGER AS n,
                sum(latitude)::FLOAT AS sum_lat, sum(point_longitude)::FLOAT AS sum_lon, sum(frp_mw)::FLOAT AS frp
            FROM p GROUP BY ALL
        ) ORDER BY acq_date, y, x, platform
    """, out / "map" / "d4" / name, 40_000)
    copy(connection, """
        SELECT * FROM (SELECT platform, floor(gy / 4)::SMALLINT AS y, floor(gx / 4)::SMALLINT AS x, count(*)::INTEGER AS n,
            sum(latitude)::FLOAT AS sum_lat, sum(point_longitude)::FLOAT AS sum_lon, sum(frp_mw)::FLOAT AS frp,
            bit_or(1 << (day(acq_date) - 1))::INTEGER AS days
            FROM p GROUP BY ALL
        ) ORDER BY y, x, platform
    """, out / "map" / "m4" / name, 40_000)
    connection.execute("""
        INSERT INTO cells
        SELECT acq_date, cy, cx,
            count_if(dataset = 'modis')::INTEGER, count_if(dataset = 'viirs_snpp')::INTEGER,
            count_if(dataset = 'viirs_noaa20')::INTEGER, bit_or(bit)::UTINYINT
        FROM p WHERE qualifies GROUP BY ALL
    """)
    return rows


def build_year(connection: duckdb.DuckDBPyConnection, out: Path, year: int) -> None:
    name = f"{year}.parquet"
    regions = """
        SELECT * FROM (SELECT {period} AS {column}, floor(y / 10)::SMALLINT AS ry, floor(x / 10)::SMALLINT AS rx, mask,
            count(*)::INTEGER AS n, sum(modis)::INTEGER AS modis,
            sum(viirs_snpp)::INTEGER AS viirs_snpp, sum(viirs_noaa20)::INTEGER AS viirs_noaa20
        FROM cells GROUP BY ALL) ORDER BY {order}
    """
    copy(connection, regions.format(period="acq_date", column="acq_date", order="acq_date, ry, rx, mask"),
         out / "cal" / "region_day" / name, 40_000)
    copy(connection, regions.format(period="date_trunc('month', acq_date)::DATE", column="month_start", order="ry, rx, month_start, mask"),
         out / "cal" / "region_month" / name, 40_000)
    # Written last: its presence marks the year as complete.
    copy(connection, """
        SELECT acq_date, y, x, modis, viirs_snpp, viirs_noaa20, mask FROM cells
        ORDER BY floor(y / 50), floor(x / 50), acq_date, y, x
    """, out / "cal" / "cells" / name, 30_000)


def write_meta(repo: DataRepository, connection: duckdb.DuckDBPyConnection, out: Path, months: list[str]) -> None:
    coverage = repo.rows(f"""
        SELECT dataset, platform, min(first_observed_date) AS first_date, max(last_observed_date) AS last_date,
            sum(detection_count)::BIGINT AS detection_count,
            count_if(is_complete_month)::INTEGER AS complete_months,
            count_if(NOT is_complete_month)::INTEGER AS partial_months
        FROM {repo.monthly} GROUP BY dataset, platform ORDER BY dataset, platform
    """)
    monthly = repo.rows(f"""
        SELECT month_start, dataset, platform, is_complete_month, active_days = calendar_days AS platform_complete
        FROM {repo.monthly} ORDER BY month_start, dataset, platform
    """)
    calibration = {"available": False}
    if repo.calibration:
        calibration = {"available": True, **repo.calibration}
        targets = sorted(set(repo.calibration["targets"].values()))
        copy(connection, f"""
            SELECT ry::SMALLINT AS ry, rx::SMALLINT AS rx, month::TINYINT AS month,
                target::UTINYINT AS target, source::UTINYINT AS source, factor
            FROM {repo.factors} WHERE target IN ({", ".join(map(str, targets))})
            ORDER BY target, month, ry, rx, source
        """, out / "cal" / "factors.parquet", 20_000)
    meta = {
        "schema_version": SCHEMA_VERSION,
        "health": {
            "status": "ok",
            "data_profile": repo.profile,
            "point_run_id": repo.point_manifest["run_id"],
            "aggregate_run_id": repo.aggregate_manifest["run_id"],
            "spatial_run_id": repo.spatial_manifest["run_id"],
            "source_rows": repo.aggregate_manifest["source_rows"],
            "area_label": repo.point_manifest.get("area_label"),
            "calibration_available": repo.calibration is not None,
        },
        "coverage": coverage,
        "coverage_monthly": monthly,
        "calibration": calibration,
        "months": months,
    }
    (out / "meta.json").write_text(json.dumps(meta, default=str, separators=(",", ":")), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project", type=Path, default=Path.cwd(), help="Folder that contains data/merged and data/catalog")
    parser.add_argument("--out", type=Path, help="Output folder (default: <project>/data/web)")
    parser.add_argument("--year", type=int, action="append", help="Only build this year; repeat for several")
    parser.add_argument("--force", action="store_true", help="Rebuild years that are already complete")
    args = parser.parse_args()

    repo = DataRepository(args.project, profile="merged")
    if repo.profile != "merged":
        raise SystemExit("The merged archive is missing. Run the merge step first.")
    out = (args.out or args.project / "data" / "web").resolve()
    hotspots = args.project.resolve() / "data" / "merged" / "hotspots"

    by_month: dict[tuple[int, int], list[Path]] = {}
    for path in sorted(hotspots.glob("dataset=*/year=*/month=*/*.parquet")):
        year = int(path.parent.parent.name.split("=")[1])
        month = int(path.parent.name.split("=")[1])
        by_month.setdefault((year, month), []).append(path)
    if not by_month:
        raise SystemExit(f"No hotspot files under {hotspots}")

    connection = duckdb.connect()
    years = sorted({year for year, _ in by_month})
    total = 0
    for year in years:
        if args.year and year not in args.year:
            continue
        if not args.force and (out / "cal" / "cells" / f"{year}.parquet").exists():
            print(f"{year}: already built", flush=True)
            continue
        started = time.time()
        connection.execute("""
            CREATE OR REPLACE TEMP TABLE cells (acq_date DATE, y SMALLINT, x SMALLINT,
                modis INTEGER, viirs_snpp INTEGER, viirs_noaa20 INTEGER, mask UTINYINT)
        """)
        rows = 0
        for (file_year, month), files in sorted(by_month.items()):
            if file_year == year:
                rows += build_month(connection, files, out, year, month)
        build_year(connection, out, year)
        total += rows
        print(f"{year}: {rows:,} detections in {time.time() - started:.0f}s", flush=True)

    built = sorted(
        f"{year}-{month:02d}" for year, month in by_month
        if (out / "points" / f"{year}/{month:02d}.parquet").exists() and (out / "cal" / "cells" / f"{year}.parquet").exists()
    )
    write_meta(repo, connection, out, built)
    missing = len(by_month) - len(built)
    size = sum(path.stat().st_size for path in out.rglob("*.parquet")) / 1e9
    print(f"Done: {len(built)} months, {size:.1f} GB in {out}" + (f" ({missing} months not built yet)" if missing else ""), flush=True)


if __name__ == "__main__":
    main()
