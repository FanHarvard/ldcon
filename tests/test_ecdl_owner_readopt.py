"""sr422 rig owner restart with a running native PZT PI: no write, no lease release, no fault; the next owner adopts
the same lease after a READ-ONLY verification (design card-system-design.md §5 "owner 再起動とロックの再採用", ECDL row,
2026-10-27). Mirrors test_native_owner_readopt.py over the real ECDLOwner / NativeLockOwner / RigHardware code and
the SCPI + esp32servocon emulator of owner/tests/fake_rig.py. Fakes only: no hardware, no network.
"""
import copy
import importlib.util
import json
from pathlib import Path
import socket
import threading

import pytest

from ldcon.core.contracts import DeviceError, MeasurementUnavailable, OwnerUnavailable
from ldcon.core.instrument_client import BrokerError
from ldcon.core.interface_schema import digest
from ldcon.jobs.ecdl_devices import OwnerSession, SteadyLockProxy
from ldcon.jobs.ecdl_steady import parameters, run_steady_lock
from ldcon.owners.ecdl_owner import ECDLOwner, RigHardware
from ldcon.owners.ecdl_steady_owner import NativeLockOwner
from ldcon.owners import remote_instruments

_spec = importlib.util.spec_from_file_location('ecdl_readopt_fake_rig',
    (Path(__file__).resolve().parent / 'support') / 'owner/tests/fake_rig.py')
fake_rig = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fake_rig)

REMOTE_LEASES = {'current_awg': {'1': 'bk-ch1-token', '2': 'bk-ch2-token'}, 'tec_awg': {'1': 'sdg-ch1-token'}}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError('No hardware/network access in offline test')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    import ldcon.drivers.siglent as siglent
    monkeypatch.setattr(siglent, 'VisaTransport', fake_rig.Visa)
    monkeypatch.setattr(NativeLockOwner, 'watch_enabled', False)        # tests drive watch_tick() with the fake clock
    monkeypatch.setattr(NativeLockOwner, 'passive_dither', lambda self: None)   # no telemetry fit offline


class Rig(fake_rig.FakeRig):
    """The emulated rig plus the rule-G lease bookkeeping of the real RigHardware (lease_tokens/detach)."""
    def __init__(self, device, clock, previous=None, adopt_leases=None):
        super().__init__(device, clock)
        self.adopt_leases = adopt_leases
        self.closed = self.detached = False
        if previous is not None:
            # The same physical instruments (firmware config, AWG channels) seen by a NEW owner process.
            self.bk, self.sdg, self.firmware = previous.bk, previous.sdg, previous.firmware
            fake_rig.Visa.registry.update({'192.168.1.21': self.bk, '192.168.1.37': self.sdg})
            self.connections = dict(piezo=self.firmware, current_awg=fake_rig.awg_over(self.bk, '192.168.1.21'),
                                    tec_awg=fake_rig.awg_over(self.sdg, '192.168.1.37'))
            self.identities = {k: io.describe() for k, io in self.connections.items() if k != 'piezo'}
            self.leases = copy.deepcopy(adopt_leases or {})
        else:
            self.leases = copy.deepcopy(REMOTE_LEASES)

    def lease_tokens(self):
        return copy.deepcopy(self.leases)

    def detach(self):
        self.detached = True

    def close(self):
        self.closed = True
        self.leases = {}


class Clock:
    def __init__(self):
        self.now = 100.
    def __call__(self):
        return self.now
    def advance(self, seconds=1.):
        self.now += seconds


def make_owner(tmp_path, clock, holder, previous=None):
    def factory(device, adopt_leases=None):
        holder['hw'] = Rig(device, clock, previous=previous, adopt_leases=adopt_leases)
        return holder['hw']
    return ECDLOwner(fake_rig.laser_definition(), audit_path=tmp_path / 'audit.jsonl', factory=factory, clock=clock)


