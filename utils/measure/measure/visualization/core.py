"""Build frontend-neutral plot specifications from measurement artifacts."""

from collections.abc import Collection, Iterable, Mapping, Sequence
import colorsys
import csv
from dataclasses import dataclass, replace
from enum import StrEnum
import gzip
import json
import math
from pathlib import Path
from typing import TextIO, TypeGuard

from measure.controller.light.const import LutMode
from measure.runner.light_plan import (
    CSV_HEADERS,
    ColorTempVariation,
    HsVariation,
    Variation,
    variation_from_csv_row,
)
from measure.runner.smart_envelope import MeasuredPoint, ct_interest_targets, hs_interest_targets
from measure.visualization.point_meta import PlotStat, point_stats, variation_point_id, variation_rail_id
from measure.request import (
    ChargingMeasurementRequest,
    FanMeasurementRequest,
    LightMeasurementRequest,
    MeasurementRequest,
    RecorderMeasurementRequest,
    SpeakerMeasurementRequest,
)
from measure.tuning import MeasurementParameters

_DEFAULT_COLOR = "#5488e8"
_EFFECT_COLORS = (
    "#5488e8",
    "#61d4a3",
    "#f0b45b",
    "#d27df2",
    "#ff7b72",
    "#68c9e8",
    "#b8d45f",
    "#f28bb7",
)
_COMPOSITE_STRATEGY = "composite"
_LINEAR_STRATEGY = "linear"
_LIGHT_MODE_ORDER = (LutMode.BRIGHTNESS, LutMode.COLOR_TEMP, LutMode.HS, LutMode.EFFECT)
_POWER_AXIS_LABEL = "Power (W)"


class PlotKind(StrEnum):
    SCATTER = "scatter"
    LINE = "line"
    CYLINDER = "cylinder"


@dataclass(frozen=True, slots=True)
class PlotPoint:
    x: float
    y: float
    color: str | None = None
    inherited: bool = False
    id: str | None = None
    rail: str | None = None
    stats: tuple[PlotStat, ...] = ()
    ignored: bool = False
    editable: bool = False
    interest: str | None = None
    z: float | None = None


@dataclass(frozen=True, slots=True)
class PlotMarker:
    x: float
    label: str


@dataclass(frozen=True, slots=True)
class PlotSeries:
    label: str | None
    color: str | None
    points: tuple[PlotPoint, ...]


@dataclass(frozen=True, slots=True)
class PlotSpec:
    id: str
    title: str
    kind: PlotKind
    x_label: str
    y_label: str
    source: str
    series: tuple[PlotSeries, ...]
    x_min: float | None = None
    x_max: float | None = None
    markers: tuple[PlotMarker, ...] = ()


@dataclass(frozen=True, slots=True)
class PlotBuildResult:
    plots: tuple[PlotSpec, ...]
    warnings: tuple[str, ...]


class PlotDataError(ValueError):
    """Raised when an artifact cannot produce a valid plot."""


def build_session_plots(
    request: MeasurementRequest,
    files: Mapping[str, Path],
    *,
    max_scatter_points: int = 10_000,
    max_line_points: int = 4_000,
    inherited_keys: Mapping[LutMode, Collection[Variation]] | None = None,
    ignored_ids: Collection[str] | None = None,
    measured_modes_only: bool = False,
) -> PlotBuildResult:
    """Build every meaningful plot available for a persisted measurement.

    ``inherited_keys`` are LUT points copied from a refine seed. They stay on the
    plot so the shape is visible. The live running view fades them so this run's
    new points stand out; a finished session draws every point at full weight.

    By default every LUT CSV on disk is plotted, including modes copied into a
    refine but not selected for this run. ``measured_modes_only`` restricts that
    to ``request.modes`` — the in-progress view uses it so the live plots match
    what this run is measuring.
    """

    candidates = _session_plot_candidates(
        request,
        files,
        max_scatter_points=max_scatter_points,
        max_line_points=max_line_points,
        measured_modes_only=measured_modes_only,
    )
    plots: list[PlotSpec] = []
    warnings: list[str] = []
    for path, source, mode, max_points in candidates:
        try:
            plots.append(
                _build_plot(
                    path,
                    source=source,
                    color_mode=mode,
                    max_points=max_points,
                    inherited_keys=inherited_keys,
                    ignored_ids=ignored_ids,
                    request=request,
                ),
            )
            plots.extend(
                _max_bri_color_plot(
                    path,
                    source=source,
                    mode=mode,
                    inherited_keys=inherited_keys,
                    ignored_ids=ignored_ids,
                    request=request,
                ),
            )
        except (OSError, PlotDataError, json.JSONDecodeError, ValueError, csv.Error) as error:
            # ValueError/csv.Error also cover a row half-written by a still-running
            # measurement (e.g. a numeric field flushed to disk mid-write) when this is
            # called for a live preview rather than a finished session.
            warnings.append(f"Could not plot {source}: {error}")
    return PlotBuildResult(plots=tuple(plots), warnings=tuple(warnings))


