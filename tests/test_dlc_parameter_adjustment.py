from copy import deepcopy
import json
import math
from types import SimpleNamespace

import pytest

from ldcon.agents.dlc_parameter_adjustment import run
from ldcon.core.records import atomic_json
from ldcon.jobs.dlc_parameter_measurement import ParameterAdapter, candidate


class Clock:
    now = 1000.
    def __call__(self):return self.now
    def sleep(self, value):self.now += value


def measurement():
    return dict(changes=[], apply_mode='coordinated',
        measurements=dict(wavelength=True,optical_quality=True,dither=False),
        baseline_s=0.,wait_s=0.,duration_s=2.,history_wait_s=0.,minimum_samples=10,
        sample_interval_s=.1,measurement_max_age_s=2.,max_sample_gap_s=.5,timeout_s=15.)


class Adapter:
    def __init__(self, clock):
        self.clock = self.wall_clock = clock
        self.identity='task';self.target=3e14;self.family='current'
        self.origin={'current_ma':80.};self.current=deepcopy(self.origin)
        self.state={};self.calls=[];self.begin_count=0;self.close_reason='completed'
        self.check()
    def check(self, **kwargs):
        self.state.update(parameter_capabilities={'current':dict(supported=True),
            'feedforward':dict(supported=False,reason='No commissioned coefficient qualification')},
            parameter_settings={'current':deepcopy(self.current)},frequency=dict(acquired_unix_s=self.clock()))
    def select_family(self, family):assert family=='current'
    def begin(self):self.begin_count+=1
    def preflight(self, changes, mode, measurements):return dict(candidate={v['parameter']:v['value'] for v in changes})
    def apply(self, plan, sample):
        self.current=deepcopy(plan['candidate']);self.calls.append(('apply',deepcopy(self.current)))
        return dict(status='applied',readback=deepcopy(self.current),owner_result={'confirmed':True})
    def restore_checkpoint(self):
        self.current=deepcopy(self.origin);self.calls.append(('restore',deepcopy(self.current)))
        return dict(confirmed=True,readback=deepcopy(self.current),thermal_settled=False)
    def read(self, measurements):
        self.check()
        return {'wavelength':dict(valid=True,acquired_unix_s=self.clock(),values={'frequency_hz':self.target+1e6*math.sin(self.clock())}),
            'optical_quality':dict(valid=True,acquired_unix_s=self.clock(),values={'side_peak_height_max':.2+(self.current['current_ma']-80)*.1})}
    def close(self, adopt=False):
        if not adopt:self.current=deepcopy(self.origin)
        return dict(confirmed=True,state='adopted' if adopt else 'restored',identity=self.identity,
            settings=deepcopy(self.current),target_restored=True,reason=self.close_reason)


class Model:
    def __init__(self, clock, answers):
        self.deadline=clock()+1000.;self.profile={'max_turns':20};self.turns=1
        self.answers=iter(answers);self.prompts=[]
    def request(self, payload, tick):
        tick();self.turns+=1;self.prompts.append(payload['prompt'])
        return json.dumps(next(self.answers))


def decision(action, **kwargs):return dict(action=action,parameters=dict(reason='fixture rationale',**kwargs))


def execute(tmp_path, monkeypatch, answers, *, family='current', mode='auto_within_approved', adapter=None):
    clock=adapter.clock if adapter is not None else Clock();adapter=adapter or Adapter(clock)
    atomic_json(tmp_path/'invocation.json',dict(adjustment_plan=dict(kind='dlc_adjustment',family=family,
        measurements={'current':measurement(),'feedforward':measurement()})))
    (tmp_path/'laser.yaml').write_text('id: synthetic\n')
    monkeypatch.setattr('ldcon.agents.dlc_parameter_adjustment.export_evidence',lambda *a:(None,[]))
    model=Model(clock,answers);pdf=tmp_path/'fixture.pdf';pdf.write_bytes(b'no physical execution')
    result=run(tmp_path,dict(prompt='Assess current quality',adjustment_objective='',adoption_mode=mode),
        clock=clock,sleep=clock.sleep,cancel=lambda:False,emit=lambda *args:None,
        selected=(SimpleNamespace(root=tmp_path),{},None,None),decision_session=model,
        adapter=adapter,report_writer=lambda *args:pdf)
    return result,adapter,model


