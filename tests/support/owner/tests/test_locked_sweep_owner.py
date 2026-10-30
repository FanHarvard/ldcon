"""sr422 locked staircase in the rig owner + the shared-SDG guard and channel_environment of the instrument service.

Simulated drivers only (fake_rig.py): the real OffsetAWG SCPI strings, RigHardware snapshot/reserved readbacks,
ECDLOwner/NativeLockOwner dispatch, firmware writes with readback; no network, no hardware."""
import json

import numpy as np
import pytest

import ldcon.drivers.siglent as siglent
import ldcon.ecdl_locked_sweep as ls
from ldcon.ecdl_owner import ECDLOwner
from ldcon.instrument_client import BrokerError
from ldcon.instrument_service import Hub
from ldcon.interface_schema import digest

from fake_rig import C_NM, FakeRig, Visa, laser_definition
from fake_projection import bind_sibling

FF = -1.33e-11          # V/Hz, ECDLcontrol current path


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setattr(siglent, 'VisaTransport', Visa)
    monkeypatch.setattr(ls.LockedSweep, 'thread_enabled', False)
    now = [100.]
    clock = lambda: now[0]
    holder = {}

    def factory(device):
        holder['hw'] = FakeRig(device, clock, single_session=('tec_awg',))
        return holder['hw']
    owner = ECDLOwner(laser_definition(), audit_path=tmp_path / 'audit.jsonl', factory=factory, clock=clock,
                      single_session_awgs=('tec_awg',))
    owner.sleep = lambda s: now.__setitem__(0, now[0] + s)
    lease = owner.dispatch(dict(action='claim', revision=digest(owner.device), owner='test'))['lease']
    hw = holder['hw']
    def call(action, **kw):
        if action == 'background_begin':
            kw.setdefault('expected_operating_mode_revision', owner.operating_mode.status()['revision'])
        return owner.dispatch(dict(action=action, lease=lease, **kw))
    # v5.1-hostcheck: the first native() call is report-only (native_input_stale), no 'newly received' refusal.
    assert call('steady_ready', strategy='hold')['ready']
    assert owner.steady.native_input_fields()['native_input_stale'] is True
    call('steady_ready', strategy='hold')
    f0 = hw.firmware.frequency()
    assert call('steady_arm', strategy='hold', target_frequency_hz=f0, tolerance_hz=5e6)['locked']
    assert hw.firmware.cfg['pid_multiplier'] == 1
    return owner, hw, now, call, tmp_path


def plan(levels_mhz=None, **kw):
    levels = [x * 1e6 for x in (levels_mhz if levels_mhz is not None else range(-100, 101, 10))]
    p = dict(schema=ls.SCHEMA, levels_hz=levels, dwell_s=.1, lead_s=.5, tail_s=.5, settle_s=.02, acquire_s=.05,
             trigger='software', ldc_ff_v_per_hz=FF, pzt_ff_gain_scale=1., request_id='a' * 32, plan_sha256='b' * 64,
             lifetime_s=120.)
    p.update(kw)
    return p


def tick(owner, now, seconds, dt=.1):
    for _ in range(int(round(seconds / dt))):
        now[0] += dt
        with owner.lock:
            owner.steady.sweep.tick()


def channel(hw, n):
    return hw.connections['current_awg'].read(n)


def calibrated(owner, hw, call):
    result = call('locked_sweep_calibrate')
    assert result['ok'], result['reasons']
    return result


# ------------------------------------------------------------------------------------------ calibration
def test_calibration_measures_adc_scale_and_restores_ch2_exactly(rig):
    owner, hw, now, call, _ = rig
    before = channel(hw, 2)
    result = calibrated(owner, hw, call)
    assert result['counts_per_v'] == pytest.approx(19500, rel=1e-3)
    assert result['dac_response_counts'] < 1
    assert ls.same_channel(channel(hw, 2), before) and channel(hw, 2)['value'] == 1.0
    writes = [l for l in hw.bk.log if l.startswith('C2:BSWV') and not l.endswith('?')]
    assert writes == ['C2:BSWV WVTP,DC,OFST,0.90000000V', 'C2:BSWV WVTP,DC,OFST,1.10000000V', 'C2:BSWV WVTP,DC,OFST,1.00000000V']
    assert not [l for l in hw.bk.log if l.startswith('C1:') and not l.endswith('?')]


