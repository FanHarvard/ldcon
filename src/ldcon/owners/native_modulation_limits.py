"""Pure host move budgets for the ESP external-current-modulation path.

An absent flag preserves the historical step/slew ceilings. An explicit false
removes only those ceilings; callers must still prove absolute, activation and
physical DAC bounds and enforce the minimum write interval. Direct CLD current
commands do not use this policy or these helpers.
"""
from ldcon.drivers.esp32_servo import channel_metadata

FIELD = 'external_modulation_step_slew_limited'


def limited(policy):
    value = policy.get(FIELD, True)
    if type(value) is not bool:
        raise ValueError(FIELD + ' must be boolean')
    return value


def budget_a(policy):
    """Current-change ceiling, or None when this path is explicitly exempt."""
    if not limited(policy):
        return None
    return min(policy['max_step_a'], policy['max_slew_a_per_s'] * policy['update_interval_s'])


def budget_codes(policy):
    """Whole native codes; the exempt maximum is only the physical DAC span."""
    budget = budget_a(policy)
    if budget is None:
        return 65535
    dac = channel_metadata(policy['dac'])
    c = policy['calibration']
    per_code = dac['voltage_step_v'] / (1 << (16 - dac['bits']))
    current = abs(c['current_a_per_v'] * per_code) * (1 + c['gain_relative_uncertainty'])
    return int(budget / current)


def report(policy):
    enforced = limited(policy)
    return dict(external_modulation_step_slew_limited=enforced,
                host_max_step_a=policy['max_step_a'] if enforced else None,
                host_max_slew_a_per_s=policy['max_slew_a_per_s'] if enforced else None,
                configured_max_step_a=policy['max_step_a'],
                configured_max_slew_a_per_s=policy['max_slew_a_per_s'])
