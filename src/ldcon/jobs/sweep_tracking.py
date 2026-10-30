"""Sweep tracking quality and the mechanical correction (human request 2026-10-23 morning).

"A large integral means poor tracking. Stopping or not is either fine, but report it as a problem and trigger parameter
adjustment: return it to labscript immediately (labscript will answer 'send slower or change the laser control'); if a
mechanical algorithm can handle it, do that; otherwise have an agent analyse it."

- ``TrackingStats`` (lock-side session, both lasers): per playback sample the commanded offset (Hz, from the AWG-driven
  ADC), the servo DAC code, the PI integral fraction (sr1092) and the firmware error (sr422). ``summary()`` is published
  in the session status as ``tracking``.
- ``assess()`` (the proxy job, at the outcome): ``outcome.tracking = {state: ok|degraded|aborted, reason, peak_integral_
  fraction, error stats, advice[], recommendation, agent}``. The verdict rules do not change (an owner abort is failed;
  a completed record with poor tracking is succeeded + tracking degraded).
- Mechanical rule (the only one implemented): the FF gain. Over the playback the servo DAC moves by ``dac_slope`` codes
  per commanded Hz; the feed-forward alone moves it by ``ff_slope`` (= the firmware multiplier_control per ADC count x
  ADC counts per Hz, at the shot's gain scale). The PI supplies the difference, which is what drives the integral. The
  recommended gain scale is ``scale x dac_slope / ff_slope`` (bounded by the route's scale bounds), only with enough
  samples, a good linear fit and a correction of >= 3 %. It is a RECOMMENDATION: labscript (or a person/agent) passes it
  as a per-shot option (sr1092 ``ff_gain_scale``, sr422 ``pzt_ff_gain_scale``); nothing is written to the laser
  definition. Every recommendation is stored under ``<state root>/sweep-tracking/``.
- No mechanical rule applies (e.g. same-cycle error, mode degraded, ADC invalid, or a poor fit) -> one non-blocking
  notice to the deck's resident agent (rule B notice path: inbox + Teams card, no failure case, no reservation hold).
- Mode degradation (human 2026-10-23 ~12:10Z, sr422 rig owner v5.7+): 'mode obviously degraded (optical.single_mode_max)' is a
  single-mode QUALITY index, not a safety stop. The owner no longer aborts; ``DegradationLog`` collects every degraded
  playback sample (where: t, level index, commanded offset; what: side-peak height/prominence, main FWHM fraction,
  single_mode, degraded_by) and ``assess`` reports ``state degraded, reason mode_degraded`` with the per-sample list and a
  summary (count, first/last, longest run, worst, affected levels / offset range).
"""
import json
import math
import os
import time
from pathlib import Path

SCHEMA = 'ldcon.sweep-tracking/v1'
WARN_INTEGRAL_FRACTION = .75          # sr1092: a completed record whose |integral| reached 75 % of the limit is degraded
ERROR_DEGRADED_FRACTION = .5          # max |error| >= this x the owner's abort error -> degraded
MIN_FIT_SAMPLES = 20
MIN_FIT_R2 = .8
MIN_GAIN_CORRECTION = .03

REASONS = (   # (substring of the owner/session end reason, reason code, state, advice)
    ('Native integral reached approved FF abort fraction', 'integral_fraction', 'aborted', ['adjust_ff_gain', 'slower_sweep']),
    ('Same-cycle native FF error exceeded', 'same_cycle_error', 'aborted', ['slower_sweep', 'approach_ramp']),
    ('Physical ADC invalid or invalid counter advanced', 'adc_invalid', 'aborted', ['check_awg_route']),
    ('|error| >', 'error', 'aborted', ['slower_sweep', 'adjust_ff_gain']),
    ('mode obviously degraded', 'mode_degraded', 'aborted', ['change_laser_control']),
    ('Native integral has insufficient FF start reserve', 'integral_start_reserve', 'aborted', ['recentre']),
    ('PI integral fraction', 'integral_start_reserve', 'aborted', ['recentre']),
)
GAIN_OPTION = {'native': 'ff_gain_scale', 'rig': 'pzt_ff_gain_scale'}
QUALITY_METRICS = ('side_peak_height_max', 'side_peak_prominence_max', 'main_fwhm_fraction', 'near_main_peak_prominence_max')
PLAYBACK_PHASES = ('fired', 'playing', 'restore_pending')   # restore_pending: a stop waits for the record end
DEGRADED_SAMPLES_MAX = 2000           # outcome size bound; the analysis document carries every point
MODE_ADVICE = ['change_laser_control', 'avoid_degraded_range']
SCALE_BOUNDS = (.5, 2.)


