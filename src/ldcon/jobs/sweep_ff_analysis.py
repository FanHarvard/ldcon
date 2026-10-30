"""Pure staircase feedforward evaluation; no device access or parameter adoption.

``trial.condition.levels_hz`` is the complete sequence of visits, including
repetitions. Equal offsets in different visits are never pooled. Criteria are
frozen caller input, not actuator safety limits. Qualification describes only
the observed waveform at this cadence and operating point, never an untested
span or a guarantee of single-mode operation.
"""
import copy
import math
import statistics

_CRITERIA = {
    'error_tolerance_hz', 'confirmation_s', 'min_samples', 'max_sample_gap_s',
    'max_late_drift_hz_per_s', 'hop_jump_hz', 'hop_confirmation_s',
    'require_optical_quality', 'max_quality_age_s',
}
C_NM = 299792458e9
_COLUMNS = ('t_unix_s', 'wavelength_nm', 'level_index', 'in_transition',
            't_since_transition_s', 'programmed_offset_hz', 'error_hz')


def _finite(value):
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)


def _same(a, b):
    # Serialization/arithmetic agreement, not a physical acceptance tolerance.
    return abs(a-b) <= max(1e-6, 4*math.ulp(a), 4*math.ulp(b))


def validate_criteria(values):
    if not isinstance(values, dict) or set(values) != _CRITERIA:
        raise ValueError('Explicit sweep FF criteria required: ' + ', '.join(sorted(_CRITERIA)))
    if type(values['require_optical_quality']) is not bool:
        raise ValueError('require_optical_quality must be boolean')
    if type(values['min_samples']) is not int or values['min_samples'] < 2:
        raise ValueError('min_samples must be at least two')
    for key in _CRITERIA - {'require_optical_quality', 'min_samples'}:
        v = values[key]
        if not _finite(v) or v < 0 or (v == 0 and key != 'max_late_drift_hz_per_s'):
            raise ValueError(key + ' must be an explicit finite positive value')
    return copy.deepcopy(values)


def _slope(times, values):
    mt, mv = statistics.fmean(times), statistics.fmean(values)
    denom = sum((t-mt)**2 for t in times)
    return sum((t-mt)*(v-mv) for t, v in zip(times, values))/denom if denom else None


def _optical(points, indices, age):
    columns = [points.get(k) for k in ('quality_match_dt_s', 'quality_single_mode', 'quality_degraded')]
    if any(not isinstance(col, list) or len(col) != len(points['t_unix_s']) for col in columns):
        return dict(status='unknown', matched_points=0, total_points=len(indices), degraded_points=0)
    matched = [i for i in indices if _finite(columns[0][i]) and abs(columns[0][i]) <= age
               and type(columns[1][i]) is bool and type(columns[2][i]) is bool]
    bad = sum(not columns[1][i] or columns[2][i] for i in matched)
    return dict(status=('degraded' if bad else 'qualified' if len(matched) == len(indices) else 'unknown'),
                matched_points=len(matched), total_points=len(indices), degraded_points=bad)


