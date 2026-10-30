"""Offline timing boundaries: fresh pre-send checks and optional receive ACKs."""
from copy import deepcopy
import errno
import socket

import pytest

from ldcon.core.contracts import DeviceError, MeasurementUnavailable
from ldcon.drivers.dlcpro_console import Console, UncertainDLCWrite
from .test_dlcpro_console import Wire
from .test_dlcpro_offline import Clock, rig


def test_quickack_follows_each_fragment_without_changing_telnet_framing(monkeypatch):
    monkeypatch.setattr(socket, 'TCP_QUICKACK', 12, raising=False)
    wire = Wire([b'> ', b'\xff', b'\xfb\x01', b'12.', b'5\r\n', b'> '])
    options = []
    wire.setsockopt = lambda *args: options.append(args)
    console = Console(wire)
    assert console.command("(param-ref 'test)") == '12.5'
    assert options == [(socket.IPPROTO_TCP, 12, 1)]*6
    assert wire.sent == [b"(param-ref 'test)\n", b'\xff\xfe\x01']
    assert not console.broken


@pytest.mark.parametrize('code', [errno.ENOPROTOOPT, errno.EOPNOTSUPP])
def test_unsupported_quickack_keeps_original_reply_and_single_send(monkeypatch, code):
    monkeypatch.setattr(socket, 'TCP_QUICKACK', 12, raising=False)
    wire = Wire([b'> ', b'0\n', b'> '])
    attempts = []
    def unsupported(*args):
        attempts.append(args)
        raise OSError(code, 'unsupported option')
    wire.setsockopt = unsupported
    console = Console(wire)
    assert console.command('(fixture-write)', writing=True) == '0'
    assert len(attempts) == 3
    assert wire.sent == [b'(fixture-write)\n'] and not console.broken


def test_absent_quickack_option_needs_no_socket_configuration(monkeypatch):
    monkeypatch.delattr(socket, 'TCP_QUICKACK', raising=False)
    wire = Wire([b'> ', b'0\n> '])
    def unexpected(*args):
        raise AssertionError('No unsupported option should be requested')
    wire.setsockopt = unexpected
    console = Console(wire)
    assert console.command('(fixture-read)') == '0'


def test_quickack_never_extends_reply_deadline_or_retries_write(monkeypatch):
    monkeypatch.setattr(socket, 'TCP_QUICKACK', 12, raising=False)
    wire = Wire([b'> ', b'0', b'\n', b'> '])
    wire.setsockopt = lambda *args: None
    console = Console(wire, timeout_s=1.)
    clock = Clock()
    monkeypatch.setattr('ldcon.drivers.dlcpro_console.time.monotonic', clock)
    original = wire.recv
    def delayed(size):
        clock.sleep(.6)
        return original(size)
    wire.recv = delayed
    with pytest.raises(UncertainDLCWrite):
        console.command('(fixture-write)', writing=True)
    assert console.broken and wire.sent == [b'(fixture-write)\n']
    with pytest.raises(DeviceError, match='inspection'):
        console.command('(fixture-write)', writing=True)
    assert wire.sent == [b'(fixture-write)\n']


def test_write_timing_separates_storage_prewrite_and_driver(rig, monkeypatch):
    inst, owner, driver, clock = (rig[k] for k in ('instrument', 'owner', 'driver', 'clock'))
    monkeypatch.setattr('ldcon.owners.dlcpro_owner.time.monotonic', clock)
    events, phases = [], []
    def audit(event):
        events.append(deepcopy(event))
        phases.append(event['event'])
        clock.sleep({'write_intent': .07, 'write_verified': .04}[event['event']])
    inst.audit = audit
    original = driver.write
    def write(*args, **kwargs):
        phases.append('driver')
        clock.sleep(.03)
        return original(*args, **kwargs)
    monkeypatch.setattr(driver, 'write', write)
    def prewrite():
        phases.append('prewrite')
        clock.sleep(.02)
    path = driver.path(1, 'pc:voltage-set')
    assert inst._write(1, owner.lease, path, 70.1, compact=True, prewrite=prewrite) == 70.1
    assert phases == ['write_intent', 'prewrite', 'driver', 'write_verified']
    timing = inst.last_write_timing
    assert timing['outcome'] == 'verified' and timing['channel'] == 1
    for key, expected in dict(intent_audit_s=.07, prewrite_check_s=.02,
                              driver_s=.03, verified_audit_s=.04, total_s=.16).items():
        assert timing[key] == pytest.approx(expected)
    recorded = events[-1]['write_timing']
    assert recorded['driver_s'] == pytest.approx(.03)
    assert recorded['intent_audit_s'] == pytest.approx(.07)
    assert recorded['verified_audit_s'] is None  # Only known after this event persists.
    assert inst.confirmed_outputs[1]['piezo'] == 70.1


