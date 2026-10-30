"""Pure conservative FF proofs; requested plans cannot override safety inputs.

The S1 table and the reachable ADC/PI span are still computed and reported,
but since the human decision of 2026-10-21 (sr1092) they are informational:
firmware-originated PI+FF output is not step/slew capped. The hard conditions
are the DAC rails, the absolute current bounds (with calibration uncertainty)
and the approved ADC validity window; the owner adds the activation-relative
window. Host-written offset/FF-begin/unwind moves keep max_step_a and
max_slew_a_per_s. DAC conversion remains truncation.
"""
import copy
import hashlib
import json
import math

C_NM = 299792458e9
FF_KEYS = ('multiplier_control', 'baseline_control', 'multiplier_target',
           'baseline_target', 'mod_valid_min', 'mod_valid_max')
PLAN_KEYS = {'half_span_hz', 'step_hz', 'dwell_s', 'edge_s', 'cycles'}
# Optional explicit record (labscript shot): the level sequence with lead/tail at the centre.
PLAN_OPTIONAL = {'levels', 'lead_s', 'tail_s', 'ff_gain_scale', 'start_window_s', 'continuous'}
# v5.8 (sweep API v1.2, human 2026-10-23): 'continuous': true = the explicit record is ONE period of a triangle through the
# centre that the AWG repeats on its own clock until stopped. Proved exactly like the record (the wrap is seamless because
# the record starts and ends within one step of the centre); the FF lifetime is bounded by the client heartbeat and by
# the profile's max_continuous_s (absent = continuous playback not approved).
# Per-run correction of the DAC feed-forward gain (multiplier_control only) while the MHz/code response is
# being measured: the policy calibration gives 1.19 MHz/code, the PI-off staircase measured about 1.0 MHz/code.
SCALE_BOUNDS = (.5, 2.)
PROFILE_KEYS = {'enabled', 'revision', 'firmware_version', 'window_rule',
                'frequency_hz_per_v', 'awg_center_v', 'adc_center_count',
                'adc_noise_counts', 'adc_valid_bounds', 'max_half_span_hz',
                'max_duration_s', 'heartbeat_timeout_s', 'missing_timeout_s',
                'unwind_interval_s', 'unwind_steps', 'start_samples',
                'start_span_s', 'start_integral_fraction_max',
                'abort_integral_fraction_max', 'abort_error_hz'}
# Optional (human decision 2026-10-22): during an ACTIVE FF session the owner tolerates a continuous host-input error
# gap (native UDP invalid / not aligned, native-vs-independent disagree, independent missing/stale) of at most this many
# seconds before it ends FF. 0 (or absent) = the previous behaviour (end on the first error). Hard maximum 1.0 s.
# ff_input_gap_s: accepted and bounded but INERT since owner v5.2 (human decision 2026-10-22, rule B: no host-side
# re-validation of wavemeter data, so there is no host-input gap to bridge). Kept so the profile digest does not change.
PROFILE_OPTIONAL = {'ff_input_gap_s', 'max_continuous_s'}
MAX_FF_INPUT_GAP_S = 1.0


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def finite(value, name, *, positive=False):
    if type(value) not in (int, float) or not math.isfinite(value) or (positive and value <= 0):
        raise ValueError('Invalid '+name)
    return float(value)


