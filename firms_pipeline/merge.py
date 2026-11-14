"""Build validated historical/current FIRMS products without replacing live data."""

from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Any

import duckdb

from .pipeline import scalar, sql_string, utc_now, validation_queries


ARCHIVE_CUTOFF_YEAR = 2025
MERGED_SCHEMA_VERSION = 1
MERGED_AGGREGATE_SCHEMA_VERSION = 1


def _parquet_glob(root: Path) -> str:
    return str(root / "**" / "*.parquet")


def _read_manifest(path: Path, label: str) -> dict[str, Any]:
    if not path.exists():
        raise RuntimeError(f"{label} manifest is missing: {path}")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("status") != "validated":
        raise RuntimeError(f"{label} manifest is not validated: {path}")
    return manifest


def _link_tree(source: Path, destination: Path) -> tuple[int, int]:
    """Hard-link an immutable Parquet tree, falling back to copies across volumes."""
    linked = copied = 0
    for source_file in sorted(source.rglob("*.parquet")):
        target = destination / source_file.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(source_file, target)
            linked += 1
        except OSError:
            shutil.copy2(source_file, target)
            copied += 1
    return linked, copied


def merge_selection_sql(historical_glob: str, current_glob: str) -> str:
    """The explicit precedence rule used by tests and the merge implementation."""
    return f"""
        SELECT * FROM read_parquet({sql_string(historical_glob)}, hive_partitioning=true)
        WHERE year <= {ARCHIVE_CUTOFF_YEAR}
        UNION ALL BY NAME
        SELECT * FROM read_parquet({sql_string(current_glob)}, hive_partitioning=true)
        WHERE year > {ARCHIVE_CUTOFF_YEAR}
    """


