"""Offline report history: observations are not authority or inferred success."""
import hashlib
import json
from types import SimpleNamespace

import pytest
import yaml

from ldcon.jobs import interface_worker
from ldcon.jobs.operation_execution import frozen_inputs
from ldcon.jobs.operation_history import NAME, OperationHistory, optional_call, optional_history


def frozen(directory, operation='servo.native_current'):
    laser = dict(id='laser-a', name='Laser A', target=dict(frequency_hz=4e14),
        devices={}, interfaces={}, secret='do-not-copy-secret')
    card = dict(id='test-card', operation=operation, uses=[], parameters={})
    deck = dict(id='test-deck', cards=[card])
    invocation = dict(card_id=card['id'], parameters=dict(tolerance_hz=1e6,
        max_sample_age_s=2., objective='secret prompt https://host/token?sig=secret',
        webhook='https://host/token?sig=secret'))
    for name, value in (('laser.yaml', laser), ('interface-deck.yaml', deck)):
        (directory/name).write_text(yaml.safe_dump(value))
    (directory/'invocation.json').write_text(json.dumps(invocation))
    return frozen_inputs(directory)


def read(directory):
    return json.loads((directory/NAME).read_text())


def emit(history, kind, payload, elapsed=1.):
    history.event(kind, payload, elapsed_s=elapsed, run_id='run-a', monotonic_s=10.)


def native_row(**kwargs):
    return dict(dict(locked=True, native_pi_active=True, target_ramp_active=False,
        frequency_hz=4e14, error_hz=1., acquired_s=10.,
        source_acquired_unix_s=1000., dac_code=30000,
        native_config=dict(pid_multiplier=1)), **kwargs)


def worker_job(directory):
    frozen(directory, 'monitor.wavelength')
    (directory/'invocation.json').write_text(json.dumps(dict(
        card_id='test-card', parameters=dict(duration_s=1.))))


def finish(history, status='succeeded', *, effects='reported'):
    history.finish(dict(status=status, operation_outcome=dict(execution_state=status, effects=effects)))


def test_frozen_configuration_refs_and_actual_start_provenance_do_not_copy_secrets(tmp_path):
    inputs = frozen(tmp_path)
    (tmp_path/'execution-provenance.json').write_text(json.dumps(dict(
        source_commit='abcdef1234567', release_id='release-fixture', bearer='do-not-copy')))
    history = OperationHistory(tmp_path, inputs, wall_clock=lambda: 1000.)
    row = read(tmp_path)
    assert row['schema_version'] == 1 and row['laser_id'] == 'laser-a'
    assert row['provenance']['commit'] == 'abcdef1234567'
    assert row['provenance']['release_id'] == 'release-fixture'
    assert row['start']['utc'].endswith('+00:00')
    assert row['start']['local_timezone'] and row['start']['local']
    assert row['end'] is None and row['outcome'] is None
    assert row['conditions'] == dict(max_sample_age_s=2., tolerance_hz=1e6)
    raw = (tmp_path/NAME).read_text()
    assert 'secret' not in raw and 'https://' not in raw and 'bearer' not in raw
    ref = next(r for r in row['frozen_inputs'] if r['path'] == 'laser.yaml')
    assert ref['sha256'] == hashlib.sha256((tmp_path/'laser.yaml').read_bytes()).hexdigest()
    # A later config edit never retroactively changes this run's frozen identity.
    (tmp_path/'laser.yaml').write_text('changed: true')
    finish(history)
    assert read(tmp_path)['frozen_inputs'] == row['frozen_inputs']


def test_unknown_commit_is_explicit_not_inferred_from_current_checkout(tmp_path):
    OperationHistory(tmp_path, frozen(tmp_path), wall_clock=lambda: 1000.)
    provenance = read(tmp_path)['provenance']
    assert provenance['commit'] is None and provenance['commit_basis'] == 'not_recorded'
    assert all('sha256' in r for r in provenance['executing_sources'])


def test_resolved_defaults_and_selected_measurements_are_recorded_without_changing_frozen_hashes(tmp_path):
    inputs = frozen(tmp_path)
    inputs['invocation']['parameters'] = {}
    history = OperationHistory(tmp_path, inputs, wall_clock=lambda: 1000.)
    refs = read(tmp_path)['frozen_inputs']
    history.parameters_resolved(dict(tolerance_hz=1e6, max_sample_age_s=2.,
        measurements=dict(wavelength=True, optical_quality=True, dither=False),
        measurement_max_age_s=2., prompt='not copied'))
    emit(history, 'trace', native_row())
    result = read(tmp_path)
    assert result['conditions']['measurements'] == dict(wavelength=True, optical_quality=True, dither=False)
    assert result['conditions_basis'] == 'normalized_worker_parameters'
    assert result['frozen_inputs'] == refs and len(result['milestones']) == 1
    assert 'not copied' not in (tmp_path/NAME).read_text()


