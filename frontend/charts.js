// Small SVG charts for History and the report. No library: each chart is a
// <figure> with a title, a one-line note, an SVG and (in the app) a tooltip
// that follows the pointer. In the report the same marks carry <title> text.
//
// One y-axis per chart, thin marks, recessive grid. Colours come from the
// shared ramps; text always uses the text colours, never a series colour.

const NS = "http://www.w3.org/2000/svg";
const W = 680;
const H = 250;
const M = { top: 14, right: 16, bottom: 26, left: 46 };
const compact = new Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 1 });
const whole = new Intl.NumberFormat("en");

export const THEMES = {
  dark: { text: "#eff0f3", muted: "#a6abb6", grid: "#ffffff1c", axis: "#ffffff40", surface: "#08090c", band: "#e5702c33" },
  light: { text: "#1b1d22", muted: "#5d636e", grid: "#0000001a", axis: "#00000055", surface: "#ffffff", band: "#e5702c2e" },
};
export const SERIES = {
  activity: "#e5702c",
  highlight: "#7fb6e8",
  below: "#7fb6e8",
  above: "#e0672a",
  // Validated together on the dark surface (dataviz palette check).
  modis: "#c96a2e",
  viirs_snpp: "#3987c9",
  viirs_noaa20: "#2fa37a",
};

function el(tag, attributes = {}, text) {
  const node = document.createElementNS(NS, tag);
  for (const [key, value] of Object.entries(attributes)) if (value != null) node.setAttribute(key, value);
  if (text != null) node.textContent = text;
  return node;
}

// Round tick values from 0 up to at least `max`.
function ticks(max, count = 4) {
  if (!(max > 0)) return [0, 1];
  const rough = max / count;
  const power = 10 ** Math.floor(Math.log10(rough));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * power).find((value) => value >= rough);
  const result = [];
  for (let value = 0; value < max + step * .999; value += step) result.push(Number(value.toPrecision(12)));
  return result;
}

// The shared frame: figure, heading, svg, y grid and scale helpers.
function frame({ title, note, theme, yTicks, yFormat = compact.format, legend }) {
  const figure = document.createElement("figure");
  figure.className = "chart";
  const caption = document.createElement("figcaption");
  const heading = document.createElement("strong");
  heading.textContent = title;
  const detail = document.createElement("span");
  detail.textContent = note ?? "";
  caption.append(heading, detail);
  if (legend?.length > 1) {
    const key = document.createElement("ul");
    key.className = "chart-legend";
    for (const { label, color, dashed } of legend) {
      const item = document.createElement("li");
      const swatch = document.createElement("i");
      swatch.style.background = dashed ? `repeating-linear-gradient(90deg, ${color} 0 4px, transparent 4px 7px)` : color;
      item.append(swatch, label);
      key.append(item);
    }
    caption.append(key);
  }
  const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": `${title}. ${note ?? ""}` });
  svg.style.cssText = "display:block;width:100%;height:auto;overflow:visible";
  const top = yTicks.at(-1);
  const bottom = yTicks[0];
  const y = (value) => M.top + (H - M.top - M.bottom) * (1 - (value - bottom) / (top - bottom));
  for (const value of yTicks) {
    svg.append(
      el("line", { x1: M.left, x2: W - M.right, y1: y(value), y2: y(value), stroke: value === 0 ? theme.axis : theme.grid, "stroke-width": 1 }),
      el("text", { x: M.left - 7, y: y(value) + 3.5, "text-anchor": "end", "font-size": 11, fill: theme.muted }, yFormat(value)),
    );
  }
  figure.append(caption, svg);
  return { figure, svg, y, plotWidth: W - M.left - M.right };
}

function xLabels(svg, theme, labels, xOf, every) {
  labels.forEach((label, index) => {
    if (label == null || index % every) return;
    svg.append(el("text", { x: xOf(index), y: H - 8, "text-anchor": "middle", "font-size": 11, fill: theme.muted }, label));
  });
}

