"""Owner restart with an active native PI: no write, no fault, the next owner adopts the lease (2026-10-27).

Design: card-system-design.md rules A/B and section 5 ("owner 再起動とロックの再採用"); incident 2026-10-27 00:00
(preparations/refactor-20261026/NIGHTLOG.md, sr1092-owner-review-20261027). Fakes only: no hardware, no network.
"""
import copy
import importlib.util
import json
from pathlib import Path
import socket

import pytest

from ldcon.core.contracts import DeviceError
from ldcon.owners.esp32_native_client import NativeCurrentPort, OwnerUnavailable
from ldcon.owners.esp32_native_owner import NativeCurrentOwner
from ldcon.core.instrument_client import BrokerError
from ldcon.jobs.native_current_card import parameters, run_native_current

_spec = importlib.util.spec_from_file_location('native_readopt_fixture',
    (Path(__file__).resolve().parent / 'support') / 'jobs/tests/test_esp32_native_current.py')
fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fixture)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError('No hardware/network access in offline test')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)


class Gate(fixture.Gate):
    started = closed = False
    def start(self):
        self.started = True
    def close(self):
        self.closed = True


@pytest.fixture
def rig(tmp_path):
    p = fixture.policy()
    clock, bias, gate = fixture.Clock(), fixture.Bias(), Gate()
    servo = fixture.Servo(p)
    optical = fixture.Optical(clock)
    owner = NativeCurrentOwner(servo, .2, 1., None, p, tmp_path / 'state.json',
                               clock=clock, optical=optical, bias=bias, gate=gate)
    return owner, servo, clock, bias, optical, gate


def call(owner, action, **values):
    return owner.dispatch(dict(device_id=owner.driver.device_id, action=action, lease=owner.lease, **values))


def lock(owner, clock):
    call(owner, 'claim', policy_digest=owner.definition)
    call(owner, 'prepare', target_wavelength_nm=1091.7855453438308)
    for _ in range(4):
        clock.advance()
        owner.poll()
    call(owner, 'arm')
    assert owner.active and owner.fault is None


def shutdown(owner):
    """The real lifecycle: SIGTERM sets stop, run() returns through its finally."""
    owner.stop.set()
    owner.run()


def restart(owner, servo, clock, optical, bias, gate):
    return NativeCurrentOwner(servo, .2, 1., None, fixture.policy(), owner.state_file,
                              clock=clock, optical=optical, bias=bias, gate=gate)


class LostBias(fixture.Bias):
    """The CLD owner answers, but no longer holds the bias lease the state file names."""
    def status(self):
        return dict(super().status(), bias_held=False)


def test_deliberate_shutdown_with_active_pi_writes_nothing_and_latches_nothing(rig):
    owner, servo, clock, bias, optical, gate = rig
    lock(owner, clock)
    call(owner, 'heartbeat')
    writes = copy.deepcopy(servo.writes)
    shutdown(owner)
    assert servo.writes == writes and servo.cfg['pid_multiplier'] == 1
    assert owner.fault is None and owner.active and owner.lease and bias.lease
    assert gate.started and gate.closed
    saved = json.loads(owner.state_file.read_text())
    assert saved['active'] is True and saved['lease'] == owner.lease and saved['bias_lease'] == bias.lease
    assert saved['fault'] is None and saved['pending'] is None
    assert saved['expected_config'] == owner.expected_config and saved['expected_config']['pid_multiplier'] == 1
    assert saved['activation']['dac_code'] == 32768 and saved['activation']['current_estimate_a'] == owner.activation_a
    assert saved['activation']['record']['armed_unix_s'] is not None
    assert saved['prepared']['target_nm'] == pytest.approx(1091.7855453438308)
    assert saved['shutdown']['lease'] == owner.lease and saved['shutdown']['heartbeat_age_s'] == pytest.approx(0.)
    assert saved['heartbeat_unix_s'] is not None


def test_shutdown_without_active_pi_keeps_the_previous_quarantine_rule(rig):
    owner, servo, clock, bias, optical, gate = rig
    call(owner, 'claim', policy_digest=owner.definition)
    call(owner, 'prepare', target_wavelength_nm=1091.7855453438308)
    shutdown(owner)
    assert servo.cfg['pid_multiplier'] == 0 and owner.fault is None
    restarted = restart(owner, servo, clock, optical, bias, gate)
    # A lease without an active PI (open loop at the held DAC) is still reviewed offline, as before.
    assert restarted.fault and 'retained a lease' in restarted.fault and not restarted.active
    assert not any('pid_multiplier' in w for w in servo.writes[-1:]) or servo.cfg['pid_multiplier'] == 0


