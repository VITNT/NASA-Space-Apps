import { createGlobe } from "./globe.js";
import { enhanceSelect } from "./choice-menu.js";
import { mapDetail } from "./map-detail.js";
import { createSatelliteFlight } from "./satellite-flight.js";
import { seasonalMannKendall } from "./trend.js";
import {
  ACTIVITY, ANOMALY, activityClass, activityColor, activityLabel, anomalyClass, drawLegend,
} from "./ramp.js";

const $ = (id) => document.getElementById(id);
const closeMenus = [
  enhanceSelect($("region")),
  enhanceSelect($("dataset")),
  enhanceSelect($("period-month"), { fixedCaption: true, alignEnd: true }),
  enhanceSelect($("period-year"), { fixedCaption: true, alignEnd: true, searchLabel: "Search years" }),
];
const regions = {
  cambodia: [10.35, 14.8, 102.3, 107.75],
  seasia: [-12, 29, 92, 143],
  global: [-90, 90, -180, 180],
  amazon: [-21, 9, -80, -44],
  africa: [-36, 10, 9, 52],
};
const datasets = { modis: "MODIS", viirs_noaa20: "VIIRS NOAA-20", viirs_snpp: "VIIRS Suomi NPP" };
const platformNames = { aqua: "Aqua", terra: "Terra", noaa20: "NOAA-20", snpp: "Suomi NPP" };
// Each instrument's resolution and its two thermal channels, as FIRMS names them.
const instruments = {
  modis: { name: "MODIS", pixel: "1 km", primary: "Brightness (band 21/22)", secondary: "Brightness (band 31)" },
  viirs: { name: "VIIRS", pixel: "375 m", primary: "Brightness (I-4)", secondary: "Brightness (I-5)" },
};
const hotspotTypes = ["Presumed vegetation fire", "Active volcano", "Other static land source", "Offshore"];
const monthFormat = new Intl.DateTimeFormat("en", { month: "long", year: "numeric", timeZone: "UTC" });
const monthLabel = new Intl.DateTimeFormat("en", { month: "long", timeZone: "UTC" });
const formatter = new Intl.NumberFormat("en");
const format = (value) => formatter.format(value);
const dates = (period) => {
  const [year, month] = period.split("-").map(Number);
  const first = new Date(Date.UTC(year, month - 1, 1));
  const days = new Date(Date.UTC(year, month, 0)).getUTCDate();
  return { first, days, start: `${period}-01`, end: `${period}-${days}` };
};
const dayFormat = new Intl.DateTimeFormat("en", { month: "short", day: "numeric", year: "numeric", timeZone: "UTC" });
const weekdayFormat = new Intl.DateTimeFormat("en", { weekday: "short", timeZone: "UTC" });
const isoDay = (period, number) => `${period}-${String(number).padStart(2, "0")}`;
// Monday-based position of the first of the month, used to group days into calendar weeks.
const weekOffset = (first) => (first.getUTCDay() + 6) % 7;
const monthNames = Array.from({ length: 12 }, (_, i) =>
  new Intl.DateTimeFormat("en", { month: "short", timeZone: "UTC" }).format(new Date(Date.UTC(2020, i, 1))),
);

const platformOrder = ["terra", "aqua", "snpp", "noaa20"];
const percent = new Intl.NumberFormat("en", { style: "percent", signDisplay: "exceptZero" });

let coverage = [];
let monthCoverage = new Map();
// UTC date → set of platforms that reported anywhere on Earth that day.
let dayCoverage = new Map();
let historyShade = "activity";
let exportCountRequest;
let exportCount;
// The visible month against its same-month median, for the calendar export.
let monthComparison;
let harmonizing = false;
let areaLabel;
// Selected UTC days of the visible month, sorted. Empty means the whole month.
let selected = [];
let mode = "day";
let rangeAnchor = null;
let calendar = [];
let historyRows = [];
let selectionRequest;
let mapRequest;
let mapTimer;
let mapKey;
const mapCache = new Map();
let historyRequest;
let globalHistoryAvailable = false;
let customBounds = asBounds(regions.cambodia);
let view = "explore";
let selectedPeriod = "2025-02";
let firstPeriod;
let lastPeriod;
let flight;

function asBounds([min_lat, max_lat, min_lon, max_lon]) {
  return { min_lat, max_lat, min_lon, max_lon };
}

function bounds() {
  return $("region").value === "custom" ? customBounds : asBounds(regions[$("region").value]);
}

function areaName() {
  return $("region").selectedOptions[0].textContent;
}

function selectedDatasets() {
  return $("dataset").value ? [$("dataset").value] : [...new Set(coverage.map((row) => row.dataset))];
}

async function api(path, params, signal) {
  const query = params ? `?${new URLSearchParams(params)}` : "";
  const response = await fetch(path + query, { signal });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(typeof body.detail === "string" ? body.detail : `Request failed (${response.status})`);
  }
  return response.json();
}

function status(message, state) {
  $("api-status").textContent = message;
  $("api-status").dataset.state = state;
}

function coordinate(value, negative, positive, digits = 1) {
  return `${Math.abs(value).toFixed(digits)}° ${value < 0 ? negative : positive}`;
}

function showCoordinates(lat, lon) {
  $("map-coordinates").textContent = `${coordinate(lat, "S", "N")} · ${coordinate(lon, "W", "E")}`;
}

let googleGlobe;
let mapLibre;
let flatMap = false;
let satellite = false;
let googleLoad;
let mapLibreStatus = "";
// What the map shows: individual detections, or cells that group them.
let mapLayer = { cells: [] };
// Views narrower than this many degrees are tried as individual detections.
// It shrinks when a view of the current selection holds too many to draw.
let pointLimit = Infinity;
// The detections under the last click, nearest first, and the one on show.
let pointHits = [];
let pointIndex = 0;
const globe = createGlobe($("globe"), showCell, (lat, lon) => {
  if (!googleGlobe && !mapLibre) showCoordinates(lat, lon);
}, () => {
  if (!googleGlobe && !mapLibre) scheduleMap();
}, { onSelectPoints: showPoints });
const activeGlobe = () => googleGlobe ?? mapLibre ?? globe;

function setLayer(layer) {
  const shown = pointHits[pointIndex];
  mapLayer = layer;
  const map = activeGlobe();
  map.setCells(layer.cells ?? [], true);
  map.setPoints?.(layer.points ?? []);
  // Panning reloads the detections. Keep the inspected one marked.
  const same = shown && layer.points?.find((point) => point.latitude === shown.latitude
    && point.longitude === shown.longitude && point.observed_utc === shown.observed_utc && point.platform === shown.platform);
  if (same) {
    pointHits = [same];
    pointIndex = 0;
    map.selectPoint?.(same);
  }
}

function gridLabel(dots, size) {
  return `${dots ? "Activity dots" : `${Number(size.toPrecision(4))}° grid`} · brighter = more detections`;
}

function showMapKey() {
  $("map-key-label").dataset.ramp = !googleGlobe && !mapLayer.points && mapLayer.cells.length ? "cells" : "";
  $("map-key-label").textContent = googleGlobe ? "Pins · grouped detections"
    : mapLayer.points ? "Individual detections · brighter = stronger fire"
    : mapLayer.cells.length ? "Grouped detections · zoom in or pick a day for each hotspot" : "";
}

function centerMaps() {
  const area = bounds();
  globe.center(area);
  mapLibre?.center(area);
  googleGlobe?.center(area);
  scheduleMap();
}