// Shows a tooltip and a guide line for the band under the pointer. `tip`
// returns the lines of text for a band; the first is the heading.
function hover(figure, svg, theme, count, xOf, tip) {
  const guide = el("line", { y1: M.top, y2: H - M.bottom, stroke: theme.axis, "stroke-width": 1, visibility: "hidden" });
  const target = el("rect", { x: M.left, y: M.top, width: W - M.left - M.right, height: H - M.top - M.bottom, fill: "transparent" });
  svg.append(guide, target);
  const box = document.createElement("div");
  box.className = "chart-tip";
  box.hidden = true;
  figure.append(box);
  const hide = () => {
    box.hidden = true;
    guide.setAttribute("visibility", "hidden");
  };
  target.addEventListener("pointermove", (event) => {
    const rect = svg.getBoundingClientRect();
    const x = (event.clientX - rect.left) / rect.width * W;
    let index = 0;
    for (let i = 1; i < count; i++) if (Math.abs(xOf(i) - x) < Math.abs(xOf(index) - x)) index = i;
    const lines = tip(index);
    if (!lines) return hide();
    guide.setAttribute("x1", xOf(index));
    guide.setAttribute("x2", xOf(index));
    guide.setAttribute("visibility", "visible");
    box.replaceChildren(...lines.map((line, i) => {
      const row = document.createElement(i ? "span" : "strong");
      row.textContent = line;
      return row;
    }));
    box.hidden = false;
    const host = figure.getBoundingClientRect();
    const left = rect.left - host.left + xOf(index) / W * rect.width;
    const flip = left > host.width * .6;
    box.style.left = `${left + (flip ? -10 : 10)}px`;
    box.style.transform = flip ? "translateX(-100%)" : "";
    box.style.top = `${rect.top - host.top + M.top / H * rect.height}px`;
  });
  target.addEventListener("pointerleave", hide);
}

function finish(chart, theme, { interactive, count, xOf, tip }) {
  if (interactive) hover(chart.figure, chart.svg, theme, count, xOf, tip);
  return chart.figure;
}

function titled(node, lines) {
  if (lines) node.append(el("title", {}, lines.join("\n")));
  return node;
}

// Total activity per year. Years missing some months are drawn faint.
// `years`: [{ year, total, months, complete }]. `median` marks the typical year.
export function yearChart({ years, median, unit, theme = THEMES.dark, interactive = true, title = "Activity by year" }) {
  const max = Math.max(1, median ?? 0, ...years.map((item) => item.total));
  const yTicks = ticks(max);
  const partial = years.some((item) => !item.complete);
  const chart = frame({
    title, theme, yTicks,
    note: `${unit[0].toUpperCase()}${unit.slice(1)} per year` + (partial ? ". Faint bars are years with months missing from the range or the record." : "."),
  });
  const step = chart.plotWidth / years.length;
  const width = Math.max(2, Math.min(18, step - 2));
  const xOf = (index) => M.left + step * (index + .5);
  const tip = (index) => {
    const item = years[index];
    return [String(item.year), `${whole.format(Math.round(item.total))} ${unit}`,
      item.complete ? (item.rank ?? "") : `${item.months} of 12 months`,
      item.delta == null ? "" : `${item.delta > 0 ? "+" : ""}${Math.round(item.delta * 100)}% vs median year`].filter(Boolean);
  };
  years.forEach((item, index) => {
    const top = chart.y(item.total);
    const height = Math.max(item.total > 0 ? 1 : 0, chart.y(0) - top);
    const radius = Math.min(3, width / 2, height);
    const x = xOf(index) - width / 2;
    // Rounded at the data end only, square on the baseline.
    const path = `M${x},${top + height}V${top + radius}Q${x},${top} ${x + radius},${top}H${x + width - radius}Q${x + width},${top} ${x + width},${top + radius}V${top + height}Z`;
    chart.svg.append(titled(el("path", { d: path, fill: SERIES.activity, opacity: item.complete ? 1 : .4 }), interactive ? null : tip(index)));
  });
  if (median != null) {
    chart.svg.append(
      el("line", { x1: M.left, x2: W - M.right, y1: chart.y(median), y2: chart.y(median), stroke: theme.text, "stroke-width": 1, "stroke-dasharray": "4 3", opacity: .7 }),
      el("text", { x: W - M.right, y: chart.y(median) - 5, "text-anchor": "end", "font-size": 11, fill: theme.text }, `median ${compact.format(median)}`),
    );
  }
  xLabels(chart.svg, theme, years.map((item) => item.year), xOf, Math.ceil(years.length / 12));
  return finish(chart, theme, { interactive, count: years.length, xOf, tip });
}

