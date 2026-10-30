"""No network: one physical split waveform and conservative timing/restore."""
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
from types import SimpleNamespace

import pytest

from .test_dlcpro_offline import rig
from ldcon.owners.dlcpro_awg_plan import compile_record
from ldcon.owners.dlcpro_awg_sweep import Sweep
from ldcon.drivers.dlcpro_analog import inspect


def config():
    return dict(commissioned=True,internal_ff_arc_verified=True,
        wiring='one_awg_output_split_to_two_fine_inputs',pzt_input='fine1',target_input='fine2',
        pzt_v_per_input_v=1.,frequency_hz_per_input_v=1e8,adc_zero_v=0.,
        adc_tolerance_v=.001,adc_max_interval_s=.1,sample_rate_hz=4000.,
        arm_timeout_s=3.,max_duration_s=10.,max_half_span_hz=100e6,watchdog_s=2.,
        awg=dict(role='dlc_pzt',resource='TCPIP::offline.invalid::INSTR',channel=1,
            profile='siglent_sdg2000x',min_v=-1.,max_v=1.,max_vpp=2.,
            broker=dict(endpoint='http://127.0.0.1:1',instrument='test'),
            reference='absolute',load='HZ',idle_v=0.))


def request():
    p=dict(schema='ldcon.triggered-staircase/v1',axis='servo_modulation',unit='Hz',
        reference='initial_offset',shape='staircase',levels=[0.,10e6,0.,-10e6,0.],
        dwell_s=.4,settle_s=.1,acquire_s=.3,lead_s=.1,tail_s=.1,duration_s=2.2,
        trigger_mode='external',trigger_policy='one_edge_starts_entire_record',
        frequency_hz_per_unit=None)
    return dict(schema=p['schema'],request_id='a'*32,plan=p,allow_simulated=False,
        plan_sha256=hashlib.sha256(json.dumps(p,sort_keys=True,separators=(',',':')).encode()).hexdigest())


class FakePort:
    def __init__(self,*args):self.restored=False;self.closed=False;self.fail=False
    def arm(self,*args):return dict(trigger_source='EXT')
    def restore(self):
        if self.fail:raise RuntimeError('uncertain write')
        self.restored=True
    def close(self):self.closed=True


def make(rig,monkeypatch):
    o=rig['owner'];d=rig['driver']
    for axis in ('pc','cc','tc'):
        for field,value in dict(enabled=False,signal=-3,factor=0.).items():
            d.values[f'laser1:dl:{axis}:external-input:{field}']=value
    d.values['io:fine-1:value-act']=d.values['io:fine-2:value-act']=0.
    original=d.snapshot
    def snap(channel):
        value=original(channel);value['current']['total_setpoint']=value['current']['setpoint'];return value
    monkeypatch.setattr(d,'snapshot',snap)
    o.start_lock(3e14,rig['control'])
    o.observation.samples_after=lambda after:[]
    sweep=Sweep(o,config(),object(),port_factory=FakePort,sleep=rig['clock'].sleep)
    return o,sweep,d


def test_plan_bounds_and_external_only(rig,monkeypatch):
    o,s,d=make(rig,monkeypatch)
    record,proof=compile_record(config(),request(),o.snapshot(),o.policy)
    assert proof['pzt_envelope_v']==pytest.approx([69.9,70.1])
    assert record.metadata['duration_s']==pytest.approx(2.2)
    p=config();p['pzt_v_per_input_v']=1e5
    with pytest.raises(ValueError,match='PZT'):compile_record(p,request(),o.snapshot(),o.policy)
    p=config();p['internal_ff_arc_verified']=False
    with pytest.raises(ValueError,match='commissioning'):compile_record(p,request(),o.snapshot(),o.policy)


def test_analog_has_only_host_time_brackets(rig):
    values=inspect(rig['driver'],1,clock=rig['clock'])
    assert values['inputs']['fine2']['acquisition_timestamp_available'] is False
    assert values['inputs']['fine2']['started_s']==100.