def _session_plot_candidates(
    request: MeasurementRequest,
    files: Mapping[str, Path],
    *,
    max_scatter_points: int,
    max_line_points: int,
    measured_modes_only: bool = False,
) -> list[tuple[Path, str, LutMode | None, int]]:
    model_root = request.model_id or "measurement"
    if isinstance(request, LightMeasurementRequest):
        return _light_plot_candidates(
            request,
            files,
            model_root,
            max_scatter_points,
            measured_modes_only=measured_modes_only,
        )
    if isinstance(request, RecorderMeasurementRequest):
        return _single_plot_candidate(files, f"{model_root}/{request.export_filename}", max_line_points)
    if isinstance(request, SpeakerMeasurementRequest | FanMeasurementRequest | ChargingMeasurementRequest):
        return _single_plot_candidate(files, f"{model_root}/model.json", max_line_points)
    return []


def _light_plot_candidates(
    request: LightMeasurementRequest,
    files: Mapping[str, Path],
    model_root: str,
    max_points: int,
    *,
    measured_modes_only: bool = False,
) -> list[tuple[Path, str, LutMode | None, int]]:
    return [
        (*candidate, mode, max_points)
        for mode in _LIGHT_MODE_ORDER
        if not measured_modes_only or mode in request.modes
        if (candidate := _preferred_file(files, f"{model_root}/{mode.value}.csv")) is not None
    ]


def _single_plot_candidate(
    files: Mapping[str, Path],
    name: str,
    max_points: int,
) -> list[tuple[Path, str, LutMode | None, int]]:
    candidate = _preferred_file(files, name)
    return [(*candidate, None, max_points)] if candidate is not None else []


def build_plot_from_file(
    path: str | Path,
    *,
    color_mode: str | LutMode | None = None,
    max_points: int | None = None,
) -> PlotSpec:
    """Build one plot from a standalone CSV, CSV.GZ or model.json artifact."""

    file_path = Path(path)
    resolved_mode = _parse_mode(color_mode) if color_mode is not None else None
    return _build_plot(
        file_path,
        source=file_path.name,
        color_mode=resolved_mode,
        max_points=max_points,
    )


def model_has_linear_calibration(data: object) -> bool:
    """Return whether model data declares a linear calibration."""

    return bool(_linear_calibration_configs(data))


def limit_plot_points(plot: PlotSpec, max_points: int) -> PlotSpec:
    """Return the plot with every series downsampled to at most max_points."""

    limit = _limit_line if plot.kind is PlotKind.LINE else _limit_scatter
    limited: PlotSpec = replace(
        plot,
        series=tuple(replace(series, points=limit(series.points, max_points)) for series in plot.series),
    )
    return limited


def _build_plot(
    path: Path,
    *,
    source: str,
    color_mode: LutMode | None,
    max_points: int | None,
    inherited_keys: Mapping[LutMode, Collection[Variation]] | None = None,
    ignored_ids: Collection[str] | None = None,
    request: MeasurementRequest | None = None,
) -> PlotSpec:
    if path.name.endswith(".json"):
        return _linear_plot(path, source=source, max_points=max_points)
    mode = color_mode or _mode_from_filename(path)
    if mode is not None:
        return _light_plot(
            path,
            source=source,
            mode=mode,
            max_points=max_points,
            inherited_keys=inherited_keys,
            ignored_ids=ignored_ids,
            request=request,
        )
    return _recorder_plot(path, source=source, max_points=max_points)


def _light_plot(
    path: Path,
    *,
    source: str,
    mode: LutMode,
    max_points: int | None,
    inherited_keys: Mapping[LutMode, Collection[Variation]] | None = None,
    ignored_ids: Collection[str] | None = None,
    request: MeasurementRequest | None = None,
) -> PlotSpec:
    expected_fields = {
        LutMode.BRIGHTNESS: {"bri", "watt"},
        LutMode.COLOR_TEMP: {"bri", "mired", "watt"},
        LutMode.HS: {"bri", "hue", "sat", "watt"},
        LutMode.EFFECT: {"effect", "bri", "watt"},
    }[mode]
    with _open_csv(path) as file:
        reader = csv.DictReader(file)
        if reader.fieldnames is None or not expected_fields.issubset(reader.fieldnames):
            raise PlotDataError(f"expected CSV columns: {', '.join(sorted(expected_fields))}")
        rows = list(reader)

    seed = set(inherited_keys.get(mode, ())) if inherited_keys else set()
    skipped = set(ignored_ids or ())
    if mode is LutMode.EFFECT:
        series = _effect_series(rows, max_points, seed, skipped)
    else:
        series = _single_light_series(rows, mode, max_points, seed, skipped)

    title = {
        LutMode.BRIGHTNESS: "Brightness",
        LutMode.COLOR_TEMP: "Color temperature",
        LutMode.HS: "Hue and saturation",
        LutMode.EFFECT: "Effects",
    }[mode]
    x_min, x_max = _brightness_axis_domain(request, series)
    return PlotSpec(
        id=mode.value,
        title=title,
        kind=PlotKind.SCATTER,
        x_label="Brightness (%)",
        y_label=_POWER_AXIS_LABEL,
        source=source,
        series=series,
        x_min=x_min,
        x_max=x_max,
    )


