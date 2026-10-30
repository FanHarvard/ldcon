"""sr422 LDC target-feedforward term and PZT headroom (target-ff design 2026-10-27, human decision 4): pure proofs and
the real rig owner over the emulated rig (owner/tests/fake_rig.py). No hardware, no network."""
import copy
import importlib.util
import json
from pathlib import Path
import socket

import pytest

from ldcon.core.instrument_client import BrokerError
from ldcon.core.interface_schema import digest
from ldcon.owners import ecdl_target_ff as ff
from ldcon.owners.ecdl_owner import ECDLOwner
from ldcon.owners.ecdl_steady_owner import NativeLockOwner

_spec = importlib.util.spec_from_file_location('ecdl_ldc_fake_rig', (Path(__file__).resolve().parent / 'support') / 'owner/tests/fake_rig.py')
fake_rig = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fake_rig)

C_NM = ff.C_NM
LDC = dict(v_per_hz=1e-11, max_abs_v=.008, bounds_v_per_hz=[-2e-11, 2e-11], hz_per_v=5.5e9)


# ------------------------------------------------------------------------------------------ pure
def state(offset=31542., held=31796., kp=0., ki=1.):
    return dict(servo_config=dict(target=421.671066, control_offset=offset, pid_kp=kp, pid_ki=ki, pid_kd=0.,
                                  pid_integral_min=-9.072e-05, pid_integral_max=9.072e-05,
                                  multiplier_control=0, multiplier_target=0, software_modulation_enable=0),
                outputs=dict(piezo=held, current=.9185, temperature=.1411))


ORIGIN = dict(target_nm=421.671066, offset_count=31542., wavelength_nm_per_count=-5.4e-8, target_feedforward_scale=1.)
PROFILE = dict(capture_hz=50e6)


def test_ldc_desired_clamps_to_max_abs_v_and_the_share_uses_the_realised_move():
    ldc = ff.ldc_policy(dict(ldc_target_feedforward=LDC))
    assert ff.ldc_desired(ldc, .9, 20e6) == pytest.approx(.9002)
    assert ff.ldc_desired(ldc, .9, 2e9) == pytest.approx(.908)                  # 0.02 V clamped to max_abs_v
    assert ff.ldc_desired(ldc, .9, -2e9, resolution=1e-4) == pytest.approx(.892)
    assert ff.ldc_share_hz(ldc, .9, .9002) == pytest.approx(1.1e6)
    assert ff.ldc_share_hz(dict(enabled=False), .9, .95) == 0.


def test_plan_subtracts_the_ldc_share_from_the_pzt_term_only():
    target = C_NM / ORIGIN['target_nm'] + 20e6
    full = ff.plan(PROFILE, state(), ORIGIN, target, (0., 65535.))
    shared = ff.plan(PROFILE, state(), ORIGIN, target, (0., 65535.), ldc_share_hz=1.1e6)
    assert shared['values']['target'] == full['values']['target']                        # the PI target is the true one
    assert shared['values']['system_min'] == full['values']['system_min']
    expected = round(ORIGIN['offset_count'] + (C_NM / (target - 1.1e6) - ORIGIN['target_nm']) / ORIGIN['wavelength_nm_per_count'])
    assert shared['values']['control_offset'] == expected and shared['ldc_share_hz'] == 1.1e6
    assert abs(shared['delta_count']) < abs(full['delta_count'])


def test_headroom_geometry_in_counts_and_hz():
    room = ff.headroom(PROFILE, state(), ORIGIN, (0., 65535.))
    hz_per_count = C_NM / 421.671066 ** 2 * 5.4e-8                                           # ~91 kHz/count, slope < 0 -> up
    assert room['hz_per_count'] == pytest.approx(hz_per_count)
    assert room['up_counts'] == pytest.approx(65535. - 31796.) and room['up_hz'] == pytest.approx(room['up_counts'] * hz_per_count)
    assert room['down_counts'] == pytest.approx(31542. - 9.072e-05) and room['down_hz'] == pytest.approx(room['down_counts'] * hz_per_count)
    narrow = ff.headroom(PROFILE, state(), ORIGIN, (31000., 31900.))
    assert narrow['up_counts'] == pytest.approx(104.) and narrow['down_counts'] == pytest.approx(542. - 9.072e-05)
    assert ff.headroom(PROFILE, state(), dict(ORIGIN, target_feedforward_scale=2.), (0., 65535.))['up_hz'] == pytest.approx(room['up_hz'] / 2)
    # the proportional and integral terms widen the envelope edge
    gained = ff.headroom(PROFILE, state(ki=1e7), ORIGIN, (0., 65535.))          # 907 counts of integral authority
    assert gained['up_counts'] < room['up_counts']


# ------------------------------------------------------------------------------------------ the owner over the fake rig
@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError('No hardware/network access in offline test')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    import ldcon.drivers.siglent as siglent
    monkeypatch.setattr(siglent, 'VisaTransport', fake_rig.Visa)
    monkeypatch.setattr(NativeLockOwner, 'watch_enabled', False)
    monkeypatch.setattr(NativeLockOwner, 'passive_dither', lambda self: None)