def test_auto_selection_repeated_pair_then_explicit_adoption(tmp_path,monkeypatch):
    result,adapter,model=execute(tmp_path,monkeypatch,[decision('choose',family='current'),
        decision('trial',candidate={'current_ma':79.9}),decision('adopt',trial_id=0),decision('adopt',trial_id=1)],family='auto')
    assert result['status']=='succeeded' and result['adopted']
    assert adapter.current=={'current_ma':79.9} and adapter.begin_count==1
    assert [r['role'] for r in result['measurements']]==['baseline','candidate','matched_baseline','confirmation','matched_baseline']
    assert [c[0] for c in adapter.calls]==['apply','restore','apply','restore','apply']
    assert result['trials'][1]['confirmation_of']==0
    assert all((tmp_path/r['path']).is_file() for r in result['measurements'])


def test_auto_finish_is_read_only(tmp_path,monkeypatch):
    result,adapter,_=execute(tmp_path,monkeypatch,[decision('finish')],family='auto')
    assert result['output_writes']==0 and result['status']=='succeeded'
    assert not adapter.begin_count and not adapter.calls


def test_unsupported_family_cannot_be_selected(tmp_path,monkeypatch):
    adapter=Adapter(Clock())
    with pytest.raises(ValueError,match='Three invalid'):
        execute(tmp_path,monkeypatch,[decision('choose',family='feedforward')]*3,family='auto',adapter=adapter)
    assert not adapter.begin_count and not adapter.calls


def test_changed_measurement_window_requires_new_comparison(tmp_path,monkeypatch):
    result,adapter,_=execute(tmp_path,monkeypatch,[decision('trial',candidate={'current_ma':79.9}),
        decision('observe',wait_s=1.,duration_s=3.),decision('adopt',trial_id=0),decision('finish')])
    assert result['status']=='succeeded' and not result['adopted']
    assert not result['trials'][0]['eligible']
    raw=json.loads((tmp_path/result['measurements'][-1]['path']).read_text())
    assert raw['parameters']['duration_s']==3 and raw['parameters']['wait_s']==1
    assert adapter.current==adapter.origin


def test_report_only_never_adopts(tmp_path,monkeypatch):
    result,adapter,_=execute(tmp_path,monkeypatch,[decision('trial',candidate={'current_ma':79.9}),
        decision('adopt',trial_id=0),decision('finish')],mode='report_only')
    assert not result['adopted'] and adapter.current==adapter.origin


def test_preempted_cleanup_not_success(tmp_path,monkeypatch):
    adapter=Adapter(Clock());adapter.close_reason='foreground_target'
    result,_,_=execute(tmp_path,monkeypatch,[decision('finish')],adapter=adapter)
    assert result['status']=='cancelled' and not result['adopted']


def test_candidate_schema_cannot_smuggle_target_or_nonfinite():
    with pytest.raises(ValueError):candidate('current',{'current_ma':80,'target_frequency_hz':3e14})
    with pytest.raises(ValueError):candidate('feedforward',{'ff_factor_ma_per_v':float('nan')})


def test_optical_acquisition_keeps_its_own_timestamp():
    adapter=object.__new__(ParameterAdapter)
    adapter.check=lambda **kw:None;adapter.measurement_missing=False
    adapter.state=dict(frequency=dict(acquired_unix_s=101.,frequency_hz=3e14),
        quality=dict(fresh=True,quality_valid=True,acquired_unix_s=100.,metrics={'side_peak_height_max':.2}))
    value=adapter.read({'wavelength':True,'optical_quality':True})
    assert value['wavelength']['acquired_unix_s']==101.
    assert value['optical_quality']['acquired_unix_s']==100.
    adapter.state['quality'].pop('acquired_unix_s')
    assert adapter.read({'optical_quality':True})['optical_quality']['acquired_unix_s'] is None


