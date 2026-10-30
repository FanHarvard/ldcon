"""No network/hardware: full card -> scoped owner -> synthetic chassis path."""
from copy import deepcopy
from dataclasses import asdict
import json
import math
from pathlib import Path
import socket

import pytest
import yaml

from ldcon.core.contracts import FrequencySample, DeviceError, MeasurementUnavailable
from ldcon.core.interface_cards import CardCancelled
from ldcon.core.interface_schema import validate_deck,validate_laser,readiness,digest
from ldcon.core.operation_registry import admission_class
from ldcon.drivers.dlcpro import DLCPro,Console,UncertainDLCWrite
from ldcon.owners.dlcpro_owner import Instrument,LaserOwner,CycleMean,policy_from_device
from ldcon.jobs.dlcpro_cards import Protocol,parameters
from ldcon.owners.ecdl_prelock import ModeObservation,AxisPolicy
from ldcon.jobs.axis_frequency import run as calibrate

ROOT=(Path(__file__).resolve().parent / 'support')
EXAMPLES=ROOT/'examples/dlcpro'


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args,**kwargs):raise AssertionError('Tests must not access a network')
    monkeypatch.setattr(socket,'create_connection',denied)
    monkeypatch.setattr(socket.socket,'connect',denied)


class Clock:
    def __init__(self):self.now=100.
    def __call__(self):return self.now
    def sleep(self,dt):self.now+=dt


class FakeDriver:
    path=staticmethod(DLCPro.path)
    def control_transaction(self):
        from contextlib import nullcontext
        return nullcontext()
    def __init__(self):
        self.state={'piezo':70.,'temperature':25.,'current':100.}
        self.ff=True;self.factor=.1;self.enabled=False;self.writes=[];self.fail=False
        self.values={'laser1:scan:unit':'V','laser1:scan:offset':70.}
    def read(self,path):
        if path.endswith('scan:enabled'):return self.enabled
        return self.values.get(path,0.)
    def write(self,path,value,**kw):
        self.writes.append((path,value))
        if self.fail:raise UncertainDLCWrite('synthetic timeout')
        for axis,(setting,_,_) in DLCPro.AXES.items():
            if path.endswith(setting):self.state[axis]=value
        if path.endswith('feedforward-enabled'):self.ff=value
        if path.endswith('feedforward-factor'):self.factor=value
        if path.endswith('scan:enabled'):self.enabled=value
        self.values[path]=value
        return value
    def snapshot(self,channel):
        return {**{k:dict(setpoint=v,measured=v,unit=DLCPro.AXES[k][2]) for k,v in self.state.items()},
                'ff_enabled':self.ff,'ff_factor_ma_per_v':self.factor,'tec_fault':False,'emission':True,'pzt_enabled':True,'tec_enabled':True}


class Source:
    def __init__(self,clock,driver):
        self.clock,self.driver=clock,driver;self.bias=0.;self.degraded=False;self.missing=False;self.fixed=None
    def read_mode(self):
        if self.missing:raise MeasurementUnavailable('missing')
        f=3e14+(self.driver.state['piezo']-70)*1e8+(self.driver.state['temperature']-25)*1e10+self.bias
        t=self.clock() if self.fixed is None else self.fixed
        return ModeObservation(FrequencySample(int(t*1000),f,t,t,'valid','synthetic'),
            not self.degraded,'branch',True,.1,self.degraded,{'side_peak_height_max':.8 if self.degraded else .1,'side_peak_prominence_max':.05,'main_fwhm_fraction':.1},{})


@pytest.fixture
def rig():
    laser=yaml.safe_load((EXAMPLES/'laser.offline.yaml').read_text())
    deck=yaml.safe_load((EXAMPLES/'deck.yaml').read_text())
    hardware=json.loads((EXAMPLES/'owner.offline.json').read_text())['hardware']
    hardware['channels']['1'].update(output_filters_verified=True,pzt_scan_output_channel=0)
    clock=Clock();driver=FakeDriver();source=Source(clock,driver);events=[]
    inst=Instrument(driver,hardware,audit=events.append)
    device=laser['devices']['dlc']
    owner=LaserOwner(inst,1,laser['id'],policy_from_device(device),source,clock=clock,audit=events.append)
    control=dict(deck['cards'][1]['parameters'],pzt_center_v=70.)
    return dict(laser=laser,deck=deck,clock=clock,driver=driver,source=source,events=events,
                instrument=inst,owner=owner,device=device,control=control)


