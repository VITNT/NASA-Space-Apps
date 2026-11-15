# NASA FIRMS fire-activity data pipeline

This project converts the downloaded, real NASA FIRMS hotspot exports into a
validated Parquet dataset for DuckDB analytics and Burnline, its browser-based
fire activity explorer. It never generates product observations and never modifies the source CSVs.

## Open Burnline on macOS

From the project directory:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m uvicorn firms_pipeline.api:app --host 127.0.0.1 --port 8765
```

Open [Burnline](http://127.0.0.1:8765/). FastAPI serves the UI, font, imagery,
and data API from the same origin. Select an area or enter bounding coordinates,
pick a month, and select a day, a week, or several days to see their detections.
The map opens on the global archive and uses orange activity dots at wide zoom
levels. Zoom in to see progressively finer square cells for the visible area.
Brighter marks contain more detections. Select one to open its summary.
History accepts a month range.
The app can export the calendar and individual hotspot rows as CSV and the map as PNG.

Burnline opens with a dark basemap in MapLibre GL JS. It uses Natural Earth
land shapes via world-atlas 2.0.2, Natural Earth international borders and
country labels, and provincial and district data prepared from Natural Earth
and geoBoundaries. Labels use Noto Sans glyphs. The bundled geography includes
the upstream source manifest, per-source licenses, and font license.

Choose **NASA satellite** for NASA GIBS Blue Marble imagery with the same fire cells,
borders, and place labels. Both styles support the globe and 2D map. If MapLibre
or the dark basemap cannot load, Burnline uses its bundled Blue Marble map.
Pinch or scroll with two fingers to zoom, or use the on-screen zoom buttons.

### Optional Google 3D satellite basemap

Google Earth Pro is a desktop app, and the [old Google Earth API](https://developers.google.com/earth)
is retired. Burnline can display [Google's 3D satellite view](https://developers.google.com/maps/documentation/javascript/3d/overview)
with the same FIRMS observations, calendar, and map controls.

1. In your Google Cloud project, [enable the Maps JavaScript API and billing](https://developers.google.com/maps/documentation/javascript/get-api-key).
2. Use a browser API key restricted to the Maps JavaScript API and to the
   websites that will serve Burnline. For this local setup, allow
   `http://127.0.0.1:8765/*`. A Cloud project ID or map ID cannot replace the API key.
3. Start or restart Burnline with the browser key in its environment. On macOS
   with zsh, you can enter it without putting it in shell history:

   ```sh
   read -rs 'FIRMS_GOOGLE_MAPS_BROWSER_KEY?Google Maps browser key: '
   export FIRMS_GOOGLE_MAPS_BROWSER_KEY
   .venv/bin/python -m uvicorn firms_pipeline.api:app --host 127.0.0.1 --port 8765
   ```

4. Reload Burnline and select **Google 3D**. Select it again to return to the
   preceding map. PNG export is available on the dark and NASA satellite maps.

The browser can see its restricted key. Keep server keys out of this setting.
Google's [3D coverage varies by location](https://developers.google.com/maps/coverage);
Cambodia is listed without full Maps 3D coverage.
The dark and NASA satellite maps work without a key or a connection to Google.

The API needs validated Parquet. If you have annual Cambodia country exports and
want to run a regional preview while the full products are unavailable, use a
fresh data directory:

```sh
.venv/bin/python -m tools.prepare_cambodia "$HOME/Downloads/NASA DATA" .local/cambodia
FIRMS_PROJECT_ROOT="$PWD/.local/cambodia" .venv/bin/python -m uvicorn firms_pipeline.api:app --host 127.0.0.1 --port 8765
```

The preview uses only available country-years and labels its scope in the UI.
Set `FIRMS_PROJECT_ROOT` to the directory containing the full archive's
`data/` folder to use the global products. To save that location for future
local launches, link the extracted archive once from the project directory:

```sh
mkdir -p .local
ln -s "$HOME/Downloads/burnline-data" .local/archive
```

An explicit `FIRMS_PROJECT_ROOT` takes precedence over the saved local link.

If you downloaded already-merged Parquet (a folder holding `merged/` and
`catalog/`), adopt it without re-running ingestion:

```sh
.venv/bin/python -m tools.adopt_merged "$HOME/Downloads/data"
```

The download is validated and left untouched. Its point files are hard-linked
into `data/merged`, and aggregates and H3 products are built from the months
that are present. A partial download gets its own manifest with the real row
count and is labelled "Partial global" in the UI. Existing `data/processed`
products stay available with `FIRMS_DATA_PROFILE=live`.

The burning calendar counts occupied 0.1° cell-days using nominal and high
confidence detections and excluding classified non-vegetation hotspots.
Each cell counts once per UTC day across selected records. Raw instrument
counts remain visible. This common-grid index reduces repeat counting, but
differences in instrument sensitivity and observation schedules remain.
The historical comparison requires at least two prior complete months with the
same set of contributing platforms.

Earth imagery comes from NASA Earth Observatory's Blue Marble: Next Generation,
produced by Reto Stöckli. Google Sans Flex is bundled with its SIL Open Font License.

## Current inputs

The ingestion command expects exactly these four downloaded exports:

- VIIRS NOAA-20 archive and NRT
- MODIS archive and NRT

The source files remain in their existing `DL_FIRE_*` directories. Archive and
NRT provenance is preserved in every output row. MODIS and NOAA-20 remain
separate datasets, and their raw counts should not be added as if they were one
sensor.

## Run

```powershell
.\.venv\Scripts\python.exe -m firms_pipeline ingest
```

The command:

