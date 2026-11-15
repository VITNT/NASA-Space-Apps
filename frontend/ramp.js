// Colour ramps shared by the calendar, History and the map.
//
// Both ramps change lightness evenly from end to end (CIE L* 28 → 91 for
// activity), so the order of classes survives colour-vision deficiency,
// greyscale printing and the dark theme. Each class names the text colour
// that keeps a 4.5:1 contrast on top of it. Values are always printed too,
// so colour is never the only cue.

const compact = new Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 1 });
const whole = new Intl.NumberFormat("en");

// Sequential: more detected activity is lighter and warmer.
export const ACTIVITY = [
  { color: "#6e2a3a", ink: "light", name: "Very low" },
  { color: "#9a3530", ink: "light", name: "Low" },
  { color: "#b84428", ink: "light", name: "Moderate" },
  { color: "#e5702c", ink: "dark", name: "High" },
  { color: "#f7a54d", ink: "dark", name: "Very high" },
  { color: "#fde3a1", ink: "dark", name: "Extreme" },
];

// Diverging: blue below the same-month median, orange above, grey near it.
// Blue against orange stays distinct for every common colour-vision type.
export const ANOMALY = [
  { color: "#7fb6e8", ink: "dark", name: "Far below median", max: -.5, label: "< −50%" },
  { color: "#3f6f9e", ink: "light", name: "Below median", max: -.15, label: "−50 to −15%" },
  { color: "#2a3442", ink: "light", name: "Near median", max: .15, label: "±15%" },
  { color: "#8f4a24", ink: "light", name: "Above median", max: .5, label: "+15 to +50%" },
  { color: "#e0672a", ink: "dark", name: "Well above median", max: 1, label: "+50 to +100%" },
  { color: "#fdc06a", ink: "dark", name: "Far above median", max: Infinity, label: "> +100%" },
];

// Classes follow the square root of value / peak, so quiet months still
// separate from zero while the busiest months keep the top classes.
export function activityClass(value, peak) {
  if (value == null) return null;
  if (value <= 0) return 0;
  return Math.min(ACTIVITY.length, Math.max(1, Math.ceil(ACTIVITY.length * Math.sqrt(value / Math.max(value, peak)))));
}

// The smallest value in each class, for the legend and exports.
export function activityBreaks(peak) {
  return ACTIVITY.map((_, index) => index === 0 ? 1 : Math.floor(peak * (index / ACTIVITY.length) ** 2) + 1);
}

export function activityLabel(level) {
  if (level == null) return "no satellite coverage";
  return level === 0 ? "none" : `${ACTIVITY[level - 1].name.toLowerCase()} (${level} of ${ACTIVITY.length})`;
}

export function anomalyClass(delta) {
  if (delta == null || !Number.isFinite(delta)) return null;
  return ANOMALY.findIndex((step) => delta < step.max || step.max === Infinity) + 1;
}

// A continuous colour along the activity ramp, for map cells.
export function activityColor(strength, alpha = 1) {
  const position = Math.max(0, Math.min(1, strength)) * (ACTIVITY.length - 1);
  const index = Math.min(ACTIVITY.length - 2, Math.floor(position));
  const mix = position - index;
  const channel = (hex, offset) => parseInt(hex.slice(1 + offset, 3 + offset), 16);
  const [r, g, b] = [0, 2, 4].map((offset) => Math.round(
    channel(ACTIVITY[index].color, offset) * (1 - mix) + channel(ACTIVITY[index + 1].color, offset) * mix));
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

function swatch(className, color, text, title) {
  const item = document.createElement("li");
  item.className = className;
  if (color) item.style.setProperty("--swatch", color);
  const label = document.createElement("span");
  label.textContent = text;
  item.append(label);
  if (title) item.title = title;
  return item;
}

// Draws a legend into a <ul>. `peak` sets the activity breaks; `anomaly`
// switches to the diverging ramp. The list is readable as text by screen readers.
export function drawLegend(list, { peak, anomaly = false, missing = "No coverage", zero = true, partial = false }) {
  const items = [];
  if (anomaly) {
    for (const step of ANOMALY) items.push(swatch("legend-step", step.color, step.label, step.name));
    items.push(swatch("legend-missing", null, "No baseline", "Fewer than two comparable earlier years"));
  } else {
    if (zero) items.push(swatch("legend-zero", null, "0", "Observed, no qualifying activity"));
    const breaks = activityBreaks(Math.max(1, peak));
    ACTIVITY.forEach((step, index) => {
      const upper = index + 1 < breaks.length ? breaks[index + 1] - 1 : Math.max(1, peak);
      if (upper < breaks[index]) return;
      items.push(swatch("legend-step", step.color, compact.format(breaks[index]),
        `${step.name}: ${whole.format(breaks[index])}–${whole.format(upper)}`));
    });
    items.push(swatch("legend-missing", null, missing, "No satellite in the selected record observed this period"));
    if (partial) items.push(swatch("legend-partial", null, "Partial", "Some satellites of the record had no data that day"));
  }
  list.replaceChildren(...items);
}