def advance(rig, seconds, dt=.05):
    for _ in range(round(seconds/dt)):
        rig['clock'].sleep(dt)
        rig['owner'].heartbeat()
        rig['owner'].tick()


def test_example_catalog_blocked_before_commissioning(rig):
    validate_deck(deepcopy(rig['deck']));validate_laser(rig['laser'])
    for card in rig['deck']['cards']:
        r=readiness(rig['laser'],rig['deck'],card)
        assert not r['runnable']
        if card['operation'] in ('measure.change','measure.batch'):
            assert any('commissioning' in x.lower() for x in r['blocked_reasons'])
        else:
            assert any('disabled' in x for x in r['blocked_reasons'])
    assert admission_class(rig['laser'],rig['deck']['cards'][2])=='exclusive'


def test_pi_signed_slope_freshness_and_quality_warning(rig):
    o=rig['owner'];s=rig['source'];d=rig['driver']
    o.start_lock(3e14,rig['control']);s.bias=2e6;s.degraded=True
    advance(rig,2)
    assert abs(s.read_mode().frequency.frequency_hz-3e14)<2e6
    assert o.mode=='lock' and o.last_observation['degraded']
    before=len(d.writes);integral=o.integral;s.fixed=o.last_acquisition
    advance(rig,.15)
    assert len(d.writes)==before and o.integral==integral
    assert o.mode=='lock'


def test_tec_centers_even_when_quality_degraded(rig):
    o=rig['owner'];rig['source'].degraded=True
    control=dict(rig['control'],tec_period_s=.1,pzt_center_v=69.9)
    o.start_lock(3e14,control)
    advance(rig,.5)
    assert rig['driver'].state['temperature']>25
    assert not o.fault


def test_missing_input_holds_then_stops_with_deadline(rig):
    o=rig['owner'];o.start_lock(3e14,rig['control']);rig['source'].missing=True
    n=len(rig['driver'].writes)
    advance(rig,1)
    assert o.mode=='lock' and len(rig['driver'].writes)==n
    advance(rig,1.1)
    assert o.mode=='idle' and o.fault


def test_write_unknown_retains_instrument_lease_and_never_replays(rig):
    o=rig['owner'];o.start_lock(3e14,rig['control']);rig['driver'].fail=True
    with pytest.raises(UncertainDLCWrite):advance(rig,.1)
    count=len(rig['driver'].writes)
    o.tick()
    assert len(rig['driver'].writes)==count and o.fault
    with pytest.raises(DeviceError):rig['instrument'].release(1,o.lease)


def test_chassis_exclusion_and_isolation(rig):
    i=rig['instrument'];o=rig['owner']
    with pytest.raises(DeviceError):i.claim(1,'dlc-simulation',o.policy)
    with pytest.raises(DeviceError):i.move(1,'wrong','piezo',70.)
    before=len(rig['driver'].writes)
    with pytest.raises(ValueError):i.move(1,o.lease,'piezo',150.)
    assert len(rig['driver'].writes)==before


def test_ff_envelope_checks_whole_scan_before_any_write(rig):
    o=rig['owner'];i=rig['instrument']
    p=deepcopy(o.policy);p['axes']['current']['bounds']=[99,101]
    i.project(1,o.lease,digest(o.policy),p)
    n=len(rig['driver'].writes)
    with pytest.raises(ValueError):i.scan(1,o.lease,center=70,amplitude=100,frequency=1,shape='triangle',enabled=True)
    assert len(rig['driver'].writes)==n


def test_revision_and_approval_are_not_bypassed(rig):
    o=rig['owner'];p=deepcopy(o.policy);p['ff_factor_ma_per_v']=.2
    with pytest.raises(PermissionError):o.reload(digest(o.policy),p,{'approved':True})
    assert o.policy['ff_factor_ma_per_v']==.1
    o.approve=lambda old,new,evidence:evidence=='trusted-exact-change'
    with pytest.raises(ValueError):o.reload('old',p,'trusted-exact-change')
    o.reload(digest(o.policy),p,'trusted-exact-change')
    assert o.policy['ff_factor_ma_per_v']==.2


