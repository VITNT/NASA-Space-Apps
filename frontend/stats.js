// Statistics for comparing a period with its own history. Pure functions, so
// the calendar, History, the charts, the CSV exports and the report agree.

export function median(values) {
  const sorted = [...values].sort((a, b) => a - b);
  return (sorted[Math.floor((sorted.length - 1) / 2)] + sorted[Math.ceil((sorted.length - 1) / 2)]) / 2;
}

export function mean(values) {
  return values.reduce((sum, value) => sum + value, 0) / values.length;
}

// Linear-interpolated quantile, `q` from 0 to 1.
export function quantile(values, q) {
  const sorted = [...values].sort((a, b) => a - b);
  const position = (sorted.length - 1) * q;
  const lower = Math.floor(position);
  const upper = Math.ceil(position);
  return sorted[lower] + (sorted[upper] - sorted[lower]) * (position - lower);
}

// The baselines a month can be compared with. `years` picks the window of
// same-month values; `stat` their centre.
export const BASELINES = {
  "median-5": { stat: "median", years: 5, label: "Median of 5 earlier years", short: "5-year median" },
  "mean-5": { stat: "mean", years: 5, label: "Mean of 5 earlier years", short: "5-year mean" },
  "median-all": { stat: "median", years: Infinity, label: "Median of all earlier years", short: "median of all earlier years" },
  "mean-all": { stat: "mean", years: Infinity, label: "Mean of all earlier years", short: "mean of all earlier years" },
  "median-range": { stat: "median", years: "range", label: "Median of the whole range", short: "median of the range" },
  "mean-range": { stat: "mean", years: "range", label: "Mean of the whole range", short: "mean of the range" },
};

// Compares `value` with baseline `values`. Needs at least two of them.
// `delta` is the fractional difference from the centre, `z` the standard score
// (five or more values), `rank` the position among baseline and value together
// (1 = highest) and `percentile` the share of baseline values at or below it.
export function compare(value, values, stat = "median") {
  if (value == null || values.length < 2) return null;
  const centre = stat === "mean" ? mean(values) : median(values);
  const average = mean(values);
  const deviation = Math.sqrt(values.reduce((sum, item) => sum + (item - average) ** 2, 0) / (values.length - 1));
  return {
    centre,
    median: centre,
    years: values.length,
    delta: centre ? (value - centre) / centre : null,
    z: values.length >= 5 && deviation > 0 ? (value - average) / deviation : null,
    rank: 1 + values.filter((item) => item > value).length,
    of: values.length + 1,
    percentile: values.filter((item) => item <= value).length / values.length,
  };
}

const ordinalRules = new Intl.PluralRules("en", { type: "ordinal" });
export function ordinal(number) {
  return number + { one: "st", two: "nd", few: "rd", other: "th" }[ordinalRules.select(number)];
}

// "3rd highest of 12" style wording for a comparison.
export function rankText(result, noun = "years") {
  if (!result) return "";
  const { rank, of } = result;
  if (rank === 1) return `highest of ${of} ${noun}`;
  if (rank === of) return `lowest of ${of} ${noun}`;
  return rank <= of / 2 ? `${ordinal(rank)} highest of ${of} ${noun}` : `${ordinal(of - rank + 1)} lowest of ${of} ${noun}`;
}

// Totals per year from a Map of "YYYY-MM" → value. A year is `complete` when
// every one of its twelve months has a value.
export function annualTotals(values) {
  const years = new Map();
  for (const [period, value] of values) {
    const year = Number(period.slice(0, 4));
    const entry = years.get(year) ?? { year, total: 0, months: 0 };
    entry.total += value;
    entry.months += 1;
    years.set(year, entry);
  }
  return [...years.values()].sort((a, b) => a.year - b.year)
    .map((entry) => ({ ...entry, complete: entry.months === 12 }));
}

// The seasonal cycle: for each calendar month, the spread of its values over
// the years. Months with no values are null.
export function seasonalProfile(values) {
  const months = Array.from({ length: 12 }, () => []);
  for (const [period, value] of values) months[Number(period.slice(5)) - 1].push(value);
  return months.map((items, index) => items.length ? {
    month: index + 1,
    years: items.length,
    median: median(items),
    mean: mean(items),
    low: quantile(items, .1),
    high: quantile(items, .9),
    max: Math.max(...items),
  } : null);
}

// Months far from the same calendar month in the other years: a robust z on
// log(1 + value), so one extreme year does not hide another and a doubling
// counts alike in quiet and busy places. `values` maps "YYYY-MM" to a value.
// The thresholds match the regional anomaly map.
export const UNUSUAL = { z: 3, madFloor: .15, minYears: 8, minValue: 5 };
export function unusualMonths(values, options = UNUSUAL) {
  const byMonth = Array.from({ length: 12 }, () => []);
  for (const [period, value] of values) if (value != null) byMonth[Number(period.slice(5)) - 1].push([period, value]);
  const found = [];
  for (const items of byMonth) {
    for (const [period, value] of items) {
      const others = items.filter(([other]) => other !== period).map(([, item]) => item);
      if (others.length < options.minYears) continue;
      const logs = others.map((item) => Math.log1p(item));
      const centreLog = median(logs);
      const spread = median(logs.map((item) => Math.abs(item - centreLog)));
      const z = (Math.log1p(value) - centreLog) / Math.max(1.4826 * spread, options.madFloor);
      const centre = median(others);
      if (Math.abs(z) < options.z || Math.max(value, centre) < options.minValue) continue;
      found.push({ period, value, centre, z, delta: centre ? (value - centre) / centre : null, years: others.length });
    }
  }
  return found.sort((a, b) => Math.abs(b.z) - Math.abs(a.z));
}
