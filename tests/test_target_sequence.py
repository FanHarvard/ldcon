"""Run the real target card against simulated revision/owner receipts, never hardware."""
import copy
import json

import pytest
import yaml

from ldcon.jobs import lock_target as lt
from ldcon.core.contracts import DeviceError
from ldcon.core.interface_cards import CardCancelled
from ldcon.jobs.target_sequence import next_step

F = 274589143700000.


@pytest.fixture
def rig(tmp_path, monkeypatch):
    lock = tmp_path/'lock'
    lock.mkdir()
    (lock/'laser-live.json').write_text(json.dumps(dict(supported=True)))
    monkeypatch.setattr(lt, 'running_lock', lambda *_: (lock, {'id': 'lock'}, {}))
    now = [1000.]
    monkeypatch.setattr(lt.time, 'time', lambda: now[0])

    class API:
        def __init__(self):
            self.doc = dict(id='test', target=dict(frequency_hz=F,
                bounds_hz=[F-1e9, F+1e9], max_change_hz=100e6))
            self.rev = 'initial'
            self.writes = []
            self.fail_at = None
            self.events = []
            self.progress = []
            self.cancelled = False
            self.on_step = lambda: None
            self.on_read = lambda: None
            self.observation = lambda value: value
            self.write_times = []
            self.enforce_step = True

        def get_configuration(self, *_):
            self.on_read()
            return dict(document=copy.deepcopy(self.doc), yaml=json.dumps(self.doc), revision=self.rev)

        def put_configuration(self, kind, name, text, revision, **kwargs):
            if len(self.writes)+1 == self.fail_at:
                raise DeviceError('Simulated unconfirmed request; do not replay')
            assert revision == self.rev
            doc = yaml.safe_load(text)
            if self.enforce_step:
                assert abs(doc['target']['frequency_hz']-self.doc['target']['frequency_hz']) <= self.doc['target']['max_change_hz']
            before = self.rev
            self.doc = doc
            self.rev = 'revision-'+str(len(self.writes)+1)
            self.writes.append(doc['target']['frequency_hz'])
            self.write_times.append(now[0])
            rid = 'step-'+str(len(self.writes))
            (lock/('laser-reload-result-'+rid+'.json')).write_text(json.dumps(dict(
                request_id=rid, revision_before=before, revision_after=self.rev,
                status='applied', applied=['target.frequency_hz'], updated_unix_s=now[0])))
            return dict(revision=self.rev, live=dict(jobs={'lock': dict(status='queued', request_id=rid)}))

        def emit(self, kind, row):
            self.events.append((kind, copy.deepcopy(row)))
            if kind == 'lock_target_sequence_step_completed':
                self.on_step()

        def run(self, **params):
            snapshot = copy.deepcopy(self.doc)

            def sleep(seconds):
                now[0] += seconds
                hz = self.doc['target']['frequency_hz']
                (lock/'lock-target.json').write_text(json.dumps(self.observation(dict(
                    target_frequency_hz=hz, ramped_target_hz=hz, frequency_hz=hz,
                    ramp_active=False, updated_unix_s=now[0]))))

            return lt.run_lock_target(tmp_path, snapshot, {'id': 'change-target'},
                {**dict(queue_steps=True, target_frequency_hz=F+250e6, sample_interval_s=.1,
                     verify_samples=1, timeout_s=30.), **params},
                clock=lambda: now[0], sleep=sleep, cancel=lambda: self.cancelled,
                emit=self.emit, publish=lambda row: self.progress.append(copy.deepcopy(row)), api=self)

    return API(), now


@pytest.mark.parametrize('direction', [1, -1])
def test_real_card_queues_bounded_steps_until_final_target(rig, direction):
    api, now = rig
    result = api.run(target_frequency_hz=F+direction*250e6)
    assert api.writes == [F+direction*x for x in (100e6, 200e6, 250e6)]
    assert len(result['steps']) == 3 and result['target_frequency_hz'] == api.writes[-1]
    assert all(s['verify']['ok'] and s['locked'] for s in result['steps'])
    assert [r['step'] for k,r in api.events if k=='lock_target_sequence_step_completed'] == [1,2,3]
    assert api.progress[-1]['sequence']['final_target_hz'] == api.writes[-1]


@pytest.mark.parametrize('params', [dict(target_frequency_hz=None, offset_hz=250e6),
    dict(target_frequency_hz=None, target_wavelength_nm=lt.C_NM/(F+250e6))])
def test_wavelength_and_offset_resolve_once_at_start(rig, params):
    api, _ = rig
    result = api.run(**params)
    assert len(api.writes)==3 and abs(result['target_frequency_hz']-(F+250e6))<.1


@pytest.mark.parametrize('params', [dict(target_frequency_hz=F+2e9), dict(queue_steps=False)])
def test_bounds_and_default_single_step_are_not_relaxed(rig, params):
    api, _ = rig
    with pytest.raises(DeviceError):
        api.run(**params)
    assert not api.writes


