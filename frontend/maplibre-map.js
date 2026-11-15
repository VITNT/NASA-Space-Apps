import { addGeography, loadBasemap } from "./geography.js";
import { footprint } from "./footprint.js";
import { octagon, pointColors, pointOutline, pointSelection, wrapLongitude } from "./map-detail.js";

const mapLibreDist = "https://cdn.jsdelivr.net/npm/maplibre-gl@5.6.2/dist/";
const blueMarbleTiles = "https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/BlueMarble_ShadedRelief_Bathymetry/default/GoogleMapsCompatible_Level8/{z}/{y}/{x}.jpeg";
const fireLayer = "luminite-fire-grid";
const dotLayer = "luminite-fire-dots";
const pointLayer = "luminite-fire-points";
const footprintLayer = "luminite-fire-footprints";
const footprintOutline = "luminite-fire-footprint-lines";
const selectionLayer = "luminite-fire-selection";
const areaLayer = "luminite-area";
const draftLayer = "luminite-area-draft";
const draftCorners = "luminite-area-draft-corners";
const areaColor = "#bcd0ff";
const satelliteLayer = "luminite-blue-marble";
const emptyGrid = { type: "FeatureCollection", features: [] };
const mercatorLimit = 85.051129;
function loadAsset(element) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => finish(new Error("MapLibre took too long to load")), 12000);
    function finish(error) {
      clearTimeout(timer);
      if (error) {
        element.remove();
        reject(error);
      } else {
        resolve();
      }
    }
    element.crossOrigin = "anonymous";
    element.addEventListener("load", () => finish(), { once: true });
    element.addEventListener("error", () => finish(new Error("Could not load MapLibre")), { once: true });
    document.head.append(element);
  });
}

async function loadMapLibre() {
  const script = document.createElement("script");
  script.src = mapLibreDist + "maplibre-gl.js";
  script.integrity = "sha384-1qC0PnXk2fgMDybOtZF1ui4LM5Sxm7o0TbEkwfxTTVdlZvuNENNGCKUub7d644HP";
  const css = document.createElement("link");
  css.rel = "stylesheet";
  css.href = mapLibreDist + "maplibre-gl.css";
  css.integrity = "sha384-gNYNsUmuZqDYiT3gbirWTV5K7rt71RoveS/yXAaU09d4ZUmeDVTD3XoqB6uJAIFR";
  await Promise.all([loadAsset(script), loadAsset(css)]);
  return window.maplibregl;
}

const cellKey = (cell) => `${cell.west},${cell.south},${cell.size}`;

function cellFeature(cell, dots) {
  const properties = { key: cellKey(cell), color: cell.color, strength: cell.strength };
  if (dots) {
    if (Math.abs(cell.latitude) > mercatorLimit) return [];
    return [{ type: "Feature", properties, geometry: { type: "Point", coordinates: [cell.longitude, cell.latitude] } }];
  }
  if (cell.south >= mercatorLimit || cell.south + cell.size <= -mercatorLimit) return [];
  const ring = octagon(cell.south, cell.west, cell.size)
    .map(([lat, lon]) => [lon, Math.max(-mercatorLimit, Math.min(mercatorLimit, lat))]);
  return [{ type: "Feature", properties, geometry: { type: "Polygon", coordinates: [[...ring, ring[0]]] } }];
}

function waitForBasemap(map) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => finish(new Error("The dark basemap took too long to load")), 12000);
    const loaded = () => finish();
    const failed = ({ error, tile }) => {
      if (!tile) finish(error);
    };
    function finish(error) {
      clearTimeout(timer);
      map.off("load", loaded);
      map.off("error", failed);
      if (error) reject(error);
      else resolve();
    }
    map.on("load", loaded);
    map.on("error", failed);
  });
}

