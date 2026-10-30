"""No hardware: mislabelled UDP values cannot drive or integrate in host PI."""
import pytest

from ldcon.core.contracts import MeasurementUnavailable
from ldcon.owners.dlcpro_frequency_window import FrequencyWindow
from ldcon.owners.dlcpro_observation import DatagramOpticalSource
from .test_dlcpro_offline import rig
from .test_dlcpro_udp import receiver, feed


def prepared(rig):
    r=receiver(rig);feed(r,rig,1)
    o=rig['owner']
    o.frequency_window=FrequencyWindow(dict(frequency_hz=3e14,
        allowed_change_hz=[-3e12,3e12]),rig['events'].append)
    o.observation=DatagramOpticalSource(r,rig['source'],quality_interval_s=.1,
        quality_max_age_s=.5,clock=rig['clock'])
    o.start_lock(3e14,dict(rig['control'],tec_period_s=.1,pzt_center_v=69.9))
    return o,r


def test_wrong_band_holds_pi_integral_and_tec_then_resumes_without_gap_integral(rig):
    o,r=prepared(rig);o.integral=.01
    original=dict(rig['driver'].state);last=o.last_valid
    rig['clock'].sleep(.1);feed(r,rig,2,value=710.9628388)
    o.tick()
    assert not rig['driver'].writes and rig['driver'].state==original
    assert o.integral==.01 and o.last_valid==last and o.mode=='lock' and not o.fault
    snapshot=o.snapshot()
    assert snapshot['control_frequency_window']['held']
    assert snapshot['control_frequency_window']['rejected_samples']==1
    assert snapshot['control_frequency_window']['last_rejection']['sequence']==2
    rig['clock'].sleep(.5);feed(r,rig,3,value=300.000001)
    o.heartbeat();o.tick()
    assert o.integral==.01  # no integration of the held interval or bad packet
    assert any('pc:voltage-set' in key for key,_ in rig['driver'].writes)
    assert not o.frequency_window.held and not o.fault


def test_invalid_buffered_packet_never_enters_integral_when_latest_is_valid(rig):
    o,r=prepared(rig);o.integral=.01
    rig['clock'].sleep(.05);feed(r,rig,2,value=710.9628388)
    rig['clock'].sleep(.06);feed(r,rig,3,value=300.000001)
    o.tick()
    expected=70.+.01-o.control['kp']*1e6/o.policy['axes']['piezo']['frequency_hz_per_unit']
    assert o.integral==.01 and o.applied_pzt==pytest.approx(expected)
    assert not o.fault


def test_range_reject_before_write_when_source_changes_during_console_wait(rig):
    from contextlib import contextmanager
    o,r=prepared(rig)
    rig['clock'].sleep(.1);feed(r,rig,2)
    @contextmanager
    def transaction():
        rig['clock'].sleep(.01);feed(r,rig,3,value=710.9628388)
        yield
    rig['driver'].control_transaction=transaction
    o.tick()
    assert not rig['driver'].writes and o.integral==0 and not o.fault


def test_continuous_fresh_out_of_range_packets_keep_existing_missing_deadline(rig):
    o,r=prepared(rig)
    for seq in range(2,27):
        rig['clock'].sleep(.1);feed(r,rig,seq,value=710.9628388)
        o.heartbeat();o.tick()
    assert o.mode=='idle' and o.fault=='control frequency deadline exceeded'
    assert not rig['driver'].writes


def test_inclusive_window_follows_live_target_not_startup_target(rig):
    o,r=prepared(rig)
    for seq,value in [(2,297.),(3,303.)]:
        rig['clock'].sleep(.01);feed(r,rig,seq,value=value)
        assert o.read_control().frequency_hz==value*1e12
    rig['clock'].sleep(.01);feed(r,rig,4,value=303.000001)
    with pytest.raises(MeasurementUnavailable,match='target range'):o.read_control()
    o.target=3e14+2e6
    assert o.read_control().frequency_hz==303.000001e12
    assert o.frequency_window.view(o.target)['bounds_hz']==[297e12+2e6,303e12+2e6]


def test_scan_marks_cycle_invalid_for_coalesced_wrong_band_packet(rig):
    o,r=prepared(rig);o.stop()
    spec=dict(rig['deck']['cards'][2]['parameters'],pzt_center_v=70.,period_s=.2,
        max_gap_s=.03,boundary_tolerance_s=.1,center_lock=False)
    o.start_scan(3e14,spec)
    for i in range(1,22):
        rig['clock'].sleep(.01);feed(r,rig,i+1,value=710.9628388 if i==10 else 300.)
    o.tick()
    assert len(o.events)==1 and not o.events[0]['valid']
    assert o.mode=='scan' and not o.fault


def test_restart_uses_requested_target_for_input_window(rig):
    o,r=prepared(rig);o.stop()
    target=303e12+1e6
    rig['clock'].sleep(.01);feed(r,rig,2,value=target/1e12)
    o.start_lock(target,rig['control'])
    assert o.mode=='lock' and o.target==target
