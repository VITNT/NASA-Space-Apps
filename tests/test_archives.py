from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from firms_pipeline.archives import EXPECTED_HEADERS, audit_annual_archives, discover_annual_archives


def write_archive(root: Path, family: str, year: int, suffix: str = "") -> Path:
    path = root / f"{family}_{year}_all_countries{suffix}.zip"
    member = f"{family}/{year}/{family}_{year}_Example.csv"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(member, EXPECTED_HEADERS[family] + "\n")
    return path


class AnnualArchiveTests(unittest.TestCase):
    def test_identical_repeat_download_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = write_archive(root, "viirs-jpss1", 2025)
            duplicate = root / "viirs-jpss1_2025_all_countries (2).zip"
            duplicate.write_bytes(original.read_bytes())

            archives, duplicates = discover_annual_archives(root)

            self.assertEqual(len(archives), 1)
            self.assertEqual(archives[0].path, original.resolve())
            self.assertEqual(len(duplicates), 1)

    def test_audit_reports_missing_expected_years(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_archive(root, "modis", 2025)

            report = audit_annual_archives(root)

            self.assertEqual(report["status"], "invalid")
            self.assertIn(2000, report["coverage"]["modis"]["missing_years"])

    def test_conflicting_same_year_downloads_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_archive(root, "viirs-snpp", 2025)
            conflicting = root / "viirs-snpp_2025_all_countries (1).zip"
            with zipfile.ZipFile(conflicting, "w") as archive:
                archive.writestr(
                    "viirs-snpp/2025/viirs-snpp_2025_Different.csv",
                    EXPECTED_HEADERS["viirs-snpp"] + "\n",
                )

            with self.assertRaisesRegex(RuntimeError, "Conflicting archives"):
                discover_annual_archives(root)


if __name__ == "__main__":
    unittest.main()