def test_cycle_mean_time_weighted_duplicate_and_gap():
    c=CycleMean(0,1,.6)
    assert c.add(0,100) is None
    assert c.add(.1,110) is None
    assert c.add(.1,900) is None
    assert c.add(.5,150) is None
    result=c.add(1,200)
    assert result['valid'] and result['mean_frequency_hz']==pytest.approx(150)
    bad=CycleMean(0,1,.2)
    bad.add(0,100);bad.add(.1,100)
    assert not bad.add(1,100)['valid']


def test_cycle_complete_scan_correction_and_stop(rig):
    o=rig['owner'];s=rig['source'];d=rig['driver']
    p=dict(rig['control'],period_s=1.,span_hz=1e7,shape='triangle',center_lock=True,max_gap_s=.2,boundary_tolerance_s=.15)
    o.start_scan(3e14,p);s.bias=1e6
    # Seed exactly at start; rest of mock data represent a complete symmetric cycle.
    o.tick();before=d.values['laser1:scan:offset']
    advance(rig,.8)
    assert d.values['laser1:scan:offset']==before
    advance(rig,.3)
    assert d.values['laser1:scan:offset']<before
    assert any(event.get('event')=='scan_cycle' and event['valid'] for event in rig['events'])
    o.stop()
    assert not d.enabled and o.mode=='idle'


def test_target_revision_never_restored_by_stop(rig):
    o=rig['owner'];o.start_lock(3e14,rig['control'])
    rev=o.set_target(3e14+2e7,o.target_revision)
    with pytest.raises(ValueError):o.set_target(3e14,rev-1)
    o.stop()
    assert o.target==3e14+2e7


class Wire:
    def __init__(self,chunks):self.chunks=list(chunks);self.sent=[]
    def recv(self,n):
        result=self.chunks.pop(0)
        if isinstance(result,Exception):raise result
        return result
    def sendall(self,raw):self.sent.append(raw)
    def close(self):pass


def test_console_fragmentation_and_verified_readback():
    wire=Wire([b'DLC hello\r\n>',b' ',b'0\r\n> ',b'70.0\r\n> '])
    driver=DLCPro(Console(wire))
    assert driver.write('laser1:dl:pc:voltage-set',70)==70
    assert len(wire.sent)==2


@pytest.mark.parametrize('reply',[b'2\n> ',b'-1\n> ',b'unknown\n> ',TimeoutError('late')])
def test_console_clipping_warning_timeout_not_success(reply):
    wire=Wire([b'> ',reply]);driver=DLCPro(Console(wire))
    with pytest.raises(UncertainDLCWrite):driver.write('laser1:dl:pc:voltage-set',70)
    assert len(wire.sent)==1


def test_protocol_exact_scope_and_serialization(rig):
    p=Protocol(rig['owner'],rig['device']);laser=rig['laser']['id']
    r=p.dispatch(dict(action='claim',laser_id=laser,revision=digest(rig['device'])))
    lease=r['lease']
    with pytest.raises(PermissionError):p.dispatch(dict(action='claim',laser_id=laser,revision=digest(rig['device'])))
    with pytest.raises(PermissionError):p.dispatch(dict(action='move',laser_id='another',lease=lease,axis='piezo',value=71))
    p.dispatch(dict(action='move',laser_id=laser,lease=lease,axis='piezo',value=70.1))
    assert rig['driver'].state['piezo']==70.1
    p.dispatch(dict(action='release',laser_id=laser,lease=lease))


def calibration_ports(rig):
    o=rig['owner'];clock=rig['clock']
    class Axis:
        policy=AxisPolicy('V',0,140,140,2800,.05,.05,1e8,'mock.pzt','r1',1e-6)
        def readback(self):return rig['driver'].state['piezo']
        def move(self,value,duration_s):o.move('piezo',value)
    class Loop:
        def is_open_loop(self):return o.mode=='idle'
    return dict(axis=Axis(),observation=rig['source'],loop=Loop(),context=o.snapshot)


