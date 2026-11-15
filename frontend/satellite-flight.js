import { createGlobe } from "./globe.js";
import { createSatelliteModel } from "./satellite-model.js";
import {
  kmPerDegree, nightCrossing, orbit, satelliteOrder, satellites, swathHalfWidth, trackHeading,
} from "./satellites.js";

const radians = Math.PI / 180;
const compass = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"];
const clamp = (value, min, max) => Math.min(max, Math.max(min, value));
const wrap = (value, span) => ((value % span) + span) % span;
const wrapLongitude = (value) => wrap(value + 180, 360) - 180;
const start = { lat: 12.55, lon: 104.85, heading: 35 };
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

const numberFormat = new Intl.NumberFormat("en");
const monthYear = new Intl.DateTimeFormat("en", { month: "short", year: "numeric", timeZone: "UTC" });

// The instrument and orbit facts a reader needs to interpret one satellite's detections.
function specRows(satellite, archive) {
  const { periodMin } = orbit(satellite);
  const night = satellite.dayPass === "ascending" ? "descending" : "ascending";
  const rows = [
    ["Instrument", `${satellite.instrument} · ${satellite.product}`],
    ["Orbit", `Sun-synchronous · ${satellite.altitudeKm} km · ${satellite.inclination}° · ${periodMin.toFixed(1)} min`],
    ["Equator crossing", `${satellite.crossing} ${satellite.dayPass} (day) · ${nightCrossing(satellite)} ${night} (night), local solar time`],
    ["Swath", `${numberFormat.format(satellite.swathKm)} km`],
    ["Fire pixel", `${satellite.nadirPixel} at nadir, ${satellite.edgePixel} at swath edge`],
    ["Fire channels", satellite.bands],
  ];
  if (archive) {
    const month = (date) => monthYear.format(new Date(`${date}T00:00:00Z`));
    rows.push(["In this archive", `${month(archive.first)} – ${month(archive.last)}`]);
  }
  return rows;
}

