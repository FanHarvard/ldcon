"""No sockets: electrical transitions, durable inhibit and scoped card orchestration."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from threading import RLock
import time

import pytest
from experiment_core.records import digest
from ldcon.owners.emission import EmissionController, EmissionStore, validate_policy, monitor_sample
from ldcon.jobs.emission_card import parameters, run_emission
from ldcon.core.instrument_client import BrokerError


def policy(tmp_path):
    return dict(enabled=True, method='driver_enable', revision='fixture-only',
        current_bounds_a=[0.,.1], off_current_max_a=.001, on_current_bounds_a=[.04,.06],
        max_sample_age_s=2., verify_samples=3, verify_timeout_s=2., sample_interval_s=.01,
        current_uncertainty_a=.00001, interlock_file=str(tmp_path/'emission.sqlite3'))


class Adapter:
    def __init__(self):
        self.mutex=RLock();self.enabled=True;self.counter=0;self.calls=[];self.current=None
        self.stale=False;self.repeat=False;self.lost=False;self.busy=False;self.closed=False
    def preflight(self,p):pass
    def quiet(self,p):
        if self.busy:raise BrokerError('busy','PI remains active')
    def open(self,p):self.calls.append('claim')
    def snapshot(self,p):
        self.counter+=1
        return dict(current=dict(current_a=self.current if self.current is not None else (.05 if self.enabled else 0.),
            acquired_unix_s=time.time()-(10 if self.stale else 0),acquisition_id=0 if self.repeat else self.counter),
            tec=True,enabled=self.enabled)
    def validate_transition(self,p,desired,before):pass
    def apply(self,p,desired,permit):
        self.calls.append(desired);self.enabled=desired=='on'
        if self.lost:raise OSError('ack lost after write')
        return time.time()
    def verify_preserved(self,before,after,desired):assert after['tec']==before['tec']
    def close(self):self.closed=True


@pytest.fixture
def controller(tmp_path):
    p=policy(tmp_path);a=Adapter();s=EmissionStore(p['interlock_file'],'laser')
    c=EmissionController(s,lambda:p,a)
    yield c,a,p
    if c.thread:c.thread.join(3)
    s.close()


def reserve(c,p,state='off',rid='one'):
    return c.dispatch(dict(action='emission_reserve',request_id=rid,state=state,policy_revision=digest(p)))


def finish(c,rid='one'):
    c.dispatch(dict(action='emission_execute',request_id=rid));c.thread.join(3)
    assert not c.thread.is_alive()
    return c.store.status()


def test_electrical_off_on_no_optical_claim_and_no_replay(controller):
    c,a,p=controller
    reserve(c,p);assert c.store.status()['inhibited']
    result=finish(c)
    assert result['phase']=='output_disabled' and result['optical_state']=='unknown'
    assert len(result['evidence'])==3 and a.calls==['claim','off']
    with pytest.raises(BrokerError):c.store.require_control('claim')
    assert c.dispatch(dict(action='emission_execute',request_id='one'))==result
    assert reserve(c,p)==result and a.calls==['claim','off']
    reserve(c,p,'on','two');assert finish(c,'two')['phase']=='enabled_unlocked'
    assert not c.store.status()['inhibited'] and a.calls==['claim','off','claim','on']


def test_restart_does_not_replay_unknown_transition(controller):
    c,a,p=controller;reserve(c,p)
    c2=EmissionController(c.store,lambda:p,a)
    assert c2.store.status()['phase']=='unknown' and not a.calls
    assert c2.dispatch(dict(action='emission_execute',request_id='one'))['phase']=='unknown'
    with pytest.raises(BrokerError,match='existing'):reserve(c2,p,'on','new')


@pytest.mark.parametrize('failure',['stale','repeat','lost'])
def test_missing_evidence_or_uncertain_write_never_relights_or_releases(controller,failure):
    c,a,p=controller;p['verify_timeout_s']=.06;p['sample_interval_s']=.01
    setattr(a,failure,True);reserve(c,p);state=finish(c)
    assert state['phase']=='unknown' and state['inhibited']
    assert not a.closed and a.calls.count('off') <= 1 and 'on' not in a.calls
    finish(c);assert a.calls.count('off') <= 1


def test_busy_owner_is_waited_not_stopped_from_a_device_route(controller):
    c,a,p=controller;reserve(c,p);a.busy=True
    with pytest.raises(BrokerError,match='PI'):c.dispatch(dict(action='emission_execute',request_id='one'))
    assert not a.calls and c.store.status()['phase']=='quiescing'


def test_cancel_never_undoes_intent_to_old_on_state(controller):
    c,a,p=controller;reserve(c,p)
    c.dispatch(dict(action='emission_cancel',request_id='one'))
    assert c.store.status()['phase']=='cancelled' and c.store.status()['inhibited'] and not a.calls


def test_exact_request_policy_revision_and_scope(controller):
    c,a,p=controller;reserve(c,p)
    with pytest.raises(BrokerError):reserve(c,p,'on')
    with pytest.raises(BrokerError):c.dispatch(dict(action='emission_execute',request_id='other'))
    p['revision']='changed'
    with pytest.raises(BrokerError):c.dispatch(dict(action='emission_execute',request_id='one'))
    assert not a.calls


def test_cleanup_only_during_quiescing_and_token_never_public(controller):
    c,a,p=controller;reserve(c,p)
    assert 'permit' not in c.store.status()
    c.store.require_control('stop');c.store.require_control('locked_sweep_restore')
    with pytest.raises(BrokerError):c.store.require_control('steady_arm')
    token=c.store.private()['permit']
    assert not c.store.authorize_ld(token,False)
    c.store.update(phase='changing')
    assert c.store.authorize_ld(token,False) and not c.store.authorize_ld(token,True)
    assert not c.store.authorize_ld('forged',False)
    with pytest.raises(BrokerError):c.store.require_control('locked_sweep_restore')


def test_measurement_uncertainty_and_negative_zero_noise(controller):
    c,a,p=controller
    c.check_current(dict(current_a=-.000005,acquisition_id='x',acquired_unix_s=time.time()),p)
    with pytest.raises(BrokerError):c.check_current(dict(current_a=.1,acquisition_id='x',acquired_unix_s=time.time()),p)


def test_disabled_configuration_and_unknown_recipe_parameters(tmp_path):
    assert validate_policy({'enabled':False,'reason':'not wired'})['enabled'] is False
    p=policy(tmp_path);p['verify_samples']=False
    with pytest.raises(ValueError):validate_policy(p)
    with pytest.raises(ValueError):parameters({'parameters':{'state':'off'}},{'off_current_max_a':1})


def test_scope_transfer_is_not_an_acquisition():
    config=dict(endpoint='http://127.0.0.1:1234',device_id='scope',channel='CH2',
        calibration_revision='fixture',amperes_per_volt=.1,zero_voltage_v=.2,uncertainty_a=.0001)
    sample=dict(schema='ldcon.current-monitor/1',device_id='scope',channel='CH2',acquisition_time_known=True,
        valid=True,clipped=False,voltage_v=.7,acquired_unix_s=time.time(),acquisition_id='capture-1')
    assert monitor_sample(sample,config)['current_a']==pytest.approx(.05)
    for change in ({'acquisition_time_known':False},{'channel':'CH1'},{'clipped':True},{'valid':False}):
        with pytest.raises(BrokerError):monitor_sample({**sample,**change},config)


def test_card_reserves_before_stopping_only_its_laser_physical_jobs(tmp_path):
    p=policy(tmp_path);calls=[];state={'phase':'unmanaged'}
    rows=[{'id':i,'laser':{'id':l},'cards':[{'operation':op}]} for i,l,op in
        [('lock','laser','servo.native_current'),('peer','other','servo.ecdl_steady_lock'),
         ('monitor','laser','agent.monitor'),('resident','laser','agent.resident'),('job','laser','laser.emission')]]
    def call(action,**kw):
        calls.append(action)
        if action=='emission_reserve':state.update(phase='quiescing',request_id=kw['request_id'])
        if action=='emission_execute':state.update(phase='output_disabled',inhibited=True,optical_state='unknown')
        return dict(state)
    def api(method,path,body=None):
        calls.append(path)
        if method=='GET':return {'jobs':list(rows)}
        assert state['phase']=='quiescing' and path=='api/jobs/lock/stop'
        rows[:]=[r for r in rows if r['id']!='lock']
    owner=SimpleNamespace(policy=p,call=call)
    result=run_emission(tmp_path/'job',{'id':'laser'},{},{'state':'off','timeout_s':1},clock=time.monotonic,
        sleep=lambda _:None,cancel=lambda:False,emit=lambda *a:None,owner=owner,dashboard=SimpleNamespace(_request=api))
    assert result['status']=='succeeded' and calls[0]=='emission_reserve'
    assert [c for c in calls if '/stop' in c]==['api/jobs/lock/stop']


def test_cancel_racing_quiet_cannot_dispatch(controller):
    c,a,p=controller;reserve(c,p)
    a.quiet=lambda p:c.dispatch(dict(action='emission_cancel',request_id='one'))
    with pytest.raises(BrokerError):c.dispatch(dict(action='emission_execute',request_id='one'))
    assert not a.calls and c.store.status()['phase']=='cancelled'


def test_owner_emission_checks_global_enable_before_any_io():
    from ldcon.owners.emission_adapters import ECDLEmissionAdapter,NativeEmissionAdapter
    e=ECDLEmissionAdapter(SimpleNamespace(laser={'allow_output':False}))
    with pytest.raises(BrokerError,match='not enabled'):e.preflight({'method':'current_modulation'})
    n=NativeEmissionAdapter(SimpleNamespace(policy={'writes_enabled':False}))
    with pytest.raises(BrokerError,match='not enabled'):n.preflight({'method':'driver_enable'})


def test_real_cld_owner_rejects_bypass_and_allows_only_exact_ld_permit(tmp_path):
    import importlib.util
    spec=importlib.util.spec_from_file_location("cld_fixture",Path(__file__).with_name("test_cld_system_boundary.py"))
    fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)
    config,writes=fixture.config,fixture.writes
    from ldcon.owners.cld_owner import CLDOwner
    from ldcon.drivers.cld1015 import MockIO
    raw=MockIO();cfg=config(tmp_path,current=True)
    cfg['controller']['temperature_writes_enabled']=False
    cfg['controller']['ld_enable_writes_enabled']=True
    cfg['emission_interlock']={'path':str(tmp_path/'interlock.sqlite3'),'laser_id':'laser'}
    owner=CLDOwner(cfg,io_factory=lambda:raw,lock_factory=lambda resource:SimpleNamespace(close=lambda:None))
    owner.refresh();gate=owner.emission_interlock
    # rule M step 3: the operating conditions are the laser system owner's projection (temperature_writes_enabled
    # in the controller block above is a legacy copy, ignored at start)
    fixture.system(tmp_path,owner,fixture.cld_document(current=True)).client.install()
    gate.reserve('off','off','fixture')
    lease=owner.dispatch(dict(action='claim',instrument='controller',channel=0,profile='cld1015',resource=cfg['resource'],owner='test'))['lease']
    def call(method,**values):
        return owner.dispatch(dict(action='controller',instrument='controller',lease=lease,method=method,**values))
    try:
        with pytest.raises(BrokerError,match='inhibit'):call('enable',output='ld',enabled=True)
        with pytest.raises(BrokerError,match='inhibit'):call('apply',values={'current_a':.051})
        with pytest.raises(BrokerError,match='inhibit'):owner.dispatch(dict(action='bias_claim',instrument='controller'))
        assert writes(raw)==[]
        gate.update(phase='changing')
        call('enable',output='ld',enabled=False,emission_permit=gate.private()['permit'])
        assert writes(raw)==['OUTP1 0'] and raw.state['OUTP2']==1
        with pytest.raises(BrokerError):call('enable',output='tec',enabled=False,emission_permit=gate.private()['permit'])
        assert writes(raw)==['OUTP1 0']
    finally:owner.close()


def test_ecdls_dc_proof_rejects_remaining_dither_and_disabled_output():
    from ldcon.owners.emission_adapters import ECDLEmissionAdapter
    p={'modulation':{'readback_tolerance_v':1e-5}}
    dc=dict(enabled=True,load='HZ',polarity='NOR',waveform='DC',burst='OFF',modulation='OFF',sweep='OFF',value=.5)
    ECDLEmissionAdapter.verify_dc(dc,.5,p)
    for change in ({'waveform':'SINE'},{'enabled':False},{'burst':'ON'},{'value':.51},{'load':'50'}):
        with pytest.raises(BrokerError):ECDLEmissionAdapter.verify_dc({**dc,**change},.5,p)


def test_card_catalog_and_disabled_readiness():
    from ldcon.core.operation_registry import spec,admission_class
    from ldcon.core.interface_cards import INTERFACE_CATALOG
    from ldcon.jobs.emission_card import configuration
    laser=dict(id='laser',allow_output=True,interfaces={'lock':{'device':'rig'}},
               devices={'rig':{'type':'ecdl.rig.owner','control_settings':{}}})
    assert spec('laser.emission').execution=='job'
    assert admission_class(laser,{'operation':'laser.emission'})=='proxy'
    assert 'laser.emission' in INTERFACE_CATALOG
    with pytest.raises(ValueError,match='disabled'):configuration(laser)


def test_cancel_during_prewrite_reads_cannot_enable_output(controller):
    c,a,p=controller;reserve(c,p,'on')
    a.validate_transition=lambda *args:c.dispatch(dict(action='emission_cancel',request_id='one'))
    state=finish(c)
    assert state['phase']=='unknown' and state['inhibited']
    assert a.calls==['claim']


def test_shutdown_cancels_before_write_and_prevents_new_requests(controller):
    c,a,p=controller;reserve(c,p,'on')
    a.validate_transition=lambda *args:c.stopping.set()
    original=a.apply
    def apply(*args):
        c.check_cancelled()
        return original(*args)
    a.apply=apply
    state=finish(c)
    assert state['phase']=='unknown' and a.calls==['claim']
    assert c.shutdown()
    with pytest.raises(BrokerError,match='shutting down'):reserve(c,p,'on','new')


def test_owner_shutdown_before_dispatch_is_durable_unknown(controller):
    c,a,p=controller;reserve(c,p)
    assert c.shutdown()
    assert c.store.status()['phase']=='unknown' and c.store.status()['inhibited']
    assert not a.calls