def _continuity(points, indices, p):
    """Flag sustained, uncommanded discontinuities; never infer a mode number."""
    events, pending = [], []
    for left, right in zip(indices, indices[1:]):
        gap = points['t_unix_s'][right]-points['t_unix_s'][left]
        if gap > p['max_sample_gap_s']:
            continue
        # Target steps have their own settling test. Do not call the intended
        # frequency jump a hop or impose a tracking tolerance as a hop limit.
        if not _same(points['programmed_offset_hz'][left], points['programmed_offset_hz'][right]):
            continue
        delta = points['error_hz'][right]-points['error_hz'][left]
        if abs(delta) < p['hop_jump_hz'] or abs(points['error_hz'][right]) <= abs(points['error_hz'][left]):
            continue
        begin, base = points['t_unix_s'][right], points['error_hz'][left]
        samples = []
        for k in indices[indices.index(right):]:
            if not _same(points['programmed_offset_hz'][k], points['programmed_offset_hz'][right]):
                break
            if samples and points['t_unix_s'][k]-points['t_unix_s'][samples[-1]] > p['max_sample_gap_s']:
                break
            if (points['error_hz'][k]-base)*math.copysign(1, delta) < p['hop_jump_hz']:
                break
            samples.append(k)
            if points['t_unix_s'][k]-begin >= p['hop_confirmation_s']:
                break
        row = dict(index=right, jump_hz=delta, observed_s=(points['t_unix_s'][samples[-1]]-begin)
                   if samples else 0., sample_count=len(samples))
        (events if row['observed_s'] >= p['hop_confirmation_s'] and len(samples) >= p['min_samples']
         else pending).append(row)
    # A loss coincident with an intended step has no within-plateau jump.
    # Sustained large canonical residuals still make continuity unproven. The
    # same evidence can arise from bad FF or very slow tracking; do not label
    # it an owner-confirmed physical hop, and do not reuse tracking tolerance.
    runs, run = [], []
    for i in indices:
        if (abs(points['error_hz'][i]) < p['hop_jump_hz'] or (run and
                (points['t_unix_s'][i]-points['t_unix_s'][run[-1]] > p['max_sample_gap_s']
                 or points['error_hz'][i]*points['error_hz'][run[-1]] <= 0))):
            if run:
                runs.append(run)
            run = []
        if abs(points['error_hz'][i]) >= p['hop_jump_hz']:
            run.append(i)
    if run:
        runs.append(run)
    large = [dict(start_index=r[0], end_index=r[-1], sample_count=len(r),
                  observed_s=points['t_unix_s'][r[-1]]-points['t_unix_s'][r[0]],
                  max_abs_error_hz=max(abs(points['error_hz'][i]) for i in r)) for r in runs
             if len(r) >= p['min_samples'] and
             points['t_unix_s'][r[-1]]-points['t_unix_s'][r[0]] >= p['hop_confirmation_s']]
    return dict(status=('discontinuity' if events else 'unconfirmed_mode_loss' if large
                        else 'unconfirmed_jump' if pending else 'continuous'),
                events=events, unconfirmed_events=pending, persistent_large_residuals=large,
                qualification='Observed frequency continuity only; not an optical single-mode guarantee. '
                'A sustained residual above the independent hop criterion, including after an intended step, '
                'is unconfirmed mode loss: mode hop, bad FF and slow response are not distinguished.')