@pytest.fixture
def rig(tmp_path):
    clock, holder = Clock(), {}
    owner = make_owner(tmp_path, clock, holder)
    lease = owner.dispatch(dict(action='claim', revision=digest(owner.device), owner='test'))['lease']
    return owner, holder, clock, lease


def call(owner, action, **values):
    return owner.dispatch(dict(action=action, lease=owner.lease, **values))


def lock(owner, hw):
    assert call(owner, 'steady_ready', strategy='hold')['ready']
    call(owner, 'steady_ready', strategy='hold')
    f0 = hw.firmware.frequency()
    assert call(owner, 'steady_arm', strategy='hold', target_frequency_hz=f0, tolerance_hz=5e6)['locked']
    assert hw.firmware.cfg['pid_multiplier'] == 1 and owner.steady.armed and owner.steady.active
    return f0


def shutdown(owner):
    """The real lifecycle: SIGTERM -> instrument_service.Hub.close(hold=False) -> ECDLOwner.close(hold=False)."""
    return owner.close(hold=False)


def restart(owner, tmp_path, clock, holder):
    previous = holder['hw']
    new_holder = {}
    restarted = make_owner(tmp_path, clock, new_holder, previous=previous)
    holder.update(new_holder)
    return restarted


def audit_kinds(owner):
    return [json.loads(line)['kind'] for line in owner.audit_path.read_text().splitlines()]


# ---- deliberate shutdown ------------------------------------------------------------------------------------------

def test_deliberate_shutdown_with_armed_lock_writes_nothing_and_latches_nothing(rig, tmp_path):
    owner, holder, clock, lease = rig
    hw = holder['hw']
    lock(owner, hw)
    call(owner, 'steady_heartbeat')
    writes = len(hw.firmware.sets)
    assert shutdown(owner) is True
    assert hw.firmware.cfg['pid_multiplier'] == 1 and len(hw.firmware.sets) == writes
    assert owner.fault is None and owner.lease == lease and owner.steady.armed
    assert hw.detached and not hw.closed and hw.leases == REMOTE_LEASES   # BK/SDG channel leases retained, not released
    assert owner.steady.stop_event.is_set()                                # no watchdog hold after the process stops serving
    saved = json.loads(owner.state_path.read_text())
    assert saved['active'] is True and saved['lease'] == lease and saved['fault'] is None
    assert saved['activation'] == owner.activation and saved['remote_leases'] == REMOTE_LEASES
    steady = saved['steady']
    assert steady['profile']['firmware_version'] == '2026.10.21-antiwindup' and steady['strategy'] == 'hold'
    assert steady['expected_servo']['pid_multiplier'] == 1 and steady['arm_reference'] == owner.steady.arm_reference
    assert steady['expected_servo']['pid_ki'] == pytest.approx(hw.firmware.cfg['pid_ki'])
    assert steady['tec_reference_lease'] == lease and steady['in_flight'] == [] and steady['target'] == owner.steady.target
    assert saved['shutdown']['lease'] == lease and saved['shutdown']['heartbeat_remaining_s'] == pytest.approx(90.)
    assert 'owner_shutdown_active_lock' in audit_kinds(owner) and 'native_hold_requested' not in audit_kinds(owner)


def test_shutdown_without_an_armed_lock_keeps_the_previous_release_and_clean_restart(rig, tmp_path):
    owner, holder, clock, lease = rig
    hw = holder['hw']
    shutdown(owner)
    assert hw.closed and not hw.detached and hw.leases == {}              # 2026-10-22 behaviour: owner leases released
    restarted = restart(owner, tmp_path, clock, holder)
    assert restarted.lease is None and restarted.fault is None and restarted.adoption is None
    assert not restarted.steady.armed


# ---- restart: adoption ----------------------------------------------------------------------------------------------