function updateMapView() {
  const google = Boolean(googleGlobe);
  const vectorMap = Boolean(mapLibre) && !google;
  for (const [id, visible] of [["google-globe", google], ["maplibre", vectorMap]]) {
    $(id).inert = !visible;
    $(id).setAttribute("aria-hidden", String(!visible));
  }
  $("globe").hidden = google || vectorMap;
  $("explore").classList.toggle("google-active", google);
  $("explore").classList.toggle("maplibre-active", vectorMap);
  $("basemap").setAttribute("aria-pressed", String(google));
  $("view-globe").setAttribute("aria-pressed", String(!google && !flatMap));
  $("view-flat").setAttribute("aria-pressed", String(!google && flatMap));
  $("style-dark").setAttribute("aria-pressed", String(vectorMap && !satellite));
  $("style-satellite").setAttribute("aria-pressed", String(!google && (satellite || !mapLibre)));
  $("map-attribution").hidden = !vectorMap;
  $("nasa-attribution").hidden = !satellite;
  $("download-visual").disabled = google;
  $("download-visual").title = google ? "Switch to the dark map or NASA satellite to download a map PNG" : "Download map PNG";
  $("download-map").disabled = google;
  $("export-map-note").textContent = google ? "Switch to the dark map or NASA satellite to save the map."
    : "PNG of the current map view.";
  if (vectorMap) mapLibre.resize();
  activeGlobe().reportCenter?.();
  setLayer(mapLayer);
  showMapKey();
  scheduleMap();
}

function showLocalMap(flat) {
  googleLoad = undefined;
  $("basemap-status").textContent = mapLibreStatus;
  googleGlobe?.destroy();
  googleGlobe = undefined;
  flatMap = flat;
  globe.setFlat(flat);
  mapLibre?.setFlat(flat);
  updateMapView();
}

function selectBasemap(useSatellite) {
  satellite = useSatellite;
  mapLibre?.setSatellite(satellite);
  showLocalMap(flatMap);
}

async function initMapLibre() {
  try {
    const { createMapLibreMap } = await import("./maplibre-map.js");
    mapLibre = await createMapLibreMap($("maplibre"), showCell, (lat, lon) => {
      if (!googleGlobe) showCoordinates(lat, lon);
    }, () => {
      if (!googleGlobe) scheduleMap();
    }, showPoints);
    mapLibre.setFlat(flatMap);
    mapLibre.center(bounds());
    $("style-dark").disabled = false;
    $("style-satellite").disabled = false;
    updateMapView();
  } catch {
    mapLibreStatus = "Dark map unavailable. Showing bundled NASA Blue Marble.";
    if (!googleGlobe && !googleLoad) $("basemap-status").textContent = mapLibreStatus;
  }
}

async function initGoogleMap() {
  const configuration = api("/map-config").catch(() => null);
  const button = $("basemap");
  const status = $("basemap-status");
  button.addEventListener("click", async () => {
    status.textContent = "";
    if (googleGlobe) {
      showLocalMap(flatMap);
      return;
    }
    button.disabled = true;
    const load = {};
    googleLoad = load;
    status.textContent = "Loading Google 3D…";
    let map;
    try {
      const config = await configuration;
      if (googleLoad !== load) return;
      if (!config?.googleMapsKey) {
        status.textContent = config
          ? "Google 3D needs a browser key configured on this server."
          : "Could not check Google 3D configuration. Refresh and try again.";
        return;
      }
      const { createGoogleMap } = await import("./google-map.js");
      const initialBounds = bounds();
      map = await createGoogleMap($("google-globe"), config.googleMapsKey, initialBounds, showCell,
        (lat, lon) => { if (googleGlobe === map && map) showCoordinates(lat, lon); },
        () => {
          if (!map || googleGlobe !== map) return;
          showLocalMap(flatMap);
          status.textContent = "Google 3D became unavailable.";
        }, () => { if (googleGlobe === map) scheduleMap(); });
      if (googleLoad !== load) {
        map.destroy();
        return;
      }
      const area = bounds();
      if (Object.keys(area).some((key) => area[key] !== initialBounds[key])) map.center(area);
      googleGlobe = map;
      updateMapView();
      status.textContent = "";
    } catch (error) {
      map?.destroy();
      if (googleLoad === load) {
        status.textContent = error.message.startsWith("Google 3D") || error.message.startsWith("Google Maps") ||
          error.message === "Could not reach Google Maps."
          ? error.message
          : "Google 3D is unavailable. Check the Maps key, billing, or connection.";
      }
    } finally {
      if (googleLoad === load) googleLoad = undefined;
      button.disabled = false;
    }
  });
}

// Shows a title and rows in the inspector. Anything but a detection clears the marked one.
function showDetails(title, values, detection) {
  if (!detection) {
    pointHits = [];
    activeGlobe().selectPoint?.();
  }
  $("selection-title").textContent = title;
  $("selection-details").replaceChildren(...values.map(([label, value]) => {
    const row = document.createElement("div");
    const term = document.createElement("dt");
    const definition = document.createElement("dd");
    term.textContent = label;
    definition.textContent = value;
    row.append(term, definition);
    return row;
  }));
}

function showCell(cell) {
  showDetails("Grouped detections", [
    ["Average location", `${coordinate(cell.latitude, "S", "N", 3)}, ${coordinate(cell.longitude, "W", "E", 3)}`],
    ["Detections", format(cell.detection_count)],
    ["Days with detections", format(cell.observed_days)],
    ["Platforms", cell.platforms.map((platform) => platformNames[platform] ?? platform).join(", ")],
    ["Total FRP", `${format(Math.round(cell.frp_sum_mw))} MW`],
  ]);
  $("inspector").open = true;
}

function showPoints(hits) {
  pointHits = hits;
  showPoint(0);
  $("inspector").open = true;
}

// The full FIRMS record of one detection, as the satellite reported it.
function showPoint(index) {
  pointIndex = index;
  const point = pointHits[index];
  const instrument = instruments[point.dataset === "modis" ? "modis" : "viirs"];
  const value = (number, digits, unit) => number == null ? "—" : `${number.toFixed(digits)} ${unit}`;
  const confidence = point.confidence_class[0].toUpperCase() + point.confidence_class.slice(1)
    + (point.dataset === "modis" && point.confidence_raw != null ? ` (${point.confidence_raw}%)` : "");
  showDetails(pointHits.length > 1 ? `Detection ${index + 1} of ${pointHits.length} here` : "Selected detection", [
    ["Latitude", coordinate(point.latitude, "S", "N", 5)],
    ["Longitude", coordinate(point.longitude, "W", "E", 5)],
    ["Acquired", `${point.observed_utc.slice(0, 10)} ${point.observed_utc.slice(11, 16)} UTC`],
    ["Overpass", point.daynight === "D" ? "Day" : "Night"],
    ["Satellite", platformNames[point.platform] ?? point.platform],
    ["Instrument", `${instrument.name} · ${instrument.pixel}`],
    ["Confidence", confidence],
    ["Fire radiative power", value(point.frp_mw, 1, "MW")],
    [instrument.primary, value(point.brightness_primary_k, 1, "K")],
    [instrument.secondary, value(point.brightness_secondary_k, 1, "K")],
    ["Pixel (scan × track)", point.scan_km == null || point.track_km == null ? "—" : `${point.scan_km} × ${point.track_km} km`],
    ["Type", hotspotTypes[point.hotspot_type] ?? "Not yet classified"],
    ["Version", [point.product_version, point.source_stage].filter(Boolean).join(" · ") || "—"],
  ], true);
  if (pointHits.length > 1) {
    const next = document.createElement("button");
    next.type = "button";
    next.className = "inspector-next";
    next.textContent = "Next detection here ›";
    next.addEventListener("click", () => showPoint((index + 1) % pointHits.length));
    $("selection-details").append(next);
  }
  activeGlobe().selectPoint?.(point);
}

