const regions = {
  global: { label: "Global", minLat: -90, maxLat: 90, minLon: -180, maxLon: 180 },
  seasia: { label: "Southeast Asia", minLat: -12, maxLat: 30, minLon: 90, maxLon: 145 },
  amazon: { label: "Amazon basin", minLat: -22, maxLat: 10, minLon: -82, maxLon: -44 },
  africa: { label: "Central and southern Africa", minLat: -36, maxLat: 16, minLon: 5, maxLon: 48 },
};

const colors = ["#62d8e7", "#ff9b54", "#f15b50"];
const state = { cells: [], selectedCell: null };
const $ = (selector) => document.querySelector(selector);
const fmt = new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 });

function monthDates(value) {
  const [year, month] = value.split("-").map(Number);
  const last = new Date(Date.UTC(year, month, 0)).getUTCDate();
  return { year, month, start: `${value}-01`, end: `${value}-${String(last).padStart(2, "0")}` };
}

async function getJson(path, params = {}) {
  const query = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => value !== undefined && value !== null && query.set(key, value));
  const response = await fetch(`${path}?${query}`);
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.detail || `${response.status} ${response.statusText}`);
  }
  return response.json();
}

function svgElement(name, attributes = {}) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", name);
  Object.entries(attributes).forEach(([key, value]) => node.setAttribute(key, value));
  return node;
}

function project(latitude, longitude, region) {
  const x = ((longitude - region.minLon) / (region.maxLon - region.minLon)) * 1000;
  const y = ((region.maxLat - latitude) / (region.maxLat - region.minLat)) * 500;
  return [x, y];
}

function drawGrid(svg, region) {
  svg.replaceChildren();
  for (let index = 0; index <= 5; index += 1) {
    const x = (index / 5) * 1000;
    const y = (index / 5) * 500;
    svg.append(svgElement("line", { x1: x, y1: 0, x2: x, y2: 500, class: "grid-line" }));
    svg.append(svgElement("line", { x1: 0, y1: y, x2: 1000, y2: y, class: "grid-line" }));
    const lon = region.minLon + (index / 5) * (region.maxLon - region.minLon);
    const lat = region.maxLat - (index / 5) * (region.maxLat - region.minLat);
    const lonLabel = svgElement("text", { x: Math.min(960, x + 5), y: 494, class: "grid-label" });
    lonLabel.textContent = `${lon.toFixed(0)}°`;
    const latLabel = svgElement("text", { x: 6, y: Math.max(14, y - 5), class: "grid-label" });
    latLabel.textContent = `${lat.toFixed(0)}°`;
    svg.append(lonLabel, latLabel);
  }
}

function metricValue(cell) {
  return Number(cell[$("#metric").value] || 0);
}

function renderMap(cells, region) {
  const svg = $("#activity-map");
  drawGrid(svg, region);
  if (!cells.length) return;
  const values = cells.map(metricValue);
  const max = Math.max(...values, 1);
  cells.forEach((cell) => {
    const [cx, cy] = project(cell.center_latitude, cell.center_longitude, region);
    if (cx < 0 || cx > 1000 || cy < 0 || cy > 500) return;
    const ratio = Math.min(1, metricValue(cell) / max);
    const radius = 1.8 + Math.sqrt(ratio) * 10;
    const colorIndex = ratio > 0.66 ? 2 : ratio > 0.25 ? 1 : 0;
    const circle = svgElement("circle", {
      cx, cy, r: radius, fill: colors[colorIndex], opacity: 0.42 + ratio * 0.48,
      class: "cell", tabindex: 0, "aria-label": `${fmt.format(cell.detection_count)} observations`,
    });
    circle.addEventListener("click", () => selectCell(cell, circle));
    circle.addEventListener("keydown", (event) => event.key === "Enter" && selectCell(cell, circle));
    svg.append(circle);
  });
}

function selectCell(cell, circle) {
  document.querySelectorAll(".cell.selected").forEach((item) => item.classList.remove("selected"));
  circle.classList.add("selected");
  state.selectedCell = cell;
  $("#detail-title").textContent = cell.h3_cell;
  const values = [
    cell.dataset, cell.platform, fmt.format(cell.detection_count),
    `${fmt.format(cell.detections_per_km2)} / km²`, `${fmt.format(cell.frp_sum_mw)} MW`,
    `${fmt.format(cell.cell_area_km2)} km²`, fmt.format(cell.active_days),
  ];
  document.querySelectorAll("#cell-details dd").forEach((item, index) => { item.textContent = values[index]; });
}

function renderTable(points, truncated) {
  const body = $("#observation-rows");
  body.replaceChildren();
  points.forEach((point) => {
    const row = document.createElement("tr");
    const values = [
      point.observation_time_utc.replace("T", " ").replace("Z", ""), point.platform,
      Number(point.latitude).toFixed(4), Number(point.longitude).toFixed(4),
      fmt.format(point.frp_mw), point.confidence_class, point.daynight, point.source_stage,
    ];
    values.forEach((value) => { const cell = document.createElement("td"); cell.textContent = value; row.append(cell); });
    body.append(row);
  });
  $("#table-note").textContent = truncated
    ? "Showing the first 100 matching point observations; more are available."
    : `Showing all ${points.length} matching point observations.`;
}

