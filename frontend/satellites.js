// The spacecraft whose active-fire detections FIRMS distributes, with the
// nominal orbit and instrument geometry that shape what each one can see.
// Sources: MODIS C6/C6.1 and VIIRS C2 375 m active fire user's guides
// (Giglio et al. 2021; 2025), Schroeder et al. 2014, NASA and NOAA mission pages.

const earthRadiusKm = 6371;
const earthMu = 398600.4418;          // km³/s²
const earthRotation = 7.2921159e-5;   // rad/s
const radians = Math.PI / 180;

const modis = {
  instrument: "MODIS",
  swathKm: 2330,
  nadirPixel: "1 km",
  edgePixel: "≈ 2.0 × 4.8 km",
  bands: "4 µm (bands 21/22) and 11 µm (band 31)",
};
const viirs = {
  instrument: "VIIRS",
  swathKm: 3040,
  nadirPixel: "375 m",
  edgePixel: "≈ 0.8 km",
  bands: "I-4 (3.7 µm) and I-5 (11 µm)",
};

export const satellites = {
  terra: {
    ...modis,
    name: "Terra",
    mission: "EOS AM-1",
    product: "MOD14 · Collection 6.1",
    launched: "1999-12-18",
    altitudeKm: 705,
    inclination: 98.2,
    dayPass: "descending",
    crossing: "10:30",
    model: { url: "/static/assets/satellites/terra.glb", credit: "Terra model", source: "https://science.nasa.gov/3d-resources/terra/", author: "NASA / Christian A. Lopez" },
    note: "Samples fires before the usual mid-afternoon peak, so it sees fewer than the afternoon satellites. "
      + "Inclination control ended in 2020 and it left its constellation orbit in 2022, so its crossing time is drifting earlier.",
  },
  aqua: {
    ...modis,
    name: "Aqua",
    mission: "EOS PM-1",
    product: "MYD14 · Collection 6.1",
    launched: "2002-05-04",
    altitudeKm: 705,
    inclination: 98.2,
    dayPass: "ascending",
    crossing: "13:30",
    model: {
      url: "/static/assets/satellites/aqua.glb", rotation: [0, 90, 0],
      credit: "Aqua model", source: "https://github.com/nasa/NASA-3D-Resources/tree/master/3D%20Models/Aqua%20%28A%29", author: "NASA / Kevin K. Lane",
    },
    note: "Afternoon pass near the daily fire peak. It left the A-Train orbit in 2022, "
      + "so its crossing time is drifting later.",
  },
  snpp: {
    ...viirs,
    name: "Suomi NPP",
    mission: "NPOESS Preparatory Project",
    product: "VNP14IMG · Collection 2",
    launched: "2011-10-28",
    altitudeKm: 824,
    inclination: 98.7,
    dayPass: "ascending",
    crossing: "13:30",
    model: {
      url: "/static/assets/satellites/snpp.glb", rotation: [0, 90, 0], scale: .7,
      credit: "Suomi NPP model", source: "https://science.nasa.gov/3d-resources/", author: "NASA",
    },
    note: "VIIRS aggregates pixels across the scan, so a pixel at the swath edge is about twice the nadir size, "
      + "where a MODIS pixel grows almost fivefold across the scan.",
  },
  noaa20: {
    ...viirs,
    name: "NOAA-20",
    mission: "JPSS-1",
    product: "VJ114IMG · Collection 2",
    launched: "2017-11-18",
    altitudeKm: 824,
    inclination: 98.7,
    dayPass: "ascending",
    crossing: "13:30",
    // NOAA-20 (JPSS-1) is built on the same Ball BCP-2000 spacecraft as Suomi NPP.
    model: {
      url: "/static/assets/satellites/snpp.glb", rotation: [0, 90, 0], scale: .7,
      credit: "Suomi NPP model, same spacecraft design", source: "https://science.nasa.gov/3d-resources/", author: "NASA",
    },
    note: "Shares Suomi NPP's orbit, half an orbit (about 50 minutes) ahead, so the two see the same place "
      + "about 50 minutes apart.",
  },
};

export const satelliteOrder = ["terra", "aqua", "snpp", "noaa20"];

// Circular-orbit figures for a satellite: orbital period (minutes), speed of
// the sub-satellite point over a non-rotating Earth (km/s) and the highest
// latitude the ground track reaches.
export function orbit({ altitudeKm, inclination }) {
  const radius = earthRadiusKm + altitudeKm;
  const speed = Math.sqrt(earthMu / radius);
  return {
    periodMin: 2 * Math.PI * radius / speed / 60,
    groundSpeed: speed * earthRadiusKm / radius,
    turnLatitude: 180 - inclination,
  };
}

// Heading (degrees from north) of the ground track at `latitude` on an
// ascending or descending pass, including the Earth turning beneath the orbit.
export function trackHeading(satellite, latitude, ascending) {
  const { groundSpeed, turnLatitude } = orbit(satellite);
  const cosLat = Math.cos(Math.min(Math.abs(latitude), turnLatitude) * radians);
  // Inertial azimuth of the orbit at this latitude: sin A = cos i / cos φ.
  const sinA = Math.max(-1, Math.min(1, Math.cos(satellite.inclination * radians) / cosLat));
  const north = groundSpeed * Math.sqrt(1 - sinA * sinA) * (ascending ? 1 : -1);
  const east = groundSpeed * sinA - earthRotation * earthRadiusKm * cosLat;
  return ((Math.atan2(east, north) / radians) % 360 + 360) % 360;
}

// Half the swath, as an angle along the Earth's surface (degrees).
export function swathHalfWidth(satellite) {
  return satellite.swathKm / 2 / earthRadiusKm / radians;
}

export const kmPerDegree = earthRadiusKm * radians;

// Night-pass crossing time, twelve hours from the day pass.
export function nightCrossing({ crossing }) {
  const [hours, minutes] = crossing.split(":").map(Number);
  return `${String((hours + 12) % 24).padStart(2, "0")}:${String(minutes).padStart(2, "0")}`;
}