def _brightness_pct(bri: int) -> float:
    return bri / 255 * 100


def _brightness_axis_domain(
    request: MeasurementRequest | None,
    series: Sequence[PlotSeries],
) -> tuple[float | None, float | None]:
    """Span the configured brightness range, or existing points, whichever is wider."""

    xs = [point.x for item in series for point in item.points]
    data_min = min(xs) if xs else None
    data_max = max(xs) if xs else None
    if not isinstance(request, LightMeasurementRequest):
        return data_min, data_max
    configured_min = _brightness_pct(request.parameters.min_brightness)
    configured_max = _brightness_pct(request.parameters.max_brightness)
    return (
        configured_min if data_min is None else min(configured_min, data_min),
        configured_max if data_max is None else max(configured_max, data_max),
    )


def _effect_series(
    rows: list[dict[str, str | None]],
    max_points: int | None,
    inherited: Collection[Variation] = (),
    ignored_ids: Collection[str] = (),
) -> tuple[PlotSeries, ...]:
    grouped: dict[str, list[PlotPoint]] = {}
    for row in rows:
        effect = str(row.get("effect", "")).strip()
        point = _light_point(
            row,
            LutMode.EFFECT,
            inherited=_row_is_inherited(row, LutMode.EFFECT, inherited),
            ignored_ids=ignored_ids,
        )
        if effect and point is not None:
            grouped.setdefault(effect, []).append(point)
    if not grouped:
        raise PlotDataError("no valid effect measurements found")
    return tuple(
        PlotSeries(
            label=effect,
            color=_EFFECT_COLORS[index % len(_EFFECT_COLORS)],
            points=_limit_scatter(points, max_points),
        )
        for index, (effect, points) in enumerate(grouped.items())
    )


def _single_light_series(
    rows: list[dict[str, str | None]],
    mode: LutMode,
    max_points: int | None,
    inherited: Collection[Variation] = (),
    ignored_ids: Collection[str] = (),
) -> tuple[PlotSeries, ...]:
    points = [
        point
        for row in rows
        if (
            point := _light_point(
                row,
                mode,
                inherited=_row_is_inherited(row, mode, inherited),
                ignored_ids=ignored_ids,
            )
        )
        is not None
    ]
    if not points:
        raise PlotDataError("no valid light measurements found")
    return (
        PlotSeries(
            label=None,
            color=_DEFAULT_COLOR if mode is LutMode.BRIGHTNESS else None,
            points=_limit_scatter(points, max_points),
        ),
    )


def _row_is_inherited(
    row: Mapping[str, str | None],
    mode: LutMode,
    inherited: Collection[Variation],
) -> bool:
    if not inherited:
        return False
    variation = variation_from_csv_row([str(row.get(name) or "") for name in CSV_HEADERS[mode]], mode)
    return variation is not None and variation in inherited


def _light_point(
    row: Mapping[str, str | None],
    mode: LutMode,
    *,
    inherited: bool = False,
    ignored_ids: Collection[str] = (),
) -> PlotPoint | None:
    brightness = _finite_float(row.get("bri"))
    power = _finite_float(row.get("watt"))
    if brightness is None or power is None:
        return None
    color = None
    if mode is LutMode.COLOR_TEMP:
        mired = _finite_float(row.get("mired"))
        if mired is None or mired <= 0:
            return None
        color = _mired_color(mired)
    elif mode is LutMode.HS:
        hue = _finite_float(row.get("hue"))
        saturation = _finite_float(row.get("sat"))
        if hue is None or saturation is None:
            return None
        # HSV, not HLS, and value fixed at 1.0 rather than fed this point's actual
        # brightness. HSV value=1 is what makes full saturation render as the pure hue
        # (red, green, ...) and saturation=0 fade to white as it drops -- the picture this
        # swatch is meant to show, independent of where the point sits on the brightness
        # sweep, exactly like the CT branch above already is via `_mired_color`. HLS's
        # equivalent axis is *lightness*, not brightness: lightness=1 is white regardless
        # of hue or saturation (that was the original bug here), and there's no fixed
        # lightness that reproduces this fade -- 0.5 gives a pure hue at any saturation
        # instead of fading toward white as saturation falls.
        red, green, blue = colorsys.hsv_to_rgb(hue / 65535, saturation / 255, 1.0)
        color = _rgb_color(red * 255, green * 255, blue * 255)
    # HA's own light attribute is 1-255; the rest of the UI (and the request parameters
    # this profile was built from) talks about brightness in %, so the plot should too.
    return _annotated_point(
        row,
        mode,
        x=brightness / 255 * 100,
        y=power,
        color=color,
        inherited=inherited,
        ignored_ids=ignored_ids,
    )


