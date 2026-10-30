"""Queued real heartbeats, never optimizer-generated watchdog renewal."""
from contextlib import contextmanager
from copy import deepcopy
import threading

import pytest

from ldcon.core.contracts import DeviceError
from ldcon.core.contracts import FrequencySample
from ldcon.core.interface_schema import digest
from ldcon.jobs.dlcpro_cards import Protocol
from .test_dlcpro_offline import rig


def setup(rig):
    owner = rig['owner']; owner.start_lock(3e14, rig['control'])
    protocol = Protocol(owner, rig['device']); protocol.client = 'controller'
    def call(action, **kwargs):
        return protocol.dispatch(dict(action=action, laser_id=owner.laser_id, **kwargs))
    started = call('pi_trial_begin', device_revision=digest(rig['device']), identity='task-one',
        target_revision=owner.target_revision, policy_revision=digest(owner.policy), watchdog_s=30., family='current')
    return owner, protocol, call, started['token']


@pytest.mark.parametrize('lease', ['controller', 'wrong'])
def test_verified_transaction_can_credit_only_real_matching_pending_heartbeat(rig, monkeypatch, lease):
    owner, protocol, call, token = setup(rig)
    arrived = threading.Event(); completed = threading.Event(); errors = []
    original_incoming = protocol.client_liveness.incoming

    @contextmanager
    def incoming(request):
        with original_incoming(request):
            if request['action']=='heartbeat':arrived.set()
            yield
    monkeypatch.setattr(protocol.client_liveness, 'incoming', incoming)
    original_move = owner.instrument.move
    threads = []
    def heartbeat():
        try:call('heartbeat', lease=lease)
        except Exception as error:errors.append(error)
        finally:completed.set()
    def slow_move(*args, **kwargs):
        worker = threading.Thread(target=heartbeat); threads.append(worker); worker.start()
        assert arrived.wait(1.)
        assert not completed.is_set()
        rig['clock'].sleep(3.)
        return original_move(*args, **kwargs)
    monkeypatch.setattr(owner.instrument, 'move', slow_move)
    with owner.mutex:
        call('pi_trial_apply', token=token, expected_revision=owner.pi_trial.revision, candidate=dict(current_ma=100.1))
        assert owner.last_client == (owner.clock() if lease=='controller' else 100.)
        assert not protocol.client_liveness.credit_verified_boundary()  # No duplicate credit.
    for thread in threads:thread.join(1.)
    assert completed.is_set() and not protocol.client_liveness.pending
    assert bool(errors) == (lease=='wrong')


def test_optimizer_without_a_real_heartbeat_does_not_extend_client_watchdog(rig):
    owner, protocol, call, token = setup(rig)
    rig['clock'].sleep(3.)
    call('pi_trial_apply', token=token, expected_revision=owner.pi_trial.revision, candidate=dict(current_ma=100.1))
    assert owner.last_client == 100.
    owner.tick()
    assert owner.mode=='idle' and owner.pi_trial.active is None
    assert any(row['event']=='client_timeout_hold' for row in rig['events'])


@pytest.mark.parametrize('case', ['cancelled', 'new_lease', 'expired', 'unconfirmed'])
def test_pending_request_cannot_mask_cancel_revoke_expiry_or_unknown_effect(rig, case):
    owner, protocol, call, token = setup(rig)
    request = dict(action='heartbeat', laser_id=owner.laser_id, lease='controller')
    with protocol.client_liveness.incoming(request):
        if case=='new_lease':protocol.client='new-controller'
        if case=='expired':rig['clock'].sleep(30.)
        if case=='unconfirmed':
            with pytest.raises(DeviceError):owner.pi_trial._parameter_receipt(dict(confirmed=False))
        elif case!='cancelled':assert not protocol.client_liveness.credit_verified_boundary()
        assert owner.last_client==100.
    assert not protocol.client_liveness.pending
    assert not protocol.client_liveness.credit_verified_boundary()


def test_other_owner_calls_never_count_as_controller_heartbeat(rig):
    owner, protocol, call, token = setup(rig)
    for action in ('pi_trial_heartbeat', 'read', 'pi_trial_apply'):
        with protocol.client_liveness.incoming(dict(action=action, laser_id=owner.laser_id, lease='controller')):
            assert not protocol.client_liveness.credit_verified_boundary()
    with protocol.client_liveness.incoming(dict(action='heartbeat', laser_id='another', lease='controller')):
        assert not protocol.client_liveness.credit_verified_boundary()


def test_held_transaction_does_not_replay_contiguous_udp_history_into_integral(rig, monkeypatch):
    owner, protocol, call, token = setup(rig)
    owner.control.update(kp=0.,ki_per_s=1.)
    rig['source'].bias = 1e6
    def samples_after(after):
        return [FrequencySample(i,3e14+1e6,100+i*.01,100+i*.01,'valid','synthetic')
            for i in range(1,400) if after < 100+i*.01 <= owner.clock()]
    monkeypatch.setattr(rig['source'], 'samples_after', samples_after, raising=False)
    move = owner.instrument.move
    def slow_move(*args, **kwargs):
        rig['clock'].sleep(3.)
        return move(*args, **kwargs)
    monkeypatch.setattr(owner.instrument, 'move', slow_move)
    applied = deepcopy(owner.applied_observation)
    changed = call('pi_trial_apply',token=token,expected_revision=owner.pi_trial.revision,candidate=dict(current_ma=100.1))
    assert changed['integral_resume_after_s']==103.
    assert owner.applied_observation==applied  # No invented PZT feedback proof.
    owner.heartbeat()
    count = len(rig['driver'].writes)
    owner.tick()  # Latest UDP sample is still at the hold boundary.
    assert len(rig['driver'].writes)==count and owner.integral==0.
    assert owner.applied_observation==applied
    rig['clock'].sleep(.1);owner.heartbeat();owner.tick()
    assert owner.integral==pytest.approx(-.001,abs=1e-12)
    assert owner.integral_resume_after_s is None
    assert owner.mode=='lock' and not owner.fault
