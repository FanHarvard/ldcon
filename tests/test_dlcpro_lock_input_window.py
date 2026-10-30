"""A large optical jump must not rail PI; command/recovery ranges stay separate."""
from copy import deepcopy

import pytest

from ldcon.core.contracts import MeasurementUnavailable
from ldcon.owners.dlcpro_owner import policy_check
from .test_dlcpro_offline import rig
from .test_dlcpro_frequency_window import prepared
from .test_dlcpro_udp import feed


def locked(rig):
    rig['owner'].policy['lock_input_max_error_hz'] = 20e9
    return prepared(rig)


def test_34_ghz_jump_holds_pzt_integral_and_tec_until_fresh_return(rig):
    o, r = locked(rig)
    o.integral = .01
    before = dict(rig['driver'].state)
    rig['clock'].sleep(.1)
    feed(r, rig, 2, value=299.966)
    o.heartbeat(); o.tick()
    assert not rig['driver'].writes and rig['driver'].state == before
    assert o.integral == .01 and o.mode == 'lock' and not o.fault
    state = o.snapshot()['control_frequency_window']
    assert state['held'] and state['lock_max_error_hz'] == 20e9
    assert state['bounds_hz'] == [3e14-20e9, 3e14+20e9]
    assert state['target_bounds_hz'] == [297e12, 303e12]
    rig['clock'].sleep(.5)
    feed(r, rig, 3, value=300.000001)
    o.heartbeat(); o.tick()
    assert o.integral == .01  # no integration over rejected data or the gap
    assert not o.frequency_window.held and not o.fault
    assert any('pc:voltage-set' in key for key, _ in rig['driver'].writes)


def test_coalesced_34_ghz_jump_does_not_enter_integral(rig):
    o, r = locked(rig)
    o.integral = .01
    rig['clock'].sleep(.05); feed(r, rig, 2, value=299.966)
    rig['clock'].sleep(.06); feed(r, rig, 3, value=300.000001)
    o.tick()
    expected = 70.+.01-o.control['kp']*1e6/o.policy['axes']['piezo']['frequency_hz_per_unit']
    assert o.integral == .01 and o.applied_pzt == pytest.approx(expected)


def test_jump_during_console_wait_is_rejected_before_write(rig):
    from contextlib import contextmanager
    o, r = locked(rig)
    rig['clock'].sleep(.1); feed(r, rig, 2)

    @contextmanager
    def transaction():
        rig['clock'].sleep(.01); feed(r, rig, 3, value=299.966)
        yield

    rig['driver'].control_transaction = transaction
    o.tick()
    assert not rig['driver'].writes and o.integral == 0 and not o.fault


def test_fresh_but_rejected_input_does_not_extend_missing_deadline(rig):
    o, r = locked(rig)
    for seq in range(2, 27):
        rig['clock'].sleep(.1); feed(r, rig, seq, value=299.966)
        o.heartbeat(); o.tick()
    assert o.mode == 'idle' and o.fault == 'control frequency deadline exceeded'
    assert not rig['driver'].writes


def test_window_follows_live_target_and_does_not_restrict_idle_recovery(rig):
    o, r = locked(rig)
    for seq, value in [(2, 299.98), (3, 300.02)]:
        rig['clock'].sleep(.01); feed(r, rig, seq, value=value)
        assert o.read_control().frequency_hz == value*1e12
    rig['clock'].sleep(.01); feed(r, rig, 4, value=300.020001)
    with pytest.raises(MeasurementUnavailable, match='lock input window'):
        o.read_control()
    o.target += 2e6
    assert o.read_control().frequency_hz == 300.020001e12
    o.stop()
    rig['clock'].sleep(.01); feed(r, rig, 5, value=299.966)
    assert o.read_control().frequency_hz == 299.966e12
    assert o.snapshot()['control_frequency_window']['lock_max_error_hz'] is None


@pytest.mark.parametrize('value', [0, -1, False, float('nan'), float('inf')])
def test_invalid_guard_policy_is_rejected(rig, value):
    p = deepcopy(rig['owner'].policy)
    p['lock_input_max_error_hz'] = value
    with pytest.raises(ValueError):
        policy_check(p, rig['owner'].instrument.hardware)


def test_policy_projection_updates_guard_at_a_fresh_boundary(rig):
    from ldcon.core.interface_schema import digest
    o, r = prepared(rig)
    o.approve = lambda old, new, evidence: True
    policy = deepcopy(o.policy); policy['lock_input_max_error_hz'] = 20e9
    assert o.reload(digest(o.policy), policy, 'test exact approval')['status'] == 'pending'
    rig['clock'].sleep(.1); feed(r, rig, 2)
    o.tick()
    assert o.pending_policy is None
    rig['driver'].writes.clear()
    rig['clock'].sleep(.1); feed(r, rig, 3, value=299.966)
    o.tick()
    assert not rig['driver'].writes and o.frequency_window.held
