"""Explicit finite staircase experiments; no device writes or physical defaults."""
import copy
import math

OPERATION = 'optimize.sweep_feedforward'
KEYS = {'sweep_card_id', 'candidates', 'baseline_candidate_id', 'conditions',
        'criteria', 'max_baseline_shift_hz', 'confirmation_runs', 'max_trials',
        'timeout_s', 'poll_s', 'cleanup_timeout_s', 'analysis_wait_s', 'sweep_parameters'}
CONDITION_KEYS = {'id', 'levels_hz', 'dwell_s', 'edge_s', 'lead_s', 'tail_s',
                  'repetitions', 'precondition_s'}
SWEEP_KEYS = {'sample_rate_hz', 'sample_interval_s', 'max_sample_age_s',
              'duration_s', 'ready', 'recentre_before_ff', 'full_data_log_ms'}


def number(value, name, *, positive=False, nonnegative=False):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(name + ' must be a finite number')
    if positive and value <= 0 or nonnegative and value < 0:
        raise ValueError(name + ' has an invalid sign')
    return value


def integer(value, name, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(name + ' must be an integer in the declared finite range')


def parameters(card, overrides):
    from ldcon.core.interface_schema import identifier
    from ldcon.jobs.sweep_ff_analysis import validate_criteria
    p = copy.deepcopy({**card.get('parameters', {}), **overrides})
    if set(p) != KEYS:
        raise ValueError('Sweep FF parameters require explicit fields; missing=' +
                         ','.join(sorted(KEYS-set(p))) + '; unknown=' + ','.join(sorted(set(p)-KEYS)))
    identifier(p['sweep_card_id'])
    for key in ('max_baseline_shift_hz', 'timeout_s', 'poll_s', 'cleanup_timeout_s', 'analysis_wait_s'):
        number(p[key], key, positive=True)
    if p['timeout_s'] > 86400 or p['cleanup_timeout_s'] > 86400:
        raise ValueError('Card and cleanup deadlines must not exceed 24 hours')
    integer(p['confirmation_runs'], 'confirmation_runs', 1, 100)
    integer(p['max_trials'], 'max_trials', 1, 10000)
    p['criteria'] = validate_criteria(p['criteria'])
    candidates = p['candidates']
    if not isinstance(candidates, list) or not 2 <= len(candidates) <= 100:
        raise ValueError('Declare a baseline and at least one FF candidate')
    ids = set()
    family = None
    for candidate in candidates:
        if not isinstance(candidate, dict):raise ValueError('FF candidate must be a mapping')
        keys = set(candidate)-{'id'}
        if keys not in ({'ldc_ff_v_per_hz', 'pzt_ff_gain_scale'}, {'ff_gain_scale'}):
            raise ValueError('A candidate is one joint LDC/PZT pair or one native-current FF scale')
        if family is not None and keys != family:raise ValueError('Do not mix FF routes')
        family = keys
        identifier(candidate.get('id'))
        if candidate['id'] in ids:raise ValueError('Duplicate candidate id')
        ids.add(candidate['id'])
        for key in keys:number(candidate[key], key)
    if p['baseline_candidate_id'] not in ids:raise ValueError('Baseline must name a declared candidate')
    conditions = p['conditions']
    if not isinstance(conditions, list) or not 1 <= len(conditions) <= 100:
        raise ValueError('Declare finite staircase conditions')
    ids = set()
    for condition in conditions:
        if not isinstance(condition, dict) or set(condition) != CONDITION_KEYS:
            raise ValueError('Each condition requires explicit waveform, cadence and preconditioning')
        identifier(condition['id'])
        if condition['id'] in ids:raise ValueError('Duplicate condition id')
        ids.add(condition['id'])
        for key in ('dwell_s', 'lead_s', 'tail_s'):number(condition[key], key, positive=True)
        for key in ('edge_s', 'precondition_s'):number(condition[key], key, nonnegative=True)
        if condition['edge_s'] >= condition['dwell_s']:raise ValueError('Edge must fit inside dwell')
        if p['criteria']['confirmation_s'] >= condition['dwell_s']:
            raise ValueError('Measured convergence confirmation must fit inside every dwell')
        integer(condition['repetitions'], 'repetitions', 1, 1000)
        levels = condition['levels_hz']
        if not isinstance(levels, list) or len(levels) < 3:
            raise ValueError('Declare an explicit cyclic staircase with both travel directions')
        for value in levels:number(value, 'level')
        if len(levels)*condition['repetitions'] > 10000:
            raise ValueError('Record exceeds 10000 levels')
        # Cyclic levels exclude a duplicated endpoint: the wrap is the last step.
        expanded = levels*condition['repetitions']
        differences = [b-a for a,b in zip(expanded, expanded[1:])]
        if any(delta == 0 for delta in differences):raise ValueError('Adjacent repeated levels have no observable edge')
        if not min(levels) < 0 < max(levels):raise ValueError('Qualification must cover both sides of the starting frequency')
        if not any(d>0 for d in differences) or not any(d<0 for d in differences):
            raise ValueError('Qualification requires both travel directions')
    if not isinstance(p['sweep_parameters'], dict) or set(p['sweep_parameters']) != SWEEP_KEYS:
        raise ValueError('Sweep execution parameters must be explicit')
    if p['sweep_parameters']['full_data_log_ms'] != 0:
        raise ValueError('Full-rate source evidence requires full_data_log_ms=0')
    number(p['sweep_parameters']['sample_rate_hz'], 'sample_rate_hz', positive=True)
    for condition in conditions:
        for candidate in candidates:
            child_parameters(p, condition, candidate)  # Existing sweep schema before any command.
    return p


def condition_metadata(condition):
    out = copy.deepcopy(condition)
    levels = condition['levels_hz']
    expanded = levels*condition['repetitions']
    period = len(levels)*condition['dwell_s']
    out.update(levels_hz=expanded, cycle_levels_hz=levels,
               cycle_period_s=period, cycle_frequency_hz=1/period,
               step_rate_hz=1/condition['dwell_s'], direction='bidirectional',
               span_hz=max(levels)-min(levels),
               record_s=condition['lead_s']+period*condition['repetitions']+condition['tail_s'],
               step_sizes_hz=sorted({abs(b-a) for a,b in zip(expanded,expanded[1:])}))
    return out


def child_parameters(p, condition, candidate):
    from ldcon.jobs.modulation_sweep import parameters as validate
    values = dict(p['sweep_parameters'], shape='staircase', trigger='auto',
                  shot_sweep=None, calibrate_only=False,
                  levels_hz=condition_metadata(condition)['levels_hz'],
                  dwell_s=condition['dwell_s'], edge_s=condition['edge_s'],
                  lead_s=condition['lead_s'], tail_s=condition['tail_s'],
                  ldc_ff_v_per_hz=0., pzt_ff_gain_scale=1., ff_gain_scale=1.)
    values.update({k:v for k,v in candidate.items() if k!='id'})
    # ECDL hardware staircases have no separately programmable edge. Native
    # ramps require a nonzero edge; a structural zero is passed only to ECDL.
    if condition['edge_s'] == 0:
        if 'ldc_ff_v_per_hz' not in candidate:raise ValueError('Native FF requires a positive edge duration')
        values['edge_s'] = min(condition['dwell_s'], 1/values['sample_rate_hz'])
    result = validate({'parameters': values}, {})
    if result['duration_s'] <= condition_metadata(condition)['record_s']:
        raise ValueError('Child lifetime must include preparation and restoration, beyond record duration')
    return result


def validate_laser(laser, deck, p):
    from ldcon.jobs.ecdl_locked_sweep_proxy import rig_locked_route, locked_settings, RigRoute
    from ldcon.jobs.native_ff_proxy import feedforward_route, NativeFFRoute
    child = next((c for c in deck['cards'] if c['id']==p['sweep_card_id']), None)
    if child is None or child['operation'] not in ('sweep.servo_modulation', 'sweep.current_modulation'):
        raise ValueError('sweep_card_id must name a registered modulation sweep card')
    if not laser.get('allow_output'):raise ValueError('Laser output permission is required')
    role = child['uses'][0]
    if rig_locked_route(laser, role):
        route = RigRoute(laser)
        maximum = locked_settings(laser)['ldc_ff']['max_abs_v_per_hz']
        for candidate in p['candidates']:
            if 'ldc_ff_v_per_hz' not in candidate:raise ValueError('ECDL requires joint LDC and PZT candidates')
            if abs(candidate['ldc_ff_v_per_hz']) > maximum:raise ValueError('LDC FF exceeds the existing owner envelope')
        if any(c['edge_s'] != 0 for c in p['conditions']):
            raise ValueError('ECDL buffered steps have no programmable edge; specify edge_s=0')
    elif feedforward_route(laser, role):
        route = NativeFFRoute(laser)
        if any('ff_gain_scale' not in c for c in p['candidates']):raise ValueError('Native-current route has no PZT FF axis')
    else:
        raise ValueError('Requires an existing locked ECDL or native-current buffered sweep route; DLC native scan is not a staircase trial route')
    for condition in p['conditions']:
        for candidate in p['candidates']:
            route.make_plan(child_parameters(p, condition, candidate), None)
    return child


def readiness(laser, deck, card, overrides):
    p = parameters(card, overrides or {})
    reasons = []
    try:validate_laser(laser, deck, p)
    except (ValueError, KeyError, TypeError) as error:reasons.append(str(error))
    return dict(runnable=not reasons, blocked_reasons=reasons, parameters=p)