function linePath(points) {
  let path = "";
  let pen = false;
  for (const point of points) {
    if (!point) {
      pen = false;
      continue;
    }
    path += `${pen ? "L" : "M"}${point[0].toFixed(1)},${point[1].toFixed(1)}`;
    pen = true;
  }
  return path;
}

// The seasonal cycle: the median of each calendar month, the band most years
// fall in, and one highlighted year on top.
// `profile`: 12 × { median, low, high } | null. `focus`: { label, values: 12 × number|null }.
export function seasonChart({ profile, focus, monthNames, unit, theme = THEMES.dark, interactive = true, title = "Seasonal cycle" }) {
  const max = Math.max(1, ...profile.map((item) => item?.high ?? 0), ...(focus?.values ?? []).map((value) => value ?? 0));
  const yTicks = ticks(max);
  const legend = [{ label: "Median month", color: SERIES.activity }, ...(focus ? [{ label: focus.label, color: SERIES.highlight }] : [])];
  const chart = frame({
    title, theme, yTicks, legend,
    note: `${unit[0].toUpperCase()}${unit.slice(1)} per calendar month. The band holds the middle 80% of years.`,
  });
  const step = chart.plotWidth / 12;
  const xOf = (index) => M.left + step * (index + .5);
  const upper = profile.map((item, index) => item && [xOf(index), chart.y(item.high)]);
  const lower = profile.map((item, index) => item && [xOf(index), chart.y(item.low)]);
  if (upper.every(Boolean)) {
    chart.svg.append(el("path", { d: `${linePath(upper)}L${[...lower].reverse().map((point) => point.map((v) => v.toFixed(1)).join(",")).join("L")}Z`, fill: theme.band }));
  }
  const line = (points, color) => el("path", { d: linePath(points), fill: "none", stroke: color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" });
  chart.svg.append(line(profile.map((item, index) => item && [xOf(index), chart.y(item.median)]), SERIES.activity));
  if (focus) {
    const points = focus.values.map((value, index) => value == null ? null : [xOf(index), chart.y(value)]);
    chart.svg.append(line(points, SERIES.highlight));
    for (const point of points) if (point) chart.svg.append(el("circle", { cx: point[0], cy: point[1], r: 3, fill: SERIES.highlight, stroke: theme.surface, "stroke-width": 1.5 }));
  }
  xLabels(chart.svg, theme, monthNames, xOf, 1);
  const tip = (index) => {
    const item = profile[index];
    const value = focus?.values[index];
    return [monthNames[index],
      item ? `Median ${whole.format(Math.round(item.median))} ${unit}` : "No data",
      item ? `Middle 80% of ${item.years} years: ${whole.format(Math.round(item.low))}–${whole.format(Math.round(item.high))}` : "",
      value == null ? "" : `${focus.label}: ${whole.format(Math.round(value))}`].filter(Boolean);
  };
  if (!interactive) {
    profile.forEach((item, index) => chart.svg.append(titled(
      el("rect", { x: xOf(index) - step / 2, y: M.top, width: step, height: H - M.top - M.bottom, fill: "transparent" }), tip(index))));
  }
  return finish(chart, theme, { interactive, count: 12, xOf, tip });
}

// Each month's difference from its baseline: blue below, orange above.
// `months`: [{ period, label, delta }] in order; delta null where there is no baseline.
export function anomalyChart({ months, baseline, theme = THEMES.dark, interactive = true, title = "Difference from baseline" }) {
  const limit = 2;
  const chart = frame({
    title, theme, yTicks: [-1, 0, 1, 2],
    yFormat: (value) => `${value > 0 ? "+" : ""}${value * 100}%`,
    note: `Each month against the ${baseline} for the same calendar month. Bars above +200% are cut off.`,
    legend: [{ label: "Above baseline", color: SERIES.above }, { label: "Below baseline", color: SERIES.below }],
  });
  const step = chart.plotWidth / months.length;
  const width = Math.max(1, Math.min(8, step - (step > 3 ? 1 : 0)));
  const xOf = (index) => M.left + step * (index + .5);
  const zero = chart.y(0);
  const tip = (index) => {
    const { label, delta, value, centre } = months[index];
    return [label, delta == null ? "No baseline" : `${delta > 0 ? "+" : ""}${Math.round(delta * 100)}% vs baseline`,
      value == null ? "" : `${whole.format(Math.round(value))} against ${whole.format(Math.round(centre))}`].filter(Boolean);
  };
  months.forEach((month, index) => {
    if (month.delta == null || !month.delta) return;
    const end = chart.y(Math.max(-1, Math.min(limit, month.delta)));
    chart.svg.append(el("rect", {
      x: xOf(index) - width / 2, y: Math.min(zero, end), width, height: Math.max(.6, Math.abs(end - zero)),
      fill: month.delta > 0 ? SERIES.above : SERIES.below,
    }));
  });
  const years = months.map((month) => month.period.endsWith("-01") ? month.period.slice(0, 4) : null);
  const shown = years.filter(Boolean).length;
  let seen = 0;
  xLabels(chart.svg, theme, years.map((year) => year && (seen++ % Math.ceil(shown / 12) ? null : year)), (index) => xOf(index) + step * 5.5, 1);
  if (!interactive) {
    months.forEach((month, index) => month.delta != null && chart.svg.append(titled(
      el("rect", { x: xOf(index) - step / 2, y: M.top, width: step, height: H - M.top - M.bottom, fill: "transparent" }), tip(index))));
  }
  return finish(chart, theme, { interactive, count: months.length, xOf, tip });
}

// One line per series over the years, named in the legend.
// `series`: [{ key, label, values: [number|null per year] }].
export function linesChart({ years, series, unit, note, theme = THEMES.dark, interactive = true, title }) {
  const max = Math.max(1, ...series.flatMap((item) => item.values.map((value) => value ?? 0)));
  const yTicks = ticks(max);
  const legend = series.map((item) => ({ label: item.label, color: SERIES[item.key] }));
  const chart = frame({ title, theme, yTicks, legend, note });
  const step = chart.plotWidth / years.length;
  const xOf = (index) => M.left + step * (index + .5);
  for (const item of series) {
    const points = item.values.map((value, index) => value == null ? null : [xOf(index), chart.y(value)]);
    chart.svg.append(el("path", { d: linePath(points), fill: "none", stroke: SERIES[item.key], "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }));
    // A lone year between gaps would vanish as a line, so mark every point of short runs.
    points.forEach((point, index) => {
      if (point && !points[index - 1] && !points[index + 1]) chart.svg.append(el("circle", { cx: point[0], cy: point[1], r: 2.5, fill: SERIES[item.key] }));
    });
  }
  xLabels(chart.svg, theme, years, xOf, Math.ceil(years.length / 12));
  const tip = (index) => [String(years[index]), ...series.filter((item) => item.values[index] != null)
    .map((item) => `${item.label}: ${compact.format(item.values[index])} ${unit}`)];
  if (!interactive) {
    years.forEach((_, index) => chart.svg.append(titled(
      el("rect", { x: xOf(index) - step / 2, y: M.top, width: step, height: H - M.top - M.bottom, fill: "transparent" }), tip(index))));
  }
  return finish(chart, theme, { interactive, count: years.length, xOf, tip });
}
