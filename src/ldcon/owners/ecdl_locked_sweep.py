"""Locked staircase sweep executed by the sr422 ECDL rig owner while the native PZT PI stays armed.

Wiring (human + ECDLcontrol audit, 2026-10-22): BK 4054B CH2 drives the ESP32 servo ADC
(``mod_raw``; linear 0.1..1.9 V, ECDLcontrol sweep.py U_LO/U_HI); CH1 is the LDC current input
(DC offset + the verified dither ARB). The firmware follows the CH2 staircase with target
modulation (t_eff = target + multiplier_target*(mod_raw - baseline_target), nm) and PZT
feed-forward (control_offset + multiplier_control*(mod_raw - baseline_control)); the PI trims
the residual. CH1 carries a synchronized LDC feed-forward staircase INSTEAD of the dither. The
outer loop is frozen (no proposals, TEC feed-forward paused = TEC open, TEC set-point untouched).

Order of operations (every write is acknowledged and read back; any failure after the first
write latches the owner fault = quarantine, nothing is retried):
  prepare: CH1 burst armed on the dither (idle at the DC offset) -> CH1 FF ARB uploaded and
           selected as the burst carrier (idle) -> CH2 switched from DC to a 2 mV ARB at the
           centre, staircase selected, burst armed (idle at the centre) -> ADC baseline read ->
           firmware: wavemeter acceptance window widened to the plan span + margin, time
           matching, ADC validity window, then ONE atomic message with baselines+multipliers.
  fire:    one socket write with C1/C2 BTWV MTRIG (software trigger, once only).
  restore: firmware back to the snapshot (atomic group first, while the ADC sits at the
           centre), CH1 DC offset then the dither ARB (continuous), CH2 back to its DC level,
           all compared field by field with the snapshot; reserved channels compared; TEC
           feed-forward/planner resumed; then a measured settle (own wavemeter criteria).
The record always begins and ends at the centre (idle level of the burst), approaches the first
level and leaves the last level with a bounded ramp inside lead_s/tail_s, so CH1 never jumps by
more than the axis max_step (and never changes faster than max_step per update interval).
A stop during playback waits for the record to end; only a safety abort (sustained error,
mode loss, foreign firmware change, lost session heartbeat past the record) restores at once.
"""
import copy
import hashlib
import json
import math
import threading
import time

from ldcon.core.contracts import DeviceError, MeasurementUnavailable
from ldcon.drivers.ecdl import is_sdg
from ldcon.core.instrument_client import BrokerError

SCHEMA = 'ldcon.locked-staircase/v1'
C_NM = 299792458e9
ARB_CH1, ARB_CH2 = 'ldcsw1', 'ldcsw2'
IDLE_CORRECTION_MAX_COUNTS = 300.   # v5.6: SDG armed-idle DC error corrected up to this (~15 mV); beyond it a refusal
FW_GROUP = ('baseline_target', 'multiplier_target', 'baseline_control', 'multiplier_control')
FW_SINGLE = ('system_min', 'system_max', 'target_time_match', 'target_delay_us', 'mod_valid_min', 'mod_valid_max')
ACTIVE = ('preparing', 'armed', 'fired', 'playing', 'done', 'restore_pending', 'restoring')
FINISHED = ('restored', 'restored_unverified', 'rejected')
CHANNEL_KEYS = ('value', 'waveform', 'frequency_hz', 'arb_name', 'amplitude_vpp', 'enabled', 'load', 'polarity',
                'burst', 'modulation', 'sweep')

SETTINGS_KEYS = {'revision', 'awg', 'ext_mod', 'firmware', 'ldc_ff', 'limits'}
AWG_KEYS = {'channel', 'ch2_bounds_v', 'max_vpp', 'amp_min_vpp', 'max_points', 'max_sample_rate_hz',
            'ch1_range_amp_max_vpp', 'ch1_range_offset_v'}
EXT_KEYS = {'hz_per_v', 'adc_counts_per_v_nominal', 'adc_scale_tolerance', 'adc_margin_counts',
            'calibration_step_v', 'calibration_settle_s', 'calibration_max_age_s', 'analog_path_dac_tolerance',
            'baseline_tolerance_counts'}
FW_KEYS = {'target_time_match', 'target_delay_us', 'system_margin_hz'}
FF_KEYS = {'max_abs_v_per_hz'}
LIMIT_KEYS = {'max_half_span_hz', 'max_duration_s', 'max_levels', 'heartbeat_timeout_s', 'abort_error_hz',
              'abort_samples', 'approach_hz_per_s', 'settle_timeout_s', 'lifetime_max_s', 'max_integral_fraction'}
# v5.4 (2026-10-23, sweep API v1.2): the continuous triangle is accepted only when this explicit cap is configured.
LIMIT_OPTIONAL = {'continuous_max_s'}
MAX_BURST_CYCLES = 1000000      # SDG2000X / BK 4050 N-cycle burst upper bound (data sheets; not bench-verified here)


def finite(value, name, *, positive=False, nonnegative=False):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise DeviceError('Finite ' + name + ' required')
    if positive and value <= 0 or nonnegative and value < 0:
        raise DeviceError(name + ' must be ' + ('positive' if positive else 'non-negative'))
    return float(value)


def pair(value, name):
    if not isinstance(value, list) or len(value) != 2:
        raise DeviceError(name + ' must be [low, high]')
    low, high = finite(value[0], name), finite(value[1], name)
    if not low < high:
        raise DeviceError(name + ' must be increasing')
    return [low, high]


def exact_keys(mapping, keys, name):
    if not isinstance(mapping, dict) or set(mapping) != keys:
        raise DeviceError('%s must define exactly %s' % (name, sorted(keys)))
    return mapping


def configuration(device):
    """control_settings.locked_sweep of the rig: explicit envelopes and calibration choices, never guessed."""
    cs = device.get('control_settings', {}).get('locked_sweep')
    exact_keys(cs, SETTINGS_KEYS, 'control_settings.locked_sweep')
    if not isinstance(cs['revision'], str) or not cs['revision']:
        raise DeviceError('locked_sweep.revision required')
    raw_awg = cs.get('awg')
    if not isinstance(raw_awg, dict) or not AWG_KEYS <= set(raw_awg) <= AWG_KEYS | {'ch2_preflight'}:
        raise DeviceError('locked_sweep.awg must define %s (optional ch2_preflight)' % sorted(AWG_KEYS))
    awg = dict(raw_awg)
    awg.setdefault('ch2_preflight', True)
    if type(awg['ch2_preflight']) is not bool:
        raise DeviceError('awg.ch2_preflight must be boolean')
    if awg['channel'] != 2:
        raise DeviceError('Locked sweep drives the servo ADC from current AWG channel 2')
    awg['ch2_bounds_v'] = pair(awg['ch2_bounds_v'], 'awg.ch2_bounds_v')
    awg['ch1_range_offset_v'] = pair(awg['ch1_range_offset_v'], 'awg.ch1_range_offset_v')
    for key in ('max_vpp', 'amp_min_vpp', 'max_sample_rate_hz', 'ch1_range_amp_max_vpp'):
        finite(awg[key], 'awg.' + key, positive=True)
    # 2026-10-22 SDG swap: the ceiling is the largest supported current-AWG profile (SDG2000X 8 Mpts); the profile's own
    # limit (BK 4054B 32768) is checked against the compiled record in prepare, before any write.
    if type(awg['max_points']) is not int or not 16 <= awg['max_points'] <= 8_000_000:
        raise DeviceError('awg.max_points must be an integer within [16, 8000000] (BK 4054B: <= 32768)')
    ext = dict(exact_keys(cs['ext_mod'], EXT_KEYS, 'locked_sweep.ext_mod'))
    for key in EXT_KEYS:
        finite(ext[key], 'ext_mod.' + key, nonnegative=True)
    for key in ('hz_per_v', 'adc_counts_per_v_nominal', 'calibration_step_v', 'calibration_max_age_s', 'adc_scale_tolerance'):
        finite(ext[key], 'ext_mod.' + key, positive=True)
    fw = dict(exact_keys(cs['firmware'], FW_KEYS, 'locked_sweep.firmware'))
    if fw['target_time_match'] not in (0, 1) or type(fw['target_delay_us']) is not int or not 0 <= fw['target_delay_us'] <= 100000:
        raise DeviceError('firmware.target_time_match must be 0/1 and target_delay_us an integer 0..100000')
    finite(fw['system_margin_hz'], 'firmware.system_margin_hz', positive=True)
    ff = dict(exact_keys(cs['ldc_ff'], FF_KEYS, 'locked_sweep.ldc_ff'))
    finite(ff['max_abs_v_per_hz'], 'ldc_ff.max_abs_v_per_hz', nonnegative=True)
    raw_limits = cs['limits']
    if not isinstance(raw_limits, dict) or not LIMIT_KEYS <= set(raw_limits) <= LIMIT_KEYS | LIMIT_OPTIONAL:
        raise DeviceError('locked_sweep.limits must define exactly %s (optional %s)' % (sorted(LIMIT_KEYS), sorted(LIMIT_OPTIONAL)))
    limits = dict(raw_limits)
    for key in set(limits):
        finite(limits[key], 'limits.' + key, positive=True)
    for key in ('max_levels', 'abort_samples'):
        if type(limits[key]) is not int:
            raise DeviceError('limits.%s must be an integer' % key)
    if not 0 < limits['max_integral_fraction'] < 1:
        raise DeviceError('limits.max_integral_fraction must leave PI reserve')
    resources = device['connection']['resources']['current_awg']
    if resources.get('channel') != 1 or 2 not in resources.get('preserve_channels', []):
        raise DeviceError('Locked sweep expects CH1 = LDC current and CH2 reserved on the current AWG')
    return dict(revision=cs['revision'], awg=awg, ext_mod=ext, firmware=fw, ldc_ff=ff, limits=limits)


def validate_plan(plan, cfg):
    if not isinstance(plan, dict) or plan.get('schema') != SCHEMA:
        raise DeviceError('Unsupported locked sweep plan schema')
    allowed = {'schema', 'levels_hz', 'dwell_s', 'lead_s', 'tail_s', 'settle_s', 'acquire_s', 'sample_rate_hz',
               'trigger', 'ldc_ff_v_per_hz', 'pzt_ff_gain_scale', 'request_id', 'plan_sha256', 'lifetime_s', 'full_data_log_ms', 'waveform_shape'}
    if set(plan) - allowed:
        raise DeviceError('Unknown locked sweep plan keys: ' + ', '.join(sorted(set(plan) - allowed)))
    if plan.get('waveform_shape', 'staircase') not in ('triangle', 'staircase'):
        raise DeviceError('waveform_shape must be triangle or staircase')
    levels = plan.get('levels_hz')
    if (not isinstance(levels, list) or not 1 <= len(levels) <= cfg['limits']['max_levels']
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in levels)):
        raise DeviceError('levels_hz must list 1..%d finite frequency offsets' % cfg['limits']['max_levels'])
    p = dict(plan, levels_hz=[float(v) for v in levels])
    p['dwell_s'] = finite(plan.get('dwell_s'), 'dwell_s', positive=True)
    for key in ('lead_s', 'tail_s', 'settle_s', 'acquire_s'):
        p[key] = finite(plan.get(key, 0.), key, nonnegative=True)
    if p['lead_s'] <= 0 or p['tail_s'] <= 0:
        raise DeviceError('lead_s and tail_s must be positive (the record starts and ends at the centre)')
    if p['settle_s'] + p['acquire_s'] > p['dwell_s'] + 1e-12:
        raise DeviceError('Acquisition window exceeds dwell')
    p['sample_rate_hz'] = finite(plan.get('sample_rate_hz', cfg['awg']['max_sample_rate_hz']), 'sample_rate_hz', positive=True)
    if plan.get('trigger') not in ('software', 'external', 'continuous'):
        raise DeviceError('trigger must be software (MAN burst, fired by locked_sweep_trigger), external (EXT edge) or '
                          'continuous (one-period record repeated by the AWG, fired by locked_sweep_trigger)')
    if plan['trigger'] == 'continuous':
        if 'continuous_max_s' not in cfg['limits']:
            raise DeviceError('trigger must be software or external: continuous playback needs '
                              'locked_sweep.limits.continuous_max_s (not configured)')
        if (abs(p['lead_s'] - p['dwell_s'] / 2) > 1e-9 or abs(p['tail_s'] - p['dwell_s'] / 2) > 1e-9):
            raise DeviceError('A continuous record is one period: lead_s = tail_s = dwell_s/2 (the centre dwell split '
                              'across the seam)')
    p['ldc_ff_v_per_hz'] = finite(plan.get('ldc_ff_v_per_hz', 0.), 'ldc_ff_v_per_hz')
    if abs(p['ldc_ff_v_per_hz']) > cfg['ldc_ff']['max_abs_v_per_hz']:
        raise DeviceError('ldc_ff_v_per_hz exceeds the approved bound %.3g V/Hz' % cfg['ldc_ff']['max_abs_v_per_hz'])
    from ldcon.owners.full_data_log import validate_ms
    try:
        p['full_data_log_ms'] = validate_ms(plan.get('full_data_log_ms'))
    except ValueError as error:
        raise DeviceError(str(error)) from error
    p['pzt_ff_gain_scale'] = finite(plan.get('pzt_ff_gain_scale', 1.), 'pzt_ff_gain_scale')
    if not .5 <= p['pzt_ff_gain_scale'] <= 2.:
        raise DeviceError('pzt_ff_gain_scale must be within [0.5, 2]')
    if not isinstance(plan.get('request_id'), str) or not 8 <= len(plan['request_id']) <= 64:
        raise DeviceError('request_id required')
    if not isinstance(plan.get('plan_sha256'), str) or len(plan['plan_sha256']) != 64:
        raise DeviceError('plan_sha256 required')
    if max(abs(v) for v in p['levels_hz']) > cfg['limits']['max_half_span_hz']:
        raise DeviceError('Levels exceed the approved half span %.0f Hz' % cfg['limits']['max_half_span_hz'])
    duration = p['lead_s'] + p['dwell_s'] * len(p['levels_hz']) + p['tail_s']
    if duration > cfg['limits']['max_duration_s']:
        raise DeviceError('Record duration %.1f s exceeds the approved %.1f s' % (duration, cfg['limits']['max_duration_s']))
    cap = cfg['limits']['continuous_max_s'] if p['trigger'] == 'continuous' else cfg['limits']['lifetime_max_s']
    p['lifetime_s'] = finite(plan.get('lifetime_s', cap), 'lifetime_s', positive=True)
    if p['trigger'] == 'continuous' and p['lifetime_s'] > cap + 1e-9:
        raise DeviceError('lifetime_s %.1f s exceeds locked_sweep.limits.continuous_max_s %.1f s' % (p['lifetime_s'], cap))
    p['lifetime_s'] = min(p['lifetime_s'], cap)
    if p['lifetime_s'] < duration + 10.:
        raise DeviceError('lifetime_s must exceed the record duration by at least 10 s')
    return p


