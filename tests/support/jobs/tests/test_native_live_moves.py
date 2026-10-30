"""Rule F generic cards on the sr1092 native owner (2026-10-22): bounded live re-centring of the PI centre
(recenter) and the bounded live target ramp (retarget) while the PI is active; both refused otherwise."""
import pytest

from ldcon.esp32_native_owner import C_NM, digest
from ldcon.instrument_client import BrokerError

from test_esp32_native_current import rig, policy, Clock, Bias, Gate, Optical, Servo  # noqa: F401  (fixtures)


def armed(rig):
    owner, servo, clock, bias, optical, gate, call, ready = rig
    ready()
    call('arm')
    assert owner.active
    return owner, servo, clock, optical, call


def test_recenter_steps_the_pi_centre_by_one_bounded_code_while_active(rig):
    owner, servo, clock, optical, call = armed(rig)
    old = int(servo.cfg['control_offset'])
    with pytest.raises(BrokerError, match='Update interval'):
        call('recenter', code=old + 1, expected_code=old)
    clock.advance(.3); owner.poll()
    r = call('recenter', code=old + 1, expected_code=old)
    assert r['code'] == old + 1 and servo.cfg['control_offset'] == old + 1 and servo.writes[-1] == {'control_offset': old + 1}
    assert owner.expected_config['control_offset'] == old + 1       # no 'external modification' trip
    clock.advance(.3); owner.poll()
    assert owner.fault is None and owner.active
    with pytest.raises(BrokerError, match='max_step_codes'):
        call('recenter', code=old + 5, expected_code=old + 1)       # default 1 code per call
    with pytest.raises(BrokerError, match='changed since caller observation'):
        call('recenter', code=old + 2, expected_code=old)
    with pytest.raises(BrokerError):
        call('recenter', code=old + 3000, expected_code=old + 1, max_step_codes=64)   # leaves the activation window / budget
    assert servo.cfg['control_offset'] == old + 1


def test_recenter_and_retarget_need_an_active_pi(rig):
    owner, servo, clock, bias, optical, gate, call, ready = rig
    ready()
    old = int(servo.cfg['control_offset'])
    with pytest.raises(BrokerError, match='active PI'):
        call('recenter', code=old + 1, expected_code=old)
    with pytest.raises(BrokerError, match='active PI'):
        call('retarget', target_frequency_hz=C_NM / 1091.7855453438308 + 5e6)


def test_retarget_ramps_target_window_and_centre_within_the_envelope(rig):
    owner, servo, clock, optical, call = armed(rig)
    f0 = C_NM / owner.prepared['target_nm']
    offset0 = int(servo.cfg['control_offset'])
    r = call('retarget', target_frequency_hz=f0 + 40e6, step_hz=10e6)             # 4 steps of capture/2 = 10 MHz
    assert r['active'] and r['step_hz'] == 10e6 and r['current_hz'] == f0
    with pytest.raises(BrokerError, match='ramp is in progress'):
        call('retarget', target_frequency_hz=f0)
    with pytest.raises(BrokerError, match='ramp is in progress'):
        call('recenter', code=offset0 + 1, expected_code=offset0)
    seen = []
    for _ in range(12):
        clock.advance(.3)
        optical.frequency = C_NM / servo.cfg['target']               # the PI holds the laser at the firmware target
        servo.status['system_value'] = servo.cfg['target']           # ... and the firmware sees the same wavemeter sample
        owner.poll()
        state = owner.snapshot()
        seen.append((state['retarget']['current_hz'], servo.cfg['target'], servo.cfg['control_offset']))
        if not state['retarget']['active']:
            break
    final = owner.snapshot()['retarget']
    assert not final['active'] and final['error'] is None and final['steps'] == 4
    assert abs(C_NM / servo.cfg['target'] - (f0 + 40e6)) < 1.
    width = owner.policy['native'].get('acceptance_hz', owner.policy['native']['capture_hz'])
    assert abs(C_NM / servo.cfg['system_max'] - (f0 + 40e6 - width)) < 1e3          # acceptance window followed the ramp
    assert owner.prepared['target_nm'] == servo.cfg['target']
    # feed-forward of the PI centre: frequency up -> wavelength down -> codes down (calibration sign), bounded
    assert servo.cfg['control_offset'] < offset0 and offset0 - servo.cfg['control_offset'] <= 4 * owner._offset_budget_codes()
    assert owner.fault is None and owner.active and owner.expected_config['target'] == servo.cfg['target']
    steps = [w for w in servo.writes if 'target' in w and 'control_offset' in w]
    assert len(steps) == 4 and all(set(w) == {'target', 'system_min', 'system_max', 'control_offset'} for w in steps)


def test_retarget_refuses_a_change_outside_the_envelope_and_ff_waits_for_the_ramp(rig):
    owner, servo, clock, optical, call = armed(rig)
    f0 = C_NM / owner.prepared['target_nm']
    with pytest.raises(BrokerError):
        call('retarget', target_frequency_hz=f0 + 5e9)                 # the centre shift would leave the activation window
    assert owner.retarget is None
    call('retarget', target_frequency_hz=f0 + 10e6)
    with pytest.raises(BrokerError, match='ramp is in progress|FF requires|profile'):
        call('ff_validate', plan={}, profile_digest='x' * 64)
