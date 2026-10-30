import json
import os

import pytest

from ldcon.jobs.quality_baseline import QualityBaseline, export_metrics, export_reference_metrics, safe_read


def cycle(source, *, value=.2, **changes):
    row = dict(state='locked', source_acquired_unix_s=source, acquired_s=source,
        native_input_seq=int(source*10), native_input_stale=False,
        control_ready_passed=True, target_ramp_active=False,
        mode_id='desired', target_frequency_hz=700e12,
        owner_context=dict(system_id='laser', generation='owner-1', policy_revision='policy-1'),
        outputs=dict(current=.9, temperature=.14, piezo=25000+source),
        metrics=dict(side_peak_height_max=value, side_peak_prominence_max=value/2,
                     main_fwhm_fraction=.12, near_main_peak_prominence_max=.01),
        single_mode=False, maintenance_required=True, entry_gate_passed=False,
        dither_response_valid=True, dither_response=.0024, dither_response_unit='nm / V',
        dither_source_start_unix_s=100.1, dither_source_unix_s=source)
    row.update(changes)
    return row


def collector(tmp_path, **kwargs):
    options = dict(laser_id='laser', controller_job_id='job-1', clock=lambda: 100.)
    options.update(kwargs)
    obj = QualityBaseline(tmp_path/'quality-baseline.json', **options)
    obj.observe(cycle(99.9), now=100.)
    return obj


def complete(obj, accepted=100., *, family='current', action='adopt-1'):
    assert obj.accepted(family, action, accepted)['state'] == 'pending'
    result = None
    for index, offset in enumerate((.5, 1., 1.5, 2., 2.5)):
        result = obj.observe(cycle(accepted+offset, value=.1*index,
            dither_source_start_unix_s=accepted+.1), now=accepted+offset)
    assert result == {'state': 'available', 'persisted': True}
    return safe_read(obj.path)


def test_post_adoption_medians_allow_degraded_quality_and_preserve_source_times(tmp_path):
    obj = collector(tmp_path)
    record = complete(obj)
    assert record['metrics']['side_peak_height_max'] == pytest.approx(.2)
    assert record['sample_count'] == 5
    assert record['source_start_unix_s'] == 100.1
    assert record['optical_source_start_unix_s'] == 100.5
    assert record['source_end_unix_s'] == 102.5
    assert record['recorded_unix_s'] == 102.5
    assert record['outputs']['piezo'] == 25102.5
    assert record['output_ranges']['piezo'] == [25100.5, 25102.5]
    assert record['dither']['unit'] == 'nm/V'
    exported = export_metrics(record, now=1000.)
    assert exported['quality_baseline_available'] == 1
    assert exported['quality_baseline_age_s'] == 897.5
    assert exported['quality_baseline_recorded_unix_s'] == 102.5
    assert exported['quality_baseline_dither_response'] == .0024
    assert safe_read(obj.path)['recorded_unix_s'] == 102.5


def test_no_acceptance_does_not_write_or_replace_a_baseline(tmp_path):
    obj = collector(tmp_path)
    for source in range(101, 110):
        assert obj.observe(cycle(source), now=source)['state'] == 'absent'
    assert not obj.path.exists()


def test_new_pending_adjustment_hides_previous_measurements_but_keeps_provenance(tmp_path):
    obj = collector(tmp_path)
    old = complete(obj)
    old.pop('previous', None)
    obj.clock = lambda: 110.
    obj.observe(cycle(109.9), now=110.)
    assert obj.accepted('pi', 'pi-accepted', 110.)['persisted']
    pending = safe_read(obj.path)
    assert pending['state'] == 'pending'
    assert pending['previous'] == old
    assert 'metrics' not in pending
    assert export_metrics(pending, 111.) == {'quality_baseline_available': 0}
    for source in (110.5, 111., 111.5, 112., 112.5):
        obj.observe(cycle(source, dither_source_start_unix_s=110.1), now=source)
    measured = safe_read(obj.path)
    assert measured['family'] == 'pi' and measured['available']
    assert measured['previous'] == old


def test_duplicate_acceptance_and_restart_do_not_refresh_acceptance_or_deadline(tmp_path):
    obj = collector(tmp_path)
    assert obj.accepted('current', 'adopt-1', 100.)['persisted']
    before = safe_read(obj.path)
    obj = collector(tmp_path, clock=lambda: 105.)
    result = obj.accepted('current', 'adopt-1', 105.)
    assert result['duplicate']
    assert safe_read(obj.path) == before
    for source in (105.5, 106., 106.5, 107., 107.5):
        obj.observe(cycle(source), now=source)
    record = safe_read(obj.path)
    assert record['accepted_unix_s'] == 100.
    assert record['deadline_unix_s'] == 220.
    assert record['available']