// `layer` supplies the activity the explore globe shows: a title for the current
// selection, the satellites of the selected record, a query for a camera's view
// (optionally for one satellite), and a description of a selected cell.
export function createSatelliteFlight(root, layer, onExit) {
  const get = (id) => root.querySelector(`#${id}`);
  const canvas = get("flight-globe");
  const selection = get("flight-selection");
  const dataStatus = get("flight-data-status");
  const detail = get("flight-detection");
  const position = get("flight-position");
  const heading = get("flight-heading");
  const speedReadout = get("flight-speed");
  const craft = root.querySelector(".flight-craft");
  const modelCanvas = get("flight-model");
  const fallbackCraft = craft.querySelector(".flight-craft-model");
  const modelCredit = get("flight-model-credit");
  const picker = get("flight-satellites");
  const specCard = get("flight-spec-card");
  const spec = get("flight-spec");
  const specNote = get("flight-spec-note");
  const passRow = get("flight-pass-row");
  const passReadout = get("flight-pass");
  const swathButton = get("flight-swath");
  const orbitButton = get("flight-orbit");
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
  let lastKey;
  let request;
  let timer;
  // The satellite being flown (a key of `satellites`), or undefined for every
  // satellite of the record. Following, the craft keeps to its ground track.
  let platform;
  let following = false;
  let ascending = true;
  let showSwath = true;
  const cellCache = new Map();
  let modelView;
  try {
    modelView = createSatelliteModel(modelCanvas);
  } catch (error) {
    console.warn("Spacecraft model:", error.message);
  }
  modelCanvas.addEventListener("webglcontextlost", () => {
    modelView = undefined;
    showModel();
  });

  // Draws the chosen satellite's NASA model, or the generic craft when there
  // is none. Every satellite of the record together flies as Terra.
  function showModel() {
    const source = satellites[platform ?? "terra"].model;
    model = undefined;
    modelCanvas.hidden = true;
    modelCredit.hidden = true;
    fallbackCraft.hidden = false;
    if (!source || !modelView) return;
    modelView.show(source).then((ready) => {
      if (!ready || satellites[platform ?? "terra"].model !== source) return;
      model = modelView;
      modelCanvas.hidden = false;
      fallbackCraft.hidden = true;
      const link = Object.assign(document.createElement("a"), {
        href: source.source, target: "_blank", rel: "noopener", textContent: source.author,
      });
      modelCredit.replaceChildren(`${source.credit} · `, link);
      modelCredit.hidden = false;
      invalidate();
    }).catch((error) => console.warn("Spacecraft model:", error.message));
  }

  function selectSatellite(key) {
    platform = key;
    for (const button of picker.children) button.setAttribute("aria-pressed", String(button.dataset.platform === (key ?? "")));
    const satellite = satellites[key];
    specCard.hidden = !satellite;
    swathButton.disabled = orbitButton.disabled = !satellite;
    if (satellite) {
      const archive = layer.satellites().find((item) => item.key === key)?.archive;
      spec.replaceChildren(...specRows(satellite, archive).map(([term, value]) => {
        const row = document.createElement("div");
        row.append(Object.assign(document.createElement("dt"), { textContent: term }),
          Object.assign(document.createElement("dd"), { textContent: value }));
        return row;
      }));
      specNote.textContent = satellite.note;
    }
    setFollowing(following && !!satellite);
    showModel();
    lastKey = undefined;
    detail.hidden = true;
    invalidate();
  }

  // The record's satellites, each available when it observed the selected month.
  function buildPicker() {
    const items = layer.satellites();
    const choices = items.length > 1 ? [{ key: undefined, available: true }, ...items] : items;
    picker.replaceChildren(...choices.map(({ key, available }) => {
      const button = document.createElement("button");
      button.type = "button";
      button.dataset.platform = key ?? "";
      button.textContent = key ? satellites[key].name : "All";
      button.disabled = !available;
      button.title = !key ? "Every satellite of the selected record"
        : available ? `${satellites[key].name} · ${satellites[key].instrument} only`
        : `${satellites[key].name} has no data in the selected month`;
      button.addEventListener("click", () => selectSatellite(key));
      return button;
    }));
    const keep = items.find((item) => item.key === platform && item.available);
    selectSatellite(keep ? platform : items.length === 1 && items[0].available ? items[0].key : undefined);
  }

  function setFollowing(next) {
    following = next;
    orbitButton.setAttribute("aria-pressed", String(following));
    passRow.hidden = !following;
    if (following) {
      state.turnRate = state.slide = 0;
      // Start on the daytime pass, the one most detections come from.
      ascending = satellites[platform].dayPass === "ascending";
      if (Math.abs(state.lat) >= orbit(satellites[platform]).turnLatitude - .05) ascending = state.lat < 0;
      state.heading = trackHeading(satellites[platform], state.lat, ascending);
      invalidate();
    }
  }

  // Keeps the craft on its ground track, turning onto the other pass where the
  // orbit reaches its highest latitude.
  function holdTrack() {
    const satellite = satellites[platform];
    const poleward = Math.cos(state.heading * radians) * state.speed * state.lat > 0;
    if (poleward && Math.abs(state.lat) >= orbit(satellite).turnLatitude - .05) ascending = !ascending;
    state.heading = trackHeading(satellite, state.lat, ascending);
    state.turnRate = state.slide = 0;
  }

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

  function showCells({ items, dots, label }) {
    globe.setCells(items, dots);
    const only = platform ? ` · ${satellites[platform].name} only` : "";
    dataStatus.textContent = items.length ? label + only : `No mapped ${platform ? satellites[platform].name + " " : ""}observations in this view`;
  }

  function clearInput() {
    pressed.clear();
    markerPress = undefined;
    for (const button of buttons) delete button.dataset.held;
  }

  function clearCells() {
    clearTimeout(timer);
    timer = undefined;
    request?.abort();
    lastKey = undefined;
    detail.hidden = true;
    globe.setCells([], true);
  }

  // Checked a few times a second while the view moves, like the explore globe.
  function refreshCells() {
    if (timer === undefined) timer = setTimeout(loadCells, 180);
  }

  async function loadCells() {
    timer = undefined;
    const camera = active && globe.getViewport();
    if (!camera) return;
    const { key, load } = layer.query(camera, platform);
    if (key === lastKey) return;
    lastKey = key;
    request?.abort();
    const next = new AbortController();
    request = next;
    try {
      let result = cellCache.get(key);
      if (!result) {
        dataStatus.textContent = "Loading map observations…";
        result = await load(next.signal);
        if (!active || next.signal.aborted) return;
      }
      cellCache.delete(key);
      cellCache.set(key, result);
      if (cellCache.size > 6) cellCache.delete(cellCache.keys().next().value);
      showCells(result);
    } catch (error) {
      if (next.signal.aborted) return;
      lastKey = undefined;
      dataStatus.textContent = error.message;
    }
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
    const satellite = satellites[platform];
    globe.setSwath(satellite && showSwath
      ? { lat: state.lat, lon: state.lon, heading: state.heading, halfWidth: swathHalfWidth(satellite) } : null);
    globe.setCamera(camera.lat, camera.lon, zoom, bearingFrom(camera, forward));
    craft.style.setProperty("--flight-bank", `${bank.toFixed(1)}deg`);
    craft.style.setProperty("--flight-shift", `${bank.toFixed(1)}px`);
    model?.draw(bank);
    position.textContent = positionLabel(state);
    heading.textContent = `${Math.round(state.heading) % 360}° ${compass[Math.round(state.heading / 45) % 8]}`;
    const speed = Math.round(100 * Math.abs(state.speed));
    let pace = "";
    if (following && speed) {
      // How much faster than the real sub-satellite point the view moves.
      const kmPerSecond = Math.abs(state.speed) * flight.cruise / zoom * kmPerDegree;
      pace = ` · ${numberFormat.format(Math.round(kmPerSecond / orbit(satellite).groundSpeed))}× real`;
    }
    speedReadout.textContent = `${speed}%${state.speed < 0 && speed ? " reverse" : ""}${pace}`;
    if (following) {
      const day = ascending === (satellite.dayPass === "ascending");
      const crossing = day ? satellite.crossing : nightCrossing(satellite);
      passReadout.textContent = `${day ? "Day" : "Night"} · ${ascending ? "ascending" : "descending"} · ${crossing} at equator`;
    }
    refreshCells();
  }

  function tick(time) {
    frame = undefined;
    if (!active) return;
    const dt = previousTime ? Math.min(.05, (time - previousTime) / 1000) : 0;
    previousTime = time;
    const turn = Number(isHeld("right")) - Number(isHeld("left"));
    // Steering by hand leaves the ground track.
    if (turn && following) setFollowing(false);
    const thrust = Number(isHeld("forward")) - Number(isHeld("reverse"));
    const zooming = Number(isHeld("zoomIn")) - Number(isHeld("zoomOut"));
    const moving = state.speed || state.slide || state.turnRate;
    const oldBank = bank;
    const oldZoom = zoom;
    if (zooming) targetZoom = clamp(targetZoom * Math.exp(zooming * flight.zoomRate * dt), 1, 9);
    if (following) holdTrack();
    stepFlight(state, { thrust, turn, boost: isHeld("boost") }, dt, zoom);
    if (following) holdTrack();
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
    if (following) setFollowing(true);
    invalidate();
  }

  function changeZoom(factor) {
    targetZoom = clamp(targetZoom * factor, 1, 9);
    invalidate();
  }

  function inspect(cell) {
    detail.hidden = !cell;
    if (cell) detail.textContent = layer.describe(cell);
  }

  get("flight-reset").addEventListener("click", reset);
  swathButton.addEventListener("click", () => {
    showSwath = !showSwath;
    swathButton.setAttribute("aria-pressed", String(showSwath));
    invalidate();
  });
  orbitButton.addEventListener("click", () => setFollowing(!following));
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
      inspect(globe.cellAt(event.clientX, event.clientY));
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
    start() {
      if (active) return;
      active = true;
      previousTime = undefined;
      // The selection can change whenever flight is off.
      cellCache.clear();
      selection.textContent = layer.title();
      measure();
      reset();
      buildPicker();
      canvas.focus({ preventScroll: true });
    },
    stop() {
      active = false;
      cancelAnimationFrame(frame);
      frame = undefined;
      clearInput();
      clearCells();
    },
  };
}
