"""Locked staircase on sr1092: the sweep card job is a PROXY for an FF session inside the running lock job.

Why a proxy (labscript contract, 2026-10-22): labscript polls the job id that /start
returned, reads ``shot_sweep`` on it (request_id, plan_sha256, state ready/armed,
restored) and finally POSTs /stop to that same id. The resident lock job must never be
that id. So ``frequency-staircase`` (sweep.current_modulation) on a laser whose
modulation route is the native feed-forward chain (AWG CH2 -> ESP32 ADC -> firmware FF +
target modulation -> DAC -> LDC) starts its own job, hands a request to the running
``servo.native_current`` job (file queue under the lock job, at-most-once via
native_ff_sweep.RequestJournal) and mirrors the session's measured state on its own
progress. Its stop cancels only that shot; the lock keeps running.

Measured criteria (not lock_start, not "no hop"): ``ready`` needs N fresh wavemeter
samples with |f - target| <= tolerance_hz, span <= stability_hz, valid native input,
FF session active and the AWG armed; ``restored`` needs the owner-reported FF restore,
the AWG restore and the same N-sample criteria again around the original target.
"""
import copy
import hashlib
import json
import math
import os
import time
from pathlib import Path

from ldcon.core.contracts import DeviceError, MeasurementUnavailable
from ldcon.core.interface_cards import CardCancelled

READY_DEFAULTS = dict(tolerance_hz=5e6, stability_hz=5e6, samples=5)
REQUEST_TTL_S = 60.
STATUS_NAME = '.status.json'


def feedforward_route(laser, role='current_modulation'):
    """True when this modulation role is the native FF chain (AWG -> ESP ADC) with an approved profile."""
    binding = laser.get('interfaces', {}).get(role)
    if not binding:
        return False
    device = laser['devices'].get(binding['device'], {})
    if 'native_feedforward_awg' not in device.get('control_settings', {}):
        return False
    native = laser.get('interfaces', {}).get('native_current')
    if not native:
        return False
    return 'native_ff_sweep' in laser['devices'].get(native['device'], {}).get('control_settings', {})


def ff_profile(laser):
    native = laser['interfaces']['native_current']
    return laser['devices'][native['device']]['control_settings']['native_ff_sweep']


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def ready_criteria(value):
    p = {**READY_DEFAULTS, **(value or {})}
    if set(p) != set(READY_DEFAULTS):
        raise ValueError('ready accepts tolerance_hz, stability_hz and samples')
    for key in ('tolerance_hz', 'stability_hz'):
        if type(p[key]) not in (int, float) or not math.isfinite(p[key]) or p[key] <= 0:
            raise ValueError(key + ' must be positive')
    if type(p['samples']) is not int or not 1 <= p['samples'] <= 200:
        raise ValueError('ready.samples must be within 1..200')
    return p


APPROACH_STEP_HZ = 1e8        # v4.17 (human approval 2026-10-23 ~04:45Z): approach/leave ramp step of a shot record
APPROACH_STEP_S = .1          # each ramp level is held at least this long (D-series value), even for short dwells
APPROACH_MIN_CENTRE_S = .01   # the record must still start and end at the centre (the armed burst idles at sample 0)


def approach_ramp(level_hz, step_hz=APPROACH_STEP_HZ):
    """Ramp levels between the centre and ``level_hz`` in steps of at most ``step_hz`` (excluding 0 and the level):
    sign*k*step for k = 1 .. ceil(|level|/step) - 1. Empty when |level| <= step."""
    count = int(math.ceil(abs(level_hz) / step_hz - 1e-9)) - 1 if level_hz else 0
    return [math.copysign(k * step_hz, level_hz) for k in range(1, max(0, count) + 1)]


def ramp_repeats(dwell_s):
    """How many record dwells each ramp level lasts: r = ceil(APPROACH_STEP_S / dwell_s) (r = 1 at 0.1 s dwells, so
    0.1 s staircases are unchanged; an arbitrary record at 1000 S/s holds each 100 MHz ramp level for 100 samples)."""
    return max(1, int(math.ceil(APPROACH_STEP_S / float(dwell_s) - 1e-9)))


def ramp_requirements(levels, dwell_s, step_hz=APPROACH_STEP_HZ):
    """Minimum lead_s / tail_s that hold the approach / leave ramps (r dwells per ramp level) plus a centre margin:
    lead = n_lead x r x dwell + 0.01 s (if n_lead > 0), tail likewise. The web facade mirrors this formula (422)."""
    r = ramp_repeats(dwell_s)
    lead = len(approach_ramp(levels[0], step_hz)) * r * dwell_s if levels else 0.
    tail = len(approach_ramp(levels[-1], step_hz)) * r * dwell_s if levels else 0.
    return dict(lead_s=lead + (APPROACH_MIN_CENTRE_S if lead else 0.), tail_s=tail + (APPROACH_MIN_CENTRE_S if tail else 0.))


def plan_from_card(p, shot):
    """Explicit level sequence for the FF session (lead/tail at the centre), from the card or the shot plan.

    v4.17: a shot record gets bounded approach/leave ramps of APPROACH_STEP_HZ INSIDE lead_s/tail_s (the first
    requested level still starts exactly at lead_s, the leave ramp starts right after the last requested level), so the
    0 -> -600 MHz jump no longer happens within one wavemeter exposure ('Same-cycle native FF error exceeded approved
    limit', 2026-10-23 B1/B2; 4/4 clean with 100 MHz ramps, D1/D3/D4). ``ramp_levels`` counts them (not requested
    levels). A trigger 'continuous' card plays ONE period of a triangle of one-step linear ramps (edge_s = dwell_s)
    that the AWG repeats: plan['continuous'] = True (owner v5.8)."""
    ramp_lead = ramp_tail = []
    if shot:
        levels = [float(v) for v in shot['levels']]
        dwell, lead, tail = float(shot['dwell_s']), float(shot['lead_s']), float(shot['tail_s'])
        if shot['unit'] != 'Hz':
            raise ValueError('Native FF staircase takes frequency levels (unit Hz)')
        need = ramp_requirements(levels, dwell)
        if lead + 1e-9 < need['lead_s'] or tail + 1e-9 < need['tail_s']:
            raise ValueError('lead_s/tail_s too short for the bounded %.0f MHz approach/leave ramps: need lead_s >= %.3f s '
                             'and tail_s >= %.3f s' % (APPROACH_STEP_HZ / 1e6, need['lead_s'], need['tail_s']))
        r = ramp_repeats(dwell)
        ramp_lead = [v for v in approach_ramp(levels[0]) for _ in range(r)]
        ramp_tail = [v for v in reversed(approach_ramp(levels[-1])) for _ in range(r)]
    else:
        levels = [float(v) for v in p['levels_hz']]
        # Direct finite records honor the explicitly requested centre holds.
        # Their ordinary edge_s transition remains unchanged; only externally
        # supplied shot plans prepend the separately described approach levels.
        dwell, lead, tail = float(p['dwell_s']), float(p.get('lead_s', 0.)), float(p.get('tail_s', 0.))
        if p['shape'] == 'triangle':
            raise ValueError('Native FF route plays staircases only (shape staircase)')
    half_span = max(abs(v) for v in levels)
    # Nominal level step (between consecutive REQUESTED levels); the centre->first-level jump (now the ramp) is reported
    # by the proof as a jump, not capped (human decision: no firmware step/slew cap).
    steps = [abs(b - a) for a, b in zip(levels, levels[1:]) if b != a]
    step = max(steps) if steps else (half_span or 1.)
    if half_span and step and not math.isclose(half_span / step, round(half_span / step), rel_tol=0, abs_tol=1e-9):
        # v4.17 (arbitrary shots, 2026-10-23 ~05:10Z): sampled waveforms have no common step; the owner proof and the
        # record compiler need half_span = n x step_hz. Use the smallest step >= the largest consecutive change that
        # divides the half span (step_hz only scales the record and bounds the reported slope; levels are exact).
        step = half_span / max(1, math.floor(half_span / step + 1e-12))
    record = ramp_lead + levels + ramp_tail
    plan = dict(half_span_hz=half_span, step_hz=step, dwell_s=dwell,
                edge_s=float(p.get('edge_s', .01)), cycles=1, levels=record,
                lead_s=round(lead - len(ramp_lead) * dwell, 9), tail_s=round(tail - len(ramp_tail) * dwell, 9),
                ff_gain_scale=float(p.get('ff_gain_scale', 1.)))
    if ramp_lead or ramp_tail:
        plan['ramp_levels'] = dict(lead=len(ramp_lead), tail=len(ramp_tail), step_hz=APPROACH_STEP_HZ)
    if not shot and p.get('trigger') == 'continuous':
        plan['continuous'] = True
    return plan


def owner_plan(plan):
    """The plan the owner proves: without the host-side ramp annotation (owner plan keys are exact)."""
    return {k: v for k, v in plan.items() if k != 'ramp_levels'}


def requested_levels(plan):
    """The requested levels of a record (ramp levels removed)."""
    levels = list(plan.get('levels') or plan.get('levels_hz') or [])
    ramps = plan.get('ramp_levels') or {}
    return levels[int(ramps.get('lead', 0)):len(levels) - int(ramps.get('tail', 0))]


def measured(samples, target_hz, criteria):
    """READY/RESTORED verdict from a list of (frequency_hz or None, valid) samples."""
    rows = [f for f, ok in samples if ok and f is not None]
    if len(rows) < criteria['samples']:
        return dict(ready=False, reason='insufficient fresh samples', samples=len(rows))
    rows = rows[-criteria['samples']:]
    error = max(abs(f - target_hz) for f in rows)
    span = max(rows) - min(rows)
    ok = error <= criteria['tolerance_hz'] and span <= criteria['stability_hz']
    return dict(ready=ok, samples=len(rows), max_abs_error_hz=error, span_hz=span,
                tolerance_hz=criteria['tolerance_hz'], stability_hz=criteria['stability_hz'],
                reason=None if ok else 'error or stability outside the ready criteria')


