"""Atomic PI changes, verified interruption and no legacy tuning-size budget."""
import copy
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from ldcon.core.contracts import DeviceError
from ldcon.jobs.background_pid_session import background_parameters
from ldcon.owners import ecdl_native_pid
from ldcon.owners.ecdl_independent_pid import IndependentPIDTuning
from ldcon.owners.flexible_native_trial import pid_from_config
from ldcon.owners.pi_handover import FIELDS, validate_request, validate_receipt

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_pi_trial_envelope_20261028 import Native, profile


class Servo:
    """Transport fixture. Real firmware arithmetic/parser are tested in C++."""
    def __init__(self,n):
        self.n=n;self.requests=[];self.dac=31000;self.error=1.e-5;self.mode='committed';self.receipt=None

    def get(self,path):
        assert path=='config'
        return copy.deepcopy(self.n.config)

    def set(self,values):
        assert set(values)=={'pi_handover'}
        r=values['pi_handover'];self.requests.append(copy.deepcopy(r))
        if r.get('query'):return {'pi_handover':copy.deepcopy(self.receipt)}
        if self.mode=='timeout':raise OSError('connection lost')
        before={k:self.n.config[k] for k in FIELDS}
        if self.mode=='rejected':return {'pi_handover':dict(request_id=r['request_id'],state='rejected',error='fresh native input required')}
        # A deliberately shifted offset ensures host restoration NEVER restores
        # the historical offset or DAC. Firmware independently proves feasibility.
        after=dict(before,pid_kp=r['kp'],pid_ki=r['ki'],pid_integral_min=-r['integral_limit_count']/abs(r['ki']),
            pid_integral_max=r['integral_limit_count']/abs(r['ki']),control_offset=before['control_offset']+17.)
        raw=(self.dac+.5-after['control_offset']-r['kp']*self.error)/r['ki']
        self.n.config.update(after)
        self.n.config['pi_handover_revision']+=1
        bounds=[after['control_offset']+r['kp']*(after[k]-after['target'])+i
                for k in ('system_min','system_max') for i in (-r['integral_limit_count'],r['integral_limit_count'])]
        reply=dict(request_id=r['request_id'],state='committed',revision=self.n.config['pi_handover_revision'],
            before=before,after=after,dac_before=self.dac,dac_after=self.dac,raw_integral_before=0.,
            raw_integral_after=raw,error_before=self.error,error_after=self.error,
            committed_uptime_ms=100001.,sample_age_ms=10.,envelope_min_count=min(bounds),envelope_max_count=max(bounds))
        if self.mode=='bad_dac':reply['dac_after']+=1
        if self.mode=='wrong_target':reply['after']['target']+=1
        if self.mode=='reboot':self.n.config['pi_handover_boot_id']='rebooted'
        self.receipt=copy.deepcopy(reply)
        if self.mode=='accepted':reply=dict(request_id=r['request_id'],state='accepted')
        return {'pi_handover':reply}


def fixture(tmp_path,monkeypatch,p_factor=1.,i_factor=1.,limit_factor=1.):
    monkeypatch.setattr(ecdl_native_pid,'adjustment_ready',lambda *a:True)
    n=Native(tmp_path,profile())
    n.t=100.;n.active=True;n.o.fault=None;n.o.recovery=None
    n.o.clock=lambda:n.t
    n.o.policies=lambda:{'piezo':SimpleNamespace(low=0.,high=65535.),
        'current':SimpleNamespace(resolution=.0001),'temperature':SimpleNamespace(resolution=.0001)}
    n.config.update(pid_multiplier=1,multiplier_control=0,multiplier_target=0,software_modulation_enable=0,
        dac_min_count=0,dac_max_count=65535,pi_handover_capability=1,pi_handover_boot_id='test-boot',
        pi_handover_revision=3,pi_handover_uptime_ms=100000.)
    n.servo=Servo(n)
    n.o.hw=lambda:SimpleNamespace(connections={'piezo':n.servo},snapshot=lambda:n.read_hardware())
    n.read_hardware=lambda:dict(servo_config=copy.deepcopy(n.config),outputs=dict(current=.1,temperature=.5))
    n.note_servo_config=lambda cfg:None
    n.persist=lambda:None
    p=background_parameters({'parameters':{}},dict(strategy='validated_candidates',save_settings=False,
        candidate=dict(kp_count_per_nm=n.config['pid_kp']*p_factor,ki_count_per_nm_s=n.config['pid_ki']*i_factor,
            integral_limit_count=20000.*limit_factor)),require_candidate=True)
    t=IndependentPIDTuning(n,p,period=.2,handoff_timeout_s=.001)
    return n,t