@pytest.fixture
def owner_adapter(rig,monkeypatch):
    from dataclasses import replace
    from ldcon.jobs.dlcpro_cards import Protocol
    from ldcon.core.contracts import MeasurementUnavailable
    from ldcon.core.instrument_client import BrokerError
    owner=rig['owner'];owner.start_lock(3e14,rig['control'])
    protocol=Protocol(owner,rig['device']);protocol.client='existing-lock'
    old_read=rig['source'].read_mode
    def read():
        value=old_read()
        return replace(value,frequency=replace(value.frequency,acquired_unix_s=rig['clock']()))
    rig['source'].read_mode=read
    rig['source'].samples_after=lambda after:[read().frequency] if rig['clock']()>after else []
    class Session:
        def __init__(self,device):pass
        def rpc(self,action,**kw):
            try:return protocol.dispatch(dict(action=action,laser_id=owner.laser_id,**kw))
            except MeasurementUnavailable as error:raise BrokerError('measurement',str(error)) from error
    monkeypatch.setattr('ldcon.jobs.dlc_pi_measurement.Session',Session)
    laser=deepcopy(rig['laser']);laser['allow_output']=True
    adapter=ParameterAdapter(laser,'attached-parameter-test',clock=rig['clock'],wall_clock=rig['clock'])
    return owner,adapter


# The shared fixture is entirely simulated and denies network use.
from .test_dlcpro_offline import rig


def test_adapter_uses_existing_owner_epoch_and_independent_quality_clock(owner_adapter,rig):
    owner,adapter=owner_adapter;original=rig['driver'].state['current']
    adapter.select_family('current');adapter.begin()
    receipt=adapter.apply(adapter.preflight([dict(parameter='current_ma',value=original+.1)],
        'coordinated',{'wavelength':True,'optical_quality':True}),lambda:None)
    assert receipt['owner_result']['confirmed'] and rig['driver'].state['current']==original+.1
    rig['clock'].sleep(.25)
    measured=adapter.read({'wavelength':True,'optical_quality':True})
    assert measured['optical_quality']['acquired_unix_s']==rig['clock']()
    restored=adapter.restore_checkpoint()
    assert restored['confirmed'] and rig['driver'].state['current']==original
    assert owner.mode=='lock' and adapter.close(False)['state']=='restored'


def test_adapter_foreground_target_is_never_restored(owner_adapter,rig):
    owner,adapter=owner_adapter
    adapter.select_family('current');adapter.begin()
    origin=adapter.origin['current_ma']
    adapter.apply(adapter.preflight([dict(parameter='current_ma',value=origin+.1)],'coordinated',{}),lambda:None)
    rig['clock'].sleep(.25)
    owner.set_target(3e14+1e6,owner.target_revision)
    receipt=adapter.close(False)
    assert receipt['state']=='restored' and receipt['reason']=='foreground_target'
    assert owner.target==3e14+1e6 and rig['driver'].state['current']==origin
    # The preemption receipt precedes the foreground target write; it is not
    # evidence that the later target was restored to the old value.
    assert owner.target!=adapter.origin_target


def test_adapter_unknown_write_does_not_restore_or_retry(owner_adapter,rig):
    from ldcon.core.contracts import DeviceError
    from ldcon.drivers.dlcpro import UncertainDLCWrite
    owner,adapter=owner_adapter
    adapter.select_family('current');adapter.begin()
    plan=adapter.preflight([dict(parameter='current_ma',value=adapter.origin['current_ma']+.1)],'coordinated',{})
    rig['driver'].fail=True
    with pytest.raises(UncertainDLCWrite):adapter.apply(plan,lambda:None)
    writes=len(rig['driver'].writes)
    with pytest.raises(DeviceError,match='Unknown physical effect'):adapter.close(False)
    assert len(rig['driver'].writes)==writes


def test_cleanup_race_reads_terminal_receipt_without_replaying_finish(owner_adapter,monkeypatch):
    from ldcon.core.instrument_client import BrokerError
    owner,adapter=owner_adapter
    adapter.select_family('current');adapter.begin()
    rpc=adapter.session.rpc;attempts=[]
    def revoked(action,**values):
        if action=='pi_trial_finish':
            attempts.append(action);owner.pi_trial.preempt('monitor_grant_revoked')
            raise BrokerError('configuration','PI trial is no longer active')
        return rpc(action,**values)
    monkeypatch.setattr(adapter.session,'rpc',revoked)
    result=adapter.close(adopt=True)
    assert result['state']=='restored' and result['reason']=='monitor_grant_revoked'
    assert attempts==['pi_trial_finish'] and adapter.token is None