1. discovers and strictly parses all four CSVs;
2. normalizes compatible fields while preserving sensor/platform provenance;
3. writes Zstandard-compressed Parquet under `data/staging`;
4. validates row counts, coordinates, timestamps, confidence, day/night, and
   hotspot-type values;
5. hashes every source file for provenance; and
6. publishes only validated files to `data/processed/hotspots`.

Published files use Hive partitions:

```text
data/processed/hotspots/
  dataset=modis/year=YYYY/month=MM/*.parquet
  dataset=viirs_noaa20/year=YYYY/month=MM/*.parquet
```

The full manifest is stored at `data/catalog/manifest.json`. A rerun is
idempotent when the current inputs match the published manifest. Existing
published data is never silently overwritten.

## Audit annual historical archives

The long-term downloads may remain as yearly ZIPs containing country CSV
partitions; they do not need to be manually combined or extracted. Validate
their year coverage, every CSV header, duplicate downloads, and (optionally)
all ZIP CRCs with:

```powershell
.\.venv\Scripts\python.exe -m firms_pipeline audit-archives --check-crc
```

The machine-readable result is written to
`data/catalog/annual_archives.json`. Identical repeat downloads are ignored by
content fingerprint so they cannot be counted twice. Keep these ZIPs as the
immutable source archive until converted Parquet data has been validated and
backed up; the deployed API only needs the Parquet products.

Convert the audited archives into a separate, validated historical Parquet
dataset with:

```powershell
.\.venv\Scripts\python.exe -m firms_pipeline ingest-history
```

If a long conversion is interrupted, reuse every completed annual fragment
instead of starting over:

```powershell
.\.venv\Scripts\python.exe -m firms_pipeline ingest-history --resume
```

This deliberately publishes beneath `data/historical`, leaving the existing
current/NRT product untouched. A later merge can give the complete annual
archive priority through 2025 and use the current feed from 2026 onward,
without double-counting their overlapping 2025 observations.

Re-run validation at any time:

```powershell
.\.venv\Scripts\python.exe -m firms_pipeline validate
```

## Build product aggregates

After point ingestion, build the real global daily and monthly product metrics:

```powershell
.\.venv\Scripts\python.exe -m firms_pipeline aggregate
```

This creates `data/aggregates/daily_global.parquet` and
`data/aggregates/monthly_global.parquet`. Every row retains `dataset` and
`platform`; MODIS and VIIRS counts are never silently combined. Metrics include
detection count, FRP sum/mean/max, confidence classes, day/night counts,
archive/NRT provenance, and hotspot-type availability. Monthly rows also expose
platform-specific active days, dataset-wide observed days, and `period_status`,
so the partial November 2025 and November 2026 coverage is explicit rather than
presented as a full month.

## FIRMS API key

The local Parquet pipeline does not require an API key. To run the optional API
connectivity check in `app.py`, provide the key through the environment rather
than source code:

```powershell
$env:FIRMS_MAP_KEY = "your-key"
.\.venv\Scripts\python.exe .\app.py
```

If the previously hard-coded key was shared or committed anywhere, rotate it in
NASA FIRMS.

## Run the read-only API

```powershell
.\.venv\Scripts\python.exe -m uvicorn firms_pipeline.api:app --host 127.0.0.1 --port 8000
```

Interactive OpenAPI documentation is then available at `http://127.0.0.1:8000/docs`.
The demo frontend is available at `http://127.0.0.1:8000/`.

The service provides:

- `GET /health`
- `GET /v1/coverage`
- `GET /v1/coverage/monthly`
- `GET /v1/calendar`
- `GET /v1/activity/monthly`
- `GET /v1/activity/daily`
- `GET /v1/hotspots`
- `GET /v1/summary/bbox`
- `GET /v1/spatial/grid`
- `GET /v1/spatial/monthly`

Point queries require a dataset, date range, and bounding box. They are limited
to 31 days and 5,000 returned observations. Bounding boxes that cross the
antimeridian are supported by supplying `min_lon` greater than `max_lon`.
Longer-range views should use aggregate or summary endpoints.

## Build area-aware spatial aggregates

```powershell
.\.venv\Scripts\python.exe -m firms_pipeline spatial
```

This builds monthly H3 resolution 4 and 5 products under `data/spatial/h3`.
Resolution 4 supports global views; resolution 5 supports regional views. H3 is
not an equal-area grid, so every row stores the exact spherical cell area and
includes area-normalized detection and FRP metrics. Sensor/platform dimensions
remain separate, and each resolution is independently validated back to the
complete point-row count.

The spatial API returns H3 cell identifiers, centers, exact areas, raw metrics,
and area-normalized metrics. Optional bounding boxes filter on cell centers and
support antimeridian wrapping. Responses are capped at 50,000 cells and report
whether additional cells were truncated.

Run integration tests against the real published data:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```
## Merge historical archives with current data

The merged workflow keeps the working API products untouched. Complete annual
archives have priority through 2025; current archive/NRT rows are selected only
from 2026 onward. Every large operation is bounded by year or month.

```powershell
python -m firms_pipeline merge --threads 4
python -m firms_pipeline aggregate-merged --threads 4
python -m firms_pipeline spatial-merged --threads 4
# Resume an interrupted spatial build:
python -m firms_pipeline spatial-merged --threads 4 --resume
```

Validated outputs are written beneath `data/merged`; their manifests are
`data/catalog/merged_manifest.json`, `merged_aggregates.json`, and
`merged_spatial.json`. Once all three exist, the API uses the merged profile by
default. Set `FIRMS_DATA_PROFILE=live` to roll back to the untouched pre-merge
products.