def test_another_controller_does_not_resume_old_pending_capture(tmp_path):
    obj = collector(tmp_path)
    obj.accepted('current', 'adopt-1', 100.)
    other = collector(tmp_path, controller_job_id='job-2')
    for source in range(101, 110):
        result = other.observe(cycle(source), now=source)
    assert result['state'] == 'pending'
    assert not safe_read(obj.path)['available']


def test_deadline_is_fixed_and_unavailable_never_inherits_old_numbers(tmp_path):
    obj = collector(tmp_path, deadline_s=10.)
    old = complete(obj)
    old.pop('previous', None)
    obj.clock = lambda: 110.
    obj.observe(cycle(109.9), now=110.)
    obj.accepted('feedforward', 'ff-1', 110.)
    result = obj.observe(cycle(120.1), now=120.1)
    record = safe_read(obj.path)
    assert result['state'] == 'unavailable'
    assert record['previous'] == old
    assert 'deadline' in record['unavailable_reason']
    assert export_metrics(record, 121.) == {'quality_baseline_available': 0}


@pytest.mark.parametrize('changes', [
    dict(source_acquired_unix_s=100.), dict(source_acquired_unix_s=None),
    dict(source_acquired_unix_s=105.), dict(native_input_stale=True),
    dict(native_input_seq=True), dict(control_ready_passed=False),
    dict(target_ramp_active=True), dict(quality_baseline_blocked=True),
    dict(fringe_period_qualified=False),
    dict(parameter_adjustment={'phase': 'candidate'}), dict(pid_tuning={'ready': True}),
    dict(state='lost'), dict(metrics={'side_peak_height_max': float('nan')}),
    dict(outputs={'current': .9}), dict(mode_id=None),
])
def test_invalid_or_active_trial_rows_never_count_as_baseline_samples(tmp_path, changes):
    obj = collector(tmp_path)
    obj.accepted('pi', 'pi-1', 100.)
    result = obj.observe(cycle(101., **changes), now=101.)
    assert result['state'] == 'pending'
    assert not obj.rows
    assert not safe_read(obj.path)['available']


def test_repeated_or_regressed_sources_and_native_sequences_do_not_count(tmp_path):
    obj = collector(tmp_path, require_dither=False)
    obj.accepted('current', 'adopt-1', 100.)
    obj.observe(cycle(101.), now=101.)
    for _ in range(8):
        obj.observe(cycle(101.), now=101.1)
    obj.observe(cycle(101.5, native_input_seq=1010), now=101.5)
    obj.observe(cycle(100.5, native_input_seq=1005), now=101.5)
    assert len(obj.rows) == 1
    for source in (102., 103., 104., 105.):
        obj.observe(cycle(source), now=source)
    assert safe_read(obj.path)['sample_count'] == 5


def test_stale_source_and_large_gap_restart_optical_window(tmp_path):
    obj = collector(tmp_path, require_dither=False)
    obj.accepted('current', 'adopt-1', 100.)
    obj.observe(cycle(101.), now=101.)
    obj.observe(cycle(102.), now=110.)
    assert not obj.rows
    for source in (111., 112., 113., 114.):
        obj.observe(cycle(source), now=source)
    obj.observe(cycle(118.), now=118.)
    assert len(obj.rows) == 1
    assert not safe_read(obj.path)['available']


@pytest.mark.parametrize('change', [
    dict(mode_id='other'), dict(target_frequency_hz=701e12),
    dict(owner_context=dict(system_id='laser', generation='owner-2', policy_revision='policy-1')),
    dict(owner_context=dict(system_id='laser', generation='owner-1', policy_revision='policy-2')),
])
def test_context_change_invalidates_capture_instead_of_blessing_new_context(tmp_path, change):
    obj = collector(tmp_path)
    obj.accepted('current', 'adopt-1', 100.)
    result = obj.observe(cycle(101., **change), now=101.)
    assert result['state'] == 'unavailable'
    assert 'changed' in safe_read(obj.path)['unavailable_reason']


def test_missing_pre_acceptance_context_is_not_inferred_from_later_rows(tmp_path):
    obj = QualityBaseline(tmp_path/'quality-baseline.json', laser_id='laser',
                          controller_job_id='job-1', clock=lambda: 100.)
    obj.accepted('current', 'adopt-1', 100.)
    for source in range(101, 110):
        obj.observe(cycle(source), now=source)
    assert not safe_read(obj.path)['available']
    assert safe_read(obj.path)['expected_context'] is None


