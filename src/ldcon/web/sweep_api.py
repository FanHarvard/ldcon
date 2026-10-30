"""Sweep API for labscript (contract v1.3 verdict, 2026-10-23; v1.1 2026-10-22: preparations/sweep-api-20261022/API.md).

labscript sends only ARM (a sequence) and GO (software trigger), plus status and cancel. Everything else is ldcon's:
the ``ldcon.triggered-staircase/v1`` shot request (its 32-hex request id, the plan and its ``plan_sha256`` exactly as
``modulation_sweep.validate_request`` checks them), the FF parameters and the minimum lead (per-laser defaults), and
the proxy job. ARM starts the deck's ``frequency-staircase`` card -- the existing locked-staircase PROXY (sr1092:
``native_ff_proxy`` native FF session; sr422: ``ecdl_locked_sweep_proxy`` rig-owner session) -- through the same start
path as the dashboard. GO is the existing once-only ``/api/jobs/<id>/sweep-trigger``; cancel is the job stop.
No owner, instrument or laser definition is touched here.

Routes (under the dashboard prefix; POSTs accept ``X-LDCon-Client: labscript`` or ``web``):

  POST api/lasers/<laser>/sweeps                       {request_id, sequence, trigger, options?}   -> 202 arming
  GET  api/lasers/<laser>/sweeps?request_id=<id>       lookup after a lost response (404 if unknown)
  GET  api/lasers/<laser>/sweeps/<sweep_id>
  POST api/lasers/<laser>/sweeps/<sweep_id>/go         software trigger, once
  POST api/lasers/<laser>/sweeps/<sweep_id>/cancel

Per-laser defaults: ``control_settings.sweep_api_defaults`` of the device bound to the sweep's modulation interface
(sr1092 ``current_modulation`` -> current_awg, sr422 ``servo_modulation`` -> rig), else the code defaults below.
Persistence: ``<state>/sweep-api/<laser>/requests.json`` and ``sweeps/<sweep_id>.json``; the sweep id IS the proxy
job id, so the job store keeps single flight across a web restart.
"""
import hashlib
import json
import math
import re
import time
import uuid

from fastapi import Request
from fastapi.responses import JSONResponse

from ldcon.web.lock_target_api import ApiError, LockTargetApi, REQUEST_ID, _op

ROUTE = r'/api/lasers/[A-Za-z0-9][A-Za-z0-9_.-]{0,79}/sweeps(?:/[A-Za-z0-9][A-Za-z0-9_.-]{0,79}/(?:go|cancel))?'
KEEP_S = 7 * 86400.
RESTORE_MARGIN_S = 10.          # = native_ff_proxy.RESTORE_MARGIN_S: unwind + AWG restore + settle after the record
FINAL = ('done', 'failed', 'cancelled')
BUSY_OPERATIONS = ('tune.lock_target', 'tune.operating_point', 'tune.device_parameters')
DONE_STATUSES = ('succeeded', 'completed', 'completed_unsettled')
MAX_LEVELS_NATIVE = 10000       # = modulation_sweep.validate_request

# Code defaults (human/owner decisions 2026-10-22). A laser's control_settings.sweep_api_defaults overrides them.
CODE_DEFAULTS = {
    'native': dict(ff_gain_scale=1.05, min_lead_s=.05, arm_window_s=30., min_arm_window_s=5.,
                   settle_fraction=.3, acquire_fraction=.5),
    'rig': dict(ldc_ff_v_per_hz=-1.33e-11, pzt_ff_gain_scale=1., min_lead_s=None, lead_margin=1.2, arm_window_s=30.,
                min_arm_window_s=5., settle_fraction=.3, acquire_fraction=.5),
}
OPTIONAL_OPTIONS = {'native': (), 'rig': ('full_data_log_ms',)}
OPTIONS = {'native': ('ff_gain_scale',), 'rig': ('ldc_ff_v_per_hz', 'pzt_ff_gain_scale')}


def labscript_route(prefix, path):
    return re.fullmatch(re.escape(prefix) + ROUTE, path) is not None


def _finite(value, name, *, positive=False, nonnegative=False):
    if type(value) not in (int, float) or not math.isfinite(value) or (positive and value <= 0) or (nonnegative and value < 0):
        raise ApiError(422, 'sequence', '%s must be a %s number' % (name, 'positive' if positive else 'non-negative' if nonnegative else 'finite'))
    return float(value)


def plan_sha256(plan):
    """Exactly modulation_sweep.validate_request's digest."""
    return hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def body_digest(body):
    return hashlib.sha256(json.dumps(body, sort_keys=True, allow_nan=False).encode()).hexdigest()


class Route:
    """The laser's locked-staircase route: the proxy kind, its modulation interface and its configured limits."""

    def __init__(self, laser):
        from ldcon.jobs.native_ff_proxy import feedforward_route, ff_profile
        from ldcon.jobs.ecdl_locked_sweep_proxy import rig_locked_route, locked_settings
        self.laser = laser
        if feedforward_route(laser, 'current_modulation'):
            self.kind, self.role, self.operation = 'native', 'current_modulation', 'sweep.current_modulation'
            profile = ff_profile(laser)
            self.max_half_span_hz = float(profile['max_half_span_hz'])
            self.max_duration_s = float(profile['max_duration_s'])         # record + start window (FF lifetime)
            self.max_levels = MAX_LEVELS_NATIVE
        elif rig_locked_route(laser, 'servo_modulation'):
            self.kind, self.role, self.operation = 'rig', 'servo_modulation', 'sweep.servo_modulation'
            self.settings = locked_settings(laser)
            limits = self.settings['limits']
            self.max_half_span_hz = float(limits['max_half_span_hz'])
            self.max_duration_s = float(limits['max_duration_s'])          # record only
            self.lifetime_max_s = float(limits['lifetime_max_s'])
            self.max_levels = int(limits['max_levels'])
        else:
            raise ApiError(409, 'not_configured', 'This laser has no locked-staircase route (native FF or ECDL rig)')
        self.defaults = self._defaults()
        # v1.2 continuous triangle: an explicit capability block sweep_api_defaults.continuous
        # {max_s, step_hz[, max_slope_hz_per_s]} on the modulation device. It is added only together with the owner and
        # proxy changes that make a continuous record real (heartbeat-bounded FF/session lifetime, no 'done' after one
        # record); without it continuous is refused (409 not_supported). It is not put into the native FF profile or the
        # locked_sweep block: those are validated/digested by the owner, which enforces its own continuous limit.
        self.continuous = self._continuous_block

    def _defaults(self):
        binding = self.laser['interfaces'][self.role]
        block = self.laser['devices'][binding['device']].get('control_settings', {}).get('sweep_api_defaults')
        values = dict(CODE_DEFAULTS[self.kind])
        self._continuous_block = None
        if isinstance(block, dict) and 'continuous' in block:
            cont = block['continuous']
            if (not isinstance(cont, dict) or not {'max_s', 'step_hz'} <= set(cont)
                    or set(cont) - {'max_s', 'step_hz', 'max_slope_hz_per_s'}
                    or any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in cont.values())):
                raise ApiError(409, 'not_configured', 'sweep_api_defaults.continuous is {max_s, step_hz[, max_slope_hz_per_s]} (positive)')
            self._continuous_block = cont
            block = {k: v for k, v in block.items() if k != 'continuous'}
        if block is None:
            return dict(values, source='code')
        if not isinstance(block, dict) or set(block) - set(values):
            raise ApiError(409, 'not_configured', 'control_settings.sweep_api_defaults accepts only: ' + ', '.join(sorted(values)))
        for key, value in block.items():
            if value is not None and (type(value) not in (int, float) or not math.isfinite(value)):
                raise ApiError(409, 'not_configured', 'sweep_api_defaults.%s must be a finite number' % key)
        values.update(block)
        return dict(values, source='laser')

    def options(self, given):
        if given is None:
            given = {}
        if not isinstance(given, dict):
            raise ApiError(400, 'invalid_request', 'options must be an object')
        allowed = tuple(OPTIONS[self.kind]) + tuple(OPTIONAL_OPTIONS.get(self.kind, ()))
        unknown = set(given) - set(allowed)
        if unknown:
            raise ApiError(422, 'sequence', 'Unknown options for this laser: %s (allowed: %s)'
                           % (', '.join(sorted(unknown)), ', '.join(allowed)))
        values = {key: float(self.defaults[key]) for key in OPTIONS[self.kind]}
        for key, value in given.items():
            if key == 'full_data_log_ms':
                # temporary ESP32 full-data logging during the sweep (owner telemetry setting, restored afterwards)
                if value is None:
                    continue
                if type(value) is not int or not 0 <= value <= 100:
                    raise ApiError(422, 'sequence', 'full_data_log_ms must be an integer 0..100 (ms between full-data rows)')
                values[key] = value
                continue
            values[key] = _finite(value, key)
        for key in ('ff_gain_scale', 'pzt_ff_gain_scale'):
            if key in values and not .5 <= values[key] <= 2.:
                raise ApiError(422, 'sequence', '%s must be within [0.5, 2]' % key)
        if 'ldc_ff_v_per_hz' in values:
            bound = float(self.settings['ldc_ff']['max_abs_v_per_hz'])
            if abs(values['ldc_ff_v_per_hz']) > bound:
                raise ApiError(422, 'sequence', '|ldc_ff_v_per_hz| must be <= %.3g V/Hz' % bound)
        return values

    def min_ramp_s(self, level, options, dwell_s=None):
        """Minimum lead (first level) / tail (last level) in s."""
        floor = self.defaults.get('min_lead_s')
        if self.kind == 'native':
            # v1.5.1 (human-approved 2026-10-23 ~04:45Z): the proxy puts a bounded approach/leave ramp of
            # approach_step_hz steps (one dwell each) INSIDE lead_s/tail_s, ending exactly at lead_s, so a 0 -> -600 MHz
            # jump never straddles one wavemeter exposure ('Same-cycle native FF error exceeded approved limit', B1/B2
            # in preparations/sweep-speed-20261023). The timing stays as requested; a lead/tail that cannot hold the
            # ramp is refused (422 with the minimum), never re-timed.
            return max(float(floor), native_ramp_s(level, dwell_s))
        # sr422 owner rule (ecdl_locked_sweep.compile_record): the approach/leave ramp needs
        #   max(|level| / approach_hz_per_s, |ldc_ff * level| / (0.9 * current max_step / update_interval_s))
        # plus one AWG sample; lead_margin covers the sample-rate rounding (2.65 s at +-600 MHz with -1.33e-11 V/Hz).
        axes_abs = self.laser['devices'][self.laser['interfaces']['current']['device']]['absolute_conditions']['axes']['current']
        axes_cs = self.laser['devices'][self.laser['interfaces']['current']['device']]['control_settings']['axes']['current']
        budget_rate = float(axes_abs['max_step']) / float(axes_cs['update_interval_s'])
        need = abs(level) / float(self.settings['limits']['approach_hz_per_s'])
        ff = options.get('ldc_ff_v_per_hz', 0.)
        if ff:
            need = max(need, abs(ff * level) / (.9 * budget_rate))
        rate = float(self.settings['awg']['max_sample_rate_hz'])
        value = need * float(self.defaults['lead_margin']) + 1. / rate if need else 2. / rate
        value = math.ceil(value * 100. - 1e-9) / 100.
        return max(value, float(floor)) if floor is not None else value


