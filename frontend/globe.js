import { bindMapZoom } from "./map-zoom.js";
import { createEarthRenderer } from "./earth-renderer.js";
import { cameraBounds, octagon, pointColors, pointOutline, pointSelection } from "./map-detail.js";

const radians = Math.PI / 180;
const degrees = 1 / radians;
const wrap = (value) => ((value + 180) % 360 + 360) % 360 - 180;
const globeRadius = (width, height) => Math.min(width * .43, height * .35);
const globeCenterY = .46;
const flatMapWidth = (width, height) => Math.max(width, height * 1.4);
const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
const flatZoomMin = 1;
const flatZoomMax = 48;
const maxZoomTurn = 3;

export function createGlobe(canvas, onSelect, onMove, onViewportChange, { pilot = false, onSelectPoints } = {}) {
  const ctx = canvas.getContext("2d");
  const earthCanvas = document.createElement("canvas");
  earthCanvas.id = `${canvas.id}-earth`;
  earthCanvas.setAttribute("aria-hidden", "true");
  canvas.before(earthCanvas);
  let renderer = createEarthRenderer(earthCanvas);
  const texture = document.createElement("canvas");
  const earth = new Image();
  let source;
  let longitude = 104.8;
  let latitude = 17;
  let zoom = 1;
  let bearing = 0;
  let flatZoom = 1;
  let flat = false;
  let steeringRemaining = 0;
  let zoomAnchor;
  let cells = [];
  let cellsByLocation = new Map();
  let cellSize;
  let dots = true;
  // Individual detections: unit vectors, and where each was last drawn on the canvas.
  let points = [];
  let pointVectors = new Float64Array(0);
  let pointScreen = new Float32Array(0);
  let selectedPoint = -1;
  // Flight only: the instrument swath under the spacecraft.
  let swath;
  // Canvas pixels per CSS pixel.
  let unit = 1;
  let frame;
  let drag;
  let geometry;
  let base;
  let reportedLatitude;
  let reportedLongitude;
  let reportedViewport;

  function loadFallbackImage() {
    if (!earth.naturalWidth || source) return;
    const pixels = texture.getContext("2d", { willReadFrequently: true });
    texture.width = earth.naturalWidth;
    texture.height = earth.naturalHeight;
    pixels.drawImage(earth, 0, 0);
    source = pixels.getImageData(0, 0, texture.width, texture.height);
  }

  earth.onload = () => {
    if (renderer && !renderer.setImage(earth)) renderer = null;
    if (!renderer) loadFallbackImage();
    geometry = undefined;
    redraw();
  };
  earth.src = "/static/assets/earth.jpg";

  earthCanvas.addEventListener("webglcontextlost", () => {
    renderer = null;
    loadFallbackImage();
    geometry = undefined;
    redraw();
  });

  function redraw() {
    if (frame === undefined) frame = requestAnimationFrame(() => draw());
  }

  function drawEarth(width, height, radius, centerX, centerY, sinLat, cosLat, sinBearing, cosBearing) {
    const image = ctx.createImageData(width, height);
    const output = image.data;
    const input = source?.data;
    for (let py = Math.max(0, Math.floor(centerY - radius)); py < Math.min(height, centerY + radius); py++) {
      const y = (centerY - py - .5) / radius;
      const half = radius * Math.sqrt(Math.max(0, 1 - y * y));
      for (let px = Math.max(0, Math.floor(centerX - half)); px < Math.min(width, centerX + half); px++) {
        const x = (px + .5 - centerX) / radius;
        const depth = 1 - x * x - y * y;
        if (depth <= 0) continue;
        const z = Math.sqrt(depth);
        const light = .32 + .58 * Math.max(0, -.48 * x + .42 * y + .77 * z);
        const index = (py * width + px) * 4;
        const east = x * cosBearing + y * sinBearing;
        const north = y * cosBearing - x * sinBearing;
        const u = Math.atan2(east, z * cosLat - north * sinLat) * degrees;
        const lon = wrap(longitude + u);
        const lat = Math.asin(clamp(north * cosLat + z * sinLat, -1, 1)) * degrees;
        const sx = Math.floor((lon + 180) / 360 * texture.width);
        const sy = Math.min(texture.height - 1, Math.floor((90 - lat) / 180 * texture.height));
        const sample = (sy * texture.width + sx) * 4;
        const rim = (1 - z) ** 4;
        output[index] = (input ? input[sample] : 29) * light + 15 * rim;
        output[index + 1] = (input ? input[sample + 1] : 52) * light + 43 * rim;
        output[index + 2] = (input ? input[sample + 2] : 80) * light + 82 * rim;
        output[index + 3] = Math.min(255, depth * radius * 148);
      }
    }
    return image;
  }

  function drawFlatMap(width, height, centerX, centerY, worldWidth) {
    ctx.clearRect(0, 0, width, height);
    const west = centerX - (longitude + 180) * worldWidth / 360;
    const north = centerY - (90 - latitude) * worldWidth / 360;
    ctx.fillStyle = "#131d2a";
    ctx.fillRect(0, north, width, worldWidth / 2);
    if (!earth.naturalWidth) return;
    // Draw the adjacent copy when the viewport crosses the antimeridian.
    const left = west > 0 ? west - worldWidth : west;
    for (let x = left; x < width; x += worldWidth) {
      ctx.drawImage(earth, x, north, worldWidth, worldWidth / 2);
    }
  }

  function drawFlatCells(width, height, centerX, centerY, worldWidth) {
    const pixelsPerDegree = worldWidth / 360;
    const size = cellSize * pixelsPerDegree;
    const inset = Math.min(.35, size * .035);
    for (const { cell } of cells) {
      const y = centerY - (cell.south + cellSize - latitude) * pixelsPerDegree;
      if (y + size < 0 || y > height) continue;
      const west = centerX + (cell.west - longitude) * pixelsPerDegree;
      const first = west + worldWidth * Math.ceil((-size - west) / worldWidth);
      ctx.fillStyle = cell.color;
      if (dots) outlineDots();
      for (let x = first; x < width; x += worldWidth) {
        if (dots) drawDot(cell, x + (cell.longitude - cell.west) * pixelsPerDegree,
          y + (cell.south + cellSize - cell.latitude) * pixelsPerDegree);
        else drawOctagon(x + size / 2, y + size / 2, size / 2 - inset);
      }
    }
  }

  function outlineDots() {
    ctx.strokeStyle = pointOutline;
    ctx.lineWidth = .7 * unit;
  }

  function drawDot(cell, x, y) {
    drawOctagon(x, y, (1.6 + cell.strength * 1.6) * unit);
    ctx.stroke();
  }

  // Every detection at its reported position, strongest on top.
  function drawPoints(width, height, centerX, centerY, radius, worldWidth, sinLat, cosLat, sinBearing, cosBearing) {
    const size = (2.4 + Math.min(2.6, Math.log2(flat ? flatZoom : zoom) * .55)) * unit;
    const margin = size + 2;
    pointScreen.fill(NaN);
    if (flat) {
      const pixelsPerDegree = worldWidth / 360;
      for (let i = 0; i < points.length; i++) {
        const y = centerY - (points[i].latitude - latitude) * pixelsPerDegree;
        if (y < -margin || y > height + margin) continue;
        let x = centerX + (points[i].longitude - longitude) * pixelsPerDegree;
        x += worldWidth * Math.ceil((-margin - x) / worldWidth);
        if (x > width + margin) continue;
        pointScreen[2 * i] = x;
        pointScreen[2 * i + 1] = y;
      }
    } else {
      const sinLon = Math.sin(longitude * radians);
      const cosLon = Math.cos(longitude * radians);
      for (let i = 0; i < points.length; i++) {
        const east = pointVectors[3 * i];
        const north = pointVectors[3 * i + 1];
        const up = pointVectors[3 * i + 2];
        const forward = north * cosLon + east * sinLon;
        if (up * sinLat + forward * cosLat <= 0) continue;
        const across = east * cosLon - north * sinLon;
        const above = up * cosLat - forward * sinLat;
        const x = centerX + radius * (across * cosBearing - above * sinBearing);
        const y = centerY - radius * (across * sinBearing + above * cosBearing);
        if (x < -margin || x > width + margin || y < -margin || y > height + margin) continue;
        pointScreen[2 * i] = x;
        pointScreen[2 * i + 1] = y;
      }
    }
    ctx.strokeStyle = pointOutline;
    ctx.lineWidth = .8 * unit;
    pointColors.forEach((color, level) => {
      ctx.beginPath();
      for (let i = 0; i < points.length; i++) {
        const x = pointScreen[2 * i];
        if (points[i].level !== level || x !== x) continue;
        ctx.moveTo(x + size, pointScreen[2 * i + 1]);
        ctx.arc(x, pointScreen[2 * i + 1], size, 0, 2 * Math.PI);
      }
      ctx.fillStyle = color;
      ctx.fill();
      ctx.stroke();
    });
    const x = selectedPoint < 0 ? NaN : pointScreen[2 * selectedPoint];
    if (x === x) {
      ctx.beginPath();
      ctx.arc(x, pointScreen[2 * selectedPoint + 1], size + 5 * unit, 0, 2 * Math.PI);
      ctx.lineWidth = 2 * unit;
      ctx.strokeStyle = pointSelection;
      ctx.stroke();
    }
  }

  // Detections drawn within a fingertip of the pointer, nearest first.
  function pointsAt(clientX, clientY) {
    const rect = canvas.getBoundingClientRect();
    const scale = canvas.width / rect.width;
    const x = (clientX - rect.left) * scale;
    const y = (clientY - rect.top) * scale;
    const reach = 8 * scale;
    const hits = [];
    for (let i = 0; i < points.length; i++) {
      const distance = Math.hypot(pointScreen[2 * i] - x, pointScreen[2 * i + 1] - y);
      if (distance <= reach) hits.push([distance, points[i]]);
    }
    return hits.sort((a, b) => a[0] - b[0]).map(([, point]) => point);
  }

  // A regular octagon with flat sides, `apothem` from centre to each side.
  function drawOctagon(x, y, apothem) {
    const cut = apothem * (Math.SQRT2 - 1);
    ctx.beginPath();
    ctx.moveTo(x - cut, y - apothem);
    ctx.lineTo(x + cut, y - apothem);
    ctx.lineTo(x + apothem, y - cut);
    ctx.lineTo(x + apothem, y + cut);
    ctx.lineTo(x + cut, y + apothem);
    ctx.lineTo(x - cut, y + apothem);
    ctx.lineTo(x - apothem, y + cut);
    ctx.lineTo(x - apothem, y - cut);
    ctx.closePath();
    ctx.fill();
  }

  function drawGlobeCells(width, height, centerX, centerY, radius, sinLat, cosLat, sinBearing, cosBearing) {
    const sinLon = Math.sin(longitude * radians);
    const cosLon = Math.cos(longitude * radians);
    function project({ east, north, up }) {
      const forward = north * cosLon + east * sinLon;
      const across = east * cosLon - north * sinLon;
      const above = up * cosLat - forward * sinLat;
      return {
        x: across * cosBearing - above * sinBearing,
        y: across * sinBearing + above * cosBearing,
        depth: up * sinLat + forward * cosLat,
      };
    }
    for (const { cell, corners, center } of cells) {
      if (dots) {
        outlineDots();
        const { x, y, depth } = project(center);
        if (depth <= 0) continue;
        ctx.fillStyle = cell.color;
        drawDot(cell, centerX + radius * x, centerY - radius * y);
        continue;
      }
      const projected = corners.map(project);
      if (projected.every(({ depth }) => depth <= 0)
        || projected.every(({ x }) => centerX + radius * x < 0)
        || projected.every(({ x }) => centerX + radius * x > width)
        || projected.every(({ y }) => centerY - radius * y < 0)
        || projected.every(({ y }) => centerY - radius * y > height)) continue;
      ctx.fillStyle = cell.color;
      ctx.beginPath();
      tracePolygon(projected, centerX, centerY, radius);
      ctx.fill();
    }
  }

  // The band an instrument images on one pass: `halfWidth` degrees of arc to
  // each side of the great circle through the spacecraft along `heading`.
  function drawSwath(centerX, centerY, radius, sinLat, cosLat, sinBearing, cosBearing) {
    const sinLon = Math.sin(longitude * radians);
    const cosLon = Math.cos(longitude * radians);
    const p = spherePoint(swath.lat, swath.lon);
    const lat = swath.lat * radians;
    const lon = swath.lon * radians;
    const heading = swath.heading * radians;
    // Unit vectors toward north and east at the spacecraft, then along the track.
    const north = [-Math.sin(lat) * Math.sin(lon), -Math.sin(lat) * Math.cos(lon), Math.cos(lat)];
    const east = [Math.cos(lon), -Math.sin(lon), 0];
    const start = [p.east, p.north, p.up];
    const along = north.map((n, i) => n * Math.cos(heading) + east[i] * Math.sin(heading));
    const side = [
      start[1] * along[2] - start[2] * along[1],
      start[2] * along[0] - start[0] * along[2],
      start[0] * along[1] - start[1] * along[0],
    ];
    function screen(offset, t) {
      const c = Math.cos(t);
      const s = Math.sin(t);
      const cw = Math.cos(offset);
      const sw = Math.sin(offset);
      const [x, y, z] = start.map((v, i) => (v * c + along[i] * s) * cw + side[i] * sw);
      const forward = y * cosLon + x * sinLon;
      const across = x * cosLon - y * sinLon;
      const above = z * cosLat - forward * sinLat;
      return {
        x: across * cosBearing - above * sinBearing,
        y: across * sinBearing + above * cosBearing,
        depth: z * sinLat + forward * cosLat,
      };
    }
    const reach = 70 * radians;
    const steps = 48;
    const edge = (offset) => Array.from({ length: steps + 1 }, (_, i) => screen(offset, -reach + 2 * reach * i / steps));
    const left = edge(-swath.halfWidth * radians);
    const right = edge(swath.halfWidth * radians);
    // Fill the band, cutting it at the limb as the cells are.
    ctx.beginPath();
    tracePolygon([...left, ...[...right].reverse()], centerX, centerY, radius);
    ctx.fillStyle = "#8fd8ff24";
    ctx.fill();
    ctx.lineWidth = 1.5 * unit;
    ctx.strokeStyle = "#b4e6ffc0";
    for (const line of [left, right]) strokeVisible(line, centerX, centerY, radius);
    ctx.setLineDash([5 * unit, 6 * unit]);
    ctx.strokeStyle = "#a8e1ff66";
    strokeVisible(edge(0), centerX, centerY, radius);
    ctx.setLineDash([]);
  }

  function strokeVisible(line, centerX, centerY, radius) {
    ctx.beginPath();
    let drawing = false;
    for (const point of line) {
      if (point.depth <= 0) {
        drawing = false;
        continue;
      }
      const x = centerX + radius * point.x;
      const y = centerY - radius * point.y;
      if (drawing) ctx.lineTo(x, y);
      else ctx.moveTo(x, y);
      drawing = true;
    }
    ctx.stroke();
  }

  // Adds a projected outline to the current path, following the limb where it
  // passes behind the Earth.
  function tracePolygon(projected, centerX, centerY, radius) {
    let previous = projected.at(-1);
    for (const point of projected) {
      if ((previous.depth > 0) !== (point.depth > 0)) {
        const fraction = previous.depth / (previous.depth - point.depth);
        const x = previous.x + fraction * (point.x - previous.x);
        const y = previous.y + fraction * (point.y - previous.y);
        const limb = radius / Math.hypot(x, y);
        ctx.lineTo(centerX + limb * x, centerY - limb * y);
      }
      if (point.depth > 0) ctx.lineTo(centerX + radius * point.x, centerY - radius * point.y);
      previous = point;
    }
    ctx.closePath();
  }

  function cellAt(clientX, clientY) {
    if (!cellSize) return;
    const rect = canvas.getBoundingClientRect();
    const x = clientX - rect.left - rect.width / 2;
    const y = rect.top + rect.height * (flat ? .5 : globeCenterY) - clientY;
    let lat;
    let lon;
    if (flat) {
      const degreesPerPixel = 360 / (flatMapWidth(rect.width, rect.height) * flatZoom);
      lat = latitude + y * degreesPerPixel;
      lon = longitude + x * degreesPerPixel;
    } else {
      const radius = globeRadius(rect.width, rect.height) * zoom;
      const sinBearing = Math.sin(bearing * radians);
      const cosBearing = Math.cos(bearing * radians);
      const across = (x * cosBearing + y * sinBearing) / radius;
      const up = (y * cosBearing - x * sinBearing) / radius;
      const depth = 1 - across * across - up * up;
      if (depth <= 0) return;
      const forward = Math.sqrt(depth);
      const sinLat = Math.sin(latitude * radians);
      const cosLat = Math.cos(latitude * radians);
      lat = Math.asin(clamp(up * cosLat + forward * sinLat, -1, 1)) * degrees;
      lon = longitude + Math.atan2(across, forward * cosLat - up * sinLat) * degrees;
    }
    if (Math.abs(lat) > 90) return;
    const south = Math.min(90 - cellSize, Math.floor(lat / cellSize) * cellSize);
    const west = Math.floor(wrap(lon) / cellSize) * cellSize;
    return cellsByLocation.get(`${south},${west}`);
  }

  function getViewport() {
    const { width, height } = canvas.getBoundingClientRect();
    if (!width || !height) return;
    if (flat) {
      const scale = 180 / (flatMapWidth(width, height) * flatZoom);
      return cameraBounds(latitude, longitude, height * scale, width * scale);
    }
    const diagonal = Math.hypot(width / 2, height * (1 - globeCenterY));
    const angle = Math.asin(Math.min(1, diagonal / (globeRadius(width, height) * zoom)));
    const lonRadius = Math.abs(latitude) + angle * degrees >= 90
      ? 180 : Math.asin(Math.sin(angle) / Math.cos(latitude * radians)) * degrees;
    return cameraBounds(latitude, longitude, angle * degrees, lonRadius);
  }

  function draw(fullResolution = false) {
    frame = undefined;
    const box = canvas.getBoundingClientRect();
    if (!box.width || !box.height) return;
    const pixelLimit = renderer || fullResolution ? 1200 : 480;
    const scale = Math.min(1, pixelLimit / Math.max(box.width, box.height));
    const width = Math.max(1, Math.round(box.width * scale));
    const height = Math.max(1, Math.round(box.height * scale));
    unit = width / box.width;
    if (canvas.width !== width || canvas.height !== height) {
      canvas.width = width;
      canvas.height = height;
    }
    const radius = globeRadius(width, height) * zoom;
    const centerX = width / 2;
    const centerY = height * (flat ? .5 : globeCenterY);
    const worldWidth = flatMapWidth(width, height) * flatZoom;
    const sinLat = Math.sin(latitude * radians);
    const cosLat = Math.cos(latitude * radians);
    const sinBearing = Math.sin(bearing * radians);
    const cosBearing = Math.cos(bearing * radians);
    const key = [width, height, zoom, latitude, longitude, bearing].join(",");
    if (flat) {
      drawFlatMap(width, height, centerX, centerY, worldWidth);
    } else if (renderer) {
      renderer.draw(width, height, radius, centerX, centerY, sinLat, cosLat, longitude, sinBearing, cosBearing);
      ctx.clearRect(0, 0, width, height);
    } else {
      if (geometry !== key) {
        base = drawEarth(width, height, radius, centerX, centerY, sinLat, cosLat, sinBearing, cosBearing);
        geometry = key;
      }
      ctx.putImageData(base, 0, 0);
    }
    if (swath && !flat) drawSwath(centerX, centerY, radius, sinLat, cosLat, sinBearing, cosBearing);
    if (cells.length) {
      if (flat) drawFlatCells(width, height, centerX, centerY, worldWidth);
      else drawGlobeCells(width, height, centerX, centerY, radius, sinLat, cosLat, sinBearing, cosBearing);
    }
    if (points.length) {
      drawPoints(width, height, centerX, centerY, radius, worldWidth, sinLat, cosLat, sinBearing, cosBearing);
    }
    if (latitude !== reportedLatitude || longitude !== reportedLongitude) {
      reportedLatitude = latitude;
      reportedLongitude = longitude;
      onMove(latitude, longitude);
    }
    const viewport = [key, flat, flatZoom].join(",");
    if (viewport !== reportedViewport) {
      reportedViewport = viewport;
      onViewportChange();
    }
  }

  function setCells(items, showDots) {
    dots = showDots;
    cellSize = items[0]?.size;
    cellsByLocation = new Map(items.map((cell) => [`${cell.south},${cell.west}`, cell]));
    cells = items.map((cell) => {
      if (dots) return { cell, center: spherePoint(cell.latitude, cell.longitude) };
      const corners = octagon(cell.south, cell.west, cell.size).map(([lat, lon]) => spherePoint(lat, lon));
      return { cell, corners };
    });
    redraw();
  }

  if (pilot) {
    new ResizeObserver(redraw).observe(canvas);
    return {
      setCamera(lat, lon, scale, heading) {
        latitude = lat;
        longitude = lon;
        zoom = scale;
        bearing = heading;
        // Flight already updates inside an animation frame. Draw the Earth and
        // its activity before the spacecraft's new pose reaches the screen.
        if (frame !== undefined) cancelAnimationFrame(frame);
        draw();
      },
      // Degrees along the surface from the view centre down to the point drawn
      // `offsetY` pixels below the top of the canvas, at zoom `scale`.
      lead(offsetY, scale) {
        const { width, height } = canvas.getBoundingClientRect();
        const radius = globeRadius(width, height) * scale;
        if (!radius) return 0;
        return Math.asin(clamp((offsetY - height * globeCenterY) / radius, -.9, .9)) * degrees;
      },
      // { lat, lon, heading, halfWidth } in degrees, or null for none. Drawn on
      // the next setCamera.
      setSwath(next) {
        swath = next;
      },
      setCells,
      getViewport,
      cellAt,
    };
  }

  function rotate(dx, dy) {
    longitude = wrap(longitude + dx);
    latitude = Math.max(-85, Math.min(85, latitude + dy));
    redraw();
  }

  function steerZoom(next, clientX, clientY) {
    if (!steeringRemaining || !Number.isFinite(clientX) || !Number.isFinite(clientY)) {
      zoomAnchor = undefined;
      return;
    }
    const box = canvas.getBoundingClientRect();
    const radius = globeRadius(box.width, box.height);
    if (!radius) return;
    const x = (clientX - box.left - box.width / 2) / radius;
    const y = (box.top + box.height * globeCenterY - clientY) / radius;
    if (!zoomAnchor || zoomAnchor.x !== x || zoomAnchor.y !== y) {
      const across = x / zoom;
      const up = y / zoom;
      const depth = 1 - across * across - up * up;
      zoomAnchor = undefined;
      if (depth <= 0) return;

      // Unproject the cursor onto the visible side of the sphere.
      const sinLat = Math.sin(latitude * radians);
      const cosLat = Math.cos(latitude * radians);
      const forward = Math.sqrt(depth);
      const horizontal = forward * cosLat - up * sinLat;
      const vertical = up * cosLat + forward * sinLat;
      zoomAnchor = { x, y, vertical, horizontal, across, lon: longitude + Math.atan2(across, horizontal) * degrees };
    }

    const across = x / next;
    const up = y / next;
    const depth = 1 - across * across - up * up;
    const { vertical, horizontal, across: initialAcross, lon } = zoomAnchor;
    const horizontalSquared = horizontal * horizontal + initialAcross * initialAcross - across * across;
    if (depth <= 0 || horizontalSquared <= 0) return;

    // Solve for the new center that puts the same surface point under the cursor, with north up.
    const forward = Math.sqrt(depth);
    const nextHorizontal = (horizontal < 0 ? -1 : 1) * Math.sqrt(horizontalSquared);
    const targetLon = lon - Math.atan2(across, nextHorizontal) * degrees;
    const targetLat = (Math.atan2(vertical, nextHorizontal) - Math.atan2(up, forward)) * degrees;
    const dx = wrap(targetLon - longitude);
    const dy = clamp(wrap(targetLat), -85, 85) - latitude;
    const distance = Math.hypot(dx, dy);
    if (distance < 1e-8) return;
    const turn = Math.min(.5, steeringRemaining, distance);
    steeringRemaining -= turn;
    longitude = wrap(longitude + dx * turn / distance);
    latitude += dy * turn / distance;
  }

  function changeZoom(factor, clientX, clientY) {
    if (flat) {
      const next = clamp(flatZoom * factor, flatZoomMin, flatZoomMax);
      if (next === flatZoom) return false;
      if (Number.isFinite(clientX) && Number.isFinite(clientY)) {
        const box = canvas.getBoundingClientRect();
        const degreesPerPixel = 360 * (1 / flatZoom - 1 / next) / flatMapWidth(box.width, box.height);
        longitude = wrap(longitude + (clientX - box.left - box.width / 2) * degreesPerPixel);
        latitude = clamp(latitude - (clientY - box.top - box.height / 2) * degreesPerPixel, -85, 85);
      }
      flatZoom = next;
      redraw();
      return;
    }
    const next = Math.min(7, Math.max(1, zoom * factor));
    if (next === zoom) return false;
    steerZoom(next, clientX, clientY);
    zoom = next;
    redraw();
  }

  canvas.addEventListener("pointerdown", (event) => {
    if (event.button !== 0 || drag) return;
    cancelZoom();
    drag = { id: event.pointerId, x: event.clientX, y: event.clientY, moved: false };
    canvas.setPointerCapture(event.pointerId);
  });
  canvas.addEventListener("pointermove", (event) => {
    if (!drag || drag.id !== event.pointerId) return;
    const dx = event.clientX - drag.x;
    const dy = event.clientY - drag.y;
    if (Math.hypot(dx, dy) < 2 && !drag.moved) return;
    let sensitivity = .35 / zoom;
    if (flat) {
      const box = canvas.getBoundingClientRect();
      sensitivity = 360 / (flatMapWidth(box.width, box.height) * flatZoom);
    }
    rotate(-dx * sensitivity, dy * sensitivity);
    Object.assign(drag, { x: event.clientX, y: event.clientY, moved: true });
  });
  canvas.addEventListener("pointerup", (event) => {
    if (!drag || drag.id !== event.pointerId) return;
    if (!drag.moved) {
      const hits = pointsAt(event.clientX, event.clientY);
      const cell = hits.length ? undefined : cellAt(event.clientX, event.clientY);
      if (hits.length) onSelectPoints?.(hits);
      else if (cell) onSelect(cell);
    }
    drag = undefined;
  });
  canvas.addEventListener("lostpointercapture", (event) => {
    if (drag?.id === event.pointerId) drag = undefined;
  });
  const { cancelZoom, zoomBy } = bindMapZoom(canvas, changeZoom, {
    getFlatScale: () => flat ? { current: flatZoom, min: flatZoomMin, max: flatZoomMax } : null,
    onStart() {
      drag = undefined;
      steeringRemaining = maxZoomTurn;
      zoomAnchor = undefined;
    },
  });
  canvas.addEventListener("keydown", (event) => {
    if (event.key === "+" || event.key === "=" || event.key === "-") {
      if (event.ctrlKey || event.metaKey || event.altKey) return;
      event.preventDefault();
      zoomBy(event.key === "-" ? 1 / 1.35 : 1.35);
      return;
    }
    const arrows = { ArrowLeft: [-8, 0], ArrowRight: [8, 0], ArrowUp: [0, 8], ArrowDown: [0, -8] };
    if (!arrows[event.key]) return;
    event.preventDefault();
    cancelZoom();
    rotate(...arrows[event.key].map((step) => flat ? step / flatZoom : step));
  });
  new ResizeObserver(redraw).observe(canvas);

  return {
    changeZoom: zoomBy,
    getViewport,
    setFlat(enabled) {
      cancelZoom();
      flat = enabled;
      earthCanvas.hidden = flat;
      canvas.setAttribute("aria-label", `Interactive ${flat ? "2D map. Drag or use the arrow keys to pan." : "Earth globe. Drag or use the arrow keys to rotate."} Pinch, scroll, or press plus and minus to zoom.`);
      redraw();
    },
    exportImage(callback) {
      if (frame !== undefined) cancelAnimationFrame(frame);
      draw(true);
      const image = document.createElement("canvas");
      image.width = canvas.width;
      image.height = canvas.height;
      const exportContext = image.getContext("2d");
      if (renderer && !flat) exportContext.drawImage(earthCanvas, 0, 0);
      exportContext.drawImage(canvas, 0, 0);
      image.toBlob(callback, "image/png");
      if (!renderer) redraw();
    },
    center(bounds) {
      cancelZoom();
      const lonSpan = (bounds.max_lon - bounds.min_lon + 360) % 360 || 360;
      const latSpan = bounds.max_lat - bounds.min_lat;
      longitude = wrap(bounds.min_lon + lonSpan / 2);
      latitude = (bounds.min_lat + bounds.max_lat) / 2;
      zoom = lonSpan >= 359 ? 1.5 : Math.min(4, Math.max(1, 54 / Math.max(lonSpan, latSpan)));
      const box = canvas.parentElement.getBoundingClientRect();
      const worldWidth = flatMapWidth(box.width, box.height);
      flatZoom = worldWidth
        ? clamp(300 * Math.min(box.width / Math.max(lonSpan, 1), box.height / Math.max(latSpan, 1)) / worldWidth, 1, 32)
        : 1;
      redraw();
    },
    setCells,
    setPoints(items) {
      points = items;
      selectedPoint = -1;
      pointScreen = new Float32Array(2 * items.length).fill(NaN);
      pointVectors = new Float64Array(3 * items.length);
      items.forEach((item, i) => {
        const { east, north, up } = spherePoint(item.latitude, item.longitude);
        pointVectors.set([east, north, up], 3 * i);
      });
      redraw();
    },
    selectPoint(item) {
      selectedPoint = points.indexOf(item);
      redraw();
    },
  };
}

function spherePoint(latitude, longitude) {
  const lat = latitude * radians;
  const lon = longitude * radians;
  const cosLat = Math.cos(lat);
  return {
    east: cosLat * Math.sin(lon), north: cosLat * Math.cos(lon), up: Math.sin(lat),
  };
}