def test_commit_comes_from_nearest_executing_release_not_another_python_prefix(tmp_path):
    from ldcon.jobs.operation_history import _source_manifest
    release = tmp_path/'releases'/'gabcdef1'
    package = release/'.venv/lib/python/site-packages/ldcon'
    package.mkdir(parents=True)
    (release/'SOURCE.json').write_text(json.dumps(dict(sha='abcdef1234567890',
        subject='not copied', built_by='not copied')))
    elsewhere = tmp_path/'releases'/'g1234567'
    elsewhere.mkdir(parents=True)
    (elsewhere/'SOURCE.json').write_text(json.dumps(dict(sha='1234567890abcdef')))
    actual = _source_manifest(package, elsewhere/'.venv')
    assert actual['commit'] == 'abcdef1234567890'
    assert actual['release_id'] == 'gabcdef1'
    assert actual['source_manifest']['sha256'] == hashlib.sha256((release/'SOURCE.json').read_bytes()).hexdigest()
    checkout = tmp_path/'checkout/ldcon'; checkout.mkdir(parents=True)
    assert _source_manifest(checkout, elsewhere/'.venv') is None


def test_provenance_hash_and_commit_use_the_same_snapshot_during_replacement(tmp_path, monkeypatch):
    from ldcon.jobs import operation_history as module
    release = tmp_path/'releases'/'snapshot-test'
    package = release/'lib/ldcon'
    package.mkdir(parents=True)
    source = release/'SOURCE.json'
    first = json.dumps(dict(commit='abcdef1234567')).encode()
    source.write_bytes(first)
    original = module._read_file
    def replacing(directory, name, **kwargs):
        snapshot = original(directory, name, **kwargs)
        if directory == release and name == 'SOURCE.json':
            source.write_text(json.dumps(dict(commit='1234567abcdef')))
        return snapshot
    monkeypatch.setattr(module, '_read_file', replacing)
    actual = module._source_manifest(package, release)
    assert actual['commit'] == 'abcdef1234567'
    assert actual['source_manifest']['sha256'] == hashlib.sha256(first).hexdigest()
    assert json.loads(source.read_text())['commit'] != actual['commit']


def test_frozen_provenance_is_bounded_and_regular(tmp_path):
    from ldcon.jobs.operation_history import source_provenance
    path = tmp_path/'execution-provenance.json'
    path.write_bytes(b' ' * (1024 * 1024 + 1))
    assert source_provenance(tmp_path)['frozen_provenance'] is None
    path.unlink()
    secret = tmp_path/'outside.json'
    secret.write_text(json.dumps(dict(commit='abcdef1234567')))
    path.symlink_to(secret)
    actual = source_provenance(tmp_path)
    assert actual['frozen_provenance'] is None and actual['commit'] is None


@pytest.mark.parametrize('event', ['state', 'controller_status', 'card_start', 'agent_response'])
def test_status_labels_and_agent_claims_cannot_establish_lock(event, tmp_path):
    history = OperationHistory(tmp_path, frozen(tmp_path), wall_clock=lambda: 1000.)
    emit(history, event, dict(state='locked', status='running', **native_row()))
    assert read(tmp_path)['milestones'] == []


@pytest.mark.parametrize('change', [dict(locked=False), dict(native_pi_active=False),
    dict(native_config=dict(pid_multiplier=0)),
    dict(error_hz=2e6), dict(target_ramp_active=True), dict(frequency_hz=float('nan')),
    dict(source_acquired_unix_s=990.), dict(source_acquired_unix_s=1001.)])
def test_unconfirmed_tracking_ramping_or_stale_observation_is_not_lock_acquired(change, tmp_path):
    history = OperationHistory(tmp_path, frozen(tmp_path), wall_clock=lambda: 1000.)
    emit(history, 'trace', native_row(**change))
    assert read(tmp_path)['milestones'] == []


def test_first_verified_lock_is_published_during_continuous_job_once(tmp_path):
    inputs = frozen(tmp_path)
    history = OperationHistory(tmp_path, inputs, wall_clock=lambda: 1000.)
    emit(history, 'trace', native_row())
    first = read(tmp_path)
    assert first['lifecycle'] == 'running' and first['end'] is None
    assert len(first['milestones']) == 1
    milestone = first['milestones'][0]
    assert milestone['kind'] == 'lock_acquired_from_operating_point'
    assert milestone['evidence']['dac_code'] == 30000
    # Reopening the report index does not replay or duplicate its milestone.
    reopened = OperationHistory(tmp_path, inputs, wall_clock=lambda: 1001.)
    emit(reopened, 'trace', native_row(source_acquired_unix_s=1001.), elapsed=2.)
    assert read(tmp_path)['milestones'] == first['milestones']