function renderTrend(rows) {
  const svg = $("#trend-chart");
  svg.replaceChildren();
  if (!rows.length) return;
  const grouped = rows.reduce((result, row) => {
    (result[row.platform] ||= []).push(row);
    return result;
  }, {});
  const platforms = Object.keys(grouped);
  const allValues = rows.map((row) => Number(row.detection_count));
  const max = Math.max(...allValues, 1);
  const xFor = (year, month) => {
    const serial = year * 12 + month;
    const serials = rows.map((row) => row.year * 12 + row.month);
    return 65 + ((serial - Math.min(...serials)) / Math.max(1, Math.max(...serials) - Math.min(...serials))) * 900;
  };
  const yFor = (value) => 255 - (value / max) * 210;
  svg.append(svgElement("line", { x1: 65, y1: 255, x2: 965, y2: 255, class: "axis" }));
  svg.append(svgElement("line", { x1: 65, y1: 35, x2: 65, y2: 255, class: "axis" }));
  [0, .5, 1].forEach((ratio) => {
    const label = svgElement("text", { x: 8, y: yFor(max * ratio) + 4, class: "axis-label" });
    label.textContent = fmt.format(max * ratio);
    svg.append(label);
  });
  platforms.forEach((platform, platformIndex) => {
    const points = grouped[platform].sort((a, b) => (a.year * 12 + a.month) - (b.year * 12 + b.month));
    const color = colors[platformIndex % colors.length];
    const path = points.map((row, index) => `${index ? "L" : "M"}${xFor(row.year, row.month)},${yFor(row.detection_count)}`).join(" ");
    svg.append(svgElement("path", { d: path, stroke: color, class: "trend-line" }));
    points.forEach((row) => svg.append(svgElement("circle", { cx: xFor(row.year, row.month), cy: yFor(row.detection_count), r: 4, fill: color, class: "trend-dot" })));
    const label = svgElement("text", { x: 78 + platformIndex * 120, y: 20, fill: color, class: "axis-label" });
    label.textContent = platform;
    svg.append(label);
  });
}

function updatePeriod(monthRows) {
  if (!monthRows.length) {
    $("#period-status").textContent = "No coverage";
    $("#period-detail").textContent = "No aggregate row for this selection";
    return;
  }
  const complete = monthRows.every((row) => row.is_complete_month);
  $("#period-status").textContent = complete ? "Complete month" : "Partial month";
  $("#period-detail").textContent = monthRows.map((row) => `${row.platform}: ${row.active_days} active days`).join(" · ");
}

async function loadDashboard() {
  const dataset = $("#dataset").value;
  const selectedMonth = monthDates($("#month").value);
  const region = regions[$("#region").value];
  const resolution = $("#resolution").value;
  $("#error-message").textContent = "";
  $("#map-loading").classList.remove("hidden");
  $("#refresh").disabled = true;
  try {
    const bbox = { min_lat: region.minLat, max_lat: region.maxLat, min_lon: region.minLon, max_lon: region.maxLon };
    const [spatial, points, monthly] = await Promise.all([
      getJson("/v1/spatial/monthly", { resolution, dataset, year: selectedMonth.year, month: selectedMonth.month, ...bbox, limit: 2500 }),
      getJson("/v1/hotspots", { dataset, start_date: selectedMonth.start, end_date: selectedMonth.end, ...bbox, limit: 100 }),
      getJson("/v1/activity/monthly", { dataset, limit: 100 }),
    ]);
    state.cells = spatial.data;
    renderMap(state.cells, region);
    renderTable(points.data, points.truncated);
    renderTrend(monthly.data);
    updatePeriod(monthly.data.filter((row) => row.year === selectedMonth.year && row.month === selectedMonth.month));
    $("#cell-count").textContent = fmt.format(spatial.count);
    $("#cell-truncation").textContent = spatial.truncated ? "Result capped at 2,500 cells" : "All matching occupied cells";
    $("#point-count").textContent = fmt.format(points.count);
    $("#point-truncation").textContent = points.truncated ? "More observations available" : "All matching observations";
    $("#map-title").textContent = `${region.label} · ${$("#month").value}`;
  } catch (error) {
    $("#error-message").textContent = `Unable to load the selected data: ${error.message}`;
  } finally {
    $("#map-loading").classList.add("hidden");
    $("#refresh").disabled = false;
  }
}

async function initialize() {
  try {
    const health = await getJson("/health");
    $("#api-status").classList.add("online");
    $("#api-status").innerHTML = `<span></span> ${fmt.format(health.source_rows)} validated observations`;
    await loadDashboard();
  } catch (error) {
    $("#api-status").classList.add("offline");
    $("#api-status").innerHTML = "<span></span> API unavailable";
    $("#error-message").textContent = error.message;
  }
}

$("#refresh").addEventListener("click", loadDashboard);
$("#region").addEventListener("change", (event) => {
  $("#resolution").value = event.target.value === "global" ? "4" : "5";
});
$("#metric").addEventListener("change", () => renderMap(state.cells, regions[$("#region").value]));
initialize();
