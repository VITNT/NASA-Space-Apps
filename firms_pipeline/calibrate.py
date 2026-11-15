"""Harmonize the sensitivity of the MODIS and VIIRS platforms.

The burning calendar counts occupied 0.1° cell-days. Platforms differ in what
they can detect, so the count depends on which platforms were flying. This
module measures that dependence where the platforms overlap and publishes
factors that express any month in the cell-days the full constellation would
have reported.

Method, for every 1° region and calendar month:

1. Over the months in which every platform observed every day, count the
   cell-days seen by the full constellation (the target) and by each smaller
   constellation that occurs in the archive (the source).
2. The factor is target / source. Sparse regions are pulled toward the factor
   of their 5° block, and that toward the global factor for the month.
3. The factors are checked on years they were not fitted on before publishing.
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Any

import duckdb

from .pipeline import sql_string, utc_now
from .sensitivity import (
    CELL_DAY_FILTER,
    CELL_X_SQL,
    CELL_Y_SQL,
    COMPLETE_PLATFORM_SQL,
    GLOBAL_REGION,
    PLATFORM_BIT_SQL,
    PLATFORM_BITS,
    platform_names,
)


CALIBRATION_SCHEMA_VERSION = 2
REGION_SIZE_DEGREES = 1.0
BLOCK_SIZE_REGIONS = 5
# Prior strength in source cell-days. A region needs about this many cell-days
# of its own before its ratio outweighs the surrounding block's.
SHRINKAGE_CANDIDATES = (5, 20, 80)
MODIS = PLATFORM_BITS["terra"] | PLATFORM_BITS["aqua"]


def _read_manifest(path: Path, label: str) -> dict[str, Any]:
    if not path.exists():
        raise RuntimeError(f"{label} manifest is missing: {path}")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("status") != "validated":
        raise RuntimeError(f"{label} manifest is not validated: {path}")
    return manifest


def _fit(connection: duckdb.DuckDBPyConnection, table: str, where: str, combos: list[tuple[int, int]], k: float) -> None:
    """Fit factors for (target, source) pairs from the overlap rows selected by `where`."""
    values = ", ".join(f"({target}, {source})" for target, source in combos)
    block = f"floor(ry / {BLOCK_SIZE_REGIONS}), floor(rx / {BLOCK_SIZE_REGIONS})"
    connection.execute(f"""
        CREATE OR REPLACE TEMP TABLE {table} AS
        WITH combos(target, source) AS (VALUES {values}),
        overlap AS (
            SELECT ry, rx, month, mask, sum(n) AS n FROM masks
            WHERE full_overlap AND {where} GROUP BY ALL
        ),
        base AS (
            SELECT u.ry, u.rx, u.month, c.target, c.source,
                coalesce(sum(o.n) FILTER ((o.mask & c.target) != 0), 0) AS a,
                coalesce(sum(o.n) FILTER ((o.mask & c.source) != 0), 0) AS b
            FROM universe u CROSS JOIN combos c
            LEFT JOIN overlap o ON o.ry = u.ry AND o.rx = u.rx AND o.month = u.month
            GROUP BY ALL
        ),
        pooled AS (
            SELECT *,
                sum(a) OVER (PARTITION BY {block}, month, target, source) AS a1,
                sum(b) OVER (PARTITION BY {block}, month, target, source) AS b1,
                sum(a) OVER (PARTITION BY month, target, source) AS a2,
                sum(b) OVER (PARTITION BY month, target, source) AS b2,
                sum(a) OVER (PARTITION BY target, source) AS a3,
                sum(b) OVER (PARTITION BY target, source) AS b3
            FROM base
        ),
        levels AS (
            SELECT *, (a2 + {k} * CASE WHEN b3 > 0 THEN a3 / b3 ELSE 1 END) / (b2 + {k}) AS f2 FROM pooled
        )
        SELECT ry::SMALLINT AS ry, rx::SMALLINT AS rx, month::TINYINT AS month,
            target::TINYINT AS target, source::TINYINT AS source,
            (a + {k} * (a1 + {k} * f2) / (b1 + {k})) / (b + {k}) AS factor,
            b::BIGINT AS source_cell_days
        FROM levels
        UNION ALL
        SELECT {GLOBAL_REGION}, {GLOBAL_REGION}, month, target, source, any_value(f2), any_value(b2)::BIGINT
        FROM levels GROUP BY month, target, source
    """)


def _score(connection: duckdb.DuckDBPyConnection, table: str, where: str, target: int, source: int) -> dict[str, float]:
    """Compare predicted target cell-days with the observed ones on rows not used for fitting."""
    row = connection.execute(f"""
        WITH held AS (
            SELECT year, month, ry, rx,
                sum(n) FILTER ((mask & {target}) != 0) AS actual,
                coalesce(sum(n) FILTER ((mask & {source}) != 0), 0) AS raw
            FROM masks WHERE {where} GROUP BY ALL
        ),
        scored AS (
            SELECT h.*, h.raw * coalesce(f.factor, g.factor) AS predicted
            FROM held h
            LEFT JOIN {table} f ON f.ry = h.ry AND f.rx = h.rx AND f.month = h.month
                AND f.target = {target} AND f.source = {source}
            LEFT JOIN {table} g ON g.ry = {GLOBAL_REGION} AND g.month = h.month
                AND g.target = {target} AND g.source = {source}
            WHERE h.actual > 0
        ),
        monthly AS (
            SELECT year, month, sum(actual) AS actual, sum(raw) AS raw, sum(predicted) AS predicted
            FROM scored GROUP BY ALL
        )
        SELECT
            (SELECT sum(actual) FROM scored),
            (SELECT sum(raw) / sum(actual) FROM scored),
            (SELECT sum(predicted) / sum(actual) FROM scored),
            (SELECT median(abs(raw / actual - 1)) FROM monthly),
            (SELECT median(abs(predicted / actual - 1)) FROM monthly),
            (SELECT max(abs(predicted / actual - 1)) FROM monthly),
            (SELECT sum(abs(raw - actual)) / sum(actual) FROM scored),
            (SELECT sum(abs(predicted - actual)) / sum(actual) FROM scored)
    """).fetchone()
    names = (
        "target_cell_days", "raw_share_of_target", "adjusted_share_of_target",
        "global_month_median_error_raw", "global_month_median_error_adjusted",
        "global_month_worst_error_adjusted", "region_month_error_raw", "region_month_error_adjusted",
    )
    return {name: round(float(value), 4) for name, value in zip(names, row, strict=True)}


def _mean_scores(scores: list[dict[str, float]]) -> dict[str, float]:
    total = sum(score["target_cell_days"] for score in scores)
    result = {
        name: round(sum(score[name] * score["target_cell_days"] for score in scores) / total, 4)
        for name in scores[0] if name not in ("target_cell_days", "global_month_worst_error_adjusted")
    }
    result["global_month_worst_error_adjusted"] = max(score["global_month_worst_error_adjusted"] for score in scores)
    result["target_cell_days"] = int(total)
    return result


def build_calibration(root: Path, threads: int | None = None) -> int:
    """Measure, validate and publish the harmonization factors for the merged archive."""
    root = root.resolve()
    data_root = root / "data"
    points_glob = str(data_root / "merged" / "hotspots" / "**" / "*.parquet")
    monthly_path = data_root / "merged" / "aggregates" / "monthly_global.parquet"
    output_root = data_root / "merged" / "calibration"
    calibration_path = data_root / "catalog" / "calibration.json"
    merged_manifest = _read_manifest(data_root / "catalog" / "merged_manifest.json", "Merged")
    if not monthly_path.exists():
        raise RuntimeError("Monthly aggregates are missing; run aggregate-merged first.")

    if calibration_path.exists() and (output_root / "factors.parquet").exists():
        existing = json.loads(calibration_path.read_text(encoding="utf-8"))
        if (
            existing.get("schema_version") == CALIBRATION_SCHEMA_VERSION
            and existing.get("source_run_id") == merged_manifest["run_id"]
            and existing.get("status") == "validated"
        ):
            print("Calibration already matches the merged data; nothing to do.")
            return 0

    run_id = "calibrate-" + utc_now().replace(":", "").replace("-", "")[:15] + "-" + uuid.uuid4().hex[:8]
    staging = data_root / "staging" / run_id
    staging.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect()
    try:
        if threads:
            connection.execute(f"SET threads = {int(threads)}")
        connection.execute("SET preserve_insertion_order = false")
        connection.execute(f"SET temp_directory = {sql_string(staging / 'tmp')}")

        connection.execute(f"""
            CREATE TEMP TABLE coverage AS
            SELECT year::INTEGER AS year, month::INTEGER AS month,
                coalesce(bit_or({PLATFORM_BIT_SQL}) FILTER ({COMPLETE_PLATFORM_SQL}), 0)::INTEGER AS source,
                bit_or({PLATFORM_BIT_SQL})::INTEGER AS present
            FROM read_parquet({sql_string(monthly_path)}) GROUP BY ALL
        """)
        everything = connection.execute("SELECT bit_or(present) FROM coverage").fetchone()[0]
        years = [row[0] for row in connection.execute("SELECT DISTINCT year FROM coverage ORDER BY 1").fetchall()]
        overlap_years = [row[0] for row in connection.execute(
            "SELECT year FROM coverage WHERE source = ? GROUP BY year ORDER BY year", [everything]).fetchall()]
        if len(overlap_years) < 3:
            raise RuntimeError("Fewer than three years have every platform observing; cannot calibrate.")

        # One pass per year keeps the cell-day grouping within memory.
        connection.execute("CREATE TEMP TABLE masks (year INTEGER, month INTEGER, ry INTEGER, rx INTEGER, mask INTEGER, n BIGINT)")
        for year in years:
            print(f"Counting cell-days by constellation: {year}", flush=True)
            connection.execute(f"""
                INSERT INTO masks
                SELECT {year}, month(acq_date), floor(y / 10), floor(x / 10), mask, count(*)
                FROM (
                    SELECT acq_date, {CELL_Y_SQL} AS y, {CELL_X_SQL} AS x, bit_or({PLATFORM_BIT_SQL})::INTEGER AS mask
                    FROM read_parquet({sql_string(points_glob)}, hive_partitioning=true)
                    WHERE year = {year} AND {CELL_DAY_FILTER}
                    GROUP BY ALL
                )
                GROUP BY ALL
            """)
        connection.execute(f"""
            CREATE TEMP TABLE joined AS
            SELECT m.*, c.source, c.source = {everything} AS full_overlap
            FROM masks m JOIN coverage c USING (year, month)
        """)
        connection.execute("DROP TABLE masks")
        connection.execute("ALTER TABLE joined RENAME TO masks")
        connection.execute("CREATE TEMP TABLE universe AS SELECT DISTINCT ry, rx, month FROM masks")

        targets = {"all": everything, "modis": everything & MODIS}
        for dataset, bit in (("viirs_snpp", PLATFORM_BITS["snpp"]), ("viirs_noaa20", PLATFORM_BITS["noaa20"])):
            if everything & bit:
                targets[dataset] = bit
        occurring = [row[0] for row in connection.execute("SELECT DISTINCT source FROM coverage WHERE source != 0").fetchall()]
        combos = sorted({
            (target, source & target)
            for target in set(targets.values()) for source in occurring
            if source & target and source & target != target
        })

        # Leave one overlap year out at a time, on the two historical constellations
        # that matter most: MODIS alone, and MODIS with Suomi NPP.
        checks = [(everything, source) for source in (MODIS, MODIS | PLATFORM_BITS["snpp"]) if (everything, source) in combos]
        print("Validating on held-out years...", flush=True)
        trials: dict[int, dict[str, list[dict[str, float]]]] = {}
        for k in SHRINKAGE_CANDIDATES:
            trials[k] = {str(source): [] for _, source in checks}
            for year in overlap_years:
                _fit(connection, "trial", f"year != {year}", checks, k)
                for target, source in checks:
                    trials[k][str(source)].append(
                        _score(connection, "trial", f"full_overlap AND year = {year}", target, source))
        summaries = {k: {source: _mean_scores(scores) for source, scores in by_source.items()} for k, by_source in trials.items()}
        best = min(SHRINKAGE_CANDIDATES, key=lambda k: sum(s["region_month_error_adjusted"] for s in summaries[k].values()))

        # Factors fitted on the full overlap, tested on earlier years with a smaller
        # constellation: the same backwards step the MODIS-only era relies on.
        backcast = None
        snpp_era = MODIS | PLATFORM_BITS["snpp"]
        if everything != snpp_era and connection.execute("SELECT count(*) FROM coverage WHERE source = ?", [snpp_era]).fetchone()[0]:
            _fit(connection, "trial", "true", [(snpp_era, MODIS)], best)
            backcast = _score(connection, "trial", f"source = {snpp_era} AND NOT full_overlap", snpp_era, MODIS)
            span = connection.execute("SELECT min(year), max(year) FROM coverage WHERE source = ?", [snpp_era]).fetchone()
            backcast["years"] = f"{span[0]}-{span[1]}"

        _fit(connection, "factors", "true", combos, best)
        if connection.execute("SELECT count(*) FROM factors WHERE factor IS NULL OR factor < 0.999").fetchone()[0]:
            raise RuntimeError("Calibration produced factors below 1 or missing; refusing to publish.")

        annual = connection.execute(f"""
            SELECT m.year, sum(m.n)::BIGINT AS observed,
                round(sum(CASE WHEN (m.mask & m.source) = 0 THEN 0
                    WHEN m.source = {everything} THEN m.n
                    ELSE m.n * coalesce(f.factor, g.factor) END))::BIGINT AS harmonized,
                count(DISTINCT m.month) FILTER (m.source != 0) AS harmonized_months
            FROM masks m
            LEFT JOIN factors f ON f.ry = m.ry AND f.rx = m.rx AND f.month = m.month
                AND f.target = {everything} AND f.source = m.source
            LEFT JOIN factors g ON g.ry = {GLOBAL_REGION} AND g.month = m.month
                AND g.target = {everything} AND g.source = m.source
            GROUP BY m.year ORDER BY m.year
        """).fetchall()

        factors_file = staging / "factors.parquet"
        connection.execute(f"""
            COPY (SELECT ry, rx, month, target, source, factor, source_cell_days FROM factors
                ORDER BY target, source, month, ry, rx)
            TO {sql_string(factors_file)} (FORMAT PARQUET, COMPRESSION ZSTD)
        """)
        masks_file = staging / "region_month_constellations.parquet"
        connection.execute(f"""
            COPY (SELECT year, month, ry, rx, mask, n AS cell_days FROM masks ORDER BY year, month, ry, rx, mask)
            TO {sql_string(masks_file)} (FORMAT PARQUET, COMPRESSION ZSTD)
        """)
        factor_rows = connection.execute("SELECT count(*) FROM factors").fetchone()[0]
        typical = connection.execute(f"""
            SELECT source, round(sum(factor * source_cell_days) / sum(source_cell_days), 3)
            FROM factors WHERE target = {everything} AND ry != {GLOBAL_REGION} AND source_cell_days > 0
            GROUP BY source ORDER BY source
        """).fetchall()
    finally:
        connection.close()

    result = {
        "schema_version": CALIBRATION_SCHEMA_VERSION,
        "run_id": run_id,
        "created_utc": utc_now(),
        "source_run_id": merged_manifest["run_id"],
        "status": "validated",
        "unit": "0.1° active cell-days, nominal and high confidence, vegetation fires",
        "method": (
            "factor = cell-days seen by the target constellation / cell-days seen by the source "
            "constellation, per 1° region and calendar month over the months when every platform "
            "observed every day; shrunk toward the 5° block and the global month."
        ),
        "platform_bits": PLATFORM_BITS,
        "region_size_degrees": REGION_SIZE_DEGREES,
        "overlap_years": f"{overlap_years[0]}-{overlap_years[-1]}",
        "targets": targets,
        "sources": {str(source): platform_names(source) for source in sorted({s for _, s in combos})},
        "shrinkage_cell_days": best,
        "factor_rows": factor_rows,
        "mean_factor_to_full_constellation": {"+".join(platform_names(source)): value for source, value in typical},
        "validation": {
            "leave_one_year_out": {"+".join(platform_names(int(source))): summary for source, summary in summaries[best].items()},
            "shrinkage_trials": {
                str(k): {"+".join(platform_names(int(source))): summary["region_month_error_adjusted"]
                    for source, summary in by_source.items()}
                for k, by_source in summaries.items()
            },
            "backcast_modis_to_modis_snpp": backcast,
        },
        "annual_global_cell_days": [
            {"year": year, "observed": observed, "harmonized": harmonized, "harmonized_months": months}
            for year, observed, harmonized, months in annual
        ],
    }
    output_root.mkdir(parents=True, exist_ok=True)
    os.replace(factors_file, output_root / "factors.parquet")
    os.replace(masks_file, output_root / "region_month_constellations.parquet")
    pending = staging / "calibration.json"
    pending.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    os.replace(pending, calibration_path)
    shutil.rmtree(staging, ignore_errors=True)
    print(f"Published {factor_rows:,} factors to {output_root / 'factors.parquet'}", flush=True)
    print(json.dumps(result["validation"], indent=2), flush=True)
    return 0
