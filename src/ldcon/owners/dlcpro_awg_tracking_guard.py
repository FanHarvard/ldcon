"""Pure admission and host-bracket epoch checks for optional AWG tracking."""
from copy import deepcopy
import math


def admit(proof, snapshot, policy, hardware, control):
    """Bound the software base plus every ARC value, including cleanup at zero."""
    result = deepcopy(proof)
    h = snapshot['hardware']
    base, total = h['piezo']['setpoint'], h['current']['total_setpoint']
    slope = policy['axes']['piezo']['frequency_hz_per_unit']
    if not math.isfinite(slope) or not slope:
        raise ValueError('Tracking needs a finite nonzero PZT slope')
    for key in ('kp', 'ki_per_s', 'integral_limit_v', 'capture_hz', 'tolerance_hz'):
        value = control[key]
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError('Invalid admitted tracking PI settings')
    if min(control['integral_limit_v'], control['capture_hz'], control['tolerance_hz']) <= 0:
        raise ValueError('Positive admitted tracking limits required')
    # Initial P is subtracted for continuous entry. Both initial and later
    # errors are inside capture, so this bounds their difference plus integral.
    limit = control['integral_limit_v'] + 2*control['kp']*control['capture_hz']/abs(slope)
    volts = proof.get('admitted_voltage_envelope_v', proof['voltage_envelope_v'])
    arc = [proof['pzt_factor_v_per_v']*v for v in [0., *volts]]
    arc_low, arc_high = min(arc), max(arc)
    bounds = {}
    for axis in ('piezo', 'current'):
        bounds[axis] = [max(policy['axes'][axis]['bounds'][0], hardware['axes'][axis]['bounds'][0]),
                        min(policy['axes'][axis]['bounds'][1], hardware['axes'][axis]['bounds'][1])]
    ff = policy['ff_factor_ma_per_v'] if policy['ff_enabled'] else 0.
    low = max(base-limit, bounds['piezo'][0], bounds['piezo'][0]-arc_low)
    high = min(base+limit, bounds['piezo'][1], bounds['piezo'][1]-arc_high)
    if ff:
        allowed = sorted(base+(i-total)/ff for i in bounds['current'])
        low, high = max(low, allowed[0]-arc_low), min(high, allowed[1]-arc_high)
    elif not bounds['current'][0] <= total <= bounds['current'][1]:
        raise ValueError('Tracking current anchor outside approved bounds')
    tolerance = policy['axes']['piezo']['readback_tolerance']
    # Commands are inset; even the extremal accepted readback stays safe.
    low, high = low+tolerance, high-tolerance
    if not low <= base <= high:
        raise ValueError('Tracking has no continuous safe correction envelope')
    physical = [low-tolerance+arc_low, high+tolerance+arc_high]
    currents = sorted(total+ff*(v-base) for v in physical)
    result.update(software_pi_during_record=True, tec_during_record=False,
        tracking=dict(origin_base_v=base, origin_total_current_ma=total,
            base_command_bounds_v=[low, high], readback_tolerance_v=tolerance,
            arc_envelope_v=[arc_low, arc_high], pzt_envelope_v=physical,
            total_current_envelope_ma=currents, current_factor_ma_per_v=ff,
            absolute_bounds=bounds, control=deepcopy(control)))
    return result


def interior(session, config, adc):
    """Return one verified plateau for the entire ADC request bracket, or None.

    These are host observations, not hardware acquisition timestamps. A repeat
    at the same coordinate in another plateau is a distinct epoch.
    """
    if session['trigger_bounds_s'] is None:
        return None
    low, high = session['trigger_bounds_s']
    plan = session['request']['plan']
    for index, level in enumerate(plan['levels']):
        left = high+plan['lead_s']+index*plan['dwell_s']
        right = low+plan['lead_s']+(index+1)*plan['dwell_s']
        expected = level/config['frequency_hz_per_input_v']*config['adc_v_per_awg_v']+config['adc_zero_v']
        if (left < adc['started_s'] <= adc['completed_s'] < right
                and abs(adc['value_v']-expected) <= config['adc_tolerance_v']):
            return dict(index=index, target_hz=session['origin_target_hz']+level,
                        left_s=left, right_s=right)
    return None


def verify_base(value, proof):
    p = proof['tracking']; tolerance = p['readback_tolerance_v']
    if not math.isfinite(value):
        raise ValueError('Nonfinite tracking base readback')
    if not p['base_command_bounds_v'][0]-tolerance <= value <= p['base_command_bounds_v'][1]+tolerance:
        raise ValueError('Tracking base exceeds admitted correction envelope')
    for arc in p['arc_envelope_v']:
        voltage = value+arc
        current = p['origin_total_current_ma']+p['current_factor_ma_per_v']*(voltage-p['origin_base_v'])
        for axis, actual in (('piezo', voltage), ('current', current)):
            lo, hi = p['absolute_bounds'][axis]
            if not lo <= actual <= hi:
                raise ValueError('Tracking combined '+axis+' envelope exceeded')
