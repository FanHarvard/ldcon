"""Whole records through the real Sweep/Instrument, with a nonlinear fake laser."""
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from types import SimpleNamespace

import pytest

from ldcon.owners.dlcpro_awg_plan import configuration
from .test_dlcpro_awg import make, request
from .test_dlcpro_offline import rig


def staircase(levels=None, dwell=1.5, settle=.5):
    value = request(); p = value['plan']
    p.update(levels=[0., 50e6, 100e6, 50e6, 0.] if levels is None else levels,
             dwell_s=dwell, settle_s=settle, acquire_s=dwell-settle)
    p['duration_s'] = p['lead_s']+len(p['levels'])*dwell+p['tail_s']
    value['plan_sha256'] = hashlib.sha256(json.dumps(p,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return value


@pytest.fixture
def tracked(rig, monkeypatch):
    o, s, d = make(rig, monkeypatch)
    o.control.update(kp=.45, ki_per_s=1., integral_limit_v=10., capture_hz=50e6)
    s.config['track_plateaus'] = True
    s.config['max_duration_s'] = 30.
    ring = []
    o.observation.samples_after = lambda after:[x for x in ring if x.acquired_s > after]
    o.awg_sweep = SimpleNamespace(tick=s.tick, active=s.active, stop=lambda:s.finish('cancelled','main stop',resume=False))
    return dict(rig=rig, owner=o, sweep=s, driver=d, ring=ring)


def play(t, req=None, *, gain=1.1, drift=1e6, step_hook=None, poll_s=.02):
    req = staircase() if req is None else req
    rig, o, s, d = t['rig'], t['owner'], t['sweep'], t['driver']
    token = s.begin(req, o.target_revision)['token']
    start = rig['clock'](); p = req['plan']
    restore = s.port.restore
    def park():
        restore()
        rig['source'].bias = drift*(rig['clock']()-start)
    s.port.restore = park
    for index in range(round((p['duration_s']+.3)/poll_s)):
        rig['clock'].sleep(poll_s); elapsed = rig['clock']()-start
        level_index = int((elapsed-p['lead_s'])/p['dwell_s'])
        level = p['levels'][level_index] if p['lead_s'] <= elapsed < p['duration_s']-p['tail_s'] else 0.
        d.values['io:fine-2:value-act'] = level/s.config['frequency_hz_per_input_v']*s.config['adc_v_per_awg_v']
        rig['source'].bias = gain*level+drift*elapsed
        t['ring'].append(rig['source'].read_mode().frequency)
        o.heartbeat(); s.heartbeat(token)
        if step_hook: step_hook(t, elapsed, level_index)
        o.tick()
        if not s.active(): break
    return s.last


def test_tracking_corrects_only_residual_and_keeps_original_pi_context(tracked):
    o, s, d = tracked['owner'], tracked['sweep'], tracked['driver']
    o.integral = .17; old = (o.target, o.target_revision, o.integral)
    result = play(tracked)
    assert result['playback_completed'] and result['restored'] and result['pi_resumed']
    assert result['tracking']['qualified'] and not result['tracking']['schedule_extended']
    assert (o.target, o.target_revision, o.integral) == old
    rows = result['tracking_samples']; assert rows and all(r['verified'] for r in rows)
    assert max(r['target_hz']-old[0] for r in rows) == 100e6
    # Nonlinear Fine1 already adds positive feedforward. PI must subtract its
    # residual, not add another 50/100 MHz feedforward displacement to the base.
    assert all(r['applied_base_v'] <= 70.000001 for r in rows)
    assert max(abs(r['applied_base_v']-70.) for r in rows) < .4
    assert not any(path.endswith('tc:temp-set') for path, _ in d.writes)
    assert 'tracking_samples' not in s.view()
    assert result['proof']['software_pi_during_record'] is True
    assert result['proof']['tec_during_record'] is False
    # Existing restore rebases the original PI to the actual corrected output.
    sample = o.read_control()
    desired = o.base+o.control['kp']*(o.target-sample.frequency_hz)/1e8+o.integral
    assert desired == pytest.approx(d.state['piezo'])


def test_explicit_false_preserves_open_loop_default(tracked):
    tracked['sweep'].config['track_plateaus'] = False
    result = play(tracked)
    assert result['verdict'] == 'succeeded' and 'tracking' not in result
    assert not result['proof']['software_pi_during_record']
    assert not any(path.endswith('pc:voltage-set') for path, _ in tracked['driver'].writes)


def test_tracking_uses_single_ordered_write_readback_transaction(tracked, monkeypatch):
    driver = tracked['driver']; original = driver.write; writes = []
    def checked(path, value, **kwargs):
        if path.endswith('pc:voltage-set'):
            writes.append(kwargs)
            assert kwargs.get('compact') is True
        return original(path, value, **kwargs)
    monkeypatch.setattr(driver, 'write', checked)
    result = play(tracked)
    assert writes and result['tracking']['qualified'] and result['pi_resumed']


@pytest.mark.parametrize('value', [1, 'true', {}, None])
def test_tracking_opt_in_requires_a_boolean(tracked, value):
    p = dict(tracked['sweep'].config, track_plateaus=value)
    with pytest.raises(ValueError, match='boolean'): configuration(p)


def test_ambiguous_edges_freeze_and_repeated_coordinates_have_new_epochs(tracked):
    result = play(tracked, gain=1., drift=0.)
    s = tracked['sweep']; p = result['request']['plan']; lo, hi = result['trigger_bounds_s']
    for r in result['tracking_samples']:
        left = hi+p['lead_s']+r['index']*p['dwell_s']
        right = lo+p['lead_s']+(r['index']+1)*p['dwell_s']
        assert left < r['adc_started_s'] <= r['adc_completed_s'] < right
        assert r['acquired_s'] > left
        assert r['write_started_s']+s.config['adc_max_interval_s'] < right
    assert {r['index'] for r in result['tracking_samples']} == {1,2,3,4}
    assert tracked['driver'].state['piezo'] == 70.


def test_tracking_saturation_honors_current_full_wave_and_antiwindup(tracked):
    o = tracked['owner']; inst = o.instrument
    # Full ARC reaches +0.1 mA; leave a small negative PI correction interval.
    o.policy['axes']['current']['bounds'] = [99.995, 100.11]
    from ldcon.core.interface_schema import digest
    inst.channels[o.channel].update(policy=deepcopy(o.policy), revision=digest(o.policy))
    result = play(tracked, gain=1.3, drift=0.)
    p = result['proof']['tracking']; assert p['base_command_bounds_v'][0] > 69.95
    assert all(99.995 <= i <= 100.11 for i in p['total_current_envelope_ma'])
    rows = result['tracking_samples']; assert any(r['saturated'] for r in rows)
    saturated = [r for r in rows if r['saturated']]
    assert all(r['integral_v'] == 0. for r in saturated)
    assert not result['tracking']['qualified']
    assert result['playback_completed']  # Accurate acquisition is a distinct fact.


def test_failure_to_settle_is_reported_without_schedule_extension(tracked):
    tracked['owner'].control.update(kp=0., ki_per_s=0.)
    result = play(tracked, gain=1.3, drift=0.)
    assert result['playback_completed'] and not result['tracking']['qualified']
    assert result['request']['plan']['dwell_s'] == 1.5
    assert max(r['maximum_absolute_error_hz'] for r in result['tracking']['plateaus']) >= 29e6


def test_capture_loss_stops_known_waveform_without_chasing_or_resuming(tracked):
    def hop(t, elapsed, _):
        if elapsed > 2.:
            t['rig']['source'].bias += 3e9
    result = play(tracked, step_hook=hop)
    assert result['verdict'] == 'failed' and 'no branch chase' in result['stop_reason']
    assert result['restored'] and not result['outputs_uncertain'] and not result['pi_resumed']
    assert not result['tracking']['active'] and tracked['owner'].mode == 'idle'


def test_cancel_freezes_tracking_and_never_changes_saved_target(tracked):
    def cancel(t, elapsed, _):
        if elapsed > 2.: t['sweep'].request_cancel(resume=False)
    result = play(tracked, step_hook=cancel)
    assert result['verdict'] == 'cancelled' and result['restored']
    assert not result['tracking']['active'] and not result['pi_resumed']
    assert tracked['owner'].target == 3e14


def test_identical_nonzero_coordinates_are_distinct_epochs(tracked):
    result = play(tracked,staircase([50e6]*3),gain=1.,drift=0.)
    assert result['tracking']['qualified']
    assert {r['index'] for r in result['tracking_samples']} == {0,1,2}
    assert all(r['target_hz'] == 3e14+50e6 for r in result['tracking_samples'])
    assert tracked['driver'].state['piezo'] == 70.  # Verified no-op PI is real evidence.


def test_all_zero_record_retains_no_trigger_rejection_without_fallback(tracked):
    before = list(tracked['driver'].writes)
    with pytest.raises(ValueError,match='distinguishable'):
        play(tracked,staircase([0.]*3))
    assert tracked['driver'].writes == before and tracked['sweep'].port is None


def test_negative_pzt_slope_uses_signed_residual_correction(tracked):
    from ldcon.core.interface_schema import digest
    o, s, d = tracked['owner'], tracked['sweep'], tracked['driver']
    o.policy['axes']['piezo']['frequency_hz_per_unit'] = -1e8
    o.instrument.channels[o.channel].update(policy=deepcopy(o.policy),revision=digest(o.policy))
    s.config['pzt_v_per_input_v'] = -1.
    source = tracked['rig']['source']; original = source.read_mode
    def reversed_pzt():
        obs = original()
        return replace(obs,frequency=replace(obs.frequency,
            frequency_hz=obs.frequency.frequency_hz-2*(d.state['piezo']-70.)*1e8))
    source.read_mode = reversed_pzt
    result = play(tracked)
    assert result['tracking']['qualified'] and result['restored']
    assert all(r['applied_base_v'] >= 70. for r in result['tracking_samples'])


def test_optical_quality_warning_does_not_become_a_tracking_stop(tracked):
    tracked['rig']['source'].degraded = True
    result = play(tracked)
    assert result['verdict'] == 'succeeded' and result['tracking']['qualified']
