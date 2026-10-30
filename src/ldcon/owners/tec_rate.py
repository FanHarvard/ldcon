"""TEC delivery slices derived from slew and normal hardware cadence.

There is no independent user-set step limit in rate-only mode. Missed cycles
never accumulate a larger write allowance. A physical owner's normal update
interval, rather than the slow centering-loop period, defines one slice.
"""


def rate_only(device):
    return device['absolute_conditions']['axes']['temperature'].get('max_step') is None


def delivery_step(slew_per_s, update_interval_s):
    from ldcon.core.device_conditions import finite
    slew, interval = finite(slew_per_s), finite(update_interval_s)
    if slew <= 0 or interval <= 0:
        raise ValueError('TEC slew and normal update interval must be positive')
    return slew * interval


def continuation_due(native, now):
    """Continue an already planned TEC ramp; do not start a new control epoch."""
    if not rate_only(native.o.device) or native.planner is None or native.tec_ff_paused:
        return False
    pending = native.pending
    if not pending or pending[0].get('reason') != 'PZT slow centering':
        return False
    policy = native.o.policies()['temperature']
    return now - native.o.last_write['temperature'] + 1e-9 >= policy.update_interval_s