function signatures(period, complete) {
  const datasets = selectedDatasets();
  return (monthCoverage.get(period) ?? []).filter((row) => datasets.includes(row.dataset)
    && (!complete || (row.is_complete_month && row.platform_complete)))
    .map((row) => `${row.dataset}/${row.platform}`).sort().join(",");
}

function level(value, peak) {
  return activityClass(value, peak) ?? "missing";
}

// The platforms of the selected record, in launch order, that ever flew in the archive.
function recordPlatforms() {
  const datasets = selectedDatasets();
  const flown = new Set(coverage.filter((row) => datasets.includes(row.dataset)).map((row) => row.platform));
  return platformOrder.filter((platform) => flown.has(platform));
}

// How each platform of the selected record covered a month: "full" when it
// observed every day, "partial" for some days, absent when it did not fly.
function monthStatus(period) {
  const datasets = selectedDatasets();
  const rows = (monthCoverage.get(period) ?? []).filter((row) => datasets.includes(row.dataset))
    .sort((a, b) => platformOrder.indexOf(a.platform) - platformOrder.indexOf(b.platform));
  return new Map(rows.map((row) => [row.platform, {
    state: row.is_complete_month && row.platform_complete ? "full" : "partial",
    days: row.active_days,
    total: row.calendar_days,
  }]));
}

function coverageSummary(status) {
  if (!status.size) return "none";
  return [...status.values()].every((item) => item.state === "full") ? "full" : "partial";
}

// Which of the month's platforms reported on one UTC day.
function dayStatus(date) {
  const expected = [...monthStatus(date.slice(0, 7)).keys()];
  const reported = dayCoverage.get(date);
  if (!reported) return { reporting: expected, missing: [], known: false };
  return {
    reporting: expected.filter((platform) => reported.has(platform)),
    missing: expected.filter((platform) => !reported.has(platform)),
    known: true,
  };
}

const names = (platforms) => platforms.map((platform) => platformNames[platform] ?? platform);

function median(values) {
  const sorted = [...values].sort((a, b) => a - b);
  return (sorted[Math.floor((sorted.length - 1) / 2)] + sorted[Math.ceil((sorted.length - 1) / 2)]) / 2;
}

// Up to five earlier years of the same month that can be compared with `period`.
// With harmonization any month some platform observed in full is comparable.
// Without it, only months observed in full by the very same satellites are.
function comparablePeriods(period, harmonized) {
  const active = signatures(period, false);
  if (!active || (!harmonized && active !== signatures(period, true))) return [];
  const year = Number(period.slice(0, 4));
  const periods = [];
  for (let y = Math.max(2000, year - 5); y < year; y++) {
    const previous = `${y}-${period.slice(5)}`;
    const complete = signatures(previous, true);
    if (harmonized ? complete : complete === active && signatures(previous, false) === active) periods.push(previous);
  }
  return periods;
}

// Compares a month with the median of its comparable earlier years. `valueOf`
// returns a month's value, 0 for observed months without activity, or null
// when it cannot be used. Needs at least two earlier years.
function sameMonthComparison(period, value, harmonized, valueOf) {
  const values = comparablePeriods(period, harmonized).map(valueOf).filter((item) => item != null);
  if (values.length < 2 || value == null) return null;
  const middle = median(values);
  return { median: middle, years: values.length, delta: middle ? (value - middle) / middle : null };
}

function daySpan(period, from, to) {
  return Array.from({ length: to - from + 1 }, (_, index) => isoDay(period, from + index));
}

// Calendar weeks run Monday to Sunday and stop at the edges of the visible month.
function weekOf(date) {
  const period = date.slice(0, 7);
  const { first, days } = dates(period);
  const number = Number(date.slice(8));
  const monday = number - (number - 1 + weekOffset(first)) % 7;
  return daySpan(period, Math.max(1, monday), Math.min(days, monday + 6));
}

function isRun() {
  return Number(selected.at(-1).slice(8)) - Number(selected[0].slice(8)) === selected.length - 1;
}

function selectionLabel() {
  if (!selected.length) return null;
  const first = new Date(selected[0]);
  if (selected.length === 1) return dayFormat.format(first);
  const month = monthNames[first.getUTCMonth()];
  return isRun()
    ? `${month} ${first.getUTCDate()}–${Number(selected.at(-1).slice(8))}, ${first.getUTCFullYear()}`
    : `${selected.length} days in ${month}`;
}

function selectionSlug() {
  if (!selected.length) return selectedPeriod;
  if (selected.length === 1) return selected[0];
  return isRun() ? `${selected[0]}_to_${selected.at(-1)}` : `${selectedPeriod}-${selected.length}-days`;
}

function drawCalendar() {
  const period = selectedPeriod;
  const { first, days } = dates(period);
  const offset = weekOffset(first);
  $("calendar-month").textContent = selectionLabel() ?? monthFormat.format(first);
  const peak = calendarPeak();
  const fragment = document.createDocumentFragment();
  for (let i = 1; i <= days; i++) {
    const date = isoDay(period, i);
    const button = document.createElement("button");
    const position = i - 1 + offset;
    button.dataset.date = date;
    button.dataset.week = Math.floor(position / 7);
    if (i > 1 && position % 7 === 0) button.className = "week-start";
    const { count, cover } = dayValue(date);
    const step = level(count, peak);
    button.type = "button";
    button.textContent = i;
    button.dataset.level = step;
    if (cover.missing.length && cover.reporting.length) button.dataset.coverage = "partial";
    if (count) {
      const intensity = Math.sqrt(count / peak);
      button.style.setProperty("--bar", `${3 + 19 * intensity}px`);
      button.style.setProperty("--bar-color", ACTIVITY[step - 1].color);
    }
    button.setAttribute("aria-pressed", selected.includes(date));
    const coverageText = !cover.reporting.length ? "no satellite coverage"
      : cover.missing.length ? `partial coverage, no data from ${names(cover.missing).join(", ")}` : "";
    button.setAttribute("aria-label", [
      `${weekdayFormat.format(new Date(date))} ${date}`,
      count == null ? null : `${format(count)} active cells, ${activityLabel(step)}`,
      coverageText,
    ].filter(Boolean).join(", "));
    button.title = button.getAttribute("aria-label");
    button.addEventListener("click", (event) => pickDay(date, event.shiftKey));
    fragment.append(button);
  }
  $("days").replaceChildren(fragment);
  $("whole-month").disabled = !selected.length;
  $("whole-month").hidden = !selected.length;
  drawLegend($("calendar-legend"), { peak, partial: true });
  updateExports();
}

function calendarPeak() {
  return Math.max(1, ...calendar.map((row) => row.active_cell_days));
}

// A day's activity: the count when some satellite of the record reported,
// 0 for an observed day without qualifying detections, null with no coverage.
function dayValue(date) {
  const cover = dayStatus(date);
  const row = calendar.find((item) => item.date === date);
  const count = row ? row.active_cell_days : cover.reporting.length ? 0 : null;
  return { row, count, cover };
}

