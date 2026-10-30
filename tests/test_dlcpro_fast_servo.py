"""Servo isolation, stale readbacks and ordered console transactions; no sockets."""
from contextlib import nullcontext
from types import SimpleNamespace
import threading

import pytest

from ldcon.core.contracts import DeviceError, MeasurementUnavailable
from ldcon.drivers.dlcpro_priority import PriorityLock
from ldcon.owners.dlcpro_readback import HardwareReadback
from ldcon.jobs.dlcpro_cards import Protocol
from .test_dlcpro_offline import rig, advance, preserved_rig


def test_servo_and_centering_do_not_poll_chassis(rig, monkeypatch):
    o, d = rig['owner'], rig['driver']
    o.start_lock(3e14, dict(rig['control'], tec_period_s=.1, pzt_center_v=69.9))
    monkeypatch.setattr(d, 'snapshot', lambda *a: pytest.fail('bulk read blocks servo'))
    monkeypatch.setattr(d, 'read', lambda *a: pytest.fail('unrelated read blocks servo'))
    rig['source'].bias = 1e6
    advance(rig, 1)
    assert not o.fault and o.mode == 'lock'
    assert any('pc:voltage-set' in p for p, _ in d.writes)
    assert any('tc:temp-set' in p for p, _ in d.writes)


@pytest.mark.parametrize('side',[-1,1])
def test_explicit_piezo_return_can_move_inward_from_measured_edge(rig,side):
    r=preserved_rig(rig);o,d,i=r['owner'],r['driver'],r['instrument']
    low,high=o.policy['axes']['piezo']['bounds'];edge=high if side>0 else low
    d.state['piezo']=edge
    original=d.snapshot
    def measured(channel):
        snap=original(channel);snap['piezo']['measured']=edge+side*.15
        return snap
    d.snapshot=measured
    # Admission/unrelated moves and non-inward commands still refuse.
    for axis,value in [('temperature',25.),('piezo',edge),('piezo',edge+side*.01)]:
        with pytest.raises(ValueError):o.move(axis,value)
    with pytest.raises(ValueError):i.configure_ff(1,o.lease)
    assert not d.writes
    o.move('piezo',edge-side*1.)
    assert d.writes==[('laser1:dl:pc:voltage-set',edge-side*1.)]


def test_inward_return_still_requires_current_envelope_and_verified_write(rig):
    r=preserved_rig(rig);o,d,i=r['owner'],r['driver'],r['instrument']
    high=o.policy['axes']['piezo']['bounds'][1];d.state['piezo']=high
    original=d.snapshot
    def measured(channel):
        snap=original(channel);snap['piezo']['measured']=high+.15
        snap['current']['measured']=1e6
        return snap
    d.snapshot=measured
    with pytest.raises(ValueError):o.move('piezo',high-1.)
    assert not d.writes


def test_fast_path_absolute_limits_ownership_and_prewrite_freshness(rig):
    i, o, d = rig['instrument'], rig['owner'], rig['driver']
    for token, value, error in [('bad', 70, DeviceError), (o.lease, 1000, ValueError)]:
        with pytest.raises(error):
            i.servo_piezo(1, token, lambda: (value, lambda: None))
    def stale():
        raise MeasurementUnavailable('expired')
    with pytest.raises(MeasurementUnavailable):
        i.servo_piezo(1, o.lease, lambda: (70, stale))
    assert not d.writes


def test_inspect_and_heartbeat_do_not_compete_with_pi(rig, monkeypatch):
    o, i, d = rig['owner'], rig['instrument'], rig['driver']
    o.start_lock(3e14, rig['control'])
    hardware = d.snapshot(1)
    i.readback = SimpleNamespace(read=lambda channel: (hardware, {'fresh':True, 'age_s':1.}))
    protocol = Protocol(o, rig['device'])
    protocol.client = 'client'
    monkeypatch.setattr(d, 'snapshot', lambda *a: pytest.fail('display must use timestamped cache'))
    for action in ('inspect', 'heartbeat'):
        result = protocol.dispatch(dict(action=action, laser_id=o.laser_id, lease='client'))
        snapshot = result['snapshot'] if action == 'inspect' else result
        assert snapshot['pi_enabled'] and snapshot['hardware_readback']['age_s'] == 1
        assert snapshot['outputs']['piezo'] == 70


def test_readback_expiry_and_failure_never_redate_old_hardware(rig):
    d, c = rig['driver'], rig['clock']
    d.console = SimpleNamespace(lock=SimpleNamespace(background=nullcontext))
    faults = []
    cache = HardwareReadback(d, [1], clock=c, max_age_s=2, fault=lambda *args: faults.append(args))
    cache.refresh(1)
    assert cache.read(1)[0]['piezo']['measured'] == 70
    c.sleep(3)
    assert cache.read(1)[0] == {} and not cache.read(1)[1]['fresh']
    original = d.snapshot
    d.snapshot = lambda channel: {**original(channel), 'tec_fault':True}
    cache.refresh(1)
    assert faults == [(1, 'DLC TEC fault')]
    def fail(*args):
        raise OSError('disconnected')
    d.snapshot = fail
    cache.refresh(1)
    assert cache.read(1)[0] == {} and cache.read(1)[1]['error'] == 'OSError'


def test_control_transaction_has_priority_and_is_reentrant():
    lock = PriorityLock()
    order = []
    ready = threading.Event()
    def control():
        ready.set()
        with lock:
            order.append('write')
            with lock:
                order.append('verify')
    def monitor():
        with lock.background(), lock:
            order.append('monitor')
    with lock:
        a = threading.Thread(target=control)
        a.start()
        assert ready.wait(1)
        with lock.condition:
            # Wait for the control waiter without releasing the transport.
            assert lock.condition.wait_for(lambda: lock.waiting_control == 1, timeout=1)
        b = threading.Thread(target=monitor)
        b.start()
    a.join(2); b.join(2)
    assert not a.is_alive() and not b.is_alive()
    assert order == ['write', 'verify', 'monitor']
