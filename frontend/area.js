// Areas of interest. Every area has a bounding box; a circle, a drawn outline
// or a country narrows it further on the server (see firms_pipeline/areas.py).
//
//   { kind: "box", bounds }                        preset or typed coordinates
//   { kind: "circle", lat, lon, km, bounds }        point and radius
//   { kind: "polygon", ring: [[lat, lon], …], bounds }
//   { kind: "country", name, bounds }

const kmPerDegree = 111.195;
const round = (value, digits = 4) => Number(value.toFixed(digits));
export const MAX_RADIUS_KM = 2000;
export const MAX_VERTICES = 120;

export function circleArea(lat, lon, km) {
  const dLat = km / kmPerDegree;
  // Near a pole the circle spans every longitude.
  const dLon = Math.abs(lat) + dLat >= 90 ? 180
    : Math.asin(Math.min(1, Math.sin(dLat * Math.PI / 180) / Math.cos(lat * Math.PI / 180))) * 180 / Math.PI;
  const west = lon - dLon;
  const east = lon + dLon;
  const wrap = (value) => ((value + 180) % 360 + 360) % 360 - 180;
  return {
    kind: "circle", lat: round(lat), lon: round(lon), km: round(km, 1),
    bounds: {
      min_lat: round(Math.max(-90, lat - dLat)), max_lat: round(Math.min(90, lat + dLat)),
      min_lon: dLon >= 180 ? -180 : round(wrap(west)), max_lon: dLon >= 180 ? 180 : round(wrap(east)),
    },
  };
}

export function polygonArea(ring) {
  const points = ring.map(([lat, lon]) => [round(lat), round(lon)]);
  const lats = points.map(([lat]) => lat);
  const lons = points.map(([, lon]) => lon);
  return {
    kind: "polygon", ring: points,
    bounds: { min_lat: Math.min(...lats), max_lat: Math.max(...lats), min_lon: Math.min(...lons), max_lon: Math.max(...lons) },
  };
}

// The extra query parameter that narrows the bounding box, if any.
export function shapeParams(area) {
  if (area.kind === "circle") return { circle: `${area.lat},${area.lon},${area.km}` };
  if (area.kind === "polygon") return { polygon: area.ring.flat().join(",") };
  if (area.kind === "country") return { country: area.name };
  return {};
}

// A short description of the shape for exports and the report.
export function shapeText(area) {
  if (area.kind === "circle") return `Within ${area.km} km of ${area.lat}, ${area.lon}`;
  if (area.kind === "polygon") return `Drawn outline with ${area.ring.length} corners: ${area.ring.map((point) => point.join(" ")).join("; ")}`;
  if (area.kind === "country") return `${area.name} (Natural Earth border, on the 0.1° calendar grid)`;
  return "Bounding box";
}

function destination(lat, lon, bearing, km) {
  const d = km / 6371.0088;
  const phi = lat * Math.PI / 180;
  const theta = bearing * Math.PI / 180;
  const phi2 = Math.asin(Math.sin(phi) * Math.cos(d) + Math.cos(phi) * Math.sin(d) * Math.cos(theta));
  const lambda = lon * Math.PI / 180 + Math.atan2(Math.sin(theta) * Math.sin(d) * Math.cos(phi), Math.cos(d) - Math.sin(phi) * Math.sin(phi2));
  return [lambda * 180 / Math.PI, phi2 * 180 / Math.PI];
}

// The outline to draw on the map, as GeoJSON line coordinates ([lon, lat]).
// Box edges are densified so they follow parallels on the globe. Countries
// are drawn from the basemap's own geometry instead, and the whole world has
// no outline.
export function outline(area) {
  if (area.kind === "circle") {
    return [Array.from({ length: 97 }, (_, i) => destination(area.lat, area.lon, i * 3.75, area.km))];
  }
  if (area.kind === "polygon") {
    return [[...area.ring, area.ring[0]].map(([lat, lon]) => [lon, lat])];
  }
  if (area.kind !== "box") return [];
  const { min_lat: south, max_lat: north, min_lon: west } = area.bounds;
  const east = area.bounds.max_lon < west ? area.bounds.max_lon + 360 : area.bounds.max_lon;
  if (east - west >= 359 && north - south >= 179) return [];
  const steps = Math.max(2, Math.ceil((east - west) / 2));
  const along = (lat, reverse) => Array.from({ length: steps + 1 }, (_, i) => {
    const t = (reverse ? steps - i : i) / steps;
    return [west + (east - west) * t, lat];
  });
  return [[...along(south, false), ...along(north, true), [west, south]]];
}

// Areas in links: "cambodia", "box:s,n,w,e", "circle:lat,lon,km",
// "poly:lat,lon,…" or "country:Name".
export function encodeArea(area, preset) {
  if (preset) return preset;
  if (area.kind === "circle") return `circle:${area.lat},${area.lon},${area.km}`;
  if (area.kind === "polygon") return `poly:${area.ring.flat().join(",")}`;
  if (area.kind === "country") return `country:${area.name}`;
  const { min_lat, max_lat, min_lon, max_lon } = area.bounds;
  return `box:${min_lat},${max_lat},${min_lon},${max_lon}`;
}

// Returns an area, { preset }, { country } or null for text that is not one.
export function decodeArea(text, presets) {
  if (!text) return null;
  if (Object.hasOwn(presets, text)) return { preset: text };
  const [kind, rest = ""] = [text.slice(0, text.indexOf(":")), text.slice(text.indexOf(":") + 1)];
  if (kind === "country") return rest ? { country: rest } : null;
  const numbers = rest.split(",").map(Number);
  if (!rest || numbers.some((value) => !Number.isFinite(value))) return null;
  const validLat = (value) => value >= -90 && value <= 90;
  const validLon = (value) => value >= -180 && value <= 180;
  if (kind === "box" && numbers.length === 4) {
    const [min_lat, max_lat, min_lon, max_lon] = numbers;
    if (!validLat(min_lat) || !validLat(max_lat) || min_lat > max_lat || !validLon(min_lon) || !validLon(max_lon)) return null;
    return { kind: "box", bounds: { min_lat, max_lat, min_lon, max_lon } };
  }
  if (kind === "circle" && numbers.length === 3) {
    const [lat, lon, km] = numbers;
    return validLat(lat) && validLon(lon) && km > 0 && km <= MAX_RADIUS_KM ? circleArea(lat, lon, km) : null;
  }
  if (kind === "poly" && numbers.length >= 6 && numbers.length % 2 === 0 && numbers.length <= 2 * MAX_VERTICES) {
    const ring = [];
    for (let i = 0; i < numbers.length; i += 2) ring.push([numbers[i], numbers[i + 1]]);
    return ring.every(([lat, lon]) => validLat(lat) && validLon(lon)) ? polygonArea(ring) : null;
  }
  return null;
}

// Thins a drawn outline to at most `limit` corners by dropping every nth one.
export function thin(ring, limit = MAX_VERTICES) {
  if (ring.length <= limit) return ring;
  const step = ring.length / limit;
  return Array.from({ length: limit }, (_, i) => ring[Math.floor(i * step)]);
}
