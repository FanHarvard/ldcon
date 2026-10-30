"""WSU wire protocol and software PI tests; synthetic chassis, no live routes."""
import binascii
import json
import math
import socket
import time

import pytest
import yaml

from tests.test_dlcpro_offline import rig, EXAMPLES
from ldcon.core.contracts import MeasurementUnavailable
from ldcon.drivers.wsu_udp import PACKET, WSUReceiver, decode
from ldcon.owners.dlcpro_observation import DatagramOpticalSource
from ldcon.owners.dlcpro_deployment import commissioning_issues


def config(**changes):
    return dict(listen_host='127.0.0.1',listen_port=47998,source_ip='127.0.0.1',
        valid_data_index=2,channel=3,cycle=0,trust_source_clock=True,
        max_age_s=.2,future_tolerance_s=.01,buffer_samples=32,**changes)


def packet(seq, stamp, value=300., flags=1, channel=3, quality=0):
    raw=PACKET.pack(b'WSU1',1,flags,2,channel,0,0,quality,seq,round(stamp*1e9),value,0)
    return raw[:-2]+binascii.crc_hqx(raw[:-2],0xffff).to_bytes(2,'little')


def receiver(rig):
    c=rig['clock']
    return WSUReceiver(config(),clock=c,utc=lambda:1000+c())


def feed(r, rig, seq, **kw):
    return r.feed(packet(seq,1000+rig['clock'](),**kw),'127.0.0.1')


def test_wire_units_and_quality_metadata():
    # Captured from esp32servocon/cli/udp_send.py build_datagram, not this decoder.
    golden=bytes.fromhex('5753553101050203000000027856341215cd0f9b76e2fa1800000000006885405f6d')
    parsed=decode(golden)
    assert (parsed.sequence,parsed.acquired_unix_ns,parsed.quality,parsed.value)==(0x12345678,1800000000123456789,512,685.)
    raw=packet(9,1100,quality=512,flags=0)
    assert len(raw)==34 and raw[:8]==b'WSU1\x01\x00\x02\x03'
    assert decode(raw).frequency_hz==3e14
    assert decode(raw).quality==512
    assert decode(packet(9,1100,value=685,flags=4)).frequency_hz==pytest.approx(299792458e9/685)
    for broken in (raw[:-1],raw+b'0',raw[:20]+bytes([raw[20]^1])+raw[21:]):
        with pytest.raises(ValueError):decode(broken)


@pytest.mark.parametrize('value',[0,-1,float('nan'),float('inf')])
def test_invalid_values(value):
    with pytest.raises(ValueError):decode(packet(1,1100,value=value))


def test_identity_freshness_reorder_wrap_restart(rig):
    r=receiver(rig);c=rig['clock']
    assert not r.feed(packet(1,1100),'127.0.0.2')
    assert not feed(r,rig,1,channel=4)
    assert not r.feed(packet(1,1099),'127.0.0.1')
    assert not r.feed(packet(1,1101),'127.0.0.1')
    assert feed(r,rig,0xffffffff,quality=512,flags=0)
    assert r.read_frequency().quality=='source_quality_512'
    assert not feed(r,rig,0xffffffff,flags=3)
    c.sleep(.01);assert feed(r,rig,0)
    c.sleep(.01);assert feed(r,rig,50)
    c.sleep(.01);assert not feed(r,rig,1)
    c.sleep(.01);assert feed(r,rig,2)
    assert r.snapshot()['sequence_rebases']==1
    assert not r.feed(packet(3,1100),'127.0.0.1')
    c.sleep(.21)
    with pytest.raises(MeasurementUnavailable):r.read_frequency()


def test_bounded_ring_and_latest(rig):
    r=receiver(rig)
    for n in range(70):
        rig['clock'].sleep(.01);assert feed(r,rig,n)
    assert len(r.samples_after(None))==32
    assert r.read_frequency().sequence==69
    assert r.snapshot()['ring_overwritten']==38