def test_restart_adopts_the_running_pi_when_firmware_and_bias_match(rig):
    owner, servo, clock, bias, optical, gate = rig
    lock(owner, clock)
    clock.advance(1.)
    call(owner, 'heartbeat')
    shutdown(owner)
    writes = copy.deepcopy(servo.writes)
    clock.advance(3.)                      # restart gap: heartbeat_timeout_s is 5 s in the fixture policy
    restarted = restart(owner, servo, clock, optical, bias, gate)
    assert restarted.fault is None and restarted.active and restarted.lease == owner.lease
    assert restarted.bias.lease == bias.lease
    assert restarted.adoption['state'] == 'adopted' and restarted.adoption['bias_verified'] is True
    assert restarted.adoption['pid_multiplier'] == 1 and restarted.adoption['heartbeat_grace_s'] == 5.
    assert restarted.expected_config == owner.expected_config
    assert restarted.activation_a == owner.activation_a and restarted.activation_code == owner.activation_code
    assert restarted.prepared == owner.prepared
    assert servo.writes == writes and servo.cfg['pid_multiplier'] == 1
    saved = json.loads(restarted.state_file.read_text())
    assert saved['active'] is True and saved['adoption']['state'] == 'adopted' and saved['fault'] is None
    # Supervision, telemetry and the lease-bound RPCs resume with the same lease; no write happens.
    for _ in range(4):
        clock.advance()
        call(restarted, 'heartbeat')
        restarted.poll()
    state = restarted.snapshot()
    assert state['fresh'] and state['native_pi_active'] and state['input_valid'] and state['fault'] is None
    assert state['adoption']['state'] == 'adopted' and state['last_shutdown']['lease'] == owner.lease
    assert any(e['kind'] == 'owner_restart_adopted' for e in state['input_events'])
    assert servo.writes == writes
    # The adopted lock is stoppable and releasable by the worker as before (the one typed PI-off write).
    stop = call(restarted, 'stop')
    assert stop['hold_kind'] == 'fixed_reference' and servo.cfg['pid_multiplier'] == 0
    call(restarted, 'release')
    assert restarted.lease is None and bias.lease is None
    assert json.loads(restarted.state_file.read_text())['active'] is False


def test_adoption_grace_then_the_existing_heartbeat_deadline(rig):
    owner, servo, clock, bias, optical, gate = rig
    lock(owner, clock)
    shutdown(owner)
    clock.advance(30.)                     # the owner was down far longer than heartbeat_timeout_s
    restarted = restart(owner, servo, clock, optical, bias, gate)
    assert restarted.adoption['state'] == 'adopted' and restarted.fault is None
    writes = len(servo.writes)
    for _ in range(4):                     # within the grace: no heartbeat yet, nothing happens
        clock.advance(1.)
        restarted.poll()
    assert restarted.active and restarted.fault is None and len(servo.writes) == writes
    clock.advance(1.5)                     # grace (heartbeat_timeout_s = 5 s from adoption) expired, worker gone
    restarted.poll()
    assert restarted.fault and 'heartbeat expired' in restarted.fault
    assert servo.cfg['pid_multiplier'] == 0 and not restarted.active       # the existing fixed-reference disable


@pytest.mark.parametrize('damage', ['pi_off', 'config', 'firmware', 'bias_lost', 'ff_in_flight'])
def test_restart_latches_precisely_when_the_firmware_or_bias_differs(rig, damage):
    owner, servo, clock, bias, optical, gate = rig
    lock(owner, clock)
    shutdown(owner)
    if damage == 'pi_off':
        servo.cfg['pid_multiplier'] = 0            # e.g. a human stopped it at the device
        expected = 'Native PI ownership lost'
    elif damage == 'config':
        servo.cfg['pid_kp'] *= 2                   # foreign gain change while no owner watched
        expected = 'controlled configuration changed'
    elif damage == 'firmware':
        servo.status['firmware_version'] = 'other'
        expected = 'Firmware identity'
    elif damage == 'bias_lost':
        bias = LostBias()                          # the CLD owner no longer holds the bias lease
        expected = 'CLD bias owner fault or ownership lost'
    else:
        saved = json.loads(owner.state_file.read_text())
        saved['feedforward'] = dict(token='t', state='active', expected_revision=None)
        owner.state_file.write_text(json.dumps(saved))
        expected = 'feedforward session was in flight'
    writes = copy.deepcopy(servo.writes)
    restarted = restart(owner, servo, clock, optical, bias, gate)
    assert restarted.fault and 'adoption refused' in restarted.fault and expected in restarted.fault
    assert restarted.adoption['state'] == 'refused' and restarted.lease == owner.lease
    assert servo.writes == writes                  # quarantine: nothing written, nothing replayed
    restarted.poll()
    assert servo.writes == writes
    with pytest.raises(BrokerError) as exc:
        call(restarted, 'heartbeat')
    assert exc.value.code == 'fault'
    assert json.loads(restarted.state_file.read_text())['fault'] == restarted.fault


