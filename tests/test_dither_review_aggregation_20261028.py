"""2026-10-28: the dither quality limit (steady_lock.window.warning_response) is compared with the
MEAN of the valid post-settle dither fits at the selected point (aggregation 'mean_of_valid_fits'),
not the maximum single 32 s fit. Numbers from sr422 task 32b39f45 (selected 1.2612 V)."""
import importlib.util
import statistics
from pathlib import Path

import pytest

from ldcon.agents.adjustment_quality import QualityReview

_spec = importlib.util.spec_from_file_location('dither_review_base', Path(__file__).with_name('test_dither_quality_review_20261028.py'))
base = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(base)
q = base.q
rig = base.rig
DPATH = base.DPATH
OPTICAL = 'devices.rig.control_settings.optical.single_mode_max.side_peak_height_max'

# 32b39f45: three post-settle windows at 1.2612 V, then the adoption-verification window.
WINDOWS = (('w3', 700, .0018865264062456779), ('w4', 800, .0023004358722261194), ('w8', 850, .0019050270841948647))
ADOPTION = .0029311772331721653
PROPOSED = .0028
MARGIN = .1   # margin_fraction recorded by 32b39f45


def test_32b39f45_record_compares_the_mean_and_reports_n_mean_max_std(tmp_path):
    review = QualityReview(tmp_path, base.document())
    for identifier, start, value in WINDOWS:
        latest = q.snapshot(base.measurement(identifier, start, dither=value)); review.observe(latest)
    candidate = base.best_practical({DPATH: PROPOSED}); candidate['measurement_ids'] = [w[0] for w in WINDOWS]
    result = review.record(candidate, latest)
    row = result['comparisons'][DPATH]
    values = [w[2] for w in WINDOWS]
    assert row['aggregation'] == 'mean_of_valid_fits' and row['n'] == 3
    assert row['mean'] == pytest.approx(statistics.fmean(values)) == row['observed_mean']
    assert row['max'] == row['observed_max'] == max(values)
    assert row['std'] == pytest.approx(statistics.stdev(values))
    assert row['meets_proposed'] is True and row['meets_old'] is True
    # mean .00203 <= .0024*(1-.1): the old limit is met with margin under the mean rule
    assert row['meets_old_with_margin'] is True
    assert result['physical_improvement_claimed'] is False
    assert result['comparisons'][OPTICAL]['aggregation'] == 'max_of_frames'


@pytest.mark.parametrize('proposed, accepted', [(PROPOSED, True), (.0025, False)])
def test_32b39f45_numbers_with_the_mean_rule_at_the_host(rig, proposed, accepted):
    rig.prepare({DPATH: proposed}, margin=MARGIN, fits=WINDOWS, final_dither=ADOPTION)
    rig.inspect['dither'].update(dither_response=ADOPTION, dither_source_unix_s=995.)   # same fit as the adoption row
    fits = [w[2] for w in WINDOWS]+[ADOPTION]
    mean = statistics.fmean(fits)                       # .0022558; max .00293 > .0028*(1-.1)
    assert max(fits) > proposed*(1-MARGIN)              # the maximum rule would refuse both
    assert (mean <= proposed*(1-MARGIN)) is accepted   # .00226 <= .00252 ; .00226 > .00225
    if accepted:
        assert base.apply(rig)['status'] == 'saved_pending_owner' and len(rig.calls) == 1
        record = base.module.read_json(base.module._journal(rig.runtime, 'task', 'quality-task'), {})
        adopted = record['verified_outcome']['post_adoption_verification']['dither_aggregate']
        assert adopted['n'] == 4 and adopted['mean'] == pytest.approx(mean) and adopted['max'] == ADOPTION
        fresh = record['owner_verification']['dither_aggregate']
        assert fresh['n'] == 4 and fresh['fresh_fit_included'] is False   # not counted twice
        assert record['verified_outcome']['post_adoption_dither_meets_old_with_margin'] is False
    else:
        with pytest.raises(ValueError, match='adopted dither'):base.apply(rig)
        assert not rig.calls


def test_newer_fresh_owner_fit_joins_the_mean(rig):
    rig.prepare({DPATH: PROPOSED}, margin=MARGIN, fits=WINDOWS, final_dither=ADOPTION)
    rig.inspect['dither'].update(dither_response=.0026, dither_source_unix_s=999.)
    base.apply(rig)
    record = base.module.read_json(base.module._journal(rig.runtime, 'task', 'quality-task'), {})
    fresh = record['owner_verification']['dither_aggregate']
    assert fresh['n'] == 5 and fresh['fresh_fit_included'] is True
    assert fresh['mean'] == pytest.approx(statistics.fmean([w[2] for w in WINDOWS]+[ADOPTION, .0026]))


def test_single_window_is_still_refused(tmp_path):
    review = QualityReview(tmp_path, base.document())
    latest = q.snapshot(base.measurement('w8', 850, dither=.0019)); review.observe(latest)
    candidate = base.best_practical({DPATH: PROPOSED}); candidate['measurement_ids'] = ['w8']
    with pytest.raises(ValueError, match='two distinct'):review.record(candidate, latest)
    assert review.outcome is None


def test_optical_leaves_keep_the_maximum_rule_and_relabelling_is_refused(rig):
    # optical side_peak_height_max .1 (max of frames) against .3*(1-.1): unchanged rule
    rig.prepare({DPATH: PROPOSED, OPTICAL: .105}, margin=MARGIN, fits=WINDOWS, final_dither=ADOPTION)
    with pytest.raises(ValueError, match='adopted quality'):base.apply(rig)
    assert not rig.calls
    # Old limit unmet by the mean (.0025 > .0024*(1-.1)): restored/adequate is refused and the
    # change is recorded as a reclassification, never a physical improvement.
    review = QualityReview(rig.directory, base.document())
    for identifier, start, value in (('a', 700, .0024), ('b', 800, .0026), ('c', 850, .0025)):
        latest = q.snapshot(base.measurement(identifier, start, dither=value)); review.observe(latest)
    candidate = base.best_practical({DPATH: PROPOSED}); candidate['measurement_ids'] = ['a', 'b', 'c']
    with pytest.raises(ValueError, match='relabel'):review.record(dict(candidate, classification='restored_adequate'), latest)
    result = review.record(candidate, latest)
    assert result['reclassified_paths'] == [DPATH] and result['physical_improvement_claimed'] is False