def test_restart_adopts_the_running_pi_when_the_rig_matches(rig, tmp_path):
    owner, holder, clock, lease = rig
    hw = holder['hw']
    lock(owner, hw)
    clock.advance(1.)
    call(owner, 'steady_heartbeat')
    shutdown(owner)
    writes = len(hw.firmware.sets)
    clock.advance(30.)                                   # restart gap
    restarted = restart(owner, tmp_path, clock, holder)
    new_hw = holder['hw']
    assert new_hw is not hw and new_hw.adopt_leases == REMOTE_LEASES      # adopted, not claimed, on the BK/SDG owners
    assert restarted.fault is None and restarted.lease == lease
    assert restarted.steady.armed and restarted.steady.active and restarted.steady.planner is not None
    a = restarted.adoption
    assert a['state'] == 'adopted' and a['attempts'] == 1 and a['pid_multiplier'] == 1 and a['heartbeat_grace_s'] == 90.
    assert restarted.steady.deadline == pytest.approx(clock() + 90.)
    assert restarted.activation == owner.activation and restarted.steady.target == owner.steady.target
    assert restarted.steady.tec_reference_dac == owner.steady.tec_reference_dac
    assert len(hw.firmware.sets) == writes and hw.firmware.cfg['pid_multiplier'] == 1
    assert 'owner_restart_adopted' in audit_kinds(restarted)
    assert json.loads(restarted.state_path.read_text())['adoption']['state'] == 'adopted'
    # Supervision resumes with the same lease: heartbeat, cycle reads, status; still no write.
    for _ in range(3):
        clock.advance(1.)
        assert call(restarted, 'steady_heartbeat') is True
        row = call(restarted, 'steady_read')
        assert row['state'] == 'locked' and row['tec_center_reference_dac'] == owner.steady.tec_reference_dac
    assert len(hw.firmware.sets) == writes
    status = restarted.dispatch(dict(action='status'))
    assert status['native_pi_active'] and status['adoption']['state'] == 'adopted' and status['last_shutdown']['lease'] == lease
    inspect = restarted.dispatch(dict(action='inspect'))
    assert inspect['native_pi_active'] and inspect['lease_retained'] and inspect['adoption']['state'] == 'adopted'
    # The adopted lock is stoppable and releasable by the worker as before (the one typed PI-off write).
    held = call(restarted, 'steady_hold', reason='test')
    assert held['held'] and hw.firmware.cfg['pid_multiplier'] == 0
    assert call(restarted, 'release') is True
    assert restarted.lease is None and not restarted.state_path.exists() and restarted.adoption is None


def test_adoption_grace_then_the_existing_heartbeat_deadline(rig, tmp_path):
    owner, holder, clock, lease = rig
    hw = holder['hw']
    lock(owner, hw)
    shutdown(owner)
    clock.advance(600.)                                  # down far longer than watchdog_s (90 s)
    restarted = restart(owner, tmp_path, clock, holder)
    assert restarted.adoption['state'] == 'adopted' and restarted.fault is None
    writes = len(hw.firmware.sets)
    clock.advance(89.)                                   # inside the grace, no heartbeat yet: nothing happens
    assert restarted.steady.watch_tick() is True
    assert restarted.steady.armed and hw.firmware.cfg['pid_multiplier'] == 1 and len(hw.firmware.sets) == writes
    clock.advance(2.)                                    # grace expired, worker gone: the existing watchdog hold
    assert restarted.steady.watch_tick() is False
    assert not restarted.steady.armed and hw.firmware.cfg['pid_multiplier'] == 0 and restarted.fault is None
    assert 'native_hold_applied' in audit_kinds(restarted)