def test_pi_uses_latest_of_many_new_packets(rig):
    r=receiver(rig);feed(r,rig,1)
    source=DatagramOpticalSource(r,rig['source'],quality_interval_s=.1,quality_max_age_s=.5,clock=rig['clock'])
    o=rig['owner'];o.observation=source;o.start_lock(3e14,rig['control'])
    for i in range(2,12):
        rig['clock'].sleep(.01);feed(r,rig,i,value=300.000001)
    n=len(rig['driver'].writes);o.tick()
    assert len(rig['driver'].writes)==n+1
    assert o.last_observation['sequence']==11


@pytest.mark.parametrize('interval,age',[(float('inf'),float('inf')),(float('nan'),1),(0,1),(2,1)])
def test_invalid_quality_cache_cadence(rig,interval,age):
    with pytest.raises(ValueError):
        DatagramOpticalSource(receiver(rig),rig['source'],quality_interval_s=interval,quality_max_age_s=age)


def test_auxiliary_failure_never_blocks_frequency_pi(rig):
    r=receiver(rig);feed(r,rig,1)
    source=DatagramOpticalSource(r,rig['source'],quality_interval_s=.1,quality_max_age_s=.5,clock=rig['clock'])
    rig['owner'].observation=source
    rig['source'].missing=True;source.refresh_quality()
    rig['owner'].start_lock(3e14,rig['control'])
    rig['clock'].sleep(.1);feed(r,rig,2,value=300.000002)
    rig['owner'].tick()
    assert rig['driver'].state['piezo']<70
    assert rig['owner'].last_observation['degraded'] is None
    assert not rig['owner'].fault
    n=len(rig['driver'].writes);rig['owner'].tick()
    assert len(rig['driver'].writes)==n
    rig['clock'].sleep(2.1);rig['owner'].heartbeat();rig['owner'].tick()
    assert rig['owner'].fault and rig['owner'].mode=='idle'


def test_optical_frame_preserves_acquisition_identity(rig):
    r=receiver(rig);feed(r,rig,1)
    source=DatagramOpticalSource(r,rig['source'],quality_interval_s=.1,quality_max_age_s=.5,clock=rig['clock'])
    source.refresh_quality();frame=source.read_mode()
    rig['clock'].sleep(.1);feed(r,rig,2)
    assert source.read_mode().frequency.acquired_s==frame.frequency.acquired_s
    assert source.read_frequency().acquired_s>frame.frequency.acquired_s
    rig['clock'].sleep(.5)
    with pytest.raises(MeasurementUnavailable):source.read_mode()


def test_slow_console_admission_never_sends_stale_pi(rig):
    r=receiver(rig);feed(r,rig,1)
    source=DatagramOpticalSource(r,rig['source'],quality_interval_s=.1,quality_max_age_s=.5,clock=rig['clock'])
    o=rig['owner'];o.observation=source;o.start_lock(3e14,rig['control'])
    rig['clock'].sleep(.05);feed(r,rig,2,value=300.000001)
    from contextlib import contextmanager
    @contextmanager
    def slow():
        rig['clock'].sleep(.3)
        yield
    rig['driver'].control_transaction=slow
    n=len(rig['driver'].writes);o.tick()
    assert len(rig['driver'].writes)==n and o.integral==0 and not o.fault


def test_pi_coalesces_but_scan_consumes_complete_udp_series(rig):
    r=receiver(rig);feed(r,rig,1)
    source=DatagramOpticalSource(r,rig['source'],quality_interval_s=.1,quality_max_age_s=.5,clock=rig['clock'])
    o=rig['owner'];o.observation=source
    spec=dict(rig['deck']['cards'][2]['parameters'],pzt_center_v=70.,period_s=.2,max_gap_s=.03,boundary_tolerance_s=.1,center_lock=False)
    o.start_scan(3e14,spec)
    for i in range(1,22):
        rig['clock'].sleep(.01);feed(r,rig,i+1)
    o.tick()
    assert len(o.events)==1 and o.events[0]['valid'] and o.events[0]['samples']>=20


