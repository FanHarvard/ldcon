"""Pure single-channel split-input AWG record and physical envelope proof."""
from copy import deepcopy
import math
from experiment_core.configuration import Parameter

from ldcon.drivers.staircase import compile_plan, validate_channels
from ldcon.jobs.modulation_sweep import validate_request


def finite(value, name):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(name + ' must be finite')
    return float(value)


def classify_policy_change(before, after):
    """Only the explicitly authorized input gain is operational, at idle.

    Full dictionaries are compared after removing that exact validated leaf;
    combined edits, new wiring, guards and actuator calibration remain safety.
    Configuration and active-shot revisions still bind the measurement scale.
    """
    configuration(before['awg_sweep'])
    configuration(after['awg_sweep'])
    old, new = deepcopy(before), deepcopy(after)
    old['awg_sweep'].pop('adc_v_per_awg_v', None)
    new['awg_sweep'].pop('adc_v_per_awg_v', None)
    if old == new:
        return Parameter('operational', 'V/V', 'input measurement calibration', 'idle')
    return Parameter('safety', '', '', 'cycle')


def configuration(value):
    keys = {'commissioned', 'wiring', 'awg', 'pzt_input', 'target_input',
            'pzt_v_per_input_v', 'frequency_hz_per_input_v', 'adc_zero_v',
            'adc_tolerance_v', 'adc_max_interval_s', 'sample_rate_hz',
            'arm_timeout_s', 'max_duration_s', 'max_half_span_hz',
            'watchdog_s', 'internal_ff_arc_verified'}
    if not isinstance(value, dict) or not keys <= set(value) <= keys | {'adc_v_per_awg_v', 'track_plateaus', 'tracking_io_budget_s'}:
        raise ValueError('Explicit DLC AWG configuration required: ' + ', '.join(sorted(keys)))
    p = deepcopy(value)
    if type(p.get('track_plateaus', False)) is not bool:
        raise ValueError('track_plateaus must be an explicit boolean')
    p['adc_v_per_awg_v'] = finite(p.get('adc_v_per_awg_v', 1.), 'adc_v_per_awg_v')
    if p['adc_v_per_awg_v'] <= 0:
        raise ValueError('Positive split-input ADC gain required')
    if p['commissioned'] is not True or p['internal_ff_arc_verified'] is not True:
        raise ValueError('DLC ARC/FF and input wiring commissioning required')
    if p['wiring'] != 'one_awg_output_split_to_two_fine_inputs':
        raise ValueError('This backend requires one physical split AWG output')
    if {p['pzt_input'], p['target_input']} != {'fine1', 'fine2'}:
        raise ValueError('Distinct Fine1 and Fine2 routes required')
    p['awg'] = validate_channels([p['awg']])[0]
    a = p['awg']
    if (a['profile'] != 'siglent_sdg2000x' or not a.get('broker') or
            a['load'] != 'HZ' or a['reference'] != 'absolute' or a['idle_v'] != 0 or
            not -4 <= a['min_v'] < 0 < a['max_v'] <= 4):
        raise ValueError('DLC requires brokered SDG2000X, HZ, zero idle, and reviewed bounds within ±4 V')
    for key in keys - {'commissioned', 'wiring', 'awg', 'pzt_input', 'target_input', 'internal_ff_arc_verified'}:
        p[key] = finite(p[key], key)
        if key not in ('adc_zero_v', 'pzt_v_per_input_v', 'frequency_hz_per_input_v') and p[key] <= 0:
            raise ValueError(key + ' must be positive')
    if not p['pzt_v_per_input_v'] or not p['frequency_hz_per_input_v']:
        raise ValueError('Signed PZT and frequency calibration required')
    if p['adc_max_interval_s'] >= p['watchdog_s']:
        raise ValueError('ADC interval must fit inside the shot heartbeat deadline')
    budget = finite(p.get('tracking_io_budget_s', p['adc_max_interval_s']), 'tracking_io_budget_s')
    if not p['adc_max_interval_s'] <= budget < p['watchdog_s']:
        raise ValueError('Tracking I/O budget must cover ADC freshness and fit inside watchdog')
    return p


def compile_record(config, request, snapshot, policy, *, pzt_scale=1.):
    p = configuration(config)
    plan = validate_request(request)
    if (plan.get('shape', 'staircase') != 'staircase' or plan['unit'] != 'Hz' or
            plan['axis'] != 'servo_modulation' or plan['trigger_mode'] not in ('external', 'external_rising')):
        raise ValueError('DLC AWG accepts one external-trigger frequency staircase, without software fallback')
    if not .5 <= finite(pzt_scale, 'pzt_scale') <= 2:
        raise ValueError('PZT scale outside reviewed [0.5, 2] range')
    if plan['duration_s'] > p['max_duration_s'] or max(map(abs, plan['levels'])) > p['max_half_span_hz']:
        raise ValueError('Shot exceeds the commissioned duration/span')
    budget = p.get('tracking_io_budget_s', p['adc_max_interval_s']) if p.get('track_plateaus') else p['adc_max_interval_s']
    if plan['dwell_s'] <= 2*budget:
        raise ValueError('Dwell must exceed two worst-case ADC observation intervals')
    if not any(abs(f/p['frequency_hz_per_input_v'])*p['adc_v_per_awg_v'] > 4*p['adc_tolerance_v'] for f in plan['levels']):
        raise ValueError('Record has no input transition distinguishable from the ADC uncertainty')
    channel = p['awg']; role = channel['role']
    levels = [f/p['frequency_hz_per_input_v'] for f in plan['levels']]
    record = compile_plan([channel], {role: levels}, start=0., dwell=plan['dwell_s'],
        settle=plan['settle_s'], acquire=plan['acquire_s'], sample_rate=p['sample_rate_hz'],
        lead=plan['lead_s'], tail=plan['tail_s'], frequency_labels=plan['levels'])
    # No hidden record extension: the BLACS pulse timeline must contain it all.
    if abs(record.metadata['duration_s'] - plan['duration_s']) > 1e-9:
        raise ValueError('Record length must be a multiple of 16 AWG samples; adjust tail_s')
    h = snapshot['hardware']; factor = p['pzt_v_per_input_v']*pzt_scale
    wave = record.waves[role]
    # Include the complete electrical envelope, not only intended discrete codes.
    v = [wave['offset_v'] - wave['amplitude_vpp']/2, wave['offset_v'] + wave['amplitude_vpp']/2]
    total_pzt = [h['piezo']['setpoint'] + factor*x for x in v]
    lo, hi = policy['axes']['piezo']['bounds']
    if not all(lo <= x <= hi for x in total_pzt):
        raise ValueError('ARC record exceeds laser PZT output bounds')
    # A separately commissioned ARC -> total-current relation is mandatory.
    if h['ff_enabled'] != policy['ff_enabled'] or h['ff_factor_ma_per_v'] != policy['ff_factor_ma_per_v']:
        raise ValueError('Internal DLC FF differs from the laser policy')
    ff = h['ff_factor_ma_per_v'] if h['ff_enabled'] else 0.
    currents = [h['current']['total_setpoint'] + ff*factor*x for x in v]
    lo, hi = policy['axes']['current']['bounds']
    if not all(lo <= x <= hi for x in currents):
        raise ValueError('ARC plus internal FF exceeds laser current bounds')
    return record, dict(pzt_factor_v_per_v=factor, voltage_envelope_v=v,
        pzt_envelope_v=total_pzt, total_current_envelope_ma=currents,
        waveform_duration_s=record.metadata['duration_s'],
        software_pi_during_record=False, tec_during_record=False)