def test_explicit_authoritative_context_supports_first_acceptance(tmp_path):
    obj = QualityBaseline(tmp_path/'quality-baseline.json', laser_id='laser',
                          controller_job_id='job-1', clock=lambda: 100.)
    obj.accepted('current', 'adopt-1', 100., expected_context=cycle(99.))
    for source in range(101, 106):
        obj.observe(cycle(source), now=source)
    assert safe_read(obj.path)['available']


@pytest.mark.parametrize('change', [
    dict(dither_response_valid=False), dict(dither_response=float('nan')),
    dict(dither_source_start_unix_s=99.), dict(dither_source_start_unix_s=100.),
    dict(dither_source_unix_s=200.), dict(dither_response_unit='nm/A'),
])
def test_dither_requires_valid_wholly_post_acceptance_source_window_and_unit(tmp_path, change):
    obj = collector(tmp_path)
    obj.accepted('current', 'adopt-1', 100.)
    for source in range(101, 106):
        result = obj.observe(cycle(source, **change), now=source)
    assert result['state'] == 'pending'
    assert len(obj.rows) == 5
    assert not safe_read(obj.path)['available']
    assert obj.observe(cycle(106.), now=106.)['state'] == 'available'


def test_optional_dither_and_optional_metric_are_omitted_not_fabricated(tmp_path):
    obj = collector(tmp_path, require_dither=False)
    obj.accepted('current', 'adopt-1', 100.)
    for source in range(101, 106):
        row = cycle(source, dither_response_valid=False)
        if source == 103:
            del row['metrics']['near_main_peak_prominence_max']
        obj.observe(row, now=source)
    result = export_metrics(safe_read(obj.path), 106.)
    assert result['quality_baseline_available'] == 1
    assert 'quality_baseline_near_main_peak_prominence_max' not in result
    assert 'quality_baseline_dither_response' not in result


def test_persistence_failure_is_diagnostic_and_does_not_refresh_or_raise(tmp_path, monkeypatch):
    obj = collector(tmp_path)
    def fail(*args):
        raise PermissionError('test failure')
    monkeypatch.setattr(os, 'replace', fail)
    result = obj.accepted('current', 'adopt-1', 100.)
    assert result['state'] == 'pending' and not result['persisted']
    for source in range(101, 106):
        result = obj.observe(cycle(source), now=source)
    assert result['state'] == 'available' and not result['persisted']
    assert obj.record['accepted_unix_s'] == 100.
    assert safe_read(obj.path) is None
    assert not list(tmp_path.glob('.quality-baseline.json.*'))


def test_delayed_older_controller_cannot_overwrite_newer_pending_adjustment(tmp_path):
    old = collector(tmp_path)
    old.accepted('current', 'adopt-1', 100.)
    new = collector(tmp_path, controller_job_id='job-2', clock=lambda: 110.)
    new.observe(cycle(109.9), now=110.)
    new.accepted('pi', 'pi-1', 110.)
    for source in range(111, 116):
        result = old.observe(cycle(source), now=source)
    assert not result['persisted']
    assert safe_read(old.path)['action_id'] == 'pi-1'
    assert safe_read(old.path)['state'] == 'pending'


def test_safe_read_rejects_symlinks_oversize_nonregular_invalid_and_wrong_laser(tmp_path):
    obj = collector(tmp_path)
    record = complete(obj)
    assert safe_read(obj.path, laser_id='other') is None
    link = tmp_path/'link.json'
    link.symlink_to(obj.path)
    assert safe_read(link) is None
    fifo = tmp_path/'pipe'
    os.mkfifo(fifo)
    assert safe_read(fifo) is None
    obj.path.write_text(' '*(128*1024+1))
    assert safe_read(obj.path) is None
    obj.path.write_text('{')
    assert safe_read(obj.path) is None
    record['metrics']['side_peak_height_max'] = float('nan')
    obj.path.write_text(json.dumps(record))
    assert safe_read(obj.path) is None


def test_publication_refuses_symlink_parent_and_file(tmp_path):
    actual = tmp_path/'actual'
    actual.mkdir()
    alias = tmp_path/'alias'
    alias.symlink_to(actual, target_is_directory=True)
    obj = QualityBaseline(alias/'quality-baseline.json', laser_id='laser',
                          controller_job_id='job-1', clock=lambda: 100.)
    assert not obj.accepted('current', 'adopt-1', 100.)['persisted']
    target = actual/'target.json'
    target.write_text('unchanged')
    obj = collector(tmp_path)
    obj.path.symlink_to(target)
    assert not obj.accepted('current', 'adopt-1', 100.)['persisted']
    assert target.read_text() == 'unchanged'


