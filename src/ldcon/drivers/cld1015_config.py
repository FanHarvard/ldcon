"""Compile-time configuration; SI current and degrees Celsius at the public API."""
import math


def limits(value, name):
    if value is None:
        return None
    result = tuple(float(v) for v in value)
    if len(result) != 2 or not all(math.isfinite(v) for v in result) or result[0] >= result[1]:
        raise ValueError(f"{name} must be an increasing finite pair")
    return result


def configuration(resource=None, *, current_limits_a=None, temperature_limits_c=None,
                  timeout_s=5, mock=False, modulation_role=None,
                  endpoint=None, instrument=None, temperature_conditions=None, temperature_writes_enabled=True, current_conditions=None, control_settings=None, current_writes_enabled=False, ld_enable_writes_enabled=False):
    if type(ld_enable_writes_enabled) is not bool:
        raise ValueError('ld_enable_writes_enabled must be a boolean')
    return dict(resource=resource, current_limits_a=limits(current_limits_a, "current_limits_a"),
                temperature_limits_c=limits(temperature_limits_c, "temperature_limits_c"),
                timeout_s=float(timeout_s), mock=bool(mock), modulation_role=modulation_role,
                endpoint=endpoint, instrument=instrument, temperature_conditions=temperature_conditions, temperature_writes_enabled=temperature_writes_enabled, current_conditions=current_conditions, control_settings=control_settings or {}, current_writes_enabled=current_writes_enabled, ld_enable_writes_enabled=ld_enable_writes_enabled)


def check(value, bounds, name):
    value = float(value)
    if bounds is None:
        raise ValueError(f"configure {name} safety limits before changing the setpoint")
    if not math.isfinite(value) or not bounds[0] <= value <= bounds[1]:
        raise ValueError(f"{name} {value} outside {bounds}")
    return value