def _annotated_point(
    row: Mapping[str, str | None],
    mode: LutMode,
    *,
    x: float,
    y: float,
    color: str | None,
    inherited: bool,
    ignored_ids: Collection[str],
    interest: str | None = None,
    z: float | None = None,
) -> PlotPoint:
    variation = variation_from_csv_row([str(row.get(name) or "") for name in CSV_HEADERS[mode]], mode)
    point_id = variation_point_id(mode, variation) if variation is not None else None
    watt = z if z is not None else y
    stats = point_stats(mode, variation, watt) if variation is not None else ()
    if interest:
        stats = (*stats, PlotStat("Interest", interest))
    return PlotPoint(
        x=x,
        y=y,
        color=color,
        inherited=inherited,
        id=point_id,
        rail=variation_rail_id(mode, variation) if variation is not None else None,
        stats=stats,
        ignored=bool(point_id and point_id in ignored_ids),
        editable=point_id is not None,
        interest=interest,
        z=z,
    )


def _max_bri_color_plot(
    path: Path,
    *,
    source: str,
    mode: LutMode | None,
    inherited_keys: Mapping[LutMode, Collection[Variation]] | None = None,
    ignored_ids: Collection[str] | None = None,
    request: MeasurementRequest | None = None,
) -> tuple[PlotSpec, ...]:
    """Power vs color. CT includes every brightness so the UI can slice the rail;
    HS stays the 100% hue ring the envelope scout walks first, plus a cylinder
    of every brightness (the UI slices that one)."""

    if mode not in {LutMode.COLOR_TEMP, LutMode.HS}:
        return ()
    expected = {"bri", "watt", *(("mired",) if mode is LutMode.COLOR_TEMP else ("hue", "sat"))}
    with _open_csv(path) as file:
        reader = csv.DictReader(file)
        if reader.fieldnames is None or not expected.issubset(reader.fieldnames):
            return ()
        rows = [row for row in reader if _finite_float(row.get("bri")) is not None]
    if not rows:
        return ()
    max_bri = max(int(_finite_float(row["bri"]) or 0) for row in rows)
    top = [row for row in rows if int(_finite_float(row["bri"]) or 0) == max_bri]
    seed = set(inherited_keys.get(mode, ())) if inherited_keys else set()
    skipped = set(ignored_ids or ())
    if mode is LutMode.COLOR_TEMP:
        interests = _ct_interest_by_mired(rows, request)
        points = [
            _annotated_point(
                row,
                mode,
                x=1_000_000.0 / mired,
                y=power,
                color=_mired_color(mired),
                inherited=_row_is_inherited(row, mode, seed),
                ignored_ids=skipped,
                interest=interests.get(int(mired)),
            )
            for row in rows
            if (mired := _finite_float(row.get("mired"))) and mired > 0
            if (power := _finite_float(row.get("watt"))) is not None
        ]
        if len(points) < 2:
            return ()
        points.sort(key=lambda point: point.x)
        markers = tuple(
            PlotMarker(x=1_000_000.0 / ct, label=reason) for ct, reason in sorted(interests.items())
        )
        return (
            PlotSpec(
                id="color_temp_max_bri",
                title="Color temperature at 100%",
                kind=PlotKind.SCATTER,
                x_label="Color temp (K)",
                y_label=_POWER_AXIS_LABEL,
                source=source,
                series=(PlotSeries(label=None, color=None, points=tuple(points)),),
                markers=markers,
            ),
        )
    sats = [int(_finite_float(row.get("sat")) or 0) for row in top]
    if not sats:
        return ()
    sat_max = max(sats)
    sat_min = min(sats)
    mid_sats = {sat for sat in sats if sat not in {sat_min, sat_max}}
    interests = _hs_interest_by_hue(rows, request)
    series: list[PlotSeries] = []
    vivid = _hs_hue_points(top, {sat_max}, seed, skipped, interests)
    if vivid:
        series.append(PlotSeries(label=None, color=None, points=tuple(vivid)))
    if mid_sats:
        mid = _hs_hue_points(top, mid_sats, seed, skipped, interests)
        if mid:
            series.append(PlotSeries(label=None, color=None, points=tuple(mid)))
    if sat_min != sat_max:
        white = _hs_hue_points(top, {sat_min}, seed, skipped, interests)
        if white:
            series.append(PlotSeries(label=None, color="#f4f1ea", points=tuple(white)))
    if sum(len(item.points) for item in series) < 2:
        return ()
    markers = tuple(
        PlotMarker(x=hue / 65535 * 360, label=reason) for hue, reason in sorted(interests.items())
    )
    rail = PlotSpec(
        id="hs_max_bri",
        title="Hue at 100%",
        kind=PlotKind.SCATTER,
        x_label="Hue (°)",
        y_label=_POWER_AXIS_LABEL,
        source=source,
        series=tuple(series),
        markers=markers,
    )
    cylinder = _hs_cylinder_plot(rows, seed, skipped, interests, markers, source)
    return (rail, cylinder) if cylinder is not None else (rail,)


