"""Offline request/receipt and actual-target correlation; no owner or instrument access."""
import copy
import json

import pytest
import yaml

from ldcon.jobs import lock_target as lt


TARGET = 300e12


@pytest.fixture
def rig(tmp_path, monkeypatch):
    lock = tmp_path / 'lock'
    lock.mkdir()
    (lock / 'laser-live.json').write_text(json.dumps({'supported': True}))
    monkeypatch.setattr(lt, 'running_lock', lambda *_: (lock, {'id': 'parent'}, {}))
    now = [1000.]
    monkeypatch.setattr(lt.time, 'time', lambda: now[0])

    class API:
        legacy = False

        def __init__(self):
            self.doc = dict(id='laser', target=dict(frequency_hz=TARGET,
                bounds_hz=[TARGET-1e9, TARGET+1e9], max_change_hz=100e6))
            self.revision = 'baseline'
            self.writes = []

        def get_configuration(self, *_):
            return dict(document=copy.deepcopy(self.doc), yaml=yaml.safe_dump(self.doc), revision=self.revision)

        def put_configuration(self, kind, name, text, expected_revision, **kwargs):
            assert expected_revision == self.revision
            self.doc = yaml.safe_load(text)
            before = self.revision
            self.revision = 'baseline' if self.doc['target']['frequency_hz'] == TARGET else 'outward'
            self.writes.append(dict(request_id='request-%d' % len(self.writes), revision_before=before,
                                    revision_after=self.revision))
            queued = dict(self.writes[-1], status='queued')
            return dict(revision=self.revision, live={} if self.legacy else dict(jobs={'parent': queued}))

    api = API()

    def receipt(request=None, **changes):
        value = dict(request or api.writes[-1], status='applied', applied=['target.frequency_hz'], updated_unix_s=now[0])
        value.update(changes)
        (lock / ('laser-reload-result-' + value['request_id'] + '.json')).write_text(json.dumps(value))

    def state(**changes):
        hz = api.doc['target']['frequency_hz']
        value = dict(target_frequency_hz=hz, ramped_target_hz=hz, frequency_hz=hz,
                     ramp_active=False, updated_unix_s=now[0], integral_fraction=.99)
        value.update(changes)
        (lock / 'lock-target.json').write_text(json.dumps(value))

    def run(target, advance, cancel=lambda: False):
        def sleep(seconds):
            now[0] += seconds
            advance()
        return lt.run_lock_target(tmp_path, copy.deepcopy(api.doc), {'id': 'target'},
            dict(target_frequency_hz=target, verify_samples=1, sample_interval_s=.1, timeout_s=2.),
            clock=lambda: now[0], sleep=sleep, cancel=cancel, emit=lambda *_: None, api=api)

    return api, now, receipt, state, run


@pytest.mark.parametrize('legacy', [False, True])
def test_round_trip_ignores_historical_receipts_even_with_same_revision_pair(rig, legacy):
    api, now, receipt, state, run = rig
    api.legacy = legacy
    receipt(dict(request_id='old-policy', revision_before='outward', revision_after='baseline'), applied=['devices.rig.policy'])
    receipt(dict(request_id='old-return', revision_before='outward', revision_after='baseline'))
    outward = run(TARGET+100e6, lambda: (receipt(), state()))
    returned = run(TARGET, lambda: (receipt(), state()))
    assert outward['reload_request_id'] == 'request-0'
    assert returned['reload_request_id'] == 'request-1'
    assert len(api.writes) == 2


@pytest.mark.parametrize('damage', ['old_publication', 'old_ramped_target', 'missing_ramped_target'])
def test_new_requested_label_cannot_complete_or_recentre_a_stale_actual_target(rig, monkeypatch, damage):
    api, now, receipt, state, run = rig
    ticks = []
    monkeypatch.setattr(lt, '_file_recentre', lambda *_: pytest.fail('stale state must not request a physical re-centre'))

    def advance():
        ticks.append(now[0])
        if len(ticks) == 1:
            receipt()
            changes = {'updated_unix_s': now[0]-1} if damage == 'old_publication' else {
                'ramped_target_hz': TARGET if damage == 'old_ramped_target' else None}
            state(**changes)
        else:
            state()

    result = run(TARGET+100e6, advance)
    assert len(ticks) == 2
    assert result['verify']['quality_samples'][0]['unix_s'] == now[0]


def test_exact_request_id_ignores_another_new_receipt_with_identical_revisions(rig):
    api, now, receipt, state, run = rig
    ticks = []

    def advance():
        ticks.append(now[0])
        if len(ticks) == 1:
            receipt(request_id='other-request', status='rejected', applied=[])
        else:
            receipt()
            state()

    assert run(TARGET+100e6, advance)['reload_request_id'] == 'request-0'
    assert len(ticks) == 2


def test_legacy_receipt_requires_fresh_before_and_after_identity(rig):
    api, now, receipt, state, run = rig
    api.legacy = True
    ticks = []

    def advance():
        ticks.append(now[0])
        if len(ticks) == 1:
            receipt(request_id='wrong-before', revision_before='other', status='rejected')
            receipt(request_id='old-time', updated_unix_s=999., status='rejected')
        else:
            receipt()
            state()

    assert run(TARGET+100e6, advance)['reload_request_id'] == 'request-0'
    assert len(ticks) == 2


def test_only_historical_receipt_waits_until_cancel_without_replaying_save(rig):
    api, now, receipt, state, run = rig
    api.legacy = True
    receipt(dict(request_id='old', revision_before='baseline', revision_after='outward'))
    with pytest.raises(lt.CardCancelled):
        run(TARGET+100e6, lambda: None, cancel=lambda: now[0] >= 1000.2)
    assert len(api.writes) == 1


def test_learned_completion_replaces_fixed_tolerance_and_sample_count(rig,monkeypatch):
    from ldcon.jobs import settling_profile
    api,now,receipt,state,run=rig
    calls=[]
    class Completion:
        enabled=True
        def __init__(self,*args,**kwargs):self.start=now[0]
        def poll(self,stamp):
            calls.append(stamp)
            return dict(status='settled' if stamp-self.start>.4 else 'not_settled',profile_revision='measured',
                        completion_basis='independently_validated_response_profile')
    monkeypatch.setattr(settling_profile,'Completion',Completion)
    monkeypatch.setattr(lt,'_file_recentre',lambda *_:pytest.fail('no uncontrolled recenter during learned completion'))
    result=run(TARGET+100e6,lambda:(receipt(),state(frequency_hz=TARGET+100e6+5e6)))
    assert len(calls)>3 and result['verify']['profile_revision']=='measured'


def test_new_target_preempts_learned_completion_without_restoring(rig,monkeypatch):
    from ldcon.jobs import settling_profile
    from ldcon.core.interface_cards import CardCancelled
    api,now,receipt,state,run=rig
    class Completion:
        enabled=True
        def __init__(self,*args,**kwargs):pass
        def poll(self,stamp):
            api.revision='human-new-target'
            return {'status':'not_settled'}
    monkeypatch.setattr(settling_profile,'Completion',Completion)
    with pytest.raises(CardCancelled,match='superseded'):
        run(TARGET+100e6,lambda:(receipt(),state()))
    assert len(api.writes)==1 and api.revision=='human-new-target'
