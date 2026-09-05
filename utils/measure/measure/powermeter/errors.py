class PowerMeterError(Exception):
    pass


class OutdatedMeasurementError(PowerMeterError):
    pass


class ZeroReadingError(PowerMeterError):
    pass


class ApiConnectionError(PowerMeterError):
    pass


class UnsupportedFeatureError(PowerMeterError):
    pass


class WitnessDisagreementError(PowerMeterError):
    """A witness meter read a value too far from the primary's for the sample to be trusted."""
