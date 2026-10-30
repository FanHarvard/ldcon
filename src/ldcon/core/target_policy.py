"""Target admission, separate from actuator limits and PI capture.

Relative ranges follow the saved target between requests. A multi-step request
must freeze its reference at admission, not grow its range after each step.
Legacy absolute bounds remain readable until explicitly migrated.
"""
import math


def number(value, name, *, positive=False):
    if (type(value) not in (int, float) or not math.isfinite(value)
            or positive and value <= 0):
        raise ValueError(name + ' must be a finite' + (' positive' if positive else '') + ' number')
    return float(value)


def laser_target(laser):
    block = laser.get('target')
    if block is None:
        return None
    if not isinstance(block, dict) or set(block) - {
            'frequency_hz', 'allowed_change_hz', 'bounds_hz', 'max_change_hz', 'note'}:
        raise ValueError('Invalid laser.target fields')
    frequency = number(block.get('frequency_hz'), 'target.frequency_hz', positive=True)
    relative, absolute = block.get('allowed_change_hz'), block.get('bounds_hz')
    if relative is not None and absolute is not None:
        raise ValueError('Use target.allowed_change_hz or legacy bounds_hz, not both')
    if relative is not None:
        if (not isinstance(relative, list) or len(relative) != 2
                or not number(relative[0], 'lower change') <= 0 <= number(relative[1], 'upper change')):
            raise ValueError('target.allowed_change_hz must be [lower <= 0, upper >= 0]')
    if absolute is not None:
        if (not isinstance(absolute, list) or len(absolute) != 2
                or not number(absolute[0], 'lower bound', positive=True)
                < number(absolute[1], 'upper bound', positive=True)):
            raise ValueError('target.bounds_hz must be [low, high]')
        if not absolute[0] <= frequency <= absolute[1]:
            raise ValueError('target.frequency_hz is outside target.bounds_hz')
    if block.get('max_change_hz') is not None:
        number(block['max_change_hz'], 'target.max_change_hz', positive=True)
    return block


def target_bounds(laser, *, reference_hz=None):
    """Effective inclusive frequency bounds; zero/negative destinations still fail."""
    block = laser_target(laser)
    if block is None:
        return None
    relative = block.get('allowed_change_hz')
    if relative is not None:
        reference = number(block['frequency_hz'] if reference_hz is None else reference_hz,
                           'target reference', positive=True)
        bounds = [reference + v for v in relative]
        if not all(math.isfinite(v) for v in bounds):
            raise ValueError('Target range overflow')
        return bounds
    return list(block['bounds_hz']) if block.get('bounds_hz') is not None else None


def validate_destination(laser, destination_hz, *, reference_hz=None):
    destination = number(destination_hz, 'new target', positive=True)
    bounds = target_bounds(laser, reference_hz=reference_hz)
    if bounds is None:
        raise ValueError('target.allowed_change_hz is not configured (legacy bounds_hz is also accepted)')
    if not bounds[0] <= destination <= bounds[1]:
        raise ValueError('New target is outside the admitted target range')
    return bounds


def validate_transition(before, after):
    """Check direct configuration saves against the PREVIOUS saved target.

    The caller separately authorizes edits to the allowed range. In particular,
    merely centering the candidate range on the candidate value grants nothing.
    The step cap belongs to execution, not to an unlocked configuration save.
    """
    old, new = laser_target(before), laser_target(after)
    if old and new and old['frequency_hz'] != new['frequency_hz']:
        validate_destination(after, new['frequency_hz'], reference_hz=old['frequency_hz'])
