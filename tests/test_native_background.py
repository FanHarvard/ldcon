"""Offline native background PI safety and foreground priority contracts."""
import copy
import importlib.util
from pathlib import Path
import socket
import time
from types import SimpleNamespace

import pytest

from experiment_core.records import atomic_json
from ldcon.core.instrument_client import BrokerError
from ldcon.jobs.online_pid import parameters

# Reuse the existing independent fake physical owner, optical and CLD fixtures.
spec = importlib.util.spec_from_file_location('native_fixture', (Path(__file__).resolve().parent / 'support')/'jobs/tests/test_esp32_native_current.py')
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
rig = fixture.rig


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*a, **k): raise AssertionError('No hardware/network access in offline test')
    monkeypatch.setattr(socket.socket,'connect',forbidden)
    monkeypatch.setattr(socket,'create_connection',forbidden)


@pytest.fixture
def background(rig, tmp_path, monkeypatch):
    owner, servo, clock, bias, optical, gate, call, ready = rig
    root = tmp_path/'trusted'; root.mkdir()
    monkeypatch.setenv('LDCON_ADAPTIVE_ROOT',str(root))
    directory = root/'test'/'controller'; directory.mkdir(parents=True)
    p = parameters({'parameters':{}},dict(controller_card_id='native',gain_factors=[.99],save_settings=False))
    identity = dict(monitor_id='monitor',generation='one',grant_revision='rev',controller_job_id='controller')
    grant = dict(enabled=True,generation='one',parameters_hash='rev',last_seen=time.time(),max_age_s=60,
        bounds=dict(enabled=True,families=['pi'],pid_parameters=p,handoff_timeout_s=20.))
    (root/'test'/'monitors').mkdir()
    atomic_json(root/'test'/'monitors'/'monitor.json',grant)
    atomic_json(directory/'background-control.json',dict(enabled=True,monitor_id='monitor',generation='one'))
    ready(); call('arm'); servo.status['pid_integral']=0.
    def begin(**extra):
        return call('background_begin',family='pi',identity=identity,parameters=p,expected_generation=owner.background.coordinator.generation,
                    handoff_timeout_s=extra.pop('handoff_timeout_s',20.),**extra)
    def tick(n=1):
        for _ in range(n):
            call('heartbeat'); clock.advance(); owner.poll()
    return owner,servo,clock,call,begin,tick,root,grant,p


def test_native_pi_restores_autonomously_before_retarget(background):
    owner,servo,clock,call,begin,tick,root,grant,p = background
    original=copy.deepcopy(servo.cfg)
    result=begin(); token=result['epoch']['token']
    assert result['epoch']['return_bound_s']<=20
    call('background_pid_target',token=token,pid={k:original['pid_'+k]*.99 for k in ('kp','ki','kd')})
    tick(4)
    assert servo.cfg['pid_ki']!=original['pid_ki']
    with pytest.raises(BrokerError,match='returning before retarget'):
        call('retarget',target_frequency_hz=2e14)
    tick(8)
    state=call('background_status')
    assert state['epoch'] is None and state['last']['state']=='restored'
    assert servo.cfg==original
    assert owner.active and not owner.fault and owner.lease


def test_grant_revocation_restores_without_worker_or_model(background):
    owner,servo,clock,call,begin,tick,root,grant,p=background
    original=copy.deepcopy(servo.cfg); token=begin()['epoch']['token']
    call('background_pid_target',token=token,pid={k:original['pid_'+k]*.99 for k in ('kp','ki','kd')})
    tick(4)
    grant['enabled']=False; atomic_json(root/'test'/'monitors'/'monitor.json',grant)
    tick(8)
    assert call('background_status')['last']['reason']=='grant_revoked'
    assert servo.cfg==original and owner.active


def test_adoption_is_runtime_only_and_cannot_compound(background):
    owner,servo,clock,call,begin,tick,root,grant,p=background
    original=copy.deepcopy(servo.cfg); token=begin()['epoch']['token']
    candidate={k:original['pid_'+k]*.99 for k in ('kp','ki','kd')}
    call('background_pid_target',token=token,pid=candidate); tick(4)
    call('background_pid_finish',token=token,status='succeeded')
    assert owner.saved_pid is None
    assert servo.cfg['pid_ki']==candidate['ki']
    with pytest.raises(BrokerError,match='cannot compound'): begin()


