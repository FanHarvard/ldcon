import json
from types import SimpleNamespace

import pytest

from ldcon.jobs.adaptive_window_session import AdaptiveExperimentManager, AdaptiveWindowSession
from ldcon.core.contracts import DeviceError
from experiment_core.errors import UncertainWrite


class Lock:
    def __init__(self):
        self.t = 100.
        self.session = SimpleNamespace(lease='parent-lease')
        self.owner_context = dict(system_id='laser', generation='owner-generation', policy_revision='policy-v1')
        self.current = .77
        self.uncertain_output = False
        self.moves = []
        self.active = False
        self.stale = False
        self.fail = False
        self.axis = SimpleNamespace(low=.6, high=1.0, max_step=.01, resolution=.0001, settle_s=.5, max_slew_per_s=1., update_interval_s=.1)

    def recovery_ports(self):
        return {'current': SimpleNamespace(policy=self.axis), 'temperature': SimpleNamespace(policy=self.axis)}

    def response_info(self, *, include_patterns=False):
        return dict(owner_context=dict(self.owner_context), native_pi_active=True, outputs=dict(current=self.current, temperature=.5, piezo=30000),
                    observation=dict(mode_id='branch', single_mode=None,
                        frequency=dict(acquired_unix_s=self.t, acquired_s=self.t, frequency_hz=710e12),
                        fringe_analysis=dict(raw_patterns=[dict(interferometer='1', pixels=[1., 3., 8., 2.]),
                                                           dict(interferometer='2', pixels=[4., 5., 9., 6.])]),
                        metrics=dict(side_peak_height_max=.1, main_fwhm_fraction=.03)))

    def response_begin(self):
        self.active = True
        return dict(started=True, minimum_dwell_s=35.5)

    def response_end(self):
        self.active = False
        return dict(ended=True, native_pi_active=True)

    def response_current(self, value):
        self.moves.append(value)
        if self.fail:
            self.uncertain_output = True
            raise DeviceError('unknown write')
        self.current = value
        return dict(applied=True, value=value)

    def response_dither_info(self):
        return dict(dither_response_valid=True, dither_response=.001,
                    dither_source_start_unix_s=0 if self.stale else self.t-32,
                    dither_source_unix_s=self.t, dither_sample_count=500)


def make_session(tmp_path):
    lock = Lock()
    session = AdaptiveWindowSession(tmp_path, lock, half_span_v=.0084,
                                    clock=lambda: lock.t, wall_clock=lambda: lock.t)
    return lock, session


def command(session, identifier, action, **kwargs):
    path = session.directory / 'inbox' / (identifier + '.json')
    path.write_text(json.dumps(dict(id=identifier, action=action, based_on=session.decision_revision, **kwargs)))
    session.poll()


def result(session, identifier):
    return json.loads((session.directory / 'results' / (identifier+'.json')).read_text())


def test_fresh_point_and_duplicate(tmp_path):
    lock, session = make_session(tmp_path)
    command(session, 'one', 'move_and_measure', value=.771)
    for _ in range(32):
        lock.t += 1
        session.poll()
    assert not (session.directory / 'results/one.json').exists()
    lock.t += 4
    session.poll()
    assert result(session, 'one')['state'] == 'measured'
    assert len(result(session, 'one')['optical_frames']) == 5
    assert lock.active
    duplicate = json.loads((session.directory / 'claimed/one.json').read_text())
    (session.directory / 'inbox/one.json').write_text(json.dumps(duplicate))
    session.poll()
    assert len(lock.moves) == 1
    command(session, 'done', 'finish')
    assert not lock.active
    assert result(session, 'done')['disposition'] == 'retained_last_output'


def test_outside_span_never_moves(tmp_path):
    lock, session = make_session(tmp_path)
    command(session, 'bad', 'move_and_measure', value=.78)
    assert result(session, 'bad')['state'] == 'rejected'
    assert not lock.moves and not lock.active


def test_stale_fit_times_out_without_disabling_pi(tmp_path):
    lock, session = make_session(tmp_path)
    lock.stale = True
    command(session, 'stale', 'measure_here')
    for _ in range(182):
        lock.t += 1
        session.poll()
    assert result(session, 'stale')['state'] == 'point_timeout'
    assert not lock.active and not lock.moves