def test_common_calibration_signed_bidirectional_proposal_only(rig,tmp_path):
    p=dict(rig['deck']['cards'][0]['parameters'],write_report=False)
    ports=calibration_ports(rig)
    result=calibrate(ports,p,tmp_path,clock=rig['clock'],sleep=rig['clock'].sleep,cancel=lambda:False,emit=lambda *a:None)
    assert result['status']=='succeeded' and result['fit']['frequency_hz_per_unit']==pytest.approx(1e8)
    assert result['origin_restored'] and not result['adopted']
    assert len(result['points'])==12
    assert result['fit']['directional_hz_per_unit']['up']==pytest.approx(1e8)
    assert (tmp_path/'axis-frequency-calibration.json').exists()


def test_calibration_duplicate_sample_times_deadline_retains_raw(rig,tmp_path):
    p=dict(rig['deck']['cards'][0]['parameters'],write_report=False,point_timeout_s=.3)
    rig['source'].fixed=rig['clock']()
    with pytest.raises(MeasurementUnavailable):
        calibrate(calibration_ports(rig),p,tmp_path,clock=rig['clock'],sleep=rig['clock'].sleep,cancel=lambda:False,emit=lambda *a:None)
    result=json.loads((tmp_path/'axis-frequency-calibration.json').read_text())
    assert result['status']=='failed' and result['raw'] and not result['adopted']


def test_calibration_cancel_does_not_speculatively_restore(rig,tmp_path):
    p=dict(rig['deck']['cards'][0]['parameters'],write_report=False)
    start=rig['clock']()
    with pytest.raises(CardCancelled):
        calibrate(calibration_ports(rig),p,tmp_path,clock=rig['clock'],sleep=rig['clock'].sleep,cancel=lambda:rig['clock']()-start>.1,emit=lambda *a:None)
    result=json.loads((tmp_path/'axis-frequency-calibration.json').read_text())
    assert result['status']=='cancelled' and not result['origin_restored']
    assert rig['driver'].state['piezo']==69.9


def test_active_policy_adopts_at_fresh_boundary_without_chassis_restart(rig):
    o=rig['owner'];o.approve=lambda a,b,e:e=='reviewed'
    o.start_lock(3e14,rig['control'])
    p=deepcopy(o.policy);p['quality_limits']['side_peak_height_max']=.4
    receipt=o.reload(digest(o.policy),p,'reviewed')
    assert receipt['status']=='pending' and o.policy['quality_limits']['side_peak_height_max']==.3
    advance(rig,.1)
    assert o.policy['quality_limits']['side_peak_height_max']==.4
    assert o.mode=='lock' and not o.fault


def test_registered_card_path_and_cancel_preserves_lease(rig):
    from ldcon.jobs.dlcpro_cards import Session,run
    protocol=Protocol(rig['owner'],rig['device'])
    class Local(Session):
        def rpc(self,action,_timeout=30,**values):
            return protocol.dispatch(dict(action=action,laser_id=rig['laser']['id'],**values))
    session=Local(rig['device']).connect()
    card=rig['deck']['cards'][1]
    p=dict(card['parameters'],duration_s=.5)
    def sleep(dt):advance(rig,dt)
    result=run({'dlc_lock':session},rig['laser'],card,p,clock=rig['clock'],sleep=sleep,cancel=lambda:False,emit=lambda *a:None)
    assert result['status']=='succeeded' and rig['owner'].mode=='idle'
    session.close()
    assert protocol.client is None


def test_calibration_works_in_counts_with_negative_slope(rig,tmp_path):
    ports=calibration_ports(rig)
    original=rig['source'].read_mode
    def negative():
        o=original()
        return ModeObservation(FrequencySample(o.frequency.sequence,6e14-o.frequency.frequency_hz,
            o.frequency.acquired_s,o.frequency.received_s,'valid','synthetic'),True,'branch',True,.1,False,o.metrics,{})
    rig['source'].read_mode=negative
    ports['axis'].policy=AxisPolicy('count',0,140,140,2800,.05,.05,-1e8,'mock.dac','r1',1e-6)
    p=dict(rig['deck']['cards'][0]['parameters'],write_report=False)
    result=calibrate(ports,p,tmp_path,clock=rig['clock'],sleep=rig['clock'].sleep,cancel=lambda:False,emit=lambda *a:None)
    assert result['fit']['frequency_hz_per_unit']==pytest.approx(-1e8)
    assert result['axis']['unit']=='count'


