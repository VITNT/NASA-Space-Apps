// A detection is not a point. It is one sensor pixel, and the fire is
// somewhere inside it. FIRMS reports the pixel's size on the ground as `scan`
// (across the swath) and `track` (along the orbit): 375 m at nadir for VIIRS,
// 1 km for MODIS, growing toward the swath edge up to about 0.8 km and
// 2 × 4.8 km. This rebuilds that rectangle around the reported centre.
//
// The pixel is turned to lie across the satellite's ground track. FIRMS does
// not report the track, so it comes from the nominal sun-synchronous orbit:
// the day pass of each satellite is ascending or descending, and the night
// pass is the other. The result is within a few degrees of the true scan line.

import { kmPerDegree, satellites, trackHeading } from "./satellites.js";

const radians = Math.PI / 180;

// The four corners of a detection's pixel as [longitude, latitude], or null
// when the record lacks the pixel size or a known satellite.
export function footprint(point) {
  const satellite = satellites[point.platform];
  if (!satellite || !(point.scan_km > 0) || !(point.track_km > 0)) return null;
  const ascending = (satellite.dayPass === "ascending") === (point.daynight === "D");
  const heading = trackHeading(satellite, point.latitude, ascending) * radians;
  // Unit steps along the track and across it (the scan line), as [east, north].
  const along = [Math.sin(heading), Math.cos(heading)];
  const across = [Math.cos(heading), -Math.sin(heading)];
  const halfScan = point.scan_km / 2;
  const halfTrack = point.track_km / 2;
  const cosLat = Math.max(.01, Math.cos(point.latitude * radians));
  return [[-1, -1], [1, -1], [1, 1], [-1, 1]].map(([s, t]) => {
    const east = s * halfScan * across[0] + t * halfTrack * along[0];
    const north = s * halfScan * across[1] + t * halfTrack * along[1];
    return [point.longitude + east / (kmPerDegree * cosLat), point.latitude + north / kmPerDegree];
  });
}
