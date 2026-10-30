"""sr1092 hybrid integral recentre (target-ff design, human decision 5, 2026-10-27) and the reported DAC headroom:
offline native owner over the fixture firmware (no hardware, no network)."""
import importlib.util
from pathlib import Path

import pytest

from ldcon.core.instrument_client import BrokerError
from ldcon.owners import esp32_native_owner as native
from ldcon.owners.esp32_native_owner import C_NM, dac_headroom, codes_window
from ldcon.owners.esp32_native_client import NativeCurrentPort

_spec = importlib.util.spec_from_file_location('native_recentre_fixture',
    (Path(__file__).resolve().parent / 'support') / 'jobs/tests/test_esp32_native_current.py')
fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fixture)


@pytest.fixture
def armed(tmp_path):
    owner, servo, clock, bias, optical, gate, call, ready = fixture.rig.__wrapped__(tmp_path)
    ready()
    call('arm')
    assert owner.active
    return owner, servo, clock, optical, call


def retarget(owner, servo, clock, call, shift_hz):
    target = C_NM / servo.cfg['target'] + shift_hz
    call('retarget', target_frequency_hz=target)
    first = len(servo.writes)
    clock.advance(.3)
    owner.poll()
    assert owner.retarget['active'] is False and owner.retarget['error'] is None
    return first, target


def test_small_integral_after_the_step_needs_no_recentre(armed):
    owner, servo, clock, optical, call = armed
    servo.status.update(pid_output=.2, pid_integral=.0002)          # 0.13 of the raw limit 0.0015
    first, _ = retarget(owner, servo, clock, call, 20e6)
    # judged in the same poll that finished the ramp (no write needed for the judgement)
    r = owner.retarget
    assert r['recentre_check'] == 'done' and r['recentre']['status'] == 'not_needed'
    clock.advance(.3); owner.poll()
    assert r['integral_fraction_after'] == pytest.approx(.0002 / .0015) and r['recentre_threshold'] == .5
    assert len(servo.writes[first:]) == 1 and owner.recentre is None and not owner.fault


def test_large_integral_starts_one_background_bounded_recentre_to_integral_neutral(armed):
    owner, servo, clock, optical, call = armed
    servo.status.update(pid_output=3., pid_integral=-.0012)            # 0.8 of the raw limit
    first, target = retarget(owner, servo, clock, call, 20e6)
    neutral = int(servo.cfg['control_offset']) + 3
    # the ramp's last write is younger than the update interval: the judgement waits for the next poll
    assert owner.retarget['recentre_check'] == 'pending' and owner.recentre is None
    clock.advance(.3); owner.poll()
    r = owner.retarget
    assert r['recentre']['status'] == 'started' and r['recentre']['target_code'] == neutral
    assert r['owner_recentre'] == 'hybrid_background' and r['automatic_recentre'] is False
    assert owner.recentre['reason'].startswith('post-retarget integral recentre')
    assert owner.recentre['max_step_codes'] == owner._offset_budget_codes() and r['recentre']['steps_planned'] >= 1
    # the recentre runs in the background at the owner's cadence (one bounded step per poll; 3 codes fit one step
    # of the fixture budget); the target itself is never rewritten
    for _ in range(6):
        if not owner.recentre['active']:
            break
        clock.advance(.3); owner.poll()
    assert not owner.recentre['active'] and owner.recentre['error'] is None and owner.recentre['steps'] >= 1
    assert servo.cfg['control_offset'] == neutral and abs(C_NM / servo.cfg['target'] - target) < .1
    assert all(set(w) == {'control_offset'} for w in servo.writes[first+1:]) and not owner.fault
    snapshot = owner.snapshot()
    assert snapshot['retarget']['recentre']['status'] == 'started' and snapshot['recentre']['active'] is False
    assert isinstance(snapshot['dac_headroom_hz'], dict) and snapshot['integral_fraction'] == pytest.approx(.8)


def test_recentre_fraction_zero_disables_the_hybrid_and_the_default_is_half(armed, monkeypatch):
    owner, servo, clock, optical, call = armed
    assert native.recentre_integral_fraction(owner.policy['native']) == .5
    monkeypatch.setattr(native, 'recentre_integral_fraction', lambda n: 0.)
    servo.status.update(pid_output=3., pid_integral=-.0012)
    first, _ = retarget(owner, servo, clock, call, 20e6)
    assert owner.retarget['recentre_check'] == 'disabled'
    clock.advance(.3); owner.poll()
    assert len(servo.writes[first:]) == 1 and owner.recentre is None


