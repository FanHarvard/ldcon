"""Offline learned completion: independent noise calibration and held-out use."""
import copy
import json
import math
import random

import pytest

from ldcon.learned_settling import evaluate, fit, matches_context
from ldcon.target_response_analysis import C_NM


CONTEXT = dict(laser_id='test', owner_revision='owner-a', gains=[1., 2., 100.], max_gap_s=.2)


def trials(noise=100000., bias=0., validation_noise=1., relapse=False):
    rng = random.Random(122)
    base = 300e12
    result = []
    for leg in range(4):
        request = 100.+leg*30
        previous = base+(10e6 if leg % 2 else 0)
        target = base+(0 if leg % 2 else 10e6)
        rows = []
        for n in range(481):
            t = -4+n*.05
            if t < 0:
                error = rng.uniform(-noise, noise)
                frequency = previous+error
            else:
                scale = validation_noise if leg >= 2 else 1.
                error = (previous-target)*math.exp(-t/.2)+rng.uniform(-noise, noise)*scale+bias
                if relapse and leg == 2 and 5. < t < 7.:
                    error += 2e6
                frequency = target+error
            rows.append(dict(time_unix_s=request+t, wavelength_nm=C_NM/frequency, system_seq=n))
        result.append(dict(status='completed', previous_target_frequency_hz=previous,
            target_frequency_hz=target, requested_unix_s=request, confirmed_unix_s=request+.1,
            record_end_unix_s=request+20., history=dict(samples=rows, integrity_errors=[],
                provenance=dict(host='native-test', aggregation='none'))))
    return result


def test_profile_learns_frequency_scale_and_independent_causal_validation():
    evidence = trials()
    p = fit(evidence, CONTEXT)
    assert p['status'] == 'validated', p
    assert 0 < p['mean_error_max_hz'] < 100000
    assert p['validation']['trial_indices'] == [2, 3]
    assert p['validation']['causal_first_hit']
    assert matches_context(p, CONTEXT)
    assert not matches_context(p, {**CONTEXT, 'gains': [1., 3., 100.]})
    leg = evidence[3]
    now = leg['record_end_unix_s']
    r = evaluate(p, leg['history'], leg['target_frequency_hz'], leg['confirmed_unix_s'], now)
    assert r['status'] == 'settled'
    assert r['first_completion_unix_s'] < now


def test_stable_shifted_asymptote_cannot_define_its_own_convergence():
    r = fit(trials(bias=500000.), CONTEXT)
    assert r['status'] == 'inconclusive'
    assert r['reason'] == 'late_record_has_unresolved_target_bias'


def test_noisy_validation_cannot_loosen_calibrated_bands():
    r = fit(trials(validation_noise=10.), CONTEXT)
    assert r['status'] == 'inconclusive'
    assert r['reason'].startswith('heldout_')


def test_heldout_lucky_crossing_then_relapse_is_rejected():
    r = fit(trials(relapse=True), CONTEXT)
    assert r['status'] == 'inconclusive'
    assert r['reason'] == 'heldout_false_early_completion'


@pytest.mark.parametrize('damage', ['gap', 'duplicate', 'integrity', 'source', 'overlap'])
def test_invalid_evidence_never_publishes_profile(damage):
    records = trials()
    history = records[3]['history']
    if damage == 'gap':
        history['samples'] = [r for i, r in enumerate(history['samples']) if not 200 < i < 220]
    elif damage == 'duplicate':
        history['samples'][201]['time_unix_s'] = history['samples'][200]['time_unix_s']
    elif damage == 'integrity':
        history['integrity_errors'] = ['conflicting_sequence']
    elif damage == 'source':
        history['provenance']['host'] = 'different-host'
    else:
        records[3] = copy.deepcopy(records[2])
    assert fit(records, CONTEXT)['status'] == 'inconclusive'


