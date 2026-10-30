"""Offline measured idle correction with unchanged guards and no write replay."""
import json

import pytest

from .test_dlcpro_offline import rig, Clock
from .test_dlcpro_awg import make, request, config, FakePort
from ldcon.owners.dlcpro_awg_zero import observe, at_zero
from ldcon.owners.dlcpro_awg_port import Port, PreparingChannel, PreparationInterrupted
from ldcon.drivers.siglent import DummyTransport
from ldcon.owners.dlcpro_awg_plan import compile_record


def row(clock, value):
    return dict(value_v=value, started_s=clock(), completed_s=clock(),
                acquisition_timestamp_available=False)


def test_read_window_waits_out_transient_and_keeps_raw_values():
    clock = Clock(); p = config(); p['adc_v_per_awg_v'] = 1.
    values = iter([.01, 0., 0., 0., 0.]); rows = []
    window = observe(lambda:row(clock,next(values)),p,lambda:None,clock.sleep,rows,phase='armed')
    assert at_zero(window,p) and len(rows)>=4 and rows[0]['value_v']==.01
    assert window['completed_s']-window['started_s'] >= p['adc_max_interval_s']


def test_nonfinite_failure_read_is_serializable_and_retained():
    clock = Clock(); rows = []
    with pytest.raises(ValueError,match='Nonfinite'):
        observe(lambda:row(clock,float('nan')),config(),lambda:None,clock.sleep,rows,phase='armed')
    assert rows[0]['value_v'] is None and rows[0]['nonfinite_value']=='nan'
    json.dumps(rows,allow_nan=False)


def prepare_bias(rig,monkeypatch,*,corrected=0.,fail=False):
    o,s,d=make(rig,monkeypatch)
    s.config['adc_v_per_awg_v']=.9868
    class Bias(FakePort):
        def __init__(self):
            super().__init__();self.corrections=[]
        def arm(self,*args):
            d.values['io:fine-2:value-act']=-.002
            return super().arm(*args)
        def rearm_offset(self,value):
            self.checkpoint()
            assert d.values['laser1:dl:pc:external-input:enabled'] is False
            self.corrections.append(value)
            if fail:raise TimeoutError('unknown correction write')
            d.values['io:fine-2:value-act']=corrected
            return dict(correction_v=value,original_offset_v=0.,offset_v=value,trigger_source='EXT')
    port=Bias();s.factory=lambda *a:port
    return o,s,d,port


def test_one_measured_correction_precedes_arc_and_retains_proof(rig,monkeypatch):
    o,s,d,port=prepare_bias(rig,monkeypatch)
    result=s.begin(request(),o.target_revision)
    assert result['ready'] and port.corrections==pytest.approx([.002/.9868])
    assert s.session['zero_correction']['state']=='verified'
    assert len(s.session['prearm_adc'])>=6
    assert s.session['prearm_adc'][0]['value_v']==-.002
    proof=s.session['proof'];shift=.002/.9868
    assert proof['voltage_envelope_v']==pytest.approx([-.1+shift,.1+shift])
    assert proof['admitted_voltage_envelope_v']==pytest.approx([-.1,.1+shift])
    assert proof['pzt_envelope_v']==pytest.approx([69.9,70.1+shift])
    assert s.config['adc_zero_v']==0. and s.config['adc_tolerance_v']==.001
    s.finish('cancelled','test')
    assert s.last['restored'] and o.mode=='lock'


def test_uncorrected_residual_never_enables_arc_or_repeats_correction(rig,monkeypatch):
    o,s,d,port=prepare_bias(rig,monkeypatch,corrected=.00101)
    with pytest.raises(ValueError,match='no ARC'):
        s.begin(request(),o.target_revision)
    assert len(port.corrections)==1 and port.restored and port.closed
    assert s.last['outputs_uncertain'] is False and s.last['restored']
    assert s.last['prearm_adc'][-1]['value_v']==.00101
    assert s.last['zero_correction']['state']=='rejected_after_rearm'
    assert s.last['zero_correction']['after_rearm_idle']['value_v']==.00101
    assert not any(path.endswith(':external-input:enabled') and value for path,value in d.writes)


def test_unknown_correction_does_not_restore_or_replay(rig,monkeypatch):
    o,s,d,port=prepare_bias(rig,monkeypatch,fail=True)
    with pytest.raises(TimeoutError):s.begin(request(),o.target_revision)
    assert len(port.corrections)==1 and not port.restored and not port.closed
    assert o.fault and s.last['outputs_uncertain']
    assert s.last['prearm_adc'][0]['value_v']==-.002