def _finite(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


class TrackingStats:
    """Accumulates playback samples on the lock side; ``summary()`` is JSON for the session status."""

    def __init__(self, *, ff_codes_per_hz=None, gain_scale=1., max_samples=20000):
        self.ff_codes_per_hz = ff_codes_per_hz
        self.gain_scale = gain_scale
        self.rows = []
        self.peak_fraction = None
        self.errors = []
        self.max_samples = max_samples

    def add(self, commanded_hz, dac=None, integral_fraction=None, error_hz=None):
        if _finite(integral_fraction):
            f = abs(float(integral_fraction))
            self.peak_fraction = f if self.peak_fraction is None else max(self.peak_fraction, f)
        if _finite(error_hz):
            self.errors.append(float(error_hz))
            del self.errors[:-self.max_samples]
        if _finite(commanded_hz) and _finite(dac):
            self.rows.append((float(commanded_hz), float(dac)))
            del self.rows[:-self.max_samples]

    def fit(self):
        n = len(self.rows)
        if n < 3:
            return None
        xs = [r[0] for r in self.rows]
        ys = [r[1] for r in self.rows]
        mx, my = sum(xs) / n, sum(ys) / n
        sxx = sum((x - mx) ** 2 for x in xs)
        syy = sum((y - my) ** 2 for y in ys)
        if sxx <= 0:
            return None
        sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        slope = sxy / sxx
        r2 = (sxy * sxy / (sxx * syy)) if syy > 0 else 0.
        return dict(n=n, slope=slope, r2=r2, span_hz=max(xs) - min(xs))

    def summary(self):
        fit = self.fit()
        err = self.errors
        out = dict(schema=SCHEMA, samples=len(self.rows), peak_integral_fraction=self.peak_fraction,
                   error_rms_hz=math.sqrt(sum(e * e for e in err) / len(err)) if err else None,
                   error_max_abs_hz=max(abs(e) for e in err) if err else None,
                   ff_codes_per_hz=self.ff_codes_per_hz, gain_scale=self.gain_scale,
                   dac_slope_codes_per_hz=fit['slope'] if fit else None, fit_r2=fit['r2'] if fit else None,
                   fit_span_hz=fit['span_hz'] if fit else None)
        return out


def _num(x):
    return float(x) if _finite(x) else None


def degraded_sample(row, t0_unix_s=None):
    """One degraded owner sample, located (t, level, commanded offset) and described (metrics, flags, causes)."""
    metrics = row.get('metrics') or {}
    t = row.get('unix_s')
    out = dict(unix_s=_num(t), t_s=(float(t) - float(t0_unix_s)) if _finite(t) and _finite(t0_unix_s) else None,
               phase=row.get('phase'), level_index=row.get('level_index'),
               commanded_offset_hz=_num(row.get('commanded_offset_hz')), frequency_hz=_num(row.get('frequency_hz')),
               error_hz=_num(row.get('fw_error_hz') if row.get('fw_error_hz') is not None else row.get('error_hz')),
               single_mode=row.get('single_mode'), maintenance_required=row.get('maintenance_required'))
    out.update({k: _num(metrics.get(k)) for k in QUALITY_METRICS})
    out['degraded_by'] = row.get('degraded_by')
    return out


def _severity(sample):
    """(ratio to the quality limit, metric, value, limit) of a degraded sample; the classifier-only flag ranks as 1."""
    best = None
    for cause in sample.get('degraded_by') or []:
        if not isinstance(cause, dict):
            continue
        v, lim = cause.get('value'), cause.get('limit')
        if _finite(v) and _finite(lim) and lim > 0:
            cand = (float(v) / float(lim), cause.get('metric'), float(v), float(lim))
        else:
            cand = (1., cause.get('metric'), v, lim)
        if best is None or cand[0] > best[0]:
            best = cand
    if best is None:                     # an owner before v5.7: no degraded_by; rank by the side-peak height
        v = sample.get('side_peak_height_max')
        best = (None, 'side_peak_height_max' if v is not None else None, v, None)
    return best


class DegradationLog:
    """Degraded samples of ONE playback (rows with phase fired/playing; for rows without a phase, unix_s >= t0)."""

    def __init__(self, t0_unix_s=None, *, max_samples=DEGRADED_SAMPLES_MAX):
        self.t0 = t0_unix_s
        self.max_samples = max_samples
        self.valid = 0
        self.count = 0
        self.run = 0
        self.longest = 0
        self.samples = []
        self.first = self.last = self.worst = None
        self.levels = {}
        self.offsets = []
        self.metric_max = {}
        self.by_metric = {}

    def in_playback(self, row):
        phase = row.get('phase')
        if phase is not None:
            return phase in PLAYBACK_PHASES
        t = row.get('unix_s')
        return self.t0 is None or (_finite(t) and t >= self.t0)

    def add(self, row):
        if not isinstance(row, dict) or not self.in_playback(row) or row.get('valid') is False:
            return
        self.valid += 1
        if row.get('degraded') is not True:
            self.run = 0
            return
        self.count += 1
        self.run += 1
        self.longest = max(self.longest, self.run)
        sample = degraded_sample(row, self.t0)
        if len(self.samples) < self.max_samples:
            self.samples.append(sample)
        self.first = self.first or sample
        self.last = sample
        sev = _severity(sample)
        if self.worst is None or (sev[0] or 0) > (self.worst[0] or 0) or (
                sev[0] is None and self.worst[0] is None and (sev[2] or 0) > (self.worst[2] or 0)):
            self.worst = sev + (sample,)
        key = sample['level_index']
        lv = self.levels.setdefault(key, dict(level_index=key, count=0, offsets=[]))
        lv['count'] += 1
        if sample['commanded_offset_hz'] is not None:
            lv['offsets'].append(sample['commanded_offset_hz'])
            self.offsets.append(sample['commanded_offset_hz'])
        for k in QUALITY_METRICS:
            if sample.get(k) is not None:
                self.metric_max[k] = max(self.metric_max.get(k, sample[k]), sample[k])
        for cause in sample.get('degraded_by') or []:
            if isinstance(cause, dict):
                name = cause.get('metric') or 'classifier'
                self.by_metric[name] = self.by_metric.get(name, 0) + 1

    def summary(self, *, samples=True):
        def where(s):
            return None if s is None else {k: s.get(k) for k in ('unix_s', 't_s', 'level_index', 'commanded_offset_hz')}
        worst = None
        if self.worst is not None:
            ratio, metric, value, limit, s = self.worst
            worst = dict(where(s), metric=metric, value=value, limit=limit, ratio_to_limit=ratio,
                         **{k: s.get(k) for k in QUALITY_METRICS}, single_mode=s.get('single_mode'))
        levels = []
        for key in sorted(self.levels, key=lambda k: (k is None, k if k is not None else 0)):
            lv = self.levels[key]
            offs = lv['offsets']
            levels.append(dict(level_index=key, count=lv['count'],
                               commanded_offset_hz=(sum(offs) / len(offs)) if offs else None))
        out = dict(policy='reported, not an abort (human 2026-10-23 ~12:10Z)',
                   criterion='optical.single_mode_max exceeded or classifier maintenance_required',
                   count=self.count, playback_valid_samples=self.valid,
                   fraction=(self.count / self.valid) if self.valid else None, longest_run=self.longest,
                   first=where(self.first), last=where(self.last), worst=worst, by_metric=dict(self.by_metric),
                   metric_max=dict(self.metric_max), levels=levels,
                   commanded_offset_range_hz=[min(self.offsets), max(self.offsets)] if self.offsets else None,
                   t0_unix_s=self.t0, samples_total=self.count,
                   samples_truncated=self.count > len(self.samples))
        if samples:
            out['samples'] = list(self.samples)
        return out


def degradation_from_rows(rows, t0_unix_s=None, *, samples=True):
    log = DegradationLog(t0_unix_s)
    for row in rows or []:
        log.add(row)
    return log.summary(samples=samples)


def gain_recommendation(stats, route_kind):
    """The mechanical FF-gain rule (see the module docstring), or None with the reason."""
    stats = stats or {}
    ff, slope, scale = stats.get('ff_codes_per_hz'), stats.get('dac_slope_codes_per_hz'), stats.get('gain_scale') or 1.
    if not (_finite(ff) and _finite(slope)) or ff == 0:
        return None, 'no feed-forward slope or DAC slope in the shot data'
    if (stats.get('samples') or 0) < MIN_FIT_SAMPLES or (stats.get('fit_r2') or 0.) < MIN_FIT_R2:
        return None, 'too few samples or a poor linear fit (n=%s, r2=%s)' % (stats.get('samples'), stats.get('fit_r2'))
    ratio = slope / ff
    if not ratio > 0:
        return None, 'the DAC moved against the feed-forward (ratio %.3g): not a gain error' % ratio
    if abs(ratio - 1.) < MIN_GAIN_CORRECTION:
        return None, 'the feed-forward gain is already within %.0f %%' % (100 * MIN_GAIN_CORRECTION)
    new = min(SCALE_BOUNDS[1], max(SCALE_BOUNDS[0], float(scale) * ratio))
    option = GAIN_OPTION.get(route_kind, 'ff_gain_scale')
    return dict(rule='ff_gain', options={option: round(new, 4)}, from_scale=float(scale), ratio=round(ratio, 4),
                bounded=new != float(scale) * ratio,
                basis='DAC slope %.4g codes/Hz vs feed-forward slope %.4g codes/Hz over %d samples (r2 %.3f)'
                      % (slope, ff, stats.get('samples'), stats.get('fit_r2'))), None


def assess(route_kind, verdict, reason, errors, stats=None, *, abort_error_hz=None, rms_error_hz=None,
           max_abs_error_hz=None):
    """outcome.tracking. route_kind: 'native' (sr1092) | 'rig' (sr422)."""
    stats = stats or {}
    text = ' '.join([str(reason or '')] + [str(e) for e in (errors or [])])
    state, code, advice = 'ok', None, []
    for needle, rcode, rstate, radvice in REASONS:
        if needle in text and verdict != 'succeeded':
            state, code, advice = rstate, rcode, list(radvice)
            break
    deg = stats.get('degradation') if isinstance(stats.get('degradation'), dict) else None
    mode_degraded = bool(deg and (deg.get('count') or 0) > 0)
    reasons = [code] if code else []
    peak = stats.get('peak_integral_fraction')
    err_max = stats.get('error_max_abs_hz') if stats.get('error_max_abs_hz') is not None else max_abs_error_hz
    err_rms = stats.get('error_rms_hz') if stats.get('error_rms_hz') is not None else rms_error_hz
    if state == 'ok':
        if _finite(peak) and peak >= WARN_INTEGRAL_FRACTION:
            state, code, advice = 'degraded', 'integral_fraction', ['adjust_ff_gain', 'slower_sweep']
            reasons.append(code)
        elif _finite(err_max) and _finite(abort_error_hz) and err_max >= ERROR_DEGRADED_FRACTION * abort_error_hz:
            state, code, advice = 'degraded', 'large_residual', ['adjust_ff_gain', 'slower_sweep']
            reasons.append(code)
    if mode_degraded and 'mode_degraded' not in reasons:
        # human 2026-10-23 ~12:10Z: a degraded mode during the playback is reported (never an abort); it is the primary
        # reason of a completed shot, an additional reason of an aborted one
        if state == 'aborted':
            reasons.append('mode_degraded')
            advice = advice + [a for a in MODE_ADVICE if a not in advice]
        else:
            state, code = 'degraded', 'mode_degraded'
            reasons.insert(0, 'mode_degraded')
            advice = list(MODE_ADVICE) + [a for a in advice if a not in MODE_ADVICE]
    elif code == 'mode_degraded':        # an owner before v5.7 aborted for it
        advice = list(MODE_ADVICE)
    rec, why_not = (None, None)
    if state != 'ok' and 'adjust_ff_gain' in advice:
        rec, why_not = gain_recommendation(stats, route_kind)
    if state != 'ok' and 'slower_sweep' in advice:
        slower = dict(rule='slower_sweep', sequence=dict(dwell_s_factor=2.),
                      basis='halve the step rate (twice the dwell) or add/lengthen the approach ramp')
    else:
        slower = None
    avoid = None
    if mode_degraded and deg.get('commanded_offset_range_hz'):
        avoid = dict(rule='avoid_degraded_range', commanded_offset_range_hz=deg['commanded_offset_range_hz'],
                     level_indices=[lv.get('level_index') for lv in deg.get('levels') or [] if lv.get('level_index') is not None],
                     basis='%d of %s playback samples obviously degraded (worst %s)' % (
                         deg.get('count'), deg.get('playback_valid_samples'),
                         _worst_text(deg.get('worst'))))
    out = dict(schema=SCHEMA, state=state, reason=code, reasons=reasons, advice=advice, peak_integral_fraction=peak,
               warn_integral_fraction=WARN_INTEGRAL_FRACTION if route_kind == 'native' else None,
               error_rms_hz=err_rms, error_max_abs_hz=err_max, abort_error_hz=abort_error_hz,
               samples=stats.get('samples'), dac_slope_codes_per_hz=stats.get('dac_slope_codes_per_hz'),
               ff_codes_per_hz=stats.get('ff_codes_per_hz'), gain_scale=stats.get('gain_scale'),
               recommendation=rec or slower or avoid, recommendations=[r for r in (rec, slower, avoid) if r],
               mechanical=bool(rec), no_mechanical_reason=(why_not or ('no mechanical rule for a degraded mode'
                                                                       if mode_degraded or code == 'mode_degraded' else None))
               if state != 'ok' and not rec else None,
               degradation=deg if mode_degraded else None,
               full_data_log=stats.get('full_data_log'),
               agent=None)
    return out


def _worst_text(worst):
    if not isinstance(worst, dict):
        return 'unknown'
    return '%s=%s (limit %s) at level %s, offset %s Hz, t=%s s' % (
        worst.get('metric'), _short(worst.get('value')), _short(worst.get('limit')), worst.get('level_index'),
        _short(worst.get('commanded_offset_hz')), _short(worst.get('t_s')))


def _short(x):
    return ('%.4g' % x) if _finite(x) else str(x)


def state_root_of(job_dir):
    job_dir = Path(job_dir)
    for parent in list(job_dir.parents)[:4]:
        if (parent / 'web-server.json').exists():
            return parent
    return job_dir.parent.parent.parent


def store(job_dir, tracking, *, now=time.time):
    """Keep the recommendation (never applied): <state root>/sweep-tracking/<job>.json and latest.json."""
    try:
        root = state_root_of(job_dir) / 'sweep-tracking'
        root.mkdir(parents=True, exist_ok=True)
        value = dict(tracking, proxy_job=Path(job_dir).name, stored_unix_s=now())
        for name in (Path(job_dir).name + '.json', 'latest.json'):
            tmp = root / ('.%s.%d.tmp' % (name, os.getpid()))
            tmp.write_text(json.dumps(value, default=str))
            os.replace(tmp, root / name)
        return str(root / (Path(job_dir).name + '.json'))
    except OSError:
        return None


def ask_agent(job_dir, card_id, request_id, tracking, *, notify=None):
    """No mechanical rule: one non-blocking notice to the deck's resident agent (rule B path). Never raises."""
    if notify is None:
        from ldcon.jobs.measurement_notice import notify_non_blocking as notify
    try:
        message = ('Sweep tracking %s (%s) on proxy job %s: no mechanical correction applies (%s). Please analyse the '
                   'shot (GET jobs/%s, its outcome.tracking and analysis) and recommend sweep or laser-control changes.'
                   % (tracking.get('state'), tracking.get('reason'), Path(job_dir).name,
                      tracking.get('no_mechanical_reason') or 'see advice', Path(job_dir).name))
        return notify(job_dir, card_id, 'sweep_tracking', str(request_id)[:64],
                      dict(message=message, proxy_job=Path(job_dir).name, tracking=dict({k: tracking.get(k) for k in (
                          'state', 'reason', 'reasons', 'advice', 'peak_integral_fraction', 'error_rms_hz', 'error_max_abs_hz')},
                          degradation={k: v for k, v in (tracking.get('degradation') or {}).items() if k != 'samples'} or None)))
    except Exception as error:      # noqa: BLE001 - a notice never fails the shot
        return dict(agent=dict(status='error', reason=str(error)[:200]))


def reassess(proxy_dir, *, route_kind=None, ask=True, notify=None):
    """Re-assess a finished proxy job from its published outcome and the session's tracking stats; store the
    recommendation; ask the agent when no mechanical rule applies. No device I/O."""
    proxy_dir = Path(proxy_dir)
    progress = json.loads((proxy_dir / 'progress.json').read_text())
    shot = progress.get('shot_sweep') or {}
    outcome = progress.get('outcome') or shot.get('outcome') or {}
    previous = outcome.get('tracking') or {}
    kind = route_kind or ('rig' if (shot.get('programmed') or {}).get('record') else 'native')
    tracking = assess(kind, outcome.get('verdict'), outcome.get('reason'), outcome.get('owner_errors'),
                      shot.get('tracking') or {}, abort_error_hz=previous.get('abort_error_hz'),
                      rms_error_hz=outcome.get('rms_error_hz'), max_abs_error_hz=outcome.get('max_abs_error_hz'))
    tracking['stored'] = store(proxy_dir, tracking)
    if ask and tracking['state'] != 'ok' and not tracking['mechanical']:
        tracking['agent'] = ask_agent(proxy_dir, 'frequency-staircase', proxy_dir.name + ':card', tracking, notify=notify)
    return tracking


def parameters(card, overrides):
    """tune.sweep_tracking (rule F one-shot): {proxy_job | request_id, ask_agent=True}."""
    import re
    p = {**dict(proxy_job=None, request_id=None, ask_agent=True), **(card.get('parameters') or {}), **(overrides or {})}
    unknown = set(p) - {'proxy_job', 'request_id', 'ask_agent'}
    if unknown:
        raise ValueError('Unknown tune.sweep_tracking parameter(s): ' + ', '.join(sorted(unknown)))
    for key in ('proxy_job', 'request_id'):
        if p[key] is not None and (not isinstance(p[key], str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', p[key])):
            raise ValueError(key + ' must be a job id / request id string')
    if type(p['ask_agent']) is not bool:
        raise ValueError('ask_agent must be boolean')
    return p


def run_card(directory, laser, card, p, *, publish, cancel=lambda: False, emit=lambda *a, **k: None, **_):
    """tune.sweep_tracking: the same assessment and mechanical rule on demand for one finished sweep proxy job; the
    result is this job's artifact sweep-tracking.json and the stored recommendation (never applied)."""
    from ldcon.jobs.shot_analysis import find_proxy
    directory = Path(directory)
    proxy = find_proxy(state_root_of(directory), proxy_job=p.get('proxy_job'), request_id=p.get('request_id'))
    publish(dict(state='running', proxy_job=proxy.name))
    tracking = reassess(proxy, ask=p['ask_agent'])
    (directory / 'sweep-tracking.json').write_text(json.dumps(tracking, default=str))
    emit('sweep_tracking', tracking)
    publish(dict(state='done', proxy_job=proxy.name, tracking=tracking))
    return dict(status='succeeded', proxy_job=proxy.name, tracking_state=tracking['state'],
                recommendation=tracking.get('recommendation'))