def test_calibration_detects_an_analog_path_and_prepare_then_refuses(rig):
    owner, hw, now, call, _ = rig
    hw.firmware.analog_path_counts_per_v = 3000.          # CH2 would move the PZT directly
    result = call('locked_sweep_calibrate')
    assert not result['ok'] and 'analog path' in ' '.join(result['reasons'])
    with pytest.raises(BrokerError, match='calibration'):
        call('locked_sweep_prepare', plan=plan())
    assert owner.fault is None


# ------------------------------------------------------------------------------------------ refusals before any write
@pytest.mark.parametrize('change,match', [
    (dict(ldc_ff_v_per_hz=5e-11), 'approved bound'),
    (dict(lead_s=.1), 'lead_s >= '),
    (dict(levels_hz=[7e8]), 'half span'),
    (dict(trigger='continuous'), 'trigger must be'),
])
def test_refusals_happen_before_any_write(rig, change, match):
    owner, hw, now, call, _ = rig
    calibrated(owner, hw, call)
    n_bk, n_fw = len([l for l in hw.bk.log if not l.endswith('?')]), len(hw.firmware.sets)
    with pytest.raises(BrokerError, match=match):
        call('locked_sweep_prepare', plan=dict(plan(), **change))
    assert len([l for l in hw.bk.log if not l.endswith('?')]) == n_bk and len(hw.firmware.sets) == n_fw
    assert owner.fault is None and owner.steady.sweep is None


def test_prepare_refuses_without_calibration_and_with_integral_near_bound(rig):
    owner, hw, now, call, _ = rig
    with pytest.raises(BrokerError, match='calibration'):
        call('locked_sweep_prepare', plan=plan())
    calibrated(owner, hw, call)
    hw.firmware.pid_integral = 8e-5                       # 0.88 of the raw-I bound
    with pytest.raises(BrokerError, match='integral'):
        call('locked_sweep_prepare', plan=plan())
    assert owner.fault is None


# ------------------------------------------------------------------------------------------ full software-triggered shot
def run_to_armed(owner, hw, now, call, **kw):
    calibrated(owner, hw, call)
    before = dict(cfg=dict(hw.firmware.cfg), ch1=channel(hw, 1), ch2=channel(hw, 2), tec=hw.connections['tec_awg'].read(1),
                  sdg2=hw.connections['tec_awg'].read(2), tec_ff=owner.steady.tec_ff_paused)
    result = call('locked_sweep_prepare', plan=plan(**kw), ready=dict(samples=5))
    assert result['status'] == 'armed'
    return before, result


