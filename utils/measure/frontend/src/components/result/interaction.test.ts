import {
  applyYToView,
  clampView,
  crosshairData,
  dataBounds,
  dataToPixel,
  ctBriFromPointId,
  ctMiredFromPointId,
  brightnessLevels,
  filterPlotToBrightness,
  filterPlotToHsBrightness,
  filterPlotToHue,
  filterPlotToMired,
  filterPlotToSession,
  densityPlotFor,
  densityValues,
  holdLinksFromPointId,
  histogramAnchorAt,
  histogramBins,
  histogramCountAt,
  histogramPiles,
  markerDensityScale,
  medianNearestNeighborPx,
  snapHistogramPile,
  visibleHistogramPiles,
  hoverPointForInterestMark,
  interestCirclePoints,
  hsHueFromPointId,
  hsSatFromPointId,
  isoGroups,
  nearestPointByX,
  inPlot,
  inPlotX,
  interestTickHit,
  interestTickPixel,
  matePointId,
  plotInterestMarks,
  niceYDomain,
  sharedYForPlot,
  nearestBriAt,
  nearestHueAt,
  nearestMiredAt,
  nearestPoint,
  panView,
  pixelToData,
  plotInnerFrame,
  plotLayout,
  plotMargins,
  PLOT_POINT_PAD,
  reuseYRange,
  samplesAtNoun,
  viewsEqual,
  wheelZoomFactor,
  yViewsEqual,
  zoomView,
} from "./interaction";
import type { PlotPoint, PlotSpec } from "../../types";

function plot(points: PlotPoint[]): PlotSpec {
  return {
    id: "brightness",
    title: "Brightness",
    kind: "scatter",
    x_label: "Brightness (%)",
    y_label: "Power (W)",
    source: "brightness.csv",
    series: [{ label: null, color: "#5488e8", points }],
    x_min: 0,
    x_max: 100,
  };
}

const sample = plot([
  { x: 0, y: 1, color: null, id: "brightness:1", rail: "brightness" },
  { x: 50, y: 4, color: null, id: "brightness:128", rail: "brightness" },
  { x: 100, y: 8, color: null, id: "brightness:255", rail: "brightness" },
]);

