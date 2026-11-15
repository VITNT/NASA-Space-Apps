import { createGlobe } from "./globe.js";
import { loadTerraModel } from "./terra-model.js";

const radians = Math.PI / 180;
const compass = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"];
const clamp = (value, min, max) => Math.min(max, Math.max(min, value));
const wrap = (value, span) => ((value % span) + span) % span;
const wrapLongitude = (value) => wrap(value + 180, 360) - 180;
const start = { lat: 12.55, lon: 104.85, heading: 35 };
const demoDate = "2025-03-08";
// Flight feel, modelled on a small rigid body: speed is a share of cruise (1 =
// cruise), and cruise covers the same distance on screen at every zoom.
const flight = {
  cruise: 52,          // degrees per second at cruise, at zoom 1
  boost: 1.7,          // top speed with boost ...
  reverse: .35,        // ... and backwards
  accelS: .45,         // how quickly speed follows the keys (s) ...
  thrust: 1.6,         // ... within what the thrusters can push (per s)
  brake: 3.2,          // thrusting against the motion
  easeS: .9,           // faster than asked (boost released): ease back down (s)
  drag: .3,            // keys released: a steady pull to a full stop ...
  dragV: .9,           // ... plus this much per unit of speed
  turn: 70,            // turning rate at rest and at cruise (degrees per second)
  turnFast: 44,
  turnS: .18,          // how quickly a turn starts and stops (s)
  gripS: .2,           // how long sideways drift survives a turn (s)
  zoomRate: 1.4,       // held zoom keys (per s, exponential)
};
const records = {
  terra: { dataset: "modis", label: "Terra / MODIS" },
  aqua: { dataset: "modis", label: "Aqua / MODIS" },
  noaa20: { dataset: "viirs_noaa20", label: "NOAA-20 / VIIRS" },
  snpp: { dataset: "viirs_snpp", label: "Suomi NPP / VIIRS" },
};
const keys = {
  KeyW: "forward", ArrowUp: "forward",
  KeyS: "reverse", ArrowDown: "reverse",
  KeyA: "left", ArrowLeft: "left",
  KeyD: "right", ArrowRight: "right",
  ShiftLeft: "boost", ShiftRight: "boost",
  Equal: "zoomIn", NumpadAdd: "zoomIn",
  Minus: "zoomOut", NumpadSubtract: "zoomOut",
};

function destination({ lat, lon }, heading, distance) {
  const latitude = lat * radians;
  const bearing = heading * radians;
  const arc = distance * radians;
  const nextLat = Math.asin(clamp(
    Math.sin(latitude) * Math.cos(arc) + Math.cos(latitude) * Math.sin(arc) * Math.cos(bearing), -1, 1,
  ));
  const deltaLon = Math.atan2(
    Math.sin(bearing) * Math.sin(arc) * Math.cos(latitude),
    Math.cos(arc) - Math.sin(latitude) * Math.sin(nextLat),
  );
  return { lat: nextLat / radians, lon: wrapLongitude(lon + deltaLon / radians) };
}

function bearingFrom(from, to) {
  const a = from.lat * radians;
  const b = to.lat * radians;
  const delta = (to.lon - from.lon) * radians;
  return Math.atan2(
    Math.sin(delta) * Math.cos(b),
    Math.cos(a) * Math.sin(b) - Math.sin(a) * Math.cos(b) * Math.cos(delta),
  ) / radians;
}

function positionLabel({ lat, lon }) {
  return `${Math.abs(lat).toFixed(2)}° ${lat < 0 ? "S" : "N"} · ${Math.abs(lon).toFixed(2)}° ${lon < 0 ? "W" : "E"}`;
}