def test_software_shot_end_to_end_and_exact_restoration(rig):
    owner, hw, now, call, _ = rig
    before, prepared = run_to_armed(owner, hw, now, call)
    cfg = hw.firmware.cfg
    # Firmware: one atomic message for the four FF/target keys; window widened to the plan span + 100 MHz only.
    group = [s for s in hw.firmware.sets if 'multiplier_target' in s and s['multiplier_target'] != 0]
    assert len(group) == 1 and set(group[0]) == set(ls.FW_GROUP)
    f0 = C_NM / before['cfg']['target']
    assert C_NM / cfg['system_max'] == pytest.approx(f0 - 100e6 - 100e6, abs=1e3)
    assert C_NM / cfg['system_min'] == pytest.approx(f0 + 100e6 + 100e6, abs=1e3)
    assert cfg['target_time_match'] == 1 and cfg['target_delay_us'] == 12000
    assert owner.steady.tec_ff_paused is True
    # AWG: both bursts armed on MAN and idle at the centre; CH1 never left the BK lowest range.
    for n, name in ((1, 'ldcsw1'), (2, 'ldcsw2')):
        assert hw.bk.ch[n].burst['STATE'] == 'ON' and hw.bk.ch[n].burst['TRSR'] == 'MAN' and hw.bk.ch[n].arb == name
    assert hw.bk.voltage(1) == pytest.approx(before['ch1']['value']) and hw.bk.voltage(2) == pytest.approx(1.0)
    # CH1 record: every sample step and every 0.5 s change within the current max_step (0.002 V).
    up = hw.bk.uploads['ldcsw1']
    amp = prepared['programmed']['ch1_amp_vpp']
    v1 = up['codes'] / 32767 * amp / 2
    assert np.max(np.abs(np.diff(np.r_[0, v1, 0]))) <= .002 and prepared['programmed']['ch1_largest_change_per_interval_v'] <= .002
    assert v1[0] == 0 and v1[-1] == 0 and amp == pytest.approx(2 * 100e6 * abs(FF), rel=1e-3)
    # READY from own samples, once-only fire, playback detected on the ADC, done, restore after the stop.
    with pytest.raises(BrokerError, match='READY'):
        call('locked_sweep_trigger')
    tick(owner, now, .6)
    st = call('locked_sweep_status')
    assert st['state'] == 'armed' and st['ready'] and st['measured']['max_abs_error_hz'] < 1e3
    fired = call('locked_sweep_trigger')
    assert fired['source'] == 'MAN' and 'C1:BTWV MTRIG; C2:BTWV MTRIG' == fired['command']
    again = call('locked_sweep_trigger')
    assert again['repeated'] and again['fired_unix_s'] == fired['fired_unix_s']
    assert sum('MTRIG' in l for l in hw.bk.log) == 3          # CH2 preflight + one per channel for the shot
    assert not hw.bk.dropped and not [l for l in hw.bk.log if 'WVDT' in l and 'LENGTH' in l]
    tick(owner, now, .5)
    assert call('locked_sweep_status')['state'] == 'playing'
    tick(owner, now, 3.)
    st = call('locked_sweep_status')
    assert st['state'] == 'done'
    levels = {r['level_index'] for r in st['records'] if r['level_index'] is not None}
    assert len(levels) >= 15
    assert max(abs(r['error_hz']) for r in st['records'] if r['error_hz'] is not None) < 1e3
    st = call('locked_sweep_restore', reason='shot stop')
    assert st['state'] == 'settling'
    tick(owner, now, .6)
    st = call('locked_sweep_status')
    assert st['state'] == 'restored' and st['restored'] and st['measured_after']['ready']
    for key in ls.FW_GROUP + ls.FW_SINGLE:
        assert cfg[key] == before['cfg'][key], key
    assert ls.same_channel(channel(hw, 1), before['ch1']) and ls.same_channel(channel(hw, 2), before['ch2'])
    assert hw.bk.ch[1].burst['STATE'] == 'OFF' and hw.bk.ch[1].arb == 'ldcd0'
    assert hw.connections['tec_awg'].read(1) == before['tec'] and hw.connections['tec_awg'].read(2) == before['sdg2']
    assert not [l for l in hw.sdg.log if not l.endswith('?')]           # TEC AWG never written
    assert owner.steady.tec_ff_paused == before['tec_ff'] and owner.fault is None
    with pytest.raises(Exception, match='overlaps the locked sweep'):
        owner.steady.read_passive_dither()
    audit = [json.loads(l) for l in (owner.audit_path).read_text().splitlines()]
    kinds = [a['kind'] for a in audit]
    assert 'locked_sweep_system_window' in kinds and 'locked_sweep_system_window_restored' in kinds
    # A new sweep may follow (calibration still fresh).
    assert call('locked_sweep_prepare', plan=plan(request_id='c' * 32))['status'] == 'armed'


