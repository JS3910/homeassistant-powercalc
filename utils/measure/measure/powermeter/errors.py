class PowerMeterError(Exception):
    pass


class OutdatedMeasurementError(PowerMeterError):
    pass


class WaitingForMeterError(PowerMeterError):
    """The meter is live but has no usable sample yet. Retry; do not abort."""


class ZeroReadingError(PowerMeterError):
    pass


class StandbyLikeOnError(PowerMeterError):
    """On-state sample is closer to standby than to the lowest established on-load.

    This is not a 0 W meter reading. Treating it as ``ZeroReadingError`` made a
    real dim-on sample abort the session with the low-load troubleshooting text.
    """


class ApiConnectionError(PowerMeterError):
    pass


class UnsupportedFeatureError(PowerMeterError):
    pass


class WitnessDisagreementError(PowerMeterError):
    """A witness meter read a value too far from the primary's for the sample to be trusted."""
