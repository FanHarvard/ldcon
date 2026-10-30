"""Offline host grants, owner PI transitions and final-retention receipts."""
import copy
import hashlib
from pathlib import Path
import socket
import sys
import time
from types import SimpleNamespace

import pytest
from experiment_core.background import BackgroundCoordinator
from experiment_core.records import atomic_json
from ldcon.agents.measurement_grant import MeasurementGrant
from ldcon.core.adjustment_registry import candidate as parameter_candidate
from ldcon.core.contracts import DeviceError
from ldcon.jobs.background_pid_session import background_parameters
from ldcon.jobs.change_measure_parameters import ParameterMeasurementAdapter
from ldcon.owners.background_grants import read_grant
from ldcon.owners import ecdl_background_pid

sys.path.insert(0,str(Path(__file__).resolve().parent))
from test_ecdl_independent_pid import fixture as atomic_fixture
from test_native_background import rig


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*a,**k):raise AssertionError('No device/network access in these tests')
    monkeypatch.setattr(socket.socket,'connect',forbidden)
    monkeypatch.setattr(socket,'create_connection',forbidden)


def files(tmp_path,monkeypatch,bounds,laser='test'):
    root=tmp_path/'authority';root.mkdir(exist_ok=True)
    monkeypatch.setenv('LDCON_ADAPTIVE_ROOT',str(root))
    jobs=tmp_path/'deployment'/'jobs';child=jobs/'measurement';controller=jobs/'controller'
    child.mkdir(parents=True);controller.mkdir()
    atomic_json(child/'invocation.json',dict(measurement_plan=bounds,measurement_controller_job_id='controller'))
    return root,child,controller


def bounds():
    p=background_parameters({'parameters':{}},dict(strategy='validated_candidates',save_settings=False))
    return dict(enabled=True,families=['pi'],pid_parameters=p,handoff_timeout_s=30.,timeout_s=300.)


class AtomicLock:
    """Real ECDL epoch/PI adapter, fake native datagram transport only."""
    def __init__(self,n):
        self.n=n;self.session=SimpleNamespace(lease='controller-lease')
        n.o.lease=self.session.lease;n.o.laser={'id':'test'}
        n.background=BackgroundCoordinator(n.o.clock)
        n.background_context=lambda obs:dict(system_id='test',target=n.target,lease=n.o.lease)
        self.calls=[]

    def background_status(self):
        return dict(self.n.background.status(),owner_fault=self.n.o.fault,
            supported_adjustments=['pi'],capabilities={'pi':dict(supported=True,completion_mode='verified_handover')})

    def background_begin(self,**request):
        self.calls.append('begin')
        return ecdl_background_pid.begin(self.n,request,self.n.background_context({}),request['identity'],request['poll_interval_s'])

    def _dispatch(self,action,**request):
        self.calls.append(action)
        return ecdl_background_pid.dispatch(self.n,dict(action=action,**request),self.n.background_context({}),self.n.read_hardware())

    def background_pid_target(self,**request):return self._dispatch('background_pid_target',**request)
    def background_pid_finish(self,**request):return self._dispatch('background_pid_finish',**request)
    def background_restore(self,**request):return self._dispatch('background_restore',**request)


def atomic_adapter(tmp_path,monkeypatch):
    n,_=atomic_fixture(tmp_path,monkeypatch)
    lock=AtomicLock(n)
    movement=SimpleNamespace(locked=True,lock=lock,info=lambda:dict(servo_config=copy.deepcopy(n.config)))
    root,child,controller=files(tmp_path,monkeypatch,bounds())
    adapter=ParameterMeasurementAdapter(movement,{'id':'test'},child,controller,
        clock=n.o.clock,sleep=lambda delay:None,cancel=lambda:False,emit=lambda *a:None)
    return adapter,n,lock,root