def _visit(points, indices, level, expected, previous, p):
    times = [points['t_since_transition_s'][i] for i in indices]
    errors = [points['error_hz'][i] for i in indices]
    waves = [points['wavelength_nm'][i] for i in indices]
    dwell = level['t_end_unix_s']-level['t_start_unix_s']
    gaps = [b-a for a, b in zip(times, times[1:])]
    reasons = []
    if len(indices) < p['min_samples']:
        reasons.append('insufficient_samples')
    if (not times or times[0] < -1e-6 or times[-1] >= dwell+1e-6
            or times[0] > p['max_sample_gap_s'] or dwell-times[-1] > p['max_sample_gap_s']
            or any(g <= 0 or g > p['max_sample_gap_s'] for g in gaps)):
        reasons.append('incomplete_temporal_coverage')
    last_bad = max((j for j, e in enumerate(errors) if abs(e) > p['error_tolerance_hz']), default=-1)
    suffix = list(range(last_bad+1, len(indices)))
    observed = (times[suffix[-1]]-times[suffix[0]]) if suffix else 0.
    settled = (len(suffix) >= p['min_samples'] and observed >= p['confirmation_s'])
    if not settled:
        reasons.append('convergence_not_sustained')
    late = [j for j, t in enumerate(times) if t >= dwell-p['confirmation_s']]
    drift = _slope([times[j] for j in late], [errors[j] for j in late]) if len(late) >= p['min_samples'] else None
    if drift is None:
        reasons.append('late_drift_unobserved')
    elif abs(drift) > p['max_late_drift_hz_per_s']:
        reasons.append('late_drift_exceeded')
    chosen = [indices[j] for j in suffix] if settled else []
    direction = 0 if previous is None else (1 if expected > previous else -1 if expected < previous else 0)
    quality = _optical(points, indices, p['max_quality_age_s'])
    if p['require_optical_quality'] and quality['status'] != 'qualified':
        reasons.append('optical_quality_' + quality['status'])
    return dict(level_index=level['level_index'], offset_hz=expected, input_indices=indices,
                status='qualified' if not reasons else 'not_qualified', reasons=reasons,
                n_points=len(indices), maximum_gap_s=max(gaps) if gaps else None,
                settled_indices=chosen, settling_s=times[suffix[0]] if settled else None,
                settled_start_unix_s=points['t_unix_s'][chosen[0]] if chosen else None,
                settled_end_unix_s=points['t_unix_s'][chosen[-1]] if chosen else None,
                settled_wavelength_mean_nm=statistics.fmean(points['wavelength_nm'][i] for i in chosen) if chosen else None,
                settled_wavelength_stddev_nm=statistics.pstdev(points['wavelength_nm'][i] for i in chosen) if chosen else None,
                wavelength_mean_nm=statistics.fmean(waves) if waves else None,
                wavelength_stddev_nm=statistics.pstdev(waves) if waves else None,
                mean_error_hz=statistics.fmean(errors) if errors else None,
                stddev_error_hz=statistics.pstdev(errors) if errors else None,
                peak_abs_error_hz=max(map(abs, errors)) if errors else None,
                overshoot_hz=max(0., max(direction*e for e in errors)) if errors and direction else None,
                late_drift_hz_per_s=drift, optical_quality=quality)


