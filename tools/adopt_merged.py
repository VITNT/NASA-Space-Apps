"""Adopt a downloaded merged FIRMS Parquet tree, complete or partial, for the API.

The download is validated in place and never modified. Point files are hard-linked
(copied across volumes) into ``data/merged``; aggregates and H3 products are then
built from the points that are actually present, so every published count
reconciles even when only some dataset-months were downloaded.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import uuid
from pathlib import Path

import duckdb

from firms_pipeline.merge import aggregate_merged, build_spatial_merged
from firms_pipeline.pipeline import scalar, sql_string, utc_now, validation_queries


def _link(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


def adopt(source: Path, root: Path, threads: int | None = None) -> None:
    source_points = source / "merged" / "hotspots"
    source_spatial = source / "merged" / "spatial" / "h3"
    shipped_path = source / "catalog" / "merged_manifest.json"
    data = root / "data"
    catalog = data / "catalog"
    merged = data / "merged"
    if merged.exists() or (catalog / "merged_manifest.json").exists():
        raise SystemExit(f"{merged} or its manifest already exists; nothing was changed.")
    if not shipped_path.exists():
        raise SystemExit(f"Missing {shipped_path}")
    files = sorted(source_points.rglob("*.parquet"))
    if not files:
        raise SystemExit(f"No point Parquet files under {source_points}")
    shipped = json.loads(shipped_path.read_text(encoding="utf-8"))
    points_glob = str(source_points / "**" / "*.parquet")
    points = f"read_parquet({sql_string(points_glob)}, hive_partitioning=true)"

    with duckdb.connect() as connection:
        if threads:
            connection.execute(f"SET threads = {int(threads)}")
        print(f"Validating {len(files)} downloaded point files...", flush=True)
        checks = {name: scalar(connection, query) for name, query in validation_queries(points_glob).items()}
        checks["partition_mismatch"] = scalar(
            connection,
            f"SELECT count(*) FROM {points} WHERE year(acq_date) <> year OR month(acq_date) <> month",
        )
        failures = {name: value for name, value in checks.items() if name != "output_rows" and value}
        if failures or not checks["output_rows"]:
            raise SystemExit(f"Downloaded points failed validation: {checks}")
        partitions = connection.execute(
            f"SELECT dataset, year, month, count(*) FROM {points} GROUP BY ALL ORDER BY ALL"
        ).fetchall()

        rows = checks["output_rows"]
        manifest = dict(shipped)
        if rows != shipped["output"]["rows"]:
            # Never present a subset under the full archive's identity or row count.
            manifest["run_id"] = f"{shipped['run_id']}-partial-{uuid.uuid4().hex[:8]}"
            manifest["created_utc"] = utc_now()
            manifest["area_label"] = "Partial global"
            manifest["output"] = {
                "format": "parquet",
                "compression": "zstd",
                "partitioning": ["dataset", "year", "month"],
                "rows": rows,
            }
            manifest["partial"] = {
                "complete_run_id": shipped["run_id"],
                "complete_rows": shipped["output"]["rows"],
                "present_rows": rows,
                "present_files": len(files),
                "present_dataset_months": len(partitions),
            }
            manifest["validation"] = checks
            print(
                f"Download is partial: {rows:,} of {shipped['output']['rows']:,} rows "
                f"in {len(partitions)} dataset-months.",
                flush=True,
            )

        # Reuse a downloaded H3 month only when it reconciles with the points present.
        staging = data / "staging" / ("merged-spatial-" + manifest["run_id"]) / "h3"
        reused = 0
        for dataset, year, month, count in partitions:
            relative = Path(f"dataset={dataset}") / f"year={year}" / f"month={month}" / "data.parquet"
            for resolution in (4, 5):
                candidate = source_spatial / f"resolution={resolution}" / relative
                if candidate.exists() and count == scalar(
                    connection, f"SELECT sum(detection_count) FROM read_parquet({sql_string(candidate)})"
                ):
                    _link(candidate, staging / f"resolution={resolution}" / relative)
                    reused += 1
        print(f"Reusing {reused} downloaded H3 files that match their points.", flush=True)

    for file in files:
        _link(file, merged / "hotspots" / file.relative_to(source_points))
    catalog.mkdir(parents=True, exist_ok=True)
    (catalog / "merged_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    aggregate_merged(root, threads)
    build_spatial_merged(root, threads, resume=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Folder containing merged/ and catalog/")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="Project root (default: current directory)")
    parser.add_argument("--threads", type=int, default=None, help="DuckDB worker threads")
    args = parser.parse_args()
    adopt(args.source.expanduser().resolve(), args.root.resolve(), args.threads)