def test_atomic_owner_change_readback_and_retention(tmp_path,monkeypatch):
    a,n,lock,root=atomic_adapter(tmp_path,monkeypatch)
    before=copy.deepcopy(n.config)
    plan=a.preflight([dict(parameter='kp_count_per_nm',value=before['pid_kp']*1.5)],'atomic',{})
    assert not n.servo.requests
    receipt=a.apply(plan,lambda:None)
    assert receipt['status']=='applied' and receipt['mode']=='atomic'
    assert receipt['readback']['kp_count_per_nm']==pytest.approx(before['pid_kp']*1.5)
    assert len(n.servo.requests)==1 and n.config['pid_ki']==before['pid_ki']
    assert n.pid_tuning.last_receipt['dac_before']==n.pid_tuning.last_receipt['dac_after']
    result={'status':'succeeded'};a.finish(result)
    final=result['parameter_completion']
    assert final['epoch'] is None and final['last']['state']=='adopted'
    assert lock.calls==['begin','background_pid_target','background_pid_finish']
    assert n.config['pid_kp']==pytest.approx(before['pid_kp']*1.5)
    grant=list((root/'test'/'monitors').glob('*.json'))[0]
    from ldcon.core.records import read_json
    assert read_json(grant)['enabled'] is False


def test_measurement_incomplete_keeps_requested_setting(tmp_path,monkeypatch):
    a,n,lock,_=atomic_adapter(tmp_path,monkeypatch)
    p=a.preflight([dict(parameter='ki_count_per_nm_s',offset=n.config['pid_ki']*.1)],'coordinated',{})
    a.apply(p,lambda:None);r={'status':'measurement_incomplete'};a.finish(r)
    assert r['parameter_completion']['last']['state']=='adopted'
    assert 'background_restore' not in lock.calls


def test_unknown_write_never_retries_or_restores(tmp_path,monkeypatch):
    a,n,lock,_=atomic_adapter(tmp_path,monkeypatch)
    p=a.preflight([dict(parameter='kp_count_per_nm',offset=n.config['pid_kp']*.1)],'atomic',{})
    n.servo.mode='timeout'
    with pytest.raises(DeviceError,match='Uncertain atomic'):a.apply(p,lambda:None)
    a.finish({'status':'failed','physical_outcome':'unknown'})
    assert lock.calls==['begin','background_pid_target'] and len(n.servo.requests)==1
    assert n.o.fault


def test_mixed_families_and_missing_grant_reject_before_effect(tmp_path,monkeypatch):
    a,n,lock,_=atomic_adapter(tmp_path,monkeypatch)
    with pytest.raises(DeviceError,match='split'):
        a.preflight([dict(parameter='kp_count_per_nm',value=1),dict(parameter='current',value=.5)],'atomic',{})
    atomic_json(a.directory/'invocation.json',{})
    with pytest.raises(DeviceError,match='frozen approved'):
        a.preflight([dict(parameter='kp_count_per_nm',value=1)],'atomic',{})
    assert not lock.calls and not n.servo.requests


def test_grants_do_not_replace_monitors_bind_real_controller_and_lease(tmp_path,monkeypatch):
    b=bounds();root,child,controller=files(tmp_path,monkeypatch,b)
    monitor=root/'test'/'controller'/'background-control.json';monitor.parent.mkdir(parents=True)
    sentinel={'enabled':True,'monitor_id':'active-monitor'};atomic_json(monitor,sentinel)
    g=MeasurementGrant(child,controller,'test',b,lease_sha256=hashlib.sha256(b'lease').hexdigest())
    assert read_grant('test',g.identity,lease='lease')==b
    from ldcon.core.records import read_json
    assert read_json(monitor)==sentinel
    with pytest.raises(ValueError,match='lease was replaced'):read_grant('test',g.identity,lease='new-lease')
    with pytest.raises(ValueError,match='identity differs'):
        read_grant('test',dict(g.identity,controller_job_id='different-controller'),lease='lease')
    with pytest.raises(ValueError,match='unresolved'):MeasurementGrant(child,controller,'test',b,lease_sha256='0'*64)
    g.close()
    with pytest.raises(ValueError,match='revoked'):read_grant('test',g.identity,lease='lease')
    assert read_json(monitor)==sentinel
    atomic_json(child/'invocation.json',dict(measurement_plan=b,measurement_controller_job_id='other'))
    with pytest.raises(ValueError,match='actual controller'):MeasurementGrant(child,controller,'test',b,lease_sha256='0'*64)