def validate_profile(profile, policy):
    if not isinstance(profile, dict) or not PROFILE_KEYS <= set(profile) or set(profile) - PROFILE_KEYS - PROFILE_OPTIONAL:
        raise ValueError('Complete explicit native FF profile required')
    p = copy.deepcopy(profile)
    if 'ff_input_gap_s' in p:
        gap = finite(p['ff_input_gap_s'], 'ff_input_gap_s')
        if not 0 <= gap <= MAX_FF_INPUT_GAP_S:
            raise ValueError('ff_input_gap_s must be within 0..%.1f s (human decision 2026-10-22)' % MAX_FF_INPUT_GAP_S)
    if 'max_continuous_s' in p:
        finite(p['max_continuous_s'], 'max_continuous_s', positive=True)       # v5.8: continuous FF lifetime cap
    if type(p['enabled']) is not bool or p['window_rule'] != 'S1':
        raise ValueError('Explicit FF enable and approved S1 rule required')
    for key in ('revision', 'firmware_version'):
        if not isinstance(p[key], str) or not p[key]:
            raise ValueError('Missing FF '+key)
    if p['firmware_version'] != policy['firmware_version']:
        raise ValueError('FF profile firmware must match stationary owner policy')
    for key in ('frequency_hz_per_v', 'awg_center_v', 'adc_center_count', 'adc_noise_counts'):
        finite(p[key], key)
    if p['frequency_hz_per_v'] == 0 or p['adc_noise_counts'] < 0:
        raise ValueError('FF response and ADC noise bounds are invalid')
    adc = policy['adc']
    if not adc['min_voltage'] <= p['awg_center_v'] <= adc['max_voltage']:
        raise ValueError('AWG center outside calibrated ADC interval')
    bounds = p['adc_valid_bounds']
    if (not isinstance(bounds, list) or len(bounds) != 2 or
            any(type(v) is not int for v in bounds) or
            not 0 <= bounds[0] <= p['adc_center_count'] <= bounds[1] <= 65535):
        raise ValueError('Explicit ADC validity bounds must contain center')
    for key in ('max_half_span_hz', 'max_duration_s', 'heartbeat_timeout_s',
                'missing_timeout_s', 'unwind_interval_s', 'start_span_s'):
        finite(p[key], key, positive=True)
    if (p['heartbeat_timeout_s'] > policy['heartbeat_timeout_s'] or
            p['missing_timeout_s'] > policy['missing_timeout_s'] or
            p['unwind_interval_s'] < policy['update_interval_s']):
        raise ValueError('FF supervision cannot weaken stationary deadlines or update interval')
    for key, minimum in (('unwind_steps', 1), ('start_samples', 3)):
        if type(p[key]) is not int or not minimum <= p[key] <= 10000:
            raise ValueError('Invalid FF '+key)
    if not 0 < finite(p['start_integral_fraction_max'], 'start integral fraction') < 1:
        raise ValueError('Start integral fraction must leave reserve')
    if not p['start_integral_fraction_max'] < finite(p['abort_integral_fraction_max'], 'abort integral fraction') <= 1:
        raise ValueError('Abort integral fraction must exceed start reserve and fit raw-I bounds')
    finite(p['abort_error_hz'], 'abort error', positive=True)
    if policy['dac']['bits'] != 16 or policy['adc']['bits'] != 16:
        raise ValueError('FF v1 requires explicit 16-bit calibrated ADC and DAC')
    if any(policy['preserved'][k] != 0 for k in ('multiplier_control', 'multiplier_target',
                                               'software_modulation_enable', 'target_time_match')):
        raise ValueError('FF v1 starts from stationary physical ADC with zero FF and no target time matching')
    return p