@pytest.mark.parametrize('mutation', [
    lambda r: r.update(available=False),
    lambda r: r.update(recorded_unix_s=99.),
    lambda r: r.update(source_start_unix_s=99.),
    lambda r: r.update(sample_count=1),
    lambda r: r.update(expected_context=None),
    lambda r: r['dither'].update(source_start_unix_s=99.),
    lambda r: r['dither'].update(unit='nm/A'),
])
def test_metadata_rejects_inconsistent_record_instead_of_exporting_values(tmp_path, mutation):
    record = complete(collector(tmp_path))
    mutation(record)
    assert export_metrics(record, 200.) == {'quality_baseline_available': 0}


def test_future_metadata_and_absent_record_are_unavailable(tmp_path):
    record = complete(collector(tmp_path))
    assert export_metrics(record, 101.) == {'quality_baseline_available': 0}
    assert export_metrics(None, 101.) == {'quality_baseline_available': 0}


def test_normal_monitoring_does_not_refresh_a_completed_reference(tmp_path):
    obj = collector(tmp_path)
    before = complete(obj)
    for source in range(110, 120):
        obj.observe(cycle(source, value=.9), now=source)
    assert safe_read(obj.path) == before


def test_completed_persistence_retry_preserves_original_measurement_time(tmp_path, monkeypatch):
    obj = collector(tmp_path)
    obj.accepted('pi', 'pi-1', 100.)
    replace = os.replace
    def fail(*args):
        raise PermissionError('temporary failure')
    monkeypatch.setattr(os, 'replace', fail)
    for source in range(101, 106):
        result = obj.observe(cycle(source), now=source)
    assert result['state'] == 'available' and not result['persisted']
    assert safe_read(obj.path)['state'] == 'pending'
    monkeypatch.setattr(os, 'replace', replace)
    assert obj.observe(cycle(110.), now=110.)['persisted']
    assert safe_read(obj.path)['recorded_unix_s'] == 105.


@pytest.mark.parametrize('failure', ['pending', 'context', 'deadline'])
def test_historical_reference_survives_new_capture_without_becoming_current(tmp_path, failure):
    obj = collector(tmp_path)
    old = complete(obj)
    obj.clock = lambda: 110.
    obj.observe(cycle(109.9), now=110.)
    obj.accepted('current', 'second', 110.)
    now = 111.
    code = 1
    if failure == 'context':
        obj.observe(cycle(111., target_frequency_hz=701e12), now=111.)
        code = 3
    elif failure == 'deadline':
        # No worker or wavemeter needed to recognize an expired pending window.
        now = 231.
        code = 3
    raw = obj.path.read_bytes()
    record = safe_read(obj.path)
    result = export_reference_metrics(record, now)
    assert result['quality_baseline_acquisition_state_code'] == code
    assert result['quality_baseline_reference_available'] == 1
    assert result['quality_baseline_reference_from_previous'] == 1
    assert result['quality_baseline_reference_side_peak_height_max'] == old['metrics']['side_peak_height_max']
    assert result['quality_baseline_reference_source_end_unix_s'] == 102.5
    assert result['quality_baseline_reference_age_s'] == now - 102.5
    assert export_metrics(record, now) == {'quality_baseline_available': 0}
    assert obj.path.read_bytes() == raw
    assert result == export_reference_metrics(record, now)


def test_historical_reference_uses_new_measurement_only_when_completed(tmp_path):
    obj = collector(tmp_path)
    complete(obj)
    obj.clock = lambda: 110.
    obj.observe(cycle(109.9), now=110.)
    new = complete(obj, 110., action='second')
    result = export_reference_metrics(new, 120.)
    assert result['quality_baseline_reference_source_end_unix_s'] == 112.5
    assert result['quality_baseline_reference_from_previous'] == 0
    assert result['quality_baseline_acquisition_state_code'] == 2


def test_historical_reference_never_salvages_invalid_or_future_evidence(tmp_path):
    obj = collector(tmp_path)
    old = complete(obj)
    assert export_reference_metrics(old, 100.)['quality_baseline_reference_available'] == 0
    obj.clock = lambda: 110.
    obj.accepted('current', 'second', 110., expected_context=cycle(109.9))
    record = safe_read(obj.path)
    record['previous']['laser_id'] = 'other-laser'
    for bad in (record, None, {}):
        result = export_reference_metrics(bad, 120.)
        assert result['quality_baseline_reference_available'] == 0
        assert not any(k.endswith('side_peak_height_max') for k in result)


def test_no_historical_reference_is_explicitly_absent(tmp_path):
    obj = collector(tmp_path)
    obj.accepted('current', 'first', 100.)
    result = export_reference_metrics(safe_read(obj.path), 101.)
    assert result['quality_baseline_reference_available'] == 0
    assert result['quality_baseline_acquisition_state_code'] == 1