def _interest_parameters(
    request: MeasurementRequest | None,
    max_bri: int,
) -> MeasurementParameters:
    """Use the brightness the 100% plot actually draws, not the request cap.

    An EXTEND refine can lower ``max_brightness`` (this run walks 1–199) while the
    CSV still holds the seed's 255-rail. Interest has to look at that rail or the
    arrows vanish from the plot that shows the cliff.
    """

    base = (
        request.parameters
        if isinstance(request, LightMeasurementRequest)
        else MeasurementParameters()
    )
    if base.max_brightness == max_bri:
        return base
    return replace(base, max_brightness=max_bri)


def _ct_interest_by_mired(
    rows: Sequence[Mapping[str, str | None]],
    request: MeasurementRequest | None,
) -> dict[int, str]:
    measured: list[MeasuredPoint] = []
    for row in rows:
        brightness = _finite_float(row.get("bri"))
        mired = _finite_float(row.get("mired"))
        power = _finite_float(row.get("watt"))
        if brightness is None or mired is None or mired <= 0 or power is None:
            continue
        measured.append(MeasuredPoint(ColorTempVariation(bri=int(brightness), ct=int(mired)), power))
    if not measured:
        return {}
    max_bri = max(point.variation.bri for point in measured)
    return dict(ct_interest_targets(measured, _interest_parameters(request, max_bri)))


def _hs_interest_by_hue(
    rows: Sequence[Mapping[str, str | None]],
    request: MeasurementRequest | None,
) -> dict[int, str]:
    measured: list[MeasuredPoint] = []
    for row in rows:
        brightness = _finite_float(row.get("bri"))
        hue = _finite_float(row.get("hue"))
        saturation = _finite_float(row.get("sat"))
        power = _finite_float(row.get("watt"))
        if brightness is None or hue is None or saturation is None or power is None:
            continue
        measured.append(
            MeasuredPoint(HsVariation(bri=int(brightness), hue=int(hue), sat=int(saturation)), power)
        )
    if not measured:
        return {}
    max_bri = max(point.variation.bri for point in measured)
    return dict(hs_interest_targets(measured, _interest_parameters(request, max_bri)))


def _hs_hue_points(
    rows: Sequence[Mapping[str, str | None]],
    saturations: Collection[int],
    inherited: Collection[Variation],
    ignored_ids: Collection[str] = (),
    interests: Mapping[int, str] | None = None,
) -> list[PlotPoint]:
    allowed = set(saturations)
    points: list[PlotPoint] = []
    for row in rows:
        saturation = int(_finite_float(row.get("sat")) or -1)
        if saturation not in allowed:
            continue
        hue = _finite_float(row.get("hue"))
        power = _finite_float(row.get("watt"))
        if hue is None or power is None:
            continue
        red, green, blue = colorsys.hsv_to_rgb(hue / 65535, saturation / 255, 1.0)
        points.append(
            _annotated_point(
                row,
                LutMode.HS,
                x=hue / 65535 * 360,
                y=power,
                color=_rgb_color(red * 255, green * 255, blue * 255),
                inherited=_row_is_inherited(row, LutMode.HS, inherited),
                ignored_ids=ignored_ids,
                interest=(interests or {}).get(int(hue)),
            )
        )
    points.sort(key=lambda point: point.x)
    return points