def test_unknown_move_is_not_replayed_after_restart(tmp_path):
    lock, session = make_session(tmp_path)
    lock.fail = True
    with pytest.raises(DeviceError):
        command(session, 'unknown', 'move_and_measure', value=.771)
    again = AdaptiveWindowSession(tmp_path, lock, half_span_v=.0084)
    again.poll()
    assert again.blocked and len(lock.moves) == 1


def test_recovery_releases_only_child(tmp_path):
    lock, session = make_session(tmp_path)
    command(session, 'point', 'measure_here')
    session.before_recovery()
    assert session.blocked and not lock.active
    assert result(session, 'point')['state'] == 'branch_lost'


def test_cancel_interrupts_pending_acquisition(tmp_path):
    lock, session = make_session(tmp_path)
    command(session, 'point', 'measure_here')
    command(session, 'stop', 'cancel')
    assert result(session, 'point')['state'] == 'cancelled'
    assert result(session, 'stop')['state'] == 'cancelled'
    assert not lock.active and session.blocked


def test_decision_deadline_releases_current_control(tmp_path):
    lock, session = make_session(tmp_path)
    command(session, 'point', 'measure_here')
    for _ in range(36):
        lock.t += 1
        session.poll()
    lock.t += 301
    session.poll()
    assert not lock.active and session.blocked


def test_duplicate_optical_acquisition_does_not_count_five_times(tmp_path):
    lock, session = make_session(tmp_path)
    command(session, 'point', 'measure_here')
    lock.t += 40
    for _ in range(10):
        session.poll()
    assert len(session.frames) == 1
    assert not (session.directory / 'results/point.json').exists()


def test_explicit_admission_not_instrument_constant(tmp_path):
    lock = Lock()
    session = AdaptiveWindowSession(tmp_path, lock, half_span_v=.02)
    assert session.half_span == .02


def test_partial_owner_move_is_recorded_not_replayed(tmp_path):
    lock, session = make_session(tmp_path)
    def partial(value):
        lock.moves.append(value)
        lock.current += .0001
        return dict(applied=True, value=lock.current)
    lock.response_current = partial
    command(session, 'partial', 'move_and_measure', value=.771)
    assert result(session, 'partial')['state'] == 'partial_move_requires_replan'
    assert result(session, 'partial')['receipt']['value'] == pytest.approx(.7701)
    session.poll()
    assert len(lock.moves) == 1 and not lock.active


def test_selector_cannot_switch_active_child(tmp_path):
    lock = Lock()
    host = tmp_path / "host"
    host.mkdir()
    manager = AdaptiveExperimentManager(tmp_path / "job", lock, control_root=host, laser_id="laser", clock=lambda: lock.t, wall_clock=lambda: lock.t)
    selector = manager.directory / 'adaptive-experiment.json'
    selector.write_text(json.dumps(dict(id='astra', half_span_v=.0084)))
    manager.poll()
    first = manager.session
    command(first, 'point', 'measure_here')
    selector.write_text(json.dumps(dict(id='next', half_span_v=.0084)))
    manager.poll()
    assert manager.session is first
    manager.before_recovery()
    manager.poll()
    assert manager.session is not first
    assert manager.selection['id'] == 'next'


def test_owner_dwell_is_honored_even_with_early_valid_fit(tmp_path):
    lock, session = make_session(tmp_path)
    command(session, 'point', 'measure_here')
    for _ in range(34):
        lock.t += 1
        session.poll()
    assert not (session.directory / 'results/point.json').exists()
    lock.t += 2
    session.poll()
    assert result(session, 'point')['state'] == 'measured'


def test_deadline_runs_without_successful_parent_reads(tmp_path):
    lock, session = make_session(tmp_path)
    command(session, 'point', 'measure_here')
    lock.t += 181
    session.check_deadlines()
    assert result(session, 'point')['state'] == 'point_timeout'
    assert not lock.active


