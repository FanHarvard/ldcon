"""tune.lock_target after the target-ff design (2026-10-27): completion by frequency for both lasers, report-only
owner fields (headroom, integral fraction, hybrid recentre), queued steps capped by the owner's headroom and waiting
only while the owner's background recentre is in flight. Fakes only."""
import json

import pytest

from ldcon.core.contracts import DeviceError
from ldcon.jobs import lock_target as lt
from ldcon.jobs.target_sequence import headroom_toward
from .test_target_sequence import rig, F


def test_native_steps_complete_on_frequency_and_report_the_owner_fields(rig):
    api, now = rig

    def observe(value):
        value.update(dac_headroom_hz=dict(up_hz=5e9, down_hz=5e9), integral_fraction=.42,
                     owner_recentre=dict(status='not_needed'), recentre_active=False, automatic_recentre=False)
        return value
    api.observation = observe
    result = api.run()
    assert api.writes == [F+100e6, F+200e6, F+250e6]
    for step in result['steps']:
        verify = step['verify']
        assert verify['completion_basis'] == 'frequency_arrival' and verify['tec_centering'] is None
        assert verify['dac_headroom_hz'] == dict(up_hz=5e9, down_hz=5e9) and verify['integral_fraction'] == .42
        assert verify['owner_recentre'] == dict(status='not_needed') and verify['max_integral_fraction'] == .42
    assert all(p['status'] in ('queued', 'saved', 'ramping', 'verifying', 'settled') for p in api.progress)
    assert any(p.get('dac_headroom_hz') for p in api.progress)


def test_queued_steps_are_capped_by_the_reported_headroom(rig):
    api, now = rig
    api.observation = lambda value: dict(value, pzt_headroom_hz=dict(up_hz=60e6, down_hz=5e9))
    api.run()                                                       # the first step has no live row yet: saved cap
    assert api.writes == [F+100e6, F+160e6, F+220e6, F+250e6]
    assert all(p['sequence']['maximum_step_hz'] <= 100e6 for p in api.progress if 'sequence' in p)
    assert headroom_toward(dict(pzt_headroom_hz=dict(up_hz=1., down_hz=2.)), -5.) == 2.
    assert headroom_toward(dict(dac_headroom_hz=dict(up_hz=3., down_hz=None)), 5.) == 3.
    assert headroom_toward({}, 5.) is None and headroom_toward(dict(pzt_headroom_hz=dict(up_hz=float('nan'))), 5.) is None


def test_next_step_waits_only_while_the_owner_recentre_is_in_flight(rig):
    api, now = rig

    def observe(value):
        age = now[0] - api.write_times[-1]
        value['recentre_active'] = age < 1.
        value['owner_recentre'] = dict(status='started')
        return value
    api.observation = observe
    api.run()
    assert api.writes == [F+100e6, F+200e6, F+250e6]
    assert all(b - a >= 1. for a, b in zip(api.write_times, api.write_times[1:]))
    assert any(p.get('waiting_for') == 'owner_recentre' for p in api.progress)


def test_zero_headroom_holds_the_next_step_until_room_returns(rig):
    api, now = rig

    def observe(value):
        age = now[0] - api.write_times[-1]
        value['pzt_headroom_hz'] = dict(up_hz=0. if age < 1. else 1e9, down_hz=1e9)
        return value
    api.observation = observe
    api.run()
    assert api.writes == [F+100e6, F+200e6, F+250e6]
    assert all(b - a >= 1. for a, b in zip(api.write_times, api.write_times[1:]))
    assert any(p.get('waiting_for') == 'headroom' for p in api.progress)


def test_edit_scalar_text_changes_only_the_named_scalar():
    text = ('id: x\ntarget:\n  frequency_hz: 1.0e14   # keep\n  max_change_hz: 100000000.0\n'
            'devices:\n  rig:\n    control_settings:\n      steady_lock: {target_ramp_step_hz: 100000000.0, tec_gain: 0.2}\n')
    edited = lt.edit_scalar_text(text, ('target', 'max_change_hz'), 400e6)
    assert '# keep' in edited and 'max_change_hz: 400000000.0' in edited and 'tec_gain: 0.2' in edited
    edited = lt.edit_scalar_text(edited, ('devices', 'rig', 'control_settings', 'steady_lock', 'target_ramp_step_hz'), 400e6)
    assert 'target_ramp_step_hz: 400000000.0' in edited
    with pytest.raises(DeviceError, match='Cannot edit'):
        lt.edit_scalar_text(text, ('target', 'missing'), 1.)
    with pytest.raises(DeviceError):
        lt.edit_scalar_text(text, ('id',), 1.)


def test_unlocked_target_change_restores_a_dead_studys_trial_cap_first(tmp_path):
    from ldcon.jobs.target_response_shift import CEILING_FILE
    jobs = tmp_path / 'jobs'
    card = jobs / 'card'
    dead = jobs / 'dead'
    card.mkdir(parents=True); dead.mkdir()
    text = ('id: sr9\nallow_output: true\ntarget:\n  frequency_hz: 300000000000000.0\n  allowed_change_hz: [-1000000000.0, 1000000000.0]\n'
            '  max_change_hz: 400000000.0\ndevices:\n  rig:\n    type: ecdl.rig.owner\n    connection: {}\n    absolute_conditions: {}\n'
            'interfaces:\n  lock: {device: rig, capability: ecdl_stationary_lock}\n')

    class Api:
        def __init__(self):
            self.text, self.revision, self.puts = text, 'r0', []
        def get_configuration(self, kind, name):
            import yaml
            return dict(document=yaml.safe_load(self.text), yaml=self.text, revision=self.revision)
        def put_configuration(self, kind, name, new_text, expected, **kw):
            assert expected == self.revision
            self.text = new_text
            self.revision = 'r%d' % (len(self.puts) + 1)
            self.puts.append(kw.get('actor'))
            return dict(revision=self.revision)
    api = Api()
    (dead / CEILING_FILE).write_text(json.dumps(dict(status='trial', study_id='dead', laser_id='sr9',
        paths={'target.max_change_hz': dict(before=100e6, after=400e6)})))
    (dead / 'progress.json').write_text(json.dumps(dict(status='interrupted')))
    import yaml
    laser = yaml.safe_load(text)
    events = []
    result = lt.run_lock_target(card, laser, dict(id='change-target'), dict(offset_hz=50e6), clock=lambda: 0., sleep=lambda s: None,
                                cancel=lambda: False, emit=lambda k, v: events.append(k), api=api)
    assert result['status'] == 'succeeded' and not result['locked']
    assert api.puts == ['card:change-target:ceiling-restore', 'card:change-target']
    assert yaml.safe_load(api.text)['target']['max_change_hz'] == 100e6 and laser['target']['max_change_hz'] == 100e6
    assert 'target_ceiling_restored' in events
