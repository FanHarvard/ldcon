"""study.target_response shift search (target-ff design 2026-10-27): planner order, round judgement, trial caps saved
through the configuration API, promotion / restoration and the readiness restore. Fakes only: no owner, no hardware."""
import copy
import json
from pathlib import Path

import pytest
import yaml

from ldcon.core.contracts import DeviceError
from ldcon.core.records import read_json
from ldcon.jobs import target_response as study
from ldcon.jobs import target_response_shift as shift
from ldcon.jobs.target_response_shift import restore_pending_ceiling
from .test_target_response import history

F0 = 300e12
KW = dict(requested_hz=400e6, min_hz=100e6, max_rounds=6, resolution_hz=50e6)


def rounds(*items):
    return [dict(shift_hz=s, verdict=v) for s, v in items]


# ------------------------------------------------------------------------------------------ planner
def test_plan_probes_the_saved_cap_then_the_request_then_bisects_the_bracket():
    assert shift.plan([], **KW)['shift_hz'] == 100e6
    assert shift.plan(rounds((100e6, 'pass')), **KW)['shift_hz'] == 400e6
    assert shift.plan(rounds((100e6, 'pass'), (400e6, 'fail')), **KW)['shift_hz'] == 250e6
    assert shift.plan(rounds((100e6, 'pass'), (400e6, 'fail'), (250e6, 'pass')), **KW)['shift_hz'] == 325e6
    r = rounds((100e6, 'pass'), (400e6, 'fail'), (250e6, 'pass'), (325e6, 'fail'))
    assert shift.plan(r, **KW)['shift_hz'] == 287.5e6
    r.append(dict(shift_hz=287.5e6, verdict='fail'))
    choice = shift.plan(r, **KW)
    assert choice['action'] == 'stop' and choice['best_passing_hz'] == 250e6 and 'resolution' in choice['reason']


def test_plan_stops_on_request_pass_probe_failure_fixed_mode_budget_and_hazard():
    assert shift.plan(rounds((100e6, 'pass'), (400e6, 'pass')), **KW)['action'] == 'stop'
    probe = shift.plan(rounds((100e6, 'fail')), **KW)
    assert probe['action'] == 'stop' and probe['best_passing_hz'] is None
    fixed = shift.plan(rounds((100e6, 'pass'), (400e6, 'fail')), **dict(KW, mode='fixed'))
    assert fixed['action'] == 'stop' and fixed['best_passing_hz'] == 100e6
    budget = shift.plan(rounds((100e6, 'pass'), (400e6, 'fail')), **dict(KW, max_rounds=2))
    assert budget['action'] == 'stop' and budget['best_passing_hz'] == 100e6
    assert shift.plan(rounds((100e6, 'hazard')), **KW)['action'] == 'stop'
    # a request below the saved cap is probed at the request itself and stands when it passes
    small = shift.plan([], **dict(KW, requested_hz=50e6))
    assert small['shift_hz'] == 50e6
    assert shift.plan(rounds((50e6, 'pass')), **dict(KW, requested_hz=50e6))['action'] == 'stop'


def test_plan_repeats_invalid_evidence_once_and_rejects_bad_inputs():
    repeat = shift.plan(rounds((100e6, 'pass'), (400e6, 'invalid')), **KW)
    assert repeat['action'] == 'propose_trial' and repeat['shift_hz'] == 400e6 and 'Repeat' in repeat['reason']
    stop = shift.plan(rounds((100e6, 'pass'), (400e6, 'invalid'), (400e6, 'invalid')), **KW)
    assert stop['action'] == 'stop' and stop['best_passing_hz'] == 100e6
    for bad in (dict(KW, requested_hz=0.), dict(KW, max_rounds=0), dict(KW, resolution_hz=-1.)):
        with pytest.raises(ValueError):
            shift.plan([], **bad)