def parse_sequence(route, sequence, options):
    if not isinstance(sequence, dict):
        raise ApiError(400, 'invalid_request', 'sequence must be an object')
    shape = sequence.get('shape', 'staircase')
    if shape == 'triangle':
        # v1.1: both locked routes play staircases only (sr1092 native FF: plan_from_card; sr422 rig owner: the record
        # is compiled from levels_hz/dwell_s; a triangle would have to be silently re-shaped, which v1.1 forbids).
        raise ApiError(422, 'shape', 'A triangle is played only as a continuous waveform (trigger "continuous"); shots '
                                     'play staircases')
    if shape == 'arbitrary':
        return parse_arbitrary(route, sequence, options)
    if shape != 'staircase':
        raise ApiError(422, 'shape', 'shape must be "staircase", "arbitrary" or "triangle"')
    sequence = {k: v for k, v in sequence.items() if k != 'shape'}
    keys = set(sequence)
    if keys - {'levels_hz', 'start_hz', 'stop_hz', 'step_hz', 'dwell_s', 'lead_s', 'tail_s'}:
        raise ApiError(422, 'sequence', 'Unknown sequence keys: ' + ', '.join(sorted(keys - {'levels_hz', 'start_hz', 'stop_hz', 'step_hz', 'dwell_s', 'lead_s', 'tail_s'})))
    ranged = {'start_hz', 'stop_hz', 'step_hz'} & keys
    if 'levels_hz' in keys and ranged or 'levels_hz' not in keys and ranged != {'start_hz', 'stop_hz', 'step_hz'}:
        raise ApiError(422, 'sequence', 'Give either levels_hz, or start_hz, stop_hz and step_hz')
    if 'levels_hz' in keys:
        levels = sequence['levels_hz']
        if not isinstance(levels, list) or not levels:
            raise ApiError(422, 'sequence', 'levels_hz must be a non-empty list')
        levels = [_finite(v, 'levels_hz[%d]' % i) for i, v in enumerate(levels)]
    else:
        start, stop = _finite(sequence['start_hz'], 'start_hz'), _finite(sequence['stop_hz'], 'stop_hz')
        step = _finite(sequence['step_hz'], 'step_hz', positive=True)
        n = int(math.floor(abs(stop - start) / step + 1e-9)) + 1
        if n > route.max_levels:
            raise ApiError(422, 'sequence', 'At most %d levels on this laser' % route.max_levels)
        sign = 1. if stop >= start else -1.
        levels = [start + sign * k * step for k in range(n)]
    if len(levels) > route.max_levels:
        raise ApiError(422, 'sequence', 'At most %d levels on this laser' % route.max_levels)
    worst = max(abs(v) for v in levels)
    if worst > route.max_half_span_hz:
        raise ApiError(422, 'sequence', 'Levels must stay within +-%.0f Hz of the lock target (got %.0f Hz)'
                       % (route.max_half_span_hz, worst))
    if 'dwell_s' not in sequence:
        raise ApiError(422, 'sequence', 'dwell_s is required')
    dwell = _finite(sequence['dwell_s'], 'dwell_s', positive=True)
    min_lead = route.min_ramp_s(levels[0], options, dwell)
    min_tail = route.min_ramp_s(levels[-1], options, dwell)
    lead = min_lead if sequence.get('lead_s') is None else _finite(sequence['lead_s'], 'lead_s', positive=True)
    if lead < min_lead - 1e-12:
        raise ApiError(422, 'sequence', 'lead_s must be >= %.3f s on this laser for this sequence' % min_lead, min_lead_s=min_lead)
    tail = max(lead, min_tail) if sequence.get('tail_s') is None else _finite(sequence['tail_s'], 'tail_s', positive=True)
    if tail < min_tail - 1e-12:
        raise ApiError(422, 'sequence', 'tail_s must be >= %.3f s on this laser for this sequence' % min_tail, min_tail_s=min_tail)
    record = lead + len(levels) * dwell + tail
    if route.kind == 'native':
        limit = route.max_duration_s - float(route.defaults['min_arm_window_s'])
    else:
        limit = route.max_duration_s
        rate = min(float(route.settings['awg']['max_sample_rate_hz']), float(route.settings['awg']['max_points']) / record)
        if math.floor(rate * dwell + 1e-9) < 1:
            raise ApiError(422, 'sequence', 'dwell_s is shorter than one AWG sample for this record (min %.4f s)' % (1. / rate))
    if record > limit + 1e-9:
        raise ApiError(422, 'sequence', 'Record length %.3f s exceeds %.3f s on this laser' % (record, limit), max_record_s=limit)
    if route.kind == 'native' and not native_rpc_fits(route, native_record_levels(levels, dwell), lead_s=lead, tail_s=tail, dwell_s=dwell):
        raise ApiError(422, 'sequence', 'Too many levels for one native FF record: the owner RPC limit is %d bytes '
                                        '(the level list, including the approach/leave ramps, is sent in one request)'
                       % NATIVE_RPC_MAX_BYTES, max_rpc_bytes=NATIVE_RPC_MAX_BYTES)
    return dict(levels=levels, dwell_s=dwell, lead_s=lead, tail_s=tail, record_s=record)


APPROACH_STEP_HZ = 1e8        # = jobs native_ff_proxy.APPROACH_STEP_HZ (v4.17): sr1092 approach/leave ramp step
APPROACH_STEP_S = .1          # = jobs APPROACH_STEP_S: each ramp step is held >= 0.1 s (one wavemeter exposure at 7 Hz)
APPROACH_MIN_CENTRE_S = .01   # = jobs APPROACH_MIN_CENTRE_S: the record still starts/ends at the centre


def native_ramp_levels(level, step_hz=APPROACH_STEP_HZ):
    """The approach (lead) ramp before ``level`` (reversed: the leave ramp after it): sign*k*step for k = 1 ..
    ceil(|level|/step) - 1. Same rule as jobs native_ff_proxy.approach_ramp (v4.17)."""
    if not step_hz or abs(level) <= step_hz:
        return []
    n = int(math.ceil(abs(level) / float(step_hz) - 1e-9)) - 1
    return [math.copysign(k * float(step_hz), level) for k in range(1, n + 1)]


def native_ramp_s(level, dwell_s, step_hz=APPROACH_STEP_HZ):
    """Minimum lead (first level) / tail (last level) that holds the ramp: n ramp levels, each repeated
    r = ceil(APPROACH_STEP_S / dwell) times, plus the centre margin (= jobs native_ff_proxy.ramp_requirements)."""
    n = len(native_ramp_levels(level, step_hz))
    if not n or not dwell_s:
        return 0.
    repeat = int(math.ceil(APPROACH_STEP_S / float(dwell_s) - 1e-9))
    return n * repeat * float(dwell_s) + APPROACH_MIN_CENTRE_S


REHEARSAL_MARGIN_S = 6.            # rig CH2 rehearsal: record + 1 s, plus its arming/readback (~5 s)
NATIVE_SAMPLE_RATE_HZ = 20000.     # = native_ff_sweep.compile_wave default (the proxy compiles at this rate)
NATIVE_MAX_ARBITRARY_RATE_HZ = 1000.   # each sample is one linear segment (edge_s = dwell_s) >= the 1 ms firmware period
DETECT_MARGIN = 1.2                    # an arbitrary record must leave the playback-detection band by this factor


NATIVE_RPC_MAX_BYTES = 16384          # esp32_native_owner / monitor_owner: 'Native RPC must be at most 16 KiB'
NATIVE_RPC_MARGIN_BYTES = 64          # beyond the modelled body (action, plan, digests, lease, device id); 16237 B played live