def test_unapproved_gain_factor_and_direct_pid_cannot_bypass(background):
    owner,servo,clock,call,begin,tick,root,grant,p=background
    original=copy.deepcopy(servo.cfg); token=begin()['epoch']['token']
    writes=len(servo.writes)
    with pytest.raises(BrokerError,match='factor set'):
        call('background_pid_target',token=token,pid={k:original['pid_'+k]*.5 for k in ('kp','ki','kd')})
    assert len(servo.writes)==writes
    with pytest.raises(BrokerError,match='returning before pid_save'):
        call('pid_save',pid={k:original['pid_'+k] for k in ('kp','ki','kd')})
    with pytest.raises(BrokerError):
        call('background_pid_target',token=token,pid={k:original['pid_'+k]*.99 for k in ('kp','ki','kd')})


def test_restore_timeout_blocks_foreground_without_stopping_pi(background):
    owner,servo,clock,call,begin,tick,root,grant,p=background
    original=copy.deepcopy(servo.cfg); token=begin()['epoch']['token']
    call('background_pid_target',token=token,pid={k:original['pid_'+k]*.99 for k in ('kp','ki','kd')});tick(4)
    call('background_reserve',request_ids=['shot'])
    # Simulate deadline independently of the unchanged main-input/heartbeat guards.
    owner.background.coordinator.epoch['return_deadline']=clock()-1
    tick()
    state=call('background_status')
    assert state['epoch']['state']=='reconciliation_required' and not state['foreground_ready']
    assert owner.active and not owner.fault
    with pytest.raises(BrokerError,match='returning before'):
        call('retarget',target_frequency_hz=2e14)


def test_shared_session_observes_autonomous_restore(background, tmp_path):
    from ldcon.jobs.native_background_session import NativeBackgroundPort
    from ldcon.jobs.background_pid_session import BackgroundPIDSession
    from ldcon.owners.esp32_native_client import NativeCurrentPort
    from types import SimpleNamespace
    owner,servo,clock,call,begin,tick,root,grant,p=background
    port=NativeCurrentPort.__new__(NativeCurrentPort)
    port.policy=owner.policy; port.lease=owner.lease;port.uncertain_output=False
    port._call=lambda action,**kwargs: owner.dispatch(dict(device_id=servo.device_id,action=action,**kwargs))
    adapter=NativeBackgroundPort(port,'controller')
    directory=root/'test'/'controller'
    identity=dict(monitor_id='monitor',generation='one',grant_revision='rev',handoff_timeout_s=20.,expected_operating_mode_revision=0)
    atomic_json(directory/'adaptive-experiment.json',dict(background=identity))
    adapter.manager=SimpleNamespace(directory=directory)
    session=BackgroundPIDSession(tmp_path/'session',adapter,pid_parameters=p,background=identity,clock=clock)
    session.poll()
    assert session.active
    session.port.retune({k:servo.cfg['pid_'+k]*.99 for k in ('kp','ki','kd')}); tick(4)
    grant['enabled']=False; atomic_json(root/'test'/'monitors'/'monitor.json',grant);tick(8)
    session.check_deadlines()
    assert session.blocked and not session.active and session.last_restoration
    assert owner.active and not port.uncertain_output


def test_grant_revoked_between_target_and_effect_prevents_write(background):
    owner,servo,clock,call,begin,tick,root,grant,p=background
    original=copy.deepcopy(servo.cfg); token=begin()['epoch']['token']
    call('background_pid_target',token=token,pid={k:original['pid_'+k]*.99 for k in ('kp','ki','kd')})
    grant['last_seen']=time.time()-1000
    atomic_json(root/'test'/'monitors'/'monitor.json',grant)
    writes=len(servo.writes);tick()
    assert len(servo.writes)==writes and servo.cfg==original
    assert call('background_status')['last']['state']=='restored'


def test_insufficient_return_budget_refuses_before_write(background):
    owner,servo,clock,call,begin,tick,root,grant,p=background
    grant['bounds']['handoff_timeout_s']=.1
    atomic_json(root/'test'/'monitors'/'monitor.json',grant)
    writes=len(servo.writes)
    with pytest.raises(BrokerError,match='return needs'): begin(handoff_timeout_s=.1)
    assert len(servo.writes)==writes and owner.pid_tuning is None


def test_explicit_stop_does_not_restore_or_leave_epoch_for_next_lock(background):
    owner,servo,clock,call,begin,tick,root,grant,p=background
    original=copy.deepcopy(servo.cfg); token=begin()['epoch']['token']
    call('background_pid_target',token=token,pid={k:original['pid_'+k]*.99 for k in ('kp','ki','kd')});tick(4)
    writes=len(servo.writes)
    call('stop')
    assert servo.writes[writes:]==[{'pid_multiplier':0}]
    assert owner.background.status()['epoch'] is None
    assert not owner.active and not owner.fault