@pytest.mark.parametrize('pf,if_',[(2.,1.),(.5,1.),(1.,2.),(1.,.5),(1.5,.5),(.5,1.5)])
def test_large_independent_changes_and_current_output_restore(tmp_path,monkeypatch,pf,if_):
    n,t=fixture(tmp_path,monkeypatch,pf,if_)
    original=copy.deepcopy(n.config)
    assert not n.servo.requests
    t.target(t.candidate_pid)
    assert t.ready() and t.steps==1  # no 10-count ramp, .001 s budget is irrelevant
    assert n.config['pid_kp']==pytest.approx(original['pid_kp']*pf)
    assert n.config['pid_ki']==pytest.approx(original['pid_ki']*if_)
    assert t.last_receipt['dac_before']==t.last_receipt['dac_after']==31000
    n.servo.dac=31200  # PI ran since adoption; restore must hold THIS value
    t.target(t.original_pid)
    assert t.ready() and t.last_receipt['dac_after']==31200
    assert n.config['control_offset']!=original['control_offset']
    assert n.config['target']==original['target']
    assert pid_from_config(n.config)==pytest.approx(t.original_pid)
    t.finish('cancelled')
    assert t.closed and n.o.fault is None


@pytest.mark.parametrize('pf,if_,lf,reason',[(-1,1,1,'feedback sign'),(1,-1,1,'feedback sign'),(1,1,2,'physical PZT')])
def test_physical_constraints_remain(tmp_path,monkeypatch,pf,if_,lf,reason):
    with pytest.raises(ValueError,match=reason):fixture(tmp_path,monkeypatch,pf,if_,lf)


def test_absent_firmware_no_fallback_and_no_old_cap_dependency(tmp_path,monkeypatch):
    n,t=fixture(tmp_path,monkeypatch,1.5)
    n.p['online_pid'].pop('max_step_count');n.p['online_pid'].pop('minimum_gain_factor')
    IndependentPIDTuning(n,t.p)
    n.config.pop('pi_handover_capability')
    with pytest.raises(ValueError,match='firmware capability'):IndependentPIDTuning(n,t.p)
    assert not n.servo.requests


@pytest.mark.parametrize('mode',['timeout','bad_dac','wrong_target','reboot'])
def test_unknown_or_invalid_receipt_quarantines_without_replay(tmp_path,monkeypatch,mode):
    n,t=fixture(tmp_path,monkeypatch,1.5);n.servo.mode=mode
    before=copy.deepcopy(t.expected)
    with pytest.raises(DeviceError,match='Uncertain atomic'):t.target(t.candidate_pid)
    assert n.o.fault and t.expected==before and len(n.servo.requests)==1
    with pytest.raises(DeviceError,match='Owner fault'):t.target(t.candidate_pid)
    assert len(n.servo.requests)==1


def test_prewrite_rejection_and_receipt_only_poll(tmp_path,monkeypatch):
    n,t=fixture(tmp_path,monkeypatch,1.5);n.servo.mode='rejected'
    with pytest.raises(ValueError,match='rejected before effect'):t.target(t.candidate_pid)
    assert n.o.fault is None and t.at_checkpoint()
    n.servo.mode='accepted';t.target(t.candidate_pid)
    assert t.ready() and n.servo.requests[-1]['query'] is True
    assert sum('query' not in r for r in n.servo.requests)==2


