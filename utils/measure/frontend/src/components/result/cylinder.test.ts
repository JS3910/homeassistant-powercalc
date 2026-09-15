import {
  cameraAtRest,
  clampTilt,
  clampZoom,
  cylinderDomains,
  cylinderPowerTickCount,
  cylinderWorld,
  defaultCamera,
  inCylinderFrame,
  INITIAL_TILT,
  INITIAL_YAW,
  INITIAL_ZOOM,
  ISO_ELEVATION,
  layoutCylinder,
  MAX_TILT,
  MIN_TILT,
  nearestCylinderPoint,
  pointWatt,
  projectWorld,
  projectedPixel,
  tiltFromDrag,
  yawFromDrag,
  zoomFromWheel,
} from "./cylinder";
import type { PlotPoint } from "../../types";

function point(overrides: Partial<PlotPoint> & Pick<PlotPoint, "x" | "y">): PlotPoint {
  return { color: null, ...overrides };
}

describe("hs cylinder projection", () => {
  it("reads power from z when present", () => {
    expect(pointWatt(point({ x: 0, y: 255, z: 4.2 }))).toBe(4.2);
    expect(pointWatt(point({ x: 0, y: 3.1 }))).toBe(3.1);
  });

  it("collapses every zero-saturation hue onto the same world axis", () => {
    const domains = { satMax: 255, wattMin: 0, wattMax: 5 };
    const red = cylinderWorld(point({ x: 0, y: 0, z: 2 }), domains);
    const green = cylinderWorld(point({ x: 120, y: 0, z: 2 }), domains);
    expect(red.x).toBeCloseTo(0);
    expect(red.y).toBeCloseTo(0);
    expect(green.x).toBeCloseTo(0);
    expect(green.y).toBeCloseTo(0);
    expect(projectWorld(red).sx).toBeCloseTo(projectWorld(green).sx);
    expect(projectWorld(red).sy).toBeCloseTo(projectWorld(green).sy);
  });

  it("puts full saturation on the rim and power on height", () => {
    const domains = cylinderDomains([
      point({ x: 0, y: 255, z: 0 }),
      point({ x: 180, y: 255, z: 10 }),
    ]);
    expect(domains.satMax).toBe(255);
    expect(domains.wattMin).toBe(0);
    expect(domains.wattMax).toBe(10);
    const low = cylinderWorld(point({ x: 0, y: 255, z: 0 }), domains);
    const high = cylinderWorld(point({ x: 0, y: 255, z: 10 }), domains);
    expect(Math.hypot(low.x, low.y)).toBeCloseTo(1);
    expect(low.z).toBeCloseTo(0);
    expect(high.z).toBeCloseTo(1);
    expect(projectWorld(high).sy).toBeGreaterThan(projectWorld(low).sy);
  });

  it("orbits around a fixed aim: yaw moves a vivid point without changing its height", () => {
    const domains = { satMax: 255, wattMin: 0, wattMax: 4 };
    const world = cylinderWorld(point({ x: 40, y: 255, z: 0 }), domains);
    const faceOn = projectWorld(world, 0);
    const turned = projectWorld(world, Math.PI / 3);
    expect(faceOn.sx).not.toBeCloseTo(turned.sx);
    expect(faceOn.sy).not.toBeCloseTo(turned.sy);
  });

  it("tilts around the same aim and refuses to flip past the poles", () => {
    const top = projectWorld({ x: 0, y: 0, z: 1 }, 0, ISO_ELEVATION);
    const bottom = projectWorld({ x: 0, y: 0, z: 0 }, 0, ISO_ELEVATION);
    const topDown = projectWorld({ x: 0, y: 0, z: 1 }, 0, MAX_TILT);
    const bottomDown = projectWorld({ x: 0, y: 0, z: 0 }, 0, MAX_TILT);
    expect(top.sy - bottom.sy).toBeGreaterThan(topDown.sy - bottomDown.sy);
    expect(MIN_TILT).toBe(0);
    expect(clampTilt(-0.2)).toBeCloseTo(0);
    expect(clampTilt(Math.PI)).toBeCloseTo(MAX_TILT);
    expect(tiltFromDrag(INITIAL_TILT, 100, 100)).toBeGreaterThan(INITIAL_TILT);
    expect(tiltFromDrag(INITIAL_TILT, -100, 100)).toBeLessThan(INITIAL_TILT);
  });

  it("zooms by scaling the projection and clamps the range", () => {
    const points = [point({ x: 0, y: 255, z: 2, id: "hs:255:1:255" })];
    const rest = layoutCylinder(640, 400, points);
    const close = layoutCylinder(640, 400, points, { ...defaultCamera(), zoom: 2 });
    const pixelRest = projectedPixel(rest, points[0]!);
    const pixelClose = projectedPixel(close, points[0]!);
    const restDist = Math.hypot(pixelRest.x - rest.cx, pixelRest.y - rest.cy);
    const closeDist = Math.hypot(pixelClose.x - close.cx, pixelClose.y - close.cy);
    expect(closeDist).toBeGreaterThan(restDist);
    expect(zoomFromWheel(1, -1)).toBeGreaterThan(1);
    expect(clampZoom(0.01)).toBe(0.45);
    expect(cameraAtRest(defaultCamera())).toBe(true);
    expect(cameraAtRest({ yaw: INITIAL_YAW, tilt: INITIAL_TILT, zoom: 2 })).toBe(false);
    expect(cylinderPowerTickCount(close)).toBeGreaterThan(cylinderPowerTickCount(rest));
  });

  it("hits the nearest projected sample and ignores pixels outside the frame", () => {
    const points = [
      point({ x: 0, y: 255, z: 2, id: "hs:255:1:255" }),
      point({ x: 180, y: 255, z: 2, id: "hs:255:32768:255" }),
    ];
    const layout = layoutCylinder(640, 400, points, { yaw: INITIAL_YAW, tilt: INITIAL_TILT, zoom: INITIAL_ZOOM });
    const target = projectedPixel(layout, points[0]!);
    expect(nearestCylinderPoint(points, layout, target.x, target.y)?.id).toBe("hs:255:1:255");
    expect(inCylinderFrame(layout, layout.frame.left + 4, layout.frame.top + 4)).toBe(true);
    expect(inCylinderFrame(layout, 0, 0)).toBe(false);
  });

  it("treats a full-width drag as a half turn", () => {
    const next = yawFromDrag(INITIAL_YAW, 200, 200);
    expect(next).toBeCloseTo(INITIAL_YAW - Math.PI);
  });
});