def test_quality_labels_are_reported_not_a_veto():
    # Rule K (2026-10-27): a quality label on a sample is counted into the profile; it does not reject the evidence.
    records = trials()
    records[3]['history']['samples'][200]['quality_valid'] = False
    profile = fit(records, CONTEXT)
    assert profile['status'] == 'validated' and profile['quality_flags']['flagged_samples'] == 1


def test_live_evaluator_requires_fresh_post_apply_full_windows():
    records = trials()
    p = fit(records, CONTEXT)
    leg = records[3]
    applied = leg['confirmed_unix_s']
    target = leg['target_frequency_hz']
    assert evaluate(p, leg['history'], target, applied, applied+.1)['status'] == 'not_settled'
    stale = [r for r in leg['history']['samples'] if r['time_unix_s'] < applied]
    assert evaluate(p, stale, target, applied, applied+3)['status'] == 'inconclusive'
    assert evaluate(p, leg['history'], target+1e9, applied, leg['record_end_unix_s'])['status'] == 'inconclusive'


def test_bias_in_pre_request_reference_is_not_accepted_as_noise():
    records = trials()
    for leg in records[:2]:
        for row in leg['history']['samples']:
            if row['time_unix_s'] < leg['requested_unix_s']:
                row['wavelength_nm'] = C_NM/(C_NM/row['wavelength_nm']+1e6)
    r = fit(records, CONTEXT)
    assert r['status'] == 'inconclusive'
    assert r['reason'] == 'pre_request_reference_has_target_bias'


def test_completion_scale_comes_from_evidence_not_a_fixed_mhz_band():
    low = fit(trials(noise=10000.), CONTEXT)
    high = fit(trials(noise=100000.), CONTEXT)
    assert low['status'] == high['status'] == 'validated'
    assert high['rms_error_max_hz']/low['rms_error_max_hz'] == pytest.approx(10., rel=.01)


def test_no_future_rows_can_create_an_earlier_completion():
    records = trials()
    p = fit(records, CONTEXT)
    leg = records[2]
    now = leg['confirmed_unix_s']+3
    prefix = [r for r in leg['history']['samples'] if r['time_unix_s'] <= now]
    full = evaluate(p, leg['history'], leg['target_frequency_hz'], leg['confirmed_unix_s'], now)
    partial = evaluate(p, prefix, leg['target_frequency_hz'], leg['confirmed_unix_s'], now)
    assert full == partial


def test_long_correlated_reference_needs_more_independent_evidence():
    records = trials()
    for leg in records[:2]:
        before = [r for r in leg['history']['samples'] if r['time_unix_s'] < leg['requested_unix_s']]
        for i, row in enumerate(before):
            error = 100000.*math.sin(2*math.pi*i/len(before))
            row['wavelength_nm'] = C_NM/(leg['previous_target_frequency_hz']+error)
    r = fit(records, CONTEXT)
    assert r['status'] == 'inconclusive'
    assert r['reason'] == 'insufficient_independent_blocks'


def test_delayed_old_confirmation_cannot_hide_a_heldout_early_relapse():
    records = trials(relapse=True)
    for leg in records:
        leg['confirmed_unix_s'] = leg['requested_unix_s']+10
    r = fit(records, CONTEXT)
    assert r['status'] == 'inconclusive'
    assert r['reason'] == 'heldout_false_early_completion'


def test_heldout_bias_below_single_window_band_still_fails_long_record_test():
    records = trials()
    for leg in records[2:]:
        for row in leg['history']['samples']:
            if row['time_unix_s'] >= leg['requested_unix_s']:
                row['wavelength_nm'] = C_NM/(C_NM/row['wavelength_nm']+40000.)
    r = fit(records, CONTEXT)
    assert r['status'] == 'inconclusive'
    assert r['reason'] == 'heldout_late_record_has_unresolved_target_bias'