def plan_parameters(request):
    """No defaults for physical or safety parameters and no device-specific constants."""
    if not isinstance(request, dict) or not PLAN_KEYS <= set(request) or set(request) - PLAN_KEYS - PLAN_OPTIONAL:
        raise ValueError('FF plan requires exactly half_span_hz, step_hz, dwell_s, edge_s, cycles (optional levels, lead_s, tail_s)')
    p = dict(request)
    for key in PLAN_KEYS - {'cycles'}:
        finite(p[key], key, positive=True)
    if type(p['cycles']) is not int or not 1 <= p['cycles'] <= 10000:
        raise ValueError('Invalid finite FF cycle count')
    n = p['half_span_hz']/p['step_hz']
    if not 1 <= n <= 10000 or not math.isclose(n, round(n), rel_tol=0, abs_tol=1e-9):
        raise ValueError('Half span must be a bounded integer number of steps')
    if p['edge_s'] > p['dwell_s']:
        raise ValueError('FF ramp edge must fit dwell')
    if 'levels' in p:
        seq = p['levels']
        if (not isinstance(seq, list) or not 1 <= len(seq) <= 10000
                or any(type(v) not in (int, float) or not math.isfinite(v) for v in seq)):
            raise ValueError('Explicit FF levels must be 1..10000 finite frequency offsets')
        p['levels'] = [float(v) for v in seq]
        if max(abs(v) for v in p['levels']) > p['half_span_hz'] + 1e-9:
            raise ValueError('Explicit FF levels exceed the declared half span')
        for key in ('lead_s', 'tail_s'):
            p[key] = finite(p.get(key, 0.), key)
        if p['cycles'] != 1:
            raise ValueError('An explicit level record plays exactly once')
    if 'start_window_s' in p:
        # v5.5: the bounded wait between FF enable and playback (arm -> READY -> software/external trigger). FF is on and
        # idles at the centre (FF term ~0) meanwhile; it counts into the FF lifetime and the profile's max_duration_s.
        p['start_window_s'] = finite(p['start_window_s'], 'start_window_s')
        if p['start_window_s'] < 0:
            raise ValueError('start_window_s must be non-negative')
    if 'continuous' in p:
        if type(p['continuous']) is not bool:
            raise ValueError('continuous must be a boolean')
        if p['continuous']:
            # v5.8: one repeated period: an explicit record played once per repetition, starting and ending within one
            # step of the centre so the AWG's wrap never jumps by more than one step.
            if 'levels' not in p:
                raise ValueError('A continuous plan needs an explicit level record (one period)')
            if max(abs(p['levels'][0]), abs(p['levels'][-1])) > p['step_hz'] + 1e-6:
                raise ValueError('A continuous record must start and end within one step of the centre')
    scale = p.get('ff_gain_scale', 1.)
    if type(scale) not in (int, float) or not math.isfinite(scale) or not SCALE_BOUNDS[0] <= scale <= SCALE_BOUNDS[1]:
        raise ValueError('ff_gain_scale must be within %s' % (SCALE_BOUNDS,))
    p['ff_gain_scale'] = float(scale)
    return p


def hz_per_v(profile, plan):
    """The approved profile response (AWG volts per commanded Hz); never from the device."""
    return profile['frequency_hz_per_v']


def ff_gain(plan):
    """Bounded per-run multiplier on the DAC feed-forward coefficient (target modulation is exact regardless)."""
    return float(plan.get('ff_gain_scale', 1.))


def levels(plan):
    if plan.get('levels'):
        return list(plan['levels'])
    n = int(round(plan['half_span_hz']/plan['step_hz']))
    return ([0.] + [-k*plan['step_hz'] for k in range(1, n+1)] +
            [k*plan['step_hz'] for k in range(-n+1, n+1)] +
            [k*plan['step_hz'] for k in range(n-1, -1, -1)])


def duration_s(plan):
    # cycles occurs only here and in the waveform builder, never squared.
    return (plan.get('lead_s', 0.) + plan.get('tail_s', 0.) + len(levels(plan))*plan['dwell_s'])*plan['cycles']


def all_window_s1(*, span_a, rate_a_per_s, jump_a, max_step_a, period_s):
    """Prove every real lag using a monotone upper bound, not sampled lags.

    On [kT,(k+1)T) the budget is constant and min(span, jump+rate*tau)
    is monotone. Its left limit at (k+1)T bounds the whole interval.
    Once k*step covers the total span all later windows are automatic.
    Includes arbitrary phase, cycle seams, repeated unwind and all lags >1s.
    """
    for key, value in (('span', span_a), ('rate', rate_a_per_s), ('jump', jump_a)):
        if finite(value, key) < 0:
            raise ValueError('Negative S1 bound')
    finite(max_step_a, 'maximum step', positive=True)
    finite(period_s, 'S1 period', positive=True)
    count = max(1, math.ceil(span_a/max_step_a))
    if count > 100000:
        raise ValueError('S1 proof too large; reject rather than subsample')
    rows = []
    for k in range(count):
        upper = (k+1)*period_s
        bound = min(span_a, jump_a+rate_a_per_s*upper)
        budget = (k+1)*max_step_a
        rows.append(dict(lag_lower_s=k*period_s, lag_upper_exclusive_s=upper,
                         change_bound_a=bound, budget_a=budget, ok=bound <= budget))
    return dict(ok=all(r['ok'] for r in rows), intervals=rows,
                tail_from_s=count*period_s, tail_bound_a=span_a,
                argument='monotone bound at every S1 left limit plus full-span tail')