def test_whole_record_one_trigger_and_resume(rig,monkeypatch):
    o,s,d=make(rig,monkeypatch);r=s.begin(request(),o.target_revision)
    assert r['ready'] and o.mode=='awg_sweep'
    before=len(d.writes)
    # External source: no software fire method exists on this fake transport.
    for index in range(52):
        rig['clock'].sleep(.05);o.heartbeat()
        elapsed=(index+1)*.05
        d.values['io:fine-2:value-act']=.1 if .5<=elapsed<.9 else -.1 if 1.3<=elapsed<1.7 else 0.
        if s.active():s.heartbeat(r['token']);s.tick()
    assert s.last['verdict']=='succeeded' and s.last['restored']
    assert o.mode=='lock' and o.target==3e14
    assert s.last['trigger_bounds_s'][1]-s.last['trigger_bounds_s'][0]<=.1
    assert all(':external-input:' in key for key,_ in d.writes[before:])


def test_no_trigger_timeout_is_not_playback(rig,monkeypatch):
    o,s,d=make(rig,monkeypatch);r=s.begin(request(),o.target_revision)
    for _ in range(65):
        rig['clock'].sleep(.05);o.heartbeat()
        if s.active():s.heartbeat(r['token']);s.tick()
    assert not s.last['playback_completed'] and s.last['verdict']=='cancelled'
    assert s.last['restored'] and o.mode=='lock'


def test_unknown_restore_retains_port_and_fault(rig,monkeypatch):
    o,s,d=make(rig,monkeypatch);s.begin(request(),o.target_revision)
    p=s.port;p.fail=True
    with pytest.raises(RuntimeError):s.finish('cancelled','user cancelled')
    assert o.fault and not p.closed and s.port is p and not s.last['restored']
    assert s.last['stop_verdict']=='cancelled' and s.last['stop_reason']=='user cancelled'
    assert s.last['cleanup_error']==dict(type='RuntimeError',reason='uncertain write')


def test_no_old_target_restore(rig,monkeypatch):
    o,s,d=make(rig,monkeypatch);s.begin(request(),o.target_revision)
    o.target+=1e6;o.target_revision+=1
    s.finish('cancelled','superseded')
    assert o.target==3e14+1e6 and s.last['restored'] and o.mode=='idle'


def test_wrong_interior_plateau_cannot_complete(rig,monkeypatch):
    o,s,d=make(rig,monkeypatch);r=s.begin(request(),o.target_revision)
    for index in range(52):
        rig['clock'].sleep(.05);o.heartbeat()
        elapsed=(index+1)*.05
        # First plateau and last zero are correct; a middle zero is wrong.
        d.values['io:fine-2:value-act']=.1 if .5<=elapsed<.9 else .05 if 1.05<=elapsed<1.2 else -.1 if 1.3<=elapsed<1.7 else 0.
        if s.active():s.heartbeat(r['token']);s.tick()
    assert s.last['verdict']=='failed' and not s.last['playback_completed']
    assert s.last['restored'] and not o.fault


def test_read_only_adc_loss_cleans_up_without_unknown_write(rig,monkeypatch):
    o,s,d=make(rig,monkeypatch);s.begin(request(),o.target_revision)
    p=s.port
    def missing():raise OSError('read only')
    monkeypatch.setattr(s,'_read',missing)
    rig['clock'].sleep(.1);s.tick()
    assert s.last['verdict']=='failed' and s.last['outputs_uncertain'] is False
    assert p.restored and p.closed and o.mode=='lock' and not o.fault


def test_owner_fault_never_resumes_pi(rig,monkeypatch):
    o,s,d=make(rig,monkeypatch);s.begin(request(),o.target_revision)
    o.fault='readback fault'
    rig['clock'].sleep(.1);s.tick()
    assert o.mode=='idle' and s.last['pi_resumed'] is False
    assert s.last['verdict']=='failed' and s.last['restored']