def _hs_cylinder_plot(
    rows: Sequence[Mapping[str, str | None]],
    inherited: Collection[Variation],
    ignored_ids: Collection[str],
    interests: Mapping[int, str],
    markers: tuple[PlotMarker, ...],
    source: str,
) -> PlotSpec | None:
    """Hue as angle, saturation as radius, power as height — every brightness slice."""

    max_bri = max((int(_finite_float(row.get("bri")) or 0) for row in rows), default=0)
    points = _hs_cylinder_points(rows, inherited, ignored_ids, interests, max_bri)
    if len(points) < 2:
        return None
    return PlotSpec(
        id="hs_cylinder",
        title="Hue / saturation at 100%",
        kind=PlotKind.CYLINDER,
        x_label="Hue (°)",
        y_label="Saturation",
        source=source,
        series=(PlotSeries(label=None, color=None, points=tuple(points)),),
        markers=markers,
    )


def _hs_cylinder_points(
    rows: Sequence[Mapping[str, str | None]],
    inherited: Collection[Variation],
    ignored_ids: Collection[str],
    interests: Mapping[int, str],
    max_bri: int,
) -> list[PlotPoint]:
    points: list[PlotPoint] = []
    for row in rows:
        hue = _finite_float(row.get("hue"))
        saturation = _finite_float(row.get("sat"))
        power = _finite_float(row.get("watt"))
        brightness = _finite_float(row.get("bri"))
        if hue is None or saturation is None or power is None:
            continue
        value = 1.0 if brightness is None else max(0.0, min(1.0, brightness / 255))
        red, green, blue = colorsys.hsv_to_rgb(hue / 65535, saturation / 255, value)
        bri = int(brightness) if brightness is not None else -1
        points.append(
            _annotated_point(
                row,
                LutMode.HS,
                x=hue / 65535 * 360,
                y=saturation,
                z=power,
                color=_rgb_color(red * 255, green * 255, blue * 255),
                inherited=_row_is_inherited(row, LutMode.HS, inherited),
                ignored_ids=ignored_ids,
                interest=interests.get(int(hue)) if bri == max_bri else None,
            )
        )
    return points


def _linear_plot(path: Path, *, source: str, max_points: int | None) -> PlotSpec:
    data = json.loads(path.read_text(encoding="utf-8"))
    linear_configs = _linear_calibration_configs(data)
    if not linear_configs:
        raise PlotDataError("model does not contain linear calibration data")

    series_count = len(linear_configs)
    series = tuple(
        _linear_series(
            linear_config,
            label=_condition_label(condition, index) if series_count > 1 else None,
            color=_EFFECT_COLORS[(index - 1) % len(_EFFECT_COLORS)],
            max_points=max_points,
        )
        for index, (condition, linear_config) in enumerate(linear_configs, start=1)
    )

    device_type = data.get("device_type") if isinstance(data, dict) else None
    title, x_label = _linear_labels(device_type if isinstance(device_type, str) else None)
    return PlotSpec(
        id="calibration",
        title=title,
        kind=PlotKind.LINE,
        x_label=x_label,
        y_label=_POWER_AXIS_LABEL,
        source=source,
        series=series,
    )


def _linear_calibration_configs(data: object) -> tuple[tuple[object, Mapping[str, object]], ...]:
    if not isinstance(data, dict):
        return ()

    strategy = data.get("calculation_strategy")
    if strategy == _LINEAR_STRATEGY:
        linear_config = data.get("linear_config")
        return ((None, linear_config),) if _has_calibration(linear_config) else ()
    if strategy != _COMPOSITE_STRATEGY:
        return ()

    composite_config = data.get("composite_config")
    strategies: object
    if isinstance(composite_config, list):
        strategies = composite_config
    elif isinstance(composite_config, dict):
        strategies = composite_config.get("strategies")
    else:
        return ()
    if not isinstance(strategies, list):
        return ()

    configs: list[tuple[object, Mapping[str, object]]] = []
    for strategy_config in strategies:
        if not isinstance(strategy_config, dict):
            continue
        linear_config = strategy_config.get(_LINEAR_STRATEGY)
        if _has_calibration(linear_config):
            configs.append((strategy_config.get("condition"), linear_config))
    return tuple(configs)


def _has_calibration(config: object) -> TypeGuard[Mapping[str, object]]:
    return isinstance(config, dict) and isinstance(config.get("calibrate"), list)


def _linear_series(
    linear_config: Mapping[str, object],
    *,
    label: str | None,
    color: str,
    max_points: int | None,
) -> PlotSeries:
    calibrate = linear_config.get("calibrate")
    if not isinstance(calibrate, list):  # pragma: no cover - guarded by _linear_calibration_configs
        raise PlotDataError("model does not contain linear calibration data")

    points: list[PlotPoint] = []
    for entry in calibrate:
        if not isinstance(entry, str):
            continue
        left, separator, right = entry.partition(" -> ")
        if not separator:
            continue
        x_value = _finite_float(left)
        power = _finite_float(right)
        if x_value is not None and power is not None:
            points.append(PlotPoint(x=x_value, y=power))
    if not points:
        raise PlotDataError("no valid linear calibration entries found")
    points.sort(key=lambda point: point.x)
    return PlotSeries(
        label=label,
        color=color,
        points=_limit_line(points, max_points),
    )