// Lists the record's satellites and how fully each observed the month.
function drawCoverageLabel(period) {
  const status = monthStatus(period);
  const summary = coverageSummary(status);
  const label = $("coverage-label");
  label.dataset.state = summary;
  const head = document.createElement("span");
  head.className = "coverage-state";
  head.textContent = { full: "Full coverage", partial: "Partial coverage", none: "No coverage" }[summary];
  const items = recordPlatforms().map((platform) => {
    const item = document.createElement("span");
    const entry = status.get(platform);
    item.dataset.state = entry?.state ?? "absent";
    item.textContent = platformNames[platform]
      + (entry?.state === "partial" ? ` ${entry.days}/${entry.total} d` : "");
    item.title = !entry ? `${platformNames[platform]}: no data this month`
      : entry.state === "full" ? `${platformNames[platform]}: observed every day`
      : `${platformNames[platform]}: observed ${entry.days} of ${entry.total} days`;
    return item;
  });
  label.replaceChildren(head, ...items);
  label.setAttribute("aria-label", head.textContent + ". " + items.map((item) => item.title).join(". "));
}

// Harmonized cell-days restate a month as the full satellite set would have
// seen it, so months with different satellites can be compared.
function harmonizedTotal(rows) {
  return rows.reduce((sum, row) => sum + row.harmonized_cell_days, 0);
}

function drawSelection() {
  const rows = selected.length ? calendar.filter((row) => selected.includes(row.date)) : calendar;
  const total = (key) => format(rows.reduce((sum, row) => sum + row[key], 0));
  const sources = (monthCoverage.get(selectedPeriod) ?? []).map((row) => row.dataset);
  const days = selected.length ? selected : Array.from({ length: dates(selectedPeriod).days }, (_, i) => isoDay(selectedPeriod, i + 1));
  const covers = days.map(dayStatus);
  const observed = covers.filter((cover) => cover.reporting.length).length;
  const gaps = covers.filter((cover) => cover.reporting.length && cover.missing.length).length;
  const coverageRow = ["Satellite coverage", !observed ? "None"
    : `${observed} of ${days.length} days` + (gaps ? `, ${gaps} partial` : observed === days.length ? ", all satellites" : "")];
  showDetails(selectionLabel() ?? "Month overview", rows.length ? [
    coverageRow,
    [selected.length === 1 ? "Active cells" : "Active cell-days", total("active_cell_days")],
    ...(rows.every((row) => row.harmonized_cell_days != null)
      && Math.round(harmonizedTotal(rows)) !== rows.reduce((sum, row) => sum + row.active_cell_days, 0)
      ? [["Full-constellation equivalent", format(Math.round(harmonizedTotal(rows)))]] : []),
    ...selectedDatasets().filter((key) => sources.includes(key)).map((key) => [datasets[key] + " detections", total(key)]),
    ...(sources.includes("modis") && sources.some((key) => key !== "modis") && !$("dataset").value
      ? [["MODIS + VIIRS overlaps", total("shared_cell_days")]] : []),
  ] : [coverageRow, ["Activity", observed ? "No qualifying detections" : "No satellite data"]]);
}

function pickDay(date, extend) {
  if (mode === "week") {
    selected = weekOf(date);
  } else if (mode === "days") {
    const next = new Set(selected);
    if (extend && rangeAnchor) {
      const [from, to] = [rangeAnchor, date].map((value) => Number(value.slice(8))).sort((a, b) => a - b);
      daySpan(selectedPeriod, from, to).forEach((value) => next.add(value));
    } else if (!next.delete(date)) {
      next.add(date);
    }
    selected = [...next].sort();
    rangeAnchor = date;
  } else {
    selected = [date];
  }
  applySelection(date);
}

function applySelection(focusDate) {
  const focused = focusDate && $("days").contains(document.activeElement);
  drawCalendar();
  if (focused) $("days").querySelector(`[data-date="${focusDate}"]`)?.focus();
  drawSelection();
  resetMap();
}

function setMode(next) {
  mode = next;
  $("days").dataset.mode = next;
  document.querySelectorAll(".calendar-modes button").forEach((button) => button.setAttribute("aria-pressed", button.dataset.mode === next));
  if (!selected.length) return;
  const before = selected.join();
  if (next === "week") selected = weekOf(selected[0]);
  if (next === "day") selected = [selected[0]];
  rangeAnchor = selected[0];
  if (selected.join() !== before) applySelection();
}

function highlightWeek(week) {
  for (const button of $("days").children) button.classList.toggle("peer", week !== undefined && button.dataset.week === week);
}

function scheduleMap() {
  if (!firstPeriod || view !== "explore") return;
  clearTimeout(mapTimer);
  mapTimer = setTimeout(loadMap, 180);
}

function resetMap() {
  clearTimeout(mapTimer);
  mapRequest?.abort();
  mapCache.clear();
  mapKey = undefined;
  pointLimit = Infinity;
  $("map-message").textContent = "Loading map observations…";
  setLayer({ cells: [] });
  showMapKey();
  loadMap();
}

// The request for the mapped activity in view: the selected area, sensor and
// days, at the level of detail that suits the camera.
function gridQuery(camera) {
  const { dots, size, viewport } = mapDetail(camera);
  const { start, end } = dates(selectedPeriod);
  const params = new URLSearchParams({
    ...bounds(), ...sensorParam(), start_date: selected[0] ?? start, end_date: selected.at(-1) ?? end, cell_size: size,
  });
  for (const bound of viewport ?? []) params.append("viewport", bound);
  if (selected.length && !isRun()) {
    for (const day of selected) params.append("days", day);
  }
  return { dots, size, viewport, params, key: `${dots}:${params}` };
}

// Detections arrive as rows of columns. Stronger fires are drawn brighter.
function pointItems({ columns, rows }) {
  return rows.map((row) => {
    const point = Object.fromEntries(columns.map((name, index) => [name, row[index]]));
    point.level = point.frp_mw >= 50 ? 2 : point.frp_mw >= 10 ? 1 : 0;
    return point;
  });
}

// Cells use the upper half of the activity ramp, so the faintest stay visible
// on the dark and satellite basemaps.
function gridItems(data, dots, size) {
  return data.map((row) => {
    const strength = Math.min(1, Math.log2(row.detection_count) / 9);
    const color = activityColor(.4 + .6 * strength, .82 + .14 * strength);
    const latitude = dots ? row.mean_latitude : row.south + size / 2;
    const longitude = dots ? row.mean_longitude : row.west + size / 2;
    return { ...row, size, strength, color, latitude, longitude };
  });
}

// Flight shows the same activity as the globe, for the same selection.
const flightLayer = {
  title() {
    const month = `${monthNames[Number(selectedPeriod.slice(5)) - 1]} ${selectedPeriod.slice(0, 4)}`;
    return [areaName(), selectionLabel() ?? month, $("dataset").selectedOptions[0].textContent].join(" · ");
  },
  query(camera) {
    const { dots, size, params, key } = gridQuery(camera);
    return {
      key,
      async load(signal) {
        const { data } = await api("/v1/spatial/grid", params, signal);
        return { items: gridItems(data, dots, size), label: gridLabel(dots, size), dots };
      },
    };
  },
  describe(cell) {
    return [
      `${format(cell.detection_count)} detections`,
      `${coordinate(cell.latitude, "S", "N", 2)}, ${coordinate(cell.longitude, "W", "E", 2)}`,
      `${format(cell.observed_days)} ${cell.observed_days === 1 ? "day" : "days"}`,
      cell.platforms.map((platform) => platformNames[platform] ?? platform).join(", "),
      `${format(Math.round(cell.frp_sum_mw))} MW FRP`,
    ].join(" · ");
  },
};