def test_native_coordinated_change_uses_real_owner_grant_and_readback(rig,tmp_path,monkeypatch):
    owner,servo,clock,bias,optical,gate,call,ready=rig
    ready();call('arm');servo.status['pid_integral']=0.
    b=bounds();root,child,controller=files(tmp_path,monkeypatch,b)
    before=copy.deepcopy(servo.cfg)
    class Port:
        lease=owner.lease
        def _pid_read(self,action):return call(action)
        def mutate(self,action,**values):return call(action,**values)
    movement=SimpleNamespace(locked=True,port=Port())
    def tick(delay):
        call('heartbeat');clock.advance();owner.poll()
    a=ParameterMeasurementAdapter(movement,{'id':'test'},child,controller,
        clock=clock,sleep=tick,cancel=lambda:False,emit=lambda *a:None)
    changes=[dict(parameter='ki_count_per_nm_s',value=before['pid_ki']*.99)]
    with pytest.raises(DeviceError,match='Atomic PI handover'):a.preflight(changes,'atomic',{})
    p=a.preflight(changes,'coordinated',{})
    receipt=a.apply(p,lambda:None)
    assert receipt['mode']=='coordinated' and servo.cfg['pid_ki']==pytest.approx(before['pid_ki']*.99)
    r={'status':'succeeded'};a.finish(r)
    assert r['parameter_completion']['last']['state']=='adopted'
    assert owner.active and not owner.fault and owner.saved_pid is None
    assert servo.cfg['target']==before['target']


def test_signed_ff_candidate_and_nonfinite_rejection():
    assert parameter_candidate({'current_per_temperature':-.07},'feedforward')['current_per_temperature']==-.07
    with pytest.raises(ValueError):parameter_candidate({'current_per_temperature':float('nan')},'feedforward')
    with pytest.raises(ValueError):parameter_candidate({'tec_gain':-.1},'slow_loop')


def test_worker_loss_restores_atomic_pi_without_optical_settling(tmp_path,monkeypatch):
    a,n,lock,_=atomic_adapter(tmp_path,monkeypatch)
    before=copy.deepcopy(n.config)
    p=a.preflight([dict(parameter='kp_count_per_nm',offset=n.config['pid_kp']*.1)],'atomic',{})
    a.apply(p,lambda:None)
    a.grant.close()  # identical owner handling for timed-out/crashed host grants
    n.servo.dac=32000
    result=ecdl_background_pid.poll_measurement_grant(n)
    assert result['epoch'] is None and result['last']['state']=='restored'
    assert n.config['pid_kp']==before['pid_kp']
    assert n.pid_tuning.last_receipt['dac_after']==32000
    assert n.active and not n.o.fault


def test_expired_measurement_fault_does_not_issue_return_write(tmp_path,monkeypatch):
    a,n,lock,_=atomic_adapter(tmp_path,monkeypatch)
    p=a.preflight([dict(parameter='kp_count_per_nm',offset=n.config['pid_kp']*.1)],'atomic',{})
    a.apply(p,lambda:None)
    n.background.epoch['expires_at']=n.o.clock()-1
    n.o.fault='uncertain unrelated effect';count=len(n.servo.requests)
    result=ecdl_background_pid.poll_measurement_grant(n)
    assert result['epoch']['state']=='reconciliation_required'
    assert len(n.servo.requests)==count
    a.grant.close()