def test_unreachable_firmware_keeps_adoption_pending_then_latches(rig):
    owner, servo, clock, bias, optical, gate = rig
    lock(owner, clock)
    shutdown(owner)
    servo.read_error = True
    restarted = restart(owner, servo, clock, optical, bias, gate)
    assert restarted.adoption['state'] == 'pending' and restarted.fault is None and restarted.active
    clock.advance(1.)
    restarted.poll()
    assert restarted.adoption['state'] == 'pending' and restarted.snapshot()['state'] == 'unavailable'
    servo.read_error = False
    clock.advance(1.)
    restarted.poll()
    assert restarted.adoption['state'] == 'adopted' and restarted.fault is None
    assert restarted.adoption['attempts'] == 3 and servo.cfg['pid_multiplier'] == 1
    # And the bounded version: never reachable within missing_timeout_s -> precise latch, no write.
    shutdown(restarted)
    servo.read_error = True
    again = restart(owner, servo, clock, optical, bias, gate)
    writes = len(servo.writes)
    clock.advance(fixture.policy()['missing_timeout_s'] + 1.)
    again.poll()
    assert again.fault and 'unreadable' in again.fault and len(servo.writes) == writes


def test_persisted_target_ramp_resumes_after_adoption(rig):
    owner, servo, clock, bias, optical, gate = rig
    lock(owner, clock)
    clock.advance(1.)
    final = fixture.C_NM / 1091.7855453438308 + 3e8
    call(owner, 'retarget', target_frequency_hz=final)
    assert owner.retarget['active'] and owner.retarget['steps'] == 0
    shutdown(owner)
    restarted = restart(owner, servo, clock, optical, bias, gate)
    assert restarted.adoption['retarget_resumed'] and restarted.retarget['final_hz'] == final
    for _ in range(12):
        clock.advance(.3)
        call(restarted, 'heartbeat')
        restarted.poll()
    r = restarted.retarget
    assert not r['active'] and r['error'] is None and r['steps'] >= 1 and r['current_hz'] == pytest.approx(final)
    assert restarted.fault is None and restarted.active


class SwitchablePort(NativeCurrentPort):
    """The worker's typed client routed to whichever owner process is 'running'; None models connection refused."""
    def __init__(self, device, owner):
        super().__init__(device)
        self.owner = owner
        self.calls = []
    def _call(self, action, **values):
        self.calls.append(action)
        if self.owner is None:
            raise ConnectionRefusedError(111, 'Connection refused')
        return self.owner.dispatch(dict(action=action, device_id=self.device_id, **values))


def worker(rig):
    owner, servo, clock, bias, optical, gate = rig
    device = dict(control_settings=dict(native_current=owner.policy),
                  connection=dict(endpoint='http://127.0.0.1:1', device_id=servo.device_id))
    port = SwitchablePort(device, owner)
    port.connect()
    events = []
    return port, events, lambda kind, row: events.append((kind, row))