def test_malformed_selector_does_not_escape_into_parent(tmp_path):
    lock = Lock()
    host = tmp_path / "host"
    host.mkdir()
    events = []
    manager = AdaptiveExperimentManager(tmp_path / "job", lock, control_root=host, laser_id="laser", emit=lambda *args: events.append(args))
    selector = manager.directory / 'adaptive-experiment.json'
    selector.write_text('[]')
    manager.poll()
    manager.poll()
    assert len(events) == 1
    assert events[0][0] == 'adaptive_selector_rejected'
    assert manager.session is None


def test_effect_revision_counts_unknown_attempt_before_exception(tmp_path):
    lock, session = make_session(tmp_path)
    lock.fail = True
    with pytest.raises(DeviceError):
        command(session, 'unknown', 'move_and_measure', value=.771)
    assert session.effect_revision == 2  # begin plus attempted current write
    session.close()
    assert session.effect_revision == 2  # Unknown effects never dispatch cleanup


def test_manager_effect_revision_has_session_identity(tmp_path):
    lock = Lock()
    host = tmp_path / "host"
    host.mkdir()
    manager = AdaptiveExperimentManager(tmp_path / "job", lock, control_root=host, laser_id="laser")
    assert manager.effect_revision == (None, 0)
    (manager.directory / 'adaptive-experiment.json').write_text(json.dumps(dict(id='first', half_span_v=.0084)))
    manager.poll()
    identity, revision = manager.effect_revision
    assert identity.endswith('adaptive-experiments/first/adaptive-window')
    assert revision == 0


def test_preselected_handoff_suppresses_intervening_slow_correction(tmp_path):
    lock = Lock()
    host = tmp_path / "host"
    host.mkdir()
    manager = AdaptiveExperimentManager(tmp_path / "job", lock, control_root=host, laser_id="laser", clock=lambda: lock.t,
                                        wall_clock=lambda: lock.t)
    selector = manager.directory / 'adaptive-experiment.json'
    selector.write_text(json.dumps(dict(id='reset', half_span_v=.02)))
    manager.poll()
    old = manager.session
    command(old, 'measure', 'measure_here')
    for _ in range(36):
        lock.t += 1
        manager.poll()
    assert result(old, 'measure')['state'] == 'measured'
    next_inbox = manager.directory / 'adaptive-experiments/next/adaptive-window/inbox'
    next_inbox.mkdir(parents=True)
    (next_inbox / 'initial.json').write_text(json.dumps(dict(id='initial', action='measure_here', based_on='not-yet-admitted')))
    selector.write_text(json.dumps(dict(id='next', half_span_v=.02)))
    (old.directory / 'inbox/finish.json').write_text(json.dumps(dict(id='finish', action='finish', based_on=old.decision_revision)))
    for _ in range(2):
        before = manager.effect_revision
        manager.poll()
        # This is the parent worker's slow-action exclusion condition.
        assert manager.active or manager.effect_revision != before
    assert manager.selection['id'] == 'next'
    assert not manager.active
    command(manager.session, 'fresh', 'measure_here')
    assert manager.active and lock.active
    assert manager.session.origin == .77
    assert lock.moves == []


def test_measure_here_does_not_write_float_rounding_difference(tmp_path):
    lock, session = make_session(tmp_path)
    lock.current = .7705
    command(session, 'baseline', 'measure_here')
    assert lock.moves == []
    assert session.target == lock.current
    for _ in range(36):
        lock.t += 1
        session.poll()
    assert result(session, 'baseline')['state'] == 'measured'


def test_core_intent_precedes_owner_effect_and_preserves_parent_identity(tmp_path):
    lock, session = make_session(tmp_path)
    original = lock.response_current
    def inspect_intent(value):
        rows = session.journal.list('laser')
        current = [r for r in rows if r['request']['operation_id'] == 'ldcon.adaptive.current']
        assert len(current) == 1 and current[0]['state'] == 'running'
        assert current[0]['request']['generation'] == 'owner-generation'
        assert current[0]['request']['lease_id'] != lock.session.lease
        return original(value)
    lock.response_current = inspect_intent
    command(session, 'point', 'move_and_measure', value=.771)
    assert lock.moves == [.771]
    session.close()
    assert lock.session.lease == 'parent-lease'
    assert session.journal.get('session')['result']['parent_lease_released'] is False