def test_runtime_ff_transaction_negative_coefficient_and_bounds(tmp_path,monkeypatch):
    from ldcon.core.adjustment_registry import parameters
    plan=parameters(dict(objective='frequency_rms_hz',window_s=2.,min_samples=3,settle_s=0.,
        max_sample_gap_s=1.,timeout_s=60.,minimum_improvement_fraction=.01,
        maximum_cadence_fraction=.1,minimum_tec_excursion_v=.001),'feedforward')
    b=dict(enabled=True,families=['feedforward'],adjustment_parameters={'feedforward':plan},
        handoff_timeout_s=20.,timeout_s=60.)
    root,child,controller=files(tmp_path,monkeypatch,b)
    class Lock:
        session=SimpleNamespace(lease='ff-lease')
        value=-.07
        epoch=None
        last=None
        writes=[]
        def background_status(self):
            return dict(generation=1,epoch=self.epoch,last=self.last,owner_fault=None,reservations=[],
                actual_parameters={'current_per_temperature':self.value},supported_adjustments=['feedforward'],
                capabilities={'feedforward':dict(supported=True)},
                parameter_adjustments={'feedforward':{'bounds':{'current_per_temperature':[-.1,-.01]}}})
        def background_begin(self,**request):
            assert read_grant('test',request['identity'],lease=self.session.lease)==b
            assert {k:v for k,v in request['parameters'].items() if k!='candidate'}==plan
            self.epoch=dict(token='finite-epoch',state='active',identity=request['identity'])
            self.target=request['parameters']['candidate']['current_per_temperature']
            return self.background_status()
        def background_parameters_target(self,token):
            assert token==self.epoch['token']
            read_grant('test',self.epoch['identity'],lease=self.session.lease)
            self.value=self.target;self.writes.append(self.value)
            return self.background_status()
        def background_adopt(self,token):
            assert token==self.epoch['token']
            read_grant('test',self.epoch['identity'],lease=self.session.lease)
            self.last=dict(self.epoch,state='adopted');self.epoch=None
            return self.background_status()
    lock=Lock();movement=SimpleNamespace(locked=True,lock=lock)
    a=ParameterMeasurementAdapter(movement,{'id':'test'},child,controller,
        clock=time.monotonic,sleep=lambda _:None,cancel=lambda:False,emit=lambda *a:None)
    with pytest.raises(DeviceError,match='bounds'):
        a.preflight([dict(parameter='current_per_temperature',value=-.2)],'atomic',{})
    p=a.preflight([dict(parameter='current_per_temperature',value=-.08)],'atomic',{})
    assert a.apply(p,lambda:None)['readback']=={'current_per_temperature':-.08}
    result={'status':'succeeded'};a.finish(result)
    assert result['parameter_completion']['last']['state']=='adopted' and lock.writes==[-.08]


def test_shared_statistics_preserve_bias_and_do_not_impute_missing_samples():
    from ldcon.jobs.measurement_statistics import numeric_summary
    from ldcon.jobs.background_parameter_session import summarize
    values=[1.,2.,3.]
    common=numeric_summary(values)
    measured=numeric_summary([101.,102.,103.],reference=100.)
    assert measured['mean_offset_hz']==common['mean']
    assert measured['rms_from_target_hz']==common['rms']
    rows=[dict(acquired_s=i,error_hz=v,outputs={'piezo':30000.,'temperature':.5}) for i,v in enumerate(values)]
    result=summarize(rows,'frequency_rms_hz',30000.)
    assert result['statistics']['frequency_error_hz']==common
    assert result['frequency_rms_hz']==common['rms']
    assert common['rms']>common['stddev']
    for invalid in ([],[float('nan')],[True]):
        with pytest.raises(ValueError):numeric_summary(invalid)


def test_confirmed_prewrite_refusal_closes_epoch_without_blind_write(tmp_path,monkeypatch):
    from ldcon.core.instrument_client import BrokerError
    a,n,lock,_=atomic_adapter(tmp_path,monkeypatch)
    lock.session.uncertain_output=False
    def refuse(**request):raise BrokerError('range','Candidate refused before effect')
    lock.background_pid_target=refuse
    p=a.preflight([dict(parameter='kp_count_per_nm',offset=n.config['pid_kp']*.1)],'atomic',{})
    with pytest.raises(BrokerError) as caught:a.apply(p,lambda:None)
    assert a.physical_outcome_on_error(caught.value)=='not_applied'
    result={'status':'failed','physical_outcome':'not_applied'};a.finish(result)
    assert result['parameter_handoff']['last']['state']=='restored'
    assert not n.servo.requests and n.background.epoch is None


def test_generic_failure_is_never_classified_as_prewrite_refusal(tmp_path,monkeypatch):
    a,n,lock,_=atomic_adapter(tmp_path,monkeypatch)
    lock.session.uncertain_output=False
    def fail(**request):raise ValueError('Unknown failure, no owner proof')
    lock.background_pid_target=fail
    p=a.preflight([dict(parameter='kp_count_per_nm',offset=n.config['pid_kp']*.1)],'atomic',{})
    with pytest.raises(ValueError) as caught:a.apply(p,lambda:None)
    assert a.physical_outcome_on_error(caught.value)=='unknown'
    a.finish({'status':'failed','physical_outcome':'unknown'})
    assert 'background_restore' not in lock.calls
