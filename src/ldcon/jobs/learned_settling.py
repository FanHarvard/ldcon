"""Empirical, target-centred completion profiles with independent validation.

Only the pre-request records from the first outward/return pair calibrate the
noise bands. The next pair is held out, including while choosing the window.
No frequency tolerance is an input. A fitted late asymptote is never subtracted
from a target error. This describes measured settling, not metrological accuracy
or a guarantee about unobserved future behaviour. No control I/O occurs here.
"""
import copy
import hashlib
import json
import math
import random
import statistics

from ldcon.jobs.target_response_analysis import C_NM

VERSION = 1
_REPLICATES = 4096
_QUANTILE = .99


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    allow_nan=False).encode()).hexdigest()


def matches_context(profile, context):
    """Caller supplies the same current owner/gains/source/operating scope."""
    try:
        return (profile.get('status') == 'validated' and profile.get('version') == VERSION
                and isinstance(context, dict) and _digest(context) == profile['context_revision'])
    except (ValueError, TypeError, KeyError):
        return False


def _rows(value, quality=None):
    """Finite, monotonic samples of an intact history. Quality labels (``quality_valid``/``input_valid``/``valid``
    False, raw ``quality`` bits) are counted into ``quality`` and reported (rule K, 2026-10-27); integrity errors and
    invalid or non-monotonic samples remain rejections (data validity)."""
    quality = {} if quality is None else quality
    quality.setdefault('flagged_samples', 0)
    if isinstance(value, dict):
        if value.get('integrity_errors'):
            raise ValueError('history_integrity_error')
        if value.get('quality_valid') is False:
            quality['history_quality_valid'] = False
        value = value.get('samples')
    if not isinstance(value, list) or len(value) < 2:
        raise ValueError('insufficient_samples')
    result = []
    previous = None
    sequences = set()
    for row in value:
        if not isinstance(row, dict):
            raise ValueError('invalid_sample')
        stamp, wavelength = row.get('time_unix_s'), row.get('wavelength_nm')
        if (not _finite(stamp) or not _finite(wavelength) or wavelength <= 0
                or previous is not None and stamp <= previous):
            raise ValueError('invalid_or_nonmonotonic_sample')
        # Histories with quality labels or raw quality bits keep their meaning as a reported count.
        if (any(row.get(key) is False for key in ('quality_valid', 'input_valid', 'valid'))
                or 'quality' in row and row['quality'] != 0):
            quality['flagged_samples'] += 1
        if 'system_seq' in row:
            seq = row['system_seq']
            if not _finite(seq) or seq < 0 or int(seq) != seq or seq in sequences:
                raise ValueError('invalid_or_repeated_sequence')
            sequences.add(seq)
        result.append((float(stamp), C_NM / wavelength))
        previous = stamp
    return result


def _covered(rows, start, end, gap, minimum=2):
    return (len(rows) >= minimum and rows[0][0]-start <= gap+1e-7
            and end-rows[-1][0] <= gap+1e-7
            and all(b[0]-a[0] <= gap+1e-7 for a, b in zip(rows, rows[1:])))


def _metrics(rows, target):
    errors = [value-target for _, value in rows]
    times = [stamp-rows[0][0] for stamp, _ in rows]
    mean = statistics.mean(errors)
    centre = statistics.mean(times)
    denominator = sum((t-centre)**2 for t in times)
    slope = sum((t-centre)*(e-mean) for t, e in zip(times, errors))/denominator if denominator else 0.
    return dict(mean_error_hz=mean, rms_error_hz=math.sqrt(statistics.mean(e*e for e in errors)),
                peak_error_hz=max(map(abs, errors)), slope_hz_per_s=slope,
                samples=len(rows), start_unix_s=rows[0][0], end_unix_s=rows[-1][0])