def test_worker_retries_through_an_owner_restart_and_continues_with_the_same_lease(rig):
    owner, servo, clock, bias, optical, gate = rig
    port, events, emit = worker(rig)
    lease = port.lease
    timeline = dict(down_at=102., up_at=105., stopped=False, restarted=None)
    def sleep(seconds):
        clock.advance(seconds)
        if not timeline['stopped'] and clock() >= timeline['down_at']:
            timeline['stopped'] = True
            shutdown(owner)                     # systemctl restart: old owner gone, PI left running
            port.owner = None
        elif timeline['restarted'] is None and clock() >= timeline['up_at']:
            timeline['restarted'] = restart(owner, servo, clock, optical, bias, gate)
            port.owner = timeline['restarted']
        if port.owner is not None:
            port.owner.poll()
    params = parameters(dict(parameters={}), dict(target_wavelength_nm=1091.7855453438308, duration_s=9.))
    result = run_native_current(port, params, clock=clock, sleep=sleep, cancel=lambda: False, emit=emit)
    assert result['status'] == 'succeeded' and result['locked_cycles'] > 0
    new = timeline['restarted']
    assert new is not None and new.adoption['state'] == 'adopted' and new.fault is None and new.active
    assert port.lease == lease == new.lease and port.active and not port.uncertain_output
    assert servo.cfg['pid_multiplier'] == 1
    assert sum(1 for w in servo.writes if 'pid_multiplier' in w) == 1        # the one arm write; no re-arm, no disable
    kinds = [k for k, _ in events]
    assert 'owner_wait' in kinds and 'owner_recovered' in kinds
    wait = next(row for k, row in events if k == 'owner_wait')
    assert wait['native_pi_active'] and wait['timeout_s'] == 60.
    assert [row['state'] for k, row in events if k == 'state'][-1] == 'locked'
    assert not any(k in ('card_failed',) for k in kinds)
    port.close()
    assert new.lease is None and bias.lease is None and servo.cfg['pid_multiplier'] == 0


def test_worker_fails_only_when_the_owner_answers_without_the_lease_or_with_a_fault(rig):
    owner, servo, clock, bias, optical, gate = rig
    port, events, emit = worker(rig)
    def lease_cleared(seconds):
        clock.advance(seconds)
        if clock() >= 102. and port.owner is owner:
            shutdown(owner)
            saved = json.loads(owner.state_file.read_text())
            # offline review cleared the leases (the 2026-10-27 recovery): the next owner starts clean
            saved.update(lease=None, bias_lease=None, active=False, fault=None)
            owner.state_file.write_text(json.dumps(saved))
            port.owner = restart(owner, servo, clock, optical, fixture.Bias(), gate)
        port.owner.poll()
    params = parameters(dict(parameters={}), dict(target_wavelength_nm=1091.7855453438308, duration_s=9.))
    with pytest.raises(DeviceError, match='lease is no longer held'):
        run_native_current(port, params, clock=clock, sleep=lease_cleared, cancel=lambda: False, emit=emit)
    assert not port.uncertain_output                 # nothing was written by this worker
    assert [k for k, _ in events].count('owner_wait') == 0


def test_worker_fails_when_the_restarted_owner_refused_adoption(rig):
    owner, servo, clock, bias, optical, gate = rig
    port, events, emit = worker(rig)
    def refused(seconds):
        clock.advance(seconds)
        if clock() >= 102. and port.owner is owner:
            shutdown(owner)
            servo.cfg['pid_multiplier'] = 0          # a human stopped the PI while no owner ran
            port.owner = restart(owner, servo, clock, optical, bias, gate)
            assert port.owner.fault
        port.owner.poll()
    params = parameters(dict(parameters={}), dict(target_wavelength_nm=1091.7855453438308, duration_s=9.))
    with pytest.raises(DeviceError, match='Native owner fault: .*adoption refused'):
        run_native_current(port, params, clock=clock, sleep=refused, cancel=lambda: False, emit=emit)
    assert port.uncertain_output                     # leases retained for review


def test_worker_gives_up_after_the_bounded_owner_outage(rig):
    owner, servo, clock, bias, optical, gate = rig
    port, events, emit = worker(rig)
    def outage(seconds):
        clock.advance(seconds)
        if clock() >= 102. and port.owner is owner:
            shutdown(owner)
            port.owner = None                        # never comes back
        if port.owner is not None:
            port.owner.poll()
    params = parameters(dict(parameters={}), dict(target_wavelength_nm=1091.7855453438308, duration_s=60.,
                                                  owner_outage_timeout_s=10.))
    with pytest.raises(DeviceError, match='owner unreachable for 1\\d s'):
        run_native_current(port, params, clock=clock, sleep=outage, cancel=lambda: False, emit=emit)
    assert servo.cfg['pid_multiplier'] == 1          # the worker wrote nothing; the firmware PI is still running
    with pytest.raises(ValueError, match='at most one hour'):
        parameters(dict(parameters={}), dict(owner_outage_timeout_s=7200.))


def test_client_maps_transport_loss_to_owner_unavailable_without_marking_output_uncertain(rig):
    owner, servo, clock, bias, optical, gate = rig
    port, events, emit = worker(rig)
    port.owner = None
    for read in (port.heartbeat, port.read, port.pid_state):
        with pytest.raises(OwnerUnavailable):
            read()
    assert not port.uncertain_output
    port.owner = owner
    assert port.heartbeat() is True
