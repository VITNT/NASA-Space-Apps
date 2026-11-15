// Builds the area report: one self-contained HTML page that prints cleanly
// to PDF. It restates what History shows for the area (totals, the seasonal
// cycle, anomalies, trend) with the method and the data's limits, so it can
// be read without the app.

import { ACTIVITY, activityClass } from "./ramp.js";
import { rankText } from "./stats.js";

const whole = new Intl.NumberFormat("en");
const percent = new Intl.NumberFormat("en", { style: "percent", signDisplay: "exceptZero" });
const escape = (text) => String(text ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const number = (value) => whole.format(Math.round(value));

const styles = `
  :root { color-scheme: light; }
  * { box-sizing: border-box; }
  body { margin: 0; padding: 40px 24px 64px; background: #f4f4f1; color: #1b1d22; font: 15px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif; }
  main { max-width: 860px; margin: 0 auto; padding: 44px 48px; background: #fff; border-radius: 10px; box-shadow: 0 1px 3px #0002; }
  header p, .muted, figcaption span, small { color: #5d636e; }
  .brand { font-weight: 690; letter-spacing: -.04em; font-size: 15px; }
  h1 { margin: 6px 0 4px; font-size: 30px; line-height: 1.15; letter-spacing: -.035em; font-weight: 560; }
  h2 { margin: 38px 0 10px; font-size: 18px; letter-spacing: -.02em; font-weight: 600; }
  p { margin: 0 0 10px; }
  .facts { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; margin: 22px 0 6px; padding: 0; list-style: none; }
  .facts li { padding: 12px 14px; border: 1px solid #0000001a; border-radius: 8px; }
  .facts span { display: block; color: #5d636e; font-size: 12px; }
  .facts strong { display: block; margin: 2px 0; font-size: 21px; font-weight: 560; letter-spacing: -.03em; }
  .facts small { font-size: 12px; }
  figure { margin: 22px 0 0; break-inside: avoid; }
  figcaption { margin-bottom: 6px; }
  figcaption strong { display: block; font-weight: 600; }
  figcaption span { display: block; font-size: 12.5px; }
  .chart-legend { display: flex; gap: 14px; margin: 6px 0 0; padding: 0; list-style: none; font-size: 12px; color: #5d636e; }
  .chart-legend li { display: flex; align-items: center; gap: 5px; }
  .chart-legend i { width: 14px; height: 3px; border-radius: 2px; }
  img { display: block; width: 100%; border-radius: 8px; }
  table { width: 100%; border-collapse: separate; border-spacing: 2px; font-size: 11px; font-variant-numeric: tabular-nums; break-inside: avoid; }
  th { padding: 3px; color: #5d636e; font-weight: 500; }
  td { padding: 5px 2px; border-radius: 3px; text-align: center; background: #f1f1ee; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
  td.none { color: #9aa0aa; background: repeating-linear-gradient(135deg, #f1f1ee 0 3px, #e4e4e0 3px 5px); }
  td.light { color: #fff; }
  .key { display: flex; flex-wrap: wrap; gap: 4px 12px; margin: 8px 0 0; padding: 0; list-style: none; font-size: 12px; color: #5d636e; }
  .key i { display: inline-block; width: 14px; height: 9px; margin-right: 4px; border-radius: 2px; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
  .method li { margin-bottom: 6px; }
  .caution { margin: 14px 0; padding: 10px 14px; border-left: 3px solid #e5702c; background: #fbf1ea; border-radius: 0 6px 6px 0; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
  .actions { max-width: 860px; margin: 0 auto 14px; text-align: right; }
  .actions button { padding: 8px 16px; border: 0; border-radius: 18px; background: #1b1d22; color: #fff; font: inherit; cursor: pointer; }
  a { color: inherit; }
  @media print {
    body { padding: 0; background: #fff; }
    main { padding: 0; box-shadow: none; max-width: none; }
    .actions { display: none; }
    h2 { break-after: avoid; }
  }
`;

function heatmap(model, monthNames, from, to) {
  const { values, peak, annual } = model;
  const rows = [];
  for (let year = Number(to.slice(0, 4)); year >= Number(from.slice(0, 4)); year--) {
    const cells = monthNames.map((_, index) => {
      const period = `${year}-${String(index + 1).padStart(2, "0")}`;
      if (period < from || period > to) return "<td class=\"none\"></td>";
      const entry = values.get(period);
      if (!entry) return "<td class=\"none\">–</td>";
      const step = activityClass(entry.value, peak);
      const ramp = step ? ACTIVITY[step - 1] : null;
      return `<td${ramp ? ` style="background:${ramp.color}" class="${ramp.ink}"` : ""}>${number(entry.value)}</td>`;
    });
    const total = annual.find((item) => item.year === year);
    rows.push(`<tr><th scope="row">${year}</th>${cells.join("")}<td>${total ? number(total.total) + (total.complete ? "" : "*") : ""}</td></tr>`);
  }
  return `<table><thead><tr><th>Year</th>${monthNames.map((name) => `<th scope="col">${name}</th>`).join("")}<th scope="col">Total</th></tr></thead>`
    + `<tbody>${rows.join("")}</tbody></table>`
    + `<ul class="key">${ACTIVITY.map((step) => `<li><i style="background:${step.color}"></i>${step.name}</li>`).join("")}`
    + "<li>Hatched: no satellite coverage</li><li>* year with months missing</li></ul>";
}

// Months far from the same calendar month in the other years (robust z of 3 or more).
function unusualSection(model, monthNames) {
  const items = [...(model.unusual?.values() ?? [])].sort((a, b) => a.period.localeCompare(b.period));
  if (!items.length) return "";
  const label = (period) => `${monthNames[Number(period.slice(5)) - 1]} ${period.slice(0, 4)}`;
  const rows = items.map((item) => `<li>${escape(label(item.period))}: ${item.z > 0 ? "above" : "below"} usual, `
    + `${item.delta == null ? "activity where the month usually has none" : `${escape(percent.format(item.delta))} vs the median of ${item.years} other years`}`
    + ` (robust z ${item.z > 0 ? "+" : ""}${item.z.toFixed(1)})</li>`).join("");
  return `<p><strong>Unusual months.</strong> Adjusted activity far from the same calendar month in the other years: a robust z of 3 or more on log(1 + cell-days), against the median and median absolute deviation of those years.</p><ul class="method">${rows}</ul>`;
}

export function buildReport({
  area, shape, bounds, record, from, to, lastDay, harmonizing, model, monthNames, baseline, trend, month, mapImage, figures, link,
}) {
  const { values, annual, profile, typicalYear } = model;
  const unit = harmonizing ? "adjusted cell-days" : "active cell-days";
  const complete = annual.filter((item) => item.complete);
  const busiest = complete.length ? complete.reduce((a, b) => b.total > a.total ? b : a) : null;
  const quietest = complete.length ? complete.reduce((a, b) => b.total < a.total ? b : a) : null;
  const seasons = profile.filter(Boolean);
  const peakMonth = seasons.length ? seasons.reduce((a, b) => b.median > a.median ? b : a) : null;
  const yearMedian = seasons.reduce((sum, item) => sum + item.median, 0);
  // The fire season: the calendar months that hold most of a typical year's activity.
  const season = seasons.filter((item) => yearMedian && item.median >= yearMedian / 12).map((item) => monthNames[item.month - 1]);
  const total = [...values.values()].reduce((sum, entry) => sum + entry.value, 0);
  const span = `${monthNames[Number(from.slice(5)) - 1]} ${from.slice(0, 4)} to ${monthNames[Number(to.slice(5)) - 1]} ${to.slice(0, 4)}`;
  const fact = (label, value, detail = "") => `<li><span>${escape(label)}</span><strong>${escape(value)}</strong><small>${escape(detail)}</small></li>`;
  const comparison = month.comparison;
  const facts = [
    fact("Activity in the range", number(total), unit),
    busiest ? fact("Busiest year", busiest.year, `${number(busiest.total)} ${unit}`) : "",
    quietest && quietest !== busiest ? fact("Quietest year", quietest.year, `${number(quietest.total)} ${unit}`) : "",
    typicalYear != null ? fact("Median year", number(typicalYear), `${complete.length} complete years`) : "",
    peakMonth ? fact("Peak month", monthNames[peakMonth.month - 1], `median ${number(peakMonth.median)} ${unit}`) : "",
    fact(month.label, number(month.total), comparison?.delta == null ? "active cell-days observed"
      : `${percent.format(comparison.delta)} vs median of ${comparison.years} earlier years`
        + (comparison.throughDay ? `, days 1–${comparison.throughDay}` : "") + `; ${rankText(comparison)}`),
  ].join("");
  const box = `${bounds.min_lat}° to ${bounds.max_lat}° latitude, ${bounds.min_lon}° to ${bounds.max_lon}° longitude`;

  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${escape(area)} fire activity report · Luminite</title>
<style>${styles}</style>
</head>
<body>
<div class="actions"><button type="button" onclick="print()">Print or save as PDF</button></div>
<main>
<header>
  <p class="brand">Luminite · fire activity report</p>
  <h1>${escape(area)}</h1>
  <p>${escape(span)} · ${escape(record)} · data through ${escape(lastDay)}</p>
  <p class="muted">${escape(shape)}. Bounds: ${escape(box)}.</p>
</header>

<ul class="facts">${facts}</ul>
${harmonizing ? "" : `<p class="caution"><strong>Read across years with care.</strong> These counts are as observed and are not adjusted for the satellites flying each year. More satellites see more fire: totals step up when Suomi NPP joins in 2012 and again with NOAA-20 in 2018, so years from different satellite eras are not directly comparable.</p>`}
${season.length && season.length < 12 ? `<p><strong>Fire season.</strong> In a typical year most activity falls in ${escape(season.join(", "))}.</p>` : ""}
${trend ? `<p><strong>Trend.</strong> ${escape(trend.headline)}. <span class="muted">${escape(trend.detail)}</span></p>`
    : "<p><strong>Trend.</strong> Not tested: a trend across years needs the satellite adjustment, which this archive has not had run.</p>"}

${unusualSection(model, monthNames)}

<h2>Charts</h2>
${figures.map((figure) => figure.outerHTML).join("\n")}

<h2>Burning calendar</h2>
<p class="muted">${escape(unit[0].toUpperCase() + unit.slice(1))} per month. Colour classes follow the busiest month in the range.</p>
${heatmap(model, monthNames, from, to)}

${mapImage ? `<h2>Map</h2><figure><img alt="Map of mapped fire activity" src="${mapImage}"><figcaption><span>${escape(month.mapCaption)}</span></figcaption></figure>` : ""}

<h2>How to read this</h2>
<ul class="method">
  <li><strong>Active cell-day.</strong> The Earth is divided into cells 0.1° across (about 11 km). A cell counts once for each UTC day a satellite detected fire in it. This measures how much area burned on how many days, not the number of fires, and it is far less sensitive to instrument resolution than a count of detections.</li>
  <li><strong>What is counted.</strong> NASA FIRMS active-fire detections from MODIS (Terra, Aqua) and VIIRS (Suomi NPP, NOAA-20) with nominal or high confidence. Detections classified as volcanoes, other static land sources or offshore are left out.</li>
  <li><strong>Comparing years.</strong> ${harmonizing
    ? "Counts are adjusted to what the full set of four satellites would have reported, using factors measured in the years the satellites overlap, so years with different satellites can be compared."
    : "Counts are as observed. More satellites see more fire: the record roughly triples when Suomi NPP joins in 2012 and rises again with NOAA-20 in 2018. Compare years within the same satellite era, or run the sensor calibration to adjust for this."}</li>
  <li><strong>Baseline.</strong> Differences are against the ${escape(baseline.short)} for the same calendar month.</li>
  <li><strong>Limits.</strong> Satellites miss fires under cloud or thick smoke, small or short-lived fires, and fires that burn between overpasses. A detection marks a sensor pixel (375 m to several km across), not an exact point.</li>
</ul>
<p class="muted">Data: NASA FIRMS, MODIS Collection 6.1 and VIIRS 375 m active fire products, distributed by NASA LANCE / EOSDIS. Borders: Natural Earth.
${link ? ` View live: <a href="${escape(link)}">${escape(link)}</a>.` : ""} Generated ${escape(new Date().toISOString().slice(0, 10))}.</p>
</main>
</body>
</html>
`;
}