def _correlation_length(values):
    """Initial-positive autocorrelation sum, rounded up to a sample block."""
    centre = statistics.mean(values)
    residual = [x-centre for x in values]
    denominator = sum(x*x for x in residual)
    if not denominator:
        return 1
    positive = 0.
    for lag in range(1, max(2, len(values)//4)):
        rho = sum(a*b for a, b in zip(residual, residual[lag:]))/denominator
        if rho <= 0:
            break
        positive += rho
    return max(1, math.ceil(1+2*positive))


def _quantile(values, fraction=_QUANTILE):
    ordered = sorted(values)
    return ordered[min(len(ordered)-1, math.ceil(fraction*len(ordered))-1)]


def _blocks(values, count):
    return [values[i:i+count] for i in range(0, len(values)-count+1, count)]


def _mean_bound(values, block, floor):
    means = [statistics.mean(part) for part in _blocks(values, block)]
    if len(means) < 4:
        raise ValueError('insufficient_independent_blocks')
    # A fixed, dimensionless three-standard-error criterion. Uncertainty is
    # estimated from correlated blocks, not the much larger raw sample count.
    return max(floor, 3*statistics.stdev(means)/math.sqrt(len(means)))


def _mean_diagnostic(values, block, floor):
    """Read-only description of the existing block-mean test, not a new gate."""
    means = [statistics.mean(part) for part in _blocks(values, block)]
    count = len(means)
    return dict(samples=len(values), block_samples=block, complete_blocks=count,
                unused_partial_block_samples=len(values) % block,
                mean_error_hz=statistics.mean(values) if values else None,
                stddev_hz=statistics.stdev(values) if len(values) > 1 else None,
                block_mean_standard_error_hz=statistics.stdev(means)/math.sqrt(count) if count > 1 else None,
                statistical_mean_bound_hz=_mean_bound(values, block, floor) if count >= 4 else None,
                numerical_floor_hz=floor)


def _limits(residuals, block, count, cadence, floor, comparison_windows):
    blocks = [part for values in residuals for part in _blocks(values, block)]
    if len(blocks) < 8:
        raise ValueError('insufficient_reference_blocks')
    rng = random.Random(49217)
    draws = []
    for _ in range(_REPLICATES):
        draw = []
        while len(draw) < count:
            draw.extend(rng.choice(blocks))
        draws.append(_metrics([(i*cadence, x) for i, x in enumerate(draw[:count])], 0.))
    # Correct for repeated mean/RMS/slope looks across the calibration horizon.
    # With a small empirical reference the requested extreme quantile reaches
    # the bootstrap maximum; report that finite resolution rather than claiming
    # a calibrated family-wise probability from a few hundred replicates.
    quantile = 1-(1-_QUANTILE)/max(1, 3*comparison_windows)
    # The 20% finite-reference margin is fixed before seeing validation data.
    # This is empirical calibration, not an asserted 99% frequentist guarantee.
    return dict(mean_error_max_hz=max(floor, 1.2*_quantile([abs(x['mean_error_hz']) for x in draws], quantile)),
                rms_error_max_hz=max(floor, 1.2*_quantile([x['rms_error_hz'] for x in draws], quantile)),
                peak_error_max_hz=max(floor, 1.2*max(abs(x) for values in residuals for x in values)),
                slope_max_hz_per_s=max(floor/(count*cadence),
                                      1.2*_quantile([abs(x['slope_hz_per_s']) for x in draws], quantile)))


def _passes(metrics, profile):
    return (abs(metrics['mean_error_hz']) <= profile['mean_error_max_hz']
            and metrics['rms_error_hz'] <= profile['rms_error_max_hz']
            and metrics['peak_error_hz'] <= profile['peak_error_max_hz']
            and abs(metrics['slope_hz_per_s']) <= profile['slope_max_hz_per_s'])


def _scan(profile, rows, target, applied, now):
    """Run exactly the causal detector, and retain later relapse evidence."""
    window = profile['evaluation_window_s']
    step = profile['check_interval_s']
    confirmation = profile['confirmation_s']
    gap = profile['max_gap_s']
    if now-applied < window+confirmation-1e-7:
        return dict(status='not_settled', reason='awaiting_complete_post_apply_windows',
                    first_completion_unix_s=None, relapse_after_completion=False)
    selected = [r for r in rows if applied <= r[0] <= now]
    if not _covered(selected, applied, now, gap, profile['minimum_samples']):
        return dict(status='inconclusive', reason='missing_coverage_or_sample_gap',
                    first_completion_unix_s=None, relapse_after_completion=False)
    first = None
    good_since = None
    relapse = False
    latest = None
    count = int(math.floor((now-applied-window+1e-7)/step))+1
    left = right = 0
    for index in range(count):
        end = applied+window+index*step
        start = end-window
        while left < len(selected) and selected[left][0] < start-1e-7:
            left += 1
        right = max(right, left)
        while right < len(selected) and selected[right][0] <= end+1e-7:
            right += 1
        part = selected[left:right]
        covered = _covered(part, start, end, gap, profile['minimum_samples'])
        metrics = _metrics(part, target) if covered else None
        passes = covered and _passes(metrics, profile)
        latest = dict(end_unix_s=end, passes=bool(passes), metrics=metrics)
        if passes:
            if good_since is None:
                good_since = end
            if first is None and end-good_since >= confirmation-1e-7:
                first = end
        else:
            good_since = None
            if first is not None:
                relapse = True
    current = (good_since is not None and latest['end_unix_s']-good_since >= confirmation-1e-7)
    return dict(status='settled' if current else 'not_settled',
                reason='causal_windows_sustained' if current else 'criteria_not_sustained',
                first_completion_unix_s=first, relapse_after_completion=relapse,
                last_window=latest, observed_until_unix_s=now)


def evaluate(profile, samples, target_hz, applied_unix_s, now):
    """Evaluate only fresh, post-apply data; missing evidence never means done.

    ``applied_unix_s`` must be the current request's confirmed end-of-ramp time.
    The caller must first check context, current target, request identity, native
    ramp completion and foreground supersession. This pure function cannot read
    those facts. Supplying a history dict additionally checks integrity errors.
    """
    try:
        if (profile.get('status') != 'validated' or profile.get('version') != VERSION
                or not all(_finite(x) for x in (target_hz, applied_unix_s, now))
                or target_hz <= 0 or now < applied_unix_s):
            raise ValueError('invalid_profile_or_request')
        interval = profile['validity']['target_range_hz']
        if not interval[0] <= target_hz <= interval[1]:
            raise ValueError('target_outside_measured_scope')
        if isinstance(samples, dict) and samples.get('provenance', {}) != profile.get('provenance', {}):
            raise ValueError('history_source_changed')
        quality = {}
        rows = _rows(samples, quality)
        # A transient gap clears accumulated completion evidence; it need not
        # poison a later complete fresh window for the rest of the operation.
        # Calibration still rejects gaps in its entire validation records.
        post = [r for r in rows if applied_unix_s <= r[0] <= now]
        restart = applied_unix_s
        if post and post[0][0]-restart > profile['max_gap_s']:
            restart = post[0][0]
        for left, right in zip(post, post[1:]):
            if right[0]-left[0] > profile['max_gap_s']+1e-7:
                restart = right[0]
        result = _scan(profile, rows, target_hz, restart, now)
        if restart != applied_unix_s:
            result.update(evidence_reset_due_to_gap=True, coverage_restart_unix_s=restart)
        result['quality_flags'] = quality
        return result
    except (ValueError, TypeError, KeyError, ZeroDivisionError) as error:
        return dict(status='inconclusive', reason=str(error), first_completion_unix_s=None,
                    relapse_after_completion=False)


def fit(trials, context):
    """Calibrate on two legs; validate unchanged criteria on later round trips.

    Returns a self-contained ``status=validated`` profile or an inconclusive
    result. The context is an exact caller-defined owner/gain/source/scope
    fingerprint, deliberately separate from the changing requested target.
    """
    diagnostics = dict(stage='input_validation', scope='rejection evidence only; not an accepted profile',
                       frozen_candidate_limits=None, baselines=[], tails=[])
    try:
        return _fit(trials, context, diagnostics)
    except (ValueError, TypeError, KeyError, ZeroDivisionError) as error:
        return dict(status='inconclusive', reason=str(error), version=VERSION,
                    diagnostics=diagnostics)


def _fit(trials, context, diagnostics):
    if not isinstance(context, dict) or not context:
        raise ValueError('explicit_context_required')
    if not isinstance(trials, list) or len(trials) < 4 or len(trials) % 2:
        raise ValueError('independent_calibration_and_validation_roundtrips_required')
    prepared = []
    provenance = None
    quality_flags = []
    for index, leg in enumerate(trials):
        if leg.get('status') != 'completed':
            raise ValueError('incomplete_trial')
        quality_flags.append(dict(trial_index=index))
        rows = _rows(leg['history'], quality_flags[-1])
        source = leg['history'].get('provenance', {})
        if provenance is None:
            provenance = copy.deepcopy(source)
        elif source != provenance:
            raise ValueError('history_source_changed')
        request, applied, end = (leg[k] for k in ('requested_unix_s', 'confirmed_unix_s', 'record_end_unix_s'))
        target, previous = leg['target_frequency_hz'], leg['previous_target_frequency_hz']
        if (not all(_finite(x) for x in (request, applied, end, target, previous))
                or not request <= applied < end or min(target, previous) <= 0 or target == previous):
            raise ValueError('invalid_trial_times_or_targets')
        if index and request <= prepared[-1]['end']:
            raise ValueError('training_and_validation_records_overlap')
        prepared.append(dict(rows=rows, request=request, applied=applied, end=end,
                             target=target, previous=previous, index=index))
    outward, backward = prepared[:2]
    if outward['target'] != backward['previous'] or outward['previous'] != backward['target']:
        raise ValueError('calibration_pair_is_not_a_roundtrip')
    for index, leg in enumerate(prepared[2:]):
        expected = prepared[index % 2]
        if any(leg[k] != expected[k] for k in ('target', 'previous')):
            raise ValueError('heldout_pair_does_not_repeat_calibrated_scope')
    baselines = [[r for r in leg['rows'] if r[0] < leg['request']] for leg in prepared[:2]]
    if any(len(rows) < 16 for rows in baselines):
        raise ValueError('insufficient_pre_request_reference')
    intervals = [b[0]-a[0] for rows in baselines for a, b in zip(rows, rows[1:])]
    cadence = statistics.median(intervals)
    # Three source intervals remain the coverage rule. Calibrate their duration
    # from the observed single-cycle timing envelope, rather than treating the
    # median as an exact clock period. Long/missing-cycle intervals cannot grow
    # that envelope, and neither transient nor held-out intervals participate.
    single_intervals = [value for value in intervals if value <= 1.5*cadence]
    upper_interval = max(single_intervals)
    learned_gap = 3*upper_interval
    max_gap = learned_gap
    configured_gap = context.get('max_gap_s')
    if configured_gap is not None:
        if not _finite(configured_gap) or configured_gap <= 0:
            raise ValueError('invalid_context_gap_limit')
        max_gap = min(max_gap, configured_gap)
    timing = dict(basis='first two pre-request baselines only',
        median_interval_s=cadence, single_cycle_ceiling_factor=1.5,
        single_cycle_interval_count=len(single_intervals),
        excluded_long_interval_count=len(intervals)-len(single_intervals),
        single_cycle_upper_interval_s=upper_interval,
        allowed_source_intervals=3, learned_gap_s=learned_gap,
        configured_gap_cap_s=configured_gap, effective_gap_s=max_gap)
    diagnostics.update(stage='history_coverage', timing=copy.deepcopy(timing))
    if any(not _covered(leg['rows'], leg['rows'][0][0], leg['end'], max_gap)
           for leg in prepared):
        raise ValueError('history_coverage_or_gap_invalid')
    baseline_errors = [[value-leg['previous'] for _, value in rows]
                       for rows, leg in zip(baselines, prepared)]
    correlation_lengths = [_correlation_length(values) for values in baseline_errors]
    block = max(correlation_lengths)
    floor = 8*max(math.ulp(leg['target']) for leg in prepared[:2])
    diagnostics.update(stage='baseline_target_bias', baseline_correlation_block_samples=correlation_lengths,
                       selected_correlation_block_samples=block)
    # Retain all raw tail summaries even if a pre-request reference fails. A
    # frozen candidate bound is left unavailable until calibration succeeds.
    tails = []
    for leg in prepared:
        tail_start = max(leg['applied'], leg['end']-(leg['end']-leg['applied'])/3)
        tail = [r for r in leg['rows'] if tail_start <= r[0] <= leg['end']]
        errors = [value-leg['target'] for _, value in tail]
        tails.append((tail, errors))
        diagnostics['tails'].append(dict(trial_index=leg['index'], held_out=leg['index'] >= 2,
            start_unix_s=tail_start, end_unix_s=leg['end'], target_frequency_hz=leg['target'],
            frozen_mean_bound_hz=None, effective_mean_bound_hz=None, evaluated=False,
            **_mean_diagnostic(errors, block, floor)))
    baseline_failure = None
    for index, errors in enumerate(baseline_errors):
        row = dict(trial_index=index, correlation_block_samples=correlation_lengths[index],
                   **_mean_diagnostic(errors, block, floor))
        bound = row['statistical_mean_bound_hz']
        reason = ('insufficient_independent_blocks' if bound is None else
                  'pre_request_reference_has_target_bias' if abs(statistics.mean(errors)) > bound else None)
        row.update(passes=reason is None, rejection_reason=reason)
        diagnostics['baselines'].append(row)
        if reason and baseline_failure is None:
            baseline_failure = reason
    if baseline_failure:
        raise ValueError(baseline_failure)
    residuals = [[value-statistics.mean(errors) for value in errors] for errors in baseline_errors]
    count = max(8, 4*block)
    window = count*cadence
    step = max(cadence, window/4)
    looks = sum(max(1, math.ceil((leg['end']-leg['request'])/step)) for leg in prepared[:2])
    limits = _limits(residuals, block, count, cadence, floor, looks)
    profile = dict(status='calibrating', version=VERSION, method='frozen_reference_block_bootstrap',
        context=copy.deepcopy(context), context_revision=_digest(context), provenance=provenance,
        evaluation_window_s=window, confirmation_s=window, check_interval_s=step,
        minimum_samples=max(3, math.floor(.8*count)), max_gap_s=max_gap,
        reference_cadence_s=cadence, correlation_block_samples=block,
        calibration=dict(trial_indices=[0, 1], baseline_samples=[len(x) for x in baselines],
                         independent_reference_blocks=sum(len(x)//block for x in residuals),
                         bootstrap_replicates=_REPLICATES, empirical_quantile=_QUANTILE,
                         repeated_comparison_windows=looks,
                         quantile_resolution_limited=(1-_QUANTILE)/(3*looks) < 1/_REPLICATES,
                         timing=timing,
                         finite_reference_margin=1.2, numerical_floor_hz=floor),
        validity=dict(target_range_hz=sorted([outward['target'], outward['previous']]),
                      maximum_target_step_hz=abs(outward['target']-outward['previous'])),
        qualification='Empirical completion at measured precision and scope; no metrological accuracy or future-stability guarantee',
        quality_flags=dict(flagged_samples=sum(q['flagged_samples'] for q in quality_flags), trials=quality_flags),
        **limits)
    diagnostics.update(stage='late_target_bias_and_noise',
        frozen_candidate_limits={key:copy.deepcopy(profile[key]) for key in
            (*limits, 'evaluation_window_s', 'confirmation_s', 'check_interval_s', 'minimum_samples', 'max_gap_s')},
        candidate_calibration=copy.deepcopy(profile['calibration']))
    # A late plateau must independently be target-centred. Never shift the
    # comparator to that plateau, even when doing so makes every trace pass.
    tail_failure = None
    for leg, (tail, errors), diagnostic in zip(prepared, tails, diagnostics['tails']):
        mean_bound = (min(profile['mean_error_max_hz'], diagnostic['statistical_mean_bound_hz'])
                      if diagnostic['statistical_mean_bound_hz'] is not None else None)
        metrics = _metrics(tail, leg['target']) if tail else None
        reason = None
        if (len(errors) < max(4*block, count) or
                abs(statistics.mean(errors)) > min(profile['mean_error_max_hz'],
                                                   _mean_bound(errors, block, floor))):
            reason = ('heldout_' if leg['index'] >= 2 else '')+'late_record_has_unresolved_target_bias'
        elif not _passes(metrics, profile):
            reason = ('heldout_' if leg['index'] >= 2 else '')+'late_record_exceeds_frozen_reference_noise_or_drift'
        diagnostic.update(frozen_mean_bound_hz=profile['mean_error_max_hz'], effective_mean_bound_hz=mean_bound,
            minimum_samples=max(4*block, count), evaluated=True, passes=reason is None,
            rejection_reason=reason, metrics=metrics)
        if reason and tail_failure is None:
            tail_failure = reason
            diagnostics['first_rejected_tail_index'] = leg['index']
    if tail_failure:
        raise ValueError(tail_failure)
    evaluations = []
    diagnostics.update(stage='causal_completion_validation', causal_evaluations=evaluations)
    for leg in prepared:
        # The old target card's confirmation can include coarse completion.
        # Start validation at submission so it cannot conceal an early false
        # crossing. A live caller separately gates on confirmed ramp completion.
        result = _scan(profile, leg['rows'], leg['target'], leg['request'], leg['end'])
        result.update(trial_index=leg['index'], held_out=leg['index'] >= 2)
        evaluations.append(result)
        if result['first_completion_unix_s'] is None:
            raise ValueError(('heldout_' if leg['index'] >= 2 else 'calibration_')+'did_not_complete')
        if result['relapse_after_completion']:
            raise ValueError(('heldout_' if leg['index'] >= 2 else 'calibration_')+'false_early_completion')
        # Leave an independent look-ahead interval after a causal completion.
        if leg['end']-result['first_completion_unix_s'] < 2*window:
            raise ValueError('insufficient_post_completion_validation_horizon')
    profile['status'] = 'validated'
    profile['validation'] = dict(trial_indices=list(range(2, len(prepared))),
        causal_first_hit=True, no_observed_relapse=True, evaluations=evaluations,
        time_basis='request timestamp; live use additionally requires owner ramp completion',
        evidence_revision=_digest(trials),
        minimum_post_completion_horizon_s=min(leg['end']-row['first_completion_unix_s']
            for leg, row in zip(prepared, evaluations)))
    profile['expected_settle_s'] = max(row['first_completion_unix_s']-leg['request']
                                     for leg, row in zip(prepared, evaluations))
    profile['profile_revision'] = _digest(profile)
    return profile