def native_rpc_fits(route, levels, *, lead_s=0., tail_s=0., dwell_s=1. / 3000.):
    """The ff_validate/ff_begin request body (default json.dumps, as instrument_client sends it) with this record's
    level list stays within the owner's 16 KiB limit (with a margin for the other fields)."""
    import json as _json
    plan = dict(half_span_hz=float(max(abs(v) for v in levels) if levels else 0.), step_hz=1e6, dwell_s=float(dwell_s),
                edge_s=float(dwell_s), cycles=1, levels=[float(v) for v in levels], lead_s=float(lead_s),
                tail_s=float(tail_s), ff_gain_scale=1.05, start_window_s=20.0, continuous=True)
    body = _json.dumps(dict(action='ff_validate', plan=plan, profile_digest='0' * 64, proof_digest='0' * 64,
                            lease='0' * 32, device_id='esp32servocon-sr1092'))
    return len(body.encode()) + NATIVE_RPC_MARGIN_BYTES <= NATIVE_RPC_MAX_BYTES


def native_record_levels(levels, dwell_s):
    """The levels the native proxy sends for a shot record: the approach/leave ramps (each level held >= 0.1 s) plus
    the requested levels (jobs native_ff_proxy.plan_from_card)."""
    repeat = int(math.ceil(APPROACH_STEP_S / float(dwell_s) - 1e-9)) if dwell_s else 1
    lead = [v for v in native_ramp_levels(levels[0]) for _ in range(repeat)]
    tail = [v for v in reversed(native_ramp_levels(levels[-1])) for _ in range(repeat)]
    return lead + list(levels) + tail


def detection_band_hz(route):
    """The smallest |offset| at which the AWG-driven ADC detects playback (sr1092 jobs v4.17: 3 x adc_noise_counts;
    sr422 rig owner: 3 x baseline_tolerance_counts). A record that never leaves it is never 'played' (verdict failed)."""
    if route.kind == 'native':
        from ldcon.jobs.native_ff_proxy import ff_profile
        profile = ff_profile(route.laser)
        native = route.laser['devices'][route.laser['interfaces']['native_current']['device']]['control_settings']['native_current']
        adc_step = (float(native['adc']['max_voltage']) - float(native['adc']['min_voltage'])) / 65535.
        return 3. * float(profile['adc_noise_counts']) * adc_step * abs(float(profile['frequency_hz_per_v']))
    ext = route.settings['ext_mod']
    return 3. * float(ext['baseline_tolerance_counts']) / float(ext['adc_counts_per_v_nominal']) * float(ext['hz_per_v'])


def parse_arbitrary(route, sequence, options):
    """v1.5.1 (human 2026-10-23 ~05:10Z): an arbitrary target-frequency record (neither staircase nor triangle), ARMed
    and triggered like a staircase shot. {shape:'arbitrary', sample_rate_hz, samples_hz:[...] | points:[[t_s, f_hz], ...],
    lead_s?, tail_s?}. Offsets from the lock target. Samples are played one per 1/sample_rate (sr1092: linear between
    samples, edge_s = dwell; sr422: each sample held for one dwell on the rig AWG). ``points`` are knots of a
    piecewise-linear target on the sample grid (t_0 = 0, every t a whole number of sample periods): they are expanded to
    samples by linear interpolation, which is exact on that grid. Never re-timed or re-shaped otherwise. Limits: the
    route's max half span, max levels, record length, the AWG clock (the rate must divide it), the minimum lead/tail of
    the approach/leave ramps; slew is limited only where the owners already limit it (sr422: CH1 max_step per update
    interval of the LDC FF record, checked by the rig owner at prepare). No level analysis (raw data only)."""
    keys = set(sequence)
    allowed = {'shape', 'sample_rate_hz', 'samples_hz', 'points', 'times_s', 'offsets_hz', 'lead_s', 'tail_s'}
    if keys - allowed:
        raise ApiError(422, 'sequence', 'Unknown arbitrary keys: ' + ', '.join(sorted(keys - allowed)))
    if ('times_s' in keys) != ('offsets_hz' in keys):
        raise ApiError(422, 'sequence', 'times_s and offsets_hz go together')
    if 'times_s' in keys:
        # (times_s, offsets_hz) = the same knots as points
        if 'points' in keys or not isinstance(sequence['times_s'], list) or not isinstance(sequence['offsets_hz'], list) \
                or len(sequence['times_s']) != len(sequence['offsets_hz']):
            raise ApiError(422, 'sequence', 'times_s and offsets_hz must be lists of the same length (and no points)')
        sequence = dict({k: v for k, v in sequence.items() if k not in ('times_s', 'offsets_hz')},
                        points=[[t, f] for t, f in zip(sequence['times_s'], sequence['offsets_hz'])])
        keys = set(sequence)
    if ('samples_hz' in keys) == ('points' in keys):
        raise ApiError(422, 'sequence', 'Give exactly one of samples_hz, points or times_s + offsets_hz')
    rate = _finite(sequence.get('sample_rate_hz'), 'sample_rate_hz', positive=True)
    if route.kind == 'native':
        clock, top = NATIVE_SAMPLE_RATE_HZ, NATIVE_MAX_ARBITRARY_RATE_HZ
    else:
        clock = top = float(route.settings['awg']['max_sample_rate_hz'])
    per = clock / rate
    if rate > top + 1e-9 or abs(per - round(per)) > 1e-6:
        near = sorted({clock / k for k in range(max(1, int(math.ceil(clock / top - 1e-9))), int(per) + 3) if k >= 1
                       and clock / k <= top + 1e-9} - {rate}, key=lambda r: abs(r - rate))[:4]
        raise ApiError(422, 'sequence', 'sample_rate_hz must divide the %.0f S/s AWG clock and be <= %.0f S/s on this laser'
                       % (clock, top), representable_rates_hz=near)
    dwell = 1. / rate
    if 'samples_hz' in keys:
        values = sequence['samples_hz']
        if not isinstance(values, list) or not values:
            raise ApiError(422, 'sequence', 'samples_hz must be a non-empty list')
        levels = [_finite(v, 'samples_hz[%d]' % i) for i, v in enumerate(values)]
    else:
        pts = sequence['points']
        if not isinstance(pts, list) or len(pts) < 2 or any(not isinstance(q, list) or len(q) != 2 for q in pts):
            raise ApiError(422, 'sequence', 'points must be a list of at least two [t_s, f_hz] pairs')
        knots = [(_finite(t, 'points[%d].t' % i, nonnegative=True), _finite(f, 'points[%d].f' % i)) for i, (t, f) in enumerate(pts)]
        if knots[0][0] != 0.:
            raise ApiError(422, 'sequence', 'points must start at t = 0 (the first sample; lead_s is separate)')
        index = []
        for t, f in knots:
            k = t * rate
            if abs(k - round(k)) > 1e-6:
                raise ApiError(422, 'sequence', 'point t = %.9g s is not on the %.6g S/s sample grid' % (t, rate))
            index.append((int(round(k)), f))
        if any(b[0] <= a[0] for a, b in zip(index, index[1:])):
            raise ApiError(422, 'sequence', 'point times must increase')
        if index[-1][0] + 1 > route.max_levels:
            raise ApiError(422, 'sequence', 'At most %d samples on this laser' % route.max_levels, max_samples=route.max_levels)
        levels = [index[0][1]]
        for (ka, fa), (kb, fb) in zip(index, index[1:]):
            levels += [fa + (fb - fa) * (k - ka) / (kb - ka) for k in range(ka + 1, kb + 1)]
    if len(levels) > route.max_levels:
        raise ApiError(422, 'sequence', 'At most %d samples on this laser' % route.max_levels, max_samples=route.max_levels)
    worst = max(abs(v) for v in levels)
    if worst > route.max_half_span_hz:
        raise ApiError(422, 'sequence', 'Samples must stay within +-%.0f Hz of the lock target (got %.0f Hz)'
                       % (route.max_half_span_hz, worst))
    band = detection_band_hz(route)
    if worst < DETECT_MARGIN * band:
        raise ApiError(422, 'sequence', 'The record never leaves the playback-detection band (+-%.3g Hz; need |offset| >= '
                                        '%.3g Hz somewhere): ldcon could not tell that it played' % (band, DETECT_MARGIN * band),
                       detection_band_hz=band)
    min_lead = route.min_ramp_s(levels[0], options, dwell)
    min_tail = route.min_ramp_s(levels[-1], options, dwell)
    lead = min_lead if sequence.get('lead_s') is None else _finite(sequence['lead_s'], 'lead_s', positive=True)
    if lead < min_lead - 1e-12:
        raise ApiError(422, 'sequence', 'lead_s must be >= %.3f s on this laser for this record' % min_lead, min_lead_s=min_lead)
    tail = max(lead, min_tail) if sequence.get('tail_s') is None else _finite(sequence['tail_s'], 'tail_s', positive=True)
    if tail < min_tail - 1e-12:
        raise ApiError(422, 'sequence', 'tail_s must be >= %.3f s on this laser for this record' % min_tail, min_tail_s=min_tail)
    record = lead + len(levels) * dwell + tail
    limit = (route.max_duration_s - float(route.defaults['min_arm_window_s'])) if route.kind == 'native' else route.max_duration_s
    if route.kind == 'rig':
        # rig owner compile_record: AWG rate = min(clock, max_points / record); every sample needs >= 1 AWG point
        if min(clock, float(route.settings['awg']['max_points']) / record) < rate - 1e-9:
            raise ApiError(422, 'sequence', 'The record needs more AWG points than the rig allows (%d) at %.6g S/s'
                           % (int(route.settings['awg']['max_points']), rate))
    if record > limit + 1e-9:
        raise ApiError(422, 'sequence', 'Record length %.3f s exceeds %.3f s on this laser' % (record, limit), max_record_s=limit)
    if route.kind == 'native' and not native_rpc_fits(route, native_record_levels(levels, dwell), lead_s=lead, tail_s=tail, dwell_s=dwell):
        raise ApiError(422, 'sequence', 'Too many samples for one native FF record: the owner RPC limit is %d bytes '
                                        '(the sample list, including the approach/leave ramps, is sent in one request); '
                                        'use fewer samples (a lower sample_rate_hz or a shorter record)' % NATIVE_RPC_MAX_BYTES,
                       max_rpc_bytes=NATIVE_RPC_MAX_BYTES)
    return dict(levels=levels, dwell_s=dwell, lead_s=lead, tail_s=tail, record_s=record, shape='arbitrary',
                sample_rate_hz=rate, detection_band_hz=band)