def test_nan_prearm_rejected_without_writes(rig,monkeypatch):
    o,s,d=make(rig,monkeypatch)
    d.values['io:fine-2:value-act']=float('nan');before=list(d.writes)
    with pytest.raises(ValueError,match='zero'):s.begin(request(),o.target_revision)
    assert d.writes==before and o.mode=='lock' and not s.active()


def test_nan_after_awg_arm_never_enables_arc(rig,monkeypatch):
    o,s,d=make(rig,monkeypatch)
    monkeypatch.setattr(s,'_read',lambda:dict(value_v=float('nan')))
    with pytest.raises(ValueError,match='idle'):s.begin(request(),o.target_revision)
    assert not any(key.endswith(':external-input:enabled') and value for key,value in d.writes)
    assert not o.fault and s.last['outputs_uncertain'] is False
    assert s.last['verdict']=='failed' and o.mode=='lock'


def test_source_arrays_collected_before_ring_rollover(rig,monkeypatch):
    o,s,d=make(rig,monkeypatch)
    from ldcon.core.contracts import FrequencySample
    ring=[]
    o.observation.samples_after=lambda after:[x for x in ring if x.acquired_s>after]
    s.begin(request(),o.target_revision)
    for index in range(6):
        rig['clock'].sleep(.05);o.heartbeat()
        row=FrequencySample(index,3e14,rig['clock'](),rig['clock'](),'valid','fake')
        ring[:]=[row];s.tick()
    s.finish('cancelled','test')
    assert len(s.last['samples'])==6
    assert s.last['source_coverage']['sample_count']==6
    assert s.last['timing_meta']['acquisition_timestamp_available'] is False


def test_main_heartbeat_expiry_does_not_resume_pi(rig,monkeypatch):
    o,s,d=make(rig,monkeypatch);s.begin(request(),o.target_revision)
    rig['clock'].sleep(2.1);s.tick()
    assert s.last['restored'] and s.last['pi_resumed'] is False
    assert o.mode=='idle'


def test_split_input_attenuation_and_float_selector_restore(rig,monkeypatch):
    o,s,d=make(rig,monkeypatch)
    s.config['adc_v_per_awg_v']=.9868
    d.values['laser1:dl:pc:external-input:signal']=-3.0
    write=d.write
    def typed(path,value,**kwargs):
        if path.endswith('external-input:signal'):
            assert type(value) is int
        return write(path,value,**kwargs)
    monkeypatch.setattr(d,'write',typed)
    r=s.begin(request(),o.target_revision)
    for index in range(52):
        rig['clock'].sleep(.05);o.heartbeat();elapsed=(index+1)*.05
        d.values['io:fine-2:value-act']=.09868 if .5<=elapsed<.9 else -.09868 if 1.3<=elapsed<1.7 else 0.
        if s.active():s.heartbeat(r['token']);s.tick()
    assert s.last['verdict']=='succeeded' and s.last['restored']
    assert any(row['value_v']==.09868 for row in s.last['adc'])
    assert d.values['laser1:dl:pc:external-input:signal']==-3


def test_primary_stop_survives_post_cleanup_pi_capture_refusal(rig,monkeypatch):
    o,s,d=make(rig,monkeypatch);s.begin(request(),o.target_revision)
    rig['source'].bias=1e9
    s.finish('failed','Observed plateau differs from the armed record')
    result=s.last
    assert result['stop_verdict']=='failed'
    assert result['stop_reason']=='Observed plateau differs from the armed record'
    assert result['reason'].startswith(result['stop_reason'])
    assert result['pi_resume_reason']=='Sweep ended outside the original PI capture context'
    assert result['restored'] and not result['outputs_uncertain'] and not result['pi_resumed']
    assert o.mode=='idle' and not o.fault and s.port is None
    complete=next(row for row in reversed(rig['events']) if row['event']=='awg_complete')
    assert complete['stop_reason']==result['stop_reason']
    assert complete['pi_resume_reason']==result['pi_resume_reason']