def test_live_gap_resets_evidence_and_a_full_fresh_window_can_recover():
    records = trials()
    p = fit(records, CONTEXT)
    leg = records[3]
    applied, target = leg['confirmed_unix_s'], leg['target_frequency_hz']
    data = copy.deepcopy(leg['history'])
    data['samples'] = [r for r in data['samples'] if not applied+5 < r['time_unix_s'] < applied+6]
    first = evaluate(p, data, target, applied, applied+6.1)
    assert first['status'] == 'not_settled'
    assert first['evidence_reset_due_to_gap']
    recovered = evaluate(p, data, target, applied, applied+9)
    assert recovered['status'] == 'settled'
    assert recovered['first_completion_unix_s'] > applied+6


def test_live_history_source_change_cannot_use_old_profile():
    records = trials()
    p = fit(records, CONTEXT)
    leg = records[3]
    data = copy.deepcopy(leg['history'])
    data['provenance']['host'] = 'another-native-controller'
    r = evaluate(p, data, leg['target_frequency_hz'], leg['confirmed_unix_s'], leg['record_end_unix_s'])
    assert r['status'] == 'inconclusive'
    assert r['reason'] == 'history_source_changed'


def timing_jitter_records():
    records = trials()
    # One calibrated single-cycle interval is 51 ms rather than the median
    # 50 ms. Its adjacent 49 ms interval preserves elapsed baseline time.
    records[0]['history']['samples'][20]['time_unix_s'] += .001
    # A three-cycle interval with ordinary timing jitter is 151 ms. The old
    # exact 3 * median rule would incorrectly call this missing coverage.
    rows = records[0]['history']['samples']
    rows[143]['time_unix_s'] += .001
    records[0]['history']['samples'] = [r for i, r in enumerate(rows) if i not in (141, 142)]
    return records


def test_coverage_uses_calibrated_single_cycle_jitter_with_same_three_cycle_rule():
    p = fit(timing_jitter_records(), CONTEXT)
    assert p['status'] == 'validated', p
    timing = p['calibration']['timing']
    assert timing['median_interval_s'] == pytest.approx(.05)
    assert timing['single_cycle_upper_interval_s'] == pytest.approx(.051)
    assert timing['allowed_source_intervals'] == 3
    assert p['max_gap_s'] == pytest.approx(.153)


def test_heldout_timing_cannot_enlarge_calibrated_coverage_bound():
    records = timing_jitter_records()
    records[2]['history']['samples'][200]['time_unix_s'] += .024
    p = fit(records, CONTEXT)
    assert p['status'] == 'validated', p
    assert p['calibration']['timing']['single_cycle_upper_interval_s'] == pytest.approx(.051)
    assert p['max_gap_s'] == pytest.approx(.153)


def test_missing_baseline_cycle_does_not_redefine_single_cycle_jitter():
    records = timing_jitter_records()
    del records[1]['history']['samples'][30]
    p = fit(records, CONTEXT)
    assert p['status'] == 'validated', p
    assert p['calibration']['timing']['excluded_long_interval_count'] == 1
    assert p['max_gap_s'] == pytest.approx(.153)


def test_calibrated_jitter_does_not_allow_four_source_intervals():
    records = timing_jitter_records()
    rows = records[2]['history']['samples']
    records[2]['history']['samples'] = [r for i, r in enumerate(rows) if i not in (141, 142, 143)]
    p = fit(records, CONTEXT)
    assert p['status'] == 'inconclusive'
    assert p['reason'] == 'history_coverage_or_gap_invalid'


def test_explicit_gap_cap_still_limits_calibrated_jitter():
    p = fit(timing_jitter_records(), {**CONTEXT, 'max_gap_s': .1505})
    assert p['status'] == 'inconclusive'
    assert p['reason'] == 'history_coverage_or_gap_invalid'