def test_unadmitted_candidate_external_reference_and_adoption(tmp_path,monkeypatch):
    n,t=fixture(tmp_path,monkeypatch,1.5,.5)
    with pytest.raises(ValueError,match='Only the admitted'):t.target(dict(t.candidate_pid,kp=1.))
    n.config['target']+=.001
    with pytest.raises(ValueError,match='External native'):t.target(t.candidate_pid)
    n.config['target']=t.expected['target']
    saved=copy.deepcopy(n.o.device)
    t.target(t.candidate_pid);t.finish('succeeded')
    assert n.o.device==saved and n.p['p_sample']==pytest.approx(.15)
    assert n.p['g_sample']==pytest.approx(.25)
    with pytest.raises(DeviceError,match='runtime-only'):t.save(t.candidate_pid)
    IndependentPIDTuning(n,t.p)


def begin_epoch(n,t):
    from experiment_core.background import BackgroundCoordinator
    from ldcon.owners import ecdl_background_pid as adapter
    n.background=BackgroundCoordinator(n.o.clock)
    n.background_context=lambda obs:{'system_id':'fixture','target':n.target}
    request=dict(parameters=t.p,handoff_timeout_s=.001,expected_generation=n.background.generation)
    reply=adapter.begin(n,request,n.background_context({}),{'monitor_id':'test','generation':'test'},.1)
    return reply,n.pid_tuning


def test_restore_without_optical_input_or_settling(tmp_path,monkeypatch):
    from ldcon.owners.ecdl_background_pid import restore_atomic
    n,t=fixture(tmp_path,monkeypatch,1.5,.5)
    reply,t=begin_epoch(n,t)
    assert reply['epoch']['completion_mode']=='verified_handover' and reply['epoch']['timeout_s'] is None
    t.target(t.candidate_pid)
    n.read_feedback=lambda:(_ for _ in ()).throw(AssertionError('Optical pipeline must not be consulted'))
    n.t+=300  # not a historical 20 s return budget
    result=restore_atomic(n,'foreground')
    assert result['epoch'] is None and result['last']['state']=='restored'
    assert result['last']['receipt']['confirmed'] and t.closed
    assert 'return_deadline' not in result['last']


def test_foreground_reservation_completes_handoff_same_call(tmp_path,monkeypatch):
    from ldcon.owners.ecdl_steady_owner import NativeLockOwner
    n,t=fixture(tmp_path,monkeypatch,1.5)
    _,t=begin_epoch(n,t);t.target(t.candidate_pid)
    n.read_feedback=lambda:(_ for _ in ()).throw(AssertionError('No optical wait'))
    result=NativeLockOwner.reserve_background(n,['foreground-target'])
    assert result['foreground_ready'] and result['reservations']==['foreground-target']
    assert result['last']['receipt']['handover']['dac_before']==result['last']['receipt']['handover']['dac_after']


def test_restore_fenced_by_new_target_or_changed_current(tmp_path,monkeypatch):
    from ldcon.owners.ecdl_background_pid import restore_atomic
    from experiment_core.errors import StaleContext
    n,t=fixture(tmp_path,monkeypatch,1.5)
    _,t=begin_epoch(n,t);t.target(t.candidate_pid)
    n.target+=20e6
    assert restore_atomic(n,'foreground')['epoch']['state']=='reconciliation_required'
    assert len(n.servo.requests)==1 and n.background.epoch['state']=='reconciliation_required'


