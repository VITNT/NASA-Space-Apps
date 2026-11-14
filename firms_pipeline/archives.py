"""Discovery and validation for annual NASA FIRMS country-partitioned ZIPs."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import tempfile
import uuid
import zipfile
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

import duckdb

from .pipeline import scalar, sql_string, utc_now, validation_queries


ARCHIVE_NAME = re.compile(
    r"^(?P<family>modis|viirs-jpss1|viirs-snpp)_(?P<year>\d{4})_all_countries(?: \(\d+\))?\.zip$"
)
EXPECTED_YEARS = {
    "modis": set(range(2000, 2026)),
    "viirs-snpp": set(range(2012, 2026)),
    "viirs-jpss1": set(range(2018, 2026)),
}
EXPECTED_HEADERS = {
    "modis": (
        "latitude,longitude,brightness,scan,track,acq_date,acq_time,satellite,"
        "instrument,confidence,version,bright_t31,frp,daynight,type"
    ),
    "viirs-snpp": (
        "latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,"
        "instrument,confidence,version,bright_ti5,frp,daynight,type"
    ),
    "viirs-jpss1": (
        "latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,"
        "instrument,confidence,version,bright_ti5,frp,daynight,type"
    ),
}


@dataclass(frozen=True)
class AnnualArchive:
    path: Path
    family: str
    dataset: str
    platform: str
    year: int
    csv_members: int
    uncompressed_bytes: int
    content_fingerprint: str


def _dataset_platform(family: str) -> tuple[str, str]:
    if family == "modis":
        return "modis", "aqua_terra"
    if family == "viirs-snpp":
        return "viirs_snpp", "snpp"
    return "viirs_noaa20", "noaa20"


def _member_fingerprint(members: Iterable[zipfile.ZipInfo]) -> str:
    """Identify duplicate downloads without decompressing multi-gigabyte archives."""
    digest = hashlib.sha256()
    for member in sorted(members, key=lambda item: item.filename):
        digest.update(member.filename.encode("utf-8"))
        digest.update(member.CRC.to_bytes(4, "big"))
        digest.update(member.file_size.to_bytes(8, "big"))
    return digest.hexdigest()


def discover_annual_archives(root: Path) -> tuple[list[AnnualArchive], list[dict]]:
    """Return one canonical archive per family/year plus duplicate notices."""
    candidates: list[AnnualArchive] = []
    for path in sorted(root.rglob("*.zip")):
        match = ARCHIVE_NAME.match(path.name)
        if not match:
            continue
        family = match.group("family")
        year = int(match.group("year"))
        with zipfile.ZipFile(path) as archive:
            csv_members = [
                member for member in archive.infolist()
                if not member.is_dir() and member.filename.lower().endswith(".csv")
            ]
        dataset, platform = _dataset_platform(family)
        candidates.append(
            AnnualArchive(
                path=path.resolve(),
                family=family,
                dataset=dataset,
                platform=platform,
                year=year,
                csv_members=len(csv_members),
                uncompressed_bytes=sum(member.file_size for member in csv_members),
                content_fingerprint=_member_fingerprint(csv_members),
            )
        )

    grouped: dict[tuple[str, int], list[AnnualArchive]] = defaultdict(list)
    for item in candidates:
        grouped[(item.family, item.year)].append(item)

    selected: list[AnnualArchive] = []
    duplicates: list[dict] = []
    for key, items in sorted(grouped.items()):
        fingerprints = {item.content_fingerprint for item in items}
        if len(fingerprints) > 1:
            paths = ", ".join(str(item.path) for item in items)
            raise RuntimeError(f"Conflicting archives for {key[0]} {key[1]}: {paths}")
        canonical = min(items, key=lambda item: (" (" in item.path.stem, len(item.path.name), str(item.path)))
        selected.append(canonical)
        for duplicate in items:
            if duplicate != canonical:
                duplicates.append(
                    {
                        "family": key[0],
                        "year": key[1],
                        "kept": str(canonical.path),
                        "ignored_identical_copy": str(duplicate.path),
                    }
                )
    return selected, duplicates


def audit_annual_archives(root: Path, check_crc: bool = False) -> dict:
    archives, duplicates = discover_annual_archives(root.resolve())
    issues: list[dict] = []
    observed_years: dict[str, set[int]] = defaultdict(set)
    csv_members = 0
    uncompressed_bytes = 0

    for item in archives:
        observed_years[item.family].add(item.year)
        csv_members += item.csv_members
        uncompressed_bytes += item.uncompressed_bytes
        with zipfile.ZipFile(item.path) as archive:
            if check_crc:
                bad_member = archive.testzip()
                if bad_member:
                    issues.append({"archive": str(item.path), "member": bad_member, "problem": "CRC failure"})
            for member in archive.infolist():
                if member.is_dir() or not member.filename.lower().endswith(".csv"):
                    continue
                normalized_name = member.filename.replace("\\", "/")
                if f"/{item.year}/" not in normalized_name:
                    issues.append(
                        {"archive": str(item.path), "member": member.filename, "problem": "year path mismatch"}
                    )
                with archive.open(member) as stream:
                    header = io.TextIOWrapper(stream, encoding="utf-8-sig").readline().rstrip("\r\n")
                if header != EXPECTED_HEADERS[item.family]:
                    issues.append(
                        {"archive": str(item.path), "member": member.filename, "problem": "schema mismatch"}
                    )

    coverage = {}
    for family, expected in EXPECTED_YEARS.items():
        observed = observed_years.get(family, set())
        coverage[family] = {
            "expected": [min(expected), max(expected)],
            "observed": [min(observed), max(observed)] if observed else None,
            "missing_years": sorted(expected - observed),
            "unexpected_years": sorted(observed - expected),
        }
        if expected - observed:
            issues.append({"family": family, "problem": "missing years", "years": sorted(expected - observed)})

    return {
        "status": "valid" if not issues else "invalid",
        "archive_count": len(archives),
        "csv_member_count": csv_members,
        "uncompressed_csv_bytes": uncompressed_bytes,
        "coverage": coverage,
        "duplicates": duplicates,
        "issues": issues,
        "archives": [
            {**asdict(item), "path": str(item.path)}
            for item in archives
        ],
    }


def write_audit_report(root: Path, check_crc: bool = False) -> int:
    report = audit_annual_archives(root, check_crc=check_crc)
    report_path = root.resolve() / "data" / "catalog" / "annual_archives.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "archives"}, indent=2))
    print(f"Audit report: {report_path}")
    return 0 if report["status"] == "valid" else 1


MODIS_COLUMNS = {
    "latitude": "DOUBLE", "longitude": "DOUBLE", "brightness": "DOUBLE",
    "scan": "DOUBLE", "track": "DOUBLE", "acq_date": "DATE", "acq_time": "VARCHAR",
    "satellite": "VARCHAR", "instrument": "VARCHAR", "confidence": "VARCHAR",
    "version": "VARCHAR", "bright_t31": "DOUBLE", "frp": "DOUBLE",
    "daynight": "VARCHAR", "type": "TINYINT",
}
VIIRS_COLUMNS = {
    "latitude": "DOUBLE", "longitude": "DOUBLE", "bright_ti4": "DOUBLE",
    "scan": "DOUBLE", "track": "DOUBLE", "acq_date": "DATE", "acq_time": "VARCHAR",
    "satellite": "VARCHAR", "instrument": "VARCHAR", "confidence": "VARCHAR",
    "version": "VARCHAR", "bright_ti5": "DOUBLE", "frp": "DOUBLE",
    "daynight": "VARCHAR", "type": "TINYINT",
}


def _annual_select(item: AnnualArchive, csv_glob: Path) -> str:
    columns = MODIS_COLUMNS if item.family == "modis" else VIIRS_COLUMNS
    column_sql = "{" + ", ".join(f"'{name}': '{kind}'" for name, kind in columns.items()) + "}"
    source = (
        f"read_csv({sql_string(csv_glob)}, header=true, columns={column_sql}, auto_detect=false, "
        "strict_mode=true, null_padding=false, ignore_errors=false, all_varchar=false)"
    )
    if item.family == "modis":
        brightness_primary = "brightness"
        brightness_secondary = "bright_t31"
        platform = (
            "CASE satellite WHEN 'Aqua' THEN 'aqua' WHEN 'Terra' THEN 'terra' "
            "ELSE lower(satellite) END"
        )
        confidence_numeric = "CAST(confidence AS SMALLINT)"
        confidence_class = (
            "CASE WHEN confidence IS NULL THEN NULL "
            "WHEN CAST(confidence AS SMALLINT) < 30 THEN 'low' "
            "WHEN CAST(confidence AS SMALLINT) < 80 THEN 'nominal' ELSE 'high' END"
        )
        instrument_family = "modis"
    else:
        brightness_primary = "bright_ti4"
        brightness_secondary = "bright_ti5"
        expected_code = "N" if item.family == "viirs-snpp" else "N20"
        platform = (
            f"CASE satellite WHEN {sql_string(expected_code)} THEN {sql_string(item.platform)} "
            "ELSE lower(satellite) END"
        )
        confidence_numeric = "CAST(NULL AS SMALLINT)"
        confidence_class = (
            "CASE lower(confidence) WHEN 'l' THEN 'low' WHEN 'n' THEN 'nominal' "
            "WHEN 'h' THEN 'high' ELSE NULL END"
        )
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
            {brightness_primary} AS brightness_primary_k,
            {brightness_secondary} AS brightness_secondary_k,
            scan AS scan_km,
            track AS track_km,
            frp AS frp_mw,
            upper(daynight) AS daynight,
            confidence AS confidence_raw,
            {confidence_numeric} AS confidence_numeric,
            {confidence_class} AS confidence_class,
            CAST(type AS TINYINT) AS hotspot_type,
            version AS product_version,
            'archive' AS source_stage,
            {sql_string(item.path.name)} AS source_file,
            CAST(year(acq_date) AS SMALLINT) AS year,
            CAST(month(acq_date) AS TINYINT) AS month
        FROM {source}
    """