def validate_ready(ready):
    ready = dict(ready or {})
    if set(ready) - {'tolerance_hz', 'stability_hz', 'samples'}:
        raise DeviceError('ready accepts tolerance_hz, stability_hz and samples')
    for key, default in (('tolerance_hz', 5e6), ('stability_hz', 5e6)):
        finite(ready.setdefault(key, default), key, positive=True)
    samples = ready.setdefault('samples', 5)
    if type(samples) is not int or not 1 <= samples <= 200:
        raise DeviceError('ready.samples must be an integer within [1, 200]')
    return ready


def compile_record(plan, cfg, current_policy):
    """Per-sample frequency offsets (Hz) of one burst record with bounded approach/leave ramps.

    Returns values, the AWG sample rate, the record period and the lead/tail ramp lengths. Refuses when
    lead_s/tail_s cannot hold the ramp that keeps CH1 within max_step per update interval."""
    import numpy as np
    levels = plan['levels_hz']
    n = len(levels)
    duration = plan['lead_s'] + plan['dwell_s'] * n + plan['tail_s']
    rate = min(plan['sample_rate_hz'], cfg['awg']['max_sample_rate_hz'], cfg['awg']['max_points'] / duration)
    per = int(math.floor(rate * plan['dwell_s'] + 1e-9))
    if per < 1:
        raise DeviceError('Dwell is shorter than one AWG sample at the point budget')
    r = per / plan['dwell_s']
    n_lead, n_tail = int(round(plan['lead_s'] * r)), int(round(plan['tail_s'] * r))
    total = n_lead + per * n + n_tail
    if total > cfg['awg']['max_points']:
        n_lead -= total - cfg['awg']['max_points'] if n_lead > n_tail else 0
        n_tail -= max(0, n_lead + per * n + n_tail - cfg['awg']['max_points'])
        total = n_lead + per * n + n_tail
    if plan.get('trigger') == 'continuous' and (per % 2 or per < 4 or n_lead != per // 2 or n_tail != per // 2
                                                or total != per * (n + 1)):
        # one period exactly: the AWG repeats the record on its own clock, so the record length IS the period
        raise DeviceError('Continuous record is not exactly one period at %.6g S/s (%d points per dwell; needs an even '
                          'number >= 4 and lead/tail = half a dwell): change the rate or the span' % (r, per))
    ff = plan['ldc_ff_v_per_hz']
    budget_rate = current_policy.max_step / current_policy.update_interval_s      # V/s equivalent of one host move per interval

    def ramp_s(level):
        need = abs(level) / cfg['limits']['approach_hz_per_s']
        if ff:
            need = max(need, abs(ff * level) / (.9 * budget_rate))
        return need

    k_lead = int(math.ceil(ramp_s(levels[0]) * r)) if levels[0] else 0
    k_tail = int(math.ceil(ramp_s(levels[-1]) * r)) if levels[-1] else 0
    if k_lead + 1 > n_lead or k_tail + 1 > n_tail:
        raise DeviceError('lead_s/tail_s too short for the bounded approach/leave ramps: need lead_s >= %.3f s and tail_s >= %.3f s'
                          % ((k_lead + 1) / r, (k_tail + 1) / r))
    if plan.get('waveform_shape') == 'triangle':
        if per < 4 or per % 2 or n_lead != per // 2 or n_tail != per // 2 or total != per * (n + 1):
            raise DeviceError('Triangle requires a complete period with half-dwell lead and tail')
        # Explicit vertices, joined at the AWG sample clock. Repeating plateaus
        # would only approximate a triangle, visibly so for small excursions.
        knots = np.r_[np.arange(n + 1) * per, total - 1]
        values = np.interp(np.arange(total), knots, [0., *levels, 0.])
        return dict(values=values, rate_hz=r, points=int(total), period_s=total/r, per_level=per,
                    lead_points=n_lead, tail_points=n_tail, lead_ramp_s=per/r, tail_ramp_s=(per-1)/r,
                    levels_start_s=0., waveform_shape='triangle')
    values = np.concatenate([np.zeros(n_lead), np.repeat(np.asarray(levels, dtype=float), per), np.zeros(n_tail)])
    if k_lead:
        values[n_lead - k_lead:n_lead] = levels[0] * np.arange(1, k_lead + 1) / (k_lead + 1)
    if k_tail:
        start = n_lead + per * n
        values[start:start + k_tail] = levels[-1] * (1 - np.arange(1, k_tail + 1) / (k_tail + 1))
    if values[0] != 0 or values[-1] != 0:
        raise DeviceError('Record must start and end at the centre')
    return dict(values=values, rate_hz=r, points=int(total), period_s=total / r, per_level=per,
                lead_points=n_lead, tail_points=n_tail, lead_ramp_s=k_lead / r, tail_ramp_s=k_tail / r,
                levels_start_s=n_lead / r)


def channel_codes(values, amp_min):
    """Signed 16-bit codes centred on 0 (code 0 = the DC centre, the burst idle level) and the amplitude."""
    import numpy as np
    v = np.asarray(values, dtype=float)
    peak = float(np.max(np.abs(v))) if v.size else 0.
    amp = max(amp_min, 2 * peak)
    codes = np.clip(np.rint(v / (amp / 2) * 32767), -32767, 32767).astype('<i2')
    return codes, amp


def ch1_envelope(values_v, rate_hz, policy):
    """Largest single CH1 step and largest CH1 total variation within one host update interval."""
    import numpy as np
    v = np.asarray(values_v, dtype=float)
    d = np.abs(np.diff(np.r_[0., v, 0.]))
    step = float(d.max()) if d.size else 0.
    w = max(1, int(round(policy.update_interval_s * rate_hz)))
    tv = np.r_[0., np.cumsum(d)]
    window = float(np.max(tv[w:] - tv[:-w])) if tv.size > w else float(tv[-1])
    return step, window


def rehearsal_key(codes, freq, amp, centre, source, identity, profile, revision, calibration_unix_s):
    """v5.4: cache key of a passed CH2 rehearsal (the exact record bytes and every playback setting it proved)."""
    import numpy as np
    h = hashlib.sha256(np.asarray(codes, dtype='<i2').tobytes())
    h.update(json.dumps(dict(freq=float(freq), amp=float(amp), centre=float(centre), source=source, identity=str(identity),
                             profile=str(profile), revision=revision, calibration_unix_s=calibration_unix_s),
                        sort_keys=True).encode())
    return h.hexdigest()


def plan_digest(plan):
    return hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def clean(value):
    """JSON-safe copy (numpy scalars -> float, no NaN)."""
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if hasattr(value, 'item') and not isinstance(value, (str, bytes)):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def same_channel(actual, expected):
    """Field-by-field comparison with the snapshot (ARB names compared without the BK .bin suffix)."""
    from ldcon.drivers.ecdl import arb_name_matches
    for key in CHANNEL_KEYS:
        a, b = actual.get(key), expected.get(key)
        if key == 'arb_name':
            if (a is None) != (b is None) or (a is not None and not arb_name_matches(a, b)):
                return False
        elif isinstance(b, float) or isinstance(a, float):
            if a is None or b is None or not math.isclose(float(a), float(b), rel_tol=1e-9, abs_tol=1e-9):
                return False
        elif a != b:
            return False
    return True


def reviewed_dither_policy(programming, channel):
    """Narrow the driver envelope to one owner-held reviewed programming record.

    This is only a restoration adapter. Callers still verify the original
    evidence and payload digest; it grants no authority to create a new dither.
    """
    from ldcon.drivers.ecdl import dither_programming_policy
    if not isinstance(programming, dict):
        raise DeviceError('Reviewed dither programming is required')
    frequency = finite(programming.get('frequency_hz'), 'reviewed dither frequency')
    return dither_programming_policy(dict(
        channel=channel, frequency_bounds_hz=[frequency, frequency],
        max_vpp=finite(programming.get('requested_vpp'), 'reviewed dither amplitude'),
        nominal_amplitude_vpp=finite(programming.get('nominal_amplitude_vpp'), 'reviewed carrier amplitude')))


def sdg_ch1_to_dither(awg, ch1, programming, offset):
    """SDG2000X current channel back to the reviewed dither WITHOUT a waveform-type change (a DC<->ARB change drops an
    SDG output to ~0 V for ~16 ms; on CH1 that is a laser-current transient of offset x LDC gain). The channel stays an
    ARB carrier throughout: (1) nominal amplitude/offset (a no-op on the dither; shrinks a leftover record to the 2 mVpp
    carrier), (2) the reviewed dither payload uploaded (the SDG selects it at once: ARB -> ARB), (3) burst OFF last,
    so a record never plays continuously at its full amplitude. Returns (programmed or None, readback)."""
    from ldcon.drivers.ecdl import arb_name_matches
    back = awg.read(ch1)
    if back.get('waveform') != 'ARB':
        raise DeviceError('SDG CH1 is %r, not an ARB carrier: returning it to the dither needs a waveform-type change '
                          '(~16 ms at 0 V on the LDC input); do it with the LDC output OFF (human)' % back.get('waveform'))
    nominal = finite(programming['nominal_amplitude_vpp'], 'dither nominal amplitude')
    if abs(finite(back['amplitude_vpp'], 'CH1 amplitude') - nominal) > 1e-12 or abs(finite(back['value'], 'CH1 offset') - offset) > 1e-6:
        awg.ack(f'C{ch1}:BSWV AMP,{nominal:.12g},OFST,{offset:.8f}', 'CH1 nominal carrier')
        back = awg.read(ch1)
    programmed = None
    if not (arb_name_matches(back.get('arb_name'), programming['name']) and back.get('frequency_hz') is not None
            and abs(back['frequency_hz'] - finite(programming['frequency_hz'], 'dither frequency')) <= 1e-9):
        programmed = awg.configure_dither(ch1, finite(programming['frequency_hz'], 'dither frequency'),
                                          finite(programming['requested_vpp'], 'dither vpp'), back, reviewed_dither_policy(programming, ch1))
        if programmed['name'] != programming['name'] or programmed['payload_sha256'] != programming['payload_sha256']:
            raise DeviceError('Re-programmed dither differs from the reviewed programming: ' + json.dumps(programmed))
        back = awg.read(ch1)
    if back.get('burst') == 'ON':
        awg.burst_off(ch1)
        back = awg.read(ch1)
    if (back.get('waveform') != 'ARB' or not arb_name_matches(back.get('arb_name'), programming['name'])
            or back.get('burst') != 'OFF' or abs(finite(back['value'], 'CH1') - offset) > 1e-6
            or abs(finite(back['amplitude_vpp'], 'CH1 amplitude') - nominal) > 1e-12):
        raise DeviceError('SDG CH1 dither readback mismatch: ' + json.dumps(back))
    return programmed, back


def check_sdg_dds(awg, channels):
    """Read-only: the SDG2000X plays ARB records by FRQ only in DDS mode (TrueArb would reinterpret FRQ)."""
    from ldcon.drivers.siglent import fields
    for ch in channels:
        reply = awg.query(f'C{ch}:SRATE?')
        text = reply.split(' ', 1)[1] if ' ' in reply else reply
        if fields(text).get('MODE') != 'DDS':
            raise BrokerError('request', 'SDG CH%d is not in DDS ARB mode (%r): set C%d:SRATE MODE,DDS with the LDC '
                              'output OFF first (a mode change re-selects ARB)' % (ch, reply, ch))