CONTINUOUS_GAP = ('continuous playback needs, on this route, a heartbeat-bounded FF/session lifetime and a session that '
                  'keeps playing until stopped (owner + proxy changes, contract v1.2 in preparations/sweep-api-20261022/API.md); '
                  'the laser definition has no sweep_api_defaults.continuous capability block')


def triangle_points(low, high, step):
    """One period of the triangle through the lock target, as the vertex sequence after the start point 0:
    0 -> high -> low -> 0 in steps of `step` (the last point is 0). The record starts and ends at the centre, so
    the AWG's continuous repetition is seamless (no jump at the wrap) and start/stop happen at the centre."""
    up = int(round(high / step))
    down = int(round(low / step))
    points = [k * step for k in range(1, up + 1)] + [k * step for k in range(up - 1, down - 1, -1)] + \
             [k * step for k in range(down + 1, 1)]
    return points


def parse_triangle(route, sequence, options):
    """v1.2 continuous triangle {shape:'triangle', low_hz, high_hz, rate_hz | period_s}. Never re-shaped silently:
    the endpoints must be whole steps and the rate must be exactly representable on the route's AWG clock."""
    if not isinstance(sequence, dict):
        raise ApiError(400, 'invalid_request', 'sequence must be an object')
    if sequence.get('shape') != 'triangle':
        raise ApiError(422, 'shape', 'trigger "continuous" plays a triangle: sequence.shape must be "triangle"')
    allowed = {'shape', 'low_hz', 'high_hz', 'rate_hz', 'period_s', 'max_run_s', 'step_hz'}
    if set(sequence) - allowed:
        raise ApiError(422, 'sequence', 'Unknown triangle keys: ' + ', '.join(sorted(set(sequence) - allowed)))
    if route.continuous is None:
        raise ApiError(409, 'not_supported', CONTINUOUS_GAP)
    cfg = route.continuous
    base_step = float(cfg['step_hz'])
    step = base_step
    if sequence.get('step_hz') is not None:
        # v1.5.6: the client may choose a COARSER step (a whole multiple of the laser's step) to fit a wide span into
        # the backend's record limits; ldcon never coarsens it by itself (no silent re-shaping)
        step = _finite(sequence['step_hz'], 'step_hz', positive=True)
        if step < base_step - 1e-9 or abs(step / base_step - round(step / base_step)) > 1e-9:
            raise ApiError(422, 'sequence', 'step_hz must be a whole multiple of %.0f Hz on this laser' % base_step,
                           step_hz=base_step)
    low, high = _finite(sequence.get('low_hz'), 'low_hz'), _finite(sequence.get('high_hz'), 'high_hz')
    if not low < high:
        raise ApiError(422, 'sequence', 'low_hz must be below high_hz')
    if not low <= 0 <= high:
        raise ApiError(422, 'shape', 'The continuous triangle must pass through the lock target (low_hz <= 0 <= high_hz): '
                                     'one repeating record starts and ends at the centre')
    if max(-low, high) > route.max_half_span_hz:
        raise ApiError(422, 'sequence', 'Endpoints must stay within +-%.0f Hz of the lock target' % route.max_half_span_hz)
    for name, value in (('low_hz', low), ('high_hz', high)):
        if abs(value / step - round(value / step)) > 1e-9:
            raise ApiError(422, 'sequence', '%s must be a whole number of %.0f Hz steps on this laser' % (name, step), step_hz=step)
    if ('rate_hz' in sequence) == ('period_s' in sequence):
        raise ApiError(422, 'sequence', 'Give exactly one of rate_hz or period_s')
    period = (1. / _finite(sequence['rate_hz'], 'rate_hz', positive=True) if 'rate_hz' in sequence
              else _finite(sequence['period_s'], 'period_s', positive=True))
    points = triangle_points(low, high, step)
    n = len(points)                                    # segments per period (each a linear ramp of one step)
    # v1.5.6 (2026-10-23 smoke): refuse records the backend would refuse after ARM (202 then a failed run):
    #   sr1092: the owner RPC body limit (16 KiB) carries the whole level list; sr422: the rig's limits.max_levels.
    fits = (lambda m: native_rpc_fits(route, triangle_points(low, high, m)[:-1])) if route.kind == 'native' else \
           (lambda m: len(triangle_points(low, high, m)) - 1 <= route.max_levels)
    if not fits(step):
        coarser = next((base_step * j for j in range(int(round(step / base_step)) + 1, 10001)
                        if all(abs(v / (base_step * j) - round(v / (base_step * j))) <= 1e-9 for v in (low, high))
                        and fits(base_step * j)), None)
        limit = ('the native owner RPC limit (%d bytes carry the level list)' % NATIVE_RPC_MAX_BYTES
                 if route.kind == 'native' else 'the rig limit of %d levels per record' % route.max_levels)
        raise ApiError(422, 'sequence', 'A %.0f Hz step gives %d segments per period, more than %s; use a coarser step_hz '
                                        '(smallest that fits: %s Hz) or a smaller span' % (step, n, limit, coarser),
                       min_step_hz=coarser, segments=n)
    if route.kind == 'native':
        rate_clock, minimum, multiple, max_points = NATIVE_SAMPLE_RATE_HZ, 2, 16, 8_000_000
    else:
        # v1.5.1: the rig record is levels + lead/tail of dwell/2 each -> points per dwell even, >= 4 (the owner's
        # approach/leave ramp needs >= 2 points of lead)
        rate_clock, minimum, multiple = float(route.settings['awg']['max_sample_rate_hz']), 4, 1
        max_points = int(route.settings['awg']['max_points'])
    per = period * rate_clock / n
    k = int(round(per))
    step_k = 2 if route.kind == 'rig' else 1
    if abs(per - k) > 1e-6 or k < minimum or (n * k) % multiple or k % step_k:
        options_k = sorted({max(minimum, int(math.floor(per))), max(minimum, int(math.ceil(per)))})
        options_k = sorted({kk + (kk % step_k) for kk in options_k} | {max(minimum, kk - (kk % step_k)) for kk in options_k})
        while options_k and ((n * options_k[-1]) % multiple or options_k[-1] % step_k):
            options_k[-1] += 1
        near = [round(rate_clock / (n * kk), 9) for kk in options_k if (n * kk) % multiple == 0 and kk % step_k == 0]
        raise ApiError(422, 'sequence', 'The rate is not representable on this laser\'s AWG clock (%.0f S/s, %d one-step '
                                        'segments per period): nearest rates %s Hz; or change the span'
                       % (rate_clock, n, near), representable_rates_hz=near)
    if n * k > max_points:
        raise ApiError(422, 'sequence', 'The period needs %d AWG points (max %d); raise the rate or shorten the span' % (n * k, max_points))
    max_run = float(cfg['max_s'])
    if sequence.get('max_run_s') is not None:
        # v1.5.1: the BLACS tab may bound one run below the laser's cap (never above it)
        max_run = _finite(sequence['max_run_s'], 'max_run_s', positive=True)
        if max_run > float(cfg['max_s']) + 1e-9:
            raise ApiError(422, 'sequence', 'max_run_s must be <= %.0f s on this laser' % float(cfg['max_s']),
                           max_run_s=float(cfg['max_s']))
    slope = 2. * (high - low) / period                # Hz/s along the ramps
    limit = cfg.get('max_slope_hz_per_s')
    if route.kind == 'rig':
        axes_abs = route.laser['devices'][route.laser['interfaces']['current']['device']]['absolute_conditions']['axes']['current']
        axes_cs = route.laser['devices'][route.laser['interfaces']['current']['device']]['control_settings']['axes']['current']
        budget = .9 * float(axes_abs['max_step']) / float(axes_cs['update_interval_s'])
        rig_limit = float(route.settings['limits']['approach_hz_per_s'])
        ff = abs(options.get('ldc_ff_v_per_hz', 0.))
        if ff:
            rig_limit = min(rig_limit, budget / ff)
        limit = min(float(limit), rig_limit) if limit is not None else rig_limit
    if limit is not None and slope > float(limit) * (1 + 1e-9):
        raise ApiError(422, 'sequence', 'The triangle slope %.3g Hz/s exceeds %.3g Hz/s on this laser (max rate %.4g Hz for '
                                        'this span)' % (slope, float(limit), float(limit) / (2. * (high - low))),
                       max_rate_hz=float(limit) / (2. * (high - low)))
    if route.kind == 'native':
        # The proxy's own plan path and the AWG record compiler: one record of exactly one period, centre at both ends.
        from ldcon.jobs.native_ff_proxy import owner_plan, plan_from_card
        from ldcon.owners.native_ff_proof import plan_parameters
        from ldcon.jobs.native_ff_sweep import compile_wave
        card = dict(levels_hz=points[:-1], dwell_s=k / rate_clock, edge_s=k / rate_clock, shape='staircase',
                    ff_gain_scale=options.get('ff_gain_scale', 1.))
        try:
            wave = compile_wave(plan_parameters(owner_plan(plan_from_card(card, None))),
                                float(route.laser['devices'][route.laser['interfaces']['native_current']['device']]
                                      ['control_settings']['native_ff_sweep']['frequency_hz_per_v']), sample_rate_hz=rate_clock)
        except ValueError as error:
            raise ApiError(422, 'sequence', 'Not playable by the native FF route: %s' % error) from None
        if abs(wave['duration_s'] - n * k / rate_clock) > 1e-12 or wave['codes'][-1] != 0:
            raise ApiError(422, 'sequence', 'The compiled record is not exactly one period ending at the centre')
    return dict(low_hz=low, high_hz=high, step_hz=step, period_s=n * k / rate_clock, rate_hz=rate_clock / (n * k),
                levels=points[:-1], dwell_s=k / rate_clock, segments=n, slope_hz_per_s=slope, laser_step_hz=base_step,
                max_run_s=max_run, record_s=n * k / rate_clock)


