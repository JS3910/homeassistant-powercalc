from dataclasses import dataclass


@dataclass(frozen=True)
class MeasurementParameters:
    """Canonical timing and native light-grid parameters."""

    min_brightness: int = 1
    max_brightness: int = 255
    min_sat: int = 1
    max_sat: int = 255
    min_hue: int = 1
    max_hue: int = 65535
    bri_bri_steps: int = 1
    ct_bri_steps: int = 5
    # Mired and hue are swept in bisection order (see runner/light_plan.py), not linearly,
    # so a native step size no longer describes anything meaningful about the sweep -- the
    # user-facing knob is "how many points to divide the range into" instead. Brightness
    # and saturation keep a plain dense sweep, so their *_steps fields are unaffected.
    ct_mired_divisions: int = 17
    hs_bri_steps: int = 32
    hs_hue_divisions: int = 24
    hs_sat_steps: int = 32
    effect_bri_steps: int = 40
    measure_time_effect: int = 180
    measure_time_effect_min: int = 20
    measure_time_effect_convergence_window: int = 15
    measure_time_effect_convergence_abs: float = 0.1
    measure_time_effect_convergence_rel: float = 0.01
    sleep_initial: int = 10
    sleep_standby: int = 20
    sleep_time: float = 2
    # Opt-in: 0 keeps sleep_time a fixed wait after every light change. Above 0, sleep_time
    # instead becomes an upper bound and the run polls the power meter, proceeding as soon
    # as the reading has been flat within this tolerance for settle_window_seconds --
    # falling back to the full sleep_time if it never stabilizes. See LightRunner._settle.
    settle_tolerance_pct: float = 0.0
    settle_window_seconds: float = 1.0
    settle_poll_interval_seconds: float = 0.25
    sleep_time_sample: int = 1
    sleep_time_hue: int = 5
    sleep_time_sat: int = 10
    sleep_time_ct: int = 10
    sleep_time_effect_change: int = 5
    sleep_time_nudge: float = 10
    pulse_time_nudge: float = 2
    sample_count: int = 1
    max_retries: int = 5
    max_nudges: int = 0
    fast_test_mode: bool = False
    prompt_resume: bool = False
    csv_add_datetime_column: bool = False