def test_stop_during_playback_waits_for_the_record_end(rig):
    owner, hw, now, call, _ = rig
    run_to_armed(owner, hw, now, call)
    tick(owner, now, .6)
    call('locked_sweep_trigger')
    tick(owner, now, 1.)
    st = call('locked_sweep_restore', reason='labscript stop')
    assert st['state'] == 'restore_pending'
    assert hw.bk.ch[1].burst['STATE'] == 'ON'                 # nothing cut mid-record
    n_writes = len([l for l in hw.bk.log if not l.endswith('?')])
    tick(owner, now, 1.)
    assert len([l for l in hw.bk.log if not l.endswith('?')]) == n_writes
    tick(owner, now, 2.5)
    st = call('locked_sweep_status')
    assert st['state'] in ('settling', 'restored') and hw.firmware.cfg['multiplier_target'] == 0
    tick(owner, now, .6)
    assert call('locked_sweep_status')['state'] == 'restored'


def test_sustained_error_aborts_immediately_and_restore_is_unverified_until_settled(rig):
    owner, hw, now, call, _ = rig
    run_to_armed(owner, hw, now, call)
    tick(owner, now, .6)
    call('locked_sweep_trigger')
    tick(owner, now, .5)
    hw.firmware.error_nm = 1e-4                                # ~170 MHz off the effective target
    tick(owner, now, .6)
    st = call('locked_sweep_status')
    assert 'error' in st['abort_reason'] and st['state'] == 'settling'
    assert hw.firmware.cfg['multiplier_control'] == 0 and hw.bk.ch[1].burst['STATE'] == 'OFF'
    tick(owner, now, 21.)
    st = call('locked_sweep_status')
    assert st['state'] == 'restored_unverified' and not st['restored'] and owner.fault is None


def test_lost_session_heartbeat_restores_an_armed_sweep(rig):
    owner, hw, now, call, _ = rig
    run_to_armed(owner, hw, now, call)
    with owner.lock:
        for _ in range(110):
            now[0] += .1
            owner.steady.sweep.tick()                          # no status RPC = no heartbeat
    assert owner.steady.sweep.abort_reason == 'session heartbeat lost'
    assert owner.steady.sweep.state == 'restored' and hw.firmware.cfg['multiplier_target'] == 0


def test_uncertain_prepare_write_quarantines_and_keeps_the_lease(rig):
    owner, hw, now, call, _ = rig
    calibrated(owner, hw, call)
    hw.bk.fail_on = 'C2:ARWV'
    with pytest.raises(BrokerError):
        call('locked_sweep_prepare', plan=plan())
    assert owner.fault and 'Uncertain locked sweep programming' in owner.fault
    assert owner.lease is not None and owner.steady.sweep.state == 'review_required'
    with pytest.raises(BrokerError):
        call('locked_sweep_prepare', plan=plan(request_id='d' * 32))


def test_uncertain_restore_write_quarantines(rig):
    owner, hw, now, call, _ = rig
    run_to_armed(owner, hw, now, call)
    hw.firmware.fail_on = 'multiplier_target'
    with pytest.raises(BrokerError):
        call('locked_sweep_restore')
    assert owner.fault and owner.steady.sweep.state == 'review_required'


def test_hold_during_a_sweep_restores_awg_and_firmware_first(rig):
    owner, hw, now, call, _ = rig
    before, _ = run_to_armed(owner, hw, now, call)
    held = call('steady_hold', reason='card exit')
    assert held['held'] and hw.firmware.cfg['pid_multiplier'] == 0
    assert hw.firmware.cfg['multiplier_target'] == 0 and ls.same_channel(channel(hw, 1), before['ch1'])
    assert owner.steady.sweep.state == 'restored_unverified'


def test_other_steady_operations_are_refused_while_sweeping(rig):
    owner, hw, now, call, _ = rig
    run_to_armed(owner, hw, now, call)
    with pytest.raises(BrokerError, match='locked sweep owns'):
        call('steady_hot_control', command=dict(action='pause_tec_ff'))
    row = call('steady_read')
    assert row['actions'] == [] and row['sweep_mode']['state'] == 'armed'


