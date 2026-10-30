"""A dedicated physical channel cannot energize an unreviewed carrier."""
import pytest
from ldcon.owners.ecdl_projection import operation, physical_validator

class Readback:
    def __init__(self):
        self.state=dict(value=0.,amplitude_vpp=0.,waveform='DC',burst='OFF',
                        load='HZ',polarity='NOR',modulation='OFF',sweep='OFF')
        self.burst='C1:BTWV STATE,ON,TRSR,EXT,GATE_NCYC,NCYC,EDGE,RISE,TIME,1'
    def read(self, channel):return dict(self.state)
    def query(self, command):return self.burst

def test_output_enable_needs_explicit_projection_and_bounded_dc():
    raw=Readback();v=physical_validator(raw,'current_awg')
    p={'channels':{'1':{'bounds':[-4,4],'max_vpp':8}}}
    op=operation('_send',['C1:OUTP ON'])
    with pytest.raises(Exception,match='not in this system projection'):v(op,p)
    p['channels']['1']['allow_output_enable']=True
    v(op,p)
    raw.state['value']=4.01
    with pytest.raises(Exception,match='envelope'):v(op,p)

@pytest.mark.parametrize('field,value',[('waveform','SINE'),('load','50'),('polarity','INVT'),('modulation','ON'),('sweep','ON'),('burst','ON')])
def test_output_enable_rejects_unproven_state(field,value):
    raw=Readback();raw.state[field]=value
    v=physical_validator(raw,'current_awg')
    with pytest.raises(Exception):v(operation('_send',['C1:OUTP ON']),{'channels':{'1':{'bounds':[-4,4],'max_vpp':8,'allow_output_enable':True}}})

def test_arb_enable_requires_uploaded_external_single_cycle():
    raw=Readback();raw.state.update(waveform='ARB',arb_name='probe',burst='ON',amplitude_vpp=.12)
    v=physical_validator(raw,'current_awg')
    p={'channels':{'1':{'bounds':[-4,4],'max_vpp':8,'allow_output_enable':True}}}
    op=operation('_send',['C1:OUTP ON'])
    with pytest.raises(Exception,match='acknowledged upload'):v(op,p)
    v.record_result(operation('write_raw',[b'C1:WVDT WVNM,probe,FREQ,1,AMPL,.12,OFST,0,WAVEDATA,\x00\x00']))
    v(op,p)
    raw.state['arb_name']='probe.bin'
    v(op,p)  # SDG2122X reports its stored waveform filename, including suffix.
    raw.state['arb_name']='another.bin'
    with pytest.raises(Exception,match='acknowledged upload'):v(op,p)
    raw.state['arb_name']='probe.bin'
    raw.burst=raw.burst.replace('TRSR,EXT','TRSR,MAN')
    with pytest.raises(Exception,match='externally triggered'):v(op,p)
    v(operation('_send',['C1:OUTP OFF']),p)