// Shows every detection at its own position when the view holds few enough of
// them, as FIRMS does. Busier views group detections into small cells, each
// marked at the average position of its detections.
async function loadMap() {
  if (view !== "explore") return;
  const camera = activeGlobe().getViewport();
  if (!camera) return;
  const grid = gridQuery(camera);
  const span = Math.max(camera[1] - camera[0], camera[3] - camera[2]);
  const exact = !googleGlobe && span < pointLimit;
  const pointParams = new URLSearchParams(grid.params);
  pointParams.delete("cell_size");
  const key = exact ? `points:${pointParams}` : grid.key;
  if (key === mapKey) return;
  mapKey = key;
  mapRequest?.abort();
  const request = new AbortController();
  mapRequest = request;
  try {
    let layer = mapCache.get(key);
    if (!layer && exact) {
      const result = await api("/v1/spatial/points", pointParams, request.signal);
      if (request.signal.aborted) return;
      if (result.truncated) {
        // Too many to draw one by one. Group them until the view is closer.
        pointLimit = span * .75;
        mapKey = undefined;
        return loadMap();
      }
      layer = { points: pointItems(result) };
    } else if (!layer) {
      const { data } = await api("/v1/spatial/grid", grid.params, request.signal);
      if (request.signal.aborted) return;
      layer = { cells: gridItems(data, true, grid.size) };
    }
    mapCache.delete(key);
    mapCache.set(key, layer);
    if (mapCache.size > 5) mapCache.delete(mapCache.keys().next().value);
    setLayer(layer);
    const count = (layer.points ?? layer.cells).length;
    showMapKey();
    $("map-message").textContent = count ? "" : "No mapped observations in this view";
  } catch (error) {
    if (!request.signal.aborted) {
      mapKey = undefined;
      $("map-message").textContent = error.message;
    }
  }
}

// Earlier months without qualifying activity count as zero, not as missing.
async function loadAnomaly(period, bbox, rows, signal) {
  const month = Number(period.slice(5));
  if (!signatures(period, false)) return;
  const harmonized = harmonizing && rows.every((row) => row.harmonized_cell_days != null);
  const key = harmonized ? "harmonized_cell_days" : "active_cell_days";
  const prior = comparablePeriods(period, harmonized);
  if (prior.length < 2) return;
  const historic = await api("/v1/calendar", {
    ...bbox, ...sensorParam(), interval: "month", month,
    start_date: dates(prior[0]).start, end_date: dates(prior.at(-1)).end,
  }, signal);
  if (signal.aborted) return;
  const byMonth = new Map(historic.data.map((row) => [row.date.slice(0, 7), row]));
  const value = rows.reduce((sum, row) => sum + row[key], 0);
  const result = sameMonthComparison(period, value, harmonized,
    (previous) => byMonth.has(previous) ? byMonth.get(previous)[key] : 0);
  if (!result || result.delta == null) return;
  monthComparison = { period, ...result };
  $("anomaly").textContent = percent.format(result.delta);
  $("anomaly").title = `Median of ${result.years} previous ${monthNames[month - 1]} values: ${format(Math.round(result.median))} cell-days`
    + (harmonized ? ", adjusted for the satellites flying each year" : "");
}

function sensorParam() {
  return $("dataset").value ? { dataset: $("dataset").value } : {};
}

async function loadSelection() {
  selectionRequest?.abort();
  const request = new AbortController();
  selectionRequest = request;
  const period = selectedPeriod;
  $("previous").disabled = !firstPeriod || period <= firstPeriod;
  $("next").disabled = !lastPeriod || period >= lastPeriod;
  const bbox = bounds();
  const { start, end } = dates(period);
  $("area-name").textContent = areaName();
  $("history-area").textContent = areaName() + " · " + $("dataset").selectedOptions[0].textContent;
  selected = [];
  rangeAnchor = null;
  calendar = [];
  monthComparison = undefined;
  drawCoverageLabel(period);
  drawCalendar();
  $("activity").textContent = "—";
  $("anomaly").textContent = "—";
  $("anomaly").removeAttribute("title");
  $("coverage-note").textContent = "Loading calendar…";
  showDetails("Month overview", []);
  if (view === "history") loadHistory();
  resetMap();
  try {
    const [response, days] = await Promise.all([
      api("/v1/calendar", { ...bbox, ...sensorParam(), start_date: start, end_date: end }, request.signal),
      dayCoverage.has(start) ? null : api("/v1/coverage/daily", { start_date: start, end_date: end }, request.signal).catch(() => null),
    ]);
    if (request.signal.aborted) return;
    if (days) {
      for (let i = 1; i <= dates(period).days; i++) dayCoverage.set(isoDay(period, i), new Set());
      for (const row of days.data) dayCoverage.get(row.date)?.add(row.platform);
    }
    calendar = response.data;
    drawCalendar();
    drawSelection();
    const active = signatures(period, false);
    const full = signatures(period, true);
    $("activity").textContent = active ? format(calendar.reduce((sum, row) => sum + row.active_cell_days, 0)) : "—";
    $("coverage-note").textContent = !active ? "No satellite in this record observed this month."
      : active !== full ? (calendar.some((row) => row.harmonized_cell_days != null)
        ? "The comparison uses only satellites that observed the whole month."
        : "Not every satellite observed the whole month, so it is left out of median comparisons.")
      : calendar.length ? "" : "No qualifying detections in this area.";
    await loadAnomaly(period, bbox, calendar, request.signal);
  } catch (error) {
    if (!request.signal.aborted) $("coverage-note").textContent = calendar.length ? "Historical comparison unavailable." : error.message;
  }
}

function selectPeriod(period) {
  if (!firstPeriod || period < firstPeriod || period > lastPeriod) return false;
  selectedPeriod = period;
  const [year, month] = period.split("-");
  const picker = $("period-month");
  if (picker.dataset.year !== year) {
    const options = [];
    for (let index = 0; index < 12; index++) {
      const value = String(index + 1).padStart(2, "0");
      const candidate = `${year}-${value}`;
      if (candidate < firstPeriod || candidate > lastPeriod) continue;
      options.push(new Option(monthLabel.format(new Date(Date.UTC(Number(year), index, 1))), value));
    }
    picker.replaceChildren(...options);
    picker.dataset.year = year;
  }
  picker.value = month;
  $("period-year").value = year;
  return true;
}

function shiftMonth(delta) {
  const { first } = dates(selectedPeriod);
  first.setUTCMonth(first.getUTCMonth() + delta);
  if (selectPeriod(first.toISOString().slice(0, 7))) loadSelection();
}