def test_external_trigger_plan_is_never_software_fired(rig):
    owner, hw, now, call, _ = rig
    run_to_armed(owner, hw, now, call, trigger='external')
    assert hw.bk.ch[1].burst['TRSR'] == 'EXT' and hw.bk.ch[2].burst['TRSR'] == 'EXT'
    tick(owner, now, .6)
    with pytest.raises(BrokerError, match='not software-triggered'):
        call('locked_sweep_trigger')


def test_flat_ldc_record_with_zero_coefficient_keeps_ch1_at_its_dc_offset(rig):
    owner, hw, now, call, _ = rig
    run_to_armed(owner, hw, now, call, ldc_ff_v_per_hz=0.)
    assert not np.any(hw.bk.uploads['ldcsw1']['codes'])
    tick(owner, now, .6)
    call('locked_sweep_trigger')
    for _ in range(30):
        tick(owner, now, .1)
        assert hw.bk.voltage(1) == pytest.approx(.9185)


# ------------------------------------------------------------------------------------------ shared SDG guard + channel_environment
def hub_with(owner, hw):
    instruments = dict(repump_1092=dict(resource='TCPIP::192.168.1.37::INSTR', profile='siglent_sdg2000x', channels=[2],
                                        timeout_s=10., visa_backend='@py', require_channel_environment=True,
                                        expected_identity='Siglent Technologies,SDG2042X,SDG2XFBCA00839,2.01.01.39R7'))
    hub = Hub(instruments, simulate=False, factory=lambda config: hw.sdg)
    hub.ecdl_rigs['sr422'] = owner
    hub.instruments['repump_1092'].config['shared_with_rig'] = 'sr422'
    hub.instruments['repump_1092'].config['route_through_rig']=dict(rig='sr422',awg='tec_awg')
    hub.link_shared()
    bind_sibling(hub,owner)
    return hub


CLAIM = dict(action='claim', instrument='repump_1092', channel=2, resource='TCPIP::192.168.1.37::INSTR', profile='siglent_sdg2000x', owner='sr1092 ff')


def test_shared_guard_both_directions(rig):
    owner, hw, now, call, _ = rig
    hub = hub_with(owner, hw)
    lease = hub.dispatch(CLAIM)['lease']
    calibrated(owner, hw, call)
    with pytest.raises(BrokerError, match='shared instrument is leased'):
        call('locked_sweep_prepare', plan=plan())
    assert owner.steady.sweep is None
    hub.dispatch(dict(action='release', instrument='repump_1092', lease=lease))
    call('locked_sweep_prepare', plan=plan())
    with pytest.raises(BrokerError, match='locked sweep is in progress'):
        hub.dispatch(CLAIM)
    call('locked_sweep_restore')
    tick(owner, now, .6)
    assert owner.steady.sweep.state == 'restored'
    assert hub.dispatch(CLAIM)['lease']


def test_channel_environment_is_mandatory_before_writes_and_ch1_is_unreachable(rig):
    owner, hw, now, call, _ = rig
    hub = hub_with(owner, hw)
    lease = hub.dispatch(CLAIM)['lease']
    write = dict(action='transaction', instrument='repump_1092', lease=lease, operations=[dict(op='write', data='C2:BSWV WVTP,DC,OFST,1')])
    with pytest.raises(BrokerError, match='channel_environment'):
        hub.dispatch(write)
    assert hub.dispatch(dict(action='transaction', instrument='repump_1092', lease=lease, operations=[dict(op='query', data='C2:BSWV?')]))
    hw.sdg.coupling = 'COUP TRACE,OFF,FCOUP,ON,PCOUP,OFF,ACOUP,OFF'
    env = hub.dispatch(dict(action='channel_environment', instrument='repump_1092', lease=lease))
    assert not env['ok'] and 'FCOUP' in ' '.join(env['reasons'])
    with pytest.raises(BrokerError, match='channel_environment'):
        hub.dispatch(write)
    hw.sdg.coupling = 'COUP TRACE,OFF,FCOUP,OFF,PCOUP,OFF,ACOUP,OFF,FDEV,0HZ,FRAT,1'
    env = hub.dispatch(dict(action='channel_environment', instrument='repump_1092', lease=lease))
    assert env['ok'] and env['identity'].startswith('Siglent')
    hub.dispatch(write)
    assert hw.sdg.ch[2].wave['OFST'] == '1'
    bad = dict(write, operations=[dict(op='write', data='C1:BSWV WVTP,DC,OFST,0.5')])
    with pytest.raises(BrokerError, match='leased channel'):
        hub.dispatch(bad)
    with pytest.raises(BrokerError, match='channel is not served'):
        hub.dispatch(dict(CLAIM, channel=1))
    assert hw.sdg.ch[1].wave['OFST'] == '0.1411V'


