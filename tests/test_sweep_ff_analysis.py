"""Offline evidence qualification; no controller or network dependencies."""
import copy
import json
import unittest

from ldcon.jobs.sweep_ff_analysis import evaluate_trial


def criteria(**changes):
    result = dict(error_tolerance_hz=1., confirmation_s=.3, min_samples=3,
                  max_sample_gap_s=.11, max_late_drift_hz_per_s=1.,
                  hop_jump_hz=50., hop_confirmation_s=.2,
                  require_optical_quality=False, max_quality_age_s=.2)
    result.update(changes)
    return result


def waveform(errors=None):
    levels = [0., 100., 0.]
    points = {k: [] for k in ('t_unix_s', 'wavelength_nm', 'level_index',
              'in_transition', 't_since_transition_s', 'programmed_offset_hz', 'error_hz')}
    rows = []
    for k, offset in enumerate(levels):
        rows.append(dict(level_index=k, offset_hz=offset, t_start_unix_s=100.+k,
                         t_end_unix_s=101.+k, played=True))
        err = errors[k] if errors else [4., 2., .5, .1, .1, .1, .1, .1, .1, .1]
        for j in range(10):
            points['t_unix_s'].append(100.+k+j*.1)
            points['wavelength_nm'].append(299792458e9/(400e12+offset+err[j]))
            points['level_index'].append(k)
            points['in_transition'].append(j < 1)
            points['t_since_transition_s'].append(j*.1)
            points['programmed_offset_hz'].append(offset)
            points['error_hz'].append(err[j])
    return dict(points=points, levels=rows, completeness={'complete': True},
                quality=dict(staircase_detected=True, record_complete=True, ok=True, clock_step_in_window_us=0),
                t0_unix_s=100., time_base='ESP32 unix time; receive time - system_latency_us',
                clock={'esp32': {'synced': False}},
                parameters={'delay_s': 0., 'reference': {'lock_target_hz': 400e12, 'f_ref_hz': 400e12,
                                                        'f_ref_minus_target_hz': 0.}},
                trial=dict(trial_id='offline-example', candidate=dict(ldc_ff=1., pzt_ff=1.),
                           condition=dict(levels_hz=levels, dwell_s=1., edge_s=0., cycle_period_s=3.,
                                          repetitions=1, direction='up_down', precondition_s=2.)),
                owner_outcome=dict(playback_completed=True, mode_hop=False, outputs_uncertain=False,
                                   cancelled=False, censored=False))