@pytest.mark.parametrize('cap,expected', [(500e6,[100e6,200e6,250e6]), (60e6,[60e6,120e6,180e6,240e6,250e6])])
def test_card_override_can_only_reduce_sequence_step(rig, cap, expected):
    api, _ = rig
    api.run(max_change_hz=cap)
    assert api.writes == [F+x for x in expected]


def test_unknown_step_result_stops_without_replay_or_rollback(rig):
    api, _ = rig
    api.fail_at = 2
    with pytest.raises(DeviceError, match='unconfirmed'):
        api.run()
    assert api.writes == [F+100e6]


def test_user_cancel_stops_remaining_steps(rig):
    api, _ = rig
    api.on_step = lambda: setattr(api,'cancelled',True)
    with pytest.raises(CardCancelled):
        api.run()
    assert api.writes == [F+100e6]


def test_cancel_before_first_save_has_no_effect(rig):
    api, _ = rig
    api.cancelled = True
    with pytest.raises(CardCancelled):
        api.run()
    assert not api.writes


def test_unlocked_sequence_reports_saved_only(rig, monkeypatch):
    api, _ = rig
    monkeypatch.setattr(lt, 'running_lock', lambda *_: None)
    api.enforce_step = False
    start = rig[1][0]
    result = api.run()
    assert result['locked'] is False and 'verify' not in result and 'steps' not in result
    assert api.writes == [F+250e6] and rig[1][0] == start


def test_unchanged_target_does_not_claim_physical_verification(rig):
    api, _ = rig
    result = api.run(target_frequency_hz=F)
    assert result['unchanged'] and result['steps']==[] and not api.writes
    assert 'verify' not in result and 'locked' not in result


def test_total_deadline_is_not_reset_for_each_step(rig):
    api, now = rig
    api.on_step = lambda: now.__setitem__(0, now[0]+10)
    with pytest.raises(DeviceError, match='deadline'):
        api.run(timeout_s=5.)
    assert api.writes == [F+100e6]


def test_newer_target_is_never_overwritten_or_restored(rig):
    api, _ = rig
    def new_target():
        api.doc['target']['frequency_hz'] = F-40e6
        api.rev = 'operator-revision'
    api.on_step = new_target
    with pytest.raises(CardCancelled, match='superseded'):
        api.run()
    assert api.writes == [F+100e6] and api.doc['target']['frequency_hz'] == F-40e6


def test_safety_revision_changed_inside_step_prevents_save(rig):
    api, _ = rig
    reads = []
    def newer():
        reads.append(1)
        if len(reads)==3:
            api.doc['target']['max_change_hz'] = 10e6
            api.rev = 'new-bounds'
    api.on_read = newer
    with pytest.raises(CardCancelled, match='superseded'):
        api.run()
    assert not api.writes


def test_representational_rounding_never_exceeds_cap():
    laser = dict(target=dict(frequency_hz=F, bounds_hz=[F-1e9,F+1e9],max_change_hz=10.1))
    before, after, cap = next_step(laser,F+100.)
    assert 0 < after-before <= cap


@pytest.mark.parametrize('flag', [1, None, 'true'])
def test_sequence_opt_in_is_strict_boolean(flag):
    with pytest.raises(ValueError, match='boolean'):
        lt.validate(dict(queue_steps=flag,target_frequency_hz=F))


def test_web_admission_parameter_normalization_accepts_sequence_opt_in():
    from ldcon.core.interface_schema import parameters
    card = dict(id='change-target', operation='tune.lock_target', parameters={})
    request = parameters(card, dict(target_frequency_hz=F+250e6, queue_steps=True))
    assert request['queue_steps'] is True
    assert request['max_change_hz'] is None
    assert parameters(card, dict(target_frequency_hz=F))['queue_steps'] is False


@pytest.mark.parametrize('queued', [False, True])
def test_unlocked_accepts_large_final_target_without_step_configuration(rig,monkeypatch,queued):
    api,now=rig;api.enforce_step=False
    monkeypatch.setattr(lt,'running_lock',lambda *_:None)
    del api.doc['target']['max_change_hz']
    start=now[0];result=api.run(queue_steps=queued)
    assert api.writes==[F+250e6] and now[0]==start and result['locked'] is False
    with pytest.raises(DeviceError,match='outside'):
        api.run(target_frequency_hz=F+2e9,queue_steps=queued)
    assert len(api.writes)==1


def test_lock_appearing_during_unlocked_preparation_cannot_get_unbounded_change(rig,monkeypatch):
    api,_=rig;calls=[]
    def state(*_):
        calls.append(1);return None if len(calls)==1 else ('lock',{'id':'lock'},{})
    monkeypatch.setattr(lt,'running_lock',state)
    with pytest.raises(CardCancelled,match='lock started'):
        api.run()
    assert not api.writes