# ---------------------------------------------------------------- lock-job side
class RequestQueue:
    """File queue under the lock job: pending/<id>.json -> journal claim -> <id>.status.json, <id>.cancel."""
    def __init__(self, lock_directory):
        self.root = Path(lock_directory) / 'ff-requests'
        self.pending = self.root / 'pending'
        self.pending.mkdir(parents=True, exist_ok=True)

    def submit(self, request):
        from ldcon.core.records import atomic_json
        path = self.pending / (request['request_id'] + '.json')
        if path.exists():
            raise ValueError('FF request already submitted')
        atomic_json(path, request)
        return path

    def receive(self):
        """Oldest pending request (consumed from pending/; the journal decides at-most-once)."""
        from ldcon.core.records import read_json
        for path in sorted(self.pending.glob('*.json'), key=lambda p: p.stat().st_mtime):
            value = read_json(path, None)
            try:
                path.unlink()
            except OSError:
                pass
            if isinstance(value, dict) and value.get('request_id'):
                return value
        return None

    def status_path(self, request_id):
        return self.root / (request_id + STATUS_NAME)

    def write_status(self, request_id, value):
        from ldcon.core.records import atomic_json
        atomic_json(self.status_path(request_id), dict(value, unix_s=time.time()))

    def read_status(self, request_id):
        from ldcon.core.records import read_json
        return read_json(self.status_path(request_id), None)

    def cancel(self, request_id):
        (self.root / (request_id + '.cancel')).touch()

    def finish(self, request_id):
        """Sweep API v1.4 (protocol 2): the proxy ends a shot whose record completed (labscript no longer stops it)."""
        (self.root / (request_id + '.finish')).touch()

    def finished(self, request_id):
        return (self.root / (request_id + '.finish')).exists()

    def cancelled(self, request_id):
        return (self.root / (request_id + '.cancel')).exists()

    def trigger_path(self, request_id):
        return self.root / (request_id + '.trigger.json')

    def trigger(self, request_id, record):
        """At most one software trigger per request: the first record wins, a repeat returns the existing one."""
        return write_once(self.trigger_path(request_id), record)

    def trigger_record(self, request_id):
        from ldcon.core.records import read_json
        return read_json(self.trigger_path(request_id), None)


def write_once(path, record):
    """Create ``path`` with ``record`` exactly once (O_EXCL); return the record that is on disk."""
    from ldcon.core.records import read_json
    path = Path(path)
    data = json.dumps(record, sort_keys=True, allow_nan=False)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        existing = read_json(path, None)
        return existing if existing is not None else dict(record)
    with os.fdopen(fd, 'w') as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    return dict(record)


TRIGGERS = ('software', 'auto', 'external', 'continuous')


def lock_cancel(queue, request_id, card_cancel):
    return lambda: bool(card_cancel()) or queue.cancelled(request_id)


def continuous_cycles(max_run_s, period_s):
    """v4.19: the N of the continuous MAN burst: N = ceil(max_run_s / period) + 1 (the run ends by the stop / max_run_s
    before the AWG runs out; after N records it idles at the centre). Refused above 1e6 (the SDG BTWV TIME range)."""
    n = int(math.ceil(float(max_run_s) / float(period_s) - 1e-9)) + 1
    if not 1 <= n <= 1000000:
        raise ValueError('Continuous run needs %d record cycles (max 1000000): shorten max_run_s or lengthen the period' % n)
    return n


RECENTRE_FOLLOW_FRACTION = .25     # v4.17: follow the PI centre during a continuous run above this |mean integral|
CONTEXT_MIN_WINDOW_S = 10.          # context window: >= 1 and an integer number of triangle periods, at least this long
CONTEXT_MAX_GAP_FRACTION = .5       # a window is used only if no sample gap exceeds half a period ...
CONTEXT_MAX_GAP_S = 1.              # ... and 1 s
CONTEXT_MANY_PERIODS = 8            # v4.23: with >= this many whole periods per window, gaps up to CONTEXT_MAX_GAP_S are
                                    # accepted even when that is more than half a period (see CentreFollower)


class CentreFollower:
    """sr1092 slow following during a CONTINUOUS triangle (human 2026-10-23: keep the following ON; compute the context
    over >= 1 and an integer number of periods). The native PI integral answers the triangle periodically (FF gain
    error); its mean over n whole periods, aligned to the detected playback start, is the context. When the window mean
    of |integral|/limit exceeds RECENTRE_FOLLOW_FRACTION, ONE bounded owner 'recenter' step moves the PI centre toward
    the window-mean integral-neutral code (control_offset + mean pid_output), bounded by the owner's host step budget.
    The owner (v5.8) accepts it only in an active continuous FF session and re-proves the FF at the new centre.
    Unknown start or an uncovered window -> hold (no move)."""

    def __init__(self, port, period_s, *, emit=lambda *_: None, min_window_s=CONTEXT_MIN_WINDOW_S):
        from ldcon.core.period_context import PeriodAverager
        self.port, self.emit = port, emit
        self.period_s = float(period_s)
        # coverage: the owner's cached status advances every ~0.2-0.5 s, so a gap bound of 1/8 period (the module default)
        # would reject every window of a ~1 s triangle; half a period (<= 1 s) keeps >= 2 samples per period
        # v4.23 (2026-10-23 07:53Z, +-100 MHz at 1.25 Hz: 13-period windows, every one skipped 'sample gap longer than
        # 0.4 s'): this session's loop (owner status + heartbeats + ff_status RPCs) samples every ~0.31 s (p90 0.45 s,
        # max 0.52 s) and the owner's integral itself refreshes only at its ~0.5 s polls, so half of a 0.8 s period is
        # not a usable bound. With many whole periods per window the sample phases are not locked to the triangle and
        # the time-weighted window mean keeps <= 10 % of the periodic ripple (tests: <= 0.6 code of a +-6-code answer = 4 % of
        # the integral limit; the following acts only beyond 25 %), so the bound
        # is CONTEXT_MAX_GAP_S (1 s) there; short windows (few periods) keep half a period.
        periods = int(math.ceil(float(min_window_s) / self.period_s - 1e-9)) if self.period_s > 0 else 0
        fraction = (max(CONTEXT_MAX_GAP_FRACTION, CONTEXT_MAX_GAP_S / self.period_s)
                    if periods >= CONTEXT_MANY_PERIODS else CONTEXT_MAX_GAP_FRACTION)
        self.output = PeriodAverager(period_s, min_window_s=min_window_s, max_gap_fraction=fraction,
                                     max_gap_s=CONTEXT_MAX_GAP_S)
        self.fraction = PeriodAverager(period_s, min_window_s=min_window_s, max_gap_fraction=fraction,
                                       max_gap_s=CONTEXT_MAX_GAP_S)
        self.last_hold_reason = None
        self.moves = []
        self.holds = 0
        self.last = None
        self.refusals = []

    def start(self, t0):
        self.output.set_start(t0)
        self.fraction.set_start(t0)

    def budget(self):
        try:
            from ldcon.jobs.operating_point import _NativePolicy
            return max(1, min(64, int(_NativePolicy(self.port.policy).max_step)))
        except Exception:
            return 1

    def feed(self, t, state):
        sample = (state or {}).get('sample') or {}
        status, cfg = sample.get('servo_status') or {}, sample.get('servo_config') or {}
        try:
            limit = max(abs(float(cfg['pid_integral_min'])), abs(float(cfg['pid_integral_max'])))
            fraction = float(status['pid_integral']) / limit if limit > 0 else None
            output = float(status['pid_output'])
            offset = int(round(float(cfg['control_offset'])))
        except (KeyError, TypeError, ValueError):
            return None
        window = self.output.add(t, output)
        frac = self.fraction.add(t, fraction)
        if window is None or frac is None:
            return None
        self.last = dict(window=window, fraction=frac)
        if not window['ok'] or not frac['ok']:
            self.holds += 1
            self.last_hold_reason = window.get('reason') or frac.get('reason')
            self.emit('ff_context', dict(action='hold', reason=window.get('reason') or frac.get('reason'),
                                         periods=window['periods'], window_s=window['window_s']))
            return None
        mean_fraction, mean_output = frac['mean'], window['mean']
        record = dict(action='none', periods=window['periods'], window_s=window['window_s'], start_unix_s=window['start_s'],
                      mean_integral_fraction=mean_fraction, mean_pid_output=mean_output, control_offset=offset)
        if abs(mean_fraction) > RECENTRE_FOLLOW_FRACTION:
            budget = self.budget()
            target = int(round(offset + mean_output))
            step = max(-budget, min(budget, target - offset))
            if step:
                try:
                    self.port.mutate('recenter', code=offset + step, expected_code=offset,
                                     **(dict(max_step_codes=budget) if budget > 1 else {}),
                                     reason='continuous centre following (integer-period context)')
                    record.update(action='recenter', code=offset + step, step=step, target=target)
                    self.moves.append(dict(unix_s=time.time(), code=offset + step, previous=offset,
                                           mean_integral_fraction=mean_fraction))
                except (DeviceError, MeasurementUnavailable) as exc:
                    if getattr(self.port, 'uncertain_output', False):
                        raise
                    record.update(action='refused', reason=str(exc)[:200])
                    self.refusals.append(str(exc)[:200])
        self.emit('ff_context', record)
        return record

    def summary(self):
        last = self.last or {}
        window = last.get('window') or {}
        return dict(source='integer_period_mean', period_s=self.period_s, periods=self.output.periods,
                    window_s=self.output.window_s, windows_skipped=self.output.skipped, holds=self.holds,
                    last_mean_integral_fraction=(last.get('fraction') or {}).get('mean'),
                    last_mean_pid_output=window.get('mean'), recentres=len(self.moves), refusals=len(self.refusals),
                    max_gap_s=self.output.max_gap, last_hold_reason=self.last_hold_reason,
                    follow_fraction=RECENTRE_FOLLOW_FRACTION)


