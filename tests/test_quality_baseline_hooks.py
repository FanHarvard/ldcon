import copy
from types import SimpleNamespace

import pytest

from ldcon.jobs.background_pid_session import changed_adoption
from ldcon.jobs.ecdl_window_center import WindowCenterSession
from ldcon.jobs.interface_worker import _quality_baseline_event, _quality_baseline_requires_dither


def pi_receipt():
    values=dict(pid_kp=10.,pid_ki=20.,pid_kd=0.,pid_integral_min=-1.,pid_integral_max=1.)
    return dict(state='adopted',checkpoint=values,receipt=dict(confirmed=True,response_ended=True,
                                                            actual_native=dict(values)))


def test_pi_baseline_requires_verified_changed_native_readback():
    last=pi_receipt()
    assert not changed_adoption(last,'pi')
    last['receipt']['actual_native']['pid_ki']=10.
    assert changed_adoption(last,'pi')
    last['state']='restored'
    assert not changed_adoption(last,'pi')
    last['state']='adopted';last['receipt']['confirmed']=False
    assert not changed_adoption(last,'pi')


def test_native_readback_roundoff_does_not_fabricate_a_gain_adjustment():
    last=pi_receipt()
    last['receipt']['actual_native']['pid_kp']+=1e-10
    assert not changed_adoption(last,'pi')
    del last['receipt']['actual_native']['pid_integral_min']
    assert not changed_adoption(last,'pi')


@pytest.mark.parametrize('family,field',[('slow_loop','tec_gain'),('feedforward','current_per_temperature')])
def test_parameter_baseline_compares_actual_parameters_to_exact_checkpoint(family,field):
    values=dict(tec_gain=.1,tec_dt_cap_s=5.,supervision_interval_s=10.,
                piezo_center_dac=25000.,current_per_temperature=1.)
    last=dict(state='adopted',checkpoint=dict(parameters=values),
              receipt=dict(confirmed=True,response_ended=True,actual_parameters=dict(values)))
    assert not changed_adoption(last,family)
    last['receipt']['actual_parameters'][field]=.2
    assert changed_adoption(last,family)
    last['receipt']['actual_parameters']['unverified_field']=1
    assert not changed_adoption(last,family)


def midpoint_fixture():
    session=WindowCenterSession.__new__(WindowCenterSession)
    session.request=dict(request_id='midpoint',card_id='window')
    session.p=dict(target_frequency_hz=None,hop_threshold_hz=1e6,observables=['metrics.side_peak_height_max'],
        boundary_limits={'metrics.side_peak_height_max':.5},require_single_mode=False,
        urgent_limits={},early_limits={},soft_quiet_s=0.,mode='monitor')
    session.steady={};session.point_dither=False;session.scan_points=[]
    session.policy=SimpleNamespace(features={},midpoint=.49,edge_kinds={},edges={-1:[.4],1:[.6]},
        _result=lambda:{'center':.49},qualification=lambda:{})
    session.clock=lambda:100.;session.wall_clock=lambda:100.
    session.acknowledged_moves=3;session.baseline_move_count=0;session.controlling=True
    events=[]
    session.record=lambda kind,payload:events.append((kind,payload))
    session.publish=lambda payload:None
    lock=SimpleNamespace(response_end=lambda:events.append(('cleanup',{})))
    row=dict(observation=dict(metrics={'side_peak_height_max':.1},frequency={'frequency_hz':700e12},
                              mode_id='desired',single_mode=True),
             outputs=dict(current=.49,temperature=.14,piezo=25000.))
    session.confirming=[copy.deepcopy(row),copy.deepcopy(row)]
    return session,lock,row,events


def test_manual_center_emits_only_after_cleanup_and_new_acknowledged_movement():
    session,lock,row,events=midpoint_fixture()
    session._confirm(lock,row)
    kinds=[kind for kind,_ in events]
    assert kinds.index('cleanup')<kinds.index('quality_adjustment_accepted')
    accepted=[payload for kind,payload in events if kind=='quality_adjustment_accepted']
    assert accepted[0]['action_id']=='midpoint:center:3'
    session.confirming=[copy.deepcopy(row),copy.deepcopy(row)]
    session._confirm(lock,row)
    assert sum(kind=='quality_adjustment_accepted' for kind,_ in events)==1
    session.acknowledged_moves=4
    session.confirming=[copy.deepcopy(row),copy.deepcopy(row)]
    session._confirm(lock,row)
    accepted=[payload for kind,payload in events if kind=='quality_adjustment_accepted']
    assert [item['action_id'] for item in accepted]==['midpoint:center:3','midpoint:center:4']


def test_failed_manual_cleanup_cannot_emit_an_accepted_baseline():
    session,lock,row,events=midpoint_fixture()
    def fail():raise OSError('cleanup unconfirmed')
    lock.response_end=fail
    with pytest.raises(OSError):session._confirm(lock,row)
    assert not any(kind=='quality_adjustment_accepted' for kind,_ in events)


@pytest.mark.parametrize('kind',['quality_adjustment_accepted','ecdl_lock_cycle'])
def test_optional_baseline_failure_becomes_a_diagnostic_not_control_failure(kind):
    def fail(*args):raise OSError('baseline storage unavailable')
    baseline=SimpleNamespace(accepted=fail,observe=fail)
    result=_quality_baseline_event(baseline,kind,dict(family='current',action_id='one',accepted_unix_s=100.))
    assert result['persisted'] is False
    assert result['state']=='error'
    assert 'OSError' in result['reason']


def test_baseline_callback_ignores_unrelated_events_and_returns_context_diagnostics():
    calls=[]
    baseline=SimpleNamespace(observe=lambda row:calls.append(row) or dict(state='pending',reason='Unknown branch'))
    assert _quality_baseline_event(baseline,'trace',{}) is None
    assert _quality_baseline_event(None,'ecdl_lock_cycle',{}) is None
    assert not calls
    assert _quality_baseline_event(baseline,'ecdl_lock_cycle',{})['reason']=='Unknown branch'


@pytest.mark.parametrize('verified',[False,True,None,'true'])
def test_baseline_dither_requirement_uses_only_verified_bound_lock_device(verified):
    laser=dict(interfaces=dict(lock=dict(device='chosen')),
        devices=dict(chosen=dict(control_settings=dict(steady_lock=dict(dither=dict(verified=verified)))),
                     other=dict(control_settings=dict(steady_lock=dict(dither=dict(verified=True))))))
    assert _quality_baseline_requires_dither(laser) is (verified is True)
    assert not _quality_baseline_requires_dither(dict(devices=laser['devices']))