# ------------------------------------------------------------------------------------------ judge
def leg(offset, *, status='settled', settle=.2, mode='m1', integrity=None, degraded=False):
    return dict(offset_hz=offset, analysis=dict(status=status, settling_from_request_s=settle, reason='r'),
                history=dict(integrity_errors=integrity or []),
                command=dict(status='succeeded', locked=True, verify=dict(quality_samples=[dict(mode_id=mode, degraded=degraded, single_mode=True)])))


def monitor(**changes):
    return dict(dict(mode_id_before='m1', max_integral_fraction=.1, lost=False), **changes)


P = dict(settling_goal_s=.3, abort_integral_fraction_max=.95)


def test_judge_pass_and_each_failure_reason():
    ok = shift.judge([leg(400e6), leg(0.)], P, owner_type='native', monitors=[monitor(), monitor()])
    assert ok['verdict'] == 'pass' and ok['worst_settle_s'] == .2 and not ok['quality']
    slow = shift.judge([leg(400e6, settle=.5), leg(0.)], P, owner_type='native', monitors=[monitor(), monitor()])
    assert slow['verdict'] == 'fail' and 'goal' in slow['reasons'][0]
    unsettled = shift.judge([leg(400e6), leg(0., status='not_settled')], P, owner_type='native', monitors=[monitor(), monitor()])
    assert unsettled['verdict'] == 'fail' and 'not settled' in unsettled['reasons'][0]
    hop = shift.judge([leg(400e6, mode='m2'), leg(0.)], P, owner_type='native', monitors=[monitor(), monitor()])
    assert hop['verdict'] == 'fail' and 'branch changed' in hop['reasons'][0]
    windup = shift.judge([leg(400e6), leg(0.)], P, owner_type='native', monitors=[monitor(max_integral_fraction=.97), monitor()])
    assert windup['verdict'] == 'fail' and 'integral fraction' in windup['reasons'][0]
    # the integral criterion is sr1092's; the ECDL criterion is the PZT never reported lost
    assert shift.judge([leg(400e6), leg(0.)], P, owner_type='ecdl.rig.owner', monitors=[monitor(max_integral_fraction=.97), monitor()])['verdict'] == 'pass'
    lost = shift.judge([leg(400e6), leg(0.)], P, owner_type='ecdl.rig.owner', monitors=[monitor(lost=True), monitor()])
    assert lost['verdict'] == 'fail' and 'lost' in lost['reasons'][0]
    # quality is reported, never judged (rule K)
    flagged = shift.judge([leg(400e6, degraded=True), leg(0.)], P, owner_type='native', monitors=[monitor(), monitor()])
    assert flagged['verdict'] == 'pass' and flagged['quality'] and 'report only' in flagged['quality'][0]
    assert shift.judge([leg(400e6), leg(0., integrity=['gap'])], P, owner_type='native', monitors=[monitor(), monitor()])['verdict'] == 'invalid'
    assert shift.judge([leg(400e6), leg(0., status='inconclusive')], P, owner_type='native', monitors=[monitor(), monitor()])['verdict'] == 'invalid'
    unlocked = [leg(400e6), leg(0.)]
    unlocked[1]['command']['locked'] = False
    assert shift.judge(unlocked, P, owner_type='native', monitors=[monitor(), monitor()])['verdict'] == 'hazard'


# ------------------------------------------------------------------------------------------ the study with fakes
class Clock:
    def __init__(self): self.t = 1000.
    def __call__(self): return self.t
    def sleep(self, s): self.t += s


def laser_document():
    return dict(id='test', allow_output=True,
                target=dict(frequency_hz=F0, bounds_hz=[F0-1e9, F0+1e9], max_change_hz=100e6),
                interfaces=dict(native_current=dict(device='native')),
                devices=dict(native=dict(type='esp32servocon.native_current.owner', connection={}, absolute_conditions={},
                    control_settings=dict(native_current=dict(
                        calibration=dict(wavelength_nm_per_a=4., current_a_per_v=.15),
                        dac=dict(bits=16, min_voltage=-2.5, max_voltage=2.5),
                        native=dict(target_ramp_step_hz=100e6, target_feedforward_scale=1.))))))