@pytest.mark.parametrize('state', ['locked', 'mode_degraded', 'dither_degraded'])
def test_ecdl_owner_cycle_can_acquire_frequency_lock_with_degraded_optical_quality(tmp_path, state):
    history = OperationHistory(tmp_path, frozen(tmp_path, 'servo.ecdl_steady_lock'), wall_clock=lambda: 1000.)
    emit(history, 'ecdl_lock_cycle', dict(state=state, control_ready_passed=True,
        frequency_hz=4e14, error_hz=1., source_acquired_unix_s=1000.,
        single_mode=False, maintenance_required=True, outputs=dict(current=.8, temperature=.5, piezo=30000)))
    milestone = read(tmp_path)['milestones'][0]
    assert milestone['optical_quality_is_separate']
    assert milestone['evidence']['single_mode'] is False
    assert milestone['evidence']['outputs']['current'] == .8


def test_dlc_heartbeat_requires_a_recent_observation_not_just_lock_mode(tmp_path):
    history = OperationHistory(tmp_path, frozen(tmp_path, 'servo.dlcpro_frequency'), wall_clock=lambda: 1000.)
    row = dict(mode='lock', pi_enabled=True, fault=None, target_frequency_hz=4e14,
        observed_s=10., observation=dict(acquired_s=1., acquired_unix_s=1000., frequency_hz=4e14))
    emit(history, 'trace', row)
    assert read(tmp_path)['milestones'] == []
    row['observation']['acquired_s'] = 10.
    emit(history, 'trace', row, elapsed=2.)
    assert len(read(tmp_path)['milestones']) == 1


def test_proposals_reported_effects_and_owner_receipts_have_different_evidence(tmp_path):
    history = OperationHistory(tmp_path, frozen(tmp_path), wall_clock=lambda: 1000.)
    emit(history, 'command_requested', dict(value=.8, password='hidden'))
    emit(history, 'command_applied', dict(value=.8))
    emit(history, 'change_measure_result', dict(physical_outcome='applied',
        application=dict(status='applied', readback=dict(current=.8, auth_token='hidden'))))
    # Same source event is an idempotent report write.
    emit(history, 'command_applied', dict(value=.8))
    data = read(tmp_path)
    assert [data['activity'][key]['count'] for key in
        ('proposals', 'card_reported_effects', 'owner_confirmed_applications')] == [1, 1, 1]
    assert data['activity']['owner_confirmed_applications']['latest']['values']['readback'] == {'current': .8}
    assert 'hidden' not in (tmp_path/NAME).read_text()


@pytest.mark.parametrize('status', ['succeeded', 'failed', 'cancelled', 'inconclusive'])
def test_terminal_states_are_distinct_and_finish_is_idempotent(status, tmp_path):
    history = OperationHistory(tmp_path, frozen(tmp_path), wall_clock=lambda: 1000.)
    finish(history, status)
    expected = read(tmp_path)
    assert expected['outcome']['execution_state'] == status
    assert expected['outcome']['physical_stop'] == 'unverified'
    assert expected['end']['unix_s'] == 1000.
    finish(history, 'failed')
    assert read(tmp_path) == expected


def test_unknown_application_or_review_cannot_become_success(tmp_path):
    history = OperationHistory(tmp_path, frozen(tmp_path), wall_clock=lambda: 1000.)
    emit(history, 'change_measure_result', dict(physical_outcome='unknown'))
    (tmp_path/'device-review.json').write_text(json.dumps(dict(lease='must-not-copy', endpoint='https://secret')))
    finish(history)
    row = read(tmp_path)
    assert row['outcome']['execution_state'] == 'unknown'
    assert row['outcome']['physical_effects'] == 'unknown'
    assert row['outcome']['review_required']
    assert 'must-not-copy' not in (tmp_path/NAME).read_text()
    assert any(item['path'] == 'device-review.json' for item in row['evidence'])


def test_nonfinite_or_nested_values_do_not_break_strict_json_record(tmp_path):
    history = OperationHistory(tmp_path, frozen(tmp_path), wall_clock=lambda: 1000.)
    emit(history, 'command_requested', dict(value=float('inf'), state={'secret': 'a'},
        outputs=dict(current=float('nan'), piezo=1.)))
    value = read(tmp_path)['activity']['proposals']['latest']['values']
    assert value == {'outputs': {'piezo': 1.}}
    json.dumps(read(tmp_path), allow_nan=False)