class ProxiedFFSession:
    """Runs inside the laser owner: FF enable, AWG staircase (triggered or continuous),
    measured READY, playback detection via the ADC, bounded restore with measured verification."""
    def __init__(self, port, awg, request, queue, *, clock=time.monotonic, sleep=time.sleep,
                 cancel=lambda: False, emit=lambda *_: None, sample_interval_s=.1):
        from ldcon.jobs.native_ff_sweep import NativeFFSweepSession
        self.port, self.awg, self.request, self.queue = port, awg, request, queue
        self.clock, self.sleep, self.emit = clock, sleep, emit
        self.cancel = lock_cancel(queue, request['request_id'], cancel)
        self.criteria = ready_criteria(request.get('ready'))
        trigger = request.get('trigger', 'software')
        if trigger == 'external_rising':
            trigger = 'external'
        if trigger not in TRIGGERS:
            raise ValueError('Unknown trigger mode: ' + str(trigger))
        self.trigger = trigger
        # 'software': armed MAN burst fired by POST /sweep-trigger; 'auto': the same burst fired by this session
        # as soon as READY holds (dashboard studies); 'external': EXT rising edge on the AWG trigger input;
        # 'continuous': repeating playback started once READY holds (plan cycles).
        self.triggered = trigger in ('software', 'external')
        self.inner = NativeFFSweepSession(port, awg, request['plan'], clock=clock, sleep=sleep,
                                          cancel=self.cancel, emit=emit, sample_interval_s=sample_interval_s)
        self.state = dict(state='preparing', armed=False, ready=False, restored=False, trigger_mode=trigger,
                          trigger_source={'software': 'MAN', 'auto': 'MAN', 'external': 'EXT', 'continuous': None}[trigger],
                          fired=False, fired_unix_s=None, fire_ack_unix_s=None, awg_trigger_ack=None,
                          trigger_requested_unix_s=None, trigger_forwarded_unix_s=None, playback_started_unix_s=None,
                          request_id=request['request_id'], plan_sha256=request.get('plan_sha256'), measured=None, errors=[], samples=0,
                          protocol=2,       # v1.4: '.finish' after the record, 'settling' published at the verified restore
                          playback_detection='owner_cache_mod_raw')

    def _publish(self, **update):
        self.state.update(update)
        self.queue.write_status(self.request['request_id'], self.state)
        self.emit('ff_proxy_state', copy.deepcopy(self.state))

    def _sample(self):
        """One wavemeter/native sample: (frequency_hz, valid, adc_counts). The ADC value is the OWNER's cached ESP32
        status (polled ~0.2-0.5 s), not a fresh read: playback detection can lag (2026-10-23 03:12Z ce388df2: cache
        19578 while VM already had 7862) -> reported as playback_detection 'owner_cache_mod_raw'."""
        try:
            self.port.heartbeat()
            state = self.port.read()
        except MeasurementUnavailable:
            return None, False, None
        self.last_state = state
        optical = state.get('optical') or {}
        adc = ((state.get('sample') or {}).get('servo_status') or {}).get('mod_raw')
        valid = optical.get('quality') == 'valid' and state.get('input_valid') is True
        return optical.get('frequency_hz'), valid, adc

    def _collect(self, target_hz, deadline_s, *, want_ready=True, cancellable=True):
        samples = []
        deadline = self.clock() + deadline_s
        while self.clock() < deadline:
            if cancellable and self.cancel():
                raise InterruptedError('FF proxy cancelled')
            if cancellable and getattr(self.inner, 'guardian', None) is not None:
                # v4.17: the owner ending FF (keepalive ff_lost) during READY ends the shot at once (-> park on abort)
                self.inner.guardian.check()
            f, ok, _ = self._sample()
            samples.append((f, ok))
            verdict = measured(samples, target_hz, self.criteria)
            if verdict['ready'] or not want_ready and verdict['samples'] >= self.criteria['samples']:
                return verdict
            self.sleep(self.inner.sample_interval_s)
        return measured(samples, target_hz, self.criteria)

    def run(self, target_hz):
        plan = self.request['plan']
        self.last_state = None
        from ldcon.jobs.native_ff_sweep import compile_wave
        profile = self.port.ff_profile
        result = dict(status='failed', errors=[], restored=False)
        begun = prepared = False
        guardian = None
        try:
            from ldcon.jobs.native_ff_sweep import LeaseKeepalive
            if hasattr(self.port, 'ff_token'):
                # The lock worker's port keeps the PREVIOUS session's token; heartbeating it while ff_begin replaces
                # it raced into "Exact native feedforward token required" (2026-10-22 18:59Z R4, proxy 5f8e7a74).
                self.port.ff_token = None
            guardian = LeaseKeepalive(self.port)
            guardian.__enter__()
            self.inner.guardian = guardian
            self.awg.set_checkpoint(self.inner._check_cancel, guardian.lease_check)
            validated = self.inner.retry_measurement('ff_validate', self.port.ff_validate, plan)
            if not validated['ok']:
                result.update(status='rejected', errors=list(validated['reasons']))
                self._publish(state='rejected', errors=validated['reasons'])
                return result
            from ldcon.owners.native_ff_proof import hz_per_v
            wave = compile_wave(plan, hz_per_v(profile, plan))
            self.inner._check_cancel()
            self.awg.prepare(wave, profile['awg_center_v'])
            prepared = True
            self.awg.stage()
            # Arm BEFORE the owner enables FF (2026-10-22 16:39Z): the arm changes the carrier (DC -> ARB); on the SDG
            # a waveform-type change drops the output for ~16 ms (mod_invalid advanced 0 -> 16), which the owner's FF
            # monitor rightly treats as an invalid input. Armed and untriggered, the burst idles at the record's first
            # sample = the centre, so the owner's FF start gate sees a stable centre before it enables FF.
            if self.trigger == 'external':
                self.awg.arm(source='EXT')
            elif self.trigger in ('software', 'auto'):
                self.awg.arm(source='MAN')
            elif self.trigger == 'continuous':
                # v4.19 (2026-10-23 05:51Z smoke, lock 8733b084): awg.play() AFTER ff_begin changed the carrier DC -> ARB
                # with FF active; the SDG's ~16 ms 0 V dropout advanced mod_invalid (54 -> 71) and the owner ended FF
                # ('Physical ADC invalid or invalid counter advanced', a firmware-path check rule B keeps). Continuous now
                # uses the proven shot path: the record is armed as a MAN burst of N cycles BEFORE ff_begin (the type
                # change happens with FF off; the armed burst idles at sample 0 = the centre) and fired by MTRIG after
                # READY (no type change while FF is on).
                self.cycles = continuous_cycles(float(self.request.get('max_run_s') or self.request.get('lifetime_s', 600.)),
                                                wave['duration_s'])
                self.awg.arm(source='MAN', cycles=self.cycles)
                self.state['burst_cycles'] = self.cycles          # published with 'armed'
            self.inner.retry_measurement('ff_begin', self.port.ff_begin, plan)
            begun = True
            self.session_token = getattr(self.port, 'ff_token', None)
            # continuous: nothing is started before READY holds
            # fire -> playback latency budget: the instrument's (clamped) burst delay read back at arming
            self._publish(state='armed', armed=True, burst_delay_s=getattr(self.awg, 'burst_delay_s', None))
            verdict = self._collect(target_hz, max(5., 3 * self.criteria['samples'] * self.inner.sample_interval_s))
            self._publish(measured=verdict, ready=verdict['ready'], state='ready' if verdict['ready'] else 'armed')
            if not verdict['ready']:
                raise RuntimeError('Lock did not meet the ready criteria after arming: ' + str(verdict.get('reason')))
            self._observe(wave, target_hz)
            if self.trigger == 'continuous':
                # v4.19: a NORMAL end of a continuous run (stop, max_run_s, N cycles done): ff_end FIRST (the owner goes to
                # 'unwinding' and ignores later end reasons), THEN the DC-first park at the centre (its type-change
                # dropout is then harmless), then the owner's restore, then the rest. Abnormal ends keep the immediate park.
                result['ff_end_first'] = True
                result['park_first'] = True
            result['status'] = 'succeeded'
        except InterruptedError as exc:
            result['status'] = 'cancelled'
            result['errors'].append(str(exc))
        except Exception as exc:
            result['errors'].append(str(exc))
        finally:
            if (prepared and self.state.get('playback_started_unix_s') is None and self.state.get('armed')
                    and self.trigger in ('external', 'software', 'auto') and result['status'] != 'succeeded'):
                # v4.17: an armed burst may have been started by an edge that the (cached) ADC did not show yet; the
                # verdict stays 'failed', but 'no playback' is not claimed as a fact
                self.state['playback_possibly_started'] = self.trigger == 'external' or bool(self.state.get('fired'))
            if getattr(self, 'tracking_stats', None) is not None:
                self.state['tracking'] = self.tracking_stats.summary()
            self._publish(state='restoring')
            self._restore(result, begun, prepared, target_hz)
            if guardian is not None:
                try:
                    guardian.__exit__()
                except Exception as exc:
                    result['status'] = 'review_required'
                    result['errors'].append('Keepalive cleanup: ' + str(exc))
            final = 'restored' if result.get('restored') else 'review_required'
            self._publish(state=final if result['status'] != 'rejected' else 'rejected', restored=bool(result.get('restored')),
                          errors=result['errors'], result_status=result['status'])
            self.emit('ff_session_result', result)
        return result

    def _track(self, adc, centre, adc_step, profile, ff):
        """Tracking samples over the playback (human 2026-10-23 morning): commanded offset (ADC), servo DAC code, PI
        integral fraction; the FF slope in DAC codes per commanded Hz from the owner's proven multiplier. Never raises."""
        try:
            from ldcon.jobs.sweep_tracking import TrackingStats
            stats = getattr(self, 'tracking_stats', None)
            if stats is None:
                values = (((ff or {}).get('proof') or {}).get('values') or {})
                mc = values.get('multiplier_control')
                per_hz = adc_step * float(profile['frequency_hz_per_v'])
                stats = self.tracking_stats = TrackingStats(
                    ff_codes_per_hz=(float(mc) / per_hz) if isinstance(mc, (int, float)) and per_hz else None,
                    gain_scale=float(self.request['plan'].get('ff_gain_scale', 1.)))
            sample = (self.last_state or {}).get('sample') or {}
            status, cfg = sample.get('servo_status') or {}, sample.get('servo_config') or {}
            dac = (sample.get('dac') or {}).get('native_code', status.get('dac_val'))
            limit = max(abs(float(cfg.get('pid_integral_min', 0))), abs(float(cfg.get('pid_integral_max', 0)))) or None
            integral = status.get('pid_integral')
            stats.add((adc - centre) * adc_step * float(profile['frequency_hz_per_v']), dac,
                      float(integral) / limit if limit and isinstance(integral, (int, float)) else None)
            if len(stats.rows) % 20 == 0:
                self.state['tracking'] = stats.summary()
        except Exception:                 # noqa: BLE001 - measurement bookkeeping only
            pass

    def _fire(self, record):
        """Start playback now. software/auto: BTWV MTRIG on the armed MAN burst; continuous: repeating playback.
        Times are host wall-clock (not hard-synchronized to any sequencer): requested (HTTP arrival),
        forwarded (proxy job), fired (just before the SCPI write), fire_ack (after the readback)."""
        fired_unix_s = time.time()
        ack = self.awg.fire()             # v4.19: continuous = MTRIG on the N-cycle MAN burst (no type change)
        self._publish(state='fired', fired=True, fired_unix_s=fired_unix_s, fire_ack_unix_s=time.time(), awg_trigger_ack=ack,
                      trigger_requested_unix_s=(record or {}).get('requested_unix_s'),
                      trigger_forwarded_unix_s=(record or {}).get('forwarded_unix_s'))
        self.emit('ff_trigger_fired', dict(trigger=self.trigger, fired_unix_s=fired_unix_s, ack=ack, record=record))

    def _observe(self, wave, target_hz):
        """After READY: wait for the trigger (software: the once-only record from POST /sweep-trigger; auto and
        continuous: fire now; external: the edge), detect playback on the ESP ADC, mark done after the record,
        then (software/external) hold until the shot's stop, or (auto) return. continuous (v1.2, v4.17): never 'done';
        hold until the stop request, the run bound (max_run_s) or the owner's continuous cap, following the PI centre on
        the integer-period context meanwhile."""
        from ldcon.owners.native_ff_proof import hz_per_v
        profile = dict(self.port.ff_profile, frequency_hz_per_v=hz_per_v(self.port.ff_profile, self.request['plan']))
        adc_step = (self.port.policy['adc']['max_voltage'] - self.port.policy['adc']['min_voltage']) / 65535
        # v4.17 (human 2026-10-23 ~05:10Z): playback detection never below 3 x the profile's ADC noise, for any shape
        # (sr1092: 3 x 60 counts = 180 counts = 9.06 MHz at 1 GHz/V, 3.3 V/65535 counts)
        half_level = max(1., abs(self.request['plan']['step_hz']) / abs(profile['frequency_hz_per_v']) / adc_step / 2,
                         3. * float(profile.get('adc_noise_counts') or 0.))
        centre = profile['adc_center_count']
        started = fired_at = None
        continuous = self.trigger == 'continuous'
        lifetime = self.request.get('lifetime_s', 600.)
        if continuous:
            lifetime = float(self.request.get('max_run_s') or lifetime)
        deadline = self.clock() + lifetime
        follow = CentreFollower(self.port, wave['duration_s'], emit=self.emit) if continuous else None
        if self.trigger in ('auto', 'continuous'):
            self._fire(None)
            fired_at = self.clock()
        next_sample = self.clock()
        done = False
        periods = None
        while self.clock() < deadline:
            if done and self.queue.finished(self.request['request_id']):
                # v1.4: the proxy ends a shot whose record completed (a normal end, not a cancel)
                self._publish(finished_by_proxy=True)
                return
            try:
                self.inner._check_cancel()
            except InterruptedError:
                if continuous and started is not None:
                    # the stop is the normal end of a continuous run (the park at the centre follows in _restore)
                    self._continuous_end('stop_requested', started, follow)
                    return
                raise
            except RuntimeError as exc:
                if done and 'no longer owned' in str(exc):
                    # the record already played to its end; the owner ending FF afterwards (e.g. its lifetime) is the
                    # normal end of this shot, not a failure (2026-10-22 R5)
                    self._publish(ff_ended_after_playback=str(exc))
                    return
                if continuous and started is not None and 'continuous lifetime cap' in str(exc):
                    self._continuous_end('owner_cap', started, follow)
                    return
                raise
            if self.trigger == 'software' and fired_at is None:
                record = self.queue.trigger_record(self.request['request_id'])
                if record is not None:
                    self._fire(record)
                    fired_at = self.clock()
            if self.clock() >= next_sample:
                next_sample = self.clock() + self.inner.sample_interval_s
                f, ok, adc = self._sample()
                try:
                    self.port.ff_heartbeat()
                    ff = self.port.ff_status()
                except MeasurementUnavailable:
                    ff = None
                if ff is not None and ff['state'] != 'active':
                    if done:
                        self._publish(ff_ended_after_playback=str(ff.get('reason')))
                        return
                    if continuous and started is not None and 'continuous lifetime cap' in str(ff.get('reason')):
                        self._continuous_end('owner_cap', started, follow)
                        return
                    raise RuntimeError('Owner ended FF sweep: ' + str(ff.get('reason')))
                playing = adc is not None and abs(adc - centre) >= half_level
                if playing and started is None:
                    started = self.clock()
                    self._publish(state='playing', playback_started_unix_s=time.time())
                    if follow is not None:
                        follow.start(time.time())
                if adc is not None and started is not None:
                    self._track(adc, centre, adc_step, profile, ff)
                if adc is not None:
                    self.state['samples'] += 1
                    self.emit('ff_sample', dict(frequency_hz=f, valid=ok, adc=adc,
                                                commanded_offset_hz=(adc - centre) * adc_step * profile['frequency_hz_per_v'],
                                                error_hz=(None if f is None else f - target_hz - (adc - centre) * adc_step * profile['frequency_hz_per_v']),
                                                playing=playing, elapsed_s=None if started is None else self.clock() - started))
                if continuous and started is not None and not playing and getattr(self, 'cycles', None) \
                        and self.clock() - started >= self.cycles * wave['duration_s']:
                    # the burst played its N records and idles at the centre: a normal end
                    self._continuous_end('cycles_completed', started, follow)
                    return
                if continuous and started is not None:
                    count = int((self.clock() - started) // wave['duration_s'])
                    follow.feed(time.time(), self.last_state)
                    if count != periods:
                        periods = count
                        self._publish(periods_played=count, period_s=wave['duration_s'], context=follow.summary())
                # v4.17 (human approval 2026-10-23): no +1 s host margin -- done once the record duration has elapsed
                # since the detected start AND the ADC is back at the centre (the owner/ADC fact)
                if not continuous and not done and started is not None and self.clock() - started >= wave['duration_s'] and not playing:
                    done = True
                    self._publish(state='done')
                    if self.trigger == 'auto':
                        return
                if fired_at is not None and started is None and self.clock() - fired_at > wave['duration_s'] + 2.:
                    raise RuntimeError('Playback never left the ADC centre after the trigger; check the AWG route')
            # Poll the software trigger finely while waiting for it; otherwise idle at the sample interval.
            self.sleep(.02 if self.trigger == 'software' and fired_at is None else self.inner.sample_interval_s)
        if continuous and started is not None:
            self._continuous_end('max_run_s', started, follow)
            return
        raise RuntimeError('FF proxy lifetime expired before the shot was stopped')

    def _continuous_end(self, reason, started, follow):
        run_s = self.clock() - started
        period = None
        try:
            period = float(follow.period_s)
        except (AttributeError, TypeError, ValueError):
            pass
        self._publish(stop_reason=reason, run_s=round(run_s, 3),
                      periods_played=int(run_s // period) if period else self.state.get('periods_played'),
                      context=follow.summary() if follow is not None else None)
        self.emit('ff_continuous_end', dict(reason=reason, run_s=run_s))

    def _restore(self, result, begun, prepared, target_hz):
        ff_uncertain = getattr(self.port, 'uncertain_output', False)
        ff_restored = False
        ff_end_sent = False
        if result.get('ff_end_first') and begun and not ff_uncertain:
            try:
                self.port.ff_end(reason='continuous run stopped')
                ff_end_sent = True
                self.emit('ff_end_before_park', dict(reason=self.state.get('stop_reason')))
            except Exception as exc:              # the park below still runs; the restore wait reports the outcome
                result['errors'].append('FF end before the park: ' + str(exc))
        self.inner.park_if_playing(result, begun, prepared)
        owner_ff = None
        if begun and not ff_uncertain and getattr(self, 'session_token', None) and hasattr(self.port, 'ff_owner_state'):
            # The owner may have ended FF on its own (2026-10-22 18:59Z R4: "Physical ADC invalid or invalid counter
            # advanced"): its record for THIS token says restored -> FF is verified restored; run the normal verified
            # AWG restore (DC rest + release), not review.
            owner_ff = self.port.ff_owner_state()
            if not (owner_ff and owner_ff.get('token') == self.session_token and owner_ff.get('state') == 'restored'):
                owner_ff = None
        if owner_ff is not None:
            ff_restored = True
            self.emit('ff_restore', dict(owner_ff, source='owner ended FF itself'))
            result['errors'].append('FF ended by the owner: %s' % owner_ff.get('reason'))
        elif begun and not ff_uncertain:
            try:
                if not ff_end_sent:
                    self.port.ff_end(reason='proxy session finished')
                deadline = self.clock() + self.port.unwind_timeout_s
                while True:
                    try:
                        self.port.heartbeat()
                        state = self.port.ff_status()
                    except MeasurementUnavailable:
                        if self.clock() >= deadline:
                            raise RuntimeError('FF restoration status remained unavailable until its deadline')
                        self.sleep(self.inner.sample_interval_s)
                        continue
                    self.emit('ff_restore', state)
                    if state['state'] == 'restored':
                        ff_restored = True
                        break
                    if state['state'] == 'fault' or self.clock() >= deadline:
                        raise RuntimeError('FF restoration was not verified within its deadline')
                    self.sleep(self.inner.sample_interval_s)
            except Exception as exc:
                result['errors'].append('FF cleanup: ' + str(exc))
        elif not begun and not ff_uncertain:
            ff_restored = True
        awg_restored = False
        if prepared and ff_restored:
            try:
                awg_restored = self.awg.close()
            except Exception as exc:
                result['errors'].append('AWG cleanup: ' + str(exc))
                # v4.4: a failed restore (e.g. the native keepalive failed) must not leave CH2 armed or playing: park at
                # the DC centre (SDG-only, independent of the native owner); leases retained for review.
                try:
                    if self.awg.park_centre():
                        result['errors'].append('AWG parked at the DC centre after the failed restore (review)')
                except Exception as exc2:
                    result['errors'].append('AWG park at the centre: ' + str(exc2))
        elif not prepared:
            awg_restored = True
        elif prepared and not ff_restored:
            try:
                if self.awg.park_centre():
                    result['errors'].append('AWG parked at the DC centre (FF not restored; leases retained for review)')
            except Exception as exc:
                result['errors'].append('AWG park at the centre: ' + str(exc))
        settled = None
        if ff_restored and awg_restored and not (ff_uncertain or getattr(self.port, 'uncertain_output', False)
                                                 or getattr(self.awg, 'uncertain_output', False)):
            # v1.4: the restore is verified here -> the shot's result exists; the settle check below only follows it
            self._publish(state='settling', hardware_restored=True, result_status=result['status'],
                          errors=list(result['errors']), settle_started_unix_s=time.time(),
                          settle_budget_s=max(5., 3 * self.criteria['samples'] * self.inner.sample_interval_s) + 5.)
        if ff_restored and awg_restored:
            # The settle check after a stop is part of the restoration evidence: the stop itself must not cut it short.
            settled = self._collect(target_hz, max(5., 3 * self.criteria['samples'] * self.inner.sample_interval_s), cancellable=False)
        uncertain = bool(ff_uncertain or getattr(self.port, 'uncertain_output', False) or getattr(self.awg, 'uncertain_output', False))
        restored = bool(ff_restored and awg_restored and not uncertain)
        result.update(ff_restored=ff_restored, awg_restored=awg_restored, settled=settled, uncertain=uncertain,
                      settled_ok=bool(settled and settled['ready']), restored=restored)
        if not restored and result['status'] in ('succeeded', 'cancelled'):
            # review (and the lock worker ending the lock) only for an uncertain or unrestored state
            result['status'] = 'review_required'
        elif restored and not result['settled_ok'] and result['status'] == 'succeeded':
            # 2026-10-22 18:39Z (proxy 634a8488): FF and AWG restored, nothing uncertain, but the post-shot settle check
            # missed the criteria (max|err| 16.6 MHz, span 25.4 MHz vs 5 MHz). That is a REPORTED result, not a review
            # case: the lock continues and later shots are allowed.
            result['status'] = 'completed_unsettled'
        self.state['measured_after'] = settled
        self.state['hardware_restored'] = restored
        result['hardware_restored'] = restored
        result['awg_review'] = self.awg.review_details()


RECENTRE_SETTLE_S = 30.     # how long the integral may take to relax below the start limit after the move


def recentre_before_ff(port, laser, target_hz, *, clock, sleep, cancel, emit, settle_s=RECENTRE_SETTLE_S):
    """Opt-in automatic re-centre before ff_begin (rule F: the SAME tune.operating_point procedure, run as this lock's
    own action): when |integral| exceeds the FF profile's start_integral_fraction_max, move the native PI centre to
    'integral_neutral' in bounded, read-back steps (the operating-point axis limits bound the total move), wait for the
    integral to relax, and refuse the shot if it is still above the start limit. 2026-10-22: sr1092 drifts ~-7
    codes/min; R4 needed a manual re-centre (control_offset 33042 -> 33027, integral 1.00 -> 0.08)."""
    from ldcon.jobs.operating_point import NativeAdapter, OperatingPointMove, axis_map, validate
    limit = float(ff_profile(laser)['start_integral_fraction_max'])
    # v4.17 (human approval 2026-10-23 ~04:45Z): one step may use the owner's host step budget (~41-44 codes on sr1092:
    # 50 uA / one code) instead of 1 code (9 codes took 9.3 s in ce388df2)
    from ldcon.jobs.operating_point import _NativePolicy
    budget = max(1, min(64, int(_NativePolicy(port.policy).max_step)))
    adapter = NativeAdapter(port, laser, heartbeat=port.heartbeat, max_step_codes=budget, sleep=sleep)
    before = adapter.integral_fraction()
    if before <= limit:
        return dict(status='not_needed', integral_fraction=before, limit=limit)
    emit('ff_recentre_start', dict(integral_fraction=before, limit=limit))
    p = validate(dict(moves=[dict(axis='current', mode='locked_reference', target=dict(reference='integral_neutral'))],
                      verify_samples=0, max_step_codes=budget))
    try:
        moved = OperatingPointMove(adapter, p, axis_map(laser), clock=clock, sleep=sleep, cancel=cancel, emit=emit,
                                   lock_target_hz=target_hz).run()
    except Exception as exc:
        if getattr(port, 'uncertain_output', False):
            raise
        out = dict(status='refused', reason='Automatic re-centre before FF failed: %s' % exc, integral_fraction=before, limit=limit)
        emit('ff_recentre_result', out)
        return out
    deadline = clock() + settle_s
    after = adapter.integral_fraction()
    while after > limit and clock() < deadline:
        port.heartbeat()
        sleep(.5)
        after = adapter.integral_fraction()
    out = dict(status='recentred' if after <= limit else 'refused', integral_fraction_before=before, integral_fraction=after,
               limit=limit, moves=moved.get('moves'))
    if after > limit:
        out['reason'] = ('Re-centre insufficient: integral fraction %.2f still above the FF start limit %.2f after the move'
                         % (after, limit))
    emit('ff_recentre_result', out)
    return out


def _owner_session(port, request, queue, *, card_cancel, emit, sleep):
    """Forward attachment lifecycle; the worker never opens or configures the AWG."""
    session = port.assembly_start(request, target_hz=request['target_hz'])
    session_id = session['session_id']
    trigger_sent = cancel_sent = finish_sent = False
    while True:
        status = session.get('status')
        if status:
            queue.write_status(request['request_id'], status)
            emit('ff_proxy_state', status)
        if session.get('result') is not None:
            return session['result']
        if session.get('state') in ('uncertain', 'review_required'):
            raise DeviceError('Laser owner retains an unresolved FF assembly session')
        if not cancel_sent and (card_cancel() or queue.cancelled(request['request_id'])):
            port.assembly_cancel(session_id)
            cancel_sent = True
        if not trigger_sent:
            trigger = queue.trigger_record(request['request_id'])
            if trigger is not None:
                port.assembly_trigger(session_id, trigger)
                trigger_sent = True
        if not finish_sent and queue.finished(request['request_id']):
            port.assembly_finish(session_id)
            finish_sent = True
        port.heartbeat()
        sleep(.1)
        session = port.assembly_status(session_id)


def attachment(lock_directory, laser, *, card_cancel, emit, clock=time.monotonic, sleep=time.sleep):
    """The ``feedforward`` hook for run_native_current on an FF-route laser."""
    from ldcon.jobs.native_ff_sweep import NativeFFAttachment, RequestJournal
    queue = RequestQueue(lock_directory)
    journal = RequestJournal(Path(lock_directory) / 'ff-requests' / 'journal', Path(lock_directory).name, digest(ff_profile(laser)))

    class Hook(NativeFFAttachment):
        def __call__(self, port, *, online_busy=False):
            request = self.receive()
            if request is None:
                return None
            try:
                path = self.journal.claim(request, now=self.wall_clock())
            except ValueError as exc:
                # refused at the journal claim: nothing was touched -> the pre-shot state stands (labscript abort check)
                queue.write_status(request['request_id'], dict(state='rejected', errors=[str(exc)], armed=False, ready=False, restored=True))
                return dict(status='rejected', errors=[str(exc)], request_id=request['request_id'], restored=True)
            if online_busy:
                result = dict(status='rejected', errors=['An online PID comparison is active'])
            elif not port.active:
                result = dict(status='rejected', errors=['An existing active native lock is required'])
            else:
                try:
                    result = _owner_session(port, request, queue, card_cancel=card_cancel, emit=emit, sleep=sleep)
                except Exception as exc:
                    result = dict(status='review_required', errors=[str(exc)])
                    queue.write_status(request['request_id'], dict(state='review_required', errors=[str(exc)], armed=False, ready=False, restored=False))
            if result.get('status') == 'rejected':
                # a rejection happens before any output was armed (validation, busy, inactive lock): pre-shot state stands
                clean = not getattr(port, 'uncertain_output', False)
                queue.write_status(request['request_id'], dict(state='rejected', errors=result.get('errors', []), armed=False, ready=False, restored=clean))
            self.journal.finish(path, result)
            return dict(result, request_id=request['request_id'])

    return Hook(queue.receive, journal, lambda: None, clock=clock, sleep=sleep, cancel=card_cancel, emit=emit)


# ---------------------------------------------------------------- proxy card side
def find_lock_job(directory, laser, operation='servo.native_current'):
    """The running, locked job of ``operation`` for this laser (same jobs/<laser>/ folder)."""
    from ldcon.core.records import read_json
    for path in sorted(Path(directory).parent.glob('*/job.json')):
        meta = read_json(path, {})
        if meta.get('id') == Path(directory).name:
            continue
        if not any(c.get('operation', c.get('card')) == operation for c in meta.get('cards', [])):
            continue
        if (meta.get('laser') or {}).get('id') != laser['id']:
            continue
        progress = read_json(path.parent / 'progress.json', {})
        if progress.get('status') in ('running', 'acquiring', 'finishing') and (
                progress.get('controller_state') == 'locked' or lock_report_locked(path.parent)):
            return path.parent, meta, progress
    raise DeviceError('A running, locked %s job is required for the staircase' % operation)


LOCK_REPORT_FRESH_S = 30.


def lock_report(lock_dir):
    """The lock worker's own per-cycle report (lock-target.json), or None when absent or older than 30 s."""
    from ldcon.core.records import read_json
    if lock_dir is None:
        return None
    live = read_json(Path(lock_dir) / 'lock-target.json', {}) or {}
    stamp = live.get('updated_unix_s')
    return live if isinstance(stamp, (int, float)) and 0 <= time.time() - stamp <= LOCK_REPORT_FRESH_S else None


def lock_report_locked(lock_dir):
    """Locked by the lock's own fresh report (same locked source as lock_target.running_lock): the sr422 steady-lock
    controller_state is a display state that flips to waiting_for_measurement/tracking on wavemeter dropouts while
    the lock keeps running (2026-10-22 18:04Z)."""
    live = lock_report(lock_dir)
    return bool(live) and live.get('state') == 'locked'


def lock_target_hz(lock_meta, progress, lock_dir=None, laser=None):
    """The running lock's target. Order (2026-10-22, follow-laser-target locks have null card targets):
    1. the lock's fresh lock-target.json target_frequency_hz (the final target); during a target ramp: refuse (busy);
    2. laser.target (or a pinned card target) via lock_target.resolve_target;
    3. the progress / card parameters (legacy pinned locks)."""
    live = lock_report(lock_dir)
    if live:
        if live.get('ramp_active'):
            raise DeviceError('busy: a lock-target ramp is in progress (ramped %s Hz -> %s Hz); retry after it settles'
                              % (live.get('ramped_target_hz'), live.get('target_frequency_hz')))
        if live.get('target_frequency_hz'):
            return float(live['target_frequency_hz'])
    params = (lock_meta.get('cards') or [{}])[0].get('params', {}) or {}
    if laser is not None:
        from ldcon.jobs.lock_target import resolve_target
        resolved = resolve_target(laser, params)
        if resolved:
            return float(resolved)
    target_hz = progress.get('target_frequency_hz') or params.get('target_frequency_hz')
    if not target_hz:
        nm = params.get('target_wavelength_nm')
        target_hz = 299792458e9 / nm if nm else None
    if not target_hz:
        raise DeviceError('Lock job target is unknown; cannot judge the ready criteria')
    return float(target_hz)


def ready_from_lock(lock_meta, card_ready):
    """Shot READY/settle criteria: the card's explicit values over defaults taken from the LOCK's own tolerance
    (tolerance = lock tolerance_hz, stability = 2 x lock tolerance_hz); READY_DEFAULTS (5 MHz) only when the lock has
    no tolerance. 2026-10-22: sr1092's lock noise (sigma ~6 MHz, +-10 MHz typical, lock tolerance 10 MHz) cannot meet
    the 5 MHz defaults."""
    params = (lock_meta.get('cards') or [{}])[0].get('params', {}) or {}
    tol = params.get('tolerance_hz')
    base = dict(READY_DEFAULTS)
    if type(tol) in (int, float) and math.isfinite(tol) and tol > 0:
        base.update(tolerance_hz=float(tol), stability_hz=2. * float(tol))
    card_ready = dict(card_ready or {})
    if card_ready == READY_DEFAULTS:
        # The deployed web normalizes a partial `ready` to the full legacy defaults before storing the job parameters
        # (modulation_sweep.parameters), so an exact copy of the 5 MHz defaults cannot be told apart from "unset":
        # it takes the lock-derived defaults. Any other explicit value wins.
        card_ready = {}
    return ready_criteria({**base, **card_ready})


RESTORE_MARGIN_S = 10.       # the proxy's budget for unwind + AWG restore + settle after the record
AUTO_START_WINDOW_S = 20.    # auto/continuous: arm -> ff_begin centre gate -> READY, then fire at once


class NativeFFRoute:
    """sr1092: proxy for the native FF session inside the servo.native_current job."""
    ready_from_lock_tolerance = True

    def settle_budget_s(self):
        """v1.4: the session's post-restore settle collection (<= max(5 s, 3 x samples x 0.1 s)) + margin."""
        return 10.

    def start_window(self, plan, p, shot):
        """v4.9 (owner v5.5): the FF lifetime must cover arm -> READY -> trigger wait + record. 2026-10-22 19:16:50Z R5
        (software trigger, 12.3 s record) played the full record and then ended 'failed' on the owner's "FF waveform
        lifetime expired" (lifetime = record + heartbeat timeout, counted from ff_begin). The window is bounded by the
        shot's own duration budget and the profile's max_duration_s."""
        from ldcon.owners.native_ff_proof import duration_s
        profile = ff_profile(self.laser)
        record = duration_s(plan)
        trigger = {'software': 'software', 'external': 'external', 'external_rising': 'external'}[shot['trigger_mode']] if shot else p['trigger']
        budget = float(p['duration_s']) - record - RESTORE_MARGIN_S
        if trigger in ('auto', 'continuous'):
            budget = min(budget, AUTO_START_WINDOW_S)
        window = max(0., min(budget, float(profile.get('max_duration_s', record)) - record))
        return dict(plan, start_window_s=round(window, 3))
    lock_operation = 'servo.native_current'
    axis = 'current_modulation'
    def __init__(self, laser):
        self.laser = laser
    def settings_revision(self):
        return digest(ff_profile(self.laser))
    def make_plan(self, p, shot):
        return plan_from_card(p, shot)


def run_ff_proxy(directory, laser, card, p, *, clock, sleep, cancel, emit, publish):
    return run_locked_proxy(directory, laser, card, p, NativeFFRoute(laser), clock=clock, sleep=sleep, cancel=cancel, emit=emit, publish=publish)


ANALYSIS_WAIT_MAX_S = 75.     # > the analysis' own deadline (max_runtime 45 s + VM wait 3.5 s + 10 s)


def await_analysis(directory, outcome, *, now=time.time, sleep=time.sleep, limit_s=ANALYSIS_WAIT_MAX_S):
    """Wait (bounded) until this proxy's analysis child has a result (ready/unavailable), its process is gone, or its
    deadline passed. Never raises; returns what it saw (for the job log)."""
    try:
        if ((outcome or {}).get('analysis') or {}).get('state') != 'pending':
            return dict(waited_s=0., state=((outcome or {}).get('analysis') or {}).get('state'))
        from ldcon.web.shot_analysis_api import current_status
        started = now()
        while True:
            status = current_status(Path(directory)) or {}
            state = status.get('state')
            pid = status.get('pid')
            if isinstance(pid, int) and pid > 0:
                try:
                    os.waitpid(pid, os.WNOHANG)          # reap our own child (ChildProcessError if not ours)
                except (ChildProcessError, OSError):
                    pass
            untracked = not isinstance(status.get('pid'), int) and now() - started > 3.   # no child pid was recorded
            if state not in ('pending', 'running') or untracked or now() - started > limit_s:
                return dict(waited_s=round(now() - started, 3), state=state, reason=status.get('reason'),
                            untracked=untracked or None)
            sleep(.2)
    except Exception as error:      # noqa: BLE001 - never fail the shot because of the analysis
        return dict(waited_s=None, state=None, reason='await_analysis: %s' % error)


def run_locked_proxy(directory, laser, card, p, route, *, clock, sleep, cancel, emit, publish):
    """The proxy card job: file the request into the running lock job of ``route.lock_operation``, mirror the
    session's measured state on this job's shot_sweep, forward the once-only software trigger, cancel on stop."""
    from ldcon.jobs.modulation_sweep import validate_request, PROTOCOL
    from ldcon.jobs.locked_triangle import card_parameters
    original_shape = p.get('shape')
    p = card_parameters(laser, p)
    from ldcon.jobs.progress import CancelledRun
    directory = Path(directory)
    raw_emit = emit

    def emit(kind, payload):
        # The worker's progress sink raises CancelledRun ONCE on the first event after the stop request. The proxy
        # must not exit there: a stop asks the lock-side session to restore, and labscript waits for restored:true
        # on THIS job (2026-10-22 d0602dd8 ended 'cancelled' with restored:false although the owner had restored).
        try:
            raw_emit(kind, payload)
        except CancelledRun:
            pass
    request_spec = p.get('shot_sweep')
    shot = validate_request(request_spec) if request_spec else None
    if shot and shot['axis'] != route.axis:
        raise ValueError('Shot axis does not match the card')
    plan = route.make_plan(p, shot)
    ramp_levels = plan.pop('ramp_levels', None) if isinstance(plan, dict) else None   # v4.17 host annotation only
    if getattr(route, 'start_window', None):
        plan = route.start_window(plan, p, shot)
    continuous = (not shot) and p.get('trigger') == 'continuous'
    record_levels = plan.get('levels') or plan.get('levels_hz') or []
    lead_n, tail_n = (int(ramp_levels['lead']), int(ramp_levels['tail'])) if ramp_levels else (0, 0)
    wanted_levels = record_levels[lead_n:len(record_levels) - tail_n]
    lock_dir, lock_meta, progress = find_lock_job(directory, laser, route.lock_operation)
    target_hz = lock_target_hz(lock_meta, progress, lock_dir, laser)
    request_id = request_spec['request_id'] if request_spec else hashlib.sha256((directory.name + ':ff').encode()).hexdigest()[:32]
    queue = RequestQueue(lock_dir)
    now = time.time()
    if shot:
        trigger = {'software': 'software', 'external': 'external', 'external_rising': 'external'}[shot['trigger_mode']]
    else:
        trigger = p['trigger']
    request = dict(request_id=request_id, job_id=lock_meta['id'], settings_revision=route.settings_revision(),
                   expires_unix_s=now + REQUEST_TTL_S, plan=plan, trigger=trigger, route=route.lock_operation,
                   recentre=bool(p.get('recentre_before_ff', True)) if getattr(route, 'ready_from_lock_tolerance', False) else False,
                   ready=(ready_from_lock(lock_meta, p.get('ready')) if getattr(route, 'ready_from_lock_tolerance', False)
                          else (p.get('ready') or {})), target_hz=float(target_hz), lifetime_s=float(p['duration_s']),
                   proxy_job=directory.name, plan_sha256=request_spec['plan_sha256'] if request_spec else digest(plan),
                   schema=PROTOCOL)
    if ramp_levels:
        request['ramp_levels'] = ramp_levels
    if continuous:
        # v1.2 continuous triangle: the run is bounded by the card's duration_s minus the restore margin (the web sets
        # duration_s = max_run_s + RESTORE_MARGIN_S); arming counts into it.
        request['max_run_s'] = max(1., float(p['duration_s']) - RESTORE_MARGIN_S)
    echo = {key: request_spec[key] for key in ('schema', 'request_id', 'plan_sha256')} if request_spec else dict(schema=PROTOCOL, request_id=request_id, plan_sha256=request['plan_sha256'])
    trigger_file = directory / 'sweep-trigger.json'          # written once by the dashboard (POST /sweep-trigger)
    forwarded = dict(record=None, refused=None)
    seen = {}
    sent = dict(outcome=None)                         # v1.4: the outcome, published at the verified restore
    proxy_deadline_unix_s = time.time() + float(p['duration_s']) + 60.

    def report(state, **extra):
        # trigger_source (2026-10-22, labscript TTB verify): the lock session's own value (its armed readback) when it
        # has reported one, else the mapping of the requested trigger. Never null for a triggered shot.
        source = extra.pop('trigger_source', None) or seen.get('trigger_source') or {
            'software': 'MAN', 'auto': 'MAN', 'external': 'EXT', 'continuous': None}.get(request['trigger'])
        value = dict(echo, state=state, armed=extra.get('armed', False), restored=extra.get('restored', False),
                     trigger_source=source,
                     simulated=False, trigger_mode=shot['trigger_mode'] if shot else request['trigger'], trigger=request['trigger'],
                     lock_job=lock_meta['id'], fired=bool(extra.get('fired')), fired_unix_s=extra.get('fired_unix_s'),
                     fire_ack_unix_s=extra.get('fire_ack_unix_s'), awg_trigger_ack=extra.get('awg_trigger_ack'),
                     playback_started_unix_s=extra.get('playback_started_unix_s'),
                     trigger_requested_unix_s=(forwarded['record'] or {}).get('requested_unix_s'),
                     trigger_forwarded_unix_s=(forwarded['record'] or {}).get('forwarded_unix_s'),
                     trigger_refused=forwarded['refused'],
                     auto_finish=True, proxy_protocol=2, proxy_deadline_unix_s=proxy_deadline_unix_s,
                     **{k: v for k, v in extra.items() if k not in ('armed', 'restored', 'fired', 'fired_unix_s', 'fire_ack_unix_s', 'awg_trigger_ack', 'playback_started_unix_s')})
        if sent['outcome'] is not None and 'outcome' not in value:
            value['outcome'] = sent['outcome']          # once published, the outcome stays on every later echo
        publish(value)
    from ldcon.jobs.sweep_verdict import DEFER_MARGIN_S, Facts, native_rows, record_seconds, settle_block
    facts = Facts(record_seconds(plan), wanted_levels,
                  calibrate_only=bool(plan.get('calibrate_only')), continuous=continuous,
                  shape=(shot or {}).get('shape', 'staircase') if shot else original_shape or 'staircase')
    route_budget = route.settle_budget_s() if hasattr(route, 'settle_budget_s') else 30.

    def fields(status):
        status = status or {}
        return dict(armed=bool(status.get('armed')), restored=bool(status.get('restored') or status.get('hardware_restored')),
                    ready=bool(status.get('ready')), measured=status.get('measured'), measured_after=status.get('measured_after'),
                    errors=status.get('errors', []), fired=status.get('fired'), fired_unix_s=status.get('fired_unix_s'),
                    fire_ack_unix_s=status.get('fire_ack_unix_s'), awg_trigger_ack=status.get('awg_trigger_ack'),
                    playback_started_unix_s=status.get('playback_started_unix_s'),
                    **{k: status[k] for k in ('calibration', 'residuals', 'programmed', 'result_status', 'hardware_restored',
                                              # v4.22/v3.14 (campaign 2026-10-23): continuous runs expose these LIVE
                                              'periods_played', 'period_s', 'context', 'stop_reason', 'tec_moves',
                                              'calibrated', 'playback_detection', 'tracking')
                       if status.get(k) is not None})

    def rows_for(status):
        if continuous or facts.shape == 'arbitrary':
            return None
        if facts.residuals is None and not (status or {}).get('residuals') and facts.playback_started_unix_s is not None \
                and plan.get('levels'):
            rows = native_rows(lock_dir, facts.playback_started_unix_s, facts.record_s, plan['levels'])
            # v4.17: residuals per REQUESTED level; the approach/leave ramp levels are dropped
            return None if rows is None else rows[lead_n:len(rows) - tail_n]
        return None

    def with_tracking(outcome):
        # human 2026-10-23 morning: report poor tracking at once (outcome.tracking), correct it mechanically when a rule
        # applies (a stored per-shot recommendation, never applied to the laser definition), else ask the agent
        from ldcon.jobs.sweep_tracking import ask_agent, assess, store
        try:
            native = route.lock_operation == 'servo.native_current'
            abort = (ff_profile(laser).get('abort_error_hz') if native else
                     ((laser['devices'][laser['interfaces']['servo_modulation']['device']]['control_settings']
                       .get('locked_sweep') or {}).get('limits') or {}).get('abort_error_hz'))
            tracking = assess('native' if native else 'rig', outcome.get('verdict'), outcome.get('reason'),
                              outcome.get('owner_errors'), facts.tracking, abort_error_hz=abort,
                              rms_error_hz=outcome.get('rms_error_hz'), max_abs_error_hz=outcome.get('max_abs_error_hz'))
            if tracking['state'] != 'ok':
                tracking['stored'] = store(directory, tracking)
                if not tracking['mechanical']:
                    tracking['agent'] = ask_agent(directory, card['id'], request_id, tracking)
                emit('sweep_tracking', tracking)
            outcome['tracking'] = tracking
        except Exception as error:          # noqa: BLE001 - tracking never changes or blocks the verdict
            outcome['tracking'] = dict(state='unknown', reason='tracking assessment failed: %s' % str(error)[:200])
        return outcome

    def with_analysis(outcome, status):
        outcome = with_tracking(outcome)
        # sweep API v1.5 (human 2026-10-23): the post-shot analysis (rule F card analyze.shot_sweep, shot_analysis.py)
        # starts as a DETACHED child the moment the outcome exists; the proxy never waits for it. outcome.analysis is
        # 'pending' here; the web reads the current state from <this job>/shot-analysis/status.json.
        from ldcon.jobs.shot_analysis import launch
        programmed = (status or {}).get('programmed') or (last or {}).get('programmed')
        # v4.18: a continuous triangle is analysed in raw mode (no shot staircase); arbitrary shots carry their shape in
        # the shot plan (raw mode as well)
        shaped = dict(plan, shape='continuous_triangle') if request['trigger'] == 'continuous' else plan
        outcome['analysis'] = launch(directory, request=dict(
            plan=shaped, target_hz=float(target_hz), lock_dir=str(lock_dir), lock_job=lock_meta['id'], trigger=request['trigger'],
            request_id=request_id, plan_sha256=request['plan_sha256'], record_s=facts.record_s, programmed=programmed,
            route=route.lock_operation, calibrate_only=bool(plan.get('calibrate_only'))), outcome=outcome)
        return outcome

    def final_settle(status, state):
        early = (sent['outcome'] or {}).get('settle') or {}
        return settle_block(status, state, started_unix_s=early.get('started_unix_s'), deadline_unix_s=early.get('deadline_unix_s'))

    def give_up(message):
        # No verified final state from the lock-side session. Before the restore was verified: failed, restore NOT
        # verified. After (outcome already published): the verdict stands, the settle is 'unknown'.
        if sent['outcome'] is not None:
            outcome = dict(sent['outcome'], settle=final_settle(last, 'unknown'), settled=None)
            outcome['settle']['reason'] = message
        else:
            outcome = facts.outcome(dict(state='review_required', restored=False, errors=[message], result_status='failed'))
        sent['outcome'] = outcome
        report((last or {}).get('state', 'unknown'), **dict(fields(last), restored=outcome['restored']), outcome=outcome)
        emit('sweep_outcome', outcome)
        raise DeviceError(message)
    queue.submit(request)
    emit('ff_request_submitted', dict(request_id=request_id, lock_job=lock_meta['id'], plan=plan, trigger=request['trigger']))
    report('submitted')
    started = clock()
    cancelled_at = None          # the stop (or, v1.4, the proxy's own finish) forwarded to the lock-side session
    stop_at = None               # a stop received on THIS job (a genuine abort: before or during playback)
    play_seen_at = None          # first fired/playing state seen (bounds how long a stop is held back)
    held = False
    last = None
    while True:
        status = queue.read_status(request_id)
        if request['trigger'] == 'software' and forwarded['record'] is None and forwarded['refused'] is None and trigger_file.exists():
            from ldcon.core.records import read_json
            record = read_json(trigger_file, None)
            if record is not None:
                if (record.get('request_id') == request_id and record.get('plan_sha256') == request['plan_sha256']
                        and last is not None and last.get('state') == 'ready' and last.get('armed') and stop_at is None):
                    forwarded['record'] = queue.trigger(request_id, dict(record, forwarded_unix_s=time.time(), proxy_job=directory.name))
                    emit('ff_trigger_forwarded', forwarded['record'])
                else:
                    forwarded['refused'] = 'shot not ready/armed or identity mismatch at trigger time (state=%s)' % (last or {}).get('state')
                    emit('ff_trigger_refused', dict(record=record, reason=forwarded['refused']))
                report((last or {}).get('state', 'submitted'), armed=bool((last or {}).get('armed')), ready=bool((last or {}).get('ready')),
                       measured=(last or {}).get('measured'), errors=(last or {}).get('errors', []))
        if status is not None and status.get('trigger_source'):
            seen['trigger_source'] = status['trigger_source']
        if status != last and status is not None:
            last = status
            facts.observe(status)
            if status.get('programmed'):
                facts.record_s = record_seconds(plan, status['programmed'])
            if status.get('state') in ('fired', 'playing') and play_seen_at is None:
                play_seen_at = clock()
            emit('ff_proxy_status', status)
            if status.get('state') == 'settling' and sent['outcome'] is None:
                # v1.4 (human 2026-10-23): the hardware restore is verified -> the shot's result exists. Publish the
                # verdict NOW (BLACS transition_to_manual may finish); the lock's settle check follows in outcome.settle.
                begun = status.get('settle_started_unix_s') or time.time()
                budget = status.get('settle_budget_s') or route_budget
                settle = settle_block(status, 'settling', started_unix_s=begun, deadline_unix_s=begun + float(budget))
                sent['outcome'] = with_analysis(facts.outcome(status, residual_rows=rows_for(status), settle=settle), status)
                report('settling', **fields(status), outcome=sent['outcome'])
                emit('sweep_outcome', sent['outcome'])
                continue
            if status.get('state') in ('restored', 'review_required', 'rejected'):
                if sent['outcome'] is None:
                    outcome = with_analysis(facts.outcome(status, residual_rows=rows_for(status)), status)
                else:
                    after = (status.get('measured_after') or {}).get('ready')
                    state = ('review_required' if status['state'] == 'review_required' else
                             'settled' if after else 'unsettled' if after is False else 'unknown')
                    settle = final_settle(status, state)
                    if state == 'review_required':
                        settle['reason'] = '; '.join(status.get('errors', []))
                    outcome = dict(sent['outcome'], settle=settle, settled=settle['ready'])      # the verdict never changes
                sent['outcome'] = outcome
                report(status.get('state'), **fields(status), outcome=outcome)
                emit('sweep_outcome', outcome)
                # v4.20/v3.12 (2026-10-23 06:17Z, sr422 2883041...): the detached analysis child died ~1 s after this
                # proxy job ended (every analysis that finished before its proxy ended is ready). The proxy now waits,
                # bounded, for its own analysis result before the job ends; the published outcome is unchanged.
                emit('shot_analysis_wait', await_analysis(directory, outcome))
                errors = '; '.join(status.get('errors', []))
                if status['state'] == 'rejected':
                    raise DeviceError('FF request rejected by the lock job: ' + errors)
                if status['state'] == 'review_required':
                    raise DeviceError('FF session requires review: ' + errors)
                if outcome['verdict'] == 'failed':
                    if outcome['restored']:
                        # restored and verified (lock continues), but the shot itself did not succeed
                        raise DeviceError('FF shot failed (FF and AWG restored, lock continues): ' + outcome['reason'])
                    raise DeviceError('FF shot failed, restore NOT verified (review): ' + outcome['reason'])
                if outcome['verdict'] == 'cancelled':
                    job_status = 'cancelled'
                else:
                    unsettled = outcome['settle'].get('state') == 'unsettled' or status.get('result_status') == 'completed_unsettled'
                    job_status = 'completed_unsettled' if unsettled else 'succeeded'
                if job_status == 'completed_unsettled':
                    emit('ff_settle_unmet', dict(request_id=request_id, measured_after=status.get('measured_after')))
                return dict(status=job_status, request_id=request_id,
                            lock_job=lock_meta['id'], restored=True, measured=status.get('measured'), measured_after=status.get('measured_after'),
                            samples=status.get('samples'), outcome=outcome)
            report(status.get('state', 'unknown'), **fields(status))
        if cancel() and stop_at is None and cancelled_at is None:
            # a stop after the proxy's own finish (or after the forwarded stop) changes nothing
            stop_at = clock()
            facts.stop_requested_unix_s = time.time()
            facts.stop_state = (last or {}).get('state')
            emit('sweep_stop_received', dict(request_id=request_id, state=facts.stop_state))
        if (cancelled_at is None and stop_at is None and facts.completed and (last or {}).get('state') == 'done'
                and request['trigger'] in ('software', 'external')):
            # v1.4 (human 2026-10-23): labscript no longer stops a shot whose playback completed; the proxy ends it
            # itself (lock-side protocol 2: '.finish', a normal end; older lock sessions: the stop path).
            cancelled_at = clock()
            facts.auto_finished = True
            facts.finish_forwarded_unix_s = time.time()
            if int((last or {}).get('protocol') or 1) >= 2:
                queue.finish(request_id)
            else:
                queue.cancel(request_id)
            emit('sweep_auto_finish', dict(request_id=request_id, protocol=int((last or {}).get('protocol') or 1)))
        if stop_at is not None and cancelled_at is None:
            # 2026-10-23 (human): a stop that arrives while the record is playing must not turn a complete sweep into
            # 'cancelled'. Hold the stop while the session reports fired/playing and has not reported completion, at
            # most record_s + margin from the first fired/playing seen; completion itself is the session's/owner's
            # 'done', never this timer.
            # v1.2 continuous: the stop IS the normal end of the run (no record end to wait for) -> forwarded at once
            playing = (last or {}).get('state') in ('fired', 'playing') and not facts.completed and not continuous
            if playing and play_seen_at is not None and clock() < play_seen_at + facts.record_s + DEFER_MARGIN_S:
                if not held:
                    held = True
                    emit('sweep_stop_held', dict(request_id=request_id, state=(last or {}).get('state'),
                                                 max_hold_s=round(play_seen_at + facts.record_s + DEFER_MARGIN_S - clock(), 3)))
            else:
                cancelled_at = clock()
                facts.stop_forwarded_unix_s = time.time()
                if held:
                    facts.stop_deferred_s = round(cancelled_at - stop_at, 3)
                queue.cancel(request_id)
                emit('ff_request_cancel', dict(request_id=request_id, held_s=facts.stop_deferred_s))
        if cancelled_at is not None and clock() - cancelled_at > 180.:
            give_up('FF session did not report restoration within 180 s of the stop/finish request; inspect the lock job')
        if clock() - started > float(p['duration_s']) + 60.:
            if cancelled_at is None:
                queue.cancel(request_id)
            give_up('FF proxy lifetime expired before the session finished')
        lock_progress = Path(lock_dir) / 'progress.json'
        from ldcon.core.records import read_json
        if read_json(lock_progress, {}).get('status') not in ('running', 'acquiring', 'finishing'):
            give_up('The lock job ended while the FF shot was pending')
        # poll finer while the trigger is awaited and while the session restores/settles (catch 'settling' promptly)
        sleep(.05 if request['trigger'] == 'software' and forwarded['record'] is None
              else .05 if (last or {}).get('state') in ('restoring', 'settling') else .2)