def shot_request(route, seq, trigger, request_id):
    """The ldcon.triggered-staircase/v1 shot request that modulation_sweep.validate_request accepts."""
    from ldcon.jobs.modulation_sweep import PROTOCOL, validate_request
    plan = dict(schema=PROTOCOL, axis=route.role, unit='Hz', reference='initial_offset', levels=list(seq['levels']),
                dwell_s=seq['dwell_s'], settle_s=seq['dwell_s'] * float(route.defaults['settle_fraction']),
                acquire_s=seq['dwell_s'] * float(route.defaults['acquire_fraction']), lead_s=seq['lead_s'],
                tail_s=seq['tail_s'], duration_s=seq['lead_s'] + len(seq['levels']) * seq['dwell_s'] + seq['tail_s'],
                trigger_mode=trigger, trigger_policy='one_edge_starts_entire_record', frequency_hz_per_unit=None,
                shape=seq.get('shape', 'staircase'))
    shot = dict(schema=PROTOCOL, request_id=hashlib.sha256(('sweep-api:' + request_id).encode()).hexdigest()[:32],
                plan_sha256=plan_sha256(plan), plan=plan, allow_simulated=False)
    plan = validate_request(shot)     # the proxy's own check (identity, hash, semantics, record length)
    if route.kind == 'native':
        # Use the exact current proxy/owner boundary. Approach/leave ramp metadata
        # is a host annotation; the owner receives only the physical plan keys.
        from ldcon.jobs.native_ff_proxy import owner_plan, plan_from_card
        from ldcon.owners.native_ff_proof import plan_parameters
        try:
            edge_s = seq['dwell_s'] if seq.get('shape') == 'arbitrary' else min(.01, seq['dwell_s'] / 2)
            plan_parameters(owner_plan(plan_from_card(dict(edge_s=edge_s), plan)))
        except ValueError as error:
            raise ApiError(422, 'sequence', 'Not playable by the native FF route: %s' % error) from None
    return shot


def rig_residuals(table):
    """sr422: the session's residual_table -> the fixed v1.1 fields. The error is the firmware's time-matched error
    (the wavemeter value the PI consumed); the wavemeter-frame error is used where the firmware one is absent."""
    if table is None:
        return None
    out = []
    for row in table:
        fw = row.get('fw_mean_hz') is not None
        out.append(dict(level_hz=row.get('level_hz'), n=row.get('n'),
                        mean_error_hz=row.get('fw_mean_hz') if fw else row.get('wm_mean_hz'),
                        max_abs_error_hz=row.get('fw_max_abs_hz') if fw else row.get('wm_max_abs_hz'),
                        source='firmware' if fw else 'wavemeter'))
    return out


def native_residuals(runtime, record, shot, *, max_bytes=8 << 20):
    """sr1092: the native session publishes no residual table (jobs-side gap); derive it from the lock job's own
    ff_sample events (wavemeter error against the commanded offset read from the ESP32 mod_raw ADC) inside the
    detected playback window. Each sample goes to the nearest requested level; read-only, last <= 8 MB of the log."""
    import json as _json
    from datetime import datetime
    start = shot.get('playback_started_unix_s')
    lock = record.get('lock_job')
    if start is None or not lock:
        return None
    try:
        path = runtime.directory(lock) / 'telemetry.events.jsonl'
        size = path.stat().st_size
        with path.open('rb') as stream:
            stream.seek(max(0, size - max_bytes))
            lines = stream.read().splitlines()[1:]
    except (OSError, FileNotFoundError, ValueError):
        return None
    levels = record.get('levels') or []
    if not levels:
        return None
    end = start + float(record['record_s']) + 1.
    per = {i: [] for i in range(len(levels))}
    for line in lines:
        if b'"ff_sample"' not in line:
            continue
        try:
            row = _json.loads(line)
            stamp = datetime.fromisoformat(row['recorded_utc']).timestamp()
        except (ValueError, KeyError, TypeError):
            continue
        p = row.get('payload') or {}
        if not start <= stamp <= end or not p.get('playing') or p.get('error_hz') is None or p.get('commanded_offset_hz') is None:
            continue
        index = min(range(len(levels)), key=lambda i: abs(levels[i] - p['commanded_offset_hz']))
        per[index].append(float(p['error_hz']))
    return [dict(level_hz=levels[i], n=len(v), mean_error_hz=sum(v) / len(v) if v else None,
                 max_abs_error_hz=max(map(abs, v)) if v else None, source='wavemeter') for i, v in per.items()]


