from dataclasses import dataclass


@dataclass(frozen=True)
class MeasurementParameters:
    """Canonical timing and native light-grid parameters."""

    min_brightness: int = 1
    max_brightness: int = 255
    # Kelvin is what the UI shows; the LUT and planner still walk integer mireds.
    # Defaults are the static limits so a light's own range is not clamped until
    # the user narrows these sliders. The setup page snaps the thumbs to the
    # selected light (dummy LightInfo is 150-500 mired -> 2000-6666 K).
    min_kelvin: int = 1500
    max_kelvin: int = 10000
    min_sat: int = 1
    max_sat: int = 255
    min_hue: int = 1
    max_hue: int = 65535
    bri_bri_steps: int = 1
    ct_bri_steps: int = 5
    # Mired, hue, and saturation are swept by count (see runner/light_plan.py), not a
    # native step size -- the user-facing knob is "how many sweeps to divide the range
    # into." Saturation is not symmetric: 1 sweep is full saturation only (the color
    # primary), 2 is full then the midpoint, 3+ includes the unsaturated end and then
    # bisects. Brightness *_steps stay a native increment unless the matching
    # *_bisection flag is on, in which case that field is a sweep count too. *_all
    # walks every integer in the live range and ignores the number.
    ct_mired_divisions: int = 17
    hs_bri_steps: int = 32
    hs_hue_divisions: int = 24
    hs_sat_divisions: int = 5
    effect_bri_steps: int = 40
    bri_bri_bisection: bool = False
    ct_bri_bisection: bool = False
    hs_bri_bisection: bool = False
    effect_bri_bisection: bool = False
    bri_bri_all: bool = False
    ct_bri_all: bool = False
    hs_bri_all: bool = False
    effect_bri_all: bool = False
    ct_mired_all: bool = False
    hs_hue_all: bool = False
    hs_sat_all: bool = False
    brightness_descending: bool = False
    # Smart envelope sampling invents points from the measured power band instead of a
    # cartesian HS/CT grid. smart_delta is per-rail interior fill spacing; smart_border_delta
    # is equidistant spacing along the 1D border (100% color sweep + hottest/coldest rails).
    # Both are plot-space units (brightness 0-100 vs watts 0-P_peak). A sample on one
    # color does not cover a hole on another. Manual sliders stay as they are when this
    # is off.
    smart_sampling: bool = False
    smart_delta: float = 8.0
    smart_border_delta: float = 8.0
    # When on, interior fill throws random points (biased toward high power) and
    # skips any guess that lands within the current radius of an existing sample.
    # The radius starts at smart_delta and shrinks toward smart_dart_min_delta so
    # a long-running session keeps adding detail. Off keeps the finite packed fill.
    smart_dart: bool = False
    smart_dart_min_delta: float = 2.0
    # Optional lamp rating (W). Copied from LightMeasurementRequest.rated_power_w.
    # 0 means unset. Smart CT uses it as one extra guess rail (weaker white faded
    # in on top of the stronger, up to this budget). It is not the measured peak.
    rated_power: float = 0.0
    measure_time_effect: int = 180
    measure_time_effect_min: int = 20
    measure_time_effect_convergence_window: int = 15
    measure_time_effect_convergence_abs: float = 0.1
    measure_time_effect_convergence_rel: float = 0.01
    sleep_initial: float = 10
    sleep_standby: float = 20
    sleep_time: float = 2
    # Opt-in: 0 keeps sleep_time a fixed wait after every light change. Above 0, sleep_time
    # instead becomes an upper bound and the run polls the power meter, proceeding as soon
    # as the trailing window is settled (watched for settle_window_seconds, enough samples,
    # spread within this tolerance or settle_tolerance_w) -- falling back to the full
    # sleep_time if it never stabilizes. See LightRunner._settle.
    settle_tolerance_pct: float = 0.0
    # Absolute floor used with settle_tolerance_pct: a track is flat when its spread is
    # within the percentage *or* this many watts. 0.1 W is one Shelly LSB -- at ~1.4 W
    # that step is already ~7%, so a few-percent band can never succeed without this.
    settle_tolerance_w: float = 0.1
    settle_window_seconds: float = 1.0
    settle_poll_interval_seconds: float = 0.25
    # Blind wait after HA reports the new brightness, before settle detection may
    # accept a plateau. HA often reports the command while the LED (and therefore
    # the power reading) is still at the previous point -- a flat leftover is
    # then accepted in one window. 0 disables. Unused when settle detection is off.
    settle_min_wait: float = 2.0
    sleep_time_sample: float = 1
    sleep_time_hue: float = 5
    sleep_time_sat: float = 10
    sleep_time_ct: float = 10
    sleep_time_effect_change: float = 5
    sleep_time_nudge: float = 10
    pulse_time_nudge: float = 2
    sample_count: int = 1
    max_retries: int = 5
    max_nudges: int = 0
    fast_test_mode: bool = False
    prompt_resume: bool = False
    csv_add_datetime_column: bool = False