class Rig(fake_rig.FakeRig):
    def lease_tokens(self):
        return {}

    def detach(self):
        pass


@pytest.fixture
def rig(tmp_path):
    now = [100.]
    clock = lambda: now[0]
    holder = {}

    def factory(device, adopt_leases=None):
        holder['hw'] = Rig(device, clock)
        return holder['hw']
    owner = ECDLOwner(fake_rig.laser_definition(), audit_path=tmp_path / 'audit.jsonl', factory=factory, clock=clock)
    owner.sleep = lambda s: now.__setitem__(0, now[0] + s)
    lease = owner.dispatch(dict(action='claim', revision=digest(owner.device), owner='test'))['lease']
    hw = holder['hw']

    def call(action, **kw):
        return owner.dispatch(dict(action=action, lease=lease, **kw))
    assert call('steady_ready', strategy='hold')['ready']
    call('steady_ready', strategy='hold')
    f0 = hw.firmware.frequency()
    assert call('steady_arm', strategy='hold', target_frequency_hz=f0, tolerance_hz=5e6)['locked']
    s = owner.steady
    s.p['target_feedforward_enabled'] = True
    return owner, hw, now, call, f0


def audits(owner, kind):
    return [json.loads(l) for l in owner.audit_path.read_text().splitlines() if kind in l]


def current_writes(hw, since):
    return [l for l in hw.bk.log[since:] if l.startswith('C1:BSWV') and not l.endswith('?')]


def test_ldc_term_moves_the_current_before_the_pzt_write_and_shares_the_step(rig):
    owner, hw, now, call, f0 = rig
    s = owner.steady
    s.p['ldc_target_feedforward'] = dict(LDC)
    old = copy.deepcopy(hw.firmware.cfg)
    i0 = hw.connections['current_awg'].read(1)['value']
    bk0, fw0 = len(hw.bk.log), len(hw.firmware.sets)
    result = call('steady_set_target', target_frequency_hz=f0 + 20e6)
    assert result['target_feedforward']['ldc']['enabled'] and result['target_feedforward']['origin_current'] == pytest.approx(i0)
    assert isinstance(result['target_feedforward']['pzt_headroom_hz'], dict)
    now[0] += 1.
    row = call('steady_read')
    assert not row['target_ramp_active'] and owner.fault is None
    moved = current_writes(hw, bk0)
    assert len(moved) == 1 and len(hw.firmware.sets[fw0:]) == 1
    # order: the LDC (AWG CH1) write came first, then the one firmware write of target + window + offset
    order = [l for l in hw.bk.log[bk0:] if l in moved]
    assert hw.bk.log.index(order[0]) < len(hw.bk.log)
    i1 = hw.connections['current_awg'].read(1)['value']
    assert i1 - i0 == pytest.approx(.0002, abs=1e-6)                     # 1e-11 V/Hz x 20 MHz, at the 0.1 mV resolution
    share = (i1 - i0) * LDC['hz_per_v']
    tf = s.target_ff
    assert tf['ldc_writes'] == 1 and tf['ldc_applied_v'] == pytest.approx(.0002, abs=1e-6) and tf['ldc_share_hz'] == pytest.approx(share)
    expected = round(old['control_offset'] + (C_NM / (f0 + 20e6 - share) - old['target']) / s.p['wavelength_nm_per_count'])
    assert hw.firmware.cfg['control_offset'] == expected
    assert abs(C_NM / hw.firmware.cfg['target'] - (f0 + 20e6)) < 1.      # the PI target is the true target
    applied = audits(owner, 'ldc_target_feedforward_applied')
    assert len(applied) == 1 and applied[0]['shift_hz'] == pytest.approx(20e6)
    assert audits(owner, 'target_feedforward_applied')[-1]['proof']['ldc_share_hz'] == pytest.approx(share)
    assert isinstance(row['pzt_headroom_hz'], dict) and row['pzt_headroom_hz']['up_hz'] > 0


def test_ldc_term_is_clamped_by_max_abs_v(rig):
    owner, hw, now, call, f0 = rig
    s = owner.steady
    s.p['ldc_target_feedforward'] = dict(LDC, v_per_hz=2e-11, max_abs_v=.0002)
    i0 = hw.connections['current_awg'].read(1)['value']
    call('steady_set_target', target_frequency_hz=f0 + 20e6)                     # 0.4 mV requested
    now[0] += 1.
    call('steady_read')
    assert hw.connections['current_awg'].read(1)['value'] - i0 == pytest.approx(.0002, abs=1e-6)
    assert s.target_ff['ldc_share_hz'] == pytest.approx(.0002 * LDC['hz_per_v'])