class SweepApi(LockTargetApi):
    """Reuses the lock-target facade's laser/job/lock helpers (same locked source: the lock's own report)."""

    def _sdir(self, laser_id):
        from ldcon.core.records import safe_id
        path = self.runtime.root / 'sweep-api' / safe_id(laser_id)
        (path / 'sweeps').mkdir(parents=True, exist_ok=True)
        return path

    def _srequests(self, laser_id):
        from ldcon.core.records import read_json
        value = read_json(self._sdir(laser_id) / 'requests.json', {})
        return value if isinstance(value, dict) else {}

    def _srecord(self, laser_id, sweep_id):
        from ldcon.core.records import read_json, safe_id
        return read_json(self._sdir(laser_id) / 'sweeps' / (safe_id(sweep_id) + '.json'), None)

    def _ssave(self, laser_id, record):
        from ldcon.core.result_reservations import durable_json
        durable_json(self._sdir(laser_id) / 'sweeps' / (record['sweep_id'] + '.json'), record)

    def _ssave_requests(self, laser_id, mapping):
        from ldcon.core.result_reservations import durable_json
        now = self.clock()
        mapping = {k: v for k, v in mapping.items() if isinstance(v, dict) and now - float(v.get('created_unix_s', 0)) <= KEEP_S}
        durable_json(self._sdir(laser_id) / 'requests.json', mapping)
        return mapping

    def _card(self, laser_id, route):
        found = []
        for item in self.runtime.interface_repository.list('assignments'):
            try:
                value = self.runtime.get_set(item['id'])
            except (ValueError, OSError):
                continue
            if value['document']['laser']['id'] != laser_id:
                continue
            for definition in value['deck_definitions']:
                for card in definition['document']['cards']:
                    if card['operation'] == route.operation and (card.get('parameters') or {}).get('shape') == 'staircase':
                        found.append((value, definition, card['id']))
        if not found:
            raise ApiError(409, 'not_configured', 'No deck of this laser carries a %s staircase card' % route.operation)
        found.sort(key=lambda f: (f[2] != 'frequency-staircase', f[1]['id']))
        return found[0]

    # ------------------------------------------------------------------ ARM
    def arm(self, laser_id, body):
        from ldcon.web.web_runtime import ConflictError, TERMINAL
        if not isinstance(body, dict):
            raise ApiError(400, 'invalid_request', 'Expected a JSON object')
        unknown = set(body) - {'request_id', 'sequence', 'trigger', 'options'}
        if unknown:
            raise ApiError(400, 'invalid_request', 'Unknown fields: ' + ', '.join(sorted(unknown)))
        request_id = body.get('request_id')
        if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
            raise ApiError(400, 'invalid_request', 'request_id must be 1..128 characters [A-Za-z0-9_.:-]')
        if body.get('trigger') not in ('external', 'software', 'continuous'):
            raise ApiError(400, 'invalid_request', 'trigger must be "external", "software" or "continuous"')
        continuous = body['trigger'] == 'continuous'
        laser = self._laser(laser_id)
        digest_ = body_digest(body)
        with self.runtime.mutex:
            entry = self._srequests(laser_id).get(request_id)
            if entry is not None and self._srecord(laser_id, entry['sweep_id']) is not None:
                record = self._srecord(laser_id, entry['sweep_id'])
                if record.get('body_digest') != digest_:
                    raise ApiError(409, 'request_conflict', 'request_id was already used with a different body',
                                   sweep_id=record['sweep_id'])
                return 200, self.status(laser_id, record['sweep_id'])
            route = Route(laser)
            options = route.options(body.get('options'))
            seq = (parse_triangle(route, body.get('sequence'), options) if continuous
                   else parse_sequence(route, body.get('sequence'), options))
            jobs = self._jobs(laser_id)
            from ldcon.web.sweep_guard import sweep_blocker
            blocker = sweep_blocker(jobs, now=self.clock())
            if blocker is not None:
                # v1.4 (2026-10-23): the previous shot is still settling (or active): refuse, machine-readable
                raise ApiError(409, blocker.pop('code'), blocker.pop('detail'), **blocker)
            active = [j for j in jobs if j['status'] not in TERMINAL and any(
                str(_op(c)).startswith('sweep.') or _op(c) in BUSY_OPERATIONS for c in j.get('cards', []))]
            if active:
                raise ApiError(409, 'busy', 'Another sweep or card is active on this laser', job=active[0]['id'])
            pending = self._current_sweep(laser_id)
            if pending is not None:
                raise ApiError(409, 'busy', 'Another sweep is in progress (%s)' % pending['state'], sweep_id=pending['sweep_id'])
            lock, params, live, _ = self._lock(laser, jobs)
            if lock is None or not self._is_locked(lock, live):
                raise ApiError(409, 'not_locked', 'The laser is not locked' + (' (lock state %s)' % (
                    live.get('state') if self._fresh(live) else lock.get('controller_state')) if lock else ''))
            if self._fresh(live) and live.get('ramp_active'):
                raise ApiError(409, 'busy', 'The lock is ramping to a new target; retry after it settles', retry_after_s=2)
            if continuous:
                # v1.5.1: the same 409 semantics as shots (sweep_guard above: a running continuous triangle is a
                # non-final sweep proxy -> a shot ARM gets 409 busy, and a continuous start gets 409 busy/settling
                # while a shot is active or settling)
                return self._start_continuous(laser_id, route, seq, options, body, request_id, digest_, lock)
            if route.kind == 'native':
                window = min(float(route.defaults['arm_window_s']), route.max_duration_s - seq['record_s'])
            else:
                # v1.5.3 (smoke 2026-10-23 05:52Z, sr422 8f0e5c9a expired before 'armed'): with a software (MAN) trigger
                # the rig owner rehearses the whole record on CH2 before it arms (record + 1 s, once per owner process and
                # waveform), so the window also allows that rehearsal; still within the rig's sweep lifetime (contract
                # v1.1 section 7: up to its sweep lifetime for sr422).
                rehearsal = seq['record_s'] + REHEARSAL_MARGIN_S if body['trigger'] == 'software' else 0.
                window = min(float(route.defaults['arm_window_s']) + rehearsal,
                             route.lifetime_max_s - seq['record_s'] - RESTORE_MARGIN_S)
            if window < float(route.defaults['min_arm_window_s']) - 1e-9:
                raise ApiError(422, 'sequence', 'Record too long: it leaves an arm window of %.1f s (< %.1f s)'
                               % (window, float(route.defaults['min_arm_window_s'])))
            shot = shot_request(route, seq, body['trigger'], request_id)
            value, definition, card_id = self._card(laser_id, route)
            overrides = dict(shot_sweep=shot, duration_s=round(seq['record_s'] + window + RESTORE_MARGIN_S, 6),
                             dwell_s=seq['dwell_s'], edge_s=min(.01, seq['dwell_s'] / 2), **options)
            if seq.get('shape') == 'arbitrary' and route.kind == 'native':
                overrides['edge_s'] = seq['dwell_s']        # linear between samples (jobs v4.17 contract)
            if route.kind == 'rig':
                overrides.update(lead_s=seq['lead_s'], tail_s=seq['tail_s'])
            sweep_id = uuid.uuid4().hex
            now = self.clock()
            record = dict(sweep_id=sweep_id, request_id=request_id, laser=laser_id, body_digest=digest_,
                          trigger=body['trigger'], route=route.kind, card_id=card_id, deck_id=definition['id'],
                          shot_request_id=shot['request_id'], plan_sha256=shot['plan_sha256'], options=options,
                          levels=list(seq['levels']), level_count=len(seq['levels']), shape=seq.get('shape', 'staircase'),
                          sample_rate_hz=seq.get('sample_rate_hz'), dwell_s=seq['dwell_s'], lead_s=seq['lead_s'], tail_s=seq['tail_s'],
                          record_s=seq['record_s'], arm_window_s=window, lock_job=lock['id'],
                          defaults_source=route.defaults['source'], created_unix_s=now,
                          arm_deadline_unix_s=now + window)
            self._ssave(laser_id, record)
            mapping = self._srequests(laser_id)
            mapping[request_id] = dict(sweep_id=sweep_id, created_unix_s=now)
            mapping = self._ssave_requests(laser_id, mapping)
            try:
                self.runtime.start_interface_card(value, definition['id'], card_id, overrides, job_id=sweep_id)
            except (ConflictError, ValueError, FileNotFoundError) as error:
                mapping.pop(request_id, None)
                self._ssave_requests(laser_id, mapping)
                try:
                    (self._sdir(laser_id) / 'sweeps' / (sweep_id + '.json')).unlink()
                except OSError:
                    pass
                if isinstance(error, ConflictError):
                    raise ApiError(409, 'busy', str(error)) from None
                raise ApiError(422, 'sequence', str(error)) from None
        return 202, dict(sweep_id=sweep_id, request_id=request_id, state='arming', lead_s=seq['lead_s'],
                         tail_s=seq['tail_s'], record_s=seq['record_s'], arm_deadline_unix_s=record['arm_deadline_unix_s'])

    def _start_continuous(self, laser_id, route, seq, options, body, request_id, digest_, lock):
        """Called under the runtime mutex after the busy/lock checks. The proxy card runs WITHOUT a shot request: the
        card's own continuous mode (trigger 'continuous', explicit levels = one triangle period of one-step linear
        ramps: native edge_s = dwell_s; the AWG repeats that one record on its own clock)."""
        from ldcon.web.web_runtime import ConflictError
        value, definition, card_id = self._card(laser_id, route)
        overrides = dict(trigger='continuous', levels_hz=list(seq['levels']), dwell_s=seq['dwell_s'],
                         duration_s=round(seq['max_run_s'] + RESTORE_MARGIN_S, 6), **options)
        if route.kind == 'native':
            overrides['edge_s'] = seq['dwell_s']           # a linear ramp over the whole dwell: a true triangle
        else:
            # v1.5.1: lead = tail = dwell/2 at the centre, so the rig record (owner compile_record: lead + levels*dwell
            # + tail) is exactly (len(levels)+1) dwells = one period; per-dwell points are even (parse_triangle).
            overrides.update(edge_s=min(.01, seq['dwell_s']), lead_s=seq['dwell_s'] / 2, tail_s=seq['dwell_s'] / 2,
                             sample_rate_hz=float(route.settings['awg']['max_sample_rate_hz']))
        sweep_id = uuid.uuid4().hex
        now = self.clock()
        record = dict(sweep_id=sweep_id, request_id=request_id, laser=laser_id, body_digest=digest_, kind='continuous',
                      trigger='continuous', route=route.kind, card_id=card_id, deck_id=definition['id'], options=options,
                      shape='triangle', low_hz=seq['low_hz'], high_hz=seq['high_hz'], step_hz=seq['step_hz'],
                      rate_hz=seq['rate_hz'], period_s=seq['period_s'], dwell_s=seq['dwell_s'], segments=seq['segments'],
                      slope_hz_per_s=seq['slope_hz_per_s'], max_run_s=seq['max_run_s'], lock_job=lock['id'],
                      defaults_source=route.defaults['source'], created_unix_s=now, run_until_unix_s=now + seq['max_run_s'])
        self._ssave(laser_id, record)
        mapping = self._srequests(laser_id)
        mapping[request_id] = dict(sweep_id=sweep_id, created_unix_s=now)
        mapping = self._ssave_requests(laser_id, mapping)
        try:
            self.runtime.start_interface_card(value, definition['id'], card_id, overrides, job_id=sweep_id)
        except (ConflictError, ValueError, FileNotFoundError) as error:
            mapping.pop(request_id, None)
            self._ssave_requests(laser_id, mapping)
            try:
                (self._sdir(laser_id) / 'sweeps' / (sweep_id + '.json')).unlink()
            except OSError:
                pass
            if isinstance(error, ConflictError):
                raise ApiError(409, 'busy', str(error)) from None
            raise ApiError(422, 'sequence', str(error)) from None
        return 202, dict(sweep_id=sweep_id, request_id=request_id, state='starting', kind='continuous',
                         low_hz=seq['low_hz'], high_hz=seq['high_hz'], rate_hz=seq['rate_hz'], period_s=seq['period_s'],
                         step_hz=seq['step_hz'], max_run_s=seq['max_run_s'])

    def _state_continuous(self, record, job):
        """starting -> running (playback detected on the AWG-driven ADC) -> stopping -> stopped | failed.
        The session must keep playing until stopped: a session that reports 'done' on its own is a backend failure."""
        from ldcon.web.web_runtime import TERMINAL
        if job is None:
            if self.clock() - float(record.get('created_unix_s', 0)) < 30:
                return dict(state='starting', final=False, job=None, shot={}, message=None, restored=None)
            return dict(state='failed', final=True, job=None, shot={}, message='The job was never created', restored=True)
        shot = job.get('shot_sweep') or {}
        status = job.get('status')
        restored = bool(shot.get('restored'))
        outcome = job.get('outcome') or shot.get('outcome')
        if outcome and outcome.get('kind') == 'continuous' and (status in TERMINAL or outcome.get('restored') is not None):
            # v1.5.1: the proxy's own verdict (jobs v4.17/v3.9): succeeded = playback detected, stopped (request,
            # max_run_s or the owner's cap after playback) and the restore verified
            if outcome.get('verdict') == 'succeeded' and outcome.get('restored'):
                return dict(state='stopped', final=True, job=job, shot=shot, restored=True, message=outcome.get('reason'))
            if outcome.get('verdict') == 'failed':
                return dict(state='failed', final=True, job=job, shot=shot, restored=bool(outcome.get('restored')),
                            message=outcome.get('reason'))
        if status in TERMINAL:
            if record.get('cancel_requested') and status in ('cancelled', 'succeeded', 'completed') and restored:
                return dict(state='stopped', final=True, job=job, shot=shot, restored=True, message=None)
            message = job.get('message') or '; '.join(shot.get('errors') or []) or status
            if not record.get('cancel_requested'):
                message = 'The continuous playback ended without a stop request: ' + str(message)
            return dict(state='failed', final=True, job=job, shot=shot, restored=restored, message=message)
        if record.get('cancel_requested') or status == 'stopping':
            return dict(state='stopping', final=False, job=job, shot=shot, message=None, restored=None)
        if shot.get('state') == 'done':
            return dict(state='stopping', final=False, job=job, shot=shot, restored=None,
                        message='The backend ended the continuous playback after one record (not a continuous mode)')
        if shot.get('playback_started_unix_s') is not None or shot.get('state') in ('fired', 'playing'):
            return dict(state='running', final=False, job=job, shot=shot, message=None, restored=None)
        return dict(state='starting', final=False, job=job, shot=shot, message='; '.join(shot.get('errors') or []) or None,
                    restored=None)


    # ------------------------------------------------------------------ status (v1.1)
    def _job(self, sweep_id):
        try:
            return self.runtime.get_job(sweep_id)
        except (FileNotFoundError, ValueError):
            return None

    def _state(self, laser_id, record, job=None):
        """{state, final, job, shot, message, restored} -- v1.1: never done on a timer; done needs a detected playback,
        the record played through (the session's 'done') and a verified restore; expired = no playback detected by
        the fixed arm deadline (the facade stops the proxy; its restore disarms the burst)."""
        from ldcon.web.web_runtime import TERMINAL
        job = job or self._job(record['sweep_id'])
        if record.get('kind') == 'continuous':
            return self._state_continuous(record, job)
        if job is None:
            if self.clock() - float(record.get('created_unix_s', 0)) < 30:
                return dict(state='arming', final=False, job=None, shot={}, message=None, restored=None)
            return dict(state='failed', final=True, job=None, shot={}, message='The sweep job was never created',
                        restored=True)
        shot = job.get('shot_sweep') or {}
        status = job.get('status')
        restored = bool(shot.get('restored'))
        errors = '; '.join(shot.get('errors') or [])
        played = bool(record.get('saw_done') or shot.get('state') == 'done')
        outcome = shot.get('outcome') or job.get('outcome')
        # v1.4: the outcome is published at the verified restore, before the proxy job ends (it keeps running through
        # the lock's settle check, reported in outcome.settle): the facade state is final as soon as the outcome exists.
        if isinstance(outcome, dict) and outcome.get('verdict') in ('succeeded', 'failed', 'cancelled'):
            # v1.3 (2026-10-23): the proxy job's own verdict (jobs v3.7 / v4.15, sweep_verdict.py) decides. succeeded =
            # detected playback + the record played through (the session's / owner's 'done') + verified restore, also
            # when the stop (labscript's transition_to_manual, or this facade) arrived during or after the playback.
            verdict, restored = outcome['verdict'], bool(outcome.get('restored'))
            if record.get('expired') and verdict != 'succeeded' and outcome.get('playback_started_unix_s') is None:
                return dict(state='expired', final=True, job=job, shot=shot, restored=restored, outcome=outcome,
                            message='No playback was detected by the arm deadline; the outputs were restored and the burst '
                                    'disarmed (a later edge does nothing)' if restored else
                                    'No playback was detected by the arm deadline; the restore is NOT verified (review)')
            if verdict == 'succeeded':
                note = ('the post-shot settle check missed its criteria (outputs restored, lock continues)'
                        if outcome.get('settled') is False or status == 'completed_unsettled' else None)
                return dict(state='done', final=True, job=job, shot=shot, restored=True, message=note, outcome=outcome)
            return dict(state=verdict, final=True, job=job, shot=shot, restored=restored, message=outcome.get('reason'),
                        outcome=outcome)
        if status in TERMINAL:
            if record.get('expired') and not played:
                return dict(state='expired', final=True, job=job, shot=shot, restored=restored,
                            message='No playback was detected by the arm deadline; the outputs were restored and the burst '
                                    'disarmed (a later edge does nothing)' if restored else
                                    'No playback was detected by the arm deadline; the restore is NOT verified (review)')
            if played and shot.get('playback_started_unix_s') is not None and restored and status in (
                    'succeeded', 'completed', 'completed_unsettled', 'cancelled'):
                note = ('the post-shot settle check missed its criteria (outputs restored, lock continues)'
                        if status == 'completed_unsettled' or (shot.get('measured_after') or {}).get('ready') is False else None)
                return dict(state='done', final=True, job=job, shot=shot, restored=True, message=note)
            if status == 'cancelled' and record.get('cancel_requested'):
                return dict(state='cancelled', final=True, job=job, shot=shot, restored=restored, message=job.get('message'))
            message = job.get('message') or errors or status
            if played and not restored:
                message = 'The record played but the restore is not verified (review): ' + str(message)
            return dict(state='failed', final=True, job=job, shot=shot, restored=restored, message=message)
        if status == 'stopping' and record.get('cancel_requested'):
            return dict(state='cancelling', final=False, job=job, shot=shot, message=None, restored=None)
        if record.get('expired') and not played:
            return dict(state='expiring', final=False, job=job, shot=shot, message=None, restored=None)
        s = shot.get('state')
        detected = shot.get('playback_started_unix_s') is not None
        if played or detected or (record['trigger'] == 'software' and shot.get('fired')):
            state = 'fired'
        elif s == 'ready' and shot.get('armed') and shot.get('simulated') is False:
            state = 'armed'
        else:
            state = 'arming'
        return dict(state=state, final=False, job=job, shot=shot, message=errors or None, restored=None)

    def _tick(self, laser_id, record):
        """Facade duties (run on every status read and by the background ticker):
        - the session's 'done' (record played, detected on the AWG-driven ADC) -> stop the proxy so it restores now
          (software/external sessions otherwise hold until the shot's stop);
        - arm deadline passed with no detected playback (and no software fire in flight) -> 'expired': stop the proxy;
          its restore parks the AWG / disarms the burst, so a late edge does nothing."""
        from ldcon.web.web_runtime import TERMINAL
        if record.get('final'):
            return record
        if record.get('kind') == 'continuous':
            job = self._job(record['sweep_id'])
            if job is not None and job['status'] not in TERMINAL and (job.get('shot_sweep') or {}).get('state') == 'done' \
                    and not record.get('stop_sent'):
                self.runtime.stop(record['sweep_id'])        # restore now; reported failed (not continuous)
                record['stop_sent'] = self.clock()
                self._ssave(laser_id, record)
            return record
        job = self._job(record['sweep_id'])
        if job is None or job['status'] in TERMINAL:
            return record
        shot = job.get('shot_sweep') or {}
        changed = False
        if shot.get('state') == 'done' and not record.get('saw_done'):
            record['saw_done'] = True
            changed = True
        detected = shot.get('playback_started_unix_s') is not None
        fire_in_flight = record['trigger'] == 'software' and (shot.get('fired') or record.get('go_unix_s') is not None)
        if (not record.get('expired') and not record.get('saw_done') and not detected and not fire_in_flight
                and self.clock() >= float(record['arm_deadline_unix_s'])):
            record['expired'] = True
            changed = True
        if (record.get('saw_done') and not shot.get('auto_finish') or record.get('expired')) and not record.get('stop_sent'):
            # v1.4: a proxy with auto_finish ends a completed shot itself; the facade stops only on expiry
            if job['status'] not in TERMINAL:
                self.runtime.stop(record['sweep_id'])
            record['stop_sent'] = self.clock()
            changed = True
        if changed:
            self._ssave(laser_id, record)
        return record

    def _current_sweep(self, laser_id):
        from ldcon.core.records import read_json
        for path in sorted((self._sdir(laser_id) / 'sweeps').glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True):
            record = read_json(path, None)
            if isinstance(record, dict) and 'sweep_id' in record and not record.get('final'):
                state = self._state(laser_id, record)
                if not state['final']:
                    return dict(sweep_id=record['sweep_id'], request_id=record.get('request_id'), state=state['state'])
        return None

    def tick_all(self):
        """Background duty: tick every non-final sweep of every laser (cheap: one record file per open sweep)."""
        from ldcon.core.records import read_json
        root = self.runtime.root / 'sweep-api'
        if not root.is_dir():
            return
        for laser_dir in root.iterdir():
            for path in (laser_dir / 'sweeps').glob('*.json'):
                record = read_json(path, None)
                if isinstance(record, dict) and 'sweep_id' in record and not record.get('final'):
                    try:
                        with self.runtime.mutex:
                            self._finalize(laser_dir.name, self._tick(laser_dir.name, record))
                    except Exception:
                        pass

    def _finalize(self, laser_id, record):
        state = self._state(laser_id, record)
        if state['final'] and not record.get('final'):
            record.update(final=state['state'], final_unix_s=self.clock(), restored=state['restored'])
            if (state['state'] == 'done' and record.get('route') == 'native' and record.get('residuals') is None
                    and record.get('shape', 'staircase') != 'arbitrary'):      # arbitrary: raw data only
                record['residuals'] = native_residuals(self.runtime, record, state['shot'])
            self._ssave(laser_id, record)
        return state

    def status(self, laser_id, sweep_id):
        from ldcon.web.web_runtime import safe_id, TERMINAL
        self._laser(laser_id)
        try:
            safe_id(sweep_id)
        except ValueError:
            raise ApiError(404, 'not_found', 'Unknown sweep') from None
        with self.runtime.mutex:
            record = self._srecord(laser_id, sweep_id)
            if record is None:
                raise ApiError(404, 'not_found', 'Unknown sweep')
            record = self._tick(laser_id, record)
            state = self._finalize(laser_id, record)
            shot = state['shot']
            if state['state'] == 'armed' and record.get('armed_unix_s') is None:
                record['armed_unix_s'] = self.clock()
                self._ssave(laser_id, record)
        if record.get('kind') == 'continuous':
            result = dict(sweep_id=sweep_id, request_id=record['request_id'], laser=laser_id, kind='continuous',
                          state=state['state'], final=state['final'], trigger='continuous', shape='triangle',
                          low_hz=record['low_hz'], high_hz=record['high_hz'], rate_hz=record['rate_hz'],
                          period_s=record['period_s'], step_hz=record['step_hz'], slope_hz_per_s=record['slope_hz_per_s'],
                          max_run_s=record['max_run_s'], run_until_unix_s=record['run_until_unix_s'],
                          simulated=shot.get('simulated') if shot else None,
                          running_since_unix_s=shot.get('playback_started_unix_s'), message=state['message'],
                          periods_played=shot.get('periods_played'), tec_context=shot.get('context'),
                          outcome=((state['job'] or {}).get('outcome') or (shot or {}).get('outcome')) if state['final'] else None)
            if state['final']:
                result['restored'] = bool(state['restored'])
            return result
        detected = shot.get('playback_started_unix_s')
        if record['trigger'] == 'software' and shot.get('fired_unix_s'):
            fired, source = shot['fired_unix_s'], 'software'
        elif detected is not None:
            fired, source = detected, 'detected'
        else:
            fired, source = None, None
        result = dict(sweep_id=sweep_id, request_id=record['request_id'], laser=laser_id, state=state['state'],
                      final=state['final'], trigger=record['trigger'], shape=record.get('shape', 'staircase'),
                      simulated=shot.get('simulated') if shot else None,
                      trigger_source={'external': 'EXT', 'software': 'MAN'}[record['trigger']],
                      lead_s=record['lead_s'], tail_s=record['tail_s'], dwell_s=record['dwell_s'], record_s=record['record_s'],
                      levels=record.get('levels'), arm_deadline_unix_s=record['arm_deadline_unix_s'],
                      armed_unix_s=record.get('armed_unix_s'), fired_unix_s=fired, fired_source=source,
                      playback_detected_unix_s=detected, message=state['message'], result=None,
                      outcome=state.get('outcome') or (shot.get('outcome') if state['final'] else None))
        out = result['outcome'] or {}
        job = state.get('job') or {}
        result.update(settle=out.get('settle'), analysis=out.get('analysis'),
                      proxy_final=job.get('status') in TERMINAL if job else None, proxy_job_id=sweep_id)
        if state['final']:
            result['restored'] = bool(state['restored'])
        if state['state'] == 'done':
            residuals = (record.get('residuals') if record.get('route') == 'native'
                         else rig_residuals(shot.get('residuals')))
            after = shot.get('measured_after') or {}
            result['result'] = dict(proxy_job_id=sweep_id, residuals=residuals,
                                    settle=dict(ready=after.get('ready'), max_abs_error_hz=after.get('max_abs_error_hz'),
                                                span_hz=after.get('span_hz')))
        return result

    def by_request(self, laser_id, request_id):
        self._laser(laser_id)
        if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
            raise ApiError(400, 'invalid_request', 'request_id must be 1..128 characters [A-Za-z0-9_.:-]')
        with self.runtime.mutex:
            entry = self._srequests(laser_id).get(request_id)
            if entry is None or self._srecord(laser_id, entry['sweep_id']) is None:
                raise ApiError(404, 'not_found', 'Unknown request_id')
        return self.status(laser_id, entry['sweep_id'])

    # ------------------------------------------------------------------ GO / cancel
    def go(self, laser_id, sweep_id):
        from ldcon.web.web_runtime import ConflictError, TriggerPending
        current = self.status(laser_id, sweep_id)
        record = self._srecord(laser_id, sweep_id)
        if record['trigger'] != 'software':
            raise ApiError(409, 'not_software', 'GO is only for trigger "software"')
        if current['state'] == 'fired' or record.get('go_unix_s') is not None and not current['final']:
            return 200, dict(sweep_id=sweep_id, state=current['state'], fired_unix_s=current['fired_unix_s'],
                             fired_source=current['fired_source'])
        if current['state'] != 'armed':
            raise ApiError(409, 'not_armed', 'GO needs state armed (now %s)' % current['state'], state=current['state'])
        with self.runtime.mutex:
            record['go_unix_s'] = self.clock()
            self._ssave(laser_id, record)
        try:
            fired = self.runtime.sweep_trigger(sweep_id, dict(request_id=record['shot_request_id'], plan_sha256=record['plan_sha256']))
        except TriggerPending as pending:
            return 202, dict(sweep_id=sweep_id, state='firing', fired_unix_s=None, detail=str(pending))
        except ConflictError as error:
            raise ApiError(409, 'trigger_refused', str(error)) from None
        return 202, dict(sweep_id=sweep_id, state='fired', fired_unix_s=fired.get('fired_unix_s'), fired_source='software')

    def cancel_sweep(self, laser_id, sweep_id):
        from ldcon.web.web_runtime import TERMINAL
        current = self.status(laser_id, sweep_id)
        if current['final']:
            return 200, current
        with self.runtime.mutex:
            record = self._srecord(laser_id, sweep_id)
            record['cancel_requested'] = True
            self._ssave(laser_id, record)
            try:
                job = self.runtime.get_job(sweep_id)
            except (FileNotFoundError, ValueError):
                job = None
            if job is not None and job['status'] not in TERMINAL:
                self.runtime.stop(sweep_id)          # the proxy asks the lock-side session to restore; the lock continues
        return 202, dict(sweep_id=sweep_id, request_id=record['request_id'],
                         state='stopping' if record.get('kind') == 'continuous' else 'cancelling')