def antiwindup_snapshot(sample):
    """FF snapshot from the shipped firmware (2026.10.21-antiwindup): no v1 contract block.

    The firmware publishes the atomic modulation group in /config, and mod_raw,
    mod_invalid (count), pid_integral/pid_output, target_match_* and dac_val in
    /status. There is no per-cycle sequence, no DAC write confirmation and no
    config revision; the synthesized fields below make those facts explicit:
    cycle/system sequence = the wavemeter input sequence, cycle time = uptime,
    DAC completion = the readback of dac_val (``dac_write_ok`` True by definition),
    and the firmware DAC range limits (/dac/limits, human decision 2026-10-22) are
    the final output guard and must be active.
    """
    cfg, status = sample['servo_config'], sample['servo_status']
    for key in ('mod_raw', 'mod_invalid', 'pid_integral', 'pid_output', 'system_value', 'last_udp_seq',
                'last_udp_quality', 'last_system_interval_us', 'uptime_s', 'dac_val', 'target_match_overflow'):
        if status.get(key) is None:
            raise ValueError('Missing firmware status field ' + key)
    for key in (*FF_KEYS, 'target', 'control_offset', 'polling_interval', 'target_time_match'):
        if cfg.get(key) is None:
            raise ValueError('Missing firmware configuration field ' + key)
    raw = int(status['mod_raw'])
    if not 0 <= raw <= 65535:
        raise ValueError('Invalid FF ADC code')
    valid = cfg['mod_valid_min'] <= raw <= cfg['mod_valid_max']
    seq = int(status['last_udp_seq'])
    quality_ok = status['last_udp_quality'] == 0
    cycle_time_us = int(float(status['uptime_s']) * 1e6)
    interval_us = int(status['last_system_interval_us'])
    time_match = cfg.get('target_time_match') == 1
    match_raw = status.get('target_match_mod_raw')
    match_valid = (not time_match) or (type(match_raw) is int and type(status.get('target_match_age_us')) is int
                                       and status['target_match_age_us'] >= 0)
    used_for_target = match_raw if (time_match and match_valid) else raw
    limits = sample.get('dac_limits') if isinstance(sample.get('dac_limits'), dict) else None
    # Human decision 2026-10-27: the sr1092 modulation-port DAC range is the full 0..65535; saved firmware limits that
    # span the whole range are still the active anti-windup guard (at the rails), not a disabled limiter.
    limit_enabled = bool(limits and type(limits.get('min_code')) is int and type(limits.get('max_code')) is int
                         and limits['min_code'] < limits['max_code'])
    metadata = sample.get('metadata') or {}
    s = dict(contract='antiwindup', cycle_seq=seq, cycle_time_us=cycle_time_us, config_revision=0,
             profile_digest=cfg.get('ff_profile_digest'), adc_raw=raw, adc_used=raw, mod_valid=valid, mod_has_valid=True,
             mod_invalid=int(status['mod_invalid']), target_match_valid=bool(match_valid),
             target_match_overflow=int(status['target_match_overflow']), adc_io_valid=True,
             software_modulation_enabled=cfg.get('software_modulation_enable') == 1,
             analog_test=metadata.get('mode', 'normal') != 'normal', dac_limit_enabled=limit_enabled, dac_limits=limits,
             dac_write_ok=True, dac_output_unknown=False, dac_write_failures=0, dac_val=int(status['dac_val']),
             dac_time_us=cycle_time_us, pid_cycle_seq=seq, target_config_revision=0,
             system_seq=seq, system_rx_us=max(0, cycle_time_us - interval_us), target_time_us=cycle_time_us,
             polling_interval_us=cfg['polling_interval'] * 1000, pid_output=float(status['pid_output']),
             pid_integral=float(status['pid_integral']), system_value=float(status['system_value']),
             effective_target=cfg['target'] + cfg['multiplier_target'] * (raw - cfg['baseline_target']),
             target_for_pid=cfg['target'] + cfg['multiplier_target'] * (used_for_target - cfg['baseline_target']),
             effective_offset=cfg['control_offset'] + cfg['multiplier_control'] * (raw - cfg['baseline_control']),
             adc_glitches=status.get('adc_glitches'), input_quality_ok=quality_ok,
             **{k: cfg[k] for k in FF_KEYS})
    if s['software_modulation_enabled'] or s['analog_test']:
        raise ValueError('Physical ADC normal mode required for FF')
    if not limit_enabled:
        raise ValueError('Firmware DAC range limits (/dac/limits) must be readable before FF (saved anti-windup guard)')
    return s