// One step of the flight model. `state` holds lat, lon, heading (degrees),
// speed and slide (shares of cruise, along and across the heading) and
// turnRate (degrees per second); `input` holds thrust and turn (-1, 0, 1) and boost.
export function stepFlight(state, input, dt, zoom) {
  const { thrust, turn, boost } = input;
  // Steering: nimble at rest, calmer at speed, eased in and out.
  const pace = Math.min(1, Math.abs(state.speed));
  const steer = turn * (flight.turn + (flight.turnFast - flight.turn) * pace);
  state.turnRate += (steer - state.turnRate) * (1 - Math.exp(-dt / flight.turnS));
  if (!turn && Math.abs(state.turnRate) < .05) state.turnRate = 0;
  const turned = state.turnRate * dt;
  state.heading = wrap(state.heading + turned, 360);
  // The velocity stays put while the craft turns under it, then the drift fades.
  const cos = Math.cos(turned * radians);
  const sin = Math.sin(turned * radians);
  const grip = Math.exp(-dt / flight.gripS);
  let along = state.speed * cos + state.slide * sin;
  let across = (state.slide * cos - state.speed * sin) * grip;
  if (Math.abs(across) < .002) across = 0;
  if (thrust) {
    const want = thrust > 0 ? (boost ? flight.boost : 1) : -flight.reverse;
    const over = (along - want) * thrust > 0;
    const limit = along * thrust < 0 ? flight.brake : flight.thrust;
    along += clamp((want - along) / (over ? flight.easeS : flight.accelS), -limit, limit) * dt;
  } else {
    // Coast to a definite stop instead of creeping forever.
    const slowed = (flight.drag + flight.dragV * Math.abs(along)) * dt;
    along = Math.abs(along) <= slowed ? 0 : along - Math.sign(along) * slowed;
  }
  state.speed = along;
  state.slide = across;
  const distance = Math.hypot(along, across) * flight.cruise / zoom * dt;
  if (distance > 0) {
    const drift = Math.atan2(across, along) / radians;
    const next = destination(state, state.heading + drift, distance);
    // Carry the heading along the great circle just flown.
    state.heading = wrap(bearingFrom(next, state) + 180 - drift, 360);
    state.lat = next.lat;
    state.lon = next.lon;
  }
}

// Detections are loaded for the corridor between the craft and the view centre.
function observationBounds({ lat, lon }, zoom, lead) {
  const step = zoom >= 3.5 ? 2 : 8;
  const margin = step * Math.max(1.5, Math.ceil(lead / 2 / step) + .5);
  const centerLat = Math.round(lat / step) * step;
  const centerLon = wrapLongitude(Math.round(lon / step) * step);
  const width = margin / Math.max(.12, Math.cos(centerLat * radians));
  const polar = Math.abs(centerLat) + margin >= 90;
  return {
    min_lat: Math.max(-90, centerLat - margin),
    max_lat: Math.min(90, centerLat + margin),
    min_lon: polar ? -180 : wrapLongitude(centerLon - width),
    max_lon: polar ? 180 : wrapLongitude(centerLon + width),
  };
}