def test_next_target_waits_only_while_the_post_step_recentre_is_in_flight():
    port = object.__new__(NativeCurrentPort)
    port.policy = dict(update_interval_s=.1)
    port.lease = 'lease'
    replies = [dict(recentre=dict(active=True, reason='post-retarget integral recentre (fraction 0.80 > 0.50)', steps_planned=3, steps=1)),
               dict(recentre=dict(active=True, reason='post-retarget integral recentre (fraction 0.80 > 0.50)', steps_planned=3, steps=2)),
               dict(recentre=dict(active=False, reason='post-retarget integral recentre (fraction 0.80 > 0.50)', steps_planned=3, steps=3))]
    calls, slept, mutated = [], [], []
    port._call = lambda action, **kw: calls.append(action) or replies[min(len(calls) - 1, len(replies) - 1)]
    port.sleep = lambda s: slept.append(s)
    port.mutate = lambda action, **kw: mutated.append((action, kw)) or dict(active=True)
    port.set_target(300e12)
    assert mutated == [('retarget', dict(target_frequency_hz=300e12))]
    assert calls == ['status', 'status', 'status'] and len(slept) == 2 and max(slept) <= .1
    # a recentre of another origin (an operator's operating-point ramp) is not waited for: the owner answers busy itself
    calls.clear(); slept.clear(); mutated.clear()
    replies[:] = [dict(recentre=dict(active=True, reason='requested by tune.operating_point', steps_planned=30, steps=0))]
    port.set_target(300e12)
    assert mutated and not slept
    # a stuck ramp is bounded by the planned steps plus a margin; the owner then refuses with its own reason
    calls.clear(); slept.clear(); mutated.clear()
    replies[:] = [dict(recentre=dict(active=True, reason='post-retarget integral recentre', steps_planned=3, steps=0))]
    ticks = iter(range(0, 10000))
    import time as _time
    original = _time.monotonic
    _time.monotonic = lambda: original() + next(ticks) * .5
    try:
        port.set_target(300e12)
    finally:
        _time.monotonic = original
    assert mutated and slept


def test_dac_headroom_comes_from_the_live_policy_and_firmware_limits(armed):
    owner, servo, clock, optical, call = armed
    sample = servo.read()
    hz_per_code = owner._hz_per_code(servo.cfg['target'])
    room = dac_headroom(owner.policy, sample['servo_config'], hz_per_code, dac_limits=dict(min_code=0, max_code=65535),
                        activation_code=owner.activation_code)
    window = codes_window(owner.policy, dict(min_code=0, max_code=65535), activation_code=owner.activation_code)
    assert room['window_codes'] == window and room['up_codes'] == pytest.approx(window[1] - room['envelope_codes'][1])
    assert room['up_hz'] == pytest.approx(room['up_codes'] * abs(hz_per_code)) and room['down_hz'] >= 0
    # decision 6: nothing is hard-coded -- a wider activation window (the human widened sr1092 to +-10 mA) widens the room
    wide = dict(owner.policy, activation_range_a=[-.01, .01])
    wider = dac_headroom(wide, sample['servo_config'], hz_per_code, dac_limits=dict(min_code=0, max_code=65535), activation_code=owner.activation_code)
    assert wider['up_hz'] > room['up_hz'] and wider['down_hz'] > room['down_hz']
    # firmware dac_limits (the saved range guard) narrow it
    capped = dac_headroom(wide, sample['servo_config'], hz_per_code, dac_limits=dict(min_code=32000, max_code=33000), activation_code=owner.activation_code)
    assert capped['window_codes'][1] == 33000. and capped['window_codes'][0] >= 32000. and capped['up_hz'] < wider['up_hz']
    # the FF scale divides the frequency per code
    assert dac_headroom(wide, sample['servo_config'], hz_per_code, scale=2., dac_limits=None, activation_code=owner.activation_code)['up_hz'] == pytest.approx(wider['up_hz'] / 2)
    assert owner.snapshot()['dac_headroom_hz']['up_codes'] == pytest.approx(room['up_codes'])