def status_block(sample):
    """The FF status view the owner uses for identity checks, for either firmware generation."""
    status = sample['servo_status']
    if isinstance(status.get('feedforward'), dict):
        return status['feedforward']
    try:
        return antiwindup_snapshot(sample)
    except (ValueError, KeyError, TypeError):
        return {}


def firmware_snapshot(sample):
    cfg = sample['servo_config']
    s = sample['servo_status'].get('feedforward')
    if cfg.get('ff_contract_version') != 1 and not isinstance(s, dict):
        return antiwindup_snapshot(sample)
    if (cfg.get('ff_contract_version') != 1 or cfg.get('dac_quantization') != 'truncate_nonnegative'
            or not isinstance(s, dict) or cfg.get('ff_config_revision') != s.get('config_revision')):
        raise ValueError('Coherent atomic FF v1 truncation metadata required')
    for key in ('cycle_seq', 'cycle_time_us', 'config_revision', 'adc_raw', 'adc_used',
                'mod_invalid', 'target_match_overflow', 'pid_cycle_seq', 'target_config_revision',
                'system_seq', 'system_rx_us', 'target_time_us', 'dac_time_us', 'dac_val', 'dac_write_failures'):
        if type(s.get(key)) is not int or s[key] < 0:
            raise ValueError('Missing FF snapshot '+key)
    for key in ('adc_raw', 'adc_used', 'dac_val'):
        if s[key] > 65535:
            raise ValueError('Invalid FF ADC code')
    for key in ('mod_valid', 'mod_has_valid', 'target_match_valid', 'adc_io_valid',
                'software_modulation_enabled', 'analog_test', 'dac_limit_enabled', 'dac_write_ok', 'dac_output_unknown'):
        if type(s.get(key)) is not bool:
            raise ValueError('Missing FF snapshot '+key)
    if s.get('profile_digest') != cfg.get('ff_profile_digest'):
        raise ValueError('FF snapshot profile identity mismatch')
    for key in (*FF_KEYS, 'effective_target', 'target_for_pid', 'effective_offset', 'pid_output', 'pid_integral', 'system_value'):
        finite(s.get(key), 'FF snapshot '+key)
    if any(s[k] != cfg.get(k) for k in FF_KEYS):
        raise ValueError('FF configuration and snapshot disagree')
    if not s['target_match_valid']:
        raise ValueError('No valid same-cycle comparison target')
    if s['software_modulation_enabled'] or s['analog_test']:
        raise ValueError('Physical ADC normal mode required for FF')
    if not s['dac_write_ok'] or s['dac_output_unknown'] or s['dac_time_us'] < s['cycle_time_us']:
        raise ValueError('FF DAC command completion is unconfirmed')
    if s['dac_limit_enabled'] or cfg.get('dac_limit_enabled') is not False:
        raise ValueError('FF owner v1 has not approved final-DAC limiter activation')
    if s.get('polling_interval_us') != cfg.get('polling_interval', math.nan)*1000:
        raise ValueError('FF snapshot control period differs from firmware configuration')
    if (s['pid_cycle_seq'] > s['cycle_seq'] or s['target_config_revision'] > s['config_revision']
            or s['system_rx_us'] > s['cycle_time_us'] or s['target_time_us'] > s['cycle_time_us']):
        raise ValueError('FF comparison snapshot has impossible sequence or time ordering')
    return s