# ------------------------------------------------------------------------------------------------ calibration
def calibrate(steady):
    """CH2 two-point DC step with the firmware multipliers at 0 (the laser cannot follow the ADC): measures the
    ADC counts/V, checks linearity, and proves there is no analog path from CH2 into the PZT (the armed PI
    would have to move the DAC). CH2 is returned to its exact snapshot. Owner lock held."""
    o = steady.o
    cfg = configuration(o.device)
    ext = cfg['ext_mod']
    sleep = getattr(o, 'sleep', time.sleep)
    hw = o.hw()
    awg = hw.connections['current_awg']
    ch2 = cfg['awg']['channel']
    state, obs = steady.read_feedback()
    steady.native(state, obs, steady.p, locked=True)
    before = awg.read(ch2)
    if (before.get('waveform') != 'DC' or not before.get('enabled') or before.get('load') != 'HZ'
            or before.get('burst') != 'OFF' or before.get('modulation') != 'OFF' or before.get('sweep') != 'OFF'):
        raise DeviceError('CH2 must be an enabled HZ DC level without burst/modulation before calibration: ' + json.dumps(before))
    centre = finite(before['value'], 'CH2 level')
    lo, hi = cfg['awg']['ch2_bounds_v']
    step = ext['calibration_step_v']
    if not lo <= centre - step and centre + step <= hi:
        raise DeviceError('Calibration steps leave the CH2 bounds')
    points = []
    o.audit('locked_sweep_calibration_intent', before=before, centre_v=centre, step_v=step)
    try:
        for v in (centre - step, centre + step, centre):
            if v != centre or points:
                awg.set_dc(ch2, v)
            back = awg.read(ch2)
            if back.get('waveform') != 'DC' or abs(back['value'] - v) > 1e-6 or back.get('burst') != 'OFF':
                raise DeviceError('CH2 calibration readback mismatch: ' + json.dumps(back))
            sleep(ext['calibration_settle_s'])
            raws, dacs = [], []
            for _ in range(5):
                status = hw.connections['piezo'].get('status')
                raws.append(float(status['mod_raw'])); dacs.append(float(status['dac_val']))
                sleep(.05)
            points.append(dict(v=v, mod_raw=sorted(raws)[2], mod_raw_spread=max(raws) - min(raws), dac=sorted(dacs)[2],
                               dac_spread=max(dacs) - min(dacs)))
        after = awg.read(ch2)
        if not same_channel(after, before):
            raise DeviceError('CH2 did not return to its snapshot after calibration: ' + json.dumps(after))
    except Exception as error:
        o.fault = 'Uncertain CH2 calibration write: ' + str(error)
        o.audit('locked_sweep_calibration_failed', error=str(error), points=points)
        raise DeviceError(o.fault) from error
    low, high, mid = points
    counts_per_v = (high['mod_raw'] - low['mod_raw']) / (high['v'] - low['v'])
    intercept = mid['mod_raw'] - counts_per_v * centre
    nominal = ext['adc_counts_per_v_nominal']
    linearity = abs(mid['mod_raw'] - (high['mod_raw'] + low['mod_raw']) / 2)
    dac_response = max(abs(p['dac'] - mid['dac']) for p in (low, high))
    result = dict(revision=cfg['revision'], centre_v=centre, step_v=step, points=points, counts_per_v=counts_per_v,
                  intercept_counts=intercept, centre_counts=mid['mod_raw'], linearity_counts=linearity,
                  dac_response_counts=dac_response, unix_s=time.time(), clock_s=o.clock(), ch2_before=before)
    reasons = []
    if abs(counts_per_v / nominal - 1) > ext['adc_scale_tolerance']:
        reasons.append('ADC scale %.1f counts/V differs from the nominal %.1f by more than %.0f %%' % (counts_per_v, nominal, 100 * ext['adc_scale_tolerance']))
    if linearity > max(ext['baseline_tolerance_counts'], .01 * abs(counts_per_v * step)):
        reasons.append('ADC response is not linear around the centre (%.1f counts)' % linearity)
    if dac_response > ext['analog_path_dac_tolerance']:
        reasons.append('The PZT DAC moved by %.0f counts during the CH2 step with zero multipliers: CH2 has an analog path; STOP' % dac_response)
    result['ok'] = not reasons
    result['reasons'] = reasons
    o.audit('locked_sweep_calibrated', calibration=clean(result))
    steady.sweep_calibration = result if result['ok'] else None
    steady.ch2_rehearsal = None          # v5.4: a new calibration invalidates the cached CH2 rehearsal
    return clean(result)


# ------------------------------------------------------------------------------------------------ session
class PreflightFailed(Exception):
    pass


LOCKED_SWEEP_RECORDS = (ARB_CH1, ARB_CH2)


def _leftover(channel_state):
    from ldcon.drivers.ecdl import arb_name_matches
    return channel_state.get('burst') == 'ON' or any(arb_name_matches(channel_state.get('arb_name'), n) for n in LOCKED_SWEEP_RECORDS)


def locked_sweep_leftover(o):
    """True when the BK holds this owner's own locked-sweep state (armed burst or an ldcsw1/ldcsw2 record)."""
    awg = o.hw().connections['current_awg']
    ch1 = o.device['connection']['resources']['current_awg']['channel']
    return _leftover(awg.read(ch1)) or _leftover(awg.read(2))


def cleanup_awgs(o):
    """v5.2-hostcheck: the SUPPORTED recovery of a locked sweep left on the BK (2026-10-22 21:03Z: both channels armed
    EXT with ldcsw1/ldcsw2; the owner had to do it by hand through the BK owner). Refused while the firmware
    modulation is active. DC FIRST on each channel (BSWV WVTP,DC exits burst on the BK, so no continuous ARB at any
    moment), burst OFF if still on, then CH2 -> the reviewed DC rest record and CH1 -> its DC offset and the reviewed
    dither ARB (fresh upload of the reviewed payload). Every step is read back."""
    from pathlib import Path
    from ldcon.drivers.ecdl import arb_name_matches, is_sdg
    hw = o.hw()
    cfg = hw.connections['piezo'].get('config')
    if cfg.get('multiplier_control') != 0 or cfg.get('multiplier_target') != 0 or cfg.get('software_modulation_enable') != 0:
        raise DeviceError('Firmware modulation is active; AWG cleanup refused')
    profile = o.device['control_settings'].get('steady_lock') or {}
    dither = profile.get('dither') or {}
    if not dither.get('evidence'):
        raise DeviceError('No reviewed dither evidence configured')
    record = json.loads(Path(dither['evidence']).read_text())['reserved']['current_awg/CH2']
    programming = dither.get('programming') or {}
    awg = hw.connections['current_awg']
    ch1 = o.device['connection']['resources']['current_awg']['channel']
    before = dict(ch1=awg.read(ch1), ch2=awg.read(2))
    changed = []
    o.audit('locked_sweep_cleanup_intent', before=before, target_ch2=record)
    try:
        # CH2 (ADC only) first
        c2 = before['ch2']
        if not same_channel(c2, record):
            if record.get('waveform') != 'DC' or not c2.get('enabled') or c2.get('load') != 'HZ':
                raise DeviceError('CH2 cleanup supports the reviewed DC rest state on an enabled HZ output only')
            awg.set_dc(2, record['value'])
            after = awg.read(2)
            if after.get('burst') == 'ON':
                awg.burst_off(2)
                after = awg.read(2)
            if not same_channel(after, record):
                raise DeviceError('CH2 cleanup readback mismatch: ' + json.dumps(after))
            changed.append('ch2')
        # CH1 (LDC current): DC at its own offset first, then the reviewed dither
        c1 = before['ch1']
        if _leftover(c1) and is_sdg(awg):
            # SDG: no waveform-type change on the LDC channel (ARB -> ARB, burst OFF last)
            if not programming.get('name'):
                raise DeviceError('No reviewed dither programming for the SDG CH1 cleanup')
            sdg_ch1_to_dither(awg, ch1, programming, finite(c1['value'], 'CH1 offset'))
            changed.append('ch1')
        elif _leftover(c1):
            offset = finite(c1['value'], 'CH1 offset')
            awg.set_dc(ch1, offset)
            back = awg.read(ch1)
            if back.get('burst') == 'ON':
                awg.burst_off(ch1)
                back = awg.read(ch1)
            if back.get('waveform') != 'DC' or back.get('burst') != 'OFF' or abs(finite(back['value'], 'CH1') - offset) > 1e-6:
                raise DeviceError('CH1 DC readback mismatch: ' + json.dumps(back))
            if programming.get('name'):
                amp = finite(programming['nominal_amplitude_vpp'], 'dither nominal amplitude')
                awg.ack(f'C{ch1}:BSWV WVTP,ARB,FRQ,{finite(programming["frequency_hz"], "dither frequency"):.12g},AMP,{amp:.12g},OFST,{offset:.8f}', 'CH1 carrier')
                previous = awg.read(ch1)
                programmed = awg.configure_dither(ch1, programming['frequency_hz'], finite(programming['requested_vpp'], 'dither vpp'), previous, reviewed_dither_policy(programming, ch1))
                if programmed['payload_sha256'] != programming['payload_sha256']:
                    raise DeviceError('Re-programmed dither differs from the reviewed programming')
                back = awg.read(ch1)
                if (not arb_name_matches(back.get('arb_name'), programming['name']) or back.get('burst') != 'OFF'
                        or abs(finite(back['value'], 'CH1') - offset) > 1e-6):
                    raise DeviceError('CH1 dither readback mismatch: ' + json.dumps(back))
            changed.append('ch1')
    except Exception as error:
        o.fault = 'Uncertain AWG cleanup: ' + str(error)
        o.persist_fault()
        raise DeviceError(o.fault) from error
    after = dict(ch1=awg.read(ch1), ch2=awg.read(2))
    o.audit('locked_sweep_cleanup_applied', changed=changed, after=after)
    return dict(changed=bool(changed), channels=changed, before=before, ch1=after['ch1'], ch2=after['ch2'])


def cleanup_ch2(steady):
    """locked_sweep_cleanup: return BK CH2 to the reviewed rest state (steady_lock.dither.evidence reserved record) when
    no locked sweep is in progress and the firmware multipliers are 0. For the 2026-10-22 08:11 state (CH2 left as an
    armed ARB burst idling at 1.0 V). A no-op when CH2 already matches."""
    o = steady.o
    from pathlib import Path
    evidence = (o.device['control_settings'].get('steady_lock') or {}).get('dither', {}).get('evidence')
    if not evidence:
        raise DeviceError('No reviewed dither evidence configured')
    record = json.loads(Path(evidence).read_text())['reserved']['current_awg/CH2']
    hw = o.hw()
    cfg = hw.connections['piezo'].get('config')
    if cfg.get('multiplier_control') != 0 or cfg.get('multiplier_target') != 0 or cfg.get('software_modulation_enable') != 0:
        raise DeviceError('Firmware modulation is active; CH2 cleanup refused')
    awg = hw.connections['current_awg']
    before = awg.read(2)
    if same_channel(before, record):
        return dict(changed=False, ch2=before)
    if record.get('waveform') != 'DC' or not before.get('enabled') or before.get('load') != 'HZ':
        raise DeviceError('CH2 cleanup supports the reviewed DC rest state on an enabled HZ output only')
    o.audit('locked_sweep_cleanup_intent', before=before, target=record)
    try:
        awg.set_dc(2, record['value'])
        after = awg.read(2)
        if after.get('burst') == 'ON':
            awg.burst_off(2)
            after = awg.read(2)
        if not same_channel(after, record):
            raise DeviceError('CH2 cleanup readback mismatch: ' + json.dumps(after))
    except Exception as error:
        o.fault = 'Uncertain CH2 cleanup: ' + str(error)
        o.persist_fault()
        raise DeviceError(o.fault) from error
    o.audit('locked_sweep_cleanup_applied', after=after)
    return dict(changed=True, before=before, ch2=after)


def restore_reviewed_dither(steady):
    """restore_reviewed_dither (2026-10-22 v3.1): put CH1 back to the REVIEWED dither without the commissioning Trial.
    Open, unarmed rig under the caller's lease, no recovery state. Re-uploads exactly the reviewed payload
    (steady_lock.dither.programming: name, frequency, requested_vpp; sha256 must match) on the present CH1 carrier
    (ARB, enabled, 2 mVpp, burst OFF) through OffsetAWG.configure_dither (the code path that produced the reviewed
    evidence), then verifies: name/frequency, every other CH1 field unchanged, outputs/TEC/open loop unchanged,
    reserved channels unchanged. A failure after the write latches 'Uncertain dither configuration: ...'
    (review_dither applies)."""
    from pathlib import Path
    from ldcon.drivers.ecdl import arb_name_matches
    from ldcon.core.records import atomic_json
    o = steady.o
    if steady.armed or o.recovery is not None:
        raise DeviceError('Reviewed dither restoration requires an open, unarmed rig outside current recovery')
    profile = o.device['control_settings'].get('steady_lock') or {}
    dither = profile.get('dither') or {}
    programming = dither.get('programming') or {}
    evidence = json.loads(Path(dither['evidence']).read_text())
    if evidence['programmed'].get('payload_sha256') != programming.get('payload_sha256') or evidence['programmed'].get('name') != programming.get('name'):
        raise DeviceError('Laser dither programming differs from the reviewed evidence')
    hw = o.hw()
    state = hw.snapshot()
    o.validate_state(state, o.policies())
    if not state['open_loop']:
        raise DeviceError('Reviewed dither restoration requires an open-loop servo')
    current = state['current']
    if (current.get('waveform') != 'ARB' or not current.get('enabled') or current.get('burst') != 'OFF'
            or abs(finite(current.get('amplitude_vpp'), 'CH1 amplitude') - programming['nominal_amplitude_vpp']) > 1e-9):
        raise DeviceError('CH1 must be an enabled ARB carrier at the reviewed nominal amplitude without burst: ' + json.dumps(current))
    from ldcon.drivers.ecdl import dither_payload
    channel = o.device['connection']['resources']['current_awg']['channel']
    metadata, _ = dither_payload(channel, programming['frequency_hz'], programming['requested_vpp'], current, reviewed_dither_policy(programming, channel))
    if metadata['payload_sha256'] != programming['payload_sha256'] or metadata['name'] != programming['name']:
        raise DeviceError('Re-computed payload differs from the reviewed programming (CH1 already on %s?)' % current.get('arb_name'))
    reserved = hw.reserved_readbacks()
    o.audit('reviewed_dither_restore_requested', before=state['current'], outputs=state['outputs'], reserved=reserved, programming=programming)
    try:
        programmed = hw.connections['current_awg'].configure_dither(channel, programming['frequency_hz'], programming['requested_vpp'], current, reviewed_dither_policy(programming, channel))
        after = hw.snapshot()
        o.validate_state(after, o.policies())
        if programmed['payload_sha256'] != programming['payload_sha256']:
            raise DeviceError('Uploaded payload differs from the reviewed programming')
        if (not arb_name_matches(after['current']['arb_name'], programming['name'])
                or abs(after['current']['frequency_hz'] - programming['frequency_hz']) > 1e-6):
            raise DeviceError('Selected dither readback mismatch: ' + json.dumps(after['current']))
        for key in current:
            if key not in ('arb_name', 'frequency_hz') and after['current'][key] != current[key]:
                raise DeviceError('Dither restoration changed CH1 ' + key)
        if after['outputs'] != state['outputs'] or after['temperature'] != state['temperature'] or not after['open_loop']:
            raise DeviceError('Dither restoration changed held rig state')
        if hw.reserved_readbacks() != reserved:
            raise DeviceError('Reserved channel state changed during dither restoration')
    except Exception as error:
        o.fault = 'Uncertain dither configuration: ' + str(error)
        o.persist_fault()
        raise DeviceError(o.fault) from error
    path = o.audit_path.parent / ('dither-restored-' + str(o.lease) + '.json')
    result = dict(settings=dither, programmed=programmed, before=state, after=after, reserved=reserved,
                  reviewed_evidence=dither['evidence'], evidence=str(path))
    atomic_json(path, clean(result))
    steady.programmed_dither = result
    o.audit('reviewed_dither_restored', programmed=programmed, after=after['current'], evidence=str(path))
    return clean(dict(restored=True, programmed=programmed, ch1=after['current'], outputs=after['outputs'], evidence=str(path)))