def test_assembly_and_policy_mutation_fenced_atomically(background):
    owner,servo,clock,call,begin,tick,root,grant,p=background
    begin()
    writes=len(servo.writes)
    with pytest.raises(BrokerError,match='returning before assembly_start'):
        call('assembly_start',session_request={'request_id':'shot'},target_hz=2e14)
    assert not owner.system_owner.assemblies.active()
    with pytest.raises(BrokerError,match='returning before policy_adoption'):
        call('policy_update',parameters={})
    assert len(servo.writes)==writes


def test_native_manager_runs_shared_pi_trial_and_preserves_main_lock(background, tmp_path):
    from ldcon.jobs.native_background_session import manager
    from ldcon.owners.esp32_native_client import NativeCurrentPort
    owner,servo,clock,call,begin,tick,root,grant,p=background
    p.update(window_s=1.,min_samples=3,timeout_s=30.)
    grant['bounds']['pid_parameters']=p
    atomic_json(root/'test'/'monitors'/'monitor.json',grant)
    port=NativeCurrentPort.__new__(NativeCurrentPort)
    port.policy=owner.policy;port.lease=owner.lease;port.uncertain_output=False
    port._call=lambda action,**kwargs: owner.dispatch(dict(device_id=servo.device_id,action=action,**kwargs))
    job=tmp_path/'deploy'/'jobs'/'controller';job.mkdir(parents=True)
    mgr=manager(job,port,dict(id='test'),clock=clock,emit=lambda *a:None,poll_interval_s=.2)
    identity=dict(monitor_id='monitor',generation='one',grant_revision='rev',handoff_timeout_s=20.,expected_operating_mode_revision=0)
    atomic_json(mgr.directory/'adaptive-experiment.json',dict(id='trial',family='pi',pid_parameters=p,background=identity))
    initial=copy.deepcopy(servo.cfg)
    for _ in range(100):
        tick()
        state=owner.snapshot()
        mgr.poll(row=mgr.row(state,state['optical'],0.,1e7))
        if mgr.session and mgr.session.blocked:break
    assert mgr.session.blocked and not mgr.session.active
    assert owner.active and not owner.fault and not port.uncertain_output
    assert servo.cfg==initial
    assert mgr.session.last_restoration['last']['state']=='restored'


def test_restart_retains_checkpoint_and_never_replays(background):
    owner,servo,clock,call,begin,tick,root,grant,p=background
    token=begin()['epoch']['token']; writes=len(servo.writes)
    owner._persist()   # the durable state of a running lock (a deliberate shutdown persists the same)
    restarted=fixture.NativeCurrentOwner(servo,.2,1.,None,owner.policy,owner.state_file,
        clock=clock,optical=owner.optical,bias=owner.bias,gate=owner.gate)
    restarted.poll()
    # 2026-10-27: the active PI is adopted by the next owner (no fault, same lease, no write); the background
    # checkpoint is still blocked for reconciliation by its manager, never replayed by the owner.
    assert restarted.fault is None and restarted.active and restarted.lease==owner.lease
    assert restarted.adoption['state']=='adopted'
    assert restarted.background.status()['epoch']['token']==token
    assert restarted.background.status()['epoch']['state']=='reconciliation_required'
    assert len(servo.writes)==writes


def test_trusted_namespace_alias_handles_laser_label_case(background, monkeypatch):
    owner,servo,clock,call,begin,tick,root,grant,p=background
    # Namespace binding is startup authority, not supplied by the task/model.
    monkeypatch.setenv('LDCON_BACKGROUND_LASER_ID','other-namespace')
    assert call('background_status')['system_id']=='other-namespace'
    with pytest.raises((BrokerError,FileNotFoundError)):begin()
    assert owner.pid_tuning is None
    monkeypatch.setenv('LDCON_BACKGROUND_LASER_ID','test')
    assert begin()['epoch']


def test_unknown_gain_write_is_not_retried_and_retains_owner_fault(background):
    owner,servo,clock,call,begin,tick,root,grant,p=background
    original=copy.deepcopy(servo.cfg);token=begin()['epoch']['token']
    call('background_pid_target',token=token,pid={k:original['pid_'+k]*.99 for k in ('kp','ki','kd')})
    real_write=servo.set_control
    effects=[]
    def ambiguous(values):
        effects.append(copy.deepcopy(values));real_write(values)
        if 'pid_kp' in values or 'pid_ki' in values:raise OSError('Reply lost after coefficient write')
    servo.set_control=ambiguous
    tick()
    assert owner.fault and owner.lease
    count=sum('pid_kp' in v or 'pid_ki' in v for v in effects)
    for _ in range(4): clock.advance();owner.poll()
    assert sum('pid_kp' in v or 'pid_ki' in v for v in effects)==count==1
    assert owner.background.status()['epoch']['state']=='reconciliation_required'