export async function createMapLibreMap(host, onSelect, onMove, onViewportChange, onSelectPoints) {
  const [maplibre, style] = await Promise.all([loadMapLibre(), loadBasemap()]);
  const map = new maplibre.Map({
    container: host,
    style,
    center: [0, 0],
    zoom: 1,
    attributionControl: false,
    canvasContextAttributes: { preserveDrawingBuffer: true },
    dragRotate: false,
    pitchWithRotate: false,
    touchPitch: false,
    maxPitch: 0,
  });
  let items = new Map();
  let points = [];
  let satellite = false;
  let flat = false;
  // Ends the drawing in progress, if any.
  let stopDrawing;

  function center(area) {
    const west = area.min_lon;
    const east = area.max_lon < west ? area.max_lon + 360 : area.max_lon;
    if (east - west >= 359) {
      const side = Math.min(host.clientWidth, host.clientHeight);
      const zoom = Math.log2(Math.max(1, side) / (flat ? 512 : 150));
      map.jumpTo({ center: [0, 0], zoom });
      return;
    }
    const south = Math.max(-mercatorLimit, Math.min(mercatorLimit, area.min_lat));
    const north = Math.min(mercatorLimit, Math.max(-mercatorLimit, area.max_lat));
    const side = Math.round(host.clientWidth * .12);
    map.fitBounds([[west, south], [east, north]], {
      animate: false,
      maxZoom: 11,
      padding: { top: 90, bottom: 140, left: side, right: side },
    });
  }

  function reportCenter() {
    const { lat, lng } = map.getCenter().wrap();
    onMove(lat, lng);
  }

  try {
    await waitForBasemap(map);
    map.setProjection({ type: "globe" });
    map.addSource(satelliteLayer, { type: "raster", tiles: [blueMarbleTiles], maxzoom: 8, tileSize: 256 });
    map.addLayer({
      id: satelliteLayer, type: "raster", source: satelliteLayer,
      layout: { visibility: "none" },
      paint: { "raster-brightness-max": .8, "raster-saturation": -.14 },
    }, "land");
    map.addSource(fireLayer, { type: "geojson", data: emptyGrid });
    map.addLayer({
      id: fireLayer, type: "fill", source: fireLayer,
      filter: ["==", ["geometry-type"], "Polygon"],
      paint: { "fill-color": ["to-color", ["get", "color"]], "fill-antialias": false },
    });
    map.addLayer({
      id: dotLayer, type: "circle", source: fireLayer,
      filter: ["==", ["geometry-type"], "Point"],
      paint: {
        "circle-color": ["to-color", ["get", "color"]],
        "circle-radius": ["interpolate", ["linear"], ["zoom"],
          0, ["+", 1.1, ["*", .9, ["get", "strength"]]],
          5, ["+", 1.5, ["*", 1.7, ["get", "strength"]]],
          10, ["+", 2.2, ["*", 2.2, ["get", "strength"]]]],
        "circle-stroke-color": pointOutline,
        "circle-stroke-width": .6,
      },
    });
    // The selected area's outline, and the one being drawn.
    map.addSource(areaLayer, { type: "geojson", data: emptyGrid });
    map.addLayer({
      id: areaLayer, type: "line", source: areaLayer,
      paint: { "line-color": areaColor, "line-width": 1.2, "line-opacity": .75, "line-dasharray": [3, 2] },
    });
    map.addSource(draftLayer, { type: "geojson", data: emptyGrid });
    map.addLayer({
      id: draftLayer, type: "line", source: draftLayer,
      filter: ["==", ["geometry-type"], "LineString"],
      paint: { "line-color": areaColor, "line-width": 2 },
    });
    map.addLayer({
      id: draftCorners, type: "circle", source: draftLayer,
      filter: ["==", ["geometry-type"], "Point"],
      paint: { "circle-color": "#10151b", "circle-radius": 4, "circle-stroke-color": areaColor, "circle-stroke-width": 2 },
    });
    // Close up, each detection is drawn as the sensor pixel that reported it,
    // at its true size on the ground. The fire is somewhere inside that pixel.
    const levelColor = ["match", ["get", "level"], 2, pointColors[2], 1, pointColors[1], pointColors[0]];
    map.addSource(footprintLayer, { type: "geojson", data: emptyGrid });
    map.addLayer({
      id: footprintLayer, type: "fill", source: footprintLayer, minzoom: 8.5,
      layout: { "fill-sort-key": ["get", "level"] },
      paint: {
        "fill-color": levelColor,
        "fill-opacity": ["interpolate", ["linear"], ["zoom"], 8.5, 0, 10, .62],
      },
    });
    map.addLayer({
      id: footprintOutline, type: "line", source: footprintLayer, minzoom: 8.5,
      paint: {
        "line-color": levelColor,
        "line-width": ["interpolate", ["linear"], ["zoom"], 9, .4, 13, 1.4],
        "line-opacity": ["interpolate", ["linear"], ["zoom"], 8.5, 0, 10, 1],
      },
    });
    // Farther out a pixel is smaller than a screen pixel, so each detection is
    // a dot at its reported centre, strongest on top. Detections without a
    // reported pixel size stay dots at every zoom.
    const dotOpacity = ["interpolate", ["linear"], ["zoom"], 10, 1, 11.5, ["case", ["get", "pixel"], 0, 1]];
    const pointRadius = (extra) => ["interpolate", ["linear"], ["zoom"], 0, 2.4 + extra, 6, 3.2 + extra, 10, 4.6 + extra, 14, 7 + extra];
    map.addSource(pointLayer, { type: "geojson", data: emptyGrid });
    map.addLayer({
      id: pointLayer, type: "circle", source: pointLayer,
      layout: { "circle-sort-key": ["get", "level"] },
      paint: {
        "circle-color": ["match", ["get", "level"], 2, pointColors[2], 1, pointColors[1], pointColors[0]],
        "circle-radius": pointRadius(0),
        "circle-opacity": dotOpacity,
        "circle-stroke-color": pointOutline,
        "circle-stroke-width": .8,
        "circle-stroke-opacity": dotOpacity,
      },
    });
    map.addSource(selectionLayer, { type: "geojson", data: emptyGrid });
    map.addLayer({
      id: selectionLayer, type: "circle", source: selectionLayer,
      paint: {
        "circle-color": "rgba(0, 0, 0, 0)",
        "circle-radius": pointRadius(5),
        "circle-stroke-color": pointSelection,
        "circle-stroke-width": 2,
      },
    });
  } catch (error) {
    map.remove();
    throw error;
  }

  const refreshGeography = addGeography(map);
  map.on("move", reportCenter);
  map.on("moveend", onViewportChange);
  for (const layer of [fireLayer, dotLayer, pointLayer, footprintLayer]) {
    map.on("mouseenter", layer, () => { if (!stopDrawing) map.getCanvas().style.cursor = "pointer"; });
    map.on("mouseleave", layer, () => { if (!stopDrawing) map.getCanvas().style.cursor = ""; });
  }
  map.on("click", (event) => {
    if (stopDrawing) return;
    const { x, y } = event.point;
    const near = (reach) => [[x - reach, y - reach], [x + reach, y + reach]];
    const distance = (item) => {
      const spot = map.project([item.longitude, item.latitude]);
      return Math.hypot(spot.x - x, spot.y - y);
    };
    // Detections within a fingertip of the pointer, nearest first.
    const indexes = new Set([
      ...map.queryRenderedFeatures(near(8), { layers: [pointLayer] }),
      ...map.queryRenderedFeatures(near(2), { layers: [footprintLayer] }),
    ].map((feature) => feature.properties.index));
    if (indexes.size) {
      onSelectPoints([...indexes].map((index) => points[index]).filter(Boolean).sort((a, b) => distance(a) - distance(b)));
      return;
    }
    const features = map.queryRenderedFeatures(event.point, { layers: [fireLayer] });
    const dots = map.queryRenderedFeatures(near(5), { layers: [dotLayer] })
      .map((feature) => items.get(feature.properties.key)).filter(Boolean).sort((a, b) => distance(a) - distance(b));
    const cell = dots[0] ?? items.get(features[0]?.properties.key);
    if (cell) onSelect(cell);
  });
  const canvas = map.getCanvas();
  canvas.setAttribute("aria-label", "Interactive fire map. Use arrow keys to move and plus or minus to zoom.");

  return {
    center,
    reportCenter,
    getViewport() {
      const area = map.getBounds();
      const west = area.getWest();
      const east = area.getEast();
      return [area.getSouth(), area.getNorth(), west, east < west ? east + 360 : east];
    },
    resize: () => map.resize(),
    changeZoom(factor) {
      map.zoomTo(map.getZoom() + Math.log2(factor), { duration: 180 });
    },
    setFlat(enabled) {
      flat = enabled;
      map.setProjection({ type: flat ? "mercator" : "globe" });
      refreshGeography();
    },
    setSatellite(enabled) {
      if (enabled === satellite) return;
      satellite = enabled;
      map.setLayoutProperty(satelliteLayer, "visibility", enabled ? "visible" : "none");
      map.setLayoutProperty("land", "visibility", enabled ? "none" : "visible");
      map.setPaintProperty("coast", "line-color", enabled ? "rgba(255, 255, 255, .55)" : "#33424e");
    },
    setCells(cells, dots) {
      items = new Map(cells.map((cell) => [cellKey(cell), cell]));
      map.getSource(fireLayer).setData({ type: "FeatureCollection", features: cells.flatMap((cell) => cellFeature(cell, dots)) });
    },
    setPoints(list) {
      points = list;
      map.getSource(selectionLayer).setData(emptyGrid);
      const dots = [];
      const pixels = [];
      list.forEach((item, index) => {
        if (Math.abs(item.latitude) > mercatorLimit) return;
        const corners = footprint(item);
        const properties = { index, level: item.level, pixel: Boolean(corners) };
        dots.push({ type: "Feature", properties, geometry: { type: "Point", coordinates: [item.longitude, item.latitude] } });
        if (corners) pixels.push({ type: "Feature", properties, geometry: { type: "Polygon", coordinates: [[...corners, corners[0]]] } });
      });
      map.getSource(pointLayer).setData({ type: "FeatureCollection", features: dots });
      map.getSource(footprintLayer).setData({ type: "FeatureCollection", features: pixels });
    },
    // True when detections are drawn as pixel footprints at the current zoom.
    showsFootprints: () => map.getZoom() >= 10,
    // Outlines the selected area. `lines` are arrays of [longitude, latitude].
    setArea(lines) {
      map.getSource(areaLayer).setData({
        type: "FeatureCollection",
        features: lines.map((coordinates) => ({ type: "Feature", properties: {}, geometry: { type: "LineString", coordinates } })),
      });
    },
    // Lets the user mark an area on the map. `kind` is "box" (drag a
    // rectangle), "polygon" (click each corner) or "point" (one click).
    // `done` receives bounds, a ring of [lat, lon] corners, or { lat, lon };
    // `cancelled` runs when Escape ends it. Returns a function that stops it.
    draw(kind, done, cancelled) {
      stopDrawing?.();
      const source = map.getSource(draftLayer);
      const corners = [];
      let start;
      let hover;
      const show = () => {
        const line = kind === "box" && start && hover
          ? [[start.lng, start.lat], [hover.lng, start.lat], [hover.lng, hover.lat], [start.lng, hover.lat], [start.lng, start.lat]]
          : [...corners, ...(hover ? [hover] : [])].map(({ lng, lat }) => [lng, lat]);
        source.setData({ type: "FeatureCollection", features: [
          ...(line.length > 1 ? [{ type: "Feature", properties: {}, geometry: { type: "LineString", coordinates: line } }] : []),
          ...corners.map(({ lng, lat }) => ({ type: "Feature", properties: {}, geometry: { type: "Point", coordinates: [lng, lat] } })),
        ] });
      };
      const handlers = [];
      const on = (type, handler) => { map.on(type, handler); handlers.push([type, handler]); };
      const stop = () => {
        for (const [type, handler] of handlers) map.off(type, handler);
        document.removeEventListener("keydown", key, true);
        map.doubleClickZoom.enable();
        map.getCanvas().style.cursor = "";
        source.setData(emptyGrid);
        if (stopDrawing === stop) stopDrawing = undefined;
      };
      const finish = (result) => {
        // Let the click that ended the drawing pass before selections resume.
        setTimeout(stop);
        done(result);
      };
      const finishPolygon = () => {
        if (corners.length >= 3) finish(corners.map(({ lng, lat }) => [lat, wrapLongitude(lng)]));
      };
      function key(event) {
        if (event.key === "Escape") {
          event.stopPropagation();
          stop();
          cancelled?.();
        } else if (event.key === "Enter" && kind === "polygon") {
          event.preventDefault();
          finishPolygon();
        }
      }
      stopDrawing = stop;
      document.addEventListener("keydown", key, true);
      map.doubleClickZoom.disable();
      map.getCanvas().style.cursor = "crosshair";
      if (kind === "box") {
        const begin = (event) => {
          if (event.originalEvent.touches?.length > 1) return;
          event.preventDefault();
          start = event.lngLat;
          hover = undefined;
        };
        const move = (event) => {
          if (!start) return;
          hover = event.lngLat;
          show();
        };
        const end = () => {
          if (!start) return;
          const [first, last] = [start, hover];
          start = undefined;
          if (!last || (first.lng === last.lng && first.lat === last.lat)) return;
          const west = Math.min(first.lng, last.lng);
          const east = Math.max(first.lng, last.lng);
          finish({
            min_lat: Math.max(-90, Math.min(first.lat, last.lat)), max_lat: Math.min(90, Math.max(first.lat, last.lat)),
            min_lon: east - west >= 360 ? -180 : wrapLongitude(west), max_lon: east - west >= 360 ? 180 : wrapLongitude(east),
          });
        };
        on("mousedown", begin);
        on("touchstart", begin);
        on("mousemove", move);
        on("touchmove", move);
        on("mouseup", end);
        on("touchend", end);
      } else if (kind === "polygon") {
        on("mousemove", (event) => {
          hover = event.lngLat;
          show();
        });
        on("click", (event) => {
          const near = (corner) => {
            const spot = map.project(corner);
            return Math.hypot(spot.x - event.point.x, spot.y - event.point.y) < 10;
          };
          // A click on the first corner, or a second click on the last, closes the outline.
          if (corners.length >= 3 && (near(corners[0]) || near(corners.at(-1)))) {
            finishPolygon();
            return;
          }
          if (corners.length && near(corners.at(-1))) return;
          corners.push(event.lngLat);
          hover = undefined;
          show();
        });
      } else {
        on("click", (event) => finish({ lat: event.lngLat.lat, lon: wrapLongitude(event.lngLat.lng) }));
      }
      return stop;
    },
    selectPoint(item) {
      map.getSource(selectionLayer).setData(item ? {
        type: "Feature", properties: {}, geometry: { type: "Point", coordinates: [item.longitude, item.latitude] },
      } : emptyGrid);
    },
    exportImage(callback) {
      const output = document.createElement("canvas");
      output.width = canvas.width;
      output.height = canvas.height;
      const context = output.getContext("2d");
      context.drawImage(canvas, 0, 0);
      const credit = `Natural Earth · geoBoundaries${satellite ? " · NASA GIBS" : ""}`;
      context.font = "12px sans-serif";
      context.fillStyle = "#10151b";
      context.fillRect(0, output.height - 22, Math.min(context.measureText(credit).width + 16, output.width), 22);
      context.fillStyle = "#f0f1f4";
      context.fillText(credit, 8, output.height - 7, output.width - 16);
      output.toBlob(callback, "image/png");
    },
  };
}