def _condition_label(condition: object, strategy_index: int) -> str:
    if not isinstance(condition, dict):
        return "Unconditional"

    condition_type = condition.get("condition")
    if condition_type in {"and", "or", "not"}:
        conditions = condition.get("conditions")
        if isinstance(conditions, list):
            labels = [_condition_label(item, strategy_index) for item in conditions]
            if labels:
                if condition_type == "not":
                    return f"NOT ({' AND '.join(labels)})"
                return f" {str(condition_type).upper()} ".join(labels)
    if condition_type == "state":
        subject = condition.get("attribute") or _condition_entity_label(condition.get("entity_id"))
        state = condition.get("state")
        if subject and state is not None:
            return f"{str(subject).replace('_', ' ')} = {_condition_value_label(state)}"
    return f"Strategy {strategy_index}"


def _condition_entity_label(entity_id: object) -> str | None:
    if isinstance(entity_id, list):
        entity_id = entity_id[0] if entity_id else None
    if not isinstance(entity_id, str):
        return None
    if entity_id == "[[entity]]":
        return "state"
    if entity_id.startswith("[[") and entity_id.endswith("]]"):
        entity_id = entity_id[2:-2]
    _, separator, value = entity_id.partition(":")
    return (value if separator else entity_id).replace("_", " ")


def _condition_value_label(value: object) -> str:
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    if isinstance(value, bool):
        return str(value).lower()
    return str(value)


def _recorder_plot(path: Path, *, source: str, max_points: int | None) -> PlotSpec:
    points = _stream_recorder_points(path, max_points)
    if not points:
        raise PlotDataError("no valid recorder measurements found")
    return PlotSpec(
        id="recording",
        title="Power recording",
        kind=PlotKind.LINE,
        x_label="Elapsed time (s)",
        y_label=_POWER_AXIS_LABEL,
        source=source,
        series=(PlotSeries(label=None, color=_DEFAULT_COLOR, points=points),),
    )


def _iter_recorder_points(path: Path) -> Iterable[PlotPoint]:
    if path.name.lower().endswith(".jsonl"):
        yield from _iter_jsonl_recorder_points(path)
        return
    with _open_csv(path) as file:
        for row in csv.reader(file):
            if len(row) < 2:
                continue
            elapsed = _finite_float(row[0])
            power = _finite_float(row[1])
            if elapsed is not None and power is not None:
                yield PlotPoint(x=elapsed, y=power)


def _iter_jsonl_recorder_points(path: Path) -> Iterable[PlotPoint]:
    with path.open(encoding="utf-8-sig") as file:
        for line in file:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            elapsed = _finite_float(row.get("elapsed_seconds"))
            power = _finite_float(row.get("power"))
            if elapsed is not None and power is not None:
                yield PlotPoint(x=elapsed, y=power)


def _stream_recorder_points(path: Path, max_points: int | None) -> tuple[PlotPoint, ...]:
    if max_points is None:
        return tuple(_iter_recorder_points(path))

    point_count = sum(1 for _ in _iter_recorder_points(path))
    if point_count <= max_points:
        return tuple(_iter_recorder_points(path))
    return _downsample_recorder_points(path, point_count, max_points)


def _downsample_recorder_points(path: Path, point_count: int, max_points: int) -> tuple[PlotPoint, ...]:
    if max_points <= 1:
        return tuple(point for index, point in enumerate(_iter_recorder_points(path)) if index == 0)
    if max_points < 4:
        selected_indexes = {round(index * (point_count - 1) / (max_points - 1)) for index in range(max_points)}
        return tuple(point for index, point in enumerate(_iter_recorder_points(path)) if index in selected_indexes)
    return _recorder_extrema(path, point_count, max_points)