describe("plot interaction", () => {
  it("uses configured brightness bounds even when points sit at the ends", () => {
    expect(dataBounds(sample)).toEqual({
      minX: 0,
      maxX: 100,
      minY: 1,
      maxY: 8,
    });
  });

  it("zooms toward the cursor and clamps back to the data view", () => {
    const bounds = dataBounds(sample);
    const zoomed = zoomView(bounds, 2, 50, 4);
    expect(zoomed.maxX - zoomed.minX).toBeCloseTo(50);
    expect(zoomed.minX).toBeCloseTo(25);
    expect(zoomed.maxX).toBeCloseTo(75);
    const clamped = clampView(
      { minX: -20, maxX: 140, minY: -4, maxY: 20 },
      bounds,
    );
    expect(viewsEqual(clamped, bounds)).toBe(true);
  });

  it("round-trips data and pixel coordinates", () => {
    const layout = plotLayout(sample, 400, 300);
    const pixel = dataToPixel(layout, 50, 4);
    const data = pixelToData(layout, pixel.x, pixel.y);
    expect(data.x).toBeCloseTo(50);
    expect(data.y).toBeCloseTo(4);
  });

  it("reuses the previous Y range object when the numbers did not move", () => {
    const previous = { minY: 0, maxY: 4 };
    expect(reuseYRange(previous, { minY: 0, maxY: 4 })).toBe(previous);
    expect(reuseYRange(previous, { minY: 0, maxY: 5 })).toEqual({ minY: 0, maxY: 5 });
  });

  it("keeps min/max points inside the clip so markers are not cut in half", () => {
    const layout = plotLayout(sample, 400, 300);
    const inner = plotInnerFrame(layout);
    const min = dataToPixel(layout, layout.view.minX, layout.view.minY);
    const max = dataToPixel(layout, layout.view.maxX, layout.view.maxY);
    expect(min.x).toBeCloseTo(inner.left);
    expect(min.y).toBeCloseTo(inner.top + inner.plotHeight);
    expect(max.x).toBeCloseTo(inner.left + inner.plotWidth);
    expect(max.y).toBeCloseTo(inner.top);
    expect(min.x).toBeGreaterThan(layout.left);
    expect(max.x).toBeLessThan(layout.left + layout.plotWidth);
    expect(PLOT_POINT_PAD).toBeGreaterThan(0);
  });

  it("snaps the crosshair to a measured point when one is hovered", () => {
    const cursor = { x: 51, y: 3.8 };
    const point: PlotPoint = {
      x: 50,
      y: 4,
      color: null,
      id: "color_temp:255:320",
    };
    expect(crosshairData(cursor, point)).toEqual({ x: 50, y: 4 });
    expect(crosshairData(cursor, null)).toEqual(cursor);
    expect(crosshairData(null, null)).toBeNull();
  });

  it("hits the nearest point in pixel space, not data space", () => {
    const layout = plotLayout(sample, 400, 300);
    const target = dataToPixel(layout, 50, 4);
    const hit = nearestPoint(
      sample.series[0]!.points,
      layout,
      target.x + 4,
      target.y - 3,
    );
    expect(hit?.id).toBe("brightness:128");
    expect(nearestPoint(sample.series[0]!.points, layout, 10, 10)).toBeNull();
  });

  it("reads the integer hue out of an HS point id", () => {
    expect(hsHueFromPointId("hs:255:32768:3")).toBe(32768);
    expect(hsHueFromPointId("brightness:128")).toBeNull();
  });

  it("snaps a hue-axis position to the nearest measured hue, wrapping at 360°", () => {
    const points: PlotPoint[] = [
      { x: 0, y: 4, color: null, id: "hs:255:1:255" },
      { x: 180, y: 2, color: null, id: "hs:255:32768:255" },
    ];
    expect(nearestHueAt(points, 170)).toBe(32768);
    expect(nearestHueAt(points, 350)).toBe(1);
  });

  it("filters the HS brightness plot down to one hue slice", () => {
    const hs: PlotSpec = {
      id: "hs",
      title: "Hue and saturation",
      kind: "scatter",
      x_label: "Brightness (%)",
      y_label: "Power (W)",
      source: "hs.csv",
      series: [
        {
          label: null,
          color: null,
          points: [
            { x: 100, y: 4, color: null, id: "hs:255:1:255" },
            { x: 50, y: 2, color: null, id: "hs:128:1:255" },
            { x: 100, y: 1.5, color: null, id: "hs:255:32768:255" },
          ],
        },
      ],
    };
    const sliced = filterPlotToHue(hs, 1);
    expect(sliced.title).toBe("Hue and saturation · 0°");
    expect(sliced.series[0]?.points.map((point) => point.id)).toEqual([
      "hs:255:1:255",
      "hs:128:1:255",
    ]);
    expect(filterPlotToHue(hs, null).title).toBe("Hue and saturation");
  });

  it("reads the integer brightness out of a CT point id", () => {
    expect(ctBriFromPointId("color_temp:128:350")).toBe(128);
    expect(ctBriFromPointId("hs:255:1:255")).toBeNull();
  });

  it("snaps a brightness-axis position to the nearest measured CT brightness", () => {
    const points: PlotPoint[] = [
      { x: 0.4, y: 0.4, color: null, id: "color_temp:1:150" },
      { x: 100, y: 4.6, color: null, id: "color_temp:255:150" },
    ];
    expect(nearestBriAt(points, 10)).toBe(1);
    expect(nearestBriAt(points, 80)).toBe(255);
  });

  it("filters the CT kelvin rail down to one brightness slice", () => {
    const rail: PlotSpec = {
      id: "color_temp_max_bri",
      title: "Color temperature at 100%",
      kind: "scatter",
      x_label: "Color temp (K)",
      y_label: "Power (W)",
      source: "color_temp.csv",
      series: [
        {
          label: null,
          color: null,
          points: [
            { x: 2000, y: 2.5, color: null, id: "color_temp:255:500" },
            { x: 6667, y: 4.6, color: null, id: "color_temp:255:150" },
            { x: 6667, y: 0.4, color: null, id: "color_temp:1:150" },
          ],
        },
      ],
    };
    const sliced = filterPlotToBrightness(rail, 1);
    expect(sliced.title).toBe("Color temperature at 0%");
    expect(sliced.series[0]?.points.map((point) => point.id)).toEqual([
      "color_temp:1:150",
    ]);
    expect(filterPlotToBrightness(rail, null).title).toBe(
      "Color temperature at 100%",
    );
    expect(
      filterPlotToBrightness(rail, null).series[0]?.points.map(
        (point) => point.id,
      ),
    ).toEqual(["color_temp:255:500", "color_temp:255:150"]);
  });

  it("reads the integer mired out of a CT point id", () => {
    expect(ctMiredFromPointId("color_temp:128:350")).toBe(350);
    expect(ctMiredFromPointId("hs:255:1:255")).toBeNull();
  });

  it("snaps a kelvin-axis position to the nearest measured mired", () => {
    const points: PlotPoint[] = [
      { x: 2000, y: 2.5, color: null, id: "color_temp:255:500" },
      { x: 6667, y: 4.6, color: null, id: "color_temp:255:150" },
    ];
    expect(nearestMiredAt(points, 2100)).toBe(500);
    expect(nearestMiredAt(points, 6000)).toBe(150);
  });

  it("filters the CT brightness plot down to one mired slice", () => {
    const ct: PlotSpec = {
      id: "color_temp",
      title: "Color temperature",
      kind: "scatter",
      x_label: "Brightness (%)",
      y_label: "Power (W)",
      source: "color_temp.csv",
      series: [
        {
          label: null,
          color: null,
          points: [
            { x: 100, y: 4.6, color: null, id: "color_temp:255:150" },
            { x: 50, y: 2.0, color: null, id: "color_temp:128:150" },
            { x: 100, y: 2.5, color: null, id: "color_temp:255:500" },
          ],
        },
      ],
    };
    const sliced = filterPlotToMired(ct, 150);
    expect(sliced.title).toBe("Color temperature · 6667 K");
    expect(sliced.series[0]?.points.map((point) => point.id)).toEqual([
      "color_temp:255:150",
      "color_temp:128:150",
    ]);
    expect(filterPlotToMired(ct, null).title).toBe("Color temperature");
  });

  it("keeps CT interest markers when the kelvin rail is sliced", () => {
    const rail: PlotSpec = {
      id: "color_temp_max_bri",
      title: "Color temperature at 100%",
      kind: "scatter",
      x_label: "Color temp (K)",
      y_label: "Power (W)",
      source: "color_temp.csv",
      markers: [{ x: 2500, label: "discontinuity at 2500 K" }],
      series: [
        {
          label: null,
          color: null,
          points: [
            { x: 2500, y: 4.6, color: null, id: "color_temp:255:400" },
            { x: 2500, y: 1.2, color: null, id: "color_temp:128:400" },
          ],
        },
      ],
    };
    expect(filterPlotToBrightness(rail, 128).markers).toEqual(rail.markers);
  });

  it("filters the HS cylinder down to one brightness slice", () => {
    const cylinder: PlotSpec = {
      id: "hs_cylinder",
      title: "Hue / saturation at 100%",
      kind: "cylinder",
      x_label: "Hue (°)",
      y_label: "Saturation",
      source: "hs.csv",
      series: [
        {
          label: null,
          color: null,
          points: [
            { x: 0, y: 255, z: 1.3, color: null, id: "hs:255:1:255" },
            { x: 120, y: 255, z: 1.2, color: null, id: "hs:255:21845:255" },
            { x: 0, y: 255, z: 0.8, color: null, id: "hs:128:1:255" },
          ],
        },
      ],
    };
    expect(brightnessLevels(cylinder)).toEqual([128, 255]);
    const sliced = filterPlotToHsBrightness(cylinder, 128);
    expect(sliced.title).toBe("Hue / saturation at 50%");
    expect(sliced.series[0]?.points.map((point) => point.id)).toEqual(["hs:128:1:255"]);
    expect(filterPlotToHsBrightness(cylinder, null).title).toBe("Hue / saturation at 100%");
    expect(filterPlotToHsBrightness(cylinder, null).series[0]?.points.map((point) => point.id)).toEqual([
      "hs:255:1:255",
      "hs:255:21845:255",
    ]);
  });

  it("hides inherited points only when asked for this session", () => {
    const mixed = plot([
      { x: 0, y: 1, color: null, id: "brightness:1", inherited: true },
      { x: 100, y: 8, color: null, id: "brightness:255" },
    ]);
    expect(filterPlotToSession(mixed, false)).toBe(mixed);
    expect(
      filterPlotToSession(mixed, true).series[0]?.points.map((point) => point.id),
    ).toEqual(["brightness:255"]);
  });

  it("shares one power axis across an HS pair", () => {
    const hs: PlotSpec = {
      id: "hs",
      title: "Hue and saturation",
      kind: "scatter",
      x_label: "Brightness (%)",
      y_label: "Power (W)",
      source: "hs.csv",
      series: [
        {
          label: null,
          color: null,
          points: [
            { x: 0, y: 0.26, color: null, id: "hs:1:1:255" },
            { x: 100, y: 4.0, color: null, id: "hs:255:1:4" },
          ],
        },
      ],
    };
    const rail: PlotSpec = {
      id: "hs_max_bri",
      title: "Hue at 100%",
      kind: "scatter",
      x_label: "Hue (°)",
      y_label: "Power (W)",
      source: "hs.csv",
      series: [
        {
          label: null,
          color: null,
          points: [
            { x: 0, y: 1.25, color: null, id: "hs:255:1:255" },
            { x: 120, y: 4.5, color: null, id: "hs:255:21845:4" },
          ],
        },
      ],
    };
    expect(sharedYForPlot([hs, rail], "hs")).toEqual({ minY: 0, maxY: 5 });
    expect(sharedYForPlot([hs, rail], "hs_max_bri")).toEqual({
      minY: 0,
      maxY: 5,
    });
    expect(sharedYForPlot([hs], "hs")).toBeNull();
  });

  it("shares one nice power axis across a CT pair with different extents", () => {
    const brightness: PlotSpec = {
      id: "color_temp",
      title: "Color temperature",
      kind: "scatter",
      x_label: "Brightness (%)",
      y_label: "Power (W)",
      source: "color_temp.csv",
      series: [
        {
          label: null,
          color: null,
          points: [
            { x: 8, y: 0.3, color: null, id: "color_temp:20:400" },
            { x: 75, y: 1.8, color: null, id: "color_temp:191:400" },
          ],
        },
      ],
    };
    const rail: PlotSpec = {
      id: "color_temp_max_bri",
      title: "Color temperature at 100%",
      kind: "scatter",
      x_label: "Color temp (K)",
      y_label: "Power (W)",
      source: "color_temp.csv",
      series: [
        {
          label: null,
          color: null,
          points: [
            { x: 2200, y: 2.5, color: null, id: "color_temp:255:454" },
            { x: 4000, y: 4.8, color: null, id: "color_temp:255:250" },
          ],
        },
      ],
    };
    expect(sharedYForPlot([brightness, rail], "color_temp")).toEqual({
      minY: 0,
      maxY: 5,
    });
    expect(sharedYForPlot([brightness, rail], "color_temp_max_bri")).toEqual({
      minY: 0,
      maxY: 5,
    });
  });

  it("pads a pair's power axis to the same nice ticks", () => {
    expect(niceYDomain(0.26, 4.5)).toEqual({ minY: 0, maxY: 5 });
    expect(niceYDomain(2.5, 4.8)).toEqual({ minY: 0, maxY: 5 });
  });

  it("copies a zoomed power axis onto the partner without moving X", () => {
    const partner = { minX: 0, maxX: 360, minY: 0.26, maxY: 4.5 };
    const zoomed = zoomView({ minX: 0, maxX: 100, minY: 0.26, maxY: 4.5 }, 2, 50, 2);
    const linked = applyYToView(partner, zoomed);
    expect(linked.minX).toBe(0);
    expect(linked.maxX).toBe(360);
    expect(yViewsEqual(linked, zoomed)).toBe(true);
    expect(linked.maxY - linked.minY).toBeCloseTo((4.5 - 0.26) / 2);
  });

  it("finds the max-bri mate on the hue rail for a dim HS point", () => {
    const rail: PlotSpec = {
      id: "hs_max_bri",
      title: "Hue at 100%",
      kind: "scatter",
      x_label: "Hue (°)",
      y_label: "Power (W)",
      source: "hs.csv",
      series: [
        {
          label: null,
          color: null,
          points: [
            { x: 0, y: 1.25, color: null, id: "hs:255:1:255" },
            { x: 0, y: 4.5, color: null, id: "hs:255:1:4" },
          ],
        },
      ],
    };
    expect(matePointId(rail, "hs:32:1:255")).toBe("hs:255:1:255");
    expect(holdLinksFromPointId("hs:32:1:255")).toEqual({
      hue: 1,
      bri: 32,
      mired: null,
    });
  });

  it("zooms in on wheel-up and out on wheel-down", () => {
    expect(wheelZoomFactor(-120)).toBeGreaterThan(1);
    expect(wheelZoomFactor(120)).toBeLessThan(1);
  });

  it("does not reserve header space for series legends", () => {
    const labelled = plot([
      { x: 0, y: 1, color: null, id: "a", inherited: true },
    ]);
    labelled.series[0]!.label = "Full saturation";
    expect(plotMargins(labelled).top).toBe(22);
    expect(plotMargins(sample).top).toBe(22);
  });

  it("pans by subtracting the data-space drag delta", () => {
    const panned = panView({ minX: 0, maxX: 100, minY: 0, maxY: 10 }, 10, 2);
    expect(panned).toEqual({ minX: -10, maxX: 90, minY: -2, maxY: 8 });
  });

  it("maps zoomed data outside the plot frame", () => {
    const layout = plotLayout(sample, 400, 300, {
      minX: 40,
      maxX: 60,
      minY: 3,
      maxY: 5,
    });
    const pixel = dataToPixel(layout, 100, 8);
    expect(pixel.x).toBeGreaterThan(layout.left + layout.plotWidth);
    expect(pixel.y).toBeLessThan(layout.top);
  });

  it("opens interest details from the gold triangle in the x-axis gutter", () => {
    const rail: PlotSpec = {
      id: "color_temp_max_bri",
      title: "Color temperature at 100%",
      kind: "scatter",
      x_label: "Color temperature (K)",
      y_label: "Power (W)",
      source: "color_temp.csv",
      markers: [{ x: 2500, label: "discontinuity at 2500 K" }],
      series: [
        {
          label: null,
          color: null,
          points: [
            {
              x: 2500,
              y: 8.2,
              color: null,
              id: "color_temp:255:400",
              interest: "discontinuity at 2500 K",
              stats: [
                { label: "Color temperature", value: "2500 K" },
                { label: "Power", value: "8.200 W" },
              ],
            },
            { x: 4000, y: 7.1, color: null, id: "color_temp:255:250" },
          ],
        },
      ],
    };
    const layout = plotLayout(rail, 400, 300);
    const tick = interestTickPixel(layout, 2500);
    expect(inPlot(layout, tick.x, tick.y + 6)).toBe(false);
    expect(inPlotX(layout, tick.x)).toBe(true);
    expect(
      interestTickHit(layout, plotInterestMarks(rail), tick.x, tick.y + 6)?.label,
    ).toBe("discontinuity at 2500 K");
    expect(
      interestTickHit(layout, plotInterestMarks(rail), tick.x, layout.top + 10),
    ).toBeNull();
    expect(
      interestTickHit(layout, plotInterestMarks(rail), tick.x, layout.top - 6)?.label,
    ).toBe("discontinuity at 2500 K");
    expect(interestCirclePoints(rail).map((point) => point.id)).toEqual([
      "color_temp:255:400",
    ]);
    expect(
      interestCirclePoints({ ...rail, id: "hs_max_bri" }),
    ).toEqual([]);
    const hover = hoverPointForInterestMark(rail, {
      x: 2500,
      label: "discontinuity at 2500 K",
    });
    expect(hover.id).toBe("color_temp:255:400");
    expect(hover.interest).toBe("discontinuity at 2500 K");
    const orphan = hoverPointForInterestMark(
      { ...rail, series: [{ label: null, color: null, points: [] }] },
      { x: 2500, label: "discontinuity at 2500 K" },
    );
    expect(orphan.id).toBeUndefined();
    expect(orphan.interest).toBe("discontinuity at 2500 K");
    expect(orphan.stats?.[0]?.value).toBe("2500");
  });

  it("bins sample counts along the visible x-axis", () => {
    const bins = histogramBins([0, 10, 10, 90], 0, 100, 10);
    expect(bins).toHaveLength(10);
    expect(histogramCountAt(bins, 10)).toBe(2);
    expect(histogramCountAt(bins, 90)).toBe(1);
    expect(histogramCountAt(bins, 50)).toBe(0);
  });

  it("keeps unique sample-x piles instead of merging neighbors", () => {
    const piles = histogramPiles([0, 10, 10, 90, 100, 100, 100]);
    expect(piles).toEqual([
      { x: 0, count: 1 },
      { x: 10, count: 2 },
      { x: 90, count: 1 },
      { x: 100, count: 3 },
    ]);
  });

  it("hides the shorter pile when two unique-x bars share a pixel column", () => {
    const piles = histogramPiles([
      ...Array.from({ length: 40 }, () => 50),
      50.2,
    ]);
    const visible = visibleHistogramPiles(piles, 0, 100, 80);
    expect(visible).toEqual([{ x: 50, count: 40 }]);
  });

  it("snaps to the taller visible bar even when the cursor is on a short neighbor", () => {
    const piles = histogramPiles([
      ...Array.from({ length: 80 }, () => 40),
      42,
      42,
    ]);
    const snapped = snapHistogramPile(piles, 42, 0, 100, 400);
    expect(snapped).toEqual({ x: 40, count: 80 });
  });

  it("keeps full-size markers on a sparse rail and shrinks to 75% when points pile up", () => {
    const sparse = Array.from({ length: 20 }, (_, index) => ({
      x: index * 20,
      y: 40,
    }));
    const dense = Array.from({ length: 200 }, (_, index) => ({
      x: 80 + (index % 8) * 0.4,
      y: 40 + Math.floor(index / 8) * 0.4,
    }));
    expect(medianNearestNeighborPx(sparse)).toBeGreaterThan(8);
    expect(markerDensityScale(sparse)).toBe(1);
    expect(medianNearestNeighborPx(dense)).toBeLessThan(2.5);
    expect(markerDensityScale(dense)).toBe(0.75);
  });

  it("anchors a max-brightness pile to 100 even when the cursor is left in that bin", () => {
    const values = [...Array.from({ length: 410 }, () => 100), 99.2];
    const bins = histogramBins(values, 0, 100, 10);
    expect(histogramCountAt(bins, 99.6)).toBe(411);
    expect(histogramAnchorAt(bins, 99.6)).toBe(100);
    expect(histogramAnchorAt(bins, 100)).toBe(100);
    expect(histogramAnchorAt(bins, 50)).toBeNull();
  });

  it("picks the tallest point at the nearest x for the density slider", () => {
    const points: PlotPoint[] = [
      { x: 20, y: 1, color: null, id: "a" },
      { x: 40, y: 2, color: null, id: "b" },
      { x: 40, y: 5, color: null, id: "c" },
    ];
    expect(nearestPointByX(points, 38)?.id).toBe("c");
  });

  it("counts HS brightness samples on the hue rail from the sibling plot", () => {
    const rail: PlotSpec = {
      id: "hs_max_bri",
      title: "Hue at 100%",
      kind: "scatter",
      x_label: "Hue (°)",
      y_label: "Power (W)",
      source: "hs.csv",
      series: [
        {
          label: null,
          color: null,
          points: [{ x: 0, y: 4, color: null, id: "hs:255:1:255" }],
        },
      ],
    };
    const brightness: PlotSpec = {
      id: "hs",
      title: "Hue and saturation",
      kind: "scatter",
      x_label: "Brightness (%)",
      y_label: "Power (W)",
      source: "hs.csv",
      series: [
        {
          label: null,
          color: null,
          points: [
            { x: 20, y: 1, color: null, id: "hs:51:1:255" },
            { x: 60, y: 2, color: null, id: "hs:153:1:255" },
            { x: 100, y: 4, color: null, id: "hs:255:1:255" },
            { x: 100, y: 3, color: null, id: "hs:255:21845:255" },
          ],
        },
      ],
    };
    expect(densityPlotFor([brightness, rail], rail)?.id).toBe("hs");
    const xs = densityValues(rail, brightness);
    expect(xs.filter((x) => x < 1)).toHaveLength(3);
    expect(xs.filter((x) => x > 100)).toHaveLength(1);
  });

  it("groups brightness-plot points into iso-lines by CT, hue, and saturation", () => {
    const ct: PlotPoint[] = [
      { x: 20, y: 1, color: null, id: "color_temp:51:400" },
      { x: 80, y: 3, color: null, id: "color_temp:204:400" },
      { x: 50, y: 2, color: null, id: "color_temp:128:250" },
    ];
    expect(isoGroups(ct, "mired").map((group) => group.map((point) => point.x))).toEqual([
      [20, 80],
    ]);
    const hs: PlotPoint[] = [
      { x: 10, y: 1, color: null, id: "hs:26:1:255" },
      { x: 90, y: 4, color: null, id: "hs:230:1:255" },
      { x: 40, y: 2, color: null, id: "hs:102:1:128" },
      { x: 70, y: 3, color: null, id: "hs:179:21845:128" },
    ];
    expect(hsSatFromPointId("hs:26:1:255")).toBe(255);
    expect(isoGroups(hs, "hue")[0]?.map((point) => point.x)).toEqual([10, 40, 90]);
    expect(isoGroups(hs, "sat").map((group) => group.map((point) => point.x))).toEqual([
      [10, 90],
      [40, 70],
    ]);
  });

  it("names the sample-count line without a unit or capital", () => {
    expect(samplesAtNoun({ id: "color_temp", x_label: "Brightness (%)" })).toBe(
      "brightness",
    );
    expect(
      samplesAtNoun({ id: "color_temp_max_bri", x_label: "Color temp (K)" }),
    ).toBe("color temperature");
    expect(samplesAtNoun({ id: "hs_max_bri", x_label: "Hue (°)" })).toBe("hue");
  });
});