def install_routes(app, prefix, body):
    import asyncio

    import threading

    def api():
        value = getattr(app.state, 'sweep_api', None)
        if value is None or value.runtime is not app.state.runtime:
            value = app.state.sweep_api = SweepApi(app.state.runtime)
        return value

    def ticker(period_s=.5):
        """The facade's own duties (stop the proxy after the session's 'done', expire at the arm deadline) must not
        depend on a client polling, also right after a web restart. Waits for the runtime (created in the app
        lifespan), then ticks until that runtime closes."""
        seen = None
        while True:
            runtime = getattr(app.state, 'runtime', None)
            if runtime is not None:
                seen = runtime
                lock_file = getattr(runtime, '_lock_file', None)   # test runtimes are plain namespaces without it
                if lock_file is None or lock_file.closed:
                    return
                try:
                    api().tick_all()
                except Exception:
                    pass
            elif seen is not None:
                return
            time.sleep(period_s)
    threading.Thread(target=ticker, name='sweep-api-ticker', daemon=True).start()

    async def call(function, *args):
        try:
            value = await asyncio.to_thread(function, *args)
        except ApiError as error:
            return error.response()
        if isinstance(value, tuple):
            return JSONResponse(value[1], status_code=value[0])
        return value

    @app.post(prefix + '/api/lasers/{laser_id}/sweeps')
    async def arm(laser_id: str, request: Request):
        try:
            data = await body(request)
        except ValueError as error:
            return ApiError(400, 'invalid_request', str(error)).response()
        return await call(api().arm, laser_id, data)

    @app.get(prefix + '/api/lasers/{laser_id}/sweeps')
    async def find(laser_id: str, request_id: str = None):
        return await call(api().by_request, laser_id, request_id)

    @app.get(prefix + '/api/lasers/{laser_id}/sweeps/{sweep_id}')
    async def status(laser_id: str, sweep_id: str):
        return await call(api().status, laser_id, sweep_id)

    @app.post(prefix + '/api/lasers/{laser_id}/sweeps/{sweep_id}/go')
    async def go(laser_id: str, sweep_id: str):
        return await call(api().go, laser_id, sweep_id)

    @app.post(prefix + '/api/lasers/{laser_id}/sweeps/{sweep_id}/cancel')
    async def cancel(laser_id: str, sweep_id: str):
        return await call(api().cancel_sweep, laser_id, sweep_id)