def _recorder_extrema(path: Path, point_count: int, max_points: int) -> tuple[PlotPoint, ...]:
    bucket_count = max(1, (max_points - 2) // 2)
    bucket_size = math.ceil((point_count - 2) / bucket_count)
    selected: list[tuple[int, PlotPoint]] = []
    bucket_minimum: tuple[int, PlotPoint] | None = None
    bucket_maximum: tuple[int, PlotPoint] | None = None
    current_bucket = -1
    last: tuple[int, PlotPoint] | None = None
    for index, point in enumerate(_iter_recorder_points(path)):
        if index == 0:
            selected.append((index, point))
            continue
        if index == point_count - 1:
            last = (index, point)
            continue
        bucket_index = (index - 1) // bucket_size
        if bucket_index != current_bucket:
            _append_bucket_extrema(selected, bucket_minimum, bucket_maximum)
            bucket_minimum = None
            bucket_maximum = None
            current_bucket = bucket_index
        candidate = (index, point)
        if bucket_minimum is None or point.y < bucket_minimum[1].y:
            bucket_minimum = candidate
        if bucket_maximum is None or point.y > bucket_maximum[1].y:
            bucket_maximum = candidate
    _append_bucket_extrema(selected, bucket_minimum, bucket_maximum)
    if last is not None:
        selected.append(last)
    return tuple(point for _, point in selected[:max_points])


def _append_bucket_extrema(
    selected: list[tuple[int, PlotPoint]],
    minimum: tuple[int, PlotPoint] | None,
    maximum: tuple[int, PlotPoint] | None,
) -> None:
    if minimum is None or maximum is None:
        return
    selected.extend(sorted({minimum[0]: minimum, maximum[0]: maximum}.values()))


def _preferred_file(files: Mapping[str, Path], name: str) -> tuple[Path, str] | None:
    if name in files:
        return files[name], name
    compressed_name = f"{name}.gz"
    if compressed_name in files:
        return files[compressed_name], compressed_name
    return None


def _parse_mode(value: str | LutMode) -> LutMode:
    if isinstance(value, LutMode):
        return value
    normalized = value.removesuffix("s") if value == "effects" else value
    try:
        return LutMode(normalized)
    except ValueError as error:
        raise PlotDataError(f"unsupported light mode: {value}") from error


def _mode_from_filename(path: Path) -> LutMode | None:
    name = path.name.removesuffix(".gz").removesuffix(".csv")
    if name == "effects":
        name = "effect"
    try:
        return LutMode(name)
    except ValueError:
        return None


def _linear_labels(device_type: str | None) -> tuple[str, str]:
    if device_type == "smart_speaker":
        return "Speaker calibration", "Volume (%)"
    if device_type == "fan":
        return "Fan calibration", "Fan speed (%)"
    if device_type in {"vacuum_robot", "lawn_mower_robot"}:
        return "Charging calibration", "Battery level (%)"
    return "Linear calibration", "Value"


def _limit_scatter(points: Sequence[PlotPoint], max_points: int | None) -> tuple[PlotPoint, ...]:
    if max_points is None or len(points) <= max_points:
        return tuple(points)
    if max_points <= 1:
        return (points[0],)
    return tuple(points[round(index * (len(points) - 1) / (max_points - 1))] for index in range(max_points))


def _limit_line(points: Sequence[PlotPoint], max_points: int | None) -> tuple[PlotPoint, ...]:
    if max_points is None or len(points) <= max_points:
        return tuple(points)
    if max_points < 4:
        return _limit_scatter(points, max_points)

    indexed = list(enumerate(points))
    interior = indexed[1:-1]
    bucket_count = max(1, (max_points - 2) // 2)
    bucket_size = math.ceil(len(interior) / bucket_count)
    selected: list[tuple[int, PlotPoint]] = [indexed[0]]
    for start in range(0, len(interior), bucket_size):
        bucket = interior[start : start + bucket_size]
        minimum = min(bucket, key=lambda item: item[1].y)
        maximum = max(bucket, key=lambda item: item[1].y)
        selected.extend(sorted({minimum[0]: minimum, maximum[0]: maximum}.values()))
    selected.append(indexed[-1])
    return tuple(point for _, point in sorted(selected)[:max_points])


def _open_csv(path: Path) -> TextIO:
    # utf-8-sig strips a leading BOM if present (some measurement CSVs carry one) and is otherwise identical to utf-8.
    if path.name.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8-sig", newline="")
    return path.open(encoding="utf-8-sig", newline="")


def _finite_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _mired_color(mired: float) -> str:
    temperature = min(40_000.0, max(1_000.0, 1_000_000.0 / mired)) / 100.0
    if temperature <= 66:
        red = 255.0
        green = 99.4708025861 * math.log(temperature) - 161.1195681661
    else:
        red = 329.698727446 * math.pow(temperature - 60, -0.1332047592)
        green = 288.1221695283 * math.pow(temperature - 60, -0.0755148492)
    if temperature >= 66:
        blue = 255.0
    elif temperature <= 19:
        blue = 0.0
    else:
        blue = 138.5177312231 * math.log(temperature - 10) - 305.0447927307
    return _rgb_color(red, green, blue)


def _rgb_color(red: float, green: float, blue: float) -> str:
    channels: Iterable[int] = (round(min(255.0, max(0.0, channel))) for channel in (red, green, blue))
    return "#" + "".join(f"{channel:02x}" for channel in channels)