@pytest.mark.parametrize('damage', ['pi_off', 'config', 'firmware', 'in_flight', 'dither', 'window'])
def test_restart_refuses_precisely_when_the_rig_differs(rig, tmp_path, damage):
    owner, holder, clock, lease = rig
    hw = holder['hw']
    lock(owner, hw)
    shutdown(owner)
    if damage == 'pi_off':
        hw.firmware.cfg['pid_multiplier'] = 0            # a human stopped it at the device
        expected = 'Native PI ownership lost'
    elif damage == 'config':
        hw.firmware.cfg['pid_ki'] *= 2                   # foreign gain change while no owner watched
        expected = 'controlled configuration changed'
    elif damage == 'firmware':
        get = hw.firmware.get
        hw.firmware.get = lambda path: dict(get(path), firmware_version='other') if path == 'status' else get(path)
        expected = 'Firmware identity changed'
    elif damage == 'in_flight':
        saved = json.loads(owner.state_path.read_text())
        saved['steady']['in_flight'] = ['locked sweep (playing)']
        owner.state_path.write_text(json.dumps(saved))
        expected = 'locked sweep (playing)'
    elif damage == 'dither':
        hw.bk.ch[1].arb = 'other'                        # the current AWG no longer plays the verified dither
        expected = 'differs from verified dither'
    else:
        hw.sdg.ch[1].wave['OFST'] = '0.9V'               # TEC moved far outside the activation-relative window
        expected = 'outside'
    writes = len(hw.firmware.sets)
    restarted = restart(owner, tmp_path, clock, holder)
    assert restarted.fault and 'adoption refused' in restarted.fault and expected in restarted.fault
    assert 'native PI not touched, leases retained' in restarted.fault
    assert restarted.adoption['state'] == 'refused' and restarted.lease == lease
    assert not restarted.steady.armed and len(hw.firmware.sets) == writes      # quarantine: nothing written
    quarantine = json.loads(restarted.quarantine_path.read_text())
    assert quarantine['fault'] == restarted.fault and quarantine['lease'] == lease and quarantine['adoption']['state'] == 'refused'
    with pytest.raises(BrokerError) as exc:
        call(restarted, 'steady_heartbeat')
    assert exc.value.code == 'fault' and 'adoption refused' in str(exc.value)
    assert len(hw.firmware.sets) == writes
    # A refused adoption no longer prevents the service from starting: the next start is faulted with the lease
    # retained (offline review), the firmware untouched, instead of 'requires its original supervisor'.
    again = restart(restarted, tmp_path, clock, holder)
    assert again.fault == restarted.fault and again.lease == lease and not again.steady.armed
    assert again.adoption['state'] == 'refused' and len(hw.firmware.sets) == writes


def test_unreachable_rig_keeps_adoption_pending_then_adopts_or_refuses(rig, tmp_path):
    owner, holder, clock, lease = rig
    hw = holder['hw']
    lock(owner, hw)
    shutdown(owner)
    get = hw.firmware.get
    def unreachable(path):
        raise OSError(111, 'Connection refused')
    hw.firmware.get = unreachable
    restarted = restart(owner, tmp_path, clock, holder)
    a = restarted.adoption
    assert a['state'] == 'pending' and a['attempts'] == 1 and restarted.fault is None and restarted.steady.armed
    assert 'Connection refused' in a['last_error']
    with pytest.raises(BrokerError) as exc:                      # no write, no fresh claim while pending
        call(restarted, 'steady_read')
    assert exc.value.code == 'measurement' and 'adoption pending' in str(exc.value)
    assert call(restarted, 'steady_heartbeat') is True           # liveness is accepted meanwhile
    clock.advance(1.)
    assert restarted.steady.watch_tick() is True and a['state'] == 'pending' and a['attempts'] == 2
    hw.firmware.get = get
    clock.advance(1.)
    restarted.dispatch(dict(action='inspect'))                   # a human inspect retries the adoption too
    assert a['state'] == 'adopted' and a['attempts'] == 3 and restarted.fault is None
    assert hw.firmware.cfg['pid_multiplier'] == 1
    # Bounded: never reachable within watchdog_s -> precise refusal, no write.
    shutdown(restarted)
    hw.firmware.get = unreachable
    again = restart(restarted, tmp_path, clock, holder)
    writes = len(hw.firmware.sets)
    clock.advance(91.)
    assert again.steady.watch_tick() is False
    assert again.fault and 'unreadable for 91 s' in again.fault and len(hw.firmware.sets) == writes