@pytest.fixture
def flexible(background):
    owner,servo,clock,call,begin,tick,root,grant,p=background
    p['strategy']='validated_candidates'
    grant['bounds']['pid_parameters']=copy.deepcopy(p)
    atomic_json(root/'test'/'monitors'/'monitor.json',grant)
    p['candidate']=dict(kp_count_per_nm=-3000.,ki_count_per_nm_s=-2500.,integral_limit_count=3.)
    return background


@pytest.fixture
def candidate_comparison(tmp_path):
    """Asynchronous readback with deterministic acquisition times, no hardware."""
    from ldcon.jobs.background_pid_session import _CandidateTuner,background_parameters
    now=[0.];cancel=[False];published=[];events=[]
    p=background_parameters({'parameters':{}},dict(strategy='validated_candidates',save_settings=False,
        window_s=60.,settle_s=60.,min_samples=12,timeout_s=300.,gain_factors=[.5],
        candidate=dict(kp_count_per_nm=-100.,ki_count_per_nm_s=-1.,integral_limit_count=45.)))
    pending=[dict(request_id='comparison',card_id='pi',parameters=p)]
    class Port:
        def __init__(self):
            self.pid=dict(kp=-100.,ki=-2.,kd=0.,integral_limit_count=45.)
            self.original=copy.deepcopy(self.pid);self.target=copy.deepcopy(self.pid)
            self.is_ready=True;self.commands=[];self.finished=[]
            self.config=dict(resolution=1.)
            self.session=SimpleNamespace(directory=tmp_path,segment=0)
        def ready(self):return self.is_ready
        def retune(self,pid):
            self.commands.append(copy.deepcopy(pid));self.target=copy.deepcopy(pid)
            self.is_ready=self.pid==pid
        def complete_transition(self):
            self.pid=copy.deepcopy(self.target);self.is_ready=True
        def finish(self,status):self.finished.append(status)
    port=Port()
    tuner=_CandidateTuner(lambda:pending.pop() if pending else None,published.append,
        lambda kind,value:events.append((kind,value)),lambda:now[0],lambda _:cancel[0])
    def tick(stamp,*,error=8e6,limited=False):
        now[0]=float(stamp)
        row=dict(error=error,filtered_error=error,output=10.,limited=limited,
            control_deadband=1e6,requested_tolerance=1e6,integral_saturated=False,integral_usage=.5)
        tuner.tick(now[0],row,port,port)
    for stamp in range(61):tick(stamp)
    assert len(port.commands)==1 and not port.ready()
    return tuner,port,tick,now,cancel,published,events


def test_candidate_settles_after_last_ready_transition_then_measures_full_window(candidate_comparison,tmp_path):
    import json
    tuner,port,tick,now,cancel,published,events=candidate_comparison
    for stamp in range(61,70):tick(stamp,error=100e6)
    port.complete_transition()
    for stamp in range(70,130):
        tick(stamp,error=100e6)
        assert not tuner.rows
    for stamp in range(130,140):tick(stamp,error=3e6)
    assert tuner.rows
    port.is_ready=False
    for stamp in range(140,150):tick(stamp,error=100e6)
    port.complete_transition()
    for stamp in range(150,210):
        tick(stamp,error=100e6)
        assert not tuner.rows and not port.finished
    tick(210,error=2e6)
    for stamp in range(211,270):tick(stamp,error=2e6)
    assert not port.finished
    tick(270,error=2e6)
    result=json.loads((tmp_path/'segment-000-trial-000-measurements.json').read_text())
    assert result['rows'][0][0]==210. and result['rows'][-1][0]==270.
    assert len(result['rows'])==61 and result['summary']['rms']==2e6
    assert port.finished==['succeeded'] and port.pid['ki']==-1.
    assert any(row.get('settling') for row in published)


