"""Convert real NASA FIRMS CSV exports into validated, queryable Parquet.

Raw inputs are opened read-only. A complete run is first written beneath
``data/staging`` and is published beneath ``data/processed`` only after all
validation checks pass.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import duckdb


EXPECTED_INPUTS = (
    ("DL_FIRE_J1V-C2_*", "fire_archive_J1V-C2_*.csv", "viirs_noaa20", "archive", True),
    ("DL_FIRE_J1V-C2_*", "fire_nrt_J1V-C2_*.csv", "viirs_noaa20", "nrt", False),
    ("DL_FIRE_M-C61_*", "fire_archive_M-C61_*.csv", "modis", "archive", True),
    ("DL_FIRE_M-C61_*", "fire_nrt_M-C61_*.csv", "modis", "nrt", False),
)

CSV_COLUMNS_WITH_TYPE = {
    "latitude": "DOUBLE",
    "longitude": "DOUBLE",
    "brightness": "DOUBLE",
    "scan": "DOUBLE",
    "track": "DOUBLE",
    "acq_date": "DATE",
    "acq_time": "VARCHAR",
    "satellite": "VARCHAR",
    "instrument": "VARCHAR",
    "confidence": "VARCHAR",
    "version": "VARCHAR",
    "bright_t31": "DOUBLE",
    "frp": "DOUBLE",
    "daynight": "VARCHAR",
    "type": "TINYINT",
}
CSV_COLUMNS_NO_TYPE = {k: v for k, v in CSV_COLUMNS_WITH_TYPE.items() if k != "type"}
AGGREGATE_SCHEMA_VERSION = 2
SPATIAL_SCHEMA_VERSION = 1
H3_RESOLUTIONS = (4, 5)


@dataclass(frozen=True)
class InputFile:
    path: Path
    dataset: str
    source_stage: str
    has_type: bool


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sql_string(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''").replace("\\", "/") + "'"


def discover_inputs(root: Path) -> list[InputFile]:
    found: list[InputFile] = []
    for directory_glob, file_glob, dataset, source_stage, has_type in EXPECTED_INPUTS:
        matches = sorted(root.glob(f"{directory_glob}/{file_glob}"))
        if len(matches) != 1:
            raise RuntimeError(
                f"Expected exactly one {dataset} {source_stage} CSV matching "
                f"{directory_glob}/{file_glob}; found {len(matches)}."
            )
        found.append(InputFile(matches[0].resolve(), dataset, source_stage, has_type))
    return found


def sha256_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_csv_expression(item: InputFile) -> str:
    columns = CSV_COLUMNS_WITH_TYPE if item.has_type else CSV_COLUMNS_NO_TYPE
    column_sql = "{" + ", ".join(f"'{k}': '{v}'" for k, v in columns.items()) + "}"
    return (
        f"read_csv({sql_string(item.path)}, header=true, columns={column_sql}, "
        "auto_detect=false, strict_mode=true, null_padding=false, "
        "ignore_errors=false, all_varchar=false)"
    )


def normalized_select(item: InputFile) -> str:
    type_sql = "CAST(type AS TINYINT)" if item.has_type else "CAST(NULL AS TINYINT)"
    if item.dataset == "modis":
        confidence_numeric = "CAST(confidence AS SMALLINT)"
        confidence_class = (
            "CASE WHEN confidence IS NULL THEN NULL "
            "WHEN CAST(confidence AS SMALLINT) < 30 THEN 'low' "
            "WHEN CAST(confidence AS SMALLINT) < 80 THEN 'nominal' ELSE 'high' END"
        )
        platform = (
            "CASE satellite WHEN 'Aqua' THEN 'aqua' WHEN 'Terra' THEN 'terra' "
            "ELSE lower(satellite) END"
        )
        instrument_family = "modis"
    else:
        confidence_numeric = "CAST(NULL AS SMALLINT)"
        confidence_class = (
            "CASE lower(confidence) WHEN 'l' THEN 'low' WHEN 'n' THEN 'nominal' "
            "WHEN 'h' THEN 'high' ELSE NULL END"
        )
        platform = "CASE satellite WHEN 'N20' THEN 'noaa20' ELSE lower(satellite) END"
        instrument_family = "viirs"

    return f"""
        SELECT
            strptime(strftime(acq_date, '%Y-%m-%d') || lpad(acq_time, 4, '0'),
                     '%Y-%m-%d%H%M') AS observation_time_utc,
            acq_date,
            lpad(acq_time, 4, '0') AS acq_time_hhmm,
            latitude,
            longitude,
            {sql_string(instrument_family)} AS instrument_family,
            {sql_string(item.dataset)} AS dataset,
            {platform} AS platform,
            brightness AS brightness_primary_k,
            bright_t31 AS brightness_secondary_k,
            scan AS scan_km,
            track AS track_km,
            frp AS frp_mw,
            upper(daynight) AS daynight,
            confidence AS confidence_raw,
            {confidence_numeric} AS confidence_numeric,
            {confidence_class} AS confidence_class,
            {type_sql} AS hotspot_type,
            version AS product_version,
            {sql_string(item.source_stage)} AS source_stage,
            {sql_string(item.path.name)} AS source_file,
            CAST(year(acq_date) AS SMALLINT) AS year,
            CAST(month(acq_date) AS TINYINT) AS month
        FROM {read_csv_expression(item)}
    """


def union_query(inputs: Iterable[InputFile]) -> str:
    return "\nUNION ALL BY NAME\n".join(normalized_select(item) for item in inputs)


def scalar(connection: duckdb.DuckDBPyConnection, query: str) -> int:
    return int(connection.execute(query).fetchone()[0])


def validation_queries(parquet_glob: str) -> dict[str, str]:
    source = f"read_parquet({sql_string(parquet_glob)}, hive_partitioning=true)"
    return {
        "output_rows": f"SELECT count(*) FROM {source}",
        "invalid_coordinates": (
            f"SELECT count(*) FROM {source} WHERE latitude NOT BETWEEN -90 AND 90 "
            "OR longitude NOT BETWEEN -180 AND 180"
        ),
        "invalid_times": (
            f"SELECT count(*) FROM {source} WHERE observation_time_utc IS NULL "
            "OR acq_time_hhmm !~ '^[0-2][0-9][0-5][0-9]$' "
            "OR CAST(substr(acq_time_hhmm, 1, 2) AS INTEGER) > 23"
        ),
        "invalid_daynight": f"SELECT count(*) FROM {source} WHERE daynight NOT IN ('D', 'N')",
        "invalid_confidence": (
            f"SELECT count(*) FROM {source} WHERE confidence_class IS NULL "
            "OR confidence_class NOT IN ('low', 'nominal', 'high') "
            "OR (dataset = 'modis' AND (confidence_numeric NOT BETWEEN 0 AND 100 "
            "OR confidence_numeric IS NULL)) "
            "OR (instrument_family = 'viirs' AND (confidence_numeric IS NOT NULL "
            "OR lower(confidence_raw) NOT IN ('l', 'n', 'h')))"
        ),
        "invalid_hotspot_type": (
            f"SELECT count(*) FROM {source} WHERE hotspot_type IS NOT NULL "
            "AND hotspot_type NOT BETWEEN 0 AND 3"
        ),
        "missing_required": (
            f"SELECT count(*) FROM {source} WHERE observation_time_utc IS NULL "
            "OR latitude IS NULL OR longitude IS NULL OR dataset IS NULL "
            "OR platform IS NULL OR frp_mw IS NULL OR source_file IS NULL"
        ),
    }


def input_row_counts(connection: duckdb.DuckDBPyConnection, inputs: Iterable[InputFile]) -> dict[str, int]:
    return {
        item.path.name: scalar(connection, f"SELECT count(*) FROM {read_csv_expression(item)}")
        for item in inputs
    }


def output_source_counts(connection: duckdb.DuckDBPyConnection, parquet_glob: str) -> dict[str, int]:
    rows = connection.execute(
        f"SELECT source_file, count(*) FROM read_parquet({sql_string(parquet_glob)}, "
        "hive_partitioning=true) GROUP BY source_file ORDER BY source_file"
    ).fetchall()
    return {str(name): int(count) for name, count in rows}


def build_manifest(
    inputs: list[InputFile],
    row_counts: dict[str, int],
    validation: dict[str, int],
    output_source_rows: dict[str, int],
    run_id: str,
) -> dict:
    file_entries = []
    for item in inputs:
        stat = item.path.stat()
        file_entries.append(
            {
                **asdict(item),
                "path": str(item.path),
                "size_bytes": stat.st_size,
                "modified_utc": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                "sha256": sha256_file(item.path),
                "rows": row_counts[item.path.name],
            }
        )
    return {
        "schema_version": 1,
        "run_id": run_id,
        "created_utc": utc_now(),
        "pipeline_version": "0.1.0",
        "inputs": file_entries,
        "output": {
            "format": "parquet",
            "compression": "zstd",
            "partitioning": ["dataset", "year", "month"],
            "rows_by_source": output_source_rows,
        },
        "validation": validation,
        "status": "validated",
    }


def existing_run_matches(manifest_path: Path, inputs: list[InputFile]) -> bool:
    if not manifest_path.exists():
        return False
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    existing = {
        entry["path"]: (entry["size_bytes"], entry["modified_utc"])
        for entry in manifest.get("inputs", [])
    }
    current = {
        str(item.path): (
            item.path.stat().st_size,
            datetime.fromtimestamp(item.path.stat().st_mtime, timezone.utc).isoformat(),
        )
        for item in inputs
    }
    return existing == current and manifest.get("status") == "validated"


def ingest(root: Path, threads: int | None = None) -> int:
    root = root.resolve()
    inputs = discover_inputs(root)
    data_root = root / "data"
    processed_root = data_root / "processed" / "hotspots"
    catalog_root = data_root / "catalog"
    manifest_path = catalog_root / "manifest.json"

    if processed_root.exists():
        if existing_run_matches(manifest_path, inputs):
            print("Processed data already matches the current inputs; nothing to do.")
            return 0
        raise RuntimeError(
            "Processed output already exists but does not match the current inputs. "
            "It was not overwritten. Use a future refresh/migration workflow."
        )

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    staging_run = data_root / "staging" / run_id
    staging_hotspots = staging_run / "hotspots"
    staging_hotspots.mkdir(parents=True, exist_ok=False)
    catalog_root.mkdir(parents=True, exist_ok=True)

    connection = duckdb.connect(str(staging_run / "pipeline.duckdb"))
    try:
        if threads:
            connection.execute(f"SET threads = {int(threads)}")
        connection.execute("SET preserve_insertion_order = false")

        print("Counting source rows...")
        source_counts = input_row_counts(connection, inputs)
        expected_rows = sum(source_counts.values())
        print(f"Reading and normalizing {expected_rows:,} real FIRMS observations...")

        copy_query = f"""
            COPY ({union_query(inputs)})
            TO {sql_string(staging_hotspots)}
            (FORMAT PARQUET, PARTITION_BY (dataset, year, month),
             COMPRESSION ZSTD, ROW_GROUP_SIZE 122880)
        """
        connection.execute(copy_query)

        parquet_glob = str(staging_hotspots / "**" / "*.parquet")
        print("Validating complete staged output...")
        validation = {name: scalar(connection, query) for name, query in validation_queries(parquet_glob).items()}
        output_counts = output_source_counts(connection, parquet_glob)
        validation["expected_input_rows"] = expected_rows
        validation["output_rows"] = sum(output_counts.values())

        failures = {
            name: value
            for name, value in validation.items()
            if (name.startswith("invalid_") or name == "missing_required") and value != 0
        }
        if validation["output_rows"] != expected_rows:
            failures["row_count_difference"] = validation["output_rows"] - expected_rows
        for source_file, source_rows in source_counts.items():
            if output_counts.get(source_file) != source_rows:
                failures[f"source_count:{source_file}"] = output_counts.get(source_file, 0) - source_rows
        if failures:
            raise RuntimeError(f"Validation failed; staged output retained at {staging_run}: {failures}")

        print("Hashing source files for provenance...")
        manifest = build_manifest(inputs, source_counts, validation, output_counts, run_id)
        staged_manifest = staging_run / "manifest.json"
        staged_manifest.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

        processed_root.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging_hotspots, processed_root)
        shutil.copy2(staged_manifest, manifest_path)
        print(f"Published {validation['output_rows']:,} rows to {processed_root}")
        print(f"Manifest: {manifest_path}")
        return 0
    finally:
        connection.close()


def validate_existing(root: Path) -> int:
    parquet_glob = str(root.resolve() / "data" / "processed" / "hotspots" / "**" / "*.parquet")
    connection = duckdb.connect()
    try:
        results = {name: scalar(connection, query) for name, query in validation_queries(parquet_glob).items()}
    finally:
        connection.close()
    print(json.dumps(results, indent=2))
    return 0 if all(value == 0 for name, value in results.items() if name != "output_rows") else 1


def aggregate(root: Path, threads: int | None = None) -> int:
    root = root.resolve()
    data_root = root / "data"
    points_root = data_root / "processed" / "hotspots"
    point_manifest_path = data_root / "catalog" / "manifest.json"
    aggregate_root = data_root / "aggregates"
    aggregate_manifest_path = data_root / "catalog" / "aggregates.json"
    if not points_root.exists() or not point_manifest_path.exists():
        raise RuntimeError("Validated point Parquet and its manifest are required before aggregation.")

    point_manifest = json.loads(point_manifest_path.read_text(encoding="utf-8"))
    if aggregate_root.exists():
        if aggregate_manifest_path.exists():
            previous = json.loads(aggregate_manifest_path.read_text(encoding="utf-8"))
            if (
                previous.get("source_run_id") == point_manifest.get("run_id")
                and previous.get("schema_version") == AGGREGATE_SCHEMA_VERSION
            ):
                print("Aggregates already match the published point dataset; nothing to do.")
                return 0
        raise RuntimeError("Aggregate output exists but does not match the point manifest; it was not overwritten.")

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    staging_run = data_root / "staging" / f"aggregate-{run_id}"
    staging_aggregates = staging_run / "aggregates"
    staging_aggregates.mkdir(parents=True, exist_ok=False)
    points_glob = str(points_root / "**" / "*.parquet")
    points = f"read_parquet({sql_string(points_glob)}, hive_partitioning=true)"

    connection = duckdb.connect(str(staging_run / "aggregate.duckdb"))
    try:
        if threads:
            connection.execute(f"SET threads = {int(threads)}")
        connection.execute("SET preserve_insertion_order = false")

        daily_query = f"""
            SELECT
                acq_date AS activity_date,
                dataset,
                platform,
                source_stage,
                CAST(year(acq_date) AS SMALLINT) AS year,
                CAST(month(acq_date) AS TINYINT) AS month,
                count(*)::BIGINT AS detection_count,
                sum(frp_mw)::DOUBLE AS frp_sum_mw,
                avg(frp_mw)::DOUBLE AS frp_mean_mw,
                max(frp_mw)::DOUBLE AS frp_max_mw,
                count_if(confidence_class = 'low')::BIGINT AS low_confidence_count,
                count_if(confidence_class = 'nominal')::BIGINT AS nominal_confidence_count,
                count_if(confidence_class = 'high')::BIGINT AS high_confidence_count,
                count_if(daynight = 'D')::BIGINT AS day_count,
                count_if(daynight = 'N')::BIGINT AS night_count,
                count_if(hotspot_type = 0)::BIGINT AS vegetation_fire_count,
                count_if(hotspot_type IS NOT NULL AND hotspot_type <> 0)::BIGINT AS other_type_count,
                count_if(hotspot_type IS NULL)::BIGINT AS unclassified_type_count
            FROM {points}
            GROUP BY ALL
            ORDER BY activity_date, dataset, platform, source_stage
        """
        monthly_query = f"""
            WITH source AS (
                SELECT *, year(acq_date) AS source_year, month(acq_date) AS source_month
                FROM {points}
            ),
            coverage AS (
                SELECT
                    dataset,
                    source_year,
                    source_month,
                    min(acq_date) AS dataset_first_date,
                    max(acq_date) AS dataset_last_date,
                    count(DISTINCT acq_date)::SMALLINT AS dataset_observed_days
                FROM source
                GROUP BY ALL
            )
            SELECT
                source.dataset,
                source.platform,
                CAST(source.source_year AS SMALLINT) AS year,
                CAST(source.source_month AS TINYINT) AS month,
                make_date(source.source_year, source.source_month, 1) AS month_start,
                coverage.dataset_first_date AS first_observed_date,
                coverage.dataset_last_date AS last_observed_date,
                count(DISTINCT source.acq_date)::SMALLINT AS active_days,
                coverage.dataset_observed_days,
                day(last_day(coverage.dataset_first_date))::SMALLINT AS calendar_days,
                (coverage.dataset_first_date = make_date(source.source_year, source.source_month, 1)
                 AND coverage.dataset_last_date = last_day(coverage.dataset_first_date)
                 AND coverage.dataset_observed_days = day(last_day(coverage.dataset_first_date))) AS is_complete_month,
                CASE
                    WHEN coverage.dataset_first_date = make_date(source.source_year, source.source_month, 1)
                     AND coverage.dataset_last_date = last_day(coverage.dataset_first_date)
                     AND coverage.dataset_observed_days = day(last_day(coverage.dataset_first_date)) THEN 'complete'
                    WHEN coverage.dataset_first_date > make_date(source.source_year, source.source_month, 1)
                     AND coverage.dataset_last_date < last_day(coverage.dataset_first_date) THEN 'partial_both'
                    WHEN coverage.dataset_first_date > make_date(source.source_year, source.source_month, 1) THEN 'partial_start'
                    ELSE 'partial_end'
                END AS period_status,
                count(*)::BIGINT AS detection_count,
                count_if(source_stage = 'archive')::BIGINT AS archive_count,
                count_if(source_stage = 'nrt')::BIGINT AS nrt_count,
                sum(frp_mw)::DOUBLE AS frp_sum_mw,
                avg(frp_mw)::DOUBLE AS frp_mean_mw,
                max(frp_mw)::DOUBLE AS frp_max_mw,
                count_if(confidence_class = 'low')::BIGINT AS low_confidence_count,
                count_if(confidence_class = 'nominal')::BIGINT AS nominal_confidence_count,
                count_if(confidence_class = 'high')::BIGINT AS high_confidence_count,
                count_if(daynight = 'D')::BIGINT AS day_count,
                count_if(daynight = 'N')::BIGINT AS night_count,
                count_if(hotspot_type = 0)::BIGINT AS vegetation_fire_count,
                count_if(hotspot_type IS NOT NULL AND hotspot_type <> 0)::BIGINT AS other_type_count,
                count_if(hotspot_type IS NULL)::BIGINT AS unclassified_type_count
            FROM source
            JOIN coverage USING (dataset, source_year, source_month)
            GROUP BY
                source.dataset, source.platform, source.source_year, source.source_month,
                coverage.dataset_first_date, coverage.dataset_last_date, coverage.dataset_observed_days
            ORDER BY dataset, platform, year, month
        """

        daily_path = staging_aggregates / "daily_global.parquet"
        monthly_path = staging_aggregates / "monthly_global.parquet"
        print("Building daily global aggregates...")
        connection.execute(
            f"COPY ({daily_query}) TO {sql_string(daily_path)} "
            "(FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 122880)"
        )
        print("Building monthly global aggregates...")
        connection.execute(
            f"COPY ({monthly_query}) TO {sql_string(monthly_path)} "
            "(FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 122880)"
        )

        point_rows = scalar(connection, f"SELECT count(*) FROM {points}")
        daily_detection_rows = scalar(
            connection, f"SELECT sum(detection_count) FROM read_parquet({sql_string(daily_path)})"
        )
        monthly_detection_rows = scalar(
            connection, f"SELECT sum(detection_count) FROM read_parquet({sql_string(monthly_path)})"
        )
        daily_rows = scalar(connection, f"SELECT count(*) FROM read_parquet({sql_string(daily_path)})")
        monthly_rows = scalar(connection, f"SELECT count(*) FROM read_parquet({sql_string(monthly_path)})")
        invalid_months = scalar(
            connection,
            f"SELECT count(*) FROM read_parquet({sql_string(monthly_path)}) "
            "WHERE detection_count <> low_confidence_count + nominal_confidence_count + high_confidence_count "
            "OR detection_count <> day_count + night_count",
        )
        if daily_detection_rows != point_rows or monthly_detection_rows != point_rows or invalid_months:
            raise RuntimeError(
                "Aggregate validation failed: "
                f"points={point_rows}, daily_sum={daily_detection_rows}, "
                f"monthly_sum={monthly_detection_rows}, invalid_months={invalid_months}."
            )

        manifest = {
            "schema_version": AGGREGATE_SCHEMA_VERSION,
            "run_id": run_id,
            "created_utc": utc_now(),
            "source_run_id": point_manifest["run_id"],
            "source_rows": point_rows,
            "outputs": {
                "daily_global": {"file": "daily_global.parquet", "rows": daily_rows},
                "monthly_global": {"file": "monthly_global.parquet", "rows": monthly_rows},
            },
            "validation": {
                "daily_detection_sum": daily_detection_rows,
                "monthly_detection_sum": monthly_detection_rows,
                "invalid_months": invalid_months,
            },
            "status": "validated",
        }
        staged_manifest = staging_run / "aggregates.json"
        staged_manifest.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        os.replace(staging_aggregates, aggregate_root)
        shutil.copy2(staged_manifest, aggregate_manifest_path)
        print(f"Published {daily_rows:,} daily and {monthly_rows:,} monthly aggregate rows.")
        return 0
    finally:
        connection.close()


def build_spatial(root: Path, threads: int | None = None) -> int:
    import h3

    root = root.resolve()
    data_root = root / "data"
    points_root = data_root / "processed" / "hotspots"
    point_manifest_path = data_root / "catalog" / "manifest.json"
    spatial_root = data_root / "spatial" / "h3"
    spatial_manifest_path = data_root / "catalog" / "spatial.json"
    if not points_root.exists() or not point_manifest_path.exists():
        raise RuntimeError("Validated point Parquet and its manifest are required before spatial aggregation.")

    point_manifest = json.loads(point_manifest_path.read_text(encoding="utf-8"))
    if spatial_root.exists():
        if spatial_manifest_path.exists():
            previous = json.loads(spatial_manifest_path.read_text(encoding="utf-8"))
            if (
                previous.get("source_run_id") == point_manifest.get("run_id")
                and previous.get("schema_version") == SPATIAL_SCHEMA_VERSION
            ):
                print("Spatial aggregates already match the published point dataset; nothing to do.")
                return 0
        raise RuntimeError("Spatial output exists but does not match the point manifest; it was not overwritten.")

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    staging_run = data_root / "staging" / f"spatial-{run_id}"
    staging_spatial = staging_run / "h3"
    staging_spatial.mkdir(parents=True, exist_ok=False)
    points_glob = str(points_root / "**" / "*.parquet")
    points = f"read_parquet({sql_string(points_glob)}, hive_partitioning=true)"

    connection = duckdb.connect(str(staging_run / "spatial.duckdb"))
    try:
        if threads:
            connection.execute(f"SET threads = {int(threads)}")
        connection.execute("SET preserve_insertion_order = false")
        connection.create_function(
            "h3_r4", lambda latitude, longitude: h3.latlng_to_cell(latitude, longitude, 4),
            ["DOUBLE", "DOUBLE"], "VARCHAR"
        )
        connection.create_function(
            "h3_r5", lambda latitude, longitude: h3.latlng_to_cell(latitude, longitude, 5),
            ["DOUBLE", "DOUBLE"], "VARCHAR"
        )
        connection.create_function(
            "h3_area_km2", lambda cell: h3.cell_area(cell, unit="km^2"), ["VARCHAR"], "DOUBLE"
        )
        connection.create_function(
            "h3_center_lat", lambda cell: h3.cell_to_latlng(cell)[0], ["VARCHAR"], "DOUBLE"
        )
        connection.create_function(
            "h3_center_lon", lambda cell: h3.cell_to_latlng(cell)[1], ["VARCHAR"], "DOUBLE"
        )

        point_rows = scalar(connection, f"SELECT count(*) FROM {points}")
        outputs: dict[str, dict[str, Any]] = {}
        for resolution in H3_RESOLUTIONS:
            resolution_root = staging_spatial / f"resolution={resolution}"
            resolution_root.mkdir(parents=True, exist_ok=False)
            h3_function = f"h3_r{resolution}"
            spatial_query = f"""
                WITH indexed AS (
                    SELECT
                        {h3_function}(latitude, longitude) AS h3_cell,
                        dataset,
                        platform,
                        acq_date,
                        frp_mw,
                        confidence_class,
                        daynight,
                        hotspot_type,
                        source_stage,
                        year,
                        month
                    FROM {points}
                ),
                metrics AS (
                    SELECT
                        h3_cell,
                        dataset,
                        platform,
                        CAST(year AS SMALLINT) AS year,
                        CAST(month AS TINYINT) AS month,
                        count(*)::BIGINT AS detection_count,
                        count(DISTINCT acq_date)::SMALLINT AS active_days,
                        sum(frp_mw)::DOUBLE AS frp_sum_mw,
                        avg(frp_mw)::DOUBLE AS frp_mean_mw,
                        max(frp_mw)::DOUBLE AS frp_max_mw,
                        count_if(confidence_class = 'low')::BIGINT AS low_confidence_count,
                        count_if(confidence_class = 'nominal')::BIGINT AS nominal_confidence_count,
                        count_if(confidence_class = 'high')::BIGINT AS high_confidence_count,
                        count_if(daynight = 'D')::BIGINT AS day_count,
                        count_if(daynight = 'N')::BIGINT AS night_count,
                        count_if(source_stage = 'archive')::BIGINT AS archive_count,
                        count_if(source_stage = 'nrt')::BIGINT AS nrt_count,
                        count_if(hotspot_type = 0)::BIGINT AS vegetation_fire_count,
                        count_if(hotspot_type IS NULL)::BIGINT AS unclassified_type_count
                    FROM indexed
                    GROUP BY ALL
                )
                SELECT
                    h3_cell,
                    {resolution}::TINYINT AS resolution,
                    h3_center_lat(h3_cell) AS center_latitude,
                    h3_center_lon(h3_cell) AS center_longitude,
                    h3_area_km2(h3_cell) AS cell_area_km2,
                    detection_count,
                    detection_count / h3_area_km2(h3_cell) AS detections_per_km2,
                    active_days,
                    frp_sum_mw,
                    frp_sum_mw / h3_area_km2(h3_cell) AS frp_mw_per_km2,
                    frp_mean_mw,
                    frp_max_mw,
                    low_confidence_count,
                    nominal_confidence_count,
                    high_confidence_count,
                    day_count,
                    night_count,
                    archive_count,
                    nrt_count,
                    vegetation_fire_count,
                    unclassified_type_count,
                    dataset,
                    platform,
                    year,
                    month
                FROM metrics
            """
            print(f"Building H3 resolution {resolution} monthly spatial aggregates...")
            connection.execute(
                f"COPY ({spatial_query}) TO {sql_string(resolution_root)} "
                "(FORMAT PARQUET, PARTITION_BY (dataset, year, month), "
                "COMPRESSION ZSTD, ROW_GROUP_SIZE 122880)"
            )
            output_glob = str(resolution_root / "**" / "*.parquet")
            output_rows = scalar(
                connection, f"SELECT count(*) FROM read_parquet({sql_string(output_glob)}, hive_partitioning=true)"
            )
            detection_sum = scalar(
                connection,
                f"SELECT sum(detection_count) FROM read_parquet({sql_string(output_glob)}, hive_partitioning=true)",
            )
            invalid_rows = scalar(
                connection,
                f"""SELECT count(*) FROM read_parquet({sql_string(output_glob)}, hive_partitioning=true)
                    WHERE h3_cell IS NULL OR cell_area_km2 <= 0
                       OR detection_count <> low_confidence_count + nominal_confidence_count + high_confidence_count
                       OR detection_count <> day_count + night_count
                       OR detection_count <> archive_count + nrt_count""",
            )
            if detection_sum != point_rows or invalid_rows:
                raise RuntimeError(
                    f"H3 resolution {resolution} validation failed: points={point_rows}, "
                    f"detection_sum={detection_sum}, invalid_rows={invalid_rows}."
                )
            outputs[str(resolution)] = {
                "rows": output_rows,
                "detection_sum": detection_sum,
                "invalid_rows": invalid_rows,
            }

        manifest = {
            "schema_version": SPATIAL_SCHEMA_VERSION,
            "run_id": run_id,
            "created_utc": utc_now(),
            "source_run_id": point_manifest["run_id"],
            "source_rows": point_rows,
            "grid": "H3",
            "resolutions": list(H3_RESOLUTIONS),
            "area_normalization": "Exact spherical H3 cell area in square kilometres",
            "outputs": outputs,
            "status": "validated",
        }
        staged_manifest = staging_run / "spatial.json"
        staged_manifest.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        spatial_root.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging_spatial, spatial_root)
        shutil.copy2(staged_manifest, spatial_manifest_path)
        print(
            "Published spatial aggregates: "
            + ", ".join(f"H3 r{resolution}={outputs[str(resolution)]['rows']:,} rows" for resolution in H3_RESOLUTIONS)
        )
        return 0
    finally:
        connection.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="Project root (default: current directory)")
    subparsers = parser.add_subparsers(dest="command", required=True)
    ingest_parser = subparsers.add_parser("ingest", help="Convert and validate all discovered real FIRMS inputs")
    ingest_parser.add_argument("--threads", type=int, default=None, help="DuckDB worker threads")
    subparsers.add_parser("validate", help="Revalidate the published Parquet dataset")
    aggregate_parser = subparsers.add_parser("aggregate", help="Build validated global daily and monthly metrics")
    aggregate_parser.add_argument("--threads", type=int, default=None, help="DuckDB worker threads")
    spatial_parser = subparsers.add_parser("spatial", help="Build validated area-aware H3 monthly metrics")
    spatial_parser.add_argument("--threads", type=int, default=None, help="DuckDB worker threads")
    audit_parser = subparsers.add_parser(
        "audit-archives", help="Validate annual country-partitioned FIRMS ZIP coverage and schemas"
    )
    audit_parser.add_argument(
        "--check-crc", action="store_true", help="Decompress every member to perform a complete CRC check"
    )
    history_parser = subparsers.add_parser(
        "ingest-history", help="Convert validated annual ZIP archives into staged historical Parquet"
    )
    history_parser.add_argument("--threads", type=int, default=None, help="DuckDB worker threads")
    history_parser.add_argument(
        "--resume", action="store_true", help="Reuse completed fragments from the latest interrupted history run"
    )
    merge_parser = subparsers.add_parser(
        "merge", help="Build a separately validated archive-through-2025/current-from-2026 dataset"
    )
    merge_parser.add_argument("--threads", type=int, default=None, help="DuckDB worker threads")
    merged_aggregate_parser = subparsers.add_parser(
        "aggregate-merged", help="Build merged daily/monthly metrics with bounded annual scans"
    )
    merged_aggregate_parser.add_argument("--threads", type=int, default=None, help="DuckDB worker threads")
    merged_spatial_parser = subparsers.add_parser(
        "spatial-merged", help="Build resumable H3 metrics with bounded dataset/month scans"
    )
    merged_spatial_parser.add_argument("--threads", type=int, default=None, help="DuckDB worker threads")
    merged_spatial_parser.add_argument("--resume", action="store_true", help="Reuse completed monthly fragments")
    calibrate_parser = subparsers.add_parser(
        "calibrate", help="Calculate VIIRS-to-MODIS sensitivity factors from overlap years"
    )
    calibrate_parser.add_argument("--threads", type=int, default=None, help="DuckDB worker threads")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "ingest":
            return ingest(args.root, args.threads)
        if args.command == "aggregate":
            return aggregate(args.root, args.threads)
        if args.command == "spatial":
            return build_spatial(args.root, args.threads)
        if args.command == "audit-archives":
            from .archives import write_audit_report

            return write_audit_report(args.root, check_crc=args.check_crc)
        if args.command == "ingest-history":
            from .archives import ingest_annual_archives

            return ingest_annual_archives(args.root, threads=args.threads, resume=args.resume)
        if args.command == "merge":
            from .merge import build_merged

            return build_merged(args.root, threads=args.threads)
        if args.command == "aggregate-merged":
            from .merge import aggregate_merged

            return aggregate_merged(args.root, threads=args.threads)
        if args.command == "spatial-merged":
            from .merge import build_spatial_merged

            return build_spatial_merged(args.root, threads=args.threads, resume=args.resume)
        if args.command == "calibrate":
            from .calibrate import build_calibration

            return build_calibration(args.root, threads=args.threads)
        return validate_existing(args.root)
    except (duckdb.Error, OSError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