def test_history_errors_are_optional_and_do_not_leak_exception_text(tmp_path, monkeypatch):
    inputs = frozen(tmp_path)
    def fail(*args, **kwargs):
        raise OSError('https://secret?sig=credential')
    monkeypatch.setattr(OperationHistory, '_locked', fail)
    assert optional_history(tmp_path, inputs) is None
    optional_call(SimpleNamespace(directory=tmp_path, finish=fail), 'finish', {})
    diagnostic = json.loads((tmp_path/'operation-record-error.json').read_text())
    assert diagnostic['error_type'] == 'OSError'
    assert 'secret' not in json.dumps(diagnostic)


def test_worker_records_history_failure_and_cancel_without_changing_execution_semantics(tmp_path, monkeypatch):
    worker_job(tmp_path)
    monkeypatch.setattr(interface_worker, 'open_interfaces', lambda *args: {
        'wavelength': SimpleNamespace(close=lambda: None)})
    def measure(*args, **kwargs):
        kwargs['emit']('command_requested', {'value': 1.})
        raise ValueError('simulated acquisition failure')
    monkeypatch.setattr(interface_worker, 'monitor_wavelength', measure)
    result = interface_worker.run(tmp_path)
    assert result['status'] == 'failed' and result['operation_record'] == NAME
    history = read(tmp_path)
    assert history['outcome']['error_type'] == 'ValueError'
    assert history['outcome']['execution_state'] == 'failed'
    assert any(item['path'] == 'summary.json' for item in history['evidence'])
    assert interface_worker.run(tmp_path) == result


def test_worker_optional_index_failure_does_not_fail_successful_card(tmp_path, monkeypatch):
    worker_job(tmp_path)
    opened = []
    monkeypatch.setattr(interface_worker, 'open_interfaces', lambda *args: opened.append(True) or {
        'wavelength': SimpleNamespace(close=lambda: None)})
    def measure(*args, **kwargs):
        kwargs['emit']('trace', dict(frequency_hz=4e14))
        return {'status': 'succeeded'}
    monkeypatch.setattr(interface_worker, 'monitor_wavelength', measure)
    def fail(*args, **kwargs):
        raise OSError('report write failed')
    monkeypatch.setattr(OperationHistory, 'event', fail)
    monkeypatch.setattr(OperationHistory, 'finish', fail)
    result = interface_worker.run(tmp_path)
    assert result['status'] == 'succeeded' and opened == [True]
    assert (tmp_path/'operation-record-error.json').exists()


def test_worker_prelaunch_cancel_is_not_failure_or_owner_confirmed_effect(tmp_path, monkeypatch):
    worker_job(tmp_path)
    (tmp_path/'cancel.request').touch()
    monkeypatch.setattr(interface_worker, 'open_interfaces', lambda *args: pytest.fail('must not open'))
    result = interface_worker.run(tmp_path)
    history = read(tmp_path)
    assert result['status'] == history['outcome']['execution_state'] == 'cancelled'
    assert history['outcome']['physical_effects'] == 'none'
    assert history['milestones'] == []


def test_older_record_is_not_replayed_after_recent_deduplication_window(tmp_path):
    history = OperationHistory(tmp_path, frozen(tmp_path), wall_clock=lambda: 1000.)
    for index in range(140):
        emit(history, 'command_applied', dict(value=index), elapsed=index+1.)
    emit(history, 'command_applied', dict(value=0), elapsed=1.)
    row = read(tmp_path)
    assert row['activity']['card_reported_effects']['count'] == 140
    assert row['activity']['card_reported_effects']['latest']['values']['value'] == 139
    assert len(row['recent_event_ids']) == 128


def test_conflicting_history_writer_is_nonblocking_and_cannot_replace_records(tmp_path):
    import fcntl
    history = OperationHistory(tmp_path, frozen(tmp_path), wall_clock=lambda: 1000.)
    with (tmp_path/'.operation-record.lock').open('r+') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        optional_call(history, 'event', 'command_applied', {'value': 1.}, elapsed_s=1., run_id='run-a')
    assert read(tmp_path)['activity']['card_reported_effects']['count'] == 0
    assert (tmp_path/'operation-record-error.json').exists()
    emit(history, 'command_applied', dict(value=1.))
    assert read(tmp_path)['activity']['card_reported_effects']['count'] == 1


def test_manifest_evidence_does_not_follow_symlinks(tmp_path):
    inputs = frozen(tmp_path)
    (tmp_path/'secret').write_text('not evidence')
    (tmp_path/'report.pdf').symlink_to(tmp_path/'secret')
    history = OperationHistory(tmp_path, inputs, wall_clock=lambda: 1000.)
    finish(history)
    assert not any(item['path'] == 'report.pdf' for item in read(tmp_path)['evidence'])