def build_merged(root: Path, threads: int | None = None) -> int:
    """Stage and validate archive-through-2025 plus current-from-2026 points."""
    root = root.resolve()
    data_root = root / "data"
    historical_root = data_root / "historical" / "hotspots"
    current_root = data_root / "processed" / "hotspots"
    merged_root = data_root / "merged" / "hotspots"
    historical_manifest_path = data_root / "catalog" / "historical_manifest.json"
    current_manifest_path = data_root / "catalog" / "manifest.json"
    merged_manifest_path = data_root / "catalog" / "merged_manifest.json"
    historical_manifest = _read_manifest(historical_manifest_path, "Historical")
    current_manifest = _read_manifest(current_manifest_path, "Current")
    if not historical_root.exists() or not current_root.exists():
        raise RuntimeError("Both historical and current point datasets are required.")

    source_ids = {
        "historical_run_id": historical_manifest["run_id"],
        "current_run_id": current_manifest["run_id"],
    }
    if merged_root.exists():
        if merged_manifest_path.exists():
            previous = json.loads(merged_manifest_path.read_text(encoding="utf-8"))
            if previous.get("sources") == source_ids and previous.get("status") == "validated":
                print("Merged points already match both source manifests; nothing to do.")
                return 0
        raise RuntimeError("Merged output already exists but does not match its sources; it was not overwritten.")

    run_id = "merge-" + utc_now().replace(":", "").replace("-", "")[:15] + "-" + uuid.uuid4().hex[:8]
    staging_run = data_root / "staging" / run_id
    staging_hotspots = staging_run / "hotspots"
    staging_hotspots.mkdir(parents=True, exist_ok=False)
    connection = duckdb.connect(str(staging_run / "merge.duckdb"))
    try:
        if threads:
            connection.execute(f"SET threads = {int(threads)}")
        connection.execute("SET preserve_insertion_order = false")
        historical_glob = _parquet_glob(historical_root)
        current_glob = _parquet_glob(current_root)

        print("Linking validated annual archive partitions through 2025...", flush=True)
        linked_files, copied_files = _link_tree(historical_root, staging_hotspots)
        if not linked_files and not copied_files:
            raise RuntimeError("Historical point dataset contains no Parquet files.")

        current_years = [
            int(row[0]) for row in connection.execute(
                f"SELECT DISTINCT year FROM read_parquet({sql_string(current_glob)}, hive_partitioning=true) "
                f"WHERE year > {ARCHIVE_CUTOFF_YEAR} ORDER BY year"
            ).fetchall()
        ]
        if not current_years:
            raise RuntimeError(f"Current point dataset has no rows after {ARCHIVE_CUTOFF_YEAR}.")

        current_rows_by_year: dict[str, int] = {}
        current_validation: dict[str, dict[str, int]] = {}
        for year in current_years:
            print(f"Materializing bounded current partition for {year}...", flush=True)
            year_root = staging_hotspots / "_current_years" / str(year)
            year_root.mkdir(parents=True, exist_ok=False)
            connection.execute(
                f"COPY (SELECT * FROM read_parquet({sql_string(current_glob)}, hive_partitioning=true) "
                f"WHERE year = {year}) TO {sql_string(year_root)} "
                "(FORMAT PARQUET, PARTITION_BY (dataset, year, month), COMPRESSION ZSTD, "
                "ROW_GROUP_SIZE 122880)"
            )
            # Move each bounded result into the common hive tree. Historical data has
            # no post-cutoff partitions, so a collision indicates an unsafe merge.
            for source_file in sorted(year_root.rglob("*.parquet")):
                relative = source_file.relative_to(year_root)
                target = staging_hotspots / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    raise RuntimeError(f"Unexpected merge partition collision: {target}")
                os.replace(source_file, target)
            shutil.rmtree(staging_hotspots / "_current_years")
            output_year_glob = str(staging_hotspots / "dataset=*" / f"year={year}" / "**" / "*.parquet")
            checks = {
                name: scalar(connection, query)
                for name, query in validation_queries(output_year_glob).items()
            }
            failures = {
                name: value for name, value in checks.items()
                if (name.startswith("invalid_") or name == "missing_required") and value
            }
            expected = scalar(
                connection,
                f"SELECT count(*) FROM read_parquet({sql_string(current_glob)}, hive_partitioning=true) "
                f"WHERE year = {year}",
            )
            if checks["output_rows"] != expected:
                failures["row_count_difference"] = checks["output_rows"] - expected
            if failures:
                raise RuntimeError(f"Merged current partition {year} failed validation: {failures}")
            current_rows_by_year[str(year)] = expected
            current_validation[str(year)] = checks

        historical_rows = int(historical_manifest["output"]["rows"])
        current_rows = sum(current_rows_by_year.values())
        expected_rows = historical_rows + current_rows
        # count(*) is metadata-driven for Parquet and is safe across the full tree.
        merged_glob = _parquet_glob(staging_hotspots)
        output_rows = scalar(
            connection, f"SELECT count(*) FROM read_parquet({sql_string(merged_glob)}, hive_partitioning=true)"
        )
        if output_rows != expected_rows:
            raise RuntimeError(
                f"Merged row count mismatch: output={output_rows}, expected={expected_rows}."
            )
        overlap_rows = scalar(
            connection,
            f"SELECT count(*) FROM read_parquet({sql_string(merged_glob)}, hive_partitioning=true) "
            f"WHERE year <= {ARCHIVE_CUTOFF_YEAR} AND source_file LIKE 'fire_%'",
        )
        if overlap_rows:
            raise RuntimeError(f"Found {overlap_rows} current-file rows at or before the archive cutoff.")

        manifest = {
            "schema_version": MERGED_SCHEMA_VERSION,
            "run_id": run_id,
            "created_utc": utc_now(),
            "sources": source_ids,
            "precedence": {
                "annual_archives_through_year": ARCHIVE_CUTOFF_YEAR,
                "current_archive_nrt_from_year": ARCHIVE_CUTOFF_YEAR + 1,
            },
            "output": {
                "format": "parquet",
                "compression": "zstd",
                "partitioning": ["dataset", "year", "month"],
                "rows": output_rows,
                "historical_rows": historical_rows,
                "current_rows": current_rows,
                "current_rows_by_year": current_rows_by_year,
                "hard_linked_historical_files": linked_files,
                "copied_historical_files": copied_files,
            },
            "validation": {
                "output_rows": output_rows,
                "expected_rows": expected_rows,
                "overlap_rows": overlap_rows,
                "historical": historical_manifest["validation"],
                "current_by_year": current_validation,
            },
            "status": "validated",
        }
        staged_manifest = staging_run / "merged_manifest.json"
        staged_manifest.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        merged_root.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging_hotspots, merged_root)
        shutil.copy2(staged_manifest, merged_manifest_path)
        print(f"Published {output_rows:,} validated merged rows to {merged_root}")
        return 0
    finally:
        connection.close()