def test_calibration_hop_rejects_fit_and_no_restore(rig,tmp_path):
    ports=calibration_ports(rig);original=rig['source'].read_mode;n=0
    def hop():
        nonlocal n
        n+=1
        if n==7:rig['source'].bias+=1e10
        return original()
    rig['source'].read_mode=hop
    p=dict(rig['deck']['cards'][0]['parameters'],write_report=False)
    with pytest.raises(DeviceError,match='discontinuity'):
        calibrate(ports,p,tmp_path,clock=rig['clock'],sleep=rig['clock'].sleep,cancel=lambda:False,emit=lambda *a:None)
    result=json.loads((tmp_path/'axis-frequency-calibration.json').read_text())
    assert result['status']=='failed' and 'fit' not in result and not result['origin_restored']


def test_examples_load_through_real_repository(tmp_path,rig):
    from ldcon.core.interface_schema import InterfaceRepository
    from ldcon.core.deck_yaml import dump_document
    for sub in ('lasers','interface-decks','assignments'):(tmp_path/sub).mkdir()
    (tmp_path/'lasers/dlc-simulation.yaml').write_text((EXAMPLES/'laser.offline.yaml').read_text())
    (tmp_path/'interface-decks/ecdl-dlcpro.yaml').write_text((EXAMPLES/'deck.yaml').read_text())
    (tmp_path/'assignments/dlc-simulation.yaml').write_text((EXAMPLES/'assignment.yaml').read_text())
    result=InterfaceRepository(tmp_path).assignment('dlc-simulation')
    assert set(result['deck_definitions'][0]['card_status'])=={c['id'] for c in rig['deck']['cards']}
    laser=deepcopy(rig['laser']);laser['allow_output']=True
    laser['devices']['dlc']['control_settings']['commissioned']=True
    for card in rig['deck']['cards']:
        status=readiness(laser,rig['deck'],card)
        if card['operation']=='measure.batch':
            assert not status['runnable'] and any('step' in issue for issue in status['blocked_reasons'])
        else:
            assert status['runnable'],status['blocked_reasons']


def test_scan_missing_data_stops_generator_without_replay(rig):
    o=rig['owner'];p=dict(rig['control'],period_s=1.,span_hz=1e7,shape='sine',center_lock=False,max_gap_s=.2,boundary_tolerance_s=.15)
    o.start_scan(3e14,p);rig['source'].missing=True
    advance(rig,2.1)
    assert o.mode=='idle' and not rig['driver'].enabled and o.fault


def test_scan_partial_cycle_cannot_move_center(rig):
    o=rig['owner'];p=dict(rig['control'],period_s=1.,span_hz=1e7,shape='sine',center_lock=True,max_gap_s=.05,boundary_tolerance_s=.2)
    o.start_scan(3e14,p);rig['source'].bias=1e6
    advance(rig,1.2,dt=.1)
    assert rig['driver'].values['laser1:scan:offset']==70.
    assert o.events and not o.events[0]['valid']


def test_ff_already_correct_does_not_toggle_output(rig):
    o=rig['owner'];n=len(rig['driver'].writes)
    o.instrument.configure_ff(1,o.lease)
    assert len(rig['driver'].writes)==n


def test_calibration_bad_fit_is_inconclusive_and_keeps_evidence(rig,tmp_path):
    ports=calibration_ports(rig);original=rig['source'].read_mode
    def curved():
        rig['source'].bias=(rig['driver'].state['piezo']-70)**2*1e11
        return original()
    rig['source'].read_mode=curved
    p=dict(rig['deck']['cards'][0]['parameters'],write_report=False,max_discontinuity_hz=1e11)
    result=calibrate(ports,p,tmp_path,clock=rig['clock'],sleep=rig['clock'].sleep,cancel=lambda:False,emit=lambda *a:None)
    assert result['status']=='inconclusive' and not result['fit']['valid']
    assert result['origin_restored'] and len(result['raw'])==60