@pytest.mark.parametrize('stop', ['cancel','deadline','ineligible'])
def test_candidate_guards_restore_during_settling(candidate_comparison,tmp_path,stop):
    tuner,port,tick,now,cancel,published,events=candidate_comparison
    port.complete_transition();tick(270 if stop=='deadline' else 61,error=100e6)
    stamp=80
    if stop=='cancel':cancel[0]=True
    if stop=='deadline':stamp=300
    tick(stamp,error=100e6,limited=stop=='ineligible')
    assert port.target==port.original and not port.ready()
    assert not tuner.rows and not list(tmp_path.glob('*trial*measurements.json'))
    assert not port.finished
    port.complete_transition();tick(stamp+1)
    assert port.finished==['cancelled' if stop=='cancel' else 'failed']
    assert not tuner.active and port.pid==port.original


def test_candidate_gap_discards_window_without_retune_or_deadline_reset(candidate_comparison,tmp_path):
    import json
    tuner,port,tick,now,cancel,published,events=candidate_comparison
    port.complete_transition()
    for stamp in range(61,140):tick(stamp,error=2e6)
    assert tuner.rows
    now[0]=150.;tuner.restart_window('Accepted acquisition gap')
    for stamp in range(150,210):tick(stamp,error=2e6)
    assert not port.finished and len(port.commands)==1
    assert tuner.started==0. and tuner.p['timeout_s']==300.
    tick(210,error=2e6)
    result=json.loads((tmp_path/'segment-000-trial-000-measurements.json').read_text())
    assert result['rows'][0][0]==150. and result['rows'][-1][0]==210.
    assert port.finished==['succeeded']


def test_flexible_foreground_preempts_during_candidate_settle(flexible,tmp_path):
    from ldcon.jobs.native_background_session import manager
    from ldcon.owners.esp32_native_client import NativeCurrentPort
    owner,servo,clock,call,begin,tick,root,grant,p=flexible
    p.update(window_s=4.,settle_s=60.,min_samples=24,timeout_s=300.)
    grant['bounds']['pid_parameters']={k:v for k,v in p.items() if k!='candidate'}
    atomic_json(root/'test'/'monitors'/'monitor.json',grant)
    port=NativeCurrentPort.__new__(NativeCurrentPort)
    port.policy=owner.policy;port.lease=owner.lease;port.uncertain_output=False
    port._call=lambda action,**kwargs:owner.dispatch(dict(device_id=servo.device_id,action=action,**kwargs))
    job=tmp_path/'deploy'/'jobs'/'controller';job.mkdir(parents=True)
    mgr=manager(job,port,dict(id='test'),clock=clock,emit=lambda *a:None,poll_interval_s=.2)
    identity=dict(monitor_id='monitor',generation='one',grant_revision='rev',handoff_timeout_s=20.,expected_operating_mode_revision=0)
    atomic_json(mgr.directory/'adaptive-experiment.json',dict(id='settling',family='pi',pid_parameters=p,background=identity))
    initial=copy.deepcopy(servo.cfg)
    for _ in range(240):
        tick();state=owner.snapshot()
        mgr.poll(row=mgr.row(state,state['optical'],0.,1e7))
        session=mgr.session
        if session and session.tuner and getattr(session.tuner,'candidate_ready_at',None) is not None:break
    else:pytest.fail('Candidate never reached its post-transition settling interval')
    assert not session.tuner.rows and servo.cfg!=initial
    call('background_reserve',request_ids=['foreground-target'])
    for _ in range(100):
        tick();mgr.poll()
        if session.blocked:break
    assert session.blocked and not session.active
    assert servo.cfg==initial and owner.active and not owner.fault
    assert not list(session.directory.glob('*trial*measurements.json'))


def test_flexible_gain_increase_and_integral_change_restore_every_field(flexible):
    from ldcon.owners.flexible_native_trial import pid_from_candidate
    owner,servo,clock,call,begin,tick,root,grant,p=flexible
    initial=copy.deepcopy(servo.cfg);result=begin();token=result['epoch']['token']
    assert result['epoch']['return_bound_s']<=20
    trial=owner.pid_tuning
    call('background_pid_target',token=token,pid=pid_from_candidate(p['candidate']))
    tick(10)
    assert servo.cfg['pid_kp']==-3000. and servo.cfg['pid_ki']==-2500.
    assert abs(servo.cfg['pid_integral_max']*servo.cfg['pid_ki'])==pytest.approx(3.)
    assert servo.cfg['pid_integral_min']==-servo.cfg['pid_integral_max']
    from ldcon.owners.esp32_native_owner import current_for_code
    one_code=abs(current_for_code(owner.policy,32769)-current_for_code(owner.policy,32768))*(1+owner.policy['calibration']['gain_relative_uncertainty'])
    assert all(x['quantized_delta_bound_count']*one_code<=owner.policy['max_step_a']*(1+1e-9) for x in trial.changed)
    call('background_reserve',request_ids=['target'])
    tick(10)
    assert servo.cfg==initial and owner.active and not owner.fault
    assert call('background_status')['last']['state']=='restored'
    assert not any('pid_reset_state' in row['values'] for row in trial.changed)


