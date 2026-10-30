"""Temperature tuning calibration belongs to the physical device definition."""
from ldcon.core.device_conditions import finite


def coefficient(settings):
    temperature=(settings or {}).get('temperature',{})
    if not isinstance(temperature,dict):raise ValueError('Temperature device settings must be a mapping')
    value=temperature.get('frequency_hz_per_c')
    if value is None:return None
    value=finite(value,'temperature device frequency_hz_per_c')
    if value==0:raise ValueError('Temperature device tuning coefficient cannot be zero')
    return value
