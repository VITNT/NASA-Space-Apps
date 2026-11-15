"""Shared vocabulary for sensor harmonization.

A constellation is a bitmask of platforms. Each occupied 0.1° cell-day carries
the mask of platforms that saw it, so the cell-days any subset of platforms
would have reported can be counted from the same table.
"""

from __future__ import annotations

PLATFORM_BITS = {"terra": 1, "aqua": 2, "snpp": 4, "noaa20": 8}

# Marks the rows of the factor table that hold the global per-month fallback.
GLOBAL_REGION = -999

PLATFORM_BIT_SQL = (
    "CASE platform "
    + " ".join(f"WHEN '{name}' THEN {bit}" for name, bit in PLATFORM_BITS.items())
    + " ELSE 0 END"
)

# The filters and grid that define an active cell-day. The calendar endpoint and
# the calibration must agree on these, or the factors do not apply.
CELL_DAY_FILTER = "confidence_class IN ('nominal', 'high') AND (hotspot_type = 0 OR hotspot_type IS NULL)"
CELL_Y_SQL = "least(899, floor(latitude * 10))"
CELL_X_SQL = "CASE WHEN longitude = 180 THEN -1800 ELSE floor(longitude * 10) END"

# A platform contributes to a month only when it observed every day of it.
COMPLETE_PLATFORM_SQL = "is_complete_month AND active_days = calendar_days"


def platform_names(mask: int) -> list[str]:
    return [name for name, bit in PLATFORM_BITS.items() if mask & bit]
