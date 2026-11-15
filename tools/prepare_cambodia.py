"""Build a local Cambodia preview from downloaded FIRMS annual country exports."""
from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile

import duckdb

from firms_pipeline.archives import (
    ARCHIVE_NAME,
    EXPECTED_HEADERS,
    AnnualArchive,
    _annual_select,
    _dataset_platform,
)
from firms_pipeline.pipeline import aggregate, build_spatial, scalar, sql_string, utc_now, validation_queries

COUNTRY_FILE = re.compile(r"(modis|viirs-jpss1|viirs-snpp)_(\d{4})_Cambodia\.csv$")


def country_exports(source: Path, staging: Path) -> dict[tuple[str, int], Path]:
    exports = {}
    for path in sorted(source.rglob("*Cambodia.csv")):
        match = COUNTRY_FILE.fullmatch(path.name)
        if match:
            exports.setdefault((match[1], int(match[2])), path)
    for path in sorted(source.rglob("*.zip")):
        match = ARCHIVE_NAME.fullmatch(path.name)
        if not match:
            continue
        key = match["family"], int(match["year"])
        if key in exports:
            continue
        filename = f"{key[0]}_{key[1]}_Cambodia.csv"
        with ZipFile(path) as archive:
            member = next((item for item in archive.namelist() if item.endswith("/" + filename)), None)
            if member:
                target = staging / filename
                with archive.open(member) as raw, target.open("wb") as output:
                    shutil.copyfileobj(raw, output)
                exports[key] = target
    return exports


def prepare(source: Path, target: Path) -> None:
    destination = target / "data"
    if destination.exists():
        raise SystemExit(f"{destination} already exists. Choose a fresh output directory.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="firms-cambodia-", dir=target) as tmp:
        work = Path(tmp)
        exports = country_exports(source, work)
        if not exports:
            raise SystemExit("No FIRMS Cambodia country CSVs were found.")
        queries = []
        for (family, year), path in sorted(exports.items()):
            with path.open(encoding="utf-8-sig") as stream:
                if stream.readline().strip() != EXPECTED_HEADERS[family]:
                    raise SystemExit(f"Unexpected FIRMS schema in {path.name}")
            dataset, platform = _dataset_platform(family)
            item = AnnualArchive(path, family, dataset, platform, year, 1, path.stat().st_size, "")
            queries.append(_annual_select(item, path))
        data = work / "data"
        points = data / "processed" / "hotspots"
        catalog = data / "catalog"
        points.parent.mkdir(parents=True)
        catalog.mkdir()
        with duckdb.connect() as connection:
            connection.execute(f"""
                COPY ({' UNION ALL '.join(queries)}) TO {sql_string(points)}
                (FORMAT PARQUET, PARTITION_BY (dataset, year, month), COMPRESSION ZSTD)
            """)
            checks = {
                name: scalar(connection, query)
                for name, query in validation_queries(str(points / "**" / "*.parquet")).items()
            }
            if not checks["output_rows"] or any(value for name, value in checks.items() if name != "output_rows"):
                raise RuntimeError(f"Country export validation failed: {checks}")
        manifest = {
            "run_id": "cambodia-preview-" + utc_now(),
            "area_label": "Cambodia",
            "source_files": sorted(path.name for path in exports.values()),
            "validation": checks,
        }
        (catalog / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        aggregate(work, 2)
        build_spatial(work, 2)
        shutil.move(data, destination)
    print(f"Prepared {len(exports)} Cambodia country-years in {destination}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    args = parser.parse_args()
    prepare(args.source.resolve(), args.target.resolve())