def test_flexible_success_can_recheckpoint_but_never_expand_fixed_safety(flexible):
    from ldcon.owners.flexible_native_trial import pid_from_candidate
    owner,servo,clock,call,begin,tick,root,grant,p=flexible
    token=begin()['epoch']['token']
    call('background_pid_target',token=token,pid=pid_from_candidate(p['candidate']));tick(10)
    call('background_pid_finish',token=token,status='succeeded')
    adopted=copy.deepcopy(servo.cfg)
    assert owner.saved_pid is None
    p['candidate']=dict(kp_count_per_nm=-4000.,ki_count_per_nm_s=-3000.,integral_limit_count=3.5)
    token=begin()['epoch']['token']
    assert owner.background.coordinator.epoch['checkpoint']==adopted
    call('background_pid_target',token=token,pid=pid_from_candidate(p['candidate']));tick(10)
    call('background_restore',token=token,reason='test_cancel');tick(10)
    assert servo.cfg==adopted
    p['candidate']['integral_limit_count']=100000.
    writes=len(servo.writes)
    with pytest.raises(BrokerError,match='range|bounds|rail|envelope'):begin()
    assert len(servo.writes)==writes and owner.active and not owner.fault


@pytest.mark.parametrize('candidate',[
    dict(kp_count_per_nm=3000.,ki_count_per_nm_s=-2500.,integral_limit_count=3.),
    dict(kp_count_per_nm=-3000.,ki_count_per_nm_s=-2500.,integral_limit_count=0.),
    dict(kp_count_per_nm=-3000.,ki_count_per_nm_s=-2500.,integral_limit_count=float('nan')),
])
def test_flexible_invalid_candidate_is_prewrite_refusal(flexible,candidate):
    owner,servo,clock,call,begin,tick,root,grant,p=flexible
    p['candidate']=candidate;writes=len(servo.writes)
    with pytest.raises(BrokerError):begin()
    assert len(servo.writes)==writes and not owner.fault and owner.pid_tuning is None


def test_flexible_long_return_rejected_before_any_physical_effect(flexible):
    owner,servo,clock,call,begin,tick,root,grant,p=flexible
    p['candidate']['integral_limit_count']=60.
    writes=len(servo.writes)
    with pytest.raises(BrokerError,match='handoff budget'):begin()
    assert len(servo.writes)==writes and owner.active


def test_flexible_bound_shrink_is_clamp_limited_and_no_reset(flexible):
    from ldcon.owners.flexible_native_trial import pid_from_candidate
    owner,servo,clock,call,begin,tick,root,grant,p=flexible
    original_raw=servo.cfg['pid_integral_max']
    servo.status['pid_integral']=original_raw
    p['candidate']=dict(kp_count_per_nm=-1000.,ki_count_per_nm_s=-1000.,integral_limit_count=.1)
    token=begin()['epoch']['token'];trial=owner.pid_tuning
    call('background_pid_target',token=token,pid=pid_from_candidate(p['candidate']));tick(10)
    changes=[row for row in trial.changed if 'pid_integral_max' in row['values']]
    assert changes and all(row['maximum_delta_count']<=trial.effect_cap*(1+1e-12) for row in changes)
    assert sum(row['maximum_delta_count'] for row in changes)==pytest.approx(1.4)
    assert not any('pid_reset_state' in row['values'] for row in trial.changed)