// Quotes only where a value needs it, so numbers stay numbers in spreadsheets.
function csvDownload(filename, rows, columns = Object.keys(rows[0] ?? {})) {
  if (!rows.length) return;
  const cell = (value) => {
    const text = String(value ?? "");
    return /[",\r\n]/.test(text) ? '"' + text.replaceAll('"', '""') + '"' : text;
  };
  const content = [columns.map(cell).join(","), ...rows.map((row) => columns.map((key) => cell(row[key])).join(","))].join("\r\n");
  download(filename, new Blob([content + "\r\n"], { type: "text/csv;charset=utf-8" }));
}

const slugify = (text) => text.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");

function exportName(kind, span) {
  const record = $("dataset").value ? slugify(datasets[$("dataset").value]) : "all-satellites";
  return `luminite-${slugify(areaName())}-${span}-${record}-${kind}`;
}

// The search every export shares: area, satellite record and bounds.
function searchColumns() {
  const area = bounds();
  return {
    area: areaName(),
    south: area.min_lat, north: area.max_lat, west: area.min_lon, east: area.max_lon,
    satellite_record: $("dataset").selectedOptions[0].textContent,
  };
}

// Per-record activity columns. MODIS and VIIRS counts are qualifying
// detections, the same ones that make up the active cell-days.
function activityColumns(row, covered, adjusted) {
  const value = (number) => covered ? number ?? 0 : "";
  const columns = { active_cell_days: value(row?.active_cell_days) };
  if (harmonizing) columns.adjusted_cell_days = adjusted ?? "";
  for (const key of selectedDatasets()) columns[`${key}_qualifying_detections`] = value(row?.[key]);
  if (!$("dataset").value && selectedDatasets().includes("modis") && selectedDatasets().length > 1) {
    columns.modis_viirs_shared_cell_days = value(row?.shared_cell_days);
  }
  return columns;
}

// One row per day of the selection (or the month), including days without
// activity, with the satellites that did and did not report that day.
function calendarRows() {
  const days = selected.length ? selected : Array.from({ length: dates(selectedPeriod).days }, (_, i) => isoDay(selectedPeriod, i + 1));
  const peak = calendarPeak();
  const search = searchColumns();
  return days.map((date) => {
    const { row, count, cover } = dayValue(date);
    const covered = cover.reporting.length > 0;
    return {
      date,
      weekday: weekdayFormat.format(new Date(date)),
      ...search,
      ...activityColumns(row, covered, row?.harmonized_cell_days),
      activity_class: activityLabel(activityClass(count, peak)),
      month_vs_same_month_median: monthComparison ? percent.format(monthComparison.delta) : "",
      coverage: !covered ? "none" : cover.missing.length ? "partial" : "full",
      satellites_reporting: names(cover.reporting).join("; "),
      satellites_missing: names(cover.missing).join("; "),
      coverage_source: cover.known ? "daily" : "monthly",
    };
  });
}

// One row per month of the History range, with the same-month comparison and
// the satellites that observed each month in full or in part.
function historyCsvRows() {
  const from = $("history-start").value;
  const to = $("history-end").value;
  const { values, peak, comparison } = historyModel(historyRows, from, to);
  const search = searchColumns();
  const rows = [];
  for (let period = from; period <= to; period = nextPeriod(period)) {
    const entry = values.get(period);
    const status = monthStatus(period);
    const result = comparison(period);
    const platforms = [...status.entries()];
    rows.push({
      month: period,
      ...search,
      ...activityColumns(entry?.row, status.size > 0, entry?.row?.harmonized_cell_days),
      activity_class: activityLabel(activityClass(entry?.value, peak)),
      same_month_median: result ? Math.round(result.median) : "",
      vs_same_month_median: result?.delta == null ? "" : percent.format(result.delta),
      median_years: result?.years ?? "",
      coverage: coverageSummary(status),
      satellites_full_month: names(platforms.filter(([, item]) => item.state === "full").map(([key]) => key)).join("; "),
      satellites_part_month: platforms.filter(([, item]) => item.state === "partial")
        .map(([key, item]) => `${platformNames[key]} (${item.days}/${item.total} days)`).join("; "),
    });
  }
  return rows;
}

function nextPeriod(period) {
  const { first } = dates(period);
  first.setUTCMonth(first.getUTCMonth() + 1);
  return first.toISOString().slice(0, 7);
}

// The hotspot export's query: the same area, days and record as the map.
function exportParams() {
  const { start, end } = dates(selectedPeriod);
  const params = new URLSearchParams({
    ...bounds(), ...sensorParam(), start_date: selected[0] ?? start, end_date: selected.at(-1) ?? end,
  });
  if (selected.length && !isRun()) for (const day of selected) params.append("days", day);
  return params;
}

function exportScope() {
  const month = `${monthNames[Number(selectedPeriod.slice(5)) - 1]} ${selectedPeriod.slice(0, 4)}`;
  return [areaName(), selectionLabel() ?? month, $("dataset").selectedOptions[0].textContent].join(" · ");
}

// Refreshes the Download menu for the current search. The hotspot count is
// only fetched while the menu is open.
function updateExports() {
  if (!$("export-scope")) return;
  $("export-scope").textContent = exportScope();
  $("download-calendar").disabled = !firstPeriod || !calendarRows().some((row) => row.coverage !== "none");
  exportCount = undefined;
  exportCountRequest?.abort();
  const note = $("export-points-note");
  $("download-points").disabled = true;
  note.textContent = "Every detection in this search, one row each.";
  if (!$("export-menu").open || !firstPeriod) return;
  const request = new AbortController();
  exportCountRequest = request;
  note.textContent = "Counting detections…";
  api("/v1/export/hotspots/count", exportParams(), request.signal).then((result) => {
    if (request.signal.aborted) return;
    exportCount = result;
    const over = result.count > result.limit;
    $("download-points").disabled = !result.count || over;
    note.textContent = !result.count ? "No detections in this search."
      : over ? `${format(result.count)} detections. The limit is ${format(result.limit)}. Narrow the area or pick fewer days.`
      : `${format(result.count)} detections, ${format(result.qualifying)} counted in the calendar. Every column FIRMS reports.`;
  }).catch((error) => {
    if (!request.signal.aborted) note.textContent = error.message;
  });
}

function downloadPoints() {
  if (!exportCount?.count || exportCount.count > exportCount.limit) return;
  const link = document.createElement("a");
  link.href = "/v1/export/hotspots.csv?" + exportParams();
  link.download = exportName("hotspots", selectionSlug()) + ".csv";
  link.click();
  $("export-points-note").textContent = `Downloading ${format(exportCount.count)} detections…`;
}

function download(filename, blob) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

async function loadHistory() {
  historyRequest?.abort();
  const request = new AbortController();
  historyRequest = request;
  const from = $("history-start").value;
  const to = $("history-end").value;
  const table = $("history-table");
  const note = $("history-note");
  table.replaceChildren();
  $("history-trend").hidden = true;
  $("download-history").disabled = true;
  historyRows = [];
  if (!from || !to || from > to) {
    note.textContent = "Choose a valid month range.";
    return;
  }
  const bbox = bounds();
  const params = sensorParam();
  note.textContent = "Loading monthly activity…";
  try {
    const firstYear = Number(from.slice(0, 4));
    const lastYear = Number(to.slice(0, 4));
    const rows = [];
    const isGlobal = bbox.min_lat === -90 && bbox.max_lat === 90 && bbox.min_lon === -180 && bbox.max_lon === 180;
    if (globalHistoryAvailable && isGlobal) {
      const result = await api("/v1/calendar", {
        ...bbox, ...params, start_date: dates(from).start, end_date: dates(to).end, interval: "month",
      }, request.signal);
      rows.push(...result.data);
    } else {
      for (let year = lastYear; year >= firstYear; year--) {
        if (!monthNames.some((_, index) => {
          const period = `${year}-${String(index + 1).padStart(2, "0")}`;
          return period >= from && period <= to && signatures(period, false);
        })) continue;
        note.textContent = `Loading ${year}…${rows.length ? " Recent years are ready to explore." : ""}`;
        const result = await api("/v1/calendar", {
          ...bbox, ...params,
          start_date: year === firstYear ? dates(from).start : `${year}-01-01`,
          end_date: year === lastYear ? dates(to).end : `${year}-12-31`,
          interval: "month",
        }, request.signal);
        if (request.signal.aborted) return;
        rows.push(...result.data);
        if (year !== firstYear) renderHistory(table, rows, from, to, year, lastYear);
      }
    }
    if (request.signal.aborted) return;
    historyRows = rows.sort((a, b) => a.date.localeCompare(b.date));
    const observed = historyModel(rows, from, to).values.size;
    $("download-history").disabled = !observed;
    note.textContent = !observed ? "No satellite in this record observed this range."
      : "Select a month to open it in Explore. Hatched months had no satellite coverage. " + (harmonizing
        ? "Adjusted estimates are shown where available. Hover for coverage and observed counts."
        : "Ticks under each value show which satellites observed that month.");
    renderHistory(table, rows, from, to, firstYear, lastYear);
    drawTrend(rows, from, to);
  } catch (error) {
    if (!request.signal.aborted) note.textContent = `History could not finish loading. ${error.message} Try applying the range again.`;
  }
}

// The values History shades by: the adjusted count where available, else the
// observed one, 0 for months observed without activity, absent with no coverage.
function historyModel(rows, from, to) {
  const byMonth = new Map(rows.map((row) => [row.date.slice(0, 7), row]));
  const values = new Map();
  for (let period = from; period && period <= to; period = nextPeriod(period)) {
    if (!signatures(period, false)) continue;
    const row = byMonth.get(period);
    const adjusted = row?.harmonized_cell_days;
    values.set(period, {
      row,
      observed: row?.active_cell_days ?? 0,
      value: adjusted == null ? row?.active_cell_days ?? 0 : Math.round(adjusted),
      harmonized: harmonizing && (row ? adjusted != null : true),
    });
  }
  const peak = Math.max(1, ...[...values.values()].map((entry) => entry.value));
  // Baselines can only use earlier years inside the loaded range.
  const comparison = (period) => {
    const entry = values.get(period);
    if (!entry) return null;
    const key = entry.harmonized ? "harmonized_cell_days" : "active_cell_days";
    return sameMonthComparison(period, entry.harmonized ? entry.row?.harmonized_cell_days ?? 0 : entry.observed, entry.harmonized,
      (previous) => {
        if (previous < from) return null;
        const row = byMonth.get(previous);
        return row ? row[key] : 0;
      });
  };
  return { values, peak, comparison };
}

function renderHistory(table, rows, from, to, firstYear, lastYear) {
  const focused = table.contains(document.activeElement) && document.activeElement.dataset.period;
  table.replaceChildren();
  // While years are still loading, only the loaded ones count toward the peak and baselines.
  const loaded = `${firstYear}-01` > from ? `${firstYear}-01` : from;
  const { values, peak, comparison } = historyModel(rows, loaded, to);
  const anomaly = historyShade === "anomaly";
  const platforms = recordPlatforms();
  drawLegend($("history-legend"), { peak, anomaly });
  $("history-ticks").replaceChildren(...platforms.map((platform) => {
    const item = document.createElement("span");
    item.textContent = platformNames[platform];
    return item;
  }));
  const header = table.createTHead().insertRow();
  for (const name of ["Year", ...monthNames]) {
    const cell = document.createElement("th");
    cell.scope = "col";
    cell.textContent = name;
    header.append(cell);
  }
  const body = table.createTBody();
  for (let year = lastYear; year >= firstYear; year--) {
    const row = body.insertRow();
    const yearCell = document.createElement("th");
    yearCell.scope = "row";
    yearCell.textContent = year;
    row.append(yearCell);
    monthNames.forEach((name, i) => {
      const period = `${year}-${String(i + 1).padStart(2, "0")}`;
      const cell = row.insertCell();
      if (period < from || period > to) return;
      const entry = values.get(period);
      const count = entry?.value;
      const result = entry ? comparison(period) : null;
      const button = document.createElement("button");
      button.type = "button";
      button.className = "history-cell";
      button.dataset.period = period;
      const step = anomaly ? anomalyClass(result?.delta) : activityClass(count, peak);
      const ramp = anomaly ? ANOMALY : ACTIVITY;
      button.dataset.level = step == null ? "missing" : step;
      if (step) {
        button.style.setProperty("--fill", ramp[step - 1].color);
        button.dataset.ink = ramp[step - 1].ink;
      }
      const value = document.createElement("span");
      value.textContent = count == null ? "—"
        : anomaly ? (result?.delta == null ? "n/a" : percent.format(result.delta)) : format(count);
      // One tick per satellite of the record: solid for the whole month, hollow for part of it.
      const status = monthStatus(period);
      const ticks = document.createElement("span");
      ticks.className = "coverage-ticks";
      ticks.setAttribute("aria-hidden", "true");
      ticks.append(...platforms.map((platform) => {
        const tick = document.createElement("i");
        tick.dataset.state = status.get(platform)?.state ?? "absent";
        return tick;
      }));
      button.append(value, ticks);
      const coverageText = !status.size ? "no satellite coverage" : [...status.entries()].map(([platform, item]) =>
        `${platformNames[platform]} ${item.state === "full" ? "all month" : `${item.days} of ${item.total} days`}`).join(", ");
      const comparisonText = result?.delta == null ? "no same-month baseline"
        : `${percent.format(result.delta)} vs median of ${result.years} earlier years (${format(Math.round(result.median))})`;
      const adjusted = entry && entry.value !== entry.observed;
      button.setAttribute("aria-label", [
        `${name} ${year}`,
        count == null ? null : `${format(count)} active cell-days, ${activityLabel(activityClass(count, peak))}`,
        count == null ? null : comparisonText,
        adjusted ? `observed ${format(entry.observed)}` : null,
        `coverage: ${coverageText}`,
      ].filter(Boolean).join("; "));
      button.title = button.getAttribute("aria-label");
      button.addEventListener("click", () => {
        if (!selectPeriod(period)) return;
        setView("explore");
        loadSelection();
      });
      cell.append(button);
    });
  }
  if (focused) table.querySelector(`[data-period="${focused}"]`)?.focus({ preventScroll: true });
}

// Tests the adjusted monthly series for a monotonic trend. Months that no
// platform observed in full are left out; observed months without fire count as zero.
function drawTrend(rows, from, to) {
  const output = $("history-trend");
  if (!harmonizing) return;
  const values = new Map(rows.map((row) => [row.date.slice(0, 7), row.harmonized_cell_days]));
  const points = [];
  for (let year = Number(from.slice(0, 4)); year <= Number(to.slice(0, 4)); year++) {
    for (let month = 1; month <= 12; month++) {
      const period = `${year}-${String(month).padStart(2, "0")}`;
      if (period < from || period > to || !signatures(period, true)) continue;
      const value = values.has(period) ? values.get(period) : 0;
      if (value != null) points.push({ year, month, value });
    }
  }
  const years = new Set(points.map((point) => point.year)).size;
  const result = years >= 5 ? seasonalMannKendall(points) : null;
  output.hidden = false;
  if (!result) {
    output.textContent = "Trend test: choose a range of at least five years with fire activity.";
    return;
  }
  const mean = points.reduce((sum, point) => sum + point.value, 0) / points.length;
  const rate = new Intl.NumberFormat("en", { style: "percent", signDisplay: "exceptZero", maximumFractionDigits: 1 }).format(result.slope / mean);
  const p = result.p < .001 ? "p < 0.001" : "p = " + result.p.toFixed(3);
  const significant = result.p < .05;
  const headline = significant
    ? `${result.s > 0 ? "Increasing" : "Decreasing"} trend, ${rate} per year`
    : "No significant trend";
  const detail = document.createElement("small");
  detail.textContent = `Seasonal Mann-Kendall test on ${points.length} adjusted months, ${points[0].year}–${points.at(-1).year}: `
    + `τ = ${result.tau.toFixed(2)}, Z = ${result.z.toFixed(2)}, ${p}`
    + (significant ? "" : `, Sen slope ${rate} per year`)
    + ". Trends that cross 2012 or 2018 rely on the satellite adjustment.";
  output.replaceChildren(headline, detail);
}

function setView(next) {
  const leavingFlight = view === "flight" && next !== "flight";
  const returnFocus = leavingFlight && $("flight").contains(document.activeElement);
  if (leavingFlight) flight.stop();
  if (view === "history" && next !== "history") historyRequest?.abort();
  if (view === "explore" && next !== "explore") {
    clearTimeout(mapTimer);
    mapRequest?.abort();
    mapKey = undefined;
  }
  view = next;
  closeMenus.forEach((close) => close?.());
  $("explore").hidden = next !== "explore";
  $("history").hidden = next !== "history";
  $("flight").hidden = next !== "flight";
  document.querySelectorAll(".topbar [data-view]").forEach((button) => button.setAttribute("aria-pressed", button.dataset.view === next));
  $("export-menu").open = false;
  if (next === "history" && firstPeriod) loadHistory();
  else if (next === "flight") {
    if (!flight) {
      flight = createSatelliteFlight($("flight"), flightLayer, () => setView("explore"));
    }
    flight.start();
  } else if (next === "explore") {
    mapLibre?.resize();
    scheduleMap();
  }
  if (returnFocus) $("take-orbit").focus({ preventScroll: true });
}

async function init() {
  $("download-points").disabled = true;
  $("download-calendar").disabled = true;
  $("download-history").disabled = true;
  $("whole-month").disabled = true;
  try {
    const [health, page, months] = await Promise.all([api("/health"), api("/v1/coverage"), api("/v1/coverage/monthly")]);
    coverage = page.data;
    for (const row of months.data) {
      const period = row.month_start.slice(0, 7);
      if (!monthCoverage.has(period)) monthCoverage.set(period, []);
      monthCoverage.get(period).push(row);
    }
    if (!coverage.length) throw new Error("The archive contains no usable coverage.");
    areaLabel = health.area_label;
    harmonizing = health.calibration_available;
    globalHistoryAvailable = health.global_history_available;
    status(areaLabel ? areaLabel + " archive" : "Global FIRMS archive", "ready");
    for (const [key, name] of Object.entries(datasets)) {
      if (coverage.some((row) => row.dataset === key)) $("dataset").add(new Option(name, key));
    }
    const firstDay = coverage.map((row) => row.first_date).sort()[0];
    const latestDay = coverage.map((row) => row.last_date).sort().at(-1);
    const first = firstDay.slice(0, 7);
    const latest = latestDay.slice(0, 7);
    for (const id of ["history-start", "history-end"]) {
      $(id).min = first;
      $(id).max = latest;
    }
    firstPeriod = first;
    lastPeriod = latest;
    const yearOptions = document.createDocumentFragment();
    for (let year = Number(latest.slice(0, 4)); year >= Number(first.slice(0, 4)); year--) {
      yearOptions.append(new Option(String(year)));
    }
    $("period-year").replaceChildren(yearOptions);
    if (areaLabel?.toLowerCase().includes("cambodia")) $("region").value = "cambodia";
    const platforms = new Set(coverage.map((row) => row.platform)).size;
    const latestComplete = [...monthCoverage.keys()].sort().reverse().find((period) =>
      (monthCoverage.get(period) ?? []).filter((row) => row.is_complete_month && row.platform_complete).length === platforms);
    selectPeriod(latestComplete ?? latest);
    $("period-month").disabled = false;
    $("period-year").disabled = false;
    $("history-start").value = first;
    $("history-end").value = latest;
    centerMaps();
    loadSelection();
  } catch (error) {
    status("Archive unavailable", "error");
    $("coverage-note").textContent = error.message;
    $("map-message").textContent = "Connect a validated FIRMS archive to view observations.";
  }
}

$("region").addEventListener("change", () => {
  $("bounds").hidden = $("region").value !== "custom";
  centerMaps();
  loadSelection();
});
$("bounds").addEventListener("submit", (event) => {
  event.preventDefault();
  const data = Object.fromEntries([...new FormData(event.target)].map(([key, value]) => [key, Number(value)]));
  if (data.max_lat < data.min_lat) {
    $("coverage-note").textContent = "North latitude must be greater than or equal to south latitude.";
    return;
  }
  customBounds = data;
  centerMaps();
  loadSelection();
});
$("dataset").addEventListener("change", loadSelection);
$("period-month").addEventListener("change", () => {
  if (selectPeriod(`${$("period-year").value}-${$("period-month").value}`)) loadSelection();
});
$("period-year").addEventListener("change", () => {
  const period = `${$("period-year").value}-${selectedPeriod.slice(5)}`;
  const next = period < firstPeriod ? firstPeriod : period > lastPeriod ? lastPeriod : period;
  if (selectPeriod(next)) loadSelection();
});
$("previous").addEventListener("click", () => shiftMonth(-1));
$("next").addEventListener("click", () => shiftMonth(1));
$("whole-month").addEventListener("click", () => {
  selected = [];
  rangeAnchor = null;
  applySelection();
  $("days").querySelector("button")?.focus();
});
document.querySelectorAll(".calendar-modes button").forEach((button) => button.addEventListener("click", () => setMode(button.dataset.mode)));
for (const type of ["pointerover", "focusin"]) {
  $("days").addEventListener(type, (event) => highlightWeek(mode === "week" ? event.target.dataset?.week : undefined));
}
for (const type of ["pointerleave", "focusout"]) $("days").addEventListener(type, () => highlightWeek());
$("view-globe").addEventListener("click", () => showLocalMap(false));
$("view-flat").addEventListener("click", () => showLocalMap(true));
$("style-dark").addEventListener("click", () => selectBasemap(false));
$("style-satellite").addEventListener("click", () => selectBasemap(true));
$("zoom-in").addEventListener("click", () => activeGlobe().changeZoom(1.35));
$("zoom-out").addEventListener("click", () => activeGlobe().changeZoom(1 / 1.35));
$("recenter").addEventListener("click", centerMaps);
$("history-form").addEventListener("submit", (event) => { event.preventDefault(); loadHistory(); });
$("download-calendar").addEventListener("click", () =>
  csvDownload(exportName("calendar", selectionSlug()) + ".csv", calendarRows()));
$("download-points").addEventListener("click", downloadPoints);
$("download-history").addEventListener("click", () => csvDownload(
  exportName("monthly-history", `${$("history-start").value}_to_${$("history-end").value}`) + ".csv", historyCsvRows()));
function downloadMap() {
  const filename = exportName("map", selectionSlug()) + ".png";
  activeGlobe().exportImage?.((blob) => {
    if (blob) download(filename, blob);
  });
}
$("download-visual").addEventListener("click", downloadMap);
$("download-map").addEventListener("click", downloadMap);
document.querySelectorAll(".history-shade button").forEach((button) => button.addEventListener("click", () => {
  historyShade = button.dataset.shade;
  document.querySelectorAll(".history-shade button").forEach((item) => item.setAttribute("aria-pressed", item === button));
  const from = $("history-start").value;
  const to = $("history-end").value;
  if (historyRows.length || from) renderHistory($("history-table"), historyRows, from, to, Number(from.slice(0, 4)), Number(to.slice(0, 4)));
}));
// The two top menus close each other, on Escape and on a click elsewhere.
const menus = [document.querySelector(".location .filters:not(.export-menu)"), $("export-menu")];
for (const menu of menus) {
  menu.addEventListener("toggle", () => {
    if (menu.open) menus.filter((other) => other !== menu).forEach((other) => { other.open = false; });
    if (menu === $("export-menu")) updateExports();
  });
}
document.addEventListener("pointerdown", (event) => {
  for (const menu of menus) if (menu.open && !menu.contains(event.target) && !event.target.closest?.(".choice-menu")) menu.open = false;
});
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape" || document.querySelector(".choice-menu:popover-open")) return;
  for (const menu of menus) {
    if (!menu.open) continue;
    menu.open = false;
    menu.querySelector("summary").focus();
  }
});
document.querySelectorAll("[data-view]").forEach((button) => button.addEventListener("click", () => setView(button.dataset.view)));

init();
initMapLibre();
initGoogleMap();