def preserved_rig(rig):
    """FW1.9-like readbacks with no calculated voltage or master selector."""
    driver=rig['driver'];original=driver.snapshot
    def snapshot(channel):
        snap=original(channel)
        total=driver.state['current']+(.1*(driver.state['piezo']-70.) if driver.ff else 0.)
        snap['current'].update(total_setpoint=total,measured=total)
        snap['piezo'].update(calculated_output=None,calculated_output_available=False)
        snap['feedforward_master']=None
        return snap
    driver.snapshot=snapshot
    hardware=deepcopy(rig['instrument'].hardware)
    hardware['channels']['1'].update(ff_reference_v=None,ff_delta_verified=True)
    policy=deepcopy(rig['owner'].policy);policy['ff_mode']='preserve_existing'
    inst=Instrument(driver,hardware,audit=rig['events'].append)
    owner=LaserOwner(inst,1,rig['laser']['id'],policy,rig['source'],clock=rig['clock'],audit=rig['events'].append)
    rig.update(instrument=inst,owner=owner)
    return rig


def test_preserved_ff_pi_tec_lock_without_optional_fields_or_reference(rig):
    r=preserved_rig(rig);o=r['owner'];d=r['driver']
    o.start_lock(3e14,dict(r['control'],tec_period_s=.1,pzt_center_v=69.9))
    assert not d.writes  # Starting never toggles FF or changes its factor.
    r['source'].bias=2e6
    advance(r,2)
    assert o.mode=='lock' and not o.fault
    assert any(path.endswith('pc:voltage-set') for path,value in d.writes)
    assert any(path.endswith('tc:temp-set') for path,value in d.writes)
    assert all('feedforward' not in path for path,value in d.writes)
    assert o.snapshot()['hardware']['piezo']['calculated_output'] is None
    o.stop();assert o.mode=='idle'


def test_preserved_ff_needs_calibrated_delta_not_guessed_reference(rig):
    r=preserved_rig(rig);i=r['instrument'];o=r['owner']
    i.hardware['channels']['1']['ff_delta_verified']=False
    with pytest.raises(ValueError,match='verified PZT'):
        o.start_lock(3e14,r['control'])
    assert not r['driver'].writes


def test_preserved_ff_mismatch_never_auto_configures(rig):
    r=preserved_rig(rig);r['driver'].factor=.2
    with pytest.raises(DeviceError,match='Preserved DLC FF'):
        r['owner'].start_lock(3e14,r['control'])
    assert not r['driver'].writes


def test_preserved_ff_total_current_bounds_before_first_write(rig):
    r=preserved_rig(rig);i=r['instrument'];o=r['owner']
    i.channels[1]['policy']['axes']['current']['bounds']=[99.,100.05]
    with pytest.raises(ValueError,match='approved bounds'):
        i.move(1,o.lease,'piezo',71.) # bias100, total100.1: exceeds actual output bound.
    assert not r['driver'].writes


def test_preserved_ff_cannot_scan_or_hot_change_ff(rig):
    r=preserved_rig(rig);i=r['instrument'];o=r['owner']
    with pytest.raises(ValueError,match='static lock only'):
        i.scan(1,o.lease,center=70.,amplitude=1.,frequency=1.,shape='triangle',enabled=True)
    p=deepcopy(o.policy);p['ff_factor_ma_per_v']=.2
    with pytest.raises(ValueError,match='cannot be changed'):
        i.project(1,o.lease,digest(o.policy),p)
    assert not r['driver'].writes


def test_preserved_ff_no_measured_substitute_for_missing_total(rig):
    r=preserved_rig(rig);d=r['driver'];original=d.snapshot
    def missing(channel):
        snap=original(channel);snap['current']['total_setpoint']=None;return snap
    d.snapshot=missing
    with pytest.raises(ValueError,match='Finite FF'):
        r['owner'].start_lock(3e14,r['control'])
    assert not d.writes


def test_preserved_ff_missing_frequency_holds_then_expires(rig):
    r=preserved_rig(rig);o=r['owner'];o.start_lock(3e14,r['control'])
    r['source'].missing=True;advance(r,1.)
    assert o.mode=='lock' and not r['driver'].writes
    advance(r,1.1);assert o.mode=='idle' and o.fault


def test_preserved_ff_uncertain_output_is_not_retried(rig):
    r=preserved_rig(rig);o=r['owner'];o.start_lock(3e14,r['control'])
    r['driver'].fail=True
    with pytest.raises(UncertainDLCWrite):advance(r,.1)
    count=len(r['driver'].writes);o.tick()
    assert len(r['driver'].writes)==count and o.fault