def _extract_csv_members(item: AnnualArchive, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=False)
    destination_resolved = destination.resolve()
    with zipfile.ZipFile(item.path) as archive:
        for member in archive.infolist():
            if member.is_dir() or not member.filename.lower().endswith(".csv"):
                continue
            target = (destination / member.filename).resolve()
            if destination_resolved not in target.parents:
                raise RuntimeError(f"Unsafe member path in {item.path}: {member.filename}")
            archive.extract(member, destination)
    return destination / "**" / "*.csv"


def ingest_annual_archives(root: Path, threads: int | None = None, resume: bool = False) -> int:
    """Convert annual ZIPs separately; merging with current/NRT data is a later explicit migration."""
    root = root.resolve()
    audit = audit_annual_archives(root, check_crc=False)
    if audit["status"] != "valid":
        raise RuntimeError(f"Annual archive audit failed: {audit['issues']}")
    archives, duplicates = discover_annual_archives(root)
    data_root = root / "data"
    historical_root = data_root / "historical" / "hotspots"
    manifest_path = data_root / "catalog" / "historical_manifest.json"
    if historical_root.exists() or manifest_path.exists():
        raise RuntimeError(
            "Historical output or manifest already exists; it was not overwritten. "
            "Remove it only after deciding whether a rebuild is intended."
        )

    if resume:
        candidates = sorted(
            (path for path in (data_root / "staging").glob("history-*") if (path / "fragments").is_dir()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if not candidates:
            raise RuntimeError("No interrupted historical staging run with fragments was found.")
        staging_run = candidates[0]
        run_id = staging_run.name + "-resumed-" + uuid.uuid4().hex[:8]
    else:
        run_id = "history-" + utc_now().replace(":", "").replace("-", "")[:15] + "-" + uuid.uuid4().hex[:8]
        staging_run = data_root / "staging" / run_id
    fragments_root = staging_run / "fragments"
    staged_hotspots = staging_run / ("hotspots-resume-" + uuid.uuid4().hex[:8] if resume else "hotspots")
    fragments_root.mkdir(parents=True, exist_ok=resume)
    staged_hotspots.mkdir(parents=True, exist_ok=False)
    database_name = "history-resume.duckdb" if resume else "history.duckdb"
    connection = duckdb.connect(str(staging_run / database_name))
    rows_by_archive: dict[str, int] = {}
    coverage_by_archive: dict[str, dict] = {}
    try:
        if threads:
            connection.execute(f"SET threads = {int(threads)}")
        connection.execute("SET preserve_insertion_order = false")
        for index, item in enumerate(archives, start=1):
            fragment = fragments_root / f"{item.family}_{item.year}.parquet"
            if fragment.exists():
                print(f"[{index}/{len(archives)}] Reusing {fragment.name}...", flush=True)
            else:
                print(f"[{index}/{len(archives)}] Converting {item.path.name}...", flush=True)
                extraction = staging_run / "extracted"
                csv_glob = _extract_csv_members(item, extraction)
                try:
                    query = _annual_select(item, csv_glob)
                    temporary_fragment = fragment.with_suffix(".parquet.partial")
                    if temporary_fragment.exists():
                        temporary_fragment.unlink()
                    connection.execute(
                        f"COPY ({query}) TO {sql_string(temporary_fragment)} "
                        "(FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 122880)"
                    )
                    os.replace(temporary_fragment, fragment)
                finally:
                    shutil.rmtree(extraction)
            try:
                connection.execute(
                    f"SELECT 1 FROM read_parquet({sql_string(fragment)}) LIMIT 1"
                )
                stats = connection.execute(
                    f"SELECT count(*), min(acq_date), max(acq_date), "
                    f"count_if(year <> {item.year}), string_agg(DISTINCT platform, ',' ORDER BY platform) "
                    f"FROM read_parquet({sql_string(fragment)})"
                ).fetchone()
                row_count, first_date, last_date, wrong_year, platforms = stats
                if row_count == 0 or wrong_year:
                    raise RuntimeError(
                        f"Invalid annual fragment {item.path.name}: rows={row_count}, wrong_year={wrong_year}"
                    )
                rows_by_archive[item.path.name] = int(row_count)
                coverage_by_archive[item.path.name] = {
                    "first_date": first_date.isoformat(),
                    "last_date": last_date.isoformat(),
                    "platforms": platforms.split(",") if platforms else [],
                }
            except duckdb.Error as exc:
                raise RuntimeError(f"Unreadable fragment {fragment}: {exc}") from exc

        # Repartition one annual fragment at a time. A single global COPY can
        # require tens of gigabytes of spill space because DuckDB must keep all
        # dataset/year/month writers in one operation.
        for index, item in enumerate(archives, start=1):
            fragment = fragments_root / f"{item.family}_{item.year}.parquet"
            print(f"[{index}/{len(archives)}] Partitioning {fragment.name}...", flush=True)
            connection.execute(
                f"COPY (SELECT * FROM read_parquet({sql_string(fragment)})) "
                f"TO {sql_string(staged_hotspots)} "
                "(FORMAT PARQUET, PARTITION_BY (dataset, year, month), COMPRESSION ZSTD, "
                "ROW_GROUP_SIZE 122880, OVERWRITE_OR_IGNORE true)"
            )
        output_glob = str(staged_hotspots / "**" / "*.parquet")
        validation = {
            name: scalar(connection, query)
            for name, query in validation_queries(output_glob).items()
        }
        expected_rows = sum(rows_by_archive.values())
        failures = {
            name: value for name, value in validation.items()
            if (name.startswith("invalid_") or name == "missing_required") and value
        }
        if validation["output_rows"] != expected_rows:
            failures["row_count_difference"] = validation["output_rows"] - expected_rows
        if failures:
            raise RuntimeError(f"Historical validation failed; staged output retained: {failures}")

        manifest = {
            "schema_version": 1,
            "run_id": run_id,
            "created_utc": utc_now(),
            "source_kind": "annual_country_partitioned_zip",
            "archive_count": len(archives),
            "ignored_identical_copies": duplicates,
            "rows_by_archive": rows_by_archive,
            "coverage_by_archive": coverage_by_archive,
            "output": {
                "format": "parquet", "compression": "zstd",
                "partitioning": ["dataset", "year", "month"], "rows": expected_rows,
            },
            "validation": validation,
            "status": "validated",
        }
        staged_manifest = staging_run / "historical_manifest.json"
        staged_manifest.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        historical_root.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staged_hotspots, historical_root)
        shutil.copy2(staged_manifest, manifest_path)
        print(f"Published {expected_rows:,} validated historical rows to {historical_root}")
        return 0
    finally:
        connection.close()