def test_persisted_context_follows_runtime_changes(rig, tmp_path):
    owner, holder, clock, lease = rig
    hw = holder['hw']
    lock(owner, hw)
    clock.advance(1.)
    call(owner, 'steady_read')
    status = call(owner, 'steady_parameters_status')
    patch = dict(target_frequency_hz=owner.steady.target + 2e6, tolerance_hz=status['parameters']['tolerance_hz'])
    call(owner, 'steady_parameters', expected_revision=status['revision'], patch=patch)
    saved = json.loads(owner.state_path.read_text())['steady']
    assert saved['target'] == pytest.approx(owner.steady.target) and saved['parameter_sequence'] == owner.steady.parameter_sequence
    assert saved['target_ramp_active'] is True
    shutdown(owner)
    restarted = restart(owner, tmp_path, clock, holder)
    assert restarted.adoption['state'] == 'adopted'
    assert restarted.steady.target == pytest.approx(owner.steady.target) and restarted.steady.current_target == owner.steady.current_target
    assert restarted.steady.parameter_sequence == owner.steady.parameter_sequence
    for _ in range(3):                                   # the persisted target ramp resumes under the same lease
        clock.advance(1.)
        call(restarted, 'steady_heartbeat')
        call(restarted, 'steady_read')
    assert restarted.steady.current_target == pytest.approx(restarted.steady.target) and restarted.fault is None


# ---- rule-G lease adoption on the BK/SDG owners ---------------------------------------------------------------------

class Link:
    def __init__(self, honoured=True):
        self.calls, self.honoured, self.instrument = [], honoured, 'sdg2042x:SDG2XFBCA00845'
        self.description = dict(instrument=self.instrument, host='192.168.1.38', identity='SDG', profile='siglent_sdg2000x', connected=True)
    def rpc(self, action, **values):
        self.calls.append((action, values.get('lease')))
        if action == 'transaction' and not self.honoured:
            raise BrokerError('ownership', 'no matching channel lease')
        return ['C1:OUTP ON,LOAD,HZ,PLRT,NOR']


def test_remote_awg_adopts_retained_leases_read_only_and_detaches_without_release(monkeypatch):
    link = Link()
    monkeypatch.setattr(remote_instruments, 'resolve_link', lambda spec, **kw: link)
    awg = remote_instruments.RemoteAWG.adopt(dict(instrument=link.instrument, channels=[1, 2]), {'1': 't1', '2': 't2'})
    assert awg.leases == {1: 't1', 2: 't2'} and awg.projection is None
    assert link.calls == [('transaction', 't1'), ('transaction', 't2')]      # one read-only query per lease, no claim
    awg.detach()
    assert awg.link is None and awg.leases == {1: 't1', 2: 't2'} and not any(a == 'release' for a, _ in link.calls)
    gone = Link(honoured=False)
    monkeypatch.setattr(remote_instruments, 'resolve_link', lambda spec, **kw: gone)
    with pytest.raises(DeviceError, match='not honoured by its owner \\(ownership\\)'):
        remote_instruments.RemoteAWG.adopt(dict(instrument=link.instrument), {'1': 't1'})
    hardware = RigHardware.__new__(RigHardware)
    hardware.transports, hardware.locks, hardware.connections = dict(current_awg=awg), [], {}
    assert hardware.lease_tokens() == {'current_awg': {'1': 't1', '2': 't2'}}
    hardware.detach()
    assert awg.leases == {1: 't1', 2: 't2'}


# ---- worker side ------------------------------------------------------------------------------------------------------

class Session(OwnerSession):
    """The card's typed session routed to whichever owner process is 'running'; None models connection refused."""
    def __init__(self, device, owner):
        super().__init__(device)
        self.owner, self.calls, self.failure = owner, [], None
    def rpc(self, action, _timeout=30, **values):
        self.calls.append(action)
        if self.failure is not None:
            raise self.failure
        if self.owner is None:
            raise ConnectionRefusedError(111, 'Connection refused')
        try:
            return self.owner.dispatch(dict(action=action, **values))
        except BrokerError:
            raise
        except (KeyError, ValueError, TypeError) as exc:
            raise BrokerError('request', str(exc)) from exc


