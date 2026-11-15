# Sensor harmonization

The burning calendar counts occupied 0.1° cell-days. MODIS and VIIRS differ in
what they can detect, so that count depends on which satellites were flying.
Globally the observed index nearly triples when Suomi NPP joins in 2012 and
rises again with NOAA-20 in 2018, with no matching change in fire activity.

`python -m firms_pipeline calibrate` measures the effect where the satellites
overlap and publishes factors that restate any month in the cell-days the full
constellation (Terra, Aqua, Suomi NPP, NOAA-20) would have reported.

## Method

1. Every cell-day records which platforms saw it. From that, the cell-days any
   subset of platforms would have reported can be counted.
2. A platform counts toward a month only if it observed every day of it. The
   set of such platforms is the month's *source* constellation.
3. Over the months when all four platforms observed every day (2018-2025), for
   each 1° region and calendar month:
   `factor = full-constellation cell-days / source-constellation cell-days`.
4. Sparse regions are pulled toward the factor of their 5° block, and that
   toward the global factor for the month. The strength of the pull is chosen
   by the hold-out test below.
5. `harmonized = Σ over regions (source cell-days × factor)`.

Factors exist for every source constellation that occurs in the archive,
including Terra alone (2000-2002), months with an instrument outage, and
MODIS with NOAA-20 after Suomi NPP ends. With a single-sensor filter the
target is that sensor's own platforms, so only Terra-only MODIS months change.

## Results on the global archive

Average factor to the full constellation, weighted by cell-days:

| Source | Factor |
| --- | --- |
| Terra | 10.4 |
| Terra + Aqua | 4.04 |
| Terra + Aqua + Suomi NPP | 1.37 |
| Terra + Aqua + NOAA-20 | 1.35 |

Global cell-days per year:

| Year | Observed | Harmonized |
| --- | --- | --- |
| 2011 | 1.97 M | 8.11 M |
| 2012 | 5.42 M | 7.92 M |
| 2017 | 5.39 M | 7.41 M |
| 2018 | 6.68 M | 7.20 M |
| 2019 | 7.65 M | 7.65 M |

## Validation

Each overlap year is predicted from factors fitted on the other years.

| Source | Share of target, raw | Share, adjusted | Global monthly error | 1° region-month error |
| --- | --- | --- | --- | --- |
| Terra + Aqua | 24% | 98.5% | 3.2% (worst month 25%) | 25% |
| Terra + Aqua + Suomi NPP | 73% | 100.1% | 1.2% (worst month 3.7%) | 6.7% |

Global monthly error is the median over months. Region-month error is the sum
of absolute errors over the sum of observed cell-days.

A second test goes backwards in time, as the MODIS-only era requires: factors
from 2018-2025 predict MODIS + Suomi NPP cell-days from MODIS alone in
2012-2024 months outside the overlap. The adjusted total is 103% of observed,
with a median global monthly error of 3.0% and a region-month error of 21%.

## Limits

- The adjustment is statistical. It is reliable for large areas and whole
  months and noisy for a single 1° region or a single day.
- It assumes the MODIS-to-VIIRS relationship of 2018-2025 held in 2000-2011.
  The backcast supports this to within a few percent back to 2012. Before
  that it cannot be tested.
- Harmonized 2003-2011 totals sit about 14% above 2012-2025. Part may be a real
  decline in burning and part residual bias. Treat long trends with care.
- Fires that MODIS did not detect at all in a region and month cannot be
  recovered by scaling.

## Outputs

- `data/catalog/calibration.json`: method, targets, validation, annual series.
- `data/merged/calibration/factors.parquet`: `ry, rx, month, target, source,
  factor`. Constellations are bitmasks (Terra 1, Aqua 2, Suomi NPP 4,
  NOAA-20 8). `ry = rx = -999` holds the global fallback for the month.
- `data/merged/calibration/region_month_constellations.parquet`: cell-days by
  1° region, month and platform mask, for the whole archive.

Run it after `merge` and `aggregate-merged`. It is a no-op when the merged
data has not changed, and the API ignores factors built from other data.

## API and frontend

- `GET /v1/calendar` adds `harmonized_cell_days` to every row. It is null when
  no platform observed the whole month or no calibration is available.
- `GET /v1/calibration` returns `calibration.json`.
- The same-month comparison uses harmonized values across all prior years.
  Without calibration it falls back to comparing only months with the same
  satellites.
- The History table shows harmonized values, with observed counts on hover.
  The CSV exports contain both.
