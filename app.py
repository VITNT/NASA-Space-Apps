"""Small FIRMS API connectivity check.

The map key is intentionally read from the environment. The data pipeline itself
does not need a key because it processes already-downloaded FIRMS exports.
"""

from __future__ import annotations

import csv
import io
import os
import sys

import requests


BASE_URL = "https://firms.modaps.eosdis.nasa.gov"


def main() -> int:
    map_key = os.environ.get("FIRMS_MAP_KEY")
    if not map_key:
        print("FIRMS_MAP_KEY is not set.", file=sys.stderr)
        return 2

    session = requests.Session()
    status_response = session.get(
        f"{BASE_URL}/mapserver/mapkey_status/",
        params={"MAP_KEY": map_key},
        timeout=30,
    )
    status_response.raise_for_status()
    status = status_response.json()
    print(f"Map key status: {status.get('status', 'unknown')}")
    print(f"Current transactions: {status.get('current_transactions', 'unknown')}")

    availability_response = session.get(
        f"{BASE_URL}/api/data_availability/csv/{map_key}/all",
        timeout=60,
    )
    availability_response.raise_for_status()
    rows = list(csv.DictReader(io.StringIO(availability_response.text)))
    print(f"Available FIRMS products: {len(rows)}")
    for row in rows:
        print(row)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
