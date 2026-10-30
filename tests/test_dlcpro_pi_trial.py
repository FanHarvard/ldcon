from copy import deepcopy
import json
import math
from pathlib import Path

import pytest

from ldcon.core.interface_schema import digest
from ldcon.jobs.dlcpro_cards import Protocol
from ldcon.owners.dlcpro_pi_trial import candidate
from .test_dlcpro_offline import rig


def running(rig):
    o=rig['owner'];o.start_lock(3e14,rig['control'])
    protocol=Protocol(o,rig['device']);protocol.client='controller'
    def call(action,**kw):return protocol.dispatch(dict(action=action,laser_id=o.laser_id,**kw))
    rec=call('pi_trial_begin',device_revision=digest(rig['device']),identity='task-one',
        target_revision=o.target_revision,policy_revision=digest(o.policy),watchdog_s=5.)
    return o,call,rec['token']


def test_independent_increase_decrease_and_integral_limit_are_continuous(rig):
    o,call,token=running(rig)
    o.integral=.8;o.applied_observation['frequency_hz']=3e14+3e6;o.applied_error_axis=-3e6/1e8
    o.applied_pzt=o.base+o.control['kp']*(-3e6/1e8)+o.integral
    writes=len(rig['driver'].writes)
    for p in [dict(kp=.6,ki_per_s=.1,integral_limit_v=.2),dict(kp=.1,ki_per_s=1.,integral_limit_v=3.)]:
        r=call('pi_trial_apply',token=token,expected_revision=o.pi_trial.revision,candidate=p)
        assert r['output_before_v']==pytest.approx(r['output_after_v'],abs=1e-12)
        assert o.control['kp']==p['kp'] and o.control['ki_per_s']==p['ki_per_s']
        assert abs(o.integral)<=p['integral_limit_v']
    assert len(rig['driver'].writes)==writes
    call('pi_trial_finish',token=token,adopt=False)
    assert o.pi_trial.settings()=={k:rig['control'][k] for k in p}
    assert o.mode=='lock'


def test_foreground_target_restores_only_trial_settings_and_invalidates_token(rig):
    o,call,token=running(rig)
    call('pi_trial_apply',token=token,expected_revision=o.pi_trial.revision,
         candidate=dict(kp=.7,ki_per_s=.2,integral_limit_v=2.))
    revision=o.target_revision
    o.set_target(o.target+1e6,revision)
    assert o.target==3e14+1e6 and o.control['kp']==rig['control']['kp']
    assert o.pi_trial.last['state']=='restored'
    with pytest.raises(ValueError):call('pi_trial_apply',token=token,expected_revision=o.pi_trial.revision,
         candidate=dict(kp=.4,ki_per_s=.1,integral_limit_v=1.))
    assert o.target==3e14+1e6


def test_timeout_restore_does_not_wait_for_measurement_or_stop_lock(rig):
    o,call,token=running(rig)
    call('pi_trial_apply',token=token,expected_revision=o.pi_trial.revision,
         candidate=dict(kp=.5,ki_per_s=.3,integral_limit_v=1.))
    for _ in range(49):
        rig['clock'].sleep(.1);o.heartbeat();o.tick()
    rig['source'].missing=True;rig['clock'].sleep(.2);o.heartbeat();o.tick()
    assert o.mode=='lock' and o.pi_trial.active is None
    assert o.control['kp']==rig['control']['kp']
    assert o.pi_trial.last['confirmed']


def test_stale_revision_and_unknown_fields_never_change_settings(rig):
    o,call,token=running(rig);old=deepcopy(o.control)
    with pytest.raises(ValueError):call('pi_trial_apply',token=token,expected_revision=-1,
        candidate=dict(kp=.5,ki_per_s=.3,integral_limit_v=1.))
    with pytest.raises(ValueError):call('pi_trial_apply',token=token,expected_revision=o.pi_trial.revision,
        candidate=dict(kp=.5,ki_per_s=.3,integral_limit_v=1.,temperature=24))
    assert o.control==old
    with pytest.raises(ValueError):call('pi_trial_begin',device_revision=digest(rig['device']),identity='another',
        target_revision=o.target_revision,policy_revision=digest(o.policy),watchdog_s=5.)


def test_state_does_not_poll_chassis_or_renew_controller_lease(rig,monkeypatch):
    o,call,token=running(rig)
    monkeypatch.setattr(rig['instrument'],'snapshot',lambda *a:pytest.fail('No chassis poll from optimizer'))
    before=o.last_client;rig['clock'].sleep(.2)
    state=call('pi_trial_state')
    assert state['mode']=='lock' and 'token' not in state['trial']['active']
    assert o.last_client==before
    json.dumps(state,allow_nan=False)


def test_candidate_numeric_validation():
    for v in (float('nan'),float('inf'),True,-1):
        with pytest.raises(ValueError):candidate(dict(kp=v,ki_per_s=.2,integral_limit_v=1))


def test_switch_uses_confirmed_boundary_not_a_rejected_observation(rig):
    o,call,token=running(rig)
    before=o.applied_pzt
    o.last_observation['frequency_hz']=o.target+80e6
    call('pi_trial_apply',token=token,expected_revision=o.pi_trial.revision,
         candidate=dict(kp=.7,ki_per_s=.3,integral_limit_v=1.))
    e=(o.target-o.applied_observation['frequency_hz'])/o.policy['axes']['piezo']['frequency_hz_per_unit']
    assert o.base+o.control['kp']*e+o.integral==pytest.approx(before)
