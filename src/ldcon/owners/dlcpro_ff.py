"""Explicit FF compatibility modes; never infer an unavailable master/reference."""
import math

from ldcon.core.contracts import DeviceError


def finite(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError('Finite FF/readback value required')
    return value


def mode(policy):
    value = policy.get('ff_mode', 'managed_reference')
    if value not in ('managed_reference', 'preserve_existing'):
        raise ValueError('Unknown DLC FF mode')
    return value


def validate_binding(policy, channel):
    if mode(policy) == 'managed_reference':
        finite(channel['ff_reference_v'])
    elif policy['ff_enabled'] and channel.get('ff_delta_verified') is not True:
        raise ValueError('Preserved FF needs a verified PZT-setpoint to total-current delta relation')


def require_preserved(policy, snapshot):
    if (snapshot['ff_enabled'] is not policy['ff_enabled'] or
            snapshot['ff_factor_ma_per_v'] != policy['ff_factor_ma_per_v']):
        raise DeviceError('Preserved DLC FF differs from policy; no FF writes permitted')


def inward_piezo_return(bounds, snapshot, destinations):
    """Permit an explicit inward return from a measured edge discrepancy.

    The old command and every destination must remain in the original bounds.
    This is never used for lock admission, scans, or unrelated axis moves.
    """
    low, high = bounds
    measured = finite(snapshot['piezo']['measured'])
    previous = finite(snapshot['piezo']['setpoint'])
    points = [finite(p) for p in destinations]
    if not points or not low <= previous <= high or not all(low <= p <= high for p in points):
        return False
    return ((measured > high and all(p < previous for p in points)) or
            (measured < low and all(p > previous for p in points)))


def preserved_currents(policy, snapshot, piezo_bounds, current_offset):
    """Relative prediction from readable total setpoint, not an invented reference.

    Only commissioned static PZT setting -> total current changes qualify. Native
    scan and other modulation are excluded from this initial compatibility mode.
    Measured current remains independent evidence, never substituted for a command.
    """
    require_preserved(policy, snapshot)
    pzt = finite(snapshot['piezo']['setpoint'])
    offset = finite(snapshot['current']['setpoint'])
    total = finite(snapshot['current']['total_setpoint'])
    measured = finite(snapshot['current']['measured'])
    factor = finite(policy['ff_factor_ma_per_v']) if policy['ff_enabled'] else 0.
    delta_offset = finite(current_offset) - offset
    return [total, measured] + [total + delta_offset + factor * (finite(v)-pzt)
                                for v in piezo_bounds]