def test_slow_console_replans_from_new_udp_without_losing_continuous_integral(rig):
    r=receiver(rig);feed(r,rig,1)
    source=DatagramOpticalSource(r,rig['source'],quality_interval_s=.1,quality_max_age_s=.5,clock=rig['clock'])
    o=rig['owner'];o.observation=source;o.start_lock(3e14,rig['control'])
    rig['clock'].sleep(.05);feed(r,rig,2,value=300.000001)
    original=rig['driver'].snapshot;seq=[2]
    def slow_with_data(channel):
        for _ in range(6):
            rig['clock'].sleep(.05);seq[0]+=1;feed(r,rig,seq[0],value=300.000002)
        return original(channel)
    rig['driver'].snapshot=slow_with_data
    before=len(rig['driver'].writes);o.tick()
    assert len(rig['driver'].writes)==before+1 and not o.fault
    assert o.last_observation['sequence']==seq[0] and o.integral<0
    saved=o.last_observation.copy();snap=o.snapshot()
    assert snap['observation']['sequence']>saved['sequence']
    assert snap['last_control_observation']==saved
    assert 0<=snap['observed_s']-snap['observation']['acquired_s']<.01


def test_loopback_datagram_listener_exclusive():
    # Only loopback; this suite is additionally run in an isolated network namespace.
    reservation=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
    reservation.bind(('127.0.0.1',0));port=reservation.getsockname()[1];reservation.close()
    cfg=config();cfg['listen_port']=port
    r=WSUReceiver(cfg);other=WSUReceiver(cfg)
    try:
        r.start()
        with pytest.raises(OSError):other.start()
        with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sender:
            sender.sendto(packet(1,time.time()),('127.0.0.1',port))
        deadline=time.monotonic()+1
        while r.snapshot()['accepted']==0 and time.monotonic()<deadline:time.sleep(.005)
        assert r.snapshot()['accepted']==1
    finally:r.close();other.close()


def test_target_epoch_does_not_reinterpret_old_buffered_measurements(rig):
    r=receiver(rig);feed(r,rig,1)
    source=DatagramOpticalSource(r,rig['source'],quality_interval_s=.1,quality_max_age_s=.5,clock=rig['clock'])
    o=rig['owner'];o.observation=source;o.start_lock(3e14,rig['control'])
    # Half a second of valid old-target data arrives while the console is busy.
    for seq in range(2,12):
        rig['clock'].sleep(.05);feed(r,rig,seq)
    o.integral=.01
    target=3e14+2e7;o.set_target(target,o.target_revision)
    n=len(rig['driver'].writes);o.tick()
    assert len(rig['driver'].writes)==n
    rig['clock'].sleep(.05);feed(r,rig,12,value=target/1e12)
    o.tick()
    # FF already wrote the PZT: fresh samples do not bypass its update interval.
    assert len(rig['driver'].writes)==n
    rig['clock'].sleep(.06);feed(r,rig,13,value=target/1e12)
    o.tick()
    assert len(rig['driver'].writes)==n+1
    assert o.integral==pytest.approx(.01)


def test_disabled_thf685_template_reports_not_ready_without_connections(monkeypatch):
    monkeypatch.setattr(socket,'create_connection',lambda *a,**k:pytest.fail('network access'))
    p=EXAMPLES/'thf685'
    laser=yaml.safe_load((p/'laser.yaml').read_text())
    issues=commissioning_issues(json.loads((p/'owner.json').read_text()),[laser])
    assert laser['id']=='thf685' and laser['name']=='ThF685'
    assert not any('hostname' in x for x in issues)
    assert json.loads((p/'owner.json').read_text())['expected_firmware_version']=='1.9.0.27a1cf0'
    assert laser['devices']['dlc']['control_settings']['optical_source']['channel']==5
    assert any('output authority is disabled' in x for x in issues)
    assert any('target' in x for x in issues)