def test_identity_mismatch_is_reported(rig):
    owner, hw, now, call, _ = rig
    hub = hub_with(owner, hw)
    lease = hub.dispatch(CLAIM)['lease']
    hw.sdg.idn = 'Siglent Technologies,SDG2042X,OTHER,2.01'
    env = hub.dispatch(dict(action='channel_environment', instrument='repump_1092', lease=lease))
    assert not env['ok'] and 'identity' in env['reasons'][0]


def test_shared_entry_requires_channel_environment_flag(rig):
    owner, hw, now, call, _ = rig
    hub = Hub(dict(repump_1092=dict(resource='TCPIP::192.168.1.37::INSTR', profile='siglent_sdg2000x', channels=[2])), simulate=False,
              factory=lambda config: hw.sdg)
    hub.ecdl_rigs['sr422'] = owner
    hub.instruments['repump_1092'].config['shared_with_rig'] = 'sr422'
    with pytest.raises(ValueError, match='require_channel_environment'):
        hub.link_shared()


def test_owner_thread_samples_and_finishes_the_settle(rig, monkeypatch):
    import time
    owner, hw, now, call, _ = rig
    monkeypatch.setattr(ls.LockedSweep, 'thread_enabled', True)
    run_to_armed(owner, hw, now, call)
    sweep = owner.steady.sweep
    time.sleep(.6)
    assert call('locked_sweep_status')['record_count'] >= 3
    call('locked_sweep_restore')
    deadline = time.time() + 5
    while sweep.state != 'restored' and time.time() < deadline:
        time.sleep(.05)
    assert sweep.state == 'restored'
    sweep.thread.join(1.)
    assert not sweep.thread.is_alive()


def test_full_600_mhz_record_fits_the_bk_and_the_current_budget(rig):
    owner, hw, now, call, _ = rig
    calibrated(owner, hw, call)
    levels = list(range(-600, 601, 10))
    with pytest.raises(BrokerError, match=r'need lead_s >= 2\.'):
        call('locked_sweep_prepare', plan=plan(levels, lead_s=1., tail_s=1.))
    result = call('locked_sweep_prepare', plan=plan(levels, lead_s=2.65, tail_s=2.65))
    pg = result['programmed']
    assert pg['record']['points'] <= 32768 and pg['ch1_amp_vpp'] == pytest.approx(2 * 600e6 * abs(FF), rel=1e-3)
    assert pg['ch1_largest_change_per_interval_v'] <= .002 and pg['ch2_amp_vpp'] == pytest.approx(1.2, rel=1e-3)
    assert abs(pg['pzt_excursion'][1] - pg['pzt_excursion'][0]) == pytest.approx(2 * 600e6 * (421.671066 ** 2 / C_NM) / 5.4e-8, rel=1e-3)
    tick(owner, now, .6)
    call('locked_sweep_trigger')
    records = []
    for _ in range(18):                                   # the session heartbeats through status every second
        tick(owner, now, 1.)
        st = call('locked_sweep_status', since=len(records))
        records += st['records']
    st['records'] = records
    assert st['state'] == 'done' and len({r['level_index'] for r in st['records'] if r['level_index'] is not None}) >= 115
    call('locked_sweep_restore')
    tick(owner, now, .6)
    assert call('locked_sweep_status')['state'] == 'restored'


