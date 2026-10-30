"""Native target feedforward: physical writes and fallback behavior, offline only."""
import json
import time
import pytest
from ldcon.core.instrument_client import BrokerError
from test_esp32_native_current import rig, policy, Clock, Bias, Gate, Optical, Servo
from test_native_live_moves import armed
from ldcon.owners.esp32_native_owner import C_NM


def test_one_combined_write_preserves_integral_and_ignores_neutral_offset(rig):
    owner, servo, clock, optical, call = armed(rig)
    old = dict(servo.cfg)
    servo.status.update(pid_output=1.49, pid_integral=-.00149)
    target = C_NM / old['target'] + 20e6
    expected = round(old['control_offset'] + 20e6 / owner._hz_per_code(C_NM / target))
    call('retarget', target_frequency_hz=target)
    first = len(servo.writes)
    clock.advance(.3); owner.poll()
    result = owner.snapshot()['retarget']
    assert result['active'] is False and result['error'] is None
    assert result['steps'] == 1 and result['trims'] == 0
    assert result['automatic_recentre'] is False
    assert result['completion_basis'] == 'configuration_readback'
    assert result['first_write_started_unix_s'] <= result['first_write_verified_unix_s'] <= result['finished_unix_s']
    writes = servo.writes[first:]
    assert len(writes) == 1
    assert set(writes[0]) == {'target', 'control_offset', 'system_min', 'system_max'}
    assert servo.cfg['control_offset'] == expected
    assert C_NM / servo.cfg['target'] == pytest.approx(target, abs=.1)
    for k in ('pid_multiplier','pid_kp','pid_ki','pid_integral_min','pid_integral_max'):
        assert servo.cfg[k] == old[k]
    assert servo.status['pid_integral'] == -.00149
    # Hybrid integral recentre (human decision 5, 2026-10-27): the integral fraction here is 0.99 of the raw limit
    # (integral_limit_count 1.5 / |ki| 1000), above recentre_integral_fraction 0.5, so the owner starts ONE background
    # bounded recenter_ramp of the PI centre to integral_neutral AFTER the ramp finished; the target is never
    # rewritten and the completion record above was not delayed by it.
    for _ in range(4):
        clock.advance(.3); owner.poll()
    later = servo.writes[first+1:]
    assert later and all(set(w) == {'control_offset'} for w in later) and not owner.fault
    r = owner.snapshot()['retarget']
    assert r['owner_recentre'] == 'hybrid_background' and r['recentre']['status'] == 'started'
    assert r['integral_fraction_after'] == pytest.approx(.00149 / .0015, rel=1e-6) and r['recentre_threshold'] == .5
    assert r['recentre']['target_code'] == round(expected + 1.49)
    assert isinstance(r['dac_headroom_hz'], dict) and r['dac_headroom_hz']['up_hz'] > 0


def test_large_target_is_segmented_by_existing_step_budget_without_losing_ff(rig, monkeypatch):
    owner, servo, clock, optical, call = armed(rig)
    f0 = C_NM / servo.cfg['target']; offset = int(servo.cfg['control_offset'])
    target = f0 + 60e6
    monkeypatch.setattr(owner, '_offset_budget_codes', lambda: 1)
    call('retarget', target_frequency_hz=target)
    previous = offset
    for _ in range(30):
        clock.advance(.3)
        call('heartbeat')
        optical.frequency = C_NM / servo.cfg['target']
        servo.status['system_value'] = servo.cfg['target']
        owner.poll()
        current = int(servo.cfg['control_offset'])
        assert abs(current-previous) <= 1
        previous = current
        if not owner.retarget['active']: break
    assert not owner.retarget['active'] and not owner.retarget['error']
    assert current == round(offset + 60e6 / owner._hz_per_code(C_NM/target))
    assert owner.retarget['steps'] > 1 and owner.retarget['trims'] == 0


def test_uncertain_combined_write_latches_and_is_not_retried(rig, monkeypatch):
    owner, servo, clock, optical, call = armed(rig)
    f0=C_NM/servo.cfg['target']; old=servo.set_control
    def lost_reply(values):
        old(values)
        if 'target' in values: raise OSError('lost target write reply')
    monkeypatch.setattr(servo, 'set_control', lost_reply)
    call('retarget', target_frequency_hz=f0+20e6)
    n=len(servo.writes)
    clock.advance(.3)
    with pytest.raises(BrokerError, match='Uncertain'):
        owner.poll()
    count=len(servo.writes)
    clock.advance(.3); owner.poll()
    assert owner.fault and owner.lease and len(servo.writes)==count
    assert len([w for w in servo.writes[n:] if 'target' in w]) == 1


def test_target_card_does_not_recentre_direct_ff_even_with_high_integral(tmp_path):
    from ldcon.jobs import lock_target as lt
    from test_lock_target import job_tree, FakeApi, LASER_YAML, laser, TARGET
    lock, card = job_tree(tmp_path, locked=True)
    api=FakeApi(LASER_YAML); events=[]; tick=[0.]
    def sleep(seconds):
        tick[0] += seconds
        (lock/'laser-reload-result-test.json').write_text(json.dumps(dict(request_id='test', status='applied', revision_before='r0', revision_after='r1', updated_unix_s=time.time(), applied=['target.frequency_hz'])))
        # ECDL completion (2026-10-26) also needs settled TEC-centering evidence from the worker; publish a centred row.
        tec=dict(status='centered', pzt_dac=32000., center_dac=32000., tolerance_count=10., confirmation_s=1e-6, max_age_s=5.,
                 reference_confirmed=True, context_revision='center-1', observed_unix_s=time.time(), target_frequency_hz=TARGET+20e6)
        (lock/'lock-target.json').write_text(json.dumps(dict(target_frequency_hz=TARGET+20e6,
            ramped_target_hz=TARGET+20e6, ramp_active=False, frequency_hz=TARGET+20e6+1e6, updated_unix_s=time.time(),
            integral_fraction=.99, automatic_recentre=False, target_strategy='target_feedforward', tec_centering=tec)))
    result=lt.run_lock_target(card,laser(),{'id':'target'},dict(offset_hz=20e6,verify_samples=3,tolerance_hz=10e6,sample_interval_s=.1),
        clock=lambda:tick[0],sleep=sleep,cancel=lambda:False,emit=lambda k,v:events.append(k),api=api)
    assert result['verify']['ok']
    assert 'lock_target_recentre' not in events