class API:
    """Configuration API + the lock worker's reload receipts; the cap is enforced like lock_target.check_change."""
    def __init__(self, clock, lock_dir):
        self.clock, self.lock_dir = clock, lock_dir
        self.revision = 0
        self.doc = laser_document()
        self.writes = []
        self.last_jump = 0.
        self.last_write_t = -1e9
        self.mode_id = 'm1'
        self.refuse_above_hz = None

    def _request(self, *_):
        return {'jobs': [dict(id='parent', status='running', set_id='test', cards=[{'operation': 'servo.native_current'}])]}

    def get_configuration(self, *_):
        return dict(revision=str(self.revision), document=copy.deepcopy(self.doc), yaml=yaml.safe_dump(self.doc))

    def caps(self):
        return (self.doc['target']['max_change_hz'],
                self.doc['devices']['native']['control_settings']['native_current']['native']['target_ramp_step_hz'])

    def put_configuration(self, kind, name, text, expected_revision, **kwargs):
        assert expected_revision == str(self.revision)
        before = self.revision
        new = yaml.safe_load(text)
        jump = new['target']['frequency_hz'] - self.doc['target']['frequency_hz']
        assert abs(jump) <= min(self.caps()) * (1 + 1e-9), 'a step above the saved cap was requested'
        self.doc = new
        self.revision += 1
        self.writes.append(dict(t=self.clock(), target=self.doc['target']['frequency_hz'], caps=self.caps(), actor=kwargs.get('actor')))
        if jump:
            self.last_jump, self.last_write_t = jump, self.clock()
        (self.lock_dir / ('laser-reload-result-r%d.json' % self.revision)).write_text(json.dumps(dict(
            request_id='r%d' % self.revision, revision_before=str(before), revision_after=str(self.revision),
            status='applied', applied=['target.frequency_hz'], updated_unix_s=self.clock())))
        return dict(revision=str(self.revision))


@pytest.fixture
def rig(tmp_path, monkeypatch):
    from ldcon.jobs import operating_point
    directory = tmp_path / 'jobs/test/study'
    directory.mkdir(parents=True)
    lock_dir = directory.parent / 'parent'
    lock_dir.mkdir()
    (directory / 'progress.json').write_text(json.dumps(dict(status='running')))
    clock = Clock()
    api = API(clock, lock_dir)
    plant = dict(pass_limit_hz=250e6, slow_s=2.)
    monkeypatch.setattr(operating_point, 'find_running_lock', lambda *_: (lock_dir, {'id': 'parent'}, {}))

    def runner(directory, laser, card, p, **kw):
        gateway = kw['api']
        old = gateway.get_configuration('lasers', 'test')
        doc = old['document']
        jump = p['target_frequency_hz'] - doc['target']['frequency_hz']
        if api.refuse_above_hz is not None and abs(jump) > api.refuse_above_hz:
            raise DeviceError('owner refused the target change (simulated)')
        doc['target']['frequency_hz'] = p['target_frequency_hz']
        reply = gateway.put_configuration('lasers', 'test', yaml.safe_dump(doc), old['revision'])
        clock.sleep(.2)
        kw['cancel']()
        return dict(locked=True, status='succeeded', revision=reply['revision'],
                    verify=dict(quality_samples=[dict(mode_id=api.mode_id, single_mode=True, degraded=False)]))

    class History:
        def read(self, start, end):
            slow = abs(api.last_jump) > plant['pass_limit_hz']
            return history(start, end, target=api.doc['target']['frequency_hz'],
                           error=lambda t: 25e6 if slow and t - api.last_write_t < plant['slow_s'] else 0.)

    kwargs = dict(clock=clock, sleep=clock.sleep, cancel=lambda: False, emit=lambda *a: None, api=api, history=History(),
                  wall_clock=clock, target_runner=runner, control_reader=lambda laser, ctx: dict(status='unavailable'))
    return directory, clock, api, plant, kwargs