def test_projection_rejects_unbounded_or_mixed_requests(tmp_path,monkeypatch):
    n,t=fixture(tmp_path,monkeypatch,1.5);t.target(t.candidate_pid)
    request=n.servo.requests[0];cfg=dict(request['expected'],pi_handover_capability=1,
        pi_handover_boot_id=request['expected_boot_id'],pi_handover_revision=request['expected_revision'],pi_handover_uptime_ms=100000.)
    validate_request(request,cfg,0,65535)
    for changes in [dict(output_max_count=65536),dict(expected_revision=99),dict(ki=-1),dict(reset=1),dict(deadline_uptime_ms=110000)]:
        with pytest.raises(ValueError):validate_request(dict(request,**changes),cfg,0,65535)
    with pytest.raises(ValueError):validate_request(request,cfg,100,60000)
    validate_request(dict(version=1,request_id='x',query=True),{},0,1)


@pytest.mark.parametrize('mode',['verified_handover','deadline'])
def test_worker_handoff_uses_owner_completion_contract(mode):
    from ldcon.jobs.background_pid_session import BackgroundPIDSession
    now=[1.];seen=[]
    s=object.__new__(BackgroundPIDSession)
    s._continuation_waiting=lambda:False
    s.blocked=False;s.returning=None;s.active=True
    s.epoch=dict(token='fixture',completion_mode=mode)
    s.background=dict(handoff_timeout_s=20.)
    s.clock=lambda:now[0];s.decision_revision='one'
    s._status=lambda *a,**k:seen.append((a,k))
    s.lock=SimpleNamespace(background_restore=lambda *a:dict(epoch=None,last=dict(state='restored')))
    s._effect=lambda name,fn:fn()
    s._completed=lambda *a:seen.append('completed')
    s.preempt('foreground');now[0]=30.
    s._restore()
    assert ('completed' in seen)==(mode=='verified_handover')
    assert s.blocked==(mode=='deadline')


def test_actual_physical_projection_validates_atomic_payload(tmp_path,monkeypatch):
    from ldcon.owners.ecdl_projection import physical_validator,operation
    n,t=fixture(tmp_path,monkeypatch,1.5)
    baseline=n.servo.get('config');t.target(t.candidate_pid)
    r=n.servo.requests[0]
    validator=physical_validator(SimpleNamespace(get=lambda path:baseline),'piezo')
    projected=dict(axis_limits=dict(bounds=[0,65535]))
    validator(operation('set',[{'pi_handover':r}]),projected)
    with pytest.raises(ValueError,match='bounds'):
        validator(operation('set',[{'pi_handover':dict(r,output_min_count=-1)}]),projected)
    from experiment_core.errors import AuthorizationRequired
    with pytest.raises(AuthorizationRequired):
        validator(operation('set',[{'pi_handover':r,'pid_reset_state':1}]),projected)


def test_rejected_restore_blocks_foreground_without_retry_or_optical_wait(tmp_path,monkeypatch):
    from ldcon.owners.ecdl_background_pid import restore_atomic
    n,t=fixture(tmp_path,monkeypatch,1.5)
    _,t=begin_epoch(n,t);t.target(t.candidate_pid)
    n.servo.mode='rejected'
    result=restore_atomic(n,'foreground')
    assert not result['foreground_ready'] and result['epoch']['state']=='reconciliation_required'
    assert n.o.fault is None
    count=len(n.servo.requests)
    restore_atomic(n,'foreground')
    assert len(n.servo.requests)==count


def test_real_cpp_protocol_json_roundtrip_matches_python_owner():
    import json
    rows=json.loads((Path(__file__).parent/'fixtures/pi-handover-v1.json').read_text())
    assert len(rows)==2
    for row in rows:
        r=row['request']['pi_handover']
        if r['expected']['pid_multiplier']==1:
            validate_request(r,row['before_config'],r['output_min_count'],r['output_max_count'])
            validate_receipt(row['reply']['pi_handover'],r,row['after_config'])
        else:
            # Firmware supports both polarities; this ECDL adapter is explicitly +1.
            with pytest.raises(ValueError,match='active PI'):
                validate_request(r,row['before_config'],r['output_min_count'],r['output_max_count'])