@pytest.mark.parametrize('axis', ['awg','piezo','current'])
def test_correction_envelope_rejection_precedes_mutation(rig,monkeypatch,axis):
    o,s,d,port=prepare_bias(rig,monkeypatch)
    if axis=='awg':s.config['awg']['max_v']=.101
    elif axis=='piezo':o.policy['axes']['piezo']['bounds'][1]=70.101
    else:o.policy['axes']['current']['bounds'][1]=100.0101
    with pytest.raises(ValueError,match='envelope'):
        s.begin(request(),o.target_revision)
    assert not port.corrections and port.restored and not o.fault
    assert s.last['zero_correction']['state']=='rejected_before_write'
    assert s.last['zero_correction']['measured_idle']['value_v']==-.002
    assert not any(path.endswith(':external-input:enabled') and value for path,value in d.writes)


def test_cancel_during_idle_reads_cleans_without_correction(rig,monkeypatch):
    o,s,d,port=prepare_bias(rig,monkeypatch)
    def pause(seconds):
        rig['clock'].sleep(seconds);s.request_cancel()
    s.sleep=pause
    with pytest.raises(PreparationInterrupted):s.begin(request(),o.target_revision)
    assert not port.corrections and port.restored and s.last['restored']
    assert s.last['verdict']=='cancelled'


def test_unstable_input_never_extends_main_deadline(rig,monkeypatch):
    o,s,d,port=prepare_bias(rig,monkeypatch)
    value=[0]
    def changing():
        value[0]+=1
        return row(rig['clock'], .01 if value[0]%2 else -.01)
    monkeypatch.setattr(s,'_read',changing)
    with pytest.raises(PreparationInterrupted,match='heartbeat'):
        s.begin(request(),o.target_revision)
    assert not port.corrections and not s.last['pi_resumed']
    assert s.last['outputs_uncertain'] is False


def test_rearm_turns_off_before_bswv_and_reasserts_external_burst(rig,monkeypatch):
    o,s,d=make(rig,monkeypatch);p=config()['awg']
    io=DummyTransport(p);io.state['OUTP']={}
    port=Port.__new__(Port);port.io=io;port.checkpoint=lambda:None;port.changed=False
    port.channel=PreparingChannel(port,p);port.before=port.channel.snapshot(allow_output_off=True)
    record,_=compile_record(config(),request(),o.snapshot(),o.policy)
    port.arm(record,p['role'],'abc')
    uploads=dict(io.uploads);io.log.clear()
    receipt=port.rearm_offset(.002)
    assert io.log[0]=='C1:OUTP OFF'
    fields=next(i for i,x in enumerate(io.log) if x.startswith('C1:BSWV FRQ,'))
    burst=io.log.index('C1:BTWV STATE,ON')
    enable=io.log.index('C1:OUTP ON')
    assert fields<burst<enable and 'C1:BTWV TRSR,EXT' in io.log
    assert io.state['BTWV']['STATE']=='ON' and io.state['BSWV']['OFST']=='0.002'
    assert not io.fired and io.uploads==uploads and receipt['offset_v']==.002


@pytest.mark.parametrize('failure', ['residual', 'read', 'unknown_write'])
def test_failure_adc_and_correction_survive_terminal_journal(rig,monkeypatch,tmp_path,failure):
    from .test_dlcpro_awg_protocol import prepared
    o,p,call=prepared(rig,monkeypatch,tmp_path)
    d=rig['driver'];manager=o.awg_sweep
    class Bias(FakePort):
        def arm(self,*args):
            d.values['io:fine-2:value-act']=-.002
            return super().arm(*args)
        def rearm_offset(self,value):
            if failure=='unknown_write':raise TimeoutError('unknown correction write')
            d.values['io:fine-2:value-act']=float('nan') if failure=='read' else .00101
            return dict(correction_v=value,offset_v=value,trigger_source='EXT')
    manager.sweep.factory=Bias
    call('begin',request=request(),token='b'*32,target_revision=o.target_revision)
    assert manager.prepared.wait(2.)
    full=call('result',request_id='a'*32)
    assert full['prearm_adc'][0]['value_v']==-.002
    assert full['zero_correction']['measured_idle']['value_v']==-.002
    if failure=='residual':
        assert full['zero_correction']['after_rearm_idle']['value_v']==.00101
    elif failure=='read':
        assert full['prearm_adc'][-1]['nonfinite_value']=='nan'
    assert full['outputs_uncertain']==(failure=='unknown_write')
    assert 'prearm_adc' not in call('status',request_id='a'*32)
    json.dumps(full,allow_nan=False)
    with pytest.raises(ValueError,match='already recorded'):
        call('begin',request=request(),token='b'*32,target_revision=o.target_revision)
