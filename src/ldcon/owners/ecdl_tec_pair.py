"""Verified TEC/current feedforward pairs within one owner supervision cycle.

These are sequential, read-back-verified writes, not an atomic hardware write.
Uncertain output still faults the owner. A known pre-write deferral retains the
unapplied compensation; it never repeats the already applied TEC step.
"""
import math

from ldcon.core.contracts import MeasurementUnavailable


def slew_elapsed(axis, policy, profile, since_write):
    cap = profile['tec_dt_cap_s'] if axis == 'temperature' else policy.update_interval_s
    return max(0., min(since_write, cap))


def ff_target(planner, outputs):
    cp = planner.policies['current']
    coefficient = planner.p['tec_current_feedforward']['current_per_temperature']
    raw = outputs['current'] + coefficient * (outputs['temperature'] - planner.ff_temperature) + planner.ff_remainder
    desired = round(raw / cp.resolution) * cp.resolution if cp.resolution else raw
    return desired, raw - desired


def check_current(native, state, value):
    """Prove the full dependent move before TEC, and again on actual readback."""
    o = native.o
    cp = o.policies()['current']
    previous = state['outputs']['current']
    bounds = o.device['absolute_conditions']['axes']['current']['activation_range']
    low = max(cp.low, o.activation['current'] + bounds[0],
              native.planner.origin['current'] - native.p['current_span'])
    high = min(cp.high, o.activation['current'] + bounds[1],
               native.planner.origin['current'] + native.p['current_span'])
    half = state['current']['amplitude_vpp'] / 2
    if not math.isfinite(value) or value - half < low - 1e-12 or value + half > high + 1e-12:
        raise MeasurementUnavailable('TEC current feedforward outside approved current envelope')
    if abs(value - previous) < max(cp.resolution / 2, 1e-12):
        return False
    since = o.clock() - o.last_write['current']
    if since + 1e-9 < cp.update_interval_s:
        raise MeasurementUnavailable('TEC current feedforward awaiting current update interval')
    if abs(value - previous) > min(cp.max_step, cp.max_slew_per_s * min(since, cp.update_interval_s)) + 1e-9:
        raise MeasurementUnavailable('TEC current feedforward exceeds current step/slew allowance')
    from ldcon.owners.ecdl_background_parameters import permit_output, permit_proposal
    if not permit_proposal(native, 'current', value) or not permit_output(native, 'current', value):
        raise MeasurementUnavailable('TEC current feedforward outside runtime grant')
    return True


def enabled(native, proposal):
    return bool(proposal['axis'] == 'temperature'
                and proposal.get('reason') == 'PZT slow centering'
                and native.p.get('tec_current_feedforward')
                and not native.tec_ff_paused
                and not getattr(native, '_parameter_returning', False))


def preflight(native, state, temperature):
    projected = {**state['outputs'], 'temperature': temperature}
    desired, _ = ff_target(native.planner, projected)
    check_current(native, state, desired)


def compensate(native, result):
    """Compensate the verified TEC delta; return an explicit pair receipt."""
    planner = native.planner
    desired, remainder = ff_target(planner, result['outputs'])
    planner.ff_temperature = result['outputs']['temperature']
    planner.ff_remainder = remainder
    planner.ff_target = desired
    receipt = dict(temperature=result['outputs']['temperature'], current_target=desired)
    try:
        if check_current(native, result, desired):
            after = native.o._dispatch(dict(action='locked_move', lease=native.o.lease,
                                            axis='current', value=desired))
        else:
            after = result
        planner.ack_current(after['outputs'], 'TEC current feedforward')
        receipt.update(status='verified', current=after['outputs']['current'])
    except MeasurementUnavailable as error:
        if native.o.fault:
            raise
        receipt.update(status='pending', reason=str(error))
    # The usual known pre-write broker refusal must also retain the pending FF.
    # Unknown/post-write exceptions propagate to the owner fault handler.
    native.o.audit('tec_current_pair', **receipt)
    return receipt