def worker(rig):
    owner, holder, clock, lease = rig
    session = Session(owner.device, owner)
    session.lease = lease
    events = []
    return SteadyLockProxy(session), session, events, lambda kind, row: events.append((kind, row))


def steady_parameters(hw, **overrides):
    return parameters(dict(parameters={}), dict(target_frequency_hz=hw.firmware.frequency(), tolerance_hz=5e6,
                                                 strategy='hold', **overrides))


def test_worker_retries_through_an_owner_restart_and_continues_with_the_same_lease(rig, tmp_path):
    owner, holder, clock, lease = rig
    hw = holder['hw']
    proxy, session, events, emit = worker(rig)
    timeline = dict(down_at=103., up_at=106., stopped=False, restarted=None)
    def sleep(seconds):
        clock.advance(seconds)
        if not timeline['stopped'] and clock() >= timeline['down_at']:
            timeline['stopped'] = True
            shutdown(owner)                              # systemctl restart: old owner gone, PI left running
            session.owner = None
        elif timeline['restarted'] is None and clock() >= timeline['up_at']:
            timeline['restarted'] = restart(owner, tmp_path, clock, holder)
            session.owner = timeline['restarted']
    p = steady_parameters(hw, duration_s=10.)
    result = run_steady_lock(proxy, p, clock=clock, sleep=sleep, cancel=lambda: False, emit=emit)
    assert result['status'] == 'succeeded' and result['cycles'] >= 4
    new = timeline['restarted']
    assert new is not None and new.adoption['state'] == 'adopted' and new.fault is None and new.steady.armed is False
    assert session.lease == lease == new.lease and not session.uncertain_output
    enables = [s for s in hw.firmware.sets if 'pid_multiplier' in s]
    assert enables == [dict(pid_multiplier=1), dict(control_offset=enables[-1]['control_offset'], pid_multiplier=0,
                                                   multiplier_control=0, multiplier_target=0, software_modulation_enable=0)]
    kinds = [k for k, _ in events]
    assert 'owner_wait' in kinds and 'owner_recovered' in kinds and 'ecdl_lock_held' in kinds
    wait = next(row for k, row in events if k == 'owner_wait')
    assert wait['native_pi_active'] and wait['timeout_s'] == 60.
    assert [row['state'] for k, row in events if k == 'state'][-1] == 'locked'
    assert not any(k == 'measurement_rejected' for k in kinds)        # the owner outage is not a measurement outage
    proxy.close()
    assert new.lease is None and not new.state_path.exists()


def test_worker_fails_only_when_the_owner_answers_without_the_lease_or_with_a_fault(rig, tmp_path):
    owner, holder, clock, lease = rig
    hw = holder['hw']
    proxy, session, events, emit = worker(rig)
    def lease_cleared(seconds):
        clock.advance(seconds)
        if clock() >= 103. and session.owner is owner:
            shutdown(owner)
            owner.state_path.unlink()                    # offline review cleared the lease: the next owner starts clean
            session.owner = restart(owner, tmp_path, clock, holder)
    with pytest.raises(BrokerError) as exc:
        run_steady_lock(proxy, steady_parameters(hw, duration_s=10.), clock=clock, sleep=lease_cleared, cancel=lambda: False, emit=emit)
    assert exc.value.code == 'ownership' and [k for k, _ in events].count('owner_wait') == 0
    assert hw.firmware.cfg['pid_multiplier'] == 1              # nothing written by this worker; firmware PI runs on


