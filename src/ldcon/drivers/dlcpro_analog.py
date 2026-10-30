"""Documented Fine inputs and ARC readback, using the owner's console.

Fine readbacks have no device acquisition timestamp. The host request interval
is retained and must not be presented as a synchronous ADC/WSU acquisition.
"""
import time


INPUTS = {'fine1': ('io:fine-1:value-act', 0),
          'fine2': ('io:fine-2:value-act', 1)}


def inspect(driver, channel, *, clock=time.monotonic, utc=time.time):
    result = {'inputs': {}, 'routes': {}}
    for name, (path, _) in INPUTS.items():
        start, wall = clock(), utc()
        value = driver.read(path)
        result['inputs'][name] = dict(value_v=value, started_s=start,
            completed_s=clock(), started_unix_s=wall, completed_unix_s=utc(),
            acquisition_timestamp_available=False)
    for axis in ('pc', 'cc', 'tc'):
        result['routes'][axis] = {key: driver.read(driver.path(channel,
            f'{axis}:external-input:{key}')) for key in ('enabled', 'signal', 'factor')}
    return result


def read_input(driver, name, *, clock=time.monotonic, utc=time.time):
    path, _ = INPUTS[name]
    start, wall = clock(), utc()
    value = driver.read(path)
    return dict(value_v=value, started_s=start, completed_s=clock(),
                started_unix_s=wall, completed_unix_s=utc(),
                acquisition_timestamp_available=False)