@pytest.mark.parametrize("continuation",[False,True])
def test_flexible_shared_comparator_runs_candidate_and_returns_when_unimproved(flexible,tmp_path,continuation):
    from ldcon.jobs.native_background_session import manager
    from ldcon.owners.esp32_native_client import NativeCurrentPort
    owner,servo,clock,call,begin,tick,root,grant,p=flexible
    p.update(window_s=4.,min_samples=24,timeout_s=30.)
    grant['bounds']['pid_parameters']={k:v for k,v in p.items() if k!='candidate'}
    atomic_json(root/'test'/'monitors'/'monitor.json',grant)
    port=NativeCurrentPort.__new__(NativeCurrentPort)
    port.policy=owner.policy;port.lease=owner.lease;port.uncertain_output=False
    port._call=lambda action,**kwargs:owner.dispatch(dict(device_id=servo.device_id,action=action,**kwargs))
    job=tmp_path/'deploy'/'jobs'/'controller';job.mkdir(parents=True)
    mgr=manager(job,port,dict(id='test'),clock=clock,emit=lambda *a:None,poll_interval_s=.2)
    identity=dict(monitor_id='monitor',generation='one',grant_revision='rev',handoff_timeout_s=20.,expected_operating_mode_revision=0)
    atomic_json(mgr.directory/'adaptive-experiment.json',dict(id='flexible',family='pi',pid_parameters=p,background=identity,continuation=continuation))
    initial=copy.deepcopy(servo.cfg)
    for _ in range(240):
        tick();state=owner.snapshot()
        mgr.poll(row=mgr.row(state,state['optical'],0.,1e7))
        if mgr.session and (mgr.session.blocked or mgr.session._continuation_waiting()):break
    assert mgr.session.blocked is (not continuation) and not mgr.session.active
    assert owner.active and not owner.fault and not port.uncertain_output
    assert servo.cfg==initial
    assert owner.pid_history[-1]['strategy']=='validated_candidates'
    assert any('pid_integral_max' in row['values'] for row in owner.pid_history[-1]['changes'])

    if continuation:
        import json
        session=mgr.session;session.poll()
        assert session.continuation['state']=='awaiting_replan'
        deadline=session.deadline
        atomic_json(session.directory/'inbox'/'replan.json',dict(id='replan',action='replan',based_on=session.decision_revision,
                    candidate=dict(p['candidate'],kp_count_per_nm=-2900.)))
        session.poll()
        assert session.segment==1 and session.deadline==deadline
        assert session.p['candidate']['kp_count_per_nm']==-2900.
        session.poll()
        assert session.active and session.epoch['context']==session.original_context
        assert list(session.directory.glob('segment-000-*-measurements.json'))


@pytest.mark.parametrize('outward_steps',[0,1,2,3])
def test_flexible_foreground_returns_from_each_intermediate(flexible,outward_steps):
    from ldcon.owners.flexible_native_trial import pid_from_candidate
    owner,servo,clock,call,begin,tick,root,grant,p=flexible
    initial=copy.deepcopy(servo.cfg);token=begin()['epoch']['token']
    trial=owner.pid_tuning
    call('background_pid_target',token=token,pid=pid_from_candidate(p['candidate']));tick(outward_steps)
    changed_at=len(trial.changed);started=clock()
    with pytest.raises(BrokerError,match='returning before retarget'):
        call('retarget',target_frequency_hz=2e14)
    for _ in range(20):
        tick()
        if owner.background.coordinator.epoch is None:break
    assert owner.background.coordinator.epoch is None
    assert len(trial.changed)-changed_at<=trial.maximum_return_steps
    assert clock()-started<=trial.return_bound_s<=20.
    assert servo.cfg==initial and owner.active


def test_flexible_grant_expiry_restores_bounds_without_model(flexible):
    from ldcon.owners.flexible_native_trial import pid_from_candidate
    owner,servo,clock,call,begin,tick,root,grant,p=flexible
    initial=copy.deepcopy(servo.cfg);token=begin()['epoch']['token']
    call('background_pid_target',token=token,pid=pid_from_candidate(p['candidate']));tick(4)
    grant['last_seen']=time.time()-1000.;atomic_json(root/'test'/'monitors'/'monitor.json',grant)
    tick(10)
    assert servo.cfg==initial and owner.active and not owner.fault
    assert call('background_status')['last']['reason']=='grant_revoked'


def test_flexible_cannot_select_unadmitted_runtime_values(flexible):
    from ldcon.owners.flexible_native_trial import pid_from_candidate
    owner,servo,clock,call,begin,tick,root,grant,p=flexible
    token=begin()['epoch']['token'];writes=len(servo.writes)
    with pytest.raises(BrokerError,match='admitted candidate'):
        call('background_pid_target',token=token,pid=pid_from_candidate(dict(p['candidate'],integral_limit_count=4.)))
    assert len(servo.writes)==writes and owner.active and not owner.fault


def test_flexible_proves_mixed_intermediate_envelopes(flexible):
    from ldcon.owners.flexible_native_trial import FlexibleNativeTrial
    owner,servo,clock,call,begin,tick,root,grant,p=flexible
    # A P-dominated endpoint and an I-dominated endpoint cannot mix their maxima.
    before=owner._read_before(require_input=True)
    before['servo_config']['pid_kp']=-25000.
    before['servo_config']['pid_ki']=-1000.
    before['servo_config']['pid_integral_min']=-.0001
    before['servo_config']['pid_integral_max']=.0001
    trial=FlexibleNativeTrial(owner,before,dict(kp_count_per_nm=-1000.,ki_count_per_nm_s=-1000.,integral_limit_count=2.),20.)
    paths=trial._plan(trial.original,trial.candidate_cfg)
    assert paths[0]['values'].get('pid_kp') is not None
    assert paths[-1]['values'].get('pid_integral_max') is not None
    for row in paths:
        proof=trial._proof(row['config'])
        assert proof['code_bounds'][0]>=0 and proof['code_bounds'][1]<=65535
        assert len(trial._plan(row['config'],trial.original))<=trial.maximum_return_steps