class LockedSweep:
    thread_enabled = True        # tests drive tick() directly

    def __init__(self, steady, plan, ready, cfg):
        self.steady, self.o = steady, steady.o
        self.plan, self.ready, self.cfg = plan, ready, cfg
        self.state = 'preparing'
        self.before = None
        self.programmed = None
        self.firmware_expected = None
        self.fw_touched = self.ch1_touched = self.ch2_touched = False
        self.records = []
        self.errors = []
        self.settle_rows = []
        self.restore_result = None
        self.measured_after = None
        self.abort_reason = None
        self.fired = None
        self.fired_clock = None
        self.playback_started_unix_s = None
        self.playing_clock = None
        self.done_clock = None
        self.bad_error = self.bad_mode = 0
        # v5.7 (human 2026-10-23 ~12:10Z): an obviously degraded mode (optical.single_mode_max / maintenance_required) no longer
        # aborts the sweep; every degraded sample is recorded (row.degraded_by) and counted here for the status
        self.degradation = dict(policy='report (no abort since v5.7)', samples=0, playback_samples=0, longest_run=0,
                                first_unix_s=None, last_unix_s=None, last_level_index=None)
        self.prepared_clock = None
        self.last_heartbeat = None
        self.thread = None
        self.stop_event = threading.Event()
        # v5.4 continuous triangle (sweep API v1.2): one-period record, N-cycle MAN burst, TEC following on the
        # integer-period context DAC (period_context.PeriodAverager)
        self.continuous = plan.get('trigger') == 'continuous'
        self.cycles = None
        self.period_s = None
        self.stop_boundary = None
        self.stop_requested_clock = None
        self.context = None
        self.tec_proposal = None
        self.tec_moves = []
        self.tec_last_clock = None
        self.tec_context = dict(source='period_mean', periods=None, window_s=None, mean_dac=None, start_s=None, end_s=None,
                                skipped=0, reason='continuous playback not started' if self.continuous else 'not continuous')

    # ---- helpers -------------------------------------------------------------------------------
    @property
    def ch1(self):
        return self.o.device['connection']['resources']['current_awg']['channel']

    @property
    def ch2(self):
        return self.cfg['awg']['channel']

    def summary(self):
        return dict(state=self.state, request_id=self.plan['request_id'], plan_sha256=self.plan['plan_sha256'],
                    levels=len(self.plan['levels_hz']), trigger=self.plan['trigger'], record_count=len(self.records),
                    ldc_ff_v_per_hz=self.plan['ldc_ff_v_per_hz'], pzt_ff_gain_scale=self.plan['pzt_ff_gain_scale'],
                    fired=self.fired is not None, abort_reason=self.abort_reason,
                    kind='continuous' if self.continuous else 'shot')

    def refuse(self, reason):
        raise BrokerError('request', reason)

    # ---- prepare -------------------------------------------------------------------------------
    def prepare(self):
        s, o, cfg = self.steady, self.o, self.cfg
        plan, ext, awgc = self.plan, cfg['ext_mod'], cfg['awg']
        hw = o.hw()
        awg = hw.connections['current_awg']
        servo = hw.connections['piezo']
        policies = o.policies()
        state, obs = s.read_feedback()
        o.validate_state(state, policies)
        s.native(state, obs, s.p, locked=True)
        cal = getattr(s, 'sweep_calibration', None)
        if not cal or not cal.get('ok'):
            self.refuse('No valid CH2/ADC calibration in this owner process: run the calibration rung first')
        if o.clock() - cal['clock_s'] > ext['calibration_max_age_s']:
            self.refuse('CH2/ADC calibration is older than %.0f s: run the calibration rung again' % ext['calibration_max_age_s'])
        if s.current_target != s.target:
            self.refuse('A lock target ramp is in progress')
        cfgw = state['servo_config']
        status = state['servo_status']
        raw_i = status.get('pid_integral')
        bound = max(abs(cfgw.get('pid_integral_min', 0)), abs(cfgw.get('pid_integral_max', 0)))
        fraction = abs(raw_i) / bound if type(raw_i) in (int, float) and bound > 0 else None
        if fraction is None or fraction > cfg['limits']['max_integral_fraction']:
            self.refuse('PI integral fraction %s exceeds %.2f: relock before the sweep' % (fraction, cfg['limits']['max_integral_fraction']))
        missing = [k for k in FW_GROUP + FW_SINGLE + ('target', 'control_offset') if k not in cfgw]
        if missing:
            self.refuse('Firmware configuration keys unavailable: ' + ', '.join(missing))
        if 'modulation_atomic' not in (status.get('capabilities') or []):
            self.refuse('Firmware does not report modulation_atomic')
        # ---- snapshot (restoration reference) ----------------------------------------------
        current = dict(state['current'])
        ch2_state = awg.read(self.ch2)
        dither = s.p.get('dither', {}).get('programming') or {}
        reviewed_dither_policy(dither, self.ch1)  # prove restorability before any sweep write
        from ldcon.drivers.ecdl import arb_name_matches
        if (current.get('waveform') != 'ARB' or not dither.get('name') or not arb_name_matches(current.get('arb_name'), dither['name'])
                or current.get('burst') != 'OFF' or not current.get('enabled')):
            self.refuse('CH1 must play the verified dither ARB (%s) before a locked sweep: %s' % (dither.get('name'), json.dumps(current)))
        if s.dither_paused() or s.dither_hardware_paused():
            # restore_ch1 re-uploads the reviewed sine; a sweep under a pause would silently resume it (2026-10-27).
            self.refuse('The dither is paused (steady_lock.dither.paused); resume it before a locked sweep')
        if (ch2_state.get('waveform') != 'DC' or not ch2_state.get('enabled') or ch2_state.get('load') != 'HZ'
                or ch2_state.get('burst') != 'OFF' or ch2_state.get('modulation') != 'OFF' or ch2_state.get('sweep') != 'OFF'):
            self.refuse('CH2 must be an enabled HZ DC level: ' + json.dumps(ch2_state))
        centre = finite(ch2_state['value'], 'CH2 level')
        if abs(centre - cal['centre_v']) > 1e-6:
            self.refuse('CH2 level differs from the calibrated centre')
        limits = servo.get('dac/limits')
        self.before = dict(current=current, ch2=ch2_state, temperature=dict(state['temperature']),
                           servo_config={k: cfgw[k] for k in FW_GROUP + FW_SINGLE + ('target', 'control_offset', 'pid_multiplier')},
                           outputs=dict(state['outputs']), tec_ff_paused=s.tec_ff_paused, reserved=hw.reserved_readbacks(),
                           dac_limits=limits, integral_fraction=fraction)
        # ---- record and envelopes (no write yet) -----------------------------------------------
        rec = compile_record(plan, cfg, policies['current'])
        values = rec['values']
        hz_per_v = ext['hz_per_v']
        cpv = cal['counts_per_v']
        volts2 = centre + values / hz_per_v
        lo_v, hi_v = awgc['ch2_bounds_v']
        if float(volts2.min()) < lo_v or float(volts2.max()) > hi_v:
            self.refuse('Staircase leaves the CH2/ADC linear range %s V' % awgc['ch2_bounds_v'])
        codes2, amp2 = channel_codes(values / hz_per_v, awgc['amp_min_vpp'])
        if amp2 > awgc['max_vpp'] + 1e-12:
            self.refuse('CH2 amplitude %.3f Vpp exceeds the approved %.3f Vpp' % (amp2, awgc['max_vpp']))
        ff = plan['ldc_ff_v_per_hz']
        values1 = values * ff
        codes1, amp1 = channel_codes(values1, max(awgc['amp_min_vpp'], current['amplitude_vpp']))
        offset1 = finite(current['value'], 'CH1 offset')
        cp = policies['current']
        crel = o.device['absolute_conditions']['axes']['current']['activation_range']
        clow, chigh = max(cp.low, o.activation['current'] + crel[0]), min(cp.high, o.activation['current'] + crel[1])
        if offset1 - amp1 / 2 < clow - 1e-12 or offset1 + amp1 / 2 > chigh + 1e-12:
            self.refuse('LDC feed-forward record leaves the current window [%.4f, %.4f] V' % (clow, chigh))
        if amp1 > awgc['ch1_range_amp_max_vpp'] + 1e-12 or not awgc['ch1_range_offset_v'][0] <= offset1 <= awgc['ch1_range_offset_v'][1]:
            # BK: its lowest output range (relay boundaries, ECDLcontrol sweep.py). SDG: the same envelope until the SDG's
            # own range boundaries are measured on the bench (swap procedure).
            self.refuse('CH1 would leave the approved current-AWG output range envelope (AMP <= %.3f V, OFST %s V)' % (awgc['ch1_range_amp_max_vpp'], awgc['ch1_range_offset_v']))
        step1, window1 = ch1_envelope(values1, rec['rate_hz'], cp)
        if step1 > cp.max_step + 1e-12 or window1 > cp.max_step + 1e-12:
            self.refuse('LDC feed-forward record exceeds max_step %.4f V (largest step %.5f V, largest change per %.2f s %.5f V)'
                        % (cp.max_step, step1, cp.update_interval_s, window1))
        baseline_pred = cal['intercept_counts'] + cpv * centre
        counts = baseline_pred + values / hz_per_v * cpv
        margin = ext['adc_margin_counts']
        mod_valid = [int(math.floor(min(float(counts.min()), baseline_pred) - margin)), int(math.ceil(max(float(counts.max()), baseline_pred) + margin))]
        if mod_valid[0] < 0 or mod_valid[1] > 65535:
            self.refuse('ADC validity window leaves the converter range')
        target_nm = finite(cfgw['target'], 'firmware target', positive=True)
        f0 = C_NM / target_nm
        hz_per_count = hz_per_v / cpv
        mt = -(target_nm ** 2 / C_NM) * hz_per_count
        slope = finite(s.p.get('wavelength_nm_per_count'), 'steady_lock.wavelength_nm_per_count')
        if slope == 0:
            self.refuse('wavelength_nm_per_count must be nonzero')
        mc = mt / slope * plan['pzt_ff_gain_scale']
        dac0 = state['outputs']['piezo']
        pz = policies['piezo']
        prel = o.device['absolute_conditions']['axes']['piezo']['activation_range']
        plow, phigh = max(pz.low, o.activation['piezo'] + prel[0]), min(pz.high, o.activation['piezo'] + prel[1])
        if limits:
            plow, phigh = max(plow, limits.get('min_code', 0)), min(phigh, limits.get('max_code', 65535))
        excursion = [dac0 + mc * float(values.min()) / hz_per_count, dac0 + mc * float(values.max()) / hz_per_count]
        if min(excursion) < plow or max(excursion) > phigh:
            self.refuse('PZT feed-forward excursion %s leaves the window [%.0f, %.0f]' % ([round(x) for x in excursion], plow, phigh))
        margin_hz = cfg['firmware']['system_margin_hz']
        f_low, f_high = f0 + float(values.min()) - margin_hz, f0 + float(values.max()) + margin_hz
        system = [min(cfgw['system_min'], C_NM / f_high), max(cfgw['system_max'], C_NM / f_low)]
        rec_public = {k: v for k, v in rec.items() if k != 'values'}
        self.programmed = dict(record=rec_public, centre_v=centre, ch1_offset_v=offset1, ch1_amp_vpp=amp1, ch2_amp_vpp=amp2,
                               ch1_largest_step_v=step1, ch1_largest_change_per_interval_v=window1, f0_hz=f0,
                               hz_per_count=hz_per_count, multiplier_target=mt, multiplier_control=mc,
                               baseline_predicted=baseline_pred, mod_valid=mod_valid, system_window_nm=system,
                               pzt_excursion=excursion, pzt_window=[plow, phigh], calibration=dict(counts_per_v=cpv, intercept=cal['intercept_counts'],
                               unix_s=cal['unix_s']), levels=len(plan['levels_hz']))
        self.levels = plan['levels_hz']
        self.duration_s = rec['period_s']
        self.levels_start_s = rec['levels_start_s']
        self.threshold_counts = max(3 * ext['baseline_tolerance_counts'], .25 * min([abs(v) for v in self.levels if v] or [1e9]) / hz_per_count)
        source = 'EXT' if plan['trigger'] == 'external' else 'MAN'
        if self.continuous:
            # The AWG repeats the one-period record on its own clock as an N-cycle MAN burst: the AWG itself ends the
            # run after N periods (N = ceil(lifetime/period)); the owner also stops at its lifetime, at a period boundary.
            self.period_s = rec['period_s']
            self.cycles = int(math.ceil(plan['lifetime_s'] / self.period_s - 1e-9))
            if self.cycles > MAX_BURST_CYCLES:
                self.refuse('Continuous run needs %d burst cycles (> %d): raise the rate or shorten lifetime_s' % (self.cycles, MAX_BURST_CYCLES))
            if not hasattr(awg, 'set_burst_cycles'):
                self.refuse('This current-AWG driver cannot set and read back the burst cycle count (continuous refused)')
            self.programmed['continuous'] = dict(period_s=self.period_s, cycles=self.cycles, lifetime_s=plan['lifetime_s'],
                                                 aligned_run_s=self.cycles * self.period_s)
        from ldcon.drivers.ecdl import awg_profile, max_arb_points
        profile = awg_profile(awg)
        self.programmed['awg_profile'] = profile
        if rec['points'] > max_arb_points(profile):
            self.refuse('Record of %d points exceeds the %s ARB length %d' % (rec['points'], profile, max_arb_points(profile)))
        if is_sdg(awg):
            check_sdg_dds(awg, (self.ch1, self.ch2))
        o.audit('locked_sweep_write_intent', stage='prepare', plan=clean(plan), before=clean(self.before), programmed=clean(self.programmed))
        # ---- writes ----------------------------------------------------------------------------
        try:
            freq = rec['rate_hz'] / rec['points']
            # CH2 first (it only reaches the ESP32 ADC; the multipliers are still 0, so nothing here moves the laser):
            # 2 mV ARB at the centre, the staircase selected, burst armed (idle at the centre), full amplitude.
            self.ch2_touched = True
            sdg = is_sdg(awg)
            awg.upload_arb(self.ch2, ARB_CH2, codes2, freq, awgc['amp_min_vpp'], centre)
            awg.select_continuous_arb(self.ch2, ARB_CH2, freq, awgc['amp_min_vpp'], centre)
            awg.arm_burst(self.ch2, source)
            if sdg:
                # SDG rehearsal on CH2 (ADC only, multipliers 0): EXACTLY the sequence CH1 will use -- record uploaded
                # while the burst is armed, burst must still be armed; full amplitude by BSWV while armed, burst must
                # still be armed. Any other behaviour is a clean refusal before CH1 (the laser) is touched.
                self.sdg_arm_record(awg, self.ch2, ARB_CH2, codes2, freq, awgc['amp_min_vpp'], amp2, centre, source, rehearsal=True)
            else:
                awg.select_burst_arb(self.ch2, ARB_CH2, freq, amp2, centre)
                self.verify_armed(awg, self.ch2, ARB_CH2, freq, amp2, centre, source)
            # v5.2-hostcheck (2026-10-22 21:03Z sr422 EXT incident): the ARMED idle level is checked on the ADC NOW, with
            # only CH2 changed (it reaches the ESP32 ADC alone; the multipliers are 0) -- before CH1 (the LDC current
            # feed-forward, which acts on the laser) and before the firmware. The BK idled the EXT-armed record at
            # ~0.735 V instead of the 1.0 V centre; the old order measured this only after CH1 was armed (sr422 hopped
            # +111 GHz). A wrong idle level is now a clean refusal with CH2 restored DC-first and nothing else written.
            sleep = getattr(o, 'sleep', time.sleep)
            sleep(.2)
            raws = []
            for _ in range(5):
                raws.append(float(servo.get('status')['mod_raw'])); sleep(.02)
            baseline = sorted(raws)[2]
            self.programmed['armed_idle_counts'] = baseline
            self.programmed['burst_delay'] = awg.burst_readback(self.ch2).get('DLAY')     # recorded (the BK clamps DLAY on EXT)
            if sdg and ext['baseline_tolerance_counts'] < abs(baseline - baseline_pred) <= IDLE_CORRECTION_MAX_COUNTS:
                # v5.6 (2026-10-23 P9/P10, f72e1864 / a933d577): the SDG's armed idle carries a DC error that depends on
                # the CH2 amplitude range (measured tonight: 0.2 Vpp +1, 0.6 Vpp +21, 1.2 Vpp +31, 0.4 Vpp +82..84 ADC
                # counts; record shape and last sample irrelevant). It is an output-stage offset of the whole waveform,
                # so the channel OFFSET is corrected by -error (one BSWV write while armed, the same command as the
                # armed-amplitude write), the burst must still be armed, and the idle is measured again; it must then be
                # within the usual tolerance. A gross error (> IDLE_CORRECTION_MAX_COUNTS, e.g. the BK 0.735 V idle) is
                # still a clean refusal. CH2 reaches the ESP32 ADC only; the multipliers are still 0.
                error_v = (baseline - baseline_pred) / cpv
                corrected = centre - error_v
                if not (lo_v <= corrected - amp2 / 2 and corrected + amp2 / 2 <= hi_v):
                    raise PreflightFailed('CH2 idle correction %.4f V would leave the CH2/ADC linear range' % -error_v)
                o.audit('locked_sweep_write_intent', stage='ch2_idle_correction', idle_counts=baseline,
                        predicted_counts=baseline_pred, offset_v=corrected, correction_v=-error_v)
                awg.ack(f'C{self.ch2}:BSWV FRQ,{freq:.12g},AMP,{amp2:.12g},OFST,{corrected:.8f}', 'CH2 idle offset correction')
                try:
                    self.verify_armed(awg, self.ch2, ARB_CH2, freq, amp2, corrected, source)
                except DeviceError as error:
                    raise PreflightFailed('CH2 idle correction left the burst unarmed: %s' % error) from error
                sleep(.2)
                raws = []
                for _ in range(5):
                    raws.append(float(servo.get('status')['mod_raw'])); sleep(.02)
                corrected_idle = sorted(raws)[2]
                self.programmed['ch2_idle_correction'] = dict(idle_counts_before=baseline, idle_counts_after=corrected_idle,
                                                              offset_v=corrected, correction_v=-error_v)
                o.audit('locked_sweep_ch2_idle_corrected', **self.programmed['ch2_idle_correction'])
                baseline = corrected_idle
                self.programmed['armed_idle_counts'] = baseline
            if abs(baseline - baseline_pred) > ext['baseline_tolerance_counts']:
                raise PreflightFailed('armed CH2 idles at %.1f ADC counts (%.3f V), not the calibrated centre %.1f counts '
                                      '(%s trigger); the laser was not touched'
                                      % (baseline, (baseline - cal['intercept_counts']) / cpv, baseline_pred, source))
            if awgc['ch2_preflight'] and source == 'MAN':
                # 2026-10-22 R0: MTRIG was acknowledged but the ADC never moved. Prove trigger + record + ADC route on
                # CH2 alone before CH1 or the firmware is touched; a failure is a clean refusal after a verified CH2 restore.
                # v5.4 (human approval 2026-10-23 ~04:45Z): once per owner process per verified configuration. The cache
                # key covers the exact CH2 record and its playback settings, the AWG identity/profile, the settings
                # revision and the calibration; any CH2 restore failure, fault or new calibration clears it. The armed
                # idle check above still runs on every shot.
                key = rehearsal_key(codes2, freq, amp2, centre, source, hw.identities.get('current_awg'), profile,
                                    cfg['revision'], cal.get('unix_s'))
                cached = getattr(s, 'ch2_rehearsal', None)
                if cached and cached.get('key') == key and not o.fault:
                    self.programmed['ch2_preflight'] = dict(status='cached', key=key, cached_from_unix_s=cached['unix_s'])
                    o.audit('locked_sweep_preflight_cached', key=key, cached_from_unix_s=cached['unix_s'])
                else:
                    s.ch2_rehearsal = None
                    self.ch2_preflight(awg, servo, rec, values, baseline_pred, cpv, hz_per_v)
                    s.ch2_rehearsal = dict(key=key, unix_s=time.time(), result=getattr(self, 'preflight', None))
                    self.programmed['ch2_preflight'] = dict(status='ran', key=key, cached_from_unix_s=None)
            if self.continuous:
                try:
                    awg.set_burst_cycles(self.ch2, self.cycles)
                except DeviceError as error:            # read back (a definite state): clean refusal, CH2 restored
                    raise PreflightFailed('CH2 burst cycle count: ' + str(error)) from error
            # CH1: burst armed on the playing dither (idle at its start = the DC offset), FF ARB as the carrier.
            self.ch1_touched = True
            awg.arm_burst(self.ch1, source)
            if sdg:
                # SDG: never a waveform-type change on CH1; the record replaces the dither as the armed carrier.
                self.sdg_arm_record(awg, self.ch1, ARB_CH1, codes1, freq, current['amplitude_vpp'], amp1, offset1, source, rehearsal=False)
            else:
                awg.upload_arb(self.ch1, ARB_CH1, codes1, freq, current['amplitude_vpp'], offset1)
                awg.select_burst_arb(self.ch1, ARB_CH1, freq, amp1, offset1)
                self.verify_armed(awg, self.ch1, ARB_CH1, freq, amp1, offset1, source)
            if self.continuous:
                try:
                    awg.set_burst_cycles(self.ch1, self.cycles)
                except DeviceError as error:            # read back: clean refusal, CH1 back to the dither, CH2 restored
                    raise PreflightFailed('CH1 burst cycle count: ' + str(error)) from error
            self.programmed['baseline_counts'] = baseline        # measured above, before CH1 was armed
            # Firmware: acceptance window, time match, ADC window, then the atomic FF group.
            self.fw_touched = True
            single = dict(system_min=system[0], system_max=system[1], target_time_match=cfg['firmware']['target_time_match'],
                          target_delay_us=cfg['firmware']['target_delay_us'], mod_valid_min=mod_valid[0], mod_valid_max=mod_valid[1])
            o.audit('locked_sweep_system_window', before=[cfgw['system_min'], cfgw['system_max']], during=system,
                    plan_span_hz=[float(values.min()), float(values.max())], margin_hz=margin_hz)
            s.write(single, 'locked sweep acceptance window, time match and ADC window')
            group = dict(baseline_target=baseline, multiplier_target=mt, baseline_control=baseline, multiplier_control=mc)
            s.write(group, 'locked sweep target modulation and PZT feed-forward (one atomic message)')
            self.firmware_expected = dict(single, **group)
            self._full_data_log_apply(servo)
            self._adc_log_apply(servo, cpv, hz_per_v, raws, cal)   # telemetry only, never raises (2026-10-23)
        except PreflightFailed as failure:
            s.ch2_rehearsal = None
            # Definite state: only CH2 was changed and it is idle/armed (SDG: or CH1 was found not armed after the record
            # upload and is returned to the dither ARB -> ARB). Return CH2 to its snapshot (DC first); refuse cleanly.
            try:
                if self.ch1_touched:
                    self.restore_ch1(awg, hw)
                self.restore_ch2(awg)
            except Exception as error:
                o.fault = 'Uncertain CH2 restoration after a failed preflight: ' + str(error)
                self.state = 'review_required'
                o.persist_fault()
                raise DeviceError(o.fault) from error
            o.audit('locked_sweep_preflight_failed', reason=str(failure))
            self.refuse('CH2 preflight failed (CH2 restored, nothing else written): ' + str(failure))
        except Exception as error:
            o.fault = o.fault or ('Uncertain locked sweep programming: ' + str(error))
            self.state = 'review_required'
            o.audit('locked_sweep_prepare_failed', error=str(error))
            o.persist_fault()
            raise DeviceError(o.fault) from error
        s.tec_ff_paused = True
        if s.planner is not None and hasattr(s.planner, 'rebase_ff'):
            s.planner.rebase_ff(state['outputs'])
        self.state = 'armed'
        self.prepared_clock = self.last_heartbeat = o.clock()
        self.prepared_unix_s = time.time()
        o.audit('locked_sweep_prepared', programmed=clean(self.programmed), firmware=clean(self.firmware_expected))
        if self.thread_enabled:
            self.thread = threading.Thread(target=self._loop, name='locked-sweep', daemon=True)
            self.thread.start()
        return clean(dict(status='armed', programmed=self.programmed, before=self.before, ready_criteria=self.ready,
                          plan_sha256=plan['plan_sha256'], request_id=plan['request_id'], trigger_source=source,
                          kind='continuous' if self.continuous else 'shot', period_s=self.period_s, cycles=self.cycles))

    # ---- temporary ADC log threshold (2026-10-23; telemetry only: not in firmware_expected, never a fault) ----
    def _adc_log_apply(self, servo, cpv, hz_per_v, raws, cal):
        from ldcon.owners.adc_log_threshold import apply, public, sigma_counts
        try:
            sigma = sigma_counts(raws, spreads=[p.get('mod_raw_spread') for p in (cal.get('points') or [])])
            record = apply(adc_log_journal(self.o), write=servo.set, read_config=lambda: servo.get('config'),
                           read_status=lambda: servo.get('status'), levels_hz=self.plan['levels_hz'],
                           counts_per_hz=cpv / hz_per_v, sigma=sigma,
                           context=dict(request_id=self.plan['request_id'], plan_digest=self.plan['plan_sha256'],
                                        route='rig_locked_sweep'))
        except Exception as error:      # noqa: BLE001
            record = dict(state='failed', error=str(error)[:300])
        self.programmed['adc_log_threshold'] = public(record)
        self.o.audit('locked_sweep_adc_log_threshold', record=public(record))

    def _adc_log_restore(self, reason):
        from ldcon.owners.adc_log_threshold import public, restore
        try:
            servo = self.o.hardware.connections['piezo']
            record = restore(adc_log_journal(self.o), write=servo.set, read_config=lambda: servo.get('config'),
                             read_status=lambda: servo.get('status'), reason=reason)
        except Exception as error:      # noqa: BLE001
            record = dict(state='restore_failed', error=str(error)[:300])
        if self.programmed is not None and 'adc_log_threshold' in self.programmed:
            self.programmed['adc_log_threshold'] = public(record)
            self.o.audit('locked_sweep_adc_log_threshold_restored', record=public(record))

    def _full_data_log_apply(self, servo):
        if self.plan.get('full_data_log_ms') is None:
            return
        from ldcon.owners.full_data_log import apply, public
        try:
            record = apply(full_data_log_journal(self.o), write=servo.set, read_config=lambda: servo.get('config'),
                           read_status=lambda: servo.get('status'), requested_ms=self.plan['full_data_log_ms'],
                           faulted=bool(self.o.fault),
                           context=dict(request_id=self.plan['request_id'], plan_digest=self.plan['plan_sha256'],
                                        route='rig_locked_sweep'))
        except Exception as error:      # noqa: BLE001
            record = dict(state='failed', error=str(error)[:300])
        self.programmed['full_data_log'] = public(record)
        self.o.audit('locked_sweep_full_data_log', record=public(record))


    def _full_data_log_restore(self, reason):
        if self.o.fault:
            return                      # never written in fault: the next arm or the owner start restores it
        from ldcon.owners.full_data_log import public, restore
        try:
            servo = self.o.hardware.connections['piezo']
            journal = full_data_log_journal(self.o)
            if not journal.needs_restore():
                return
            record = restore(journal, write=servo.set, read_config=lambda: servo.get('config'),
                             read_status=lambda: servo.get('status'), reason=reason)
        except Exception as error:      # noqa: BLE001
            record = dict(state='restore_failed', error=str(error)[:300])
        if self.programmed is not None:
            self.programmed['full_data_log'] = public(record)
        self.o.audit('locked_sweep_full_data_log_restored', record=public(record))


    def ch2_preflight(self, awg, servo, rec, values, baseline_pred, cpv, hz_per_v):
        o = self.o
        sleep = getattr(o, 'sleep', time.sleep)
        expected = float(max(abs(values.max()), abs(values.min()))) / hz_per_v * cpv
        tolerance = self.cfg['ext_mod']['baseline_tolerance_counts']
        o.audit('locked_sweep_write_intent', stage='ch2_preflight', expected_counts=expected, period_s=rec['period_s'])
        awg.fire_one(self.ch2)
        start = o.clock()
        raws = []
        while o.clock() - start < rec['period_s'] + 1.:
            raws.append(float(servo.get('status')['mod_raw']))
            sleep(.05)
        deviation = max(abs(r - baseline_pred) for r in raws)
        result = dict(expected_counts=expected, max_deviation_counts=deviation, final_counts=raws[-1], samples=len(raws))
        self.preflight = result
        o.audit('locked_sweep_preflight', **result)
        if deviation < .7 * expected:
            raise PreflightFailed('ADC moved %.0f counts of the expected %.0f after C%d:BTWV MTRIG' % (deviation, expected, self.ch2))
        if abs(raws[-1] - baseline_pred) > tolerance:
            raise PreflightFailed('ADC did not return to the centre after the record (%.0f vs %.0f)' % (raws[-1], baseline_pred))
        burst = awg.burst_readback(self.ch2)
        if burst.get('STATE') != 'ON' or burst.get('TRSR') != 'MAN':
            raise PreflightFailed('CH2 burst is no longer armed after the preflight: %s' % json.dumps(burst))

    def sdg_arm_record(self, awg, channel, name, codes, freq, small_amp, amp, offset, source, rehearsal):
        """SDG2000X: with the burst ALREADY armed on an ARB carrier, upload the record (the SDG selects it at once, at
        the header's small amplitude) and require the burst still armed; then the full amplitude by BSWV (does not exit
        burst on the SDG) and the full armed verification. On CH2 (rehearsal) any deviation is PreflightFailed. On CH1
        a burst that left after the upload is PreflightFailed too (the record then plays at the 2 mVpp carrier only, as
        bounded as the dither; it is replaced by the dither ARB -> ARB in the refusal path)."""
        burst = awg.burst_readback(channel)
        if burst.get('STATE') != 'ON' or burst.get('TRSR') != source:
            raise PreflightFailed('SDG CH%d burst did not arm on the ARB carrier: %s' % (channel, json.dumps(burst)))
        awg.upload_arb(channel, name, codes, freq, small_amp, offset)
        burst = awg.burst_readback(channel)
        if burst.get('STATE') != 'ON' or burst.get('TRSR') != source:
            raise PreflightFailed('SDG CH%d left burst after the record upload (%s); %s'
                                  % (channel, json.dumps(burst), 'nothing else written' if rehearsal else 'CH1 returned to the dither'))
        if rehearsal:
            try:
                self.verify_armed(awg, channel, name, freq, small_amp, offset, source)
            except DeviceError as error:
                raise PreflightFailed('SDG CH%d upload while armed: %s' % (channel, error)) from error
        awg.ack(f'C{channel}:BSWV FRQ,{freq:.12g},AMP,{amp:.12g},OFST,{offset:.8f}', 'Armed record amplitude')
        if rehearsal:
            try:
                self.verify_armed(awg, channel, name, freq, amp, offset, source)
            except DeviceError as error:
                raise PreflightFailed('SDG CH%d amplitude write while armed: %s' % (channel, error)) from error
        else:
            self.verify_armed(awg, channel, name, freq, amp, offset, source)

    def restore_ch2(self, awg):
        c2 = self.before['ch2']
        awg.set_dc(self.ch2, c2['value'])             # BSWV leaves burst on the BK; no continuous playback in between
        back = awg.read(self.ch2)
        if back.get('burst') == 'ON':
            awg.burst_off(self.ch2)
            back = awg.read(self.ch2)
        if not same_channel(back, c2):
            raise DeviceError('CH2 restoration readback mismatch: ' + json.dumps(back))

    def restore_ch1(self, awg, hw):
        """CH1 back to the reviewed dither through the proven configure_dither path (fresh WVDT upload of the dither
        payload, ARWV, BSWV FRQ). 2026-10-22 R0: an ARWV of the existing name alone did not take on the BK."""
        s = self.steady
        c = self.before['current']
        programming = s.p['dither']['programming']
        if is_sdg(awg):
            # SDG: ARB -> ARB only (no ~16 ms 0 V dropout on the LDC input); burst OFF last.
            programmed, back = sdg_ch1_to_dither(awg, self.ch1, programming, finite(c['value'], 'CH1 offset'))
            if not same_channel(back, c):
                raise DeviceError('CH1 dither restoration readback mismatch: ' + json.dumps(back))
            return programmed or dict(name=programming['name'], payload_sha256=programming['payload_sha256'], reused=True)
        awg.set_dc(self.ch1, c['value'])                                   # leave burst at the DC offset
        awg.ack(f'C{self.ch1}:BSWV WVTP,ARB,FRQ,{c["frequency_hz"]:.12g},AMP,{c["amplitude_vpp"]:.12g},OFST,{c["value"]:.8f}', 'CH1 carrier')
        previous = awg.read(self.ch1)
        # The reviewed upload used requested_vpp (5.0e-4 V); effective_amplitude_peak*2 would differ by one LSB.
        programmed = awg.configure_dither(self.ch1, finite(programming['frequency_hz'], 'dither frequency'),
                                          finite(programming['requested_vpp'], 'dither vpp'), previous, reviewed_dither_policy(programming, self.ch1))
        if programmed['name'] != programming['name'] or programmed['payload_sha256'] != programming['payload_sha256']:
            raise DeviceError('Re-programmed dither differs from the reviewed programming: ' + json.dumps(programmed))
        back = awg.read(self.ch1)
        if not same_channel(back, c):
            raise DeviceError('CH1 dither restoration readback mismatch: ' + json.dumps(back))
        return programmed

    def verify_armed(self, awg, channel, name, freq, amp, offset, source):
        from ldcon.drivers.ecdl import arb_name_matches
        from ldcon.drivers.siglent import number
        burst = awg.burst_readback(channel)
        wave, selected = awg.wave_readback(channel)
        ok = (burst.get('STATE') == 'ON' and burst.get('TRSR') == source and burst.get('GATE_NCYC', 'NCYC') == 'NCYC'
              and arb_name_matches(selected, name) and wave.get('WVTP') == 'ARB')
        for key, wanted in (('FRQ', freq), ('AMP', amp), ('OFST', offset)):
            ok = ok and key in wave and math.isclose(number(wave[key]), wanted, rel_tol=2e-5, abs_tol=1e-5)
        if not ok:
            raise DeviceError('CH%d is not armed as programmed: burst %s wave %s arb %s' % (channel, json.dumps(burst), json.dumps(wave), selected))

    # ---- sampling ------------------------------------------------------------------------------
    def sample(self):
        o, s = self.o, self.steady
        hw = o.hw()
        status = hw.connections['piezo'].get('status')
        fw = self.firmware_expected or {}
        cfgb = self.before['servo_config']
        target_nm = cfgb['target']
        raw = status.get('mod_raw')
        mt = fw.get('multiplier_target', 0.)
        bt = fw.get('baseline_target', 0.)
        t_eff = target_nm + mt * (float(raw) - bt) if type(raw) in (int, float) else None
        f0 = C_NM / target_nm
        f_eff = C_NM / t_eff if t_eff else None
        sv, pe = status.get('system_value'), status.get('pid_error')
        fw_error = (C_NM / sv - C_NM / (sv - pe)) if type(sv) in (int, float) and type(pe) in (int, float) and sv > 0 and sv - pe > 0 else None
        row = dict(unix_s=time.time(), owner_s=o.clock(), phase=self.state, mod_raw=raw, dac=status.get('dac_val'),
                   commanded_offset_hz=(f_eff - f0) if f_eff else None, effective_target_hz=f_eff, fw_error_hz=fw_error,
                   pid_integral=status.get('pid_integral'), udp_seq=status.get('last_udp_seq'),
                   udp_out_of_range=status.get('udp_out_of_range'), mod_invalid=status.get('mod_invalid'))
        row['level_index'] = self.level_index(row['commanded_offset_hz'])
        try:
            obs = o.observation()
            f = obs['frequency']['frequency_hz']
            causes = self.degradation_causes(obs)
            row.update(frequency_hz=f, valid=True, single_mode=bool(obs['single_mode']), maintenance_required=bool(obs.get('maintenance_required')),
                       degraded=bool(causes), **({'degraded_by': causes} if causes else {}),
                       error_hz=(f - f_eff) if f_eff else None, acquired_unix_s=obs['frequency'].get('acquired_unix_s'), metrics=obs.get('metrics'))
        except MeasurementUnavailable as error:
            row.update(frequency_hz=None, valid=False, single_mode=None, error_hz=None, reason=str(error)[:200])
        return row

    def degraded(self, obs):
        """Report canonical optical quality violations without aborting playback.

        Entry and physical safety checks remain separate from this quality index.
        """
        return bool(self.degradation_causes(obs))

    def degradation_causes(self, obs):
        """Why a sample is obviously degraded: [{metric, value, limit}] for each metric above optical.single_mode_max, or one
        {metric: None, reason} when the classifier itself flags maintenance (insensitive profile / unclassified)."""
        limits = self.o.device['control_settings'].get('optical', {}).get('single_mode_max') or {}
        metrics = obs.get('metrics') or {}
        causes = [dict(metric=k, value=metrics[k], limit=v) for k, v in sorted(limits.items())
                  if type(metrics.get(k)) in (int, float) and metrics[k] > v]
        if obs.get('maintenance_required') and not causes:
            causes.append(dict(metric=None, reason=str(obs.get('classification_error')
                                                       or 'maintenance_required (classifier insensitive or unclassified)')[:200]))
        return causes

    def level_index(self, offset_hz):
        if offset_hz is None:
            return None
        levels = self.levels
        index = min(range(len(levels)), key=lambda i: abs(levels[i] - offset_hz))
        steps = [abs(a - b) for a, b in zip(levels, levels[1:]) if a != b]
        half = (min(steps) / 2) if steps else max(abs(levels[0]) / 2, 1e6)
        return index if abs(levels[index] - offset_hz) <= half else None

    def measured(self, rows, centre_hz=None):
        n = self.ready['samples']
        valid = [r for r in rows if r.get('valid')][-n:]
        if len(valid) < n:
            return dict(ready=False, reason='need %d valid samples, have %d' % (n, len(valid)), samples=len(valid))
        errs = [abs((r['frequency_hz'] - centre_hz) if centre_hz is not None else (r['error_hz'] if r['error_hz'] is not None else math.inf)) for r in valid]
        freqs = [r['frequency_hz'] for r in valid]
        result = dict(samples=len(valid), max_abs_error_hz=max(errs), span_hz=max(freqs) - min(freqs),
                      single_mode=all(r['single_mode'] for r in valid), not_degraded=not any(r.get('degraded') for r in valid),
                      tolerance_hz=self.ready['tolerance_hz'], stability_hz=self.ready['stability_hz'])
        result['ready'] = bool(result['max_abs_error_hz'] <= self.ready['tolerance_hz'] and result['span_hz'] <= self.ready['stability_hz']
                               and result['not_degraded'])
        result['reason'] = None if result['ready'] else 'error, stability or an obviously degraded mode outside the criteria'
        return result

    def _loop(self):
        while not self.stop_event.wait(.1):
            try:
                with self.o.lock:
                    if self.state in FINISHED or self.state == 'review_required':
                        return
                    self.tick()
            except Exception as error:      # noqa: BLE001 - recorded; a write failure has already latched the fault
                self.errors.append(dict(unix_s=time.time(), error=str(error)[:300]))
                del self.errors[:-200]

    def tick(self):
        """One supervision step (owner lock held): sample, detect playback/done, enforce aborts, settle."""
        o = self.o
        if self.state in FINISHED or self.state == 'review_required':
            return
        if self.state == 'settling':
            return self._settle_tick()
        now = o.clock()
        try:
            row = self.sample()
        except (MeasurementUnavailable, OSError, KeyError, TypeError, ValueError) as error:
            self.errors.append(dict(unix_s=time.time(), error='sample: ' + str(error)[:300]))
            row = None
        if row is not None:
            self.records.append(clean(row))
            del self.records[:-30000]
            if now - getattr(self, 'fw_checked', -1e9) >= 1.:       # one /config read per second (ESP32 HTTP load)
                self.fw_checked = now
                self._check_firmware()
            self._progress(row, now)
            self._abort_checks(row, now)
            if self.continuous and self.state == 'playing':
                self._context(row, now)
        if self.continuous and self.state in ('fired', 'playing', 'restore_pending'):
            return self._continuous_tick(now)
        if self.state == 'restore_pending' and (self.state_done() or now > self.restore_deadline):
            self.restore_now(self.abort_reason or 'requested')
        elif now - self.last_heartbeat > self.cfg['limits']['heartbeat_timeout_s'] and self.state in ('armed', 'done'):
            self.abort('session heartbeat lost', immediate=True)
        elif now - self.prepared_clock > self.plan['lifetime_s'] and self.state in ('armed', 'done'):
            self.abort('sweep lifetime expired', immediate=True)

    def state_done(self):
        return self.state == 'done' or self.done_clock is not None

    def _check_firmware(self):
        cfg = self.o.hw().connections['piezo'].get('config')
        for key, value in (self.firmware_expected or {}).items():
            if not math.isclose(float(cfg.get(key, math.nan)), float(value), rel_tol=1e-8, abs_tol=1e-9):
                self.abort('foreign firmware change: ' + key, immediate=True)
                return
        if cfg.get('pid_multiplier') != 1:
            self.abort('native PI is no longer enabled', immediate=True)

    def _progress(self, row, now):
        raw, base = row.get('mod_raw'), self.programmed.get('baseline_counts')
        away = type(raw) in (int, float) and base is not None and abs(raw - base) > self.threshold_counts
        if self.state == 'fired' or (self.state == 'armed' and self.plan['trigger'] == 'external'):
            if away:
                self.state = 'playing'; self.playing_clock = now; self.playback_started_unix_s = row['unix_s']
                self.o.audit('locked_sweep_playing', mod_raw=raw, baseline=base)
            elif self.fired_clock is not None and now - self.fired_clock > self.levels_start_s + 1.5:
                self.abort('playback never left the ADC centre after the trigger (check the AWG route)', immediate=True)
        elif self.continuous:
            return                       # a continuous record never reports 'done' (see _continuous_tick)
        elif self.state == 'playing' or (self.state == 'restore_pending' and self.playing_clock is not None and self.done_clock is None):
            started = self.fired_clock if self.fired_clock is not None else self.playing_clock
            if not away and now - started >= self.duration_s:
                self.done_clock = now
                if self.state == 'playing':
                    self.state = 'done'
                self.o.audit('locked_sweep_done', elapsed_s=now - started)

    def _abort_checks(self, row, now):
        limits = self.cfg['limits']
        error = row.get('fw_error_hz') if row.get('fw_error_hz') is not None else row.get('error_hz')
        self.bad_error = self.bad_error + 1 if error is not None and abs(error) > limits['abort_error_hz'] else 0
        self.bad_mode = self.bad_mode + 1 if row.get('degraded') is True else 0
        if row.get('degraded') is True:
            self._note_degraded(row)
        if self.state not in ACTIVE or self.state == 'restoring':
            return
        if self.bad_error >= limits['abort_samples']:
            self.abort('|error| > %.0f Hz for %d samples' % (limits['abort_error_hz'], self.bad_error), immediate=True)
        # v5.7 (human 2026-10-23 ~12:10Z): NO abort for 'mode obviously degraded' any more. The samples are reported
        # (rows carry degraded/degraded_by/metrics/level_index/commanded_offset_hz; status.degradation counts them); the
        # session/proxy turns them into outcome.tracking (state degraded, reason mode_degraded).

    def _note_degraded(self, row):
        d = self.degradation
        first = d['samples'] == 0
        d['samples'] += 1
        if row.get('phase') in ('fired', 'playing', 'restore_pending'):     # restore_pending: the record still plays
            d['playback_samples'] += 1
        d['longest_run'] = max(d['longest_run'], self.bad_mode)
        d['first_unix_s'] = d['first_unix_s'] or row.get('unix_s')
        d['last_unix_s'], d['last_level_index'] = row.get('unix_s'), row.get('level_index')
        if first:
            self.o.audit('locked_sweep_mode_degraded', state=self.state, level_index=row.get('level_index'),
                         commanded_offset_hz=row.get('commanded_offset_hz'), degraded_by=row.get('degraded_by'))

    def abort(self, reason, *, immediate):
        if self.state in FINISHED or self.state in ('review_required', 'restoring', 'settling'):
            return
        self.abort_reason = self.abort_reason or reason
        self.o.audit('locked_sweep_abort', reason=reason, immediate=immediate, state=self.state)
        if immediate or self.state in ('armed', 'done'):
            self.restore_now(reason)
        else:
            self.request_restore(reason)

    # ---- software trigger ------------------------------------------------------------------------
    def trigger(self):
        if self.fired is not None:
            return dict(self.fired, repeated=True)
        if self.plan['trigger'] not in ('software', 'continuous'):
            raise BrokerError('request', 'This sweep is not software-triggered')
        if self.state != 'armed':
            raise BrokerError('busy', 'Sweep is not armed (state=%s)' % self.state)
        verdict = self.measured(self.records)
        if not verdict['ready']:
            raise BrokerError('busy', 'Sweep is not READY: ' + str(verdict.get('reason')))
        o = self.o
        awg = o.hw().connections['current_awg']
        o.audit('locked_sweep_write_intent', stage='fire', channels=[self.ch1, self.ch2])
        fired_unix_s = time.time()
        self.fired_clock = o.clock()
        try:
            ack = awg.fire([self.ch1, self.ch2])
        except Exception as error:
            # MTRIG may or may not have executed: never repeated; restoration needs review.
            o.fault = 'Uncertain locked sweep trigger: ' + str(error)
            self.state = 'review_required'
            o.persist_fault()
            raise DeviceError(o.fault) from error
        self.fired = dict(ok=True, source='MAN', command=ack['command'], readback=ack['readback'],
                          fired_unix_s=fired_unix_s, fire_ack_unix_s=time.time())
        self.state = 'fired'
        o.audit('locked_sweep_fired', **clean(self.fired))
        return dict(self.fired)

    # ---- restore ---------------------------------------------------------------------------------
    def request_restore(self, reason):
        """Stop: wait for a playing record to end (it always ends at the centre), then restore."""
        if self.state in FINISHED or self.state in ('review_required', 'restoring', 'settling'):
            return
        if self.continuous and self.state in ('fired', 'playing'):
            # v5.4: the continuous record is at the centre only at a period boundary (phase 0 = the MAN trigger):
            # restore exactly at the next boundary (bounded: boundary + 2 s)
            now = self.o.clock()
            self.stop_requested_clock = now
            self.stop_boundary = self.next_boundary(now)
            self.restore_deadline = self.stop_boundary + 2.
            self.abort_reason = self.abort_reason or reason
            self.state = 'restore_pending'
            self.o.audit('locked_sweep_restore_pending', reason=reason, continuous=True, stop_boundary_in_s=self.stop_boundary - now)
            return
        if self.state in ('fired', 'playing'):
            started = self.fired_clock if self.fired_clock is not None else self.playing_clock
            self.restore_deadline = (started if started is not None else self.o.clock()) + self.duration_s + 2.
            self.abort_reason = self.abort_reason or reason
            self.state = 'restore_pending'
            self.o.audit('locked_sweep_restore_pending', reason=reason)
            return
        if self.state == 'restore_pending':
            return
        self.restore_now(reason)

    # ---- continuous triangle (v5.4) -------------------------------------------------------------
    def next_boundary(self, now):
        """The next record boundary (the centre) at or after now: fired + k*period."""
        if self.fired_clock is None or not self.period_s:
            return now
        k = math.ceil((now - self.fired_clock) / self.period_s - 1e-9)
        return self.fired_clock + max(0, k) * self.period_s

    def periods_played(self, now=None):
        if self.fired_clock is None or not self.period_s:
            return 0
        now = self.o.clock() if now is None else now
        return int(max(0., now - self.fired_clock) // self.period_s)

    def _continuous_tick(self, now):
        limits = self.cfg['limits']
        if self.state == 'restore_pending':
            if now > self.restore_deadline:
                return self.restore_now(self.abort_reason or 'requested')
            if now > self.stop_boundary + .02:
                # missed the boundary (host tick jitter): the next one, still within the bounded deadline
                nxt = self.next_boundary(now)
                if nxt + .02 <= self.restore_deadline:
                    self.stop_boundary = nxt
                else:
                    return self.restore_now(self.abort_reason or 'requested')
            if self.stop_boundary - now <= .15:
                wait = max(0., self.stop_boundary - now)
                if wait:
                    getattr(self.o, 'sleep', time.sleep)(wait)
                self.restore_phase_s = self.o.clock() - self.stop_boundary
                return self.restore_now(self.abort_reason or 'requested')
            return
        if now - self.last_heartbeat > limits['heartbeat_timeout_s']:
            return self.abort('session heartbeat lost', immediate=True)
        if self.fired_clock is not None and now - self.fired_clock >= self.cycles * self.period_s:
            # the AWG has played its N cycles and idles at the centre (the record's first sample)
            self.restore_phase_s = now - (self.fired_clock + self.cycles * self.period_s)
            return self.restore_now('continuous burst completed its %d cycles' % self.cycles)
        if now - self.prepared_clock > self.plan['lifetime_s']:
            self.request_restore('continuous lifetime reached')

    def _context(self, row, now):
        """TEC following during the triangle (human decision 2026-10-23): the context DAC is the integer-period mean of
        dac - multiplier_control*(mod_raw - baseline_control) over whole periods aligned to the trigger (phase 0); at most
        one TEC proposal per completed window. Uncovered window / unknown phase -> hold."""
        from ldcon.core.period_context import PeriodAverager
        fw = self.firmware_expected or {}
        if self.context is None:
            minimum = max(self.period_s, float(self.steady.p.get('supervision_interval_s', 10.)))
            self.context = PeriodAverager(self.period_s, min_window_s=minimum,
                                          max_gap_fraction=max(.125, .35 / self.period_s), max_gap_s=2.)
            self.context.set_start(self.fired_clock)
            self.tec_context.update(periods=self.context.periods, window_s=self.context.window_s,
                                    reason='first window not complete')
        dac, raw = row.get('dac'), row.get('mod_raw')
        if (not isinstance(dac, (int, float)) or not isinstance(raw, (int, float)) or isinstance(dac, bool)
                or 'multiplier_control' not in fw):
            return
        value = float(dac) - float(fw['multiplier_control']) * (float(raw) - float(fw['baseline_control']))
        result = self.context.add(row['owner_s'], value)
        if result is None:
            return
        self.tec_context.update(start_s=result['start_s'], end_s=result['end_s'], skipped=self.context.skipped)
        if not result['ok']:
            self.tec_context.update(reason='window skipped: ' + result['reason'])
            self.tec_proposal = None
            self.steady.proposed = []
            return
        self.tec_context.update(mean_dac=result['mean'], reason=None)
        self._propose_tec(result, row, now)

    def _propose_tec(self, result, row, now):
        s, o = self.steady, self.o
        p = s.p
        reason = None
        if self.before.get('tec_ff_paused'):
            reason = 'TEC/FF was paused before the sweep (hot control): TEC following stays off'
        elif row.get('degraded') or row.get('maintenance_required') or row.get('valid') is False:
            reason = 'optical mode needs maintenance or the latest sample is invalid: TEC held'
        elif p.get('tec_center_reference') == 'activation' and not s.tec_reference_confirmed:
            reason = 'TEC centre reference not confirmed: TEC held'
        if reason:
            self.tec_context['reason'] = reason
            self.tec_proposal = None
            s.proposed = []
            return
        policies = o.policies()
        pp, tp = policies['piezo'], policies['temperature']
        reference = s.planner.tec_reference_dac if s.planner is not None else s.tec_reference_dac
        center = reference if p.get('tec_center_reference') == 'activation' else pp.low + p['tec_center_fraction'] * (pp.high - pp.low)
        delta = p['tec_gain'] * (result['mean'] - center) / p['counts_per_temperature_unit']
        elapsed = result['window_s'] if self.tec_last_clock is None else now - self.tec_last_clock
        step = min(tp.max_step, tp.max_slew_per_s * min(elapsed, p['tec_dt_cap_s']))
        tec = finite(o.hw().connections['tec_awg'].read(self.tec_channel)['value'], 'TEC readback')
        value = tec + max(-step, min(step, delta))
        self.tec_last_clock = now
        if tp.low <= value <= tp.high and abs(value - tec) >= tp.resolution / 2:
            self.tec_proposal = dict(axis='temperature', value=value, reason='PZT slow centering (integer-period context)',
                                     context_dac=result['mean'], centre_dac=center, window_start_s=result['start_s'],
                                     window_end_s=result['end_s'], periods=result['periods'])
            s.proposed = [self.tec_proposal]
            self.tec_context['reason'] = None
        else:
            self.tec_proposal = None
            s.proposed = []
            self.tec_context['reason'] = 'context within the TEC resolution or outside the TEC envelope: no move'

    @property
    def tec_channel(self):
        return self.o.device['connection']['resources']['tec_awg']['channel']

    def tec_adjust(self, request):
        """steady_adjust during a playing continuous triangle: ONLY the latest owner TEC proposal, through the same
        bounds as ECDLOwner locked_move for the temperature axis (activation-relative + absolute bounds, update
        interval, step/slew, resolution, readback; an uncertain write latches the owner fault). The TEC AWG (.37 CH1)
        is a different instrument from the current AWG that plays the record."""
        s, o = self.steady, self.o
        proposal = request.get('proposal')
        if not (self.continuous and self.state == 'playing'):
            raise DeviceError('A locked sweep owns the rig until it is restored')
        if not isinstance(proposal, dict) or proposal.get('axis') != 'temperature':
            raise DeviceError('During a continuous triangle only the owner TEC centring proposal can be applied')
        if not s.proposed or proposal != s.proposed[0] or proposal != self.tec_proposal:
            raise DeviceError('Only the latest owner-generated action can be accepted')
        if o.fault:
            raise DeviceError('Owner fault requires review first')
        policy = o.policies()['temperature']
        hw = o.hw()
        channel = hw.connections['tec_awg'].read(self.tec_channel)
        if (not channel['enabled'] or channel['load'] != 'HZ' or channel['polarity'] != 'NOR' or channel['burst'] != 'OFF'
                or channel['modulation'] != 'OFF' or channel['sweep'] != 'OFF' or channel['waveform'] != 'DC'):
            raise DeviceError('TEC AWG channel is not a plain DC output')
        previous = finite(channel['value'], 'TEC')
        since = o.clock() - o.last_write['temperature']
        if since + 1e-9 < policy.update_interval_s:
            s.proposed.pop(0); self.tec_proposal = None
            return dict(applied=False, reason='device update interval hold')
        elapsed = min(since, policy.update_interval_s, s.p['tec_dt_cap_s'])
        target = proposal['value']
        if policy.resolution:
            target = round(target / policy.resolution) * policy.resolution
        limit = min(policy.max_step, policy.max_slew_per_s * elapsed)
        delta = max(-limit, min(limit, target - previous))
        if policy.resolution:
            delta = math.copysign(math.floor((abs(delta) + 1e-12) / policy.resolution) * policy.resolution, delta)
        value = round((previous + delta) / policy.resolution) * policy.resolution if policy.resolution else previous + delta
        if abs(value - previous) < 1e-12:
            s.proposed.pop(0); self.tec_proposal = None
            return dict(applied=False, reason='device resolution hold')
        bounds = o.device['absolute_conditions']['axes']['temperature']['activation_range']
        low, high = max(policy.low, o.activation['temperature'] + bounds[0]), min(policy.high, o.activation['temperature'] + bounds[1])
        if not low <= value <= high:
            raise DeviceError('Activation-relative or absolute bound exceeded')
        o.audit('move_requested', axis='temperature', value=value, previous=previous, source='continuous triangle TEC centring',
                context=clean(proposal))
        try:
            hw.move('temperature', value)
            after = finite(hw.connections['tec_awg'].read(self.tec_channel)['value'], 'TEC readback')
            if abs(after - value) > o.device['control_settings']['axes']['temperature']['readback_tolerance']:
                raise DeviceError('Applied output readback mismatch')
        except Exception as error:
            o.fault = 'Uncertain output: ' + str(error)
            o.persist_fault()
            raise
        o.last_write['temperature'] = o.clock()
        o.audit('move_applied', axis='temperature', value=after, source='continuous triangle TEC centring')
        s.proposed.pop(0)
        self.tec_proposal = None
        self.tec_moves.append(dict(unix_s=time.time(), value=after, previous=previous, context_dac=proposal['context_dac']))
        del self.tec_moves[:-200]
        return dict(applied=True, value=after)

    def restore_now(self, reason):
        s, o = self.steady, self.o
        if self.state in FINISHED or self.state in ('review_required', 'settling', 'restoring'):
            return
        self.state = 'restoring'
        hw = o.hardware
        checks = dict(firmware=None, ch1=None, ch2=None, reserved=None)
        o.audit('locked_sweep_write_intent', stage='restore', reason=reason, before=clean(self.before))
        if self.degradation['samples']:
            o.audit('locked_sweep_mode_degraded_summary', **clean(self.degradation))
        try:
            if hw is None:
                raise DeviceError('No rig transport for restoration')
            awg = hw.connections['current_awg']
            before_fw = self.before['servo_config']
            if self.fw_touched:
                s.write({k: before_fw[k] for k in FW_GROUP}, 'locked sweep restore: FF and target modulation off (atomic)')
                s.write({k: before_fw[k] for k in FW_SINGLE}, 'locked sweep restore: acceptance window, time match, ADC window')
                o.audit('locked_sweep_system_window_restored', values={k: before_fw[k] for k in FW_SINGLE})
            self.firmware_expected = None
            checks['firmware'] = True
            self._full_data_log_restore('restore: ' + str(reason))
            self._adc_log_restore('restore: ' + str(reason))          # telemetry only, never raises
            if self.ch2_touched:                       # CH2 first: a CH1 problem must not leave CH2 armed
                self.restore_ch2(awg)
            checks['ch2'] = True
            if self.ch1_touched:
                checks['ch1_dither'] = self.restore_ch1(awg, hw)
            checks['ch1'] = True
            reserved = hw.reserved_readbacks()
            checks['reserved'] = reserved == self.before['reserved']
            if not checks['reserved']:
                raise DeviceError('Reserved channels differ from the pre-sweep snapshot: ' + json.dumps(reserved))
        except Exception as error:
            s.ch2_rehearsal = None
            o.fault = o.fault or ('Uncertain locked sweep restoration: ' + str(error))
            self.state = 'review_required'
            self.restore_result = dict(restored=False, checks=checks, error=str(error))
            o.audit('locked_sweep_restore_failed', error=str(error), checks=checks)
            o.persist_fault()
            raise DeviceError(o.fault) from error
        s.tec_ff_paused = self.before['tec_ff_paused']
        try:
            if s.planner is not None and hasattr(s.planner, 'rebase_ff'):
                s.planner.rebase_ff(hw.snapshot()['outputs'])
        except (OSError, DeviceError) as error:
            self.errors.append(dict(unix_s=time.time(), error='planner rebase deferred: ' + str(error)[:200]))
        dither = s.p.get('dither', {})
        s.dither_blackout_until = time.time() + dither.get('window_s', 32.) + dither.get('delivery_delay_s', 3.) + 2.
        s.discard_proposals('locked sweep restored')
        self.restore_result = dict(restored=None, checks=checks, reason=reason)
        o.audit('locked_sweep_hardware_restored', checks=checks, reason=reason)
        self.settle_rows = []
        self.settle_deadline = o.clock() + self.cfg['limits']['settle_timeout_s']
        self.state = 'settling' if s.armed else 'restored_unverified'
        if self.state == 'restored_unverified':
            self.restore_result['restored'] = False
            self.restore_result['reason'] = 'native PI not armed; no settle measurement'

    def _settle_tick(self):
        o = self.o
        try:
            row = self.sample()
        except (MeasurementUnavailable, OSError, KeyError, TypeError, ValueError) as error:
            self.errors.append(dict(unix_s=time.time(), error='settle sample: ' + str(error)[:300]))
            row = None
        if row is not None:
            self.settle_rows.append(clean(row))
        verdict = self.measured(self.settle_rows, centre_hz=C_NM / self.before['servo_config']['target'])
        if verdict['ready']:
            self.measured_after = verdict
            self.state = 'restored'
            self.restore_result.update(restored=True, settled=verdict)
            o.audit('locked_sweep_restored', **clean(self.restore_result))
        elif o.clock() > self.settle_deadline:
            self.measured_after = verdict
            self.state = 'restored_unverified'
            self.restore_result.update(restored=False, settled=verdict)
            o.audit('locked_sweep_restored_unverified', **clean(self.restore_result))

    def status(self, since=0):
        self.last_heartbeat = self.o.clock()
        since = max(0, int(since or 0))
        verdict = self.measured(self.records) if self.state == 'armed' else None
        return clean(dict(state=self.state, ready=bool(verdict and verdict['ready']), measured=verdict,
                          records=self.records[since:], record_count=len(self.records), settle_records=list(self.settle_rows),
                          errors=list(self.errors[-20:]), plan_sha256=self.plan['plan_sha256'], request_id=self.plan['request_id'],
                          programmed=self.programmed, trigger=self.plan['trigger'], fired=self.fired is not None,
                          awg_trigger_ack=copy.deepcopy(self.fired), fired_unix_s=(self.fired or {}).get('fired_unix_s'),
                          fire_ack_unix_s=(self.fired or {}).get('fire_ack_unix_s'), playback_started_unix_s=self.playback_started_unix_s,
                          restored=self.state == 'restored', restore=self.restore_result, measured_after=self.measured_after,
                          abort_reason=self.abort_reason, owner_fault=self.o.fault, duration_s=self.duration_s,
                          levels_start_s=self.levels_start_s, degradation=dict(self.degradation), **self.continuous_fields()))

    def continuous_fields(self):
        if not self.continuous:
            return dict(kind='shot')
        fired_unix = (self.fired or {}).get('fired_unix_s')
        return dict(kind='continuous', period_s=self.period_s, cycles=self.cycles,
                    periods_played=self.periods_played() if self.state in ('fired', 'playing', 'restore_pending') else None,
                    run_until_unix_s=None if fired_unix is None else fired_unix + self.cycles * self.period_s,
                    lifetime_until_unix_s=getattr(self, 'prepared_unix_s', None) and self.prepared_unix_s + self.plan['lifetime_s'],
                    stop_boundary_in_s=None if self.stop_boundary is None else self.stop_boundary - self.o.clock(),
                    restore_phase_s=getattr(self, 'restore_phase_s', None),
                    tec_context=dict(self.tec_context), tec_proposal=copy.deepcopy(self.tec_proposal),
                    tec_moves=list(self.tec_moves[-20:]), tec_current_feedforward='paused (CH1 carries the LDC FF record)')

    def steady_row(self):
        """steady_read during a sweep: report, never plan. v5.4: during a playing continuous triangle the only possible
        action is the owner's TEC centring proposal on the integer-period context DAC (apply it with steady_adjust)."""
        row = self.sample()
        actions, blocked = [], ['locked sweep holds current and TEC']
        if self.continuous and self.state == 'playing':
            blocked = ['continuous triangle holds current (TEC current feed-forward paused)']
            if self.tec_proposal is not None and self.steady.proposed[:1] == [self.tec_proposal]:
                actions = [self.tec_proposal]
            elif self.tec_context.get('reason'):
                blocked.append('TEC: ' + self.tec_context['reason'])
        return clean(dict(state='locked', frequency_hz=row.get('frequency_hz'), error_hz=row.get('error_hz'), actions=actions,
                          blocked=blocked, sweep_mode=self.summary(),
                          single_mode=row.get('single_mode'), maintenance_required=row.get('maintenance_required', False),
                          outputs=None, metrics=row.get('metrics') or {},
                          tec_context=dict(self.tec_context) if self.continuous else None))


def adc_log_journal(o):
    """The rig owner's journal of the temporary ESP32 log_adc_change_threshold (next to its audit log)."""
    from ldcon.owners.adc_log_threshold import Journal
    cached = getattr(o, '_adc_log_journal', None)
    if cached is None:
        cached = o._adc_log_journal = Journal(o.audit_path.with_suffix('.adc-log-threshold.json'))
    return cached


def full_data_log_journal(o):
    """The rig owner's journal of the temporary ESP32 log_full_data_min_interval (v5.9, next to its audit log)."""
    from ldcon.owners.full_data_log import Journal
    cached = getattr(o, '_full_data_log_journal', None)
    if cached is None:
        cached = o._full_data_log_journal = Journal(o.audit_path.with_suffix('.full-data-log.json'))
    return cached


def recover_full_data_log(o):
    """Owner start: restore a full-data interval a previous owner process left lowered. Never raises."""
    try:
        journal = full_data_log_journal(o)
        if o.fault or not journal.needs_restore() or active(o.steady):
            return None
        from ldcon.owners.full_data_log import restore
        servo = o.hardware.connections['piezo']
        record = restore(journal, write=servo.set, read_config=lambda: servo.get('config'),
                         read_status=lambda: servo.get('status'), reason='owner start recovery')
        o.audit('locked_sweep_full_data_log_recovered', record=record)
        return record
    except Exception:                   # noqa: BLE001
        return None


def recover_adc_log(o):
    """Owner start (first hardware open): restore a threshold a previous owner process left lowered. Never raises."""
    try:
        journal = adc_log_journal(o)
        if o.fault or not journal.needs_restore() or active(o.steady):
            return None
        from ldcon.owners.adc_log_threshold import restore
        servo = o.hardware.connections['piezo']
        record = restore(journal, write=servo.set, read_config=lambda: servo.get('config'),
                         read_status=lambda: servo.get('status'), reason='owner start recovery')
        o.audit('locked_sweep_adc_log_threshold_recovered', record=record)
        return record
    except Exception:                   # noqa: BLE001
        return None


# ------------------------------------------------------------------------------------------------ dispatch
def active(steady):
    sweep = getattr(steady, 'sweep', None)
    return sweep is not None and sweep.state not in FINISHED


def dispatch(steady, request):
    """NativeLockOwner.dispatch for locked_sweep_* (owner lock held, rig lease already checked)."""
    o = steady.o
    action = request['action']
    if action in ('locked_sweep_prepare', 'locked_sweep_calibrate'):
        if o.fault:
            raise BrokerError('fault', 'Owner fault requires review before a sweep')
        if not steady.armed or not steady.active:
            raise BrokerError('busy', 'A locked sweep requires an armed, active native lock')
        if active(steady):
            raise BrokerError('busy', 'A locked sweep is already in progress (state %s)' % steady.sweep.state)
        if (o.recovery is not None or steady.commissioning or steady.trial is not None or steady.response_control
                or (steady.study and getattr(steady.study, 'status', None) == 'running')
                or (steady.pid_tuning and not getattr(steady.pid_tuning, 'closed', False))):
            raise BrokerError('busy', 'Another controller operation (recovery/commissioning/study/tuning/response) is active')
        try:
            cfg = configuration(o.device)
        except DeviceError as error:
            raise BrokerError('configuration', str(error)) from error
        if action == 'locked_sweep_calibrate':
            try:
                return calibrate(steady)
            except (BrokerError, MeasurementUnavailable):
                raise
            except Exception as error:
                if o.fault:
                    raise
                raise BrokerError('request', str(error)) from error
        try:
            plan = validate_plan(request.get('plan'), cfg)
            ready = validate_ready(request.get('ready'))
        except DeviceError as error:
            raise BrokerError('request', str(error)) from error
        sweep = LockedSweep(steady, plan, ready, cfg)
        guard = getattr(o, 'shared_guard', None)
        lock = guard[0] if guard else None
        if lock is not None:
            lock.acquire()
        try:
            busy = guard[1]() if guard else None
            if busy:
                # Same process hosts the broker channel of the shared SDG (sr1092 modulation on the TEC AWG CH2).
                raise BrokerError('busy', 'A shared instrument is leased by another owner: ' + json.dumps(busy))
            steady.sweep = sweep            # reservation, seen by the broker claim guard
        finally:
            if lock is not None:
                lock.release()
        steady.discard_proposals('locked sweep')
        try:
            return sweep.prepare()
        except BrokerError:
            steady.sweep = None
            raise
        except MeasurementUnavailable:
            if o.fault:
                raise
            steady.sweep = None
            raise
        except Exception as error:
            if o.fault:
                raise
            steady.sweep = None
            o.audit('locked_sweep_refused', reason=str(error))
            raise BrokerError('request', str(error)) from error
    if action == 'restore_reviewed_dither':
        if o.fault:
            raise BrokerError('fault', 'Owner fault requires review first')
        if active(steady):
            raise BrokerError('busy', 'A locked sweep is in progress')
        return restore_reviewed_dither(steady)
    if action == 'locked_sweep_cleanup':
        if o.fault:
            raise BrokerError('fault', 'Owner fault requires review first')
        if active(steady):
            raise BrokerError('busy', 'A locked sweep is in progress')
        return clean(cleanup_awgs(o))
    sweep = getattr(steady, 'sweep', None)
    if sweep is None:
        raise BrokerError('request', 'No locked sweep exists in this owner')
    if action == 'locked_sweep_status':
        if sweep.thread is None and sweep.thread_enabled is False and request.get('tick') is True:
            sweep.tick()
        return sweep.status(request.get('since', 0))
    if action == 'locked_sweep_trigger':
        return sweep.trigger()
    if action == 'locked_sweep_restore':
        sweep.last_heartbeat = o.clock()
        if sweep.state in FINISHED or sweep.state in ('review_required', 'settling'):
            return sweep.status(request.get('since', 0))
        sweep.request_restore(str(request.get('reason', 'requested'))[:200])
        return sweep.status(request.get('since', 0))
    raise BrokerError('request', 'Unknown locked sweep action')


ACTIONS = {'locked_sweep_prepare', 'locked_sweep_status', 'locked_sweep_trigger', 'locked_sweep_restore', 'locked_sweep_calibrate',
           'locked_sweep_cleanup'}