# ------------------------------------------------------------------------------------------ 2026-10-22 R0 regressions (v3)
def test_preflight_failure_is_a_clean_refusal_with_ch2_restored(rig):
    owner, hw, now, call, _ = rig
    calibrated(owner, hw, call)
    before_ch2, fw = channel(hw, 2), dict(hw.firmware.cfg)
    hw.bk.mtrig_works = False                                 # the R0 symptom: MTRIG acknowledged, record never starts
    with pytest.raises(BrokerError, match='preflight failed'):
        call('locked_sweep_prepare', plan=plan())
    assert owner.fault is None and owner.steady.sweep is None
    assert ls.same_channel(channel(hw, 2), before_ch2) and hw.bk.ch[2].burst['STATE'] == 'OFF'
    assert not [l for l in hw.bk.log if l.startswith('C1:') and not l.endswith('?')]      # CH1 never touched
    assert hw.firmware.cfg == fw                                                            # firmware never touched


def test_restore_re_uploads_the_reviewed_dither_when_the_name_is_gone(rig):
    owner, hw, now, call, _ = rig
    before, _ = run_to_armed(owner, hw, now, call)
    del hw.bk.uploads['ldcd0']                                # R0: ARWV NAME,ldcd0 alone did not take
    call('locked_sweep_restore')
    tick(owner, now, .6)
    assert call('locked_sweep_status')['state'] == 'restored' and owner.fault is None
    assert hw.bk.ch[1].arb == 'ldcd0' and ls.same_channel(channel(hw, 1), before['ch1'])
    sweep = owner.steady.sweep
    assert sweep.restore_result['checks']['ch1_dither']['payload_sha256'] == owner.steady.p['dither']['programming']['payload_sha256']
    writes = [l for l in hw.bk.log if not l.endswith('?')]
    assert writes.index('C2:BSWV WVTP,DC,OFST,1.00000000V') < max(i for i, l in enumerate(writes) if l.startswith('C1:WVDT'))


def test_cleanup_returns_ch2_from_the_0811_state_to_the_reviewed_rest(rig):
    owner, hw, now, call, _ = rig
    run_to_armed(owner, hw, now, call)
    # Reproduce the 08:11 end state: firmware restored, CH2 left as an armed ARB burst idling at 1.0 V.
    sweep = owner.steady.sweep
    owner.steady.write({k: sweep.before['servo_config'][k] for k in ls.FW_GROUP}, 'test')
    sweep.state = 'restored_unverified'
    assert hw.bk.ch[2].burst['STATE'] == 'ON' and hw.bk.ch[2].wave['WVTP'] == 'ARB'
    result = call('locked_sweep_cleanup')
    import json as _json
    evidence = _json.load(open(owner.device['control_settings']['steady_lock']['dither']['evidence']))['reserved']['current_awg/CH2']
    assert result['changed'] and ls.same_channel(channel(hw, 2), evidence) and owner.fault is None
    assert call('locked_sweep_cleanup')['changed'] is False


# ------------------------------------------------------------------------------------------ v3.1 restore_reviewed_dither
@pytest.fixture
def held(tmp_path, monkeypatch):
    """The 2026-10-22 recovery state: rig claimed, PI open (never armed), CH1 = flat ldcsw1, ldcd0 gone, CH2 armed ARB."""
    import numpy as np
    monkeypatch.setattr(siglent, 'VisaTransport', Visa)
    now = [100.]
    clock = lambda: now[0]
    holder = {}

    def factory(device):
        holder['hw'] = FakeRig(device, clock)
        return holder['hw']
    owner = ECDLOwner(laser_definition(), audit_path=tmp_path / 'audit.jsonl', factory=factory, clock=clock)
    lease = owner.dispatch(dict(action='claim', revision=digest(owner.device), owner='test'))['lease']
    hw = holder['hw']
    hw.bk.uploads['ldcsw1'] = dict(WVNM='ldcsw1', AMPL='0.002', OFST='0.9185V', codes=np.zeros(5400, dtype='<i2'))
    hw.bk.uploads['ldcsw2'] = dict(WVNM='ldcsw2', AMPL='0.002', OFST='1V', codes=np.zeros(5400, dtype='<i2'))
    del hw.bk.uploads['ldcd0']
    hw.bk.ch[1].arb = 'ldcsw1'
    hw.bk.ch[2].wave = dict(WVTP='ARB', FRQ='0.37HZ', AMP='0.2V', OFST='1V')
    hw.bk.ch[2].arb = 'ldcsw2'
    hw.bk.ch[2].burst.update(STATE='ON', TRSR='MAN')
    call = lambda action, **kw: owner.dispatch(dict(action=action, lease=lease, **kw))
    return owner, hw, call