def test_late_decision_cannot_move_after_new_measurement(tmp_path):
    lock, session = make_session(tmp_path)
    old = session.decision_revision
    command(session, 'baseline', 'measure_here')
    for _ in range(36):
        lock.t += 1
        session.poll()
    path = session.directory / 'inbox/late.json'
    path.write_text(json.dumps(dict(id='late', action='move_and_measure', value=.771, based_on=old)))
    session.poll()
    assert result(session, 'late')['state'] == 'stale_decision'
    assert lock.moves == [] and lock.active


def test_full_simultaneous_raw_arrays_and_fresh_fit_are_retained(tmp_path):
    lock, session = make_session(tmp_path)
    command(session, 'baseline', 'measure_here')
    for _ in range(36):
        lock.t += 1
        session.poll()
    evidence = result(session, 'baseline')
    assert evidence['dither']['dither_source_start_unix_s'] > evidence['post_settle_unix_s']
    for frame in evidence['optical_frames']:
        raw = frame['observation']['fringe_analysis']['raw_patterns']
        assert raw == [dict(interferometer='1', pixels=[1., 3., 8., 2.]),
                       dict(interferometer='2', pixels=[4., 5., 9., 6.])]


def test_changed_owner_or_policy_prevents_next_move(tmp_path):
    lock, session = make_session(tmp_path)
    command(session, 'baseline', 'measure_here')
    for _ in range(36):
        lock.t += 1
        session.poll()
    lock.owner_context['policy_revision'] = 'policy-v2'
    command(session, 'new-policy', 'move_and_measure', value=.771)
    assert result(session, 'new-policy')['state'] == 'rejected'
    assert lock.moves == []


def test_finite_point_budget_and_session_deadline_do_not_repeat_moves(tmp_path):
    lock = Lock()
    session = AdaptiveWindowSession(tmp_path, lock, half_span_v=.01,
        max_points=1, timeout_s=60., clock=lambda: lock.t, wall_clock=lambda: lock.t)
    command(session, 'point', 'move_and_measure', value=.771)
    for _ in range(36):
        lock.t += 1
        session.poll()
    command(session, 'second', 'move_and_measure', value=.772)
    assert result(session, 'second')['state'] == 'point_budget_exhausted'
    assert lock.moves == [.771] and not lock.active
    lock.t += 100
    session.poll()
    assert lock.moves == [.771]


def test_unknown_cleanup_is_durable_and_never_retried(tmp_path):
    lock, session = make_session(tmp_path)
    command(session, 'point', 'measure_here')
    calls = []
    def unknown():
        calls.append('end')
        raise OSError('reply lost')
    lock.response_end = unknown
    with pytest.raises(UncertainWrite, match='Cleanup requires'):
        session.close()
    session.close()
    assert calls == ['end']
    assert session.write_unknown
    assert any(r['state'] == 'uncertain' for r in session.journal.list('laser'))