def qualified_pid_sample(snapshot, max_age_s):
    """Last consumed PID input has its own age/revision, separate from ADC cycles."""
    return (snapshot['pid_cycle_seq'] > 0 and snapshot['system_rx_us'] > 0
            and snapshot['target_config_revision'] == snapshot['config_revision']
            and 0 <= snapshot['cycle_time_us']-snapshot['system_rx_us'] <= max_age_s*1e6
            and 0 <= snapshot['cycle_time_us']-snapshot['target_time_us'] <= max_age_s*1e6)


def prove(policy, profile, plan, sample):
    profile = validate_profile(profile, policy)
    plan = plan_parameters(plan)
    s = firmware_snapshot(sample)
    if not qualified_pid_sample(s, policy['max_sample_age_s']):
        raise ValueError('Fresh consumed native PID sample for current FF revision required')
    cfg, c = sample['servo_config'], policy['calibration']
    if sample['firmware_version'] != profile['firmware_version']:
        raise ValueError('FF firmware identity differs from approved profile')
    dt = finite(cfg.get('polling_interval'), 'actual control period', positive=True)*.001
    if cfg['polling_interval'] != policy['preserved']['polling_interval']:
        raise ValueError('Actual firmware polling period differs from approved owner policy')
    pi_dt = finite(cfg.get('pid_max_interval'), 'actual PI maximum interval', positive=True)*.001
    if pi_dt > policy['native']['interval_bounds_s'][1]+1e-12:
        raise ValueError('Actual PI interval exceeds approved owner bound')
    if cfg.get('pid_kd') != 0:
        raise ValueError('FF proof requires commissioned PI without D')
    adc_step = (policy['adc']['max_voltage']-policy['adc']['min_voltage'])/65535
    dac_step = (policy['dac']['max_voltage']-policy['dac']['min_voltage'])/65535
    a_code = abs(c['current_a_per_v']*dac_step)*(1+c['gain_relative_uncertainty'])
    nm_code = c['wavelength_nm_per_a']*c['current_a_per_v']*dac_step
    target = finite(cfg['target'], 'target', positive=True)
    if not nm_code:
        raise ValueError('Measured wavelength/current response required')
    response = hz_per_v(profile, plan)
    mt = -target**2/C_NM*response*adc_step
    mc = mt/nm_code*ff_gain(plan)
    center = profile['adc_center_count']
    low_adc, high_adc = profile['adc_valid_bounds']
    ffs = [mc*(x-center) for x in (low_adc, high_adc)] + [0.]
    targets = [target+mt*(x-center) for x in (low_adc, high_adc)] + [target]
    errors = max(abs(x-y) for x in (cfg['system_min'], cfg['system_max']) for y in targets)
    raw_i = max(abs(cfg['pid_integral_min']), abs(cfg['pid_integral_max']))
    pi = abs(cfg['pid_kp'])*errors+abs(cfg['pid_ki'])*raw_i
    # Outward endpoints cover truncation and every coherent scale in [0,1],
    # including arbitrarily many abort/restore transactions and held ADC.
    codes = [math.floor(cfg['control_offset']+min(ffs)-pi),
             math.ceil(cfg['control_offset']+max(ffs)+pi)]
    span_a = (codes[1]-codes[0])*a_code
    period = max(policy['update_interval_s'], policy['max_step_a']/policy['max_slew_a_per_s'])
    ff_rate = abs(mc)*plan['step_hz']/abs(response)/adc_step/plan['edge_s']
    # One PI update can include the full admitted interval; P may jump at any
    # instant. Unwind contributes another bounded rate plus one initial step.
    unwind_jump = max(abs(x) for x in ffs)/profile['unwind_steps']
    jump = (ff_rate*dt + 2*abs(cfg['pid_kp'])*errors + abs(cfg['pid_ki'])*errors*pi_dt +
            2*abs(mc)*profile['adc_noise_counts'] + unwind_jump + 1)*a_code
    rate = (ff_rate+abs(cfg['pid_ki'])*errors+
            unwind_jump/profile['unwind_interval_s'])*a_code
    windows = all_window_s1(span_a=span_a, rate_a_per_s=rate, jump_a=jump,
                           max_step_a=policy['max_step_a'], period_s=period)
    reasons = []
    if not profile['enabled']:
        reasons.append('FF profile disabled')
    if plan['half_span_hz'] > profile['max_half_span_hz']:
        reasons.append('Requested half span exceeds approved FF profile')
    if duration_s(plan) > profile['max_duration_s']:
        reasons.append('Requested waveform duration exceeds approved FF profile')
    elif duration_s(plan) + plan.get('start_window_s', 0.) > profile['max_duration_s']:
        reasons.append('Requested waveform duration plus start window exceeds approved FF profile max_duration_s')
    if plan.get('continuous') and 'max_continuous_s' not in profile:
        reasons.append('FF profile has no max_continuous_s (continuous playback not approved)')
    excursion = plan['half_span_hz']/abs(response)/adc_step
    if not low_adc <= center-excursion-profile['adc_noise_counts'] <= center+excursion+profile['adc_noise_counts'] <= high_adc:
        reasons.append('Requested waveform plus approved ADC noise leaves valid window')
    if plan['edge_s'] < dt:
        reasons.append('Requested edge is shorter than actual firmware control period')
    if not 0 <= codes[0] <= codes[1] <= 65535:
        reasons.append('Reachable FF plus PI output reaches DAC rails')
    limits = s.get('dac_limits')
    if s.get('contract') == 'antiwindup':
        if not limits:
            reasons.append('Firmware DAC range limits are unavailable; the final output guard is required')
        elif not limits['min_code'] <= codes[0] <= codes[1] <= limits['max_code']:
            reasons.append('Reachable FF plus PI output leaves the firmware DAC range limits')
    # Human decision 2026-10-21 (sr1092): firmware-originated PI+FF output has
    # no per-step/S1 slew requirement. The reachable span and the S1 table are
    # reported for review only; rails, absolute current bounds and the ADC
    # window remain hard conditions. Host moves use their explicit path policy.
    from ldcon.owners.native_modulation_limits import report
    step_rule=dict(enforced=False,span_exceeds_one_host_step=span_a > policy['max_step_a'],
                   s1_windows_ok=windows['ok'],decision='human approval 2026-10-21: no firmware step/slew cap',
                   **report(policy))
    def current(code):
        v = policy['dac']['min_voltage']+code*dac_step
        return c['bias_current_a']+c['current_a_per_v']*(v-c['zero_voltage_v'])
    currents = [c['bias_current_a']+(current(x)-c['bias_current_a'])*f
                for x in codes for f in (1-c['gain_relative_uncertainty'],1+c['gain_relative_uncertainty'])]
    if not policy['current_bounds_a'][0] <= min(currents) <= max(currents) <= policy['current_bounds_a'][1]:
        reasons.append('Reachable current exceeds absolute current policy')
    result = dict(ok=not reasons, reasons=reasons, plan=plan, plan_digest=digest(plan),
        profile_digest=digest(profile), duration_s=duration_s(plan), start_window_s=float(plan.get('start_window_s', 0.)),
        continuous=bool(plan.get('continuous')),
        max_continuous_s=float(profile['max_continuous_s']) if plan.get('continuous') and 'max_continuous_s' in profile else None,
        code_bounds=codes, current_bounds_a=[min(currents), max(currents)],
        maximum_jump_a=span_a, correction_count=pi, windows=windows, step_rule=step_rule,
        firmware_contract=s.get('contract', 'v1'), dac_limits=limits, frequency_hz_per_v=response, ff_gain_scale=ff_gain(plan),
        ff_mhz_per_code_assumed=abs(nm_code/ff_gain(plan))/(target**2/C_NM)/1e6,
        timing=dict(control_period_s=dt, pi_max_interval_s=pi_dt, source='actual firmware config'),
        quantization='truncate_nonnegative', fault_trajectory='full reachable span within one control period (not step-capped)',
        values=dict(multiplier_control=mc, baseline_control=center, multiplier_target=mt,
                    baseline_target=center, mod_valid_min=low_adc, mod_valid_max=high_adc))
    result['proof_digest'] = digest(result)
    return result