class SweepFFAnalysisTest(unittest.TestCase):
    def test_observed_convergence_not_transition_exclusion(self):
        data = waveform()
        before = copy.deepcopy(data)
        result = evaluate_trial(data, criteria())
        self.assertTrue(result['qualified'], result)
        self.assertAlmostEqual(result['worst_settling_s'], .2)
        self.assertEqual(result['visits'][0]['settled_indices'], list(range(2, 10)))
        self.assertEqual(result['visits'][2]['settled_indices'], list(range(22, 30)))
        self.assertIsNotNone(result['visits'][2]['settled_wavelength_stddev_nm'])
        self.assertEqual(data, before)
        json.dumps(result, allow_nan=False)

    def test_equal_offsets_in_repeated_visits_are_not_pooled(self):
        result = evaluate_trial(waveform(), criteria())
        self.assertEqual(len(result['visits']), 3)
        self.assertEqual(result['visits'][0]['offset_hz'], result['visits'][2]['offset_hz'])
        self.assertNotEqual(result['visits'][0]['input_indices'], result['visits'][2]['input_indices'])

    def test_fitted_good_residual_cannot_hide_bad_raw_error(self):
        data = waveform([[20.]*10]*3)
        data['points']['error_fit_hz'] = [0.]*30
        result = evaluate_trial(data, criteria())
        self.assertFalse(result['qualified'])
        self.assertIn('convergence_not_sustained', result['visits'][0]['reasons'])
        self.assertIsNone(result['qualified_span_hz'])

    def test_short_lucky_crossing_and_late_drift_do_not_qualify(self):
        data = waveform([[4., .1, .1, .1, .1, 2., 2., .1, .1, .1]]*3)
        result = evaluate_trial(data, criteria())
        self.assertFalse(result['qualified'])
        self.assertIsNone(result['visits'][0]['settling_s'])
        data = waveform([[.1]*6 + [.1, .3, .5, .7]]*3)
        result = evaluate_trial(data, criteria(max_late_drift_hz_per_s=.5))
        self.assertIn('late_drift_exceeded', result['visits'][0]['reasons'])

    def test_gap_and_unobserved_visit_never_count_as_success(self):
        data = waveform()
        for col in data['points'].values():
            del col[5]
        result = evaluate_trial(data, criteria())
        self.assertIn('incomplete_temporal_coverage', result['visits'][0]['reasons'])
        data = waveform()
        for col in data['points'].values():
            del col[10:20]
        result = evaluate_trial(data, criteria())
        self.assertIn('exact_visits_not_observed', result['reasons'])

    def test_coverage_requires_start_and_end_not_just_good_middle(self):
        for cut in (slice(0, 2), slice(8, 10)):
            data = waveform()
            for col in data['points'].values():
                del col[cut]
            result = evaluate_trial(data, criteria())
            self.assertFalse(result['qualified'])
            self.assertIn('incomplete_temporal_coverage', result['visits'][0]['reasons'])

    def test_outcome_unknown_hop_cancellation_and_censoring_are_distinct(self):
        for key in ('mode_hop', 'cancelled', 'censored', 'outputs_uncertain'):
            data = waveform()
            data['owner_outcome'][key] = True
            result = evaluate_trial(data, criteria())
            self.assertFalse(result['qualified'])
            self.assertIn('owner_' + key, result['reasons'])
        data = waveform()
        data['owner_outcome']['mode_hop'] = None
        result = evaluate_trial(data, criteria())
        self.assertTrue(result['qualified'])
        self.assertEqual(result['frequency_continuity']['status'], 'continuous')
        del data['owner_outcome']['outputs_uncertain']
        self.assertIn('owner_outcome_unknown', evaluate_trial(data, criteria())['reasons'])

    def test_sustained_uncommanded_jump_is_not_tracking_tolerance(self):
        data = waveform([[.1]*4 + [60.]*6, [.1]*10, [.1]*10])
        result = evaluate_trial(data, criteria(error_tolerance_hz=100., max_late_drift_hz_per_s=1000.))
        self.assertFalse(result['qualified'])
        self.assertEqual(result['frequency_continuity']['status'], 'discontinuity')
        self.assertTrue(result['frequency_continuity']['events'])
        # Commanded 100 Hz changes exceed the independent 50 Hz hop threshold,
        # but an otherwise perfect trace remains acceptable.
        result = evaluate_trial(waveform([[.1]*10]*3), criteria())
        self.assertTrue(result['qualified'])

    def test_step_synchronous_mode_loss_or_bad_ff_does_not_claim_continuity(self):
        data = waveform([[.1]*10, [60.]*10, [.1]*10])
        # No within-plateau jump exists, and even permissive tracking criteria
        # cannot turn this into a physically qualified hop-free waveform.
        result = evaluate_trial(data, criteria(error_tolerance_hz=1000.))
        evidence = result['frequency_continuity']
        self.assertTrue(result['data_valid'])
        self.assertFalse(result['qualified'])
        self.assertEqual(evidence['status'], 'unconfirmed_mode_loss')
        self.assertEqual(evidence['events'], [])
        self.assertEqual(evidence['persistent_large_residuals'][0]['start_index'], 10)
        self.assertIn('bad FF', evidence['qualification'])
        self.assertFalse(result['owner_outcome']['mode_hop'])

    def test_brief_large_delayed_response_is_not_sustained_mode_loss(self):
        data = waveform([[.1]*10, [-100., .1, .1, .1, .1, .1, .1, .1, .1, .1], [.1]*10])
        result = evaluate_trial(data, criteria())
        self.assertTrue(result['qualified'], result)
        self.assertEqual(result['frequency_continuity']['status'], 'continuous')
        self.assertEqual(result['frequency_continuity']['persistent_large_residuals'], [])

    def test_unconfirmed_jump_is_not_proof_of_no_hop(self):
        data = waveform([[.1]*9+[60.], [.1]*10, [.1]*10])
        result = evaluate_trial(data, criteria(error_tolerance_hz=100., max_late_drift_hz_per_s=1000.))
        self.assertFalse(result['qualified'])
        self.assertEqual(result['frequency_continuity']['status'], 'unconfirmed_jump')

    def test_optical_missing_is_reported_not_synthesized(self):
        data = waveform()
        result = evaluate_trial(data, criteria())
        self.assertTrue(result['qualified'])
        self.assertEqual(result['optical_quality']['status'], 'unknown')
        result = evaluate_trial(data, criteria(require_optical_quality=True))
        self.assertFalse(result['qualified'])
        data['points'].update(quality_match_dt_s=[.05]*30, quality_single_mode=[True]*30,
                              quality_degraded=[False]*30)
        self.assertTrue(evaluate_trial(data, criteria(require_optical_quality=True))['qualified'])
        data['points']['quality_match_dt_s'][7] = .5
        self.assertFalse(evaluate_trial(data, criteria(require_optical_quality=True))['qualified'])
        data['points']['quality_match_dt_s'][7] = .05
        data['points']['quality_degraded'][7] = True
        result = evaluate_trial(data, criteria())
        self.assertTrue(result['qualified'])
        self.assertEqual(result['optical_quality']['status'], 'degraded')

    def test_incomplete_and_misaligned_data_never_qualify(self):
        data = waveform()
        data['completeness']['complete'] = False
        self.assertIn('data_incomplete', evaluate_trial(data, criteria())['reasons'])
        data = waveform()
        data['points']['error_hz'].pop()
        self.assertIn('point_arrays_misaligned', evaluate_trial(data, criteria())['reasons'])
        data = waveform()
        data['points']['error_hz'][1] = float('nan')
        result = evaluate_trial(data, criteria())
        self.assertIn('sample_value_invalid', result['reasons'])
        json.dumps(result, allow_nan=False)

    def test_bad_metadata_rejected_but_fit_alignment_does_not_control_scoring(self):
        data = waveform()
        data['trial']['condition']['cycle_period_s'] = 2.
        self.assertIn('waveform_period_inconsistent', evaluate_trial(data, criteria())['reasons'])
        data = waveform()
        data['points']['programmed_offset_hz'][15] = 101.
        data['points']['error_hz'][15] -= 1.
        self.assertTrue(evaluate_trial(data, criteria())['qualified'])
        data = waveform()
        data['points']['level_index'][5] = -1
        self.assertTrue(evaluate_trial(data, criteria())['qualified'])
        data = waveform()
        data['points']['t_unix_s'][5] = data['points']['t_unix_s'][4]
        self.assertIn('sample_times_invalid', evaluate_trial(data, criteria())['reasons'])

    def test_owner_hop_status_must_be_explicit_even_when_unknown(self):
        data = waveform()
        del data['owner_outcome']['mode_hop']
        self.assertIn('owner_outcome_unknown', evaluate_trial(data, criteria())['reasons'])
        data['owner_outcome']['mode_hop'] = 1
        self.assertIn('owner_outcome_unknown', evaluate_trial(data, criteria())['reasons'])

    def test_downstep_overshoot_and_raw_summary_have_signed_mean(self):
        data = waveform([[.1]*10, [2., .1, .1, .1, .1, .1, .1, .1, .1, .1],
                         [-3., -.1, -.1, -.1, -.1, -.1, -.1, -.1, -.1, -.1]])
        result = evaluate_trial(data, criteria())
        self.assertTrue(result['qualified'])
        self.assertEqual(result['visits'][1]['overshoot_hz'], 2.)
        self.assertEqual(result['visits'][2]['overshoot_hz'], 3.)
        self.assertAlmostEqual(result['mean_error_hz'], 0., delta=.1)
        self.assertGreater(result['rms_error_hz'], .1)
        self.assertEqual(result['qualified_span_hz'], 100.)

    def test_baseline_offset_is_not_hidden_by_reference_subtraction(self):
        data = waveform([[.1]*10]*3)
        data['parameters']['reference']['lock_target_hz'] -= 30.
        data['parameters']['reference']['f_ref_minus_target_hz'] = 30.
        result = evaluate_trial(data, criteria())
        self.assertFalse(result['qualified'])
        self.assertTrue(result['data_valid'])
        self.assertAlmostEqual(result['mean_error_hz'], 30.1, delta=.1)
        self.assertTrue(all(v > 29. for v in result['target_error_hz']))
        del data['parameters']['reference']['lock_target_hz']
        self.assertFalse(evaluate_trial(data, criteria())['data_valid'])

    def test_relative_timing_works_without_absolute_sync_but_not_across_clock_step(self):
        data = waveform()
        result = evaluate_trial(data, criteria())
        self.assertTrue(result['data_valid'])
        self.assertTrue(result['qualified'])
        data['quality']['clock_step_in_window_us'] = 1000.
        result = evaluate_trial(data, criteria())
        self.assertFalse(result['data_valid'])
        self.assertIn('clock_step_in_record', result['reasons'])
        data['quality']['clock_step_in_window_us'] = None
        self.assertTrue(evaluate_trial(data, criteria())['data_valid'])
        del data['time_base']
        self.assertFalse(evaluate_trial(data, criteria())['data_valid'])

    def test_fitted_response_delay_does_not_remove_actual_settling_latency(self):
        data = waveform([[.1]*10]*3)
        points = data['points']
        # Actual command changes at t=101, physical response follows at101.4.
        # Source shot-analysis has fitted that delay away, so its derived
        # level indices, t_since and programmed offsets all look settled.
        data['parameters']['delay_s'] = .4
        for i in range(10, 14):
            points['wavelength_nm'][i] = 299792458e9/(400e12+.1)
            points['level_index'][i] = 0
            points['programmed_offset_hz'][i] = 0.
            points['error_hz'][i] = .1
            points['t_since_transition_s'][i] = .6+(i-10)*.1
        result = evaluate_trial(data, criteria(hop_jump_hz=200.))
        self.assertTrue(result['qualified'], result)
        self.assertAlmostEqual(result['visits'][1]['settling_s'], .4)
        self.assertEqual(result['visits'][1]['settled_indices'], list(range(14, 20)))
        self.assertLess(result['target_error_hz'][10], -99.)
        self.assertFalse(result['timing']['scoring_delay_removed'])

    def test_actual_shot_analysis_document_integrates_without_removing_delay(self):
        import numpy as np
        from ldcon.jobs import shot_analysis
        levels = [0., 1e8, 0., -1e8, 0.]
        times = np.arange(99., 106., .05)

        def offsets(t):
            slots = np.floor(t-100).astype(int)
            return np.array([levels[j] if 0 <= j < len(levels) else 0. for j in slots])

        full = dict(t=times, mod_raw=10000.+offsets(times)/1e5,
                    is_new_system_value=np.ones(len(times)), system_latency_us=np.zeros(len(times)),
                    system_value=299792458e9/(400e12+offsets(times-.025)))
        raw = dict(full=full, adc={'t': times, 'adc_raw': full['mod_raw']}, window=[99., 106.],
                   nominal_start=100., search_half_s=.1, complete=True, last_point_unix_s=float(times[-1]),
                   source='synthetic', resolution_s=.001)
        inp = dict(levels=levels, dwell=1., target_hz=400e12, proxy_job='offline', shape='staircase', period=5.)
        data = shot_analysis.analyse(inp, raw, {}, clock_info={'synced': False, 'step_in_window_us': 0}, now=lambda: 106.)
        data['trial'] = waveform()['trial']
        data['trial']['condition'].update(levels_hz=levels, cycle_period_s=5.)
        data['owner_outcome'] = waveform()['owner_outcome']
        result = evaluate_trial(data, criteria(error_tolerance_hz=1e5, max_late_drift_hz_per_s=1e5, hop_jump_hz=2e8))
        self.assertTrue(result['data_valid'], result['reasons'])
        self.assertTrue(result['qualified'], result['reasons'])
        self.assertAlmostEqual(data['parameters']['delay_s'], .025)
        # The fitted25ms has not been subtracted: sampling resolves settling
        # only at the first50ms post-command sample.
        self.assertGreater(result['visits'][1]['settling_s'], .045)

    def test_explicit_criteria_required(self):
        for key in criteria():
            p = criteria()
            del p[key]
            with self.assertRaises(ValueError):
                evaluate_trial(waveform(), p)
        with self.assertRaises(ValueError):
            evaluate_trial(waveform(), criteria(min_samples=1))
        with self.assertRaises(ValueError):
            evaluate_trial(waveform(), criteria(error_tolerance_hz=float('nan')))


if __name__ == '__main__':
    unittest.main()