def test_unknown_begin_is_never_claimed_unstarted(owner_adapter,monkeypatch):
    from ldcon.core.contracts import DeviceError
    owner,adapter=owner_adapter
    adapter.select_family('current');rpc=adapter.session.rpc
    def lost(action,**values):
        result=rpc(action,**values)
        if action=='pi_trial_begin':raise OSError('simulated response loss')
        return result
    monkeypatch.setattr(adapter.session,'rpc',lost)
    with pytest.raises(OSError):adapter.begin()
    assert owner.pi_trial.active and adapter.begin_pending and adapter.token is None
    with pytest.raises(DeviceError,match='unconfirmed'):adapter.close(False)


def test_clean_owner_stop_is_inconclusive_not_unknown(owner_adapter,tmp_path,monkeypatch):
    from ldcon.agents.resident_adjustment import HostProtocolStop
    owner,adapter=owner_adapter
    (tmp_path/'laser.yaml').write_text('id: synthetic\n')
    def stopped(*args,**kwargs):
        adapter.select_family('current');adapter.begin();adapter.close(False)
        raise ValueError('measurement window incomplete')
    monkeypatch.setattr('ldcon.agents.dlc_parameter_adjustment._run',stopped)
    with pytest.raises(HostProtocolStop) as result:
        run(tmp_path,{},clock=adapter.clock,sleep=lambda dt:None,cancel=lambda:False,emit=lambda *a:None,
            selected=None,decision_session=None,adapter=adapter)
    assert result.value.physical_outcome_known and not owner.pi_trial.active
    assert json.loads((tmp_path/'adjustment-known-stop.json').read_text())['owner_receipt']['fault'] is None


def test_generic_pi_uses_existing_optimizer_with_frozen_window(tmp_path,monkeypatch):
    clock=Clock();adapter=Adapter(clock)
    adapter.state['parameter_capabilities']['pi']=dict(supported=True)
    original=adapter.check
    def checked(**kw):
        original(**kw);adapter.state['parameter_capabilities']['pi']=dict(supported=True)
    adapter.check=checked;adapter.select_family=lambda family:None
    atomic_json(tmp_path/'invocation.json',dict(adjustment_plan=dict(kind='dlc_adjustment',family='pi',
        measurements={'pi':measurement()})))
    (tmp_path/'laser.yaml').write_text('id: synthetic\n')
    captured=[]
    def pi(*args,**kwargs):
        captured.append(kwargs['plan'])
        return dict(status='succeeded',adopted=False,message='PI fixture')
    monkeypatch.setattr('ldcon.agents.dlc_pi_adjustment.run',pi)
    result=run(tmp_path,{},clock=clock,sleep=clock.sleep,cancel=lambda:False,emit=lambda *a:None,
        selected=None,decision_session=Model(clock,[]),adapter=adapter)
    assert result['family']=='pi' and captured[0]['kind']=='dlc_pi_change_measure'
    assert captured[0]['measurement']['duration_s']==2.
    record=json.loads((tmp_path/'parameter-adjustment.json').read_text())
    assert record['status']=='succeeded' and record['pi_result_path']=='pi-adjustment.json'


def test_auto_selection_intersects_monitor_authority(tmp_path,monkeypatch):
    adapter=Adapter(Clock());adapter.monitor_families=['pi']
    with pytest.raises(ValueError,match='Three invalid'):
        execute(tmp_path,monkeypatch,[decision('choose',family='current')]*3,family='auto',adapter=adapter)
    assert adapter.begin_count==0 and not adapter.calls


def test_program_cleanup_preemption_never_restores_new_target(owner_adapter,monkeypatch):
    from ldcon.core.instrument_client import BrokerError
    from ldcon.core.interface_schema import digest
    owner,adapter=owner_adapter
    adapter.family='pi';adapter.target_program={'offsets_hz':[1e6,0],'max_cycles':1}
    adapter.begin()
    adapter.session.rpc('pi_trial_target_step',token=adapter.token,
        expected_revision=owner.target_revision,index=0,cycle=0)
    rpc=adapter.session.rpc;commands=[]
    def preempted(action,**values):
        if action=='pi_trial_restore_target':
            commands.append(action);owner.pi_trial.preempt('foreground_target')
            owner.target=3e14+2e6;owner.target_revision+=1
            raise BrokerError('configuration','Epoch was preempted')
        if action=='pi_trial_finish':commands.append(action)
        return rpc(action,**values)
    monkeypatch.setattr(adapter.session,'rpc',preempted)
    receipt=adapter.close(False)
    assert receipt['state']=='restored' and owner.target==3e14+2e6
    assert commands==['pi_trial_restore_target'] and adapter.token is None
