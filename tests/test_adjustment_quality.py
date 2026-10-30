import copy
import json

import pytest

from ldcon.agents.adjustment_quality import QualityReview

PATH = 'devices.rig.control_settings.optical.single_mode_max.side_peak_height_max'


def document():
    return dict(id='laser', devices=dict(rig=dict(control_settings=dict(optical=dict(
        single_mode_max=dict(side_peak_height_max=.3), warning_max=dict(side_peak_height_max=.5))))))


def measurement(identifier, start, *, value=.77, peak=.1, branch='same', state='measured'):
    frames = [dict(owner_context=dict(system_id='laser', generation='generation', policy_revision='policy'),
        native_pi_active=True, outputs=dict(current=value, temperature=.5),
        observation=dict(mode_id=branch, single_mode=True, metrics=dict(side_peak_height_max=peak),
            frequency=dict(acquired_unix_s=start+20+i),
            fringe_analysis=dict(raw_patterns=[dict(pixels=[0., 1., 2., 1.])]))) for i in range(5)]
    return dict(id=identifier, state=state, target=value, post_settle_unix_s=start,
        optical_frames=frames, metrics_median5=dict(side_peak_height_max=peak),
        dither=dict(dither_response=.001, dither_response_valid=True,
            dither_source_start_unix_s=start+1, dither_source_unix_s=start+30))


def snapshot(row, *, segment=0):
    return dict(controller_job_id='controller', status=dict(state='waiting_for_decision',
        segment=segment, decision_revision='revision-'+row['id']), latest_result=row)


def outcome(**overrides):
    return dict(selected_current=.77, measurement_ids=['first', 'second'],
        classification='restored_adequate', margin_fraction=.1,
        history_assessment=dict(evidence_refs=['adjustment-input-1.json'], interval_unix_s=[0., 100.],
            sample_coverage='Two measured windows; earlier drift coverage is absent.',
            comparability='Same target, branch, temperature and owner policy.',
            drift_and_interventions='No demonstrated automatic correction performance.',
            practical_requirement='Optical side peaks below the historical threshold with 10% margin; dither response useful.',
            unmeasured_alternatives='Upper side not measured; no full-window midpoint claim.'),
        rationale='Restored previously useful measured quality; stop with margin.',
        follow_up='Compare fresh post-adoption drift before reconsidering thresholds.', changes={}, **overrides)


@pytest.fixture
def review(tmp_path):
    review = QualityReview(tmp_path, document())
    review.observe(snapshot(measurement('first', 100)))
    latest = snapshot(measurement('second', 150))
    review.observe(latest)
    return review, latest


def test_repeated_verified_quality_records_old_thresholds_raw_evidence_and_no_application(review, tmp_path):
    review, latest = review
    result = review.record(outcome(), latest)
    review.require_adoption(.77, latest)
    assert result['historical_quality_with_margin']
    assert result['applied'] is False and result['application_status'] == 'thresholds_unchanged'
    assert result['old_criterion_revision'] == result['proposed_criterion_revision']
    saved = json.loads((tmp_path/'adjustment-quality-evidence.json').read_text())
    assert saved['first']['result']['optical_frames'][0]['observation']['fringe_analysis']['raw_patterns']
    assert result['physical_single_mode_guarantee'] is False


@pytest.mark.parametrize('damage', ['same_id', 'wrong_current', 'old_segment', 'overlap', 'branch', 'stale_fit', 'repeated_frame'])
def test_unverified_stability_cannot_support_quality_candidate(review, damage):
    review, latest = review
    p = outcome()
    if damage == 'same_id': p['measurement_ids'] = ['first', 'first']
    elif damage == 'wrong_current': p['selected_current'] = .78
    elif damage == 'old_segment': latest['status']['segment'] = 1
    else:
        row = review.measurements['second']['result']
        if damage == 'overlap': row.update(measurement('second', 100))
        elif damage == 'branch': row['optical_frames'][0]['observation']['mode_id'] = 'other'
        elif damage == 'stale_fit': row['dither']['dither_source_start_unix_s'] = row['post_settle_unix_s']
        elif damage == 'repeated_frame': row['optical_frames'][1] = copy.deepcopy(row['optical_frames'][0])
    with pytest.raises(ValueError): review.record(p, latest)
    assert review.outcome is None


def test_relaxed_candidate_retains_failed_historical_quality_and_pending_application(tmp_path):
    review = QualityReview(tmp_path, document())
    for i, start in [('first', 100), ('second', 150)]:
        latest = snapshot(measurement(i, start, peak=.4))
        review.observe(latest)
    p = outcome(); p.update(classification='best_practical', changes={PATH: .45},
        rationale='Historical quality remains unmet after bounded exploration. Candidate operational limit awaits a practical-quality review.')
    result = review.record(p, latest)
    assert result['comparisons'][PATH]['meets_old'] is False
    assert result['comparisons'][PATH]['meets_proposed'] is True
    assert not result['historical_quality_with_margin'] and not result['applied']
    assert result['application_status'] == 'pending_point_adoption'
    p['classification'] = 'restored_adequate'
    with pytest.raises(ValueError, match='relabel'): review.record(p, latest)


@pytest.mark.parametrize('changes', [{PATH: float('nan')}, {PATH: True},
    {'devices.rig.control_settings.recovery.capture_hz': 100e6},
    {'devices.rig.control_settings.optical.single_mode_max.missing': .4},
    {'devices.rig.control_settings.optical.warning_max.side_peak_height_max': .6}])
def test_quality_candidate_cannot_propose_safety_unknown_or_inconsistent_fields(review, changes):
    review, latest = review
    p = outcome(); p['changes'] = changes
    with pytest.raises(ValueError): review.record(p, latest)


def test_history_absence_is_not_silently_replaced_by_current_point(review):
    review, latest = review
    p = outcome(); p['history_assessment'].pop('drift_and_interventions')
    with pytest.raises(ValueError, match='History'): review.record(p, latest)


def test_new_evidence_requires_review_again_before_adoption(review):
    review, latest = review
    review.record(outcome(), latest)
    latest = snapshot(measurement('third', 200))
    review.observe(latest)
    with pytest.raises(ValueError, match='current quality'): review.require_adoption(.77, latest)


def test_changed_segment_does_not_reassign_old_measurement_to_new_context(review):
    review, latest = review
    latest['status']['segment'] = 1
    review.observe(latest)
    assert review.measurements['second']['segment'] == 0


def test_adoption_quality_is_verified_separately_and_drift_is_not_hidden(review):
    review, latest = review
    review.record(outcome(), latest)
    final = snapshot(measurement('adopted', 200, peak=.4, state='adopted'))
    review.observe(final); review.finalize(final)
    assert review.outcome['historical_quality_with_margin'] is True
    assert review.outcome['post_adoption_meets_old_with_margin'] is False
    assert review.outcome['post_adoption_verification']['metric_ranges']['side_peak_height_max']['max'] == .4