def test_ldc_off_leaves_the_pzt_only_write_and_the_current_untouched(rig):
    owner, hw, now, call, f0 = rig
    s = owner.steady
    old = copy.deepcopy(hw.firmware.cfg)
    bk0, fw0 = len(hw.bk.log), len(hw.firmware.sets)
    call('steady_set_target', target_frequency_hz=f0 + 20e6)
    assert s.target_ff['ldc']['enabled'] is False
    now[0] += 1.
    call('steady_read')
    assert not current_writes(hw, bk0) and len(hw.firmware.sets[fw0:]) == 1
    assert hw.firmware.cfg['control_offset'] == round(old['control_offset'] + (C_NM / (f0 + 20e6) - old['target']) / s.p['wavelength_nm_per_count'])
    assert not audits(owner, 'ldc_target_feedforward')


def test_refused_ldc_move_is_deferred_and_the_pzt_carries_the_whole_step(rig):
    owner, hw, now, call, f0 = rig
    s = owner.steady
    s.p['ldc_target_feedforward'] = dict(LDC)
    old = copy.deepcopy(hw.firmware.cfg)
    bk0 = len(hw.bk.log)
    call('steady_set_target', target_frequency_hz=f0 + 20e6)
    now[0] += 1.
    owner.last_write['current'] = now[0]                                  # the current axis interval has not elapsed
    call('steady_read')
    assert not current_writes(hw, bk0)
    assert 'update interval' in s.target_ff['ldc_skipped'] and s.target_ff['ldc_writes'] == 0 and s.target_ff['ldc_share_hz'] == 0.
    assert hw.firmware.cfg['control_offset'] == round(old['control_offset'] + (C_NM / (f0 + 20e6) - old['target']) / s.p['wavelength_nm_per_count'])
    assert audits(owner, 'ldc_target_feedforward_deferred') and owner.fault is None


def test_headroom_caps_the_step_and_the_ramp_resumes_when_room_returns(rig, monkeypatch):
    owner, hw, now, call, f0 = rig
    s = owner.steady
    from dataclasses import replace
    reported = call('steady_set_target', target_frequency_hz=f0 + 20e6)['target_feedforward']['pzt_headroom_hz']
    envelope_high = reported['envelope_count'][1]            # PI centre + the integral authority in counts
    assert reported['up_hz'] > 1e9                           # the whole PZT range: about 1.3 GHz of room upward
    policies = owner.policies()
    narrow = dict(policies, piezo=replace(policies['piezo'], low=0., high=envelope_high + 110.))
    monkeypatch.setattr(owner, 'policies', lambda: narrow)
    now[0] += 1.
    row = call('steady_read')
    hz_per_count = C_NM / hw.firmware.cfg['target'] ** 2 * 5.4e-8
    assert row['target_ramp_active'] and 0 < row['ramped_target_hz'] - f0 <= 110 * hz_per_count + 1.
    assert row['pzt_headroom_hz']['up_hz'] == pytest.approx(110 * hz_per_count)        # the room this step used
    now[0] += 1.
    stalled = call('steady_read')
    assert stalled['ramped_target_hz'] == pytest.approx(row['ramped_target_hz'], abs=1.) and stalled['target_ramp_active']  # held, not refused
    assert stalled['pzt_headroom_hz']['up_hz'] == pytest.approx(0., abs=hz_per_count)  # no room left: reported, no refusal
    monkeypatch.setattr(owner, 'policies', lambda: policies)                             # the TEC returned room
    now[0] += 1.
    done = call('steady_read')
    assert not done['target_ramp_active'] and done['ramped_target_hz'] == pytest.approx(f0 + 20e6) and owner.fault is None


def test_profile_reload_adopts_the_new_keys_live_and_defers_them_during_a_ramp(rig):
    owner, hw, now, call, f0 = rig
    s = owner.steady
    new = copy.deepcopy(s.p)
    new['ldc_target_feedforward'] = dict(LDC)
    new['target_slew_hz_per_s'] = 500e6
    new['tec_current_feedforward'] = dict(new['tec_current_feedforward'], current_per_temperature=-.07)
    keys = ['ldc_target_feedforward.v_per_hz', 'target_slew_hz_per_s', 'tec_current_feedforward.current_per_temperature']
    result, apply = s.prepare_profile_reload(new, keys)
    assert sorted(result['applied']) == ['ldc_target_feedforward', 'target_slew_hz_per_s', 'tec_current_feedforward'] and not result['deferred']
    apply()
    assert s.p['ldc_target_feedforward'] == LDC and s.p['target_slew_hz_per_s'] == 500e6
    assert s.p['tec_current_feedforward']['current_per_temperature'] == -.07 and s.parameter_ff_config['current_per_temperature'] == -.07
    bad = copy.deepcopy(new)
    bad['ldc_target_feedforward'] = dict(LDC, v_per_hz=5e-11)
    with pytest.raises(ValueError):
        s.prepare_profile_reload(bad, ['ldc_target_feedforward.v_per_hz'])
    call('steady_set_target', target_frequency_hz=f0 + 20e6)                     # a ramp in flight defers the FF keys
    result, apply = s.prepare_profile_reload(dict(new, target_slew_hz_per_s=600e6), ['target_slew_hz_per_s'])
    assert result['deferred'] == ['target_slew_hz_per_s'] and not result['applied']