def evaluate_trial(analysis, criteria):
    """Evaluate one complete exact waveform; missing evidence never passes.

    ``analysis`` is a shot-analysis document plus ``trial`` (trial_id,
    candidate, condition) and ``owner_outcome``. Condition requires levels_hz
    (all visits), dwell_s, edge_s, cycle_period_s, repetitions, direction and
    precondition_s. Owner evidence requires playback_completed, mode_hop
    (bool or null), outputs_uncertain, cancelled and censored. Bad criteria
    raise ValueError before evaluation; malformed/missing evidence returns an
    inconclusive result. No input is mutated and no calibration is adopted.
    """
    p = validate_criteria(criteria)
    out = dict(schema='ldcon.sweep-ff-trial-evaluation/v1', qualified=False, status='inconclusive',
               data_valid=False, criteria=p, trial=copy.deepcopy(analysis.get('trial')), reasons=[], visits=[],
               qualification='Only the complete observed waveform and joint candidate at the recorded cadence; '
               'a tested span is a lower bound, not a measured mode-hop boundary.')
    reasons = out['reasons']
    trial, owner = analysis.get('trial'), analysis.get('owner_outcome')
    if not isinstance(trial, dict) or not trial.get('trial_id') or not isinstance(trial.get('candidate'), dict):
        reasons.append('trial_metadata_missing')
        return out
    condition = trial.get('condition')
    required = {'levels_hz', 'dwell_s', 'edge_s', 'cycle_period_s', 'repetitions', 'direction', 'precondition_s'}
    if not isinstance(condition, dict) or not required <= set(condition):
        reasons.append('waveform_condition_missing')
        return out
    expected = condition['levels_hz']
    if (not isinstance(expected, list) or not expected or not all(_finite(v) for v in expected)
            or not all(_finite(condition[k]) and condition[k] >= 0 for k in ('dwell_s', 'edge_s', 'cycle_period_s', 'precondition_s'))
            or condition['dwell_s'] <= 0 or condition['cycle_period_s'] <= 0
            or condition['edge_s'] >= condition['dwell_s']
            or type(condition['repetitions']) is not int or condition['repetitions'] < 1
            or not isinstance(condition['direction'], str) or not condition['direction']):
        reasons.append('waveform_condition_invalid')
        return out
    if not _same(len(expected)*condition['dwell_s'], condition['cycle_period_s']*condition['repetitions']):
        reasons.append('waveform_period_inconsistent')
        return out
    if not isinstance(owner, dict) or 'mode_hop' not in owner or any(type(owner.get(k)) is not bool for k in
            ('playback_completed', 'outputs_uncertain', 'cancelled', 'censored')) or (owner['mode_hop'] is not None and type(owner['mode_hop']) is not bool):
        reasons.append('owner_outcome_unknown')
        return out
    out['owner_outcome'] = copy.deepcopy(owner)
    for key in ('outputs_uncertain', 'cancelled', 'censored', 'mode_hop'):
        if owner.get(key) is True:
            reasons.append('owner_' + key)
    if owner['playback_completed'] is not True:
        reasons.append('playback_incomplete')
    if (analysis.get('completeness') or {}).get('complete') is not True:
        reasons.append('data_incomplete')
    if ((analysis.get('quality') or {}).get('staircase_detected') is not True
            or (analysis.get('quality') or {}).get('record_complete') is not True):
        reasons.append('waveform_not_completely_detected')
    levels, points = analysis.get('levels'), analysis.get('points')
    if not isinstance(levels, list) or len(levels) != len(expected) or not isinstance(points, dict):
        reasons.append('exact_levels_missing')
        return out
    arrays = [points.get(k) for k in _COLUMNS]
    if any(not isinstance(a, list) for a in arrays) or len({len(a) for a in arrays}) != 1:
        reasons.append('point_arrays_misaligned')
        return out
    if not all(_finite(t) for t in points['t_unix_s']) or any(b <= a for a, b in zip(points['t_unix_s'], points['t_unix_s'][1:])):
        reasons.append('sample_times_invalid')
        return out
    for i, (level, value) in enumerate(zip(levels, expected)):
        if (not isinstance(level, dict) or type(level.get('level_index')) is not int
                or not _finite(level.get('offset_hz')) or not _same(level['offset_hz'], value)
                or not all(_finite(level.get(k)) for k in ('t_start_unix_s', 't_end_unix_s'))
                or not _same(level['t_end_unix_s']-level['t_start_unix_s'], condition['dwell_s'])
                or (i and not _same(level['t_start_unix_s'], levels[i-1]['t_end_unix_s']))
                or level.get('played') is not True):
            reasons.append('exact_levels_invalid')
            return out
    # Fit-derived response delay, width and level_index must NOT remove the
    # delay we are optimizing. Partition raw acquisition times by ADC-fitted
    # command boundaries, including points still tagged as the previous level.
    groups = [(level['level_index'], [i for i, t in enumerate(points['t_unix_s'])
        if level['t_start_unix_s'] <= t < level['t_end_unix_s']]) for level in levels]
    if any(not indices for _, indices in groups):
        reasons.append('exact_visits_not_observed')
        return out
    all_indices = [i for _, group in groups for i in group]
    for i in all_indices:
        if any(not _finite(points[k][i]) for k in ('wavelength_nm', 'programmed_offset_hz', 'error_hz')) or points['wavelength_nm'][i] <= 0:
            reasons.append('sample_value_invalid')
            return out
    parameters = analysis.get('parameters') or {}
    reference = parameters.get('reference') or {}
    target, f_ref, shift = (reference.get(k) for k in ('lock_target_hz', 'f_ref_hz', 'f_ref_minus_target_hz'))
    if (not all(_finite(v) for v in (target, f_ref, shift)) or target <= 0 or f_ref <= 0
            or not _same(f_ref-target, shift)):
        reasons.append('canonical_reference_missing_or_inconsistent')
        return out
    points = copy.deepcopy(points)
    for level, (_, indices) in zip(levels, groups):
        for i in indices:
            frequency = C_NM/points['wavelength_nm'][i]
            arithmetic_error = 4*max(math.ulp(frequency), math.ulp(f_ref))
            relative = frequency-f_ref-points['programmed_offset_hz'][i]
            if abs(relative-points['error_hz'][i]) > arithmetic_error:
                reasons.append('raw_reference_residual_inconsistent')
                return out
            points['programmed_offset_hz'][i] = level['offset_hz']
            points['error_hz'][i] = frequency-target-level['offset_hz']
            points['t_since_transition_s'][i] = points['t_unix_s'][i]-level['t_start_unix_s']
    out['error_reference'] = dict(lock_target_hz=target, source_reference_hz=f_ref,
        source_reference_minus_target_hz=shift,
        definition='Raw wavelength frequency minus canonical lock target and hard commanded level offset '
                   'at raw acquisition time; no fitted response delay, width, gain or baseline offset removed.')
    out['target_error_indices'] = all_indices
    out['target_error_hz'] = [points['error_hz'][i] for i in all_indices]
    timing = analysis.get('quality') or {}
    delay = parameters.get('delay_s')
    time_base = analysis.get('time_base')
    clock_step = timing.get('clock_step_in_window_us')
    timing_known = (timing.get('ok') is True and isinstance(time_base, str) and bool(time_base)
        and _finite(analysis.get('t0_unix_s')) and _same(analysis['t0_unix_s'], levels[0]['t_start_unix_s'])
        and _finite(delay) and isinstance(analysis.get('clock'), dict)
        and (_finite(clock_step) or clock_step is None))
    if not timing_known:
        reasons.append('timing_provenance_incomplete')
    elif clock_step is not None and clock_step != 0:
        reasons.append('clock_step_in_record')
    out['timing'] = dict(time_base=time_base, source_fitted_delay_s=delay, scoring_delay_removed=False, clock_step_in_window_us=clock_step,
        qualification='Same-record level timing and complete sample coverage; absolute SNTP synchronization '
                      'is not required. Missing absolute synchronization is not replaced with a claim of it.')
    for j, (level, (_, indices)) in enumerate(zip(levels, groups)):
        visit = _visit(points, indices, level, expected[j], expected[j-1] if j else None, p)
        visit['visit_index'] = j
        out['visits'].append(visit)
    out['data_valid'] = (not reasons and all(not {'insufficient_samples', 'incomplete_temporal_coverage',
        'late_drift_unobserved'}.intersection(v['reasons']) for v in out['visits']))
    out['frequency_continuity'] = _continuity(points, all_indices, p)
    if owner['mode_hop'] is True:
        out['frequency_continuity']['status'] = 'owner_reported_hop'
    if out['frequency_continuity']['status'] != 'continuous':
        reasons.append(out['frequency_continuity']['status'])
    if any(v['status'] != 'qualified' for v in out['visits']):
        reasons.append('visit_evaluation_failed')
    out['optical_quality'] = _optical(points, all_indices, p['max_quality_age_s'])
    out['observed_span_hz'] = max(expected)-min(expected)
    settled = [v['settling_s'] for v in out['visits'] if v['settling_s'] is not None]
    out['worst_settling_s'] = max(settled) if settled else None
    raw_errors = [points['error_hz'][i] for i in all_indices]
    out['rms_error_hz'] = math.sqrt(statistics.fmean(e*e for e in raw_errors))
    out['mean_error_hz'] = statistics.fmean(raw_errors)
    drift = [abs(v['late_drift_hz_per_s']) for v in out['visits'] if v['late_drift_hz_per_s'] is not None]
    out['max_late_drift_hz_per_s'] = max(drift) if drift else None
    out['qualified'] = not reasons
    out['status'] = 'qualified' if not reasons else 'not_qualified'
    out['qualified_span_hz'] = out['observed_span_hz'] if not reasons else None
    return out