def test_restore_reviewed_dither_and_cleanup_from_the_held_0811_state(held):
    owner, hw, call = held
    before = channel(hw, 1)
    sets = len(hw.firmware.sets)
    result = call('restore_reviewed_dither')
    reviewed = owner.device['control_settings']['steady_lock']['dither']['programming']
    assert result['restored'] and result['programmed']['payload_sha256'] == reviewed['payload_sha256'] == '9320dd1d793e2a67f774820828ae6e0a7dddbdde0c93ec86629f83e83e83a481'
    ch1 = channel(hw, 1)
    assert hw.bk.ch[1].arb == 'ldcd0' and ch1['frequency_hz'] == .25 and ch1['amplitude_vpp'] == .002 and ch1['value'] == before['value']
    assert len(hw.firmware.sets) == sets and owner.fault is None                      # firmware untouched
    assert not [l for l in hw.sdg.log if not l.endswith('?')]                       # TEC AWG untouched
    out = call('locked_sweep_cleanup')
    assert out['changed'] and out['ch2']['waveform'] == 'DC' and out['ch2']['value'] == 1.0 and out['ch2']['burst'] == 'OFF'
    # The steady lock's own check accepts the restored CH1.
    owner.steady.check_dither(owner.hw().snapshot(), owner.device['control_settings']['steady_lock'])


def test_restore_reviewed_dither_refuses_unsafe_states(held):
    owner, hw, call = held
    hw.bk.ch[1].burst.update(STATE='ON', TRSR='MAN')
    with pytest.raises(BrokerError, match='without burst|armed or modulated'):
        call('restore_reviewed_dither')
    assert owner.fault is None and hw.bk.ch[1].arb == 'ldcsw1'
    hw.bk.ch[1].burst['STATE'] = 'OFF'
    hw.bk.fail_on = 'C1:ARWV'
    with pytest.raises(BrokerError):
        call('restore_reviewed_dither')
    assert owner.fault.startswith('Uncertain dither configuration')


# ------------------------------------------------------------------------------------------ v3.2 abort on obvious degradation
def test_strict_single_mode_flag_at_the_margin_does_not_abort_the_sweep(rig):
    owner, hw, now, call, _ = rig
    run_to_armed(owner, hw, now, call)
    # Exercise the flag alone: the canonical metric limit, not the retired
    # warning_max tier, determines whether this sample is degraded.
    limit = owner.device['control_settings']['optical']['single_mode_max']['side_peak_height_max']
    hw.single_mode, hw.side_peak = False, limit - .005
    tick(owner, now, 1.5)
    st = call('locked_sweep_status')
    assert st['state'] == 'armed' and st['ready'] and st['measured']['single_mode'] is False and st['measured']['not_degraded']
    hw.side_peak = limit + .005
    tick(owner, now, .6)
    st = call('locked_sweep_status')
    # v5.7 (human 2026-10-23 ~12:10Z): reported, never an abort; READY (the entry gate) is withheld while degraded
    assert st['abort_reason'] is None and st['state'] == 'armed' and not st['ready']
    assert st['degradation']['samples'] >= 5 and st['degradation']['playback_samples'] == 0