export function createSatelliteFlight(root, api, onExit) {
  const get = (id) => root.querySelector(`#${id}`);
  const canvas = get("flight-globe");
  const date = get("flight-date");
  const sensor = get("flight-sensor");
  const dataStatus = get("flight-data-status");
  const detail = get("flight-detection");
  const position = get("flight-position");
  const heading = get("flight-heading");
  const speedReadout = get("flight-speed");
  const craft = root.querySelector(".flight-craft");
  const terraCanvas = get("flight-terra");
  const fallbackCraft = craft.querySelector(".flight-craft-model");
  const modelCredit = get("flight-model-credit");
  const buttons = [...root.querySelectorAll("[data-flight-control]")];
  const globe = createGlobe(canvas, () => {}, () => {}, () => {}, { pilot: true });
  const pressed = new Map();
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  let state;
  let bank;
  let craftY;
  let zoom;
  let targetZoom;
  let markerPress;
  let previousTime;
  let model;
  let frame;
  let active = false;
  let dirty = true;
  let lastWindow;
  let request;
  let timer;
  const observationCache = new Map();
  loadTerraModel(terraCanvas).then((loaded) => {
    if (!loaded) return;
    model = loaded;
    terraCanvas.hidden = false;
    modelCredit.hidden = false;
    fallbackCraft.hidden = true;
    invalidate();
  }).catch((error) => console.warn("Terra model:", error.message));
  terraCanvas.addEventListener("webglcontextlost", () => {
    model = undefined;
    terraCanvas.hidden = true;
    modelCredit.hidden = true;
    fallbackCraft.hidden = false;
  });

  function isHeld(control) {
    for (const value of pressed.values()) if (value === control) return true;
    return false;
  }

  function scheduleFrame() {
    if (active && !document.hidden && frame === undefined) frame = requestAnimationFrame(tick);
  }

  function invalidate() {
    dirty = true;
    scheduleFrame();
  }

  function showObservations({ rows, capped }) {
    globe.setDetections(rows);
    dataStatus.textContent = rows.length
      ? `${rows.length.toLocaleString()} nearby detections loaded${capped ? " · limited result" : ""}`
      : "No nearby detections in this selection.";
  }

  function clearInput() {
    pressed.clear();
    markerPress = undefined;
    for (const button of buttons) delete button.dataset.held;
  }

  function clearObservations() {
    clearTimeout(timer);
    request?.abort();
    lastWindow = undefined;
    detail.hidden = true;
    globe.setDetections([]);
  }

  function refreshObservations(center, lead) {
    if (!date.value || !date.validity.valid) {
      dataStatus.textContent = "Choose a date in the archive.";
      return;
    }
    const bounds = observationBounds(center, zoom, lead);
    const day = date.value;
    const filter = sensor.value;
    const key = JSON.stringify({ bounds, day, filter });
    if (key === lastWindow) return;
    lastWindow = key;
    clearTimeout(timer);
    request?.abort();
    const cached = observationCache.get(key);
    if (cached) {
      observationCache.delete(key);
      observationCache.set(key, cached);
      showObservations(cached);
      return;
    }
    dataStatus.textContent = "Loading nearby detections…";
    timer = setTimeout(async () => {
      const next = new AbortController();
      request = next;
      const platforms = filter ? [filter] : Object.keys(records);
      try {
        const pages = await Promise.all(platforms.map((platform) => api("/v1/hotspots", {
          ...bounds, dataset: records[platform].dataset, platform, start_date: day, end_date: day, limit: 2500,
        }, next.signal)));
        if (!active || next.signal.aborted) return;
        const result = { rows: pages.flatMap((page) => page.data), capped: pages.some((page) => page.truncated) };
        observationCache.set(key, result);
        if (observationCache.size > 8) observationCache.delete(observationCache.keys().next().value);
        showObservations(result);
      } catch (error) {
        if (!next.signal.aborted) dataStatus.textContent = error.message;
      }
    }, 85);
  }

  // Where the spacecraft is drawn, in pixels below the top of the globe canvas.
  function measure() {
    const box = craft.getBoundingClientRect();
    craftY = box.top + box.height / 2 - canvas.getBoundingClientRect().top;
  }

  function render() {
    // Look far enough ahead that the ground under the drawn spacecraft is where
    // it really is: it turns about itself and the readout matches, at any zoom.
    const lead = globe.lead(craftY, zoom);
    const camera = destination(state, state.heading, lead);
    const forward = destination(state, state.heading, lead + .1);
    globe.setCamera(camera.lat, camera.lon, zoom, bearingFrom(camera, forward));
    craft.style.setProperty("--flight-bank", `${bank.toFixed(1)}deg`);
    craft.style.setProperty("--flight-shift", `${bank.toFixed(1)}px`);
    model?.draw(bank);
    position.textContent = positionLabel(state);
    heading.textContent = `${Math.round(state.heading) % 360}° ${compass[Math.round(state.heading / 45) % 8]}`;
    const speed = Math.round(100 * Math.abs(state.speed));
    speedReadout.textContent = `${speed}%${state.speed < 0 && speed ? " reverse" : ""}`;
    refreshObservations(destination(state, state.heading, lead / 2), lead);
  }

  function tick(time) {
    frame = undefined;
    if (!active) return;
    const dt = previousTime ? Math.min(.05, (time - previousTime) / 1000) : 0;
    previousTime = time;
    const turn = Number(isHeld("right")) - Number(isHeld("left"));
    const thrust = Number(isHeld("forward")) - Number(isHeld("reverse"));
    const zooming = Number(isHeld("zoomIn")) - Number(isHeld("zoomOut"));
    const moving = state.speed || state.slide || state.turnRate;
    const oldBank = bank;
    const oldZoom = zoom;
    if (zooming) targetZoom = clamp(targetZoom * Math.exp(zooming * flight.zoomRate * dt), 1, 9);
    stepFlight(state, { thrust, turn, boost: isHeld("boost") }, dt, zoom);
    const targetBank = reducedMotion.matches ? 0 : state.turnRate * .34;
    bank += (targetBank - bank) * (1 - Math.exp(-8 * dt));
    if (Math.abs(targetBank - bank) < .05) bank = targetBank;
    zoom = reducedMotion.matches ? targetZoom : zoom + (targetZoom - zoom) * (1 - Math.exp(-9 * dt));
    if (Math.abs(targetZoom - zoom) < .005) zoom = targetZoom;
    const drifting = state.speed || state.slide || state.turnRate;
    if (dirty || moving || drifting || oldBank !== bank || oldZoom !== zoom) {
      dirty = false;
      render();
    }
    if (turn || thrust || zooming || drifting || bank || zoom !== targetZoom) scheduleFrame();
    else previousTime = undefined;
  }

  function reset() {
    state = { ...start, speed: 0, slide: 0, turnRate: 0 };
    bank = 0;
    zoom = targetZoom = 2.15;
    clearInput();
    invalidate();
  }

  function changeZoom(factor) {
    targetZoom = clamp(targetZoom * factor, 1, 9);
    invalidate();
  }

  function inspect(row) {
    if (!row) {
      detail.hidden = true;
      return;
    }
    const timestamp = new Date(row.observation_time_utc);
    const time = Number.isNaN(timestamp.getTime()) ? row.observation_time_utc : timestamp.toISOString().slice(11, 16) + " UTC";
    const power = row.frp_mw == null ? "" : ` · ${row.frp_mw.toLocaleString()} MW FRP`;
    detail.textContent = `${records[row.platform]?.label ?? row.platform} · ${time} · ${positionLabel({ lat: row.latitude, lon: row.longitude })} · ${row.confidence_class} confidence${power}`;
    detail.hidden = false;
  }

  function updateSelection() {
    clearObservations();
    invalidate();
  }

  for (const input of [date, sensor]) input.addEventListener("change", updateSelection);
  get("flight-reset").addEventListener("click", reset);
  get("flight-exit").addEventListener("click", onExit);
  get("flight-zoom-in").addEventListener("click", () => changeZoom(1.35));
  get("flight-zoom-out").addEventListener("click", () => changeZoom(1 / 1.35));
  canvas.addEventListener("wheel", (event) => {
    if (event.ctrlKey || event.metaKey) return;
    event.preventDefault();
    changeZoom(Math.exp(-clamp(event.deltaY, -120, 120) * .003));
  }, { passive: false });
  canvas.addEventListener("pointerdown", (event) => {
    if (event.isPrimary && event.button === 0) {
      markerPress = { id: event.pointerId, x: event.clientX, y: event.clientY };
    }
  });
  canvas.addEventListener("pointerup", (event) => {
    if (markerPress?.id !== event.pointerId) return;
    if (Math.hypot(event.clientX - markerPress.x, event.clientY - markerPress.y) < 5) {
      inspect(globe.detectionAt(event.clientX, event.clientY));
    }
    markerPress = undefined;
  });
  canvas.addEventListener("pointercancel", (event) => {
    if (markerPress?.id === event.pointerId) markerPress = undefined;
  });

  for (const button of buttons) {
    button.addEventListener("pointerdown", (event) => {
      if (event.button !== 0) return;
      button.setPointerCapture(event.pointerId);
      pressed.set(event.pointerId, button.dataset.flightControl);
      button.dataset.held = "";
      scheduleFrame();
    });
    for (const type of ["pointerup", "lostpointercapture", "pointercancel"]) {
      button.addEventListener(type, (event) => {
        pressed.delete(event.pointerId);
        if (!isHeld(button.dataset.flightControl)) delete button.dataset.held;
      });
    }
    for (const type of ["keydown", "keyup"]) {
      button.addEventListener(type, (event) => {
        if (![" ", "Enter"].includes(event.key)) return;
        event.preventDefault();
        if (type === "keydown") {
          pressed.set(button, button.dataset.flightControl);
          button.dataset.held = "";
          scheduleFrame();
        } else {
          pressed.delete(button);
          delete button.dataset.held;
        }
      });
    }
    button.addEventListener("blur", () => {
      pressed.delete(button);
      if (!isHeld(button.dataset.flightControl)) delete button.dataset.held;
    });
  }

  window.addEventListener("keydown", (event) => {
    // A key released while Command is down never reports it: let go of everything.
    if (event.key === "Meta") clearInput();
    if (!active || event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey) return;
    if (document.querySelector("dialog[open]")) return;
    if (event.key === "Escape") {
      event.preventDefault();
      onExit();
      return;
    }
    if (event.target instanceof Element && event.target.closest("input, select, textarea, [contenteditable]")) return;
    const control = keys[event.code];
    if (control) {
      event.preventDefault();
      pressed.set(event.code, control);
      scheduleFrame();
    }
  });
  window.addEventListener("keyup", (event) => pressed.delete(event.code));
  window.addEventListener("blur", clearInput);
  new ResizeObserver(() => {
    measure();
    invalidate();
  }).observe(root);
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) clearInput();
    previousTime = undefined;
    scheduleFrame();
  });

  return {
    setDateLimits(first, last) {
      date.min = first;
      date.max = last;
      if (date.value < first || date.value > last) date.value = demoDate >= first && demoDate <= last ? demoDate : last;
      updateSelection();
    },
    start() {
      if (active) return;
      active = true;
      previousTime = undefined;
      measure();
      reset();
      canvas.focus({ preventScroll: true });
    },
    stop() {
      active = false;
      cancelAnimationFrame(frame);
      frame = undefined;
      clearInput();
      clearObservations();
    },
  };
}
