"""PI priority/restoration uses real owner dispatch, simulated firmware only."""
import copy

import pytest

from ldcon.instrument_client import BrokerError
from test_locked_sweep_owner import rig


PARAMETERS=dict(controller_card_id='steady-lock',window_s=3.,min_samples=3,timeout_s=300.,
                gain_factors=[.5],minimum_improvement=.1,activity_weight=.1,save_settings=False)


@pytest.fixture
def pi_rig(rig,monkeypatch):
    owner,hw,now,call,directory=rig
    # The historical fixture has a 20k-count integral bound and a 1200-count
    # commissioned trial envelope, which the existing adapter rightly refuses.
    # This synthetic test baseline satisfies that envelope; no runtime config is edited.
    owner.steady.p=copy.deepcopy(owner.steady.p)
    owner.steady.p['integral_limit_count']=1000.
    hw.firmware.cfg['pid_integral_min']=-1000./hw.firmware.cfg['pid_ki']
    hw.firmware.cfg['pid_integral_max']=1000./hw.firmware.cfg['pid_ki']
    monkeypatch.setattr(owner.steady,'passive_dither',lambda:None)
    return rig


def begin(call,timeout=100.,**parameters):
    return call('background_begin',family='pi',parameters={**PARAMETERS,**parameters},
        expected_generation=call('background_status')['generation'],poll_interval_s=1.,
        handoff_timeout_s=timeout,identity=dict(monitor_id='monitor',generation='generation'))


def reduced(result):
    pid=copy.deepcopy(result['pid_tuning']['pid'])
    pid['kp']*=.5;pid['ki']*=.5
    return pid


def read(call,now):
    now[0]+=1.
    call('steady_heartbeat')
    return call('steady_read')


def restore(call,now,token):
    for _ in range(100):
        read(call,now)
        result=call('background_restore',token=token,reason='foreground')
        if result['epoch'] is None:return result
    raise AssertionError(result)


def test_foreground_reservation_reverses_pending_ramp_and_restores_actual_gains(pi_rig):
    owner,hw,now,call,_=pi_rig
    cfg=copy.deepcopy(hw.firmware.cfg);raw=hw.firmware.pid_integral
    result=begin(call);token=result['epoch']['token']
    assert 0<result['epoch']['return_bound_s']<=100.
    start=len(hw.firmware.sets)
    call('background_pid_target',token=token,pid=reduced(result))
    for _ in range(8):read(call,now)
    assert hw.firmware.cfg['pid_ki']<cfg['pid_ki']
    call('background_reserve',request_ids=['shot'])
    assert owner.steady.pid_tuning.target_factor==1.
    with pytest.raises(BrokerError,match='fenced'):
        call('background_pid_target',token=token,pid=reduced(result))
    restored=restore(call,now,token)
    assert restored['last']['state']=='restored' and owner.steady.pid_tuning.closed
    assert hw.firmware.cfg==cfg and hw.firmware.pid_integral==raw
    assert all(set(values)<={'pid_kp','pid_ki'} for values in hw.firmware.sets[start:])
    assert hw.firmware.cfg['pid_multiplier']==1 and owner.fault is None


def test_return_time_bound_and_persistence_rejected_before_gain_write(pi_rig):
    owner,hw,now,call,_=pi_rig
    before=copy.deepcopy(hw.firmware.sets)
    with pytest.raises(BrokerError,match='return needs'):begin(call,timeout=2.)
    with pytest.raises(BrokerError,match='save_settings=false'):begin(call,save_settings=True)
    with pytest.raises(BrokerError,match='proportional'):begin(call,strategy='adaptive_coordinate',window_s=10.,min_samples=12)
    assert hw.firmware.sets==before and owner.steady.pid_tuning is None


def test_real_uncommissioned_trial_envelope_is_not_relaxed(rig):
    owner,hw,now,call,_=rig
    before=copy.deepcopy(hw.firmware.sets)
    with pytest.raises(BrokerError,match='commissioned correction envelope'):begin(call)
    assert hw.firmware.sets==before and owner.steady.active


def test_runtime_adoption_preserves_raw_integral_and_configured_profile(pi_rig):
    owner,hw,now,call,_=pi_rig
    configured=copy.deepcopy(owner.device)
    result=begin(call);token=result['epoch']['token'];pid=reduced(result)
    bounds=(hw.firmware.cfg['pid_integral_min'],hw.firmware.cfg['pid_integral_max'])
    call('background_pid_target',token=token,pid=pid)
    for _ in range(100):
        row=read(call,now)
        if row['pid_tuning']['ready']:break
    result=call('background_pid_finish',token=token,status='succeeded')
    assert result['last']['state']=='adopted' and result['runtime_only']
    assert owner.device==configured
    assert (hw.firmware.cfg['pid_integral_min'],hw.firmware.cfg['pid_integral_max'])==bounds
    assert owner.steady.p['p_sample']==configured['control_settings']['steady_lock']['p_sample']*.5
    assert hw.firmware.cfg['pid_multiplier']==1


def test_adoption_loses_race_to_foreground_without_persisting_anything(pi_rig):
    owner,hw,now,call,_=pi_rig
    cfg=copy.deepcopy(hw.firmware.cfg)
    result=begin(call);token=result['epoch']['token']
    call('background_pid_target',token=token,pid=reduced(result))
    for _ in range(100):
        if read(call,now)['pid_tuning']['ready']:break
    call('background_reserve',request_ids=['target-request'])
    with pytest.raises(BrokerError,match='fenced'):call('background_pid_finish',token=token,status='succeeded')
    restore(call,now,token)
    assert hw.firmware.cfg==cfg


def test_uncertain_gain_write_is_fatal_and_not_replayed(pi_rig):
    owner,hw,now,call,_=pi_rig
    result=begin(call);token=result['epoch']['token']
    call('background_pid_target',token=token,pid=reduced(result))
    hw.firmware.fail_on='pid_kp'
    with pytest.raises(BrokerError):read(call,now)
    writes=len(hw.firmware.sets)
    with pytest.raises(BrokerError) as error:call('background_restore',token=token,reason='foreground')
    assert error.value.code=='fault' and len(hw.firmware.sets)==writes