def _daily_query(points: str) -> str:
    return f"""
        SELECT acq_date AS activity_date, dataset, platform, source_stage,
            CAST(year(acq_date) AS SMALLINT) AS year, CAST(month(acq_date) AS TINYINT) AS month,
            count(*)::BIGINT AS detection_count, sum(frp_mw)::DOUBLE AS frp_sum_mw,
            avg(frp_mw)::DOUBLE AS frp_mean_mw, max(frp_mw)::DOUBLE AS frp_max_mw,
            count_if(confidence_class = 'low')::BIGINT AS low_confidence_count,
            count_if(confidence_class = 'nominal')::BIGINT AS nominal_confidence_count,
            count_if(confidence_class = 'high')::BIGINT AS high_confidence_count,
            count_if(daynight = 'D')::BIGINT AS day_count,
            count_if(daynight = 'N')::BIGINT AS night_count,
            count_if(hotspot_type = 0)::BIGINT AS vegetation_fire_count,
            count_if(hotspot_type IS NOT NULL AND hotspot_type <> 0)::BIGINT AS other_type_count,
            count_if(hotspot_type IS NULL)::BIGINT AS unclassified_type_count
        FROM {points} GROUP BY ALL
    """


def _monthly_query(points: str) -> str:
    return f"""
        WITH source AS (
            SELECT *, year(acq_date) AS source_year, month(acq_date) AS source_month FROM {points}
        ), coverage AS (
            SELECT dataset, source_year, source_month, min(acq_date) AS dataset_first_date,
                max(acq_date) AS dataset_last_date,
                count(DISTINCT acq_date)::SMALLINT AS dataset_observed_days
            FROM source GROUP BY ALL
        )
        SELECT source.dataset, source.platform, CAST(source.source_year AS SMALLINT) AS year,
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
            CASE WHEN is_complete_month THEN 'complete'
                 WHEN coverage.dataset_first_date > make_date(source.source_year, source.source_month, 1)
                  AND coverage.dataset_last_date < last_day(coverage.dataset_first_date) THEN 'partial_both'
                 WHEN coverage.dataset_first_date > make_date(source.source_year, source.source_month, 1) THEN 'partial_start'
                 ELSE 'partial_end' END AS period_status,
            count(*)::BIGINT AS detection_count,
            count_if(source_stage = 'archive')::BIGINT AS archive_count,
            count_if(source_stage = 'nrt')::BIGINT AS nrt_count,
            sum(frp_mw)::DOUBLE AS frp_sum_mw, avg(frp_mw)::DOUBLE AS frp_mean_mw,
            max(frp_mw)::DOUBLE AS frp_max_mw,
            count_if(confidence_class = 'low')::BIGINT AS low_confidence_count,
            count_if(confidence_class = 'nominal')::BIGINT AS nominal_confidence_count,
            count_if(confidence_class = 'high')::BIGINT AS high_confidence_count,
            count_if(daynight = 'D')::BIGINT AS day_count,
            count_if(daynight = 'N')::BIGINT AS night_count,
            count_if(hotspot_type = 0)::BIGINT AS vegetation_fire_count,
            count_if(hotspot_type IS NOT NULL AND hotspot_type <> 0)::BIGINT AS other_type_count,
            count_if(hotspot_type IS NULL)::BIGINT AS unclassified_type_count
        FROM source JOIN coverage USING (dataset, source_year, source_month)
        GROUP BY source.dataset, source.platform, source.source_year, source.source_month,
            coverage.dataset_first_date, coverage.dataset_last_date, coverage.dataset_observed_days
    """