def test_flexible_ignores_unrelated_telemetry_setting_changes(flexible):
    from ldcon.owners.flexible_native_trial import pid_from_candidate
    owner,servo,clock,call,begin,tick,root,grant,p=flexible
    servo.cfg['log_adc_change_threshold']=32
    token=begin()['epoch']['token']
    call('background_pid_target',token=token,pid=pid_from_candidate(p['candidate']))
    servo.cfg['log_adc_change_threshold']=64
    tick(5)
    assert owner.active and not owner.fault and owner.pid_tuning.state()['ready']


@pytest.mark.parametrize('raw_kind',['saturated','missing','outside'])
def test_validated_comparison_measures_saturation_but_not_invalid_integral(flexible,tmp_path,raw_kind):
    from ldcon.jobs.native_background_session import manager
    from ldcon.owners.esp32_native_client import NativeCurrentPort
    import json
    owner,servo,clock,call,begin,tick,root,grant,p=flexible
    write=servo.set_control
    def clamped_write(values):
        write(values)
        if raw_kind=='saturated':servo.status['pid_integral']=servo.cfg['pid_integral_max']
    servo.set_control=clamped_write
    p.update(window_s=4.,min_samples=24,timeout_s=30.)
    grant['bounds']['pid_parameters']={k:v for k,v in p.items() if k!='candidate'}
    atomic_json(root/'test'/'monitors'/'monitor.json',grant)
    port=NativeCurrentPort.__new__(NativeCurrentPort)
    port.policy=owner.policy;port.lease=owner.lease;port.uncertain_output=False
    port._call=lambda action,**kwargs:owner.dispatch(dict(device_id=servo.device_id,action=action,**kwargs))
    job=tmp_path/'deploy'/'jobs'/'controller';job.mkdir(parents=True)
    mgr=manager(job,port,dict(id='test'),clock=clock,emit=lambda *a:None,poll_interval_s=.2)
    identity=dict(monitor_id='monitor',generation='one',grant_revision='rev',handoff_timeout_s=20.,expected_operating_mode_revision=0)
    atomic_json(mgr.directory/'adaptive-experiment.json',dict(id='saturation',family='pi',pid_parameters=p,background=identity))
    initial=copy.deepcopy(servo.cfg)
    for _ in range(240):
        servo.status['pid_integral']={'saturated':servo.cfg['pid_integral_max'],
                                      'missing':None,'outside':2*servo.cfg['pid_integral_max']}[raw_kind]
        tick();state=owner.snapshot()
        changed=servo.cfg['pid_ki']!=initial['pid_ki']
        mgr.poll(row=mgr.row(state,state['optical'],2e6 if changed else 8e6,1e7))
        if mgr.session and mgr.session.blocked:break
    assert not owner.fault and owner.active and not port.uncertain_output
    assert mgr.session.blocked and not mgr.session.active
    if raw_kind=='saturated':
        assert owner.pid_history[-1]['status']=='succeeded'
        assert servo.cfg['pid_ki']==p['candidate']['ki_count_per_nm_s']
        measured=list(mgr.session.directory.glob('*-measurements.json'))
        assert len(measured)==2
        for path in measured:
            value=json.loads(path.read_text())
            assert value['summary']['integral_saturation_fraction']==1.
            assert value['summary']['maximum_integral_usage']==1.
        assert 'PI/integral candidate' in mgr.session.comparison_reason
    else:
        assert servo.cfg==initial
        assert not any(h['changes'] for h in owner.pid_history)


def test_legacy_factor_comparison_keeps_saturation_veto(background,tmp_path):
    from ldcon.jobs.native_background_session import NativeBackgroundManager
    owner,servo,clock,call,begin,tick,root,grant,p=background
    begin();servo.status['pid_integral']=servo.cfg['pid_integral_max']
    tick();state=owner.snapshot()
    row=NativeBackgroundManager.row(None,state,state['optical'],0.,1e7)
    assert row['pid_tuning']['eligible'] is False
    assert row['pid_tuning']['integral_observation']['valid'] is True