def params(**extra):
    p = dict(shift_hz=400e6, shift_search=dict(resolution_hz=50e6), baseline_s=.1, observation_s=1., evaluation_window_s=.2,
             check_interval_s=.05, confirmation_s=.4, max_gap_s=.09, minimum_samples=3, expected_settle_s=.3,
             settling_goal_s=.3, data_wait_s=.5, adoption_mode='auto_within_approved')
    p.update(extra)
    return study.parameters({'parameters': p}, {})


def run(rig, **extra):
    directory, clock, api, plant, kwargs = rig
    return study.run_card(directory, copy.deepcopy(api.doc), {'id': 'target-response'}, params(**extra), **kwargs)


def test_parameters_validate_the_shift_keys():
    assert params()['shift_search'] == dict(mode='bisect', min_hz=None, max_rounds=6, resolution_hz=50e6)
    for bad in (dict(offsets_hz=[20e6, 0.]), dict(shift_hz=-1.), dict(shift_search=dict(mode='grid')),
                dict(optimization_goal='settling_and_feedforward'), dict(abort_integral_fraction_max=0.),
                dict(shift_search=dict(max_rounds=20))):
        with pytest.raises(ValueError):
            params(**bad)
    with pytest.raises(ValueError, match='needs shift_hz'):
        params(shift_hz=None, shift_search=dict(mode='fixed'))


def test_bisection_promotes_the_largest_passing_shift_with_trial_then_adopted_saves(rig):
    directory, clock, api, plant, _ = rig
    plant['pass_limit_hz'] = 250e6
    result = run(rig)
    block = result['shift']
    assert [(r['shift_hz'], r['verdict']) for r in block['rounds']] == [
        (100e6, 'pass'), (400e6, 'fail'), (250e6, 'pass'), (325e6, 'fail'), (287.5e6, 'fail')]
    assert result['status'] == 'succeeded' and result['schema'] == 'ldcon.target-response/2'
    assert block['best_passing_hz'] == 250e6 and block['adopted_hz'] == 250e6 and block['shortfall_hz'] == 150e6
    assert block['adoption']['status'] == 'applied' and block['adoption']['basis'] == 'adopted'
    assert api.caps() == (250e6, 250e6)
    # the trial cap was saved BEFORE the 400 MHz round and the adopted value at the end; every save is audited
    saves = [w for w in api.writes if w['actor']]
    assert [w['actor'].split(':')[-1] for w in saves] == ['trial', 'adopted']
    assert saves[0]['caps'] == (400e6, 400e6) and saves[1]['caps'] == (250e6, 250e6)
    assert [row['basis'] for row in block['audit']] == ['trial', 'adopted']
    assert all(row['study_id'] == directory.name and row['status'] == 'adopted_by_lock' for row in block['audit'])
    ceiling = read_json(directory / shift.CEILING_FILE)
    assert ceiling['status'] == 'adopted' and ceiling['pending_write'] is False
    assert read_json(directory / shift.AUDIT_FILE)['rows'] == block['audit']
    assert result['restoration'] == 'starting_target_confirmed' and api.doc['target']['frequency_hz'] == F0
    page = (directory / 'target-response.html').read_text()
    assert 'Per-step shift search' in page and 'adopted: 250.0 MHz' in page
    assert block['adopted_parameters']['settling_goal_s'] == .3


def test_no_passing_round_adopts_nothing_and_leaves_the_saved_caps(rig):
    directory, clock, api, plant, _ = rig
    plant['pass_limit_hz'] = 50e6                 # even the probe at the saved cap fails
    result = run(rig)
    block = result['shift']
    assert [r['verdict'] for r in block['rounds']] == ['fail']
    assert result['status'] == 'inconclusive' and block['adoption']['status'] == 'no_success'
    assert api.caps() == (100e6, 100e6) and not [w for w in api.writes if w['actor']]
    assert read_json(directory / shift.CEILING_FILE)['status'] == 'restored'