def aggregate_merged(root: Path, threads: int | None = None) -> int:
    """Build global aggregates one year at a time, then combine tiny fragments."""
    root = root.resolve()
    data_root = root / "data"
    points_root = data_root / "merged" / "hotspots"
    manifest_path = data_root / "catalog" / "merged_manifest.json"
    output_root = data_root / "merged" / "aggregates"
    output_manifest_path = data_root / "catalog" / "merged_aggregates.json"
    point_manifest = _read_manifest(manifest_path, "Merged point")
    if not points_root.exists():
        raise RuntimeError("Validated merged points are required before merged aggregation.")
    if output_root.exists():
        if output_manifest_path.exists():
            previous = json.loads(output_manifest_path.read_text(encoding="utf-8"))
            if previous.get("source_run_id") == point_manifest["run_id"]:
                print("Merged aggregates already match merged points; nothing to do.")
                return 0
        raise RuntimeError("Merged aggregate output exists but does not match its source; it was not overwritten.")

    run_id = "merged-aggregate-" + uuid.uuid4().hex[:8]
    staging_run = data_root / "staging" / run_id
    fragments = staging_run / "fragments"
    staged_output = staging_run / "aggregates"
    fragments.mkdir(parents=True, exist_ok=False)
    staged_output.mkdir(parents=True, exist_ok=False)
    connection = duckdb.connect(str(staging_run / "aggregate.duckdb"))
    try:
        if threads:
            connection.execute(f"SET threads = {int(threads)}")
        connection.execute("SET preserve_insertion_order = false")
        years = sorted(int(path.name.split("=", 1)[1]) for path in points_root.glob("dataset=*/year=*"))
        years = sorted(set(years))
        for index, year in enumerate(years, start=1):
            print(f"[{index}/{len(years)}] Aggregating {year}...", flush=True)
            year_glob = str(points_root / "dataset=*" / f"year={year}" / "**" / "*.parquet")
            points = f"read_parquet({sql_string(year_glob)}, hive_partitioning=true)"
            connection.execute(
                f"COPY ({_daily_query(points)}) TO {sql_string(fragments / f'daily_{year}.parquet')} "
                "(FORMAT PARQUET, COMPRESSION ZSTD)"
            )
            connection.execute(
                f"COPY ({_monthly_query(points)}) TO {sql_string(fragments / f'monthly_{year}.parquet')} "
                "(FORMAT PARQUET, COMPRESSION ZSTD)"
            )

        daily_path = staged_output / "daily_global.parquet"
        monthly_path = staged_output / "monthly_global.parquet"
        connection.execute(
            f"COPY (SELECT * FROM read_parquet({sql_string(str(fragments / 'daily_*.parquet'))}) "
            f"ORDER BY activity_date, dataset, platform, source_stage) TO {sql_string(daily_path)} "
            "(FORMAT PARQUET, COMPRESSION ZSTD)"
        )
        connection.execute(
            f"COPY (SELECT * FROM read_parquet({sql_string(str(fragments / 'monthly_*.parquet'))}) "
            f"ORDER BY dataset, platform, year, month) TO {sql_string(monthly_path)} "
            "(FORMAT PARQUET, COMPRESSION ZSTD)"
        )
        expected = int(point_manifest["output"]["rows"])
        daily_sum = scalar(connection, f"SELECT sum(detection_count) FROM read_parquet({sql_string(daily_path)})")
        monthly_sum = scalar(connection, f"SELECT sum(detection_count) FROM read_parquet({sql_string(monthly_path)})")
        invalid_months = scalar(
            connection,
            f"SELECT count(*) FROM read_parquet({sql_string(monthly_path)}) WHERE "
            "detection_count <> low_confidence_count + nominal_confidence_count + high_confidence_count "
            "OR detection_count <> day_count + night_count",
        )
        if daily_sum != expected or monthly_sum != expected or invalid_months:
            raise RuntimeError(
                f"Merged aggregate validation failed: expected={expected}, daily={daily_sum}, "
                f"monthly={monthly_sum}, invalid_months={invalid_months}."
            )
        coverage = [
            {
                "dataset": str(dataset),
                "platform": str(platform),
                "first_date": first_date.isoformat(),
                "last_date": last_date.isoformat(),
                "detection_count": int(detection_count),
            }
            for dataset, platform, first_date, last_date, detection_count in connection.execute(
                f"SELECT dataset, platform, min(first_observed_date), max(last_observed_date), "
                f"sum(detection_count) FROM read_parquet({sql_string(monthly_path)}) "
                "GROUP BY dataset, platform ORDER BY dataset, platform"
            ).fetchall()
        ]
        manifest = {
            "schema_version": MERGED_AGGREGATE_SCHEMA_VERSION,
            "run_id": run_id,
            "created_utc": utc_now(),
            "source_run_id": point_manifest["run_id"],
            "source_rows": expected,
            "processing": "bounded_year_by_year",
            "outputs": {
                "daily_global": {"file": daily_path.name, "rows": scalar(connection, f"SELECT count(*) FROM read_parquet({sql_string(daily_path)})")},
                "monthly_global": {"file": monthly_path.name, "rows": scalar(connection, f"SELECT count(*) FROM read_parquet({sql_string(monthly_path)})")},
            },
            "validation": {"daily_detection_sum": daily_sum, "monthly_detection_sum": monthly_sum, "invalid_months": invalid_months},
            "coverage": coverage,
            "status": "validated",
        }
        staged_manifest = staging_run / "merged_aggregates.json"
        staged_manifest.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        os.replace(staged_output, output_root)
        shutil.copy2(staged_manifest, output_manifest_path)
        print(f"Published validated merged aggregates for {len(years)} years.")
        return 0
    finally:
        connection.close()