@pytest.mark.parametrize('error_type', [MeasurementUnavailable, DeviceError, ValueError])
def test_durable_intent_then_rejected_prewrite_is_closed_without_send(rig, monkeypatch, error_type):
    inst, owner, driver, clock = (rig[k] for k in ('instrument', 'owner', 'driver', 'clock'))
    inst.snapshot(1, owner.lease)
    before = deepcopy(inst.confirmed_outputs)
    events = []
    monkeypatch.setattr('ldcon.owners.dlcpro_owner.time.monotonic', clock)
    acquired = clock()
    def audit(event):
        events.append(deepcopy(event))
        if event['event'] == 'write_intent':
            clock.sleep(.21)
    inst.audit = audit
    def prewrite():
        assert events[-1]['event'] == 'write_intent'
        assert clock()-acquired > .2
        raise error_type('Fresh observation rejected after durable intent')
    with pytest.raises(error_type, match='Fresh observation'):
        inst._write(1, owner.lease, driver.path(1, 'pc:voltage-set'), 70.1,
                    prewrite=prewrite)
    assert not driver.writes and inst.confirmed_outputs == before
    assert inst.channels[1]['fault'] is None
    assert inst.channels[1]['lease'] == owner.lease
    assert [e['event'] for e in events] == ['write_intent', 'write_reviewed_not_applied']
    assert events[-1]['exception_type'] == error_type.__name__
    assert events[-1]['write_timing']['intent_audit_s'] == pytest.approx(.21)
    assert inst.last_write_timing['outcome'] == 'not_applied'
    assert inst.last_write_timing['driver_s'] is None


def test_unknown_driver_write_still_faults_retains_lease_and_never_replays(rig, monkeypatch):
    inst, owner, driver, clock = (rig[k] for k in ('instrument', 'owner', 'driver', 'clock'))
    monkeypatch.setattr('ldcon.owners.dlcpro_owner.time.monotonic', clock)
    events = []
    inst.audit = events.append
    driver.fail = True
    path = driver.path(1, 'pc:voltage-set')
    with pytest.raises(UncertainDLCWrite):
        inst._write(1, owner.lease, path, 70.1, prewrite=lambda: None)
    assert inst.channels[1]['fault'] == 'write outcome uncertain'
    assert inst.channels[1]['lease'] == owner.lease and len(driver.writes) == 1
    assert [e['event'] for e in events] == ['write_intent', 'write_uncertain']
    assert events[-1]['write_timing']['outcome'] == 'uncertain'
    with pytest.raises(DeviceError, match='quarantined'):
        inst._write(1, owner.lease, path, 70.1, prewrite=lambda: None)
    with pytest.raises(DeviceError):
        inst.release(1, owner.lease)
    assert len(driver.writes) == 1


def test_intent_storage_failure_never_reaches_prewrite_or_driver(rig):
    inst, owner, driver = (rig[k] for k in ('instrument', 'owner', 'driver'))
    checked = []
    def audit(event):
        raise OSError('intent persistence unavailable')
    inst.audit = audit
    with pytest.raises(OSError, match='persistence'):
        inst._write(1, owner.lease, driver.path(1, 'pc:voltage-set'), 70.1,
                    prewrite=lambda: checked.append(True))
    assert not checked and not driver.writes
    assert inst.channels[1]['fault'] is None
    assert inst.last_write_timing['driver_s'] is None