def test_tail_rejection_reports_frozen_limits_and_every_tail_without_accepting_profile():
    records = trials()
    for leg in records[2:]:
        for row in leg['history']['samples']:
            if row['time_unix_s'] >= leg['requested_unix_s']:
                row['wavelength_nm'] = C_NM/(C_NM/row['wavelength_nm']+40000.)
    r = fit(records, CONTEXT)
    assert r['status'] == 'inconclusive'
    assert r['reason'] == 'heldout_late_record_has_unresolved_target_bias'
    assert 'profile_revision' not in r
    assert not matches_context(r, CONTEXT)
    diagnostics = r['diagnostics']
    assert diagnostics['stage'] == 'late_target_bias_and_noise'
    assert len(diagnostics['baseline_correlation_block_samples']) == 2
    assert len(diagnostics['tails']) == 4
    assert diagnostics['first_rejected_tail_index'] == 2
    limits = diagnostics['frozen_candidate_limits']
    for leg, tail in zip(records, diagnostics['tails']):
        samples = [s for s in leg['history']['samples'] if tail['start_unix_s'] <= s['time_unix_s'] <= tail['end_unix_s']]
        errors = [C_NM/s['wavelength_nm']-leg['target_frequency_hz'] for s in samples]
        assert tail['evaluated']
        assert tail['samples'] == len(samples)
        assert tail['mean_error_hz'] == pytest.approx(sum(errors)/len(errors))
        assert tail['complete_blocks'] == len(samples)//tail['block_samples']
        assert tail['effective_mean_bound_hz'] == min(limits['mean_error_max_hz'], tail['statistical_mean_bound_hz'])
        assert tail['frozen_mean_bound_hz'] == limits['mean_error_max_hz']
    assert diagnostics['tails'][2]['passes'] is False
    assert diagnostics['tails'][3]['evaluated'] is True
    json.dumps(r, allow_nan=False)


def test_baseline_rejection_keeps_all_summaries_without_manufacturing_limits():
    records = trials()
    for row in records[0]['history']['samples']:
        if row['time_unix_s'] < records[0]['requested_unix_s']:
            row['wavelength_nm'] = C_NM/(C_NM/row['wavelength_nm']+1e6)
    r = fit(records, CONTEXT)
    assert r['reason'] == 'pre_request_reference_has_target_bias'
    diagnostics = r['diagnostics']
    assert diagnostics['stage'] == 'baseline_target_bias'
    assert diagnostics['frozen_candidate_limits'] is None
    assert diagnostics['timing']['allowed_source_intervals'] == 3
    assert len(diagnostics['baselines']) == 2
    assert diagnostics['baselines'][0]['passes'] is False
    assert diagnostics['baselines'][1]['passes'] is True
    assert len(diagnostics['tails']) == 4
    assert all(not tail['evaluated'] and tail['effective_mean_bound_hz'] is None for tail in diagnostics['tails'])
    assert all(tail['mean_error_hz'] is not None and tail['complete_blocks'] > 0 for tail in diagnostics['tails'])
    json.dumps(r, allow_nan=False)


def test_coverage_rejection_explains_timing_without_inventing_a_profile():
    records = timing_jitter_records()
    r = fit(records, {**CONTEXT, 'max_gap_s': .1505})
    assert r['reason'] == 'history_coverage_or_gap_invalid'
    assert r['diagnostics']['stage'] == 'history_coverage'
    assert r['diagnostics']['timing']['effective_gap_s'] == .1505
    assert r['diagnostics']['frozen_candidate_limits'] is None
    assert not matches_context(r, {**CONTEXT, 'max_gap_s': .1505})


def test_causal_rejection_retains_completed_tail_checks_and_first_hit_evidence():
    r = fit(trials(relapse=True), CONTEXT)
    assert r['reason'] == 'heldout_false_early_completion'
    diagnostics = r['diagnostics']
    assert diagnostics['stage'] == 'causal_completion_validation'
    assert all(tail['passes'] for tail in diagnostics['tails'])
    assert diagnostics['causal_evaluations'][-1]['trial_index'] == 2
    assert diagnostics['causal_evaluations'][-1]['relapse_after_completion']