def build_spatial_merged(root: Path, threads: int | None = None, resume: bool = False) -> int:
    """Build resumable H3 products one dataset/month partition at a time."""
    import h3

    root = root.resolve()
    data_root = root / "data"
    points_root = data_root / "merged" / "hotspots"
    point_manifest_path = data_root / "catalog" / "merged_manifest.json"
    output_root = data_root / "merged" / "spatial" / "h3"
    output_manifest_path = data_root / "catalog" / "merged_spatial.json"
    point_manifest = _read_manifest(point_manifest_path, "Merged point")
    if output_root.exists():
        if output_manifest_path.exists():
            previous = json.loads(output_manifest_path.read_text(encoding="utf-8"))
            if previous.get("source_run_id") == point_manifest["run_id"]:
                print("Merged spatial products already match merged points; nothing to do.")
                return 0
        raise RuntimeError("Merged spatial output exists but does not match its source; it was not overwritten.")

    staging_run = data_root / "staging" / ("merged-spatial-" + point_manifest["run_id"])
    fragments = staging_run / "h3"
    if staging_run.exists() and not resume:
        raise RuntimeError(
            f"A merged spatial staging run already exists at {staging_run}; use spatial-merged --resume."
        )
    fragments.mkdir(parents=True, exist_ok=resume)
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
        partitions: list[tuple[str, int, int, Path]] = []
        for dataset_path in sorted(points_root.glob("dataset=*")):
            dataset = dataset_path.name.split("=", 1)[1]
            for year_path in sorted(dataset_path.glob("year=*")):
                year = int(year_path.name.split("=", 1)[1])
                for month_path in sorted(year_path.glob("month=*")):
                    month = int(month_path.name.split("=", 1)[1])
                    partitions.append((dataset, year, month, month_path))

        for index, (dataset, year, month, month_path) in enumerate(partitions, start=1):
            targets = {
                resolution: fragments / f"resolution={resolution}" / f"dataset={dataset}" /
                f"year={year}" / f"month={month}" / "data.parquet"
                for resolution in (4, 5)
            }
            if all(path.exists() for path in targets.values()):
                print(f"[{index}/{len(partitions)}] Reusing {dataset} {year}-{month:02d}...", flush=True)
                continue
            print(f"[{index}/{len(partitions)}] Indexing {dataset} {year}-{month:02d}...", flush=True)
            source_glob = str(month_path / "*.parquet")
            points = f"read_parquet({sql_string(source_glob)}, hive_partitioning=false)"
            connection.execute("DROP TABLE IF EXISTS indexed_month")
            connection.execute(
                f"CREATE TEMP TABLE indexed_month AS SELECT h3_r4(latitude, longitude) AS h3_4, "
                f"h3_r5(latitude, longitude) AS h3_5, acq_date, platform, frp_mw, "
                f"confidence_class, daynight, hotspot_type, source_stage FROM {points}"
            )
            for resolution, h3_column in ((4, "h3_4"), (5, "h3_5")):
                target = targets[resolution]
                if target.exists():
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                partial = target.with_suffix(".parquet.partial")
                query = f"""
                    WITH metrics AS (
                        SELECT {h3_column} AS h3_cell, platform,
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
                        FROM indexed_month GROUP BY {h3_column}, platform
                    )
                    SELECT h3_cell, {resolution}::TINYINT AS resolution,
                        h3_center_lat(h3_cell) AS center_latitude,
                        h3_center_lon(h3_cell) AS center_longitude,
                        h3_area_km2(h3_cell) AS cell_area_km2,
                        detection_count, detection_count / h3_area_km2(h3_cell) AS detections_per_km2,
                        active_days, frp_sum_mw,
                        frp_sum_mw / h3_area_km2(h3_cell) AS frp_mw_per_km2,
                        frp_mean_mw, frp_max_mw, low_confidence_count,
                        nominal_confidence_count, high_confidence_count, day_count, night_count,
                        archive_count, nrt_count, vegetation_fire_count, unclassified_type_count,
                        {sql_string(dataset)} AS dataset, platform, {year}::SMALLINT AS year,
                        {month}::TINYINT AS month FROM metrics
                """
                connection.execute(
                    f"COPY ({query}) TO {sql_string(partial)} (FORMAT PARQUET, COMPRESSION ZSTD)"
                )
                os.replace(partial, target)
            connection.execute("DROP TABLE indexed_month")

        expected = int(point_manifest["output"]["rows"])
        outputs: dict[str, dict[str, int]] = {}
        for resolution in (4, 5):
            output_glob = str(fragments / f"resolution={resolution}" / "**" / "*.parquet")
            detection_sum = scalar(
                connection,
                f"SELECT sum(detection_count) FROM read_parquet({sql_string(output_glob)}, hive_partitioning=true)",
            )
            invalid_rows = scalar(
                connection,
                f"SELECT count(*) FROM read_parquet({sql_string(output_glob)}, hive_partitioning=true) "
                "WHERE h3_cell IS NULL OR cell_area_km2 <= 0 "
                "OR detection_count <> low_confidence_count + nominal_confidence_count + high_confidence_count "
                "OR detection_count <> day_count + night_count "
                "OR detection_count <> archive_count + nrt_count",
            )
            if detection_sum != expected or invalid_rows:
                raise RuntimeError(
                    f"Merged H3 r{resolution} validation failed: expected={expected}, "
                    f"detection_sum={detection_sum}, invalid_rows={invalid_rows}."
                )
            outputs[str(resolution)] = {
                "rows": scalar(connection, f"SELECT count(*) FROM read_parquet({sql_string(output_glob)}, hive_partitioning=true)"),
                "detection_sum": detection_sum,
                "invalid_rows": invalid_rows,
            }
        manifest = {
            "schema_version": 1,
            "run_id": "merged-spatial-" + uuid.uuid4().hex[:8],
            "created_utc": utc_now(),
            "source_run_id": point_manifest["run_id"],
            "source_rows": expected,
            "processing": "bounded_dataset_year_month_with_resume",
            "grid": "H3",
            "resolutions": [4, 5],
            "area_normalization": "Exact spherical H3 cell area in square kilometres",
            "outputs": outputs,
            "status": "validated",
        }
        staged_manifest = staging_run / "merged_spatial.json"
        staged_manifest.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        output_root.parent.mkdir(parents=True, exist_ok=True)
        os.replace(fragments, output_root)
        shutil.copy2(staged_manifest, output_manifest_path)
        print("Published validated merged H3 products.")
        return 0
    finally:
        connection.close()