def test_worker_fails_when_the_restarted_owner_refused_adoption(rig, tmp_path):
    owner, holder, clock, lease = rig
    hw = holder['hw']
    proxy, session, events, emit = worker(rig)
    def refused(seconds):
        clock.advance(seconds)
        if clock() >= 103. and session.owner is owner:
            shutdown(owner)
            hw.firmware.cfg['pid_multiplier'] = 0           # a human stopped the PI while no owner ran
            session.owner = restart(owner, tmp_path, clock, holder)
            assert session.owner.fault
    with pytest.raises(BrokerError, match='adoption refused: Native PI ownership lost') as exc:
        run_steady_lock(proxy, steady_parameters(hw, duration_s=10.), clock=clock, sleep=refused, cancel=lambda: False, emit=emit)
    assert exc.value.code == 'fault' and session.uncertain_output      # lease retained for review
    hold_failure = next(row for k, row in events if k == 'ecdl_lock_hold_failed')
    assert hold_failure['code'] == 'fault' and hold_failure['unreachable'] is False
    assert hw.firmware.cfg['pid_multiplier'] == 0 and len([s for s in hw.firmware.sets if 'pid_multiplier' in s]) == 1


def test_worker_gives_up_after_the_bounded_owner_outage(rig, tmp_path):
    owner, holder, clock, lease = rig
    hw = holder['hw']
    proxy, session, events, emit = worker(rig)
    def outage(seconds):
        clock.advance(seconds)
        if clock() >= 103. and session.owner is owner:
            shutdown(owner)
            session.owner = None                           # never comes back
    p = steady_parameters(hw, duration_s=60., owner_outage_timeout_s=10.)
    with pytest.raises(DeviceError, match='owner unreachable for 1\\d s .*no write attempted'):
        run_steady_lock(proxy, p, clock=clock, sleep=outage, cancel=lambda: False, emit=emit)
    assert hw.firmware.cfg['pid_multiplier'] == 1               # the worker wrote nothing; the firmware PI is still running
    kinds = [k for k, _ in events]
    assert kinds.count('owner_wait') == 1 and 'ecdl_lock_held' not in kinds
    assert next(row for k, row in events if k == 'ecdl_lock_hold_failed')['unreachable'] is True
    assert session.uncertain_output                               # the hold at exit did not reach the owner: review
    with pytest.raises(ValueError, match='at most one hour'):
        parameters(dict(parameters={}), dict(owner_outage_timeout_s=7200.))
    assert parameters(dict(parameters={}), {})['owner_outage_timeout_s'] == 60.


def test_client_maps_transport_loss_to_owner_unavailable(rig):
    owner, holder, clock, lease = rig
    lock(owner, holder['hw'])
    proxy, session, events, emit = worker(rig)
    session.owner = None
    with pytest.raises(OwnerUnavailable):
        proxy.heartbeat()
    assert not session.uncertain_output
    with pytest.raises(OwnerUnavailable):                        # refused: the read never reached the owner
        proxy.read_cycle()
    assert not session.uncertain_output
    session.failure = socket.timeout('timed out')
    with pytest.raises(OwnerUnavailable):                        # timeout: steady_read may have ramped the target
        proxy.read_cycle()
    assert session.uncertain_output
    session.failure = None
    session.owner = owner
    assert proxy.heartbeat() is True                              # the lease-bound liveness call answers again
    assert isinstance(OwnerUnavailable('x'), MeasurementUnavailable)


def test_default_factory_accepts_adopt_leases(monkeypatch, tmp_path):
    """Regression 2026-10-27 20:27: the owner's default RigHardware factory lambda refused the adopt_leases keyword,
    so a restart adoption failed with TypeError on hardware. The default factory must forward adopt_leases."""
    import inspect as _inspect
    from ldcon.owners import ecdl_owner
    captured = {}
    class FakeRig:
        def __init__(self, device, single_session=(), instrument_owners=None, registry=None, time_telemetry=None, adopt_leases=None):
            captured['adopt_leases'] = adopt_leases
    monkeypatch.setattr(ecdl_owner, 'RigHardware', FakeRig)
    source = _inspect.getsource(ecdl_owner.ECDLOwner.__init__)
    assert 'adopt_leases=None' in source.split('factory=lambda')[1].split(':')[0]