def test_report_only_restores_the_trial_cap_and_reports_the_candidate(rig):
    directory, clock, api, plant, _ = rig
    plant['pass_limit_hz'] = 1e9
    result = run(rig, adoption_mode='report_only')
    block = result['shift']
    assert [(r['shift_hz'], r['verdict']) for r in block['rounds']] == [(100e6, 'pass'), (400e6, 'pass')]
    assert block['adoption'] == dict(status='proposal_only', basis='restored', candidate_hz=400e6, mode='report_only')
    assert block['adopted_hz'] is None and api.caps() == (100e6, 100e6)
    assert [w['actor'].split(':')[-1] for w in api.writes if w['actor']] == ['trial', 'restored']
    assert read_json(directory / shift.CEILING_FILE)['status'] == 'restored'


def test_request_within_the_saved_cap_needs_no_trial_save(rig):
    directory, clock, api, plant, _ = rig
    result = run(rig, shift_hz=80e6)
    block = result['shift']
    assert [(r['shift_hz'], r['verdict']) for r in block['rounds']] == [(80e6, 'pass')]
    assert block['adoption']['status'] == 'applied' and api.caps() == (80e6, 80e6)
    assert [w['actor'].split(':')[-1] for w in api.writes if w['actor']] == ['adopted']


def test_interrupted_study_restores_the_trial_cap_itself(rig):
    directory, clock, api, plant, _ = rig
    api.refuse_above_hz = 300e6                   # the 400 MHz round's first leg fails (owner refusal)
    with pytest.raises(DeviceError, match='refused'):
        run(rig)
    assert api.caps() == (100e6, 100e6)
    assert [w['actor'].split(':')[-1] for w in api.writes if w['actor']] == ['trial', 'restored']
    saved = read_json(directory / 'target-response.json')
    assert saved['shift']['restore']['status'] == 'restored' and saved['status'] == 'failed'
    assert read_json(directory / shift.CEILING_FILE)['status'] == 'restored'


def test_readiness_restores_a_trial_cap_left_by_a_dead_study(tmp_path):
    clock = Clock()
    lock_dir = tmp_path / 'lock'
    lock_dir.mkdir()
    api = API(clock, lock_dir)
    api.doc['target']['max_change_hz'] = 400e6
    api.doc['devices']['native']['control_settings']['native_current']['native']['target_ramp_step_hz'] = 400e6
    dead = tmp_path / 'dead-study'
    dead.mkdir()
    record = dict(status='trial', study_id='dead-study', laser_id='test', paths={
        'target.max_change_hz': dict(before=100e6, after=400e6),
        'devices.native.control_settings.native_current.native.target_ramp_step_hz': dict(before=100e6, after=400e6)})
    (dead / shift.CEILING_FILE).write_text(json.dumps(record))
    (dead / 'progress.json').write_text(json.dumps(dict(status='failed')))
    alive = tmp_path / 'alive-study'
    alive.mkdir()
    (alive / shift.CEILING_FILE).write_text(json.dumps(dict(record, study_id='alive-study')))
    (alive / 'progress.json').write_text(json.dumps(dict(status='running')))
    laser = copy.deepcopy(api.doc)
    events = []
    restored = restore_pending_ceiling(tmp_path, laser, api, emit=lambda k, v: events.append((k, v)), actor='card:change-target')
    assert len(restored) == 1 and restored[0]['status'] == 'restored_by_card:change-target'
    assert api.caps() == (100e6, 100e6) and laser['target']['max_change_hz'] == 100e6
    assert api.writes[-1]['actor'] == 'card:change-target:ceiling-restore'
    assert read_json(dead / shift.CEILING_FILE)['status'].startswith('restored_by') and read_json(alive / shift.CEILING_FILE)['status'] == 'trial'
    assert events[0][0] == 'target_ceiling_restored'
    # a second pass finds nothing to do; a record whose values are no longer in the yaml is marked superseded
    assert restore_pending_ceiling(tmp_path, laser, api) == []
    (alive / 'progress.json').write_text(json.dumps(dict(status='cancelled')))
    outcome = restore_pending_ceiling(tmp_path, laser, api)
    assert outcome[0]['status'] == 'superseded' and api.caps() == (100e6, 100e6)
