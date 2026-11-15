// The map's anomaly layer: 1° regions whose adjusted activity in the selected
// month is far from the same calendar month in the other years (a robust z of
// 3 or more, see /v1/anomalies). Drawn as cells in the same octagon style as
// the activity grid, coloured by their difference from the median on the
// anomaly ramp that History uses.

import { ANOMALY, anomalyClass } from "./ramp.js";

const percent = new Intl.NumberFormat("en", { style: "percent", signDisplay: "exceptZero", maximumFractionDigits: 0 });
const whole = new Intl.NumberFormat("en");
const requests = new Map();

export function regionName({ ry, rx }) {
  const lat = (value) => `${Math.abs(value)}°${value < 0 ? "S" : "N"}`;
  const lon = (value) => `${Math.abs(value)}°${value < 0 ? "W" : "E"}`;
  return `${lat(ry)}–${lat(ry + 1)}, ${lon(rx)}–${lon(rx + 1)}`;
}

export function regionBox({ ry, rx }) {
  return { min_lat: ry, max_lat: ry + 1, min_lon: rx, max_lon: rx + 1 };
}

// A region with no usual activity this month counts as far above.
export function anomalyColor(item) {
  return ANOMALY[(item.delta == null ? ANOMALY.length : anomalyClass(item.delta)) - 1].color;
}

// The month's unusual regions as map cells, loaded once per month.
export function loadAnomalies(api, month) {
  if (!requests.has(month)) {
    const request = api("/v1/anomalies", { month }).then((result) => ({
      ...result,
      cells: result.data.map((item) => ({
        west: item.rx, south: item.ry, size: 1, latitude: item.ry + .5, longitude: item.rx + .5,
        color: anomalyColor(item), strength: 1, kind: "anomaly", anomaly: item, month,
      })),
    }));
    request.catch(() => requests.delete(month));
    requests.set(month, request);
    if (requests.size > 12) requests.delete(requests.keys().next().value);
  }
  return requests.get(month);
}

// True when a region's centre lies in a bounding box (west > east crosses 180°).
export function inBox({ latitude, longitude }, box) {
  const lon = box.min_lon <= box.max_lon
    ? longitude >= box.min_lon && longitude <= box.max_lon
    : longitude >= box.min_lon || longitude <= box.max_lon;
  return latitude >= box.min_lat && latitude <= box.max_lat && lon;
}

export function differenceText(item, monthName) {
  return item.delta == null ? `${whole.format(Math.round(item.value))} cell-days where ${monthName} usually has none`
    : `${percent.format(item.delta)} vs usual`;
}

// Inspector rows for one region.
export function anomalyDetails(item, monthLabel, monthName) {
  return [
    ["Region", regionName(item)],
    [monthLabel, `${whole.format(Math.round(item.value))} adjusted cell-days`],
    [`Median of ${item.years} other years`, whole.format(Math.round(item.baseline))],
    ["Difference", item.delta == null ? `${monthName} usually has no fire here` : percent.format(item.delta)],
    ["Robust z", `${item.z > 0 ? "+" : ""}${item.z.toFixed(1)} (unusual from ±3)`],
  ];
}