def test_real_stationary_loop_keeps_heartbeats_and_drops_pre_child_slow_proposals(tmp_path, monkeypatch):
    from ldcon.jobs.ecdl_steady import parameters, run_steady_lock
    lock = Lock()
    monkeypatch.setattr('ldcon.jobs.adaptive_window_session.AdaptiveExperimentManager',
        lambda directory, port, **kwargs: AdaptiveExperimentManager(
            directory, port, wall_clock=lambda: lock.t, **kwargs))
    heartbeats, adjusted, held = [], [], []
    lock.check_ready = lambda strategy: None
    lock.arm = lambda *args: None
    lock.heartbeat = lambda: heartbeats.append(lock.t)
    lock.hold = lambda reason: held.append(reason) or {'held': True}
    lock.adjust = lambda action: adjusted.append(lock.t) or {'applied': True}
    lock.read_cycle = lambda: dict(state='locked', frequency_hz=710e12, error_hz=0.,
        single_mode=True, mode_id='branch', outputs={'current': lock.current},
        actions=[{'axis': 'temperature', 'reason': 'old plan'}])
    host = tmp_path / 'host'
    host.mkdir()
    monkeypatch.setenv('LDCON_ADAPTIVE_ROOT', str(host))
    job = tmp_path / 'deployment/jobs/laser/job'
    job.mkdir(parents=True)
    control = host / 'laser/job'
    control.mkdir(parents=True)
    (control / 'adaptive-experiment.json').write_text(json.dumps(
        dict(id='trial', half_span_v=.0084, require_dither=True)))
    directory = control / 'adaptive-experiments/trial/adaptive-window'
    submitted = set()
    def sleep(seconds):
        lock.t += seconds
        status = json.loads((directory / 'status.json').read_text())
        if 'point' not in submitted:
            submitted.add('point')
            (directory / 'inbox/point.json').write_text(json.dumps(dict(
                id='point', action='move_and_measure', value=.771, based_on=status['decision_revision'])))
        elif (directory / 'results/point.json').exists() and 'finish' not in submitted:
            submitted.add('finish')
            (directory / 'inbox/finish.json').write_text(json.dumps(dict(
                id='finish', action='finish', based_on=status['decision_revision'])))
    p = parameters({'parameters': {}}, dict(target_frequency_hz=710e12,
        tolerance_hz=1e6, duration_s=45.))
    run_steady_lock(lock, p, clock=lambda: lock.t, sleep=sleep,
                    cancel=lambda: False, emit=lambda *args: None,
                    directory=job, laser={'id': 'laser'})
    evidence = json.loads((directory / 'results/point.json').read_text())
    finish = json.loads((directory / 'results/finish.json').read_text())
    assert len(heartbeats) == 45 and lock.moves == [.771]
    assert evidence['dither']['dither_response_valid']
    # The end acknowledgement must fence the plan obtained earlier in that
    # SAME parent cycle, even though the child is no longer active afterwards.
    assert not [t for t in adjusted if 101. <= t <= finish['recorded_unix_s']]
    assert held == ['card exit'] and lock.session.lease == 'parent-lease'


def test_live_attachment_is_disabled_without_explicit_host_root(tmp_path, monkeypatch):
    monkeypatch.delenv('LDCON_ADAPTIVE_ROOT', raising=False)
    (tmp_path / 'adaptive-experiment.json').write_text(json.dumps(dict(id='untrusted', half_span_v=.01)))
    manager = AdaptiveExperimentManager(tmp_path, Lock(), laser_id='laser')
    manager.poll()
    assert not manager.enabled and manager.session is None
    assert not (tmp_path / 'adaptive-experiments').exists()


def test_host_root_cannot_be_inside_any_part_of_deployment(tmp_path, monkeypatch):
    deployment = tmp_path / 'deployment'
    job = deployment / 'jobs/laser/job'
    job.mkdir(parents=True)
    bad = deployment / 'agent-writable'
    bad.mkdir()
    monkeypatch.setenv('LDCON_ADAPTIVE_ROOT', str(bad))
    manager = AdaptiveExperimentManager(job, Lock(), laser_id='laser')
    manager.poll()
    assert not manager.enabled and 'deployment tree' in manager.disabled_reason
    assert list(bad.iterdir()) == []


def test_host_namespace_ignores_job_side_selector_and_checks_laser(tmp_path, monkeypatch):
    job = tmp_path / 'deployment/jobs/laser/job'
    job.mkdir(parents=True)
    host = tmp_path / 'host'
    host.mkdir()
    monkeypatch.setenv('LDCON_ADAPTIVE_ROOT', str(host))
    (job / 'adaptive-experiment.json').write_text(json.dumps(dict(id='untrusted', half_span_v=100.)))
    lock = Lock()
    manager = AdaptiveExperimentManager(job, lock, laser_id='laser')
    manager.poll()
    assert manager.enabled and manager.directory == host / 'laser/job'
    assert manager.session is None
    (manager.directory / 'adaptive-experiment.json').write_text(json.dumps(dict(id='trusted', half_span_v=.01)))
    manager.poll()
    lock.owner_context['system_id'] = 'another-laser'
    command(manager.session, 'point', 'move_and_measure', value=.771)
    assert result(manager.session, 'point')['state'] == 'rejected'
    assert not lock.moves and not lock.active
