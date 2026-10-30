"""Per-shot verdict of a locked-staircase proxy job (human request 2026-10-23: only ldcon knows whether a sweep
succeeded; labscript must get that verdict, and a sweep that played through must not end 'cancelled').

The proxy job (native_ff_proxy.run_locked_proxy) records the facts the lock-side session reports in its status file
(the session's own measured states; sr422: the rig owner's states, sr1092: the session's ADC detection on the ESP32
mod_raw) and turns them into ONE outcome object, published on the job (progress.json top level ``outcome`` and
``shot_sweep.outcome``) and used for the job's own terminal status.

Rules (design doc rule F 3rd example, "sweep verdict"):
  succeeded  playback detected (playback_started_unix_s), the record played through as reported by the session /
             owner ('done', or a session result 'succeeded', which the session only reaches after 'done'), and the
             outputs restored (restored: true). A stop that arrives after that does not change the verdict.
  cancelled  a stop arrived before the record was complete (before playback, or during it), outputs restored.
  failed     anything else: an owner / session failure, a rejection, no playback, a record that did not complete
             without a stop, or a restore that is not verified (restored: false), whatever else happened.
Completion is never decided on a host timer: the timer below only BOUNDS how long a stop is held back while the
session reports fired/playing, so that the owner's own 'done' can arrive.
"""
import json
import math
import time
from datetime import datetime
from pathlib import Path

SCHEMA = 'ldcon.sweep-outcome/v2'        # v2 (sweep API v1.4): published at 'restore verified', settle/analysis follow
ANALYSIS_NOT_IMPLEMENTED = dict(state='unavailable', ref=None,
                                reason='post-shot analysis was not started for this shot')   # v1.5: shot_analysis.launch
ARBITRARY_ANALYSIS = dict(state='unavailable', ref=None, reason='arbitrary shape: raw data only (analysis agent)')
DEFER_MARGIN_S = 3.     # stop held back while fired/playing: at most record_s + this, from the first fired/playing seen
STOP_ECHOES = ('Sweep proxy stopped', 'Sweep proxy cancelled before prepare', 'FF proxy cancelled',
               'FF sweep cancelled; bounded restoration requested')


def is_stop_echo(message):
    """The session's own wording for 'a stop was requested' (not an owner / session failure)."""
    text = str(message)
    if text.startswith('owner abort: '):
        text = text[len('owner abort: '):]
    return text in STOP_ECHOES


def record_seconds(plan, programmed=None):
    """The record length: the owner's programmed record (sr422) if known, else lead + levels x dwell + tail."""
    record = ((programmed or {}).get('record') or {}).get('period_s')
    if isinstance(record, (int, float)) and math.isfinite(record) and record > 0:
        return float(record)
    levels = plan.get('levels') or plan.get('levels_hz') or []
    return (float(plan.get('lead_s') or 0.) + float(plan.get('tail_s') or 0.)
            + len(levels) * float(plan.get('dwell_s') or 0.)) * float(plan.get('cycles') or 1)


class Facts:
    """What the lock-side session reported over the shot (accumulated: the status file holds only the latest)."""

    def __init__(self, record_s, levels, *, calibrate_only=False, continuous=False, shape='staircase'):
        self.shape = shape or 'staircase'                # 'arbitrary': raw samples, no level analysis
        self.calibrate_only = bool(calibrate_only)     # sr422 ADC calibration request: no playback by design
        self.continuous = bool(continuous)             # v1.2 continuous triangle: ends by a stop, never by 'done'
        self.stop_reason = None
        self.periods_played = None
        self.context = None
        self.playback_detection = None
        self.playback_possibly_started = None
        self.run_s = None
        self.record_s = float(record_s)
        self.levels = list(levels or [])
        self.playback_started_unix_s = None
        self.completed = False
        self.completed_source = None
        self.stop_requested_unix_s = None
        self.stop_state = None
        self.stop_forwarded_unix_s = None
        self.stop_deferred_s = None
        self.residuals = None
        self.auto_finished = False                     # the proxy itself ended the shot after the record completed
        self.finish_forwarded_unix_s = None
        self.tracking = None                           # sweep_tracking.TrackingStats summary from the session

    def observe(self, status):
        if not isinstance(status, dict):
            return
        if status.get('playback_started_unix_s') is not None:
            self.playback_started_unix_s = status['playback_started_unix_s']
        if status.get('residuals'):
            self.residuals = status['residuals']
        for key in ('stop_reason', 'periods_played', 'context', 'playback_detection', 'playback_possibly_started', 'run_s',
                    'tracking'):
            if status.get(key) is not None:
                setattr(self, key, status[key])
        state = status.get('state')
        source = None
        if state == 'done':
            source = 'session_done'                       # sr422: owner 'done'; sr1092: ADC back at centre after the record
        elif status.get('playback_completed') is True or status.get('record_completed') is True:
            source = 'reported'                           # an owner/session that reports it explicitly (forward compatible)
        elif status.get('ff_ended_after_playback'):
            source = 'ff_ended_after_playback'            # sr1092: the owner ended FF after the record had played
        elif state in ('restored', 'review_required', 'settling') and status.get('result_status') == 'succeeded':
            source = 'session_succeeded'                  # the session ends 'succeeded' only after its 'done'
        if source and not self.completed:
            self.completed, self.completed_source = True, source

    def outcome(self, final, *, residual_rows=None, settle=None, analysis=None):
        """The verdict from the session's status once the restore is verified ('settling', lock-side protocol 2) or
        final (restored / review_required / rejected). ``restored`` is the verified HARDWARE restore (FF/firmware/AWG);
        the post-shot settle check is reported separately in ``settle`` and never changes the verdict."""
        final = final or {}
        state = final.get('state')
        result_status = final.get('result_status')
        restored = bool(final.get('restored') or final.get('hardware_restored'))
        errors = [str(e) for e in (final.get('errors') or [])]
        owner_errors = [e for e in errors if not is_stop_echo(e)]
        started = self.playback_started_unix_s is not None
        stopped = self.stop_requested_unix_s is not None
        settled = (final.get('measured_after') or {}).get('ready')
        if self.continuous:
            return self._continuous_outcome(final, state, result_status, restored, errors, owner_errors, started, settle, analysis)
        if self.calibrate_only:
            ok = state == 'restored' and restored and result_status == 'succeeded'
            verdict = 'succeeded' if ok else 'failed'
            reason = 'calibration only (no playback by design)' + ('' if ok else ': ' + ('; '.join(owner_errors) or str(result_status)))
        elif state == 'rejected':
            verdict, reason = 'failed', 'rejected before playback: ' + ('; '.join(owner_errors) or 'no reason given')
        elif state == 'review_required' or not restored:
            verdict, reason = 'failed', 'outputs not verified restored (review): ' + ('; '.join(owner_errors) or str(result_status))
        elif result_status in ('failed', 'review_required', 'rejected'):
            verdict, reason = 'failed', '; '.join(owner_errors) or 'the session failed'
        elif self.completed and started and result_status in ('succeeded', 'cancelled', 'completed_unsettled', None):
            verdict = 'succeeded'
            reason = 'record played through and outputs restored'
            if self.auto_finished and not stopped:
                reason += '; the proxy ended the shot itself after the record completed'
            if stopped and self.stop_deferred_s:
                reason += '; the stop arrived during playback and was held until the record completed'
            elif stopped:
                reason += '; stopped after the record completed'
        elif self.completed and not started:
            verdict, reason = 'failed', 'completion reported without a detected playback start (inconsistent; not a success)'
        elif result_status == 'cancelled' or stopped:
            verdict = 'cancelled'
            reason = ('stopped during playback; the record did not complete' if started
                      else 'stopped before any playback was detected')
        else:
            verdict, reason = 'failed', ('the record did not complete' if started else 'no playback was detected')
        arbitrary = self.shape in ('arbitrary', 'triangle')
        rows = residual_rows if residual_rows is not None else rows_from_session(final.get('residuals') or self.residuals)
        summary = residual_summary(rows, len(self.levels))
        if arbitrary:
            # human 2026-10-23 ~05:10Z: an arbitrary record has no levels to analyse here; the analysis agent fills the
            # raw data into outcome.analysis later (the slot is kept)
            summary = dict(levels_measured=None, max_abs_error_hz=None, rms_error_hz=None, source=None)
            analysis = analysis or (dict(state='unavailable', ref=None, reason='triangle: raw waveform, no settled plateaus')
                                    if self.shape == 'triangle' else ARBITRARY_ANALYSIS)
        if settle is None:
            settle = settle_block(final, 'settled' if settled else 'unsettled' if settled is False else 'unknown')
        return dict(schema=SCHEMA, verdict=verdict, reason=reason, restored=restored,
                    settled=settle.get('ready'), settle=settle,
                    analysis=dict(analysis or ANALYSIS_NOT_IMPLEMENTED),
                    playback_started_unix_s=self.playback_started_unix_s,
                    playback_completed=bool(self.completed and started), completion_source=self.completed_source,
                    record_s=round(self.record_s, 6), levels_total=len(self.levels),
                    levels_measured=summary['levels_measured'], max_abs_error_hz=summary['max_abs_error_hz'],
                    rms_error_hz=summary['rms_error_hz'], residual_source=summary['source'],
                    stop_requested_unix_s=self.stop_requested_unix_s, stop_state=self.stop_state,
                    stop_forwarded_unix_s=self.stop_forwarded_unix_s, stop_deferred_s=self.stop_deferred_s,
                    auto_finished=self.auto_finished, finish_forwarded_unix_s=self.finish_forwarded_unix_s,
                    session_result=result_status, owner_errors=owner_errors, errors=errors,
                    playback_detection=self.playback_detection, playback_possibly_started=self.playback_possibly_started,
                    shape=self.shape)

    def _continuous_outcome(self, final, state, result_status, restored, errors, owner_errors, started, settle, analysis):
        """v1.2 continuous triangle (kind 'continuous'): succeeded = playback detected, the run ended by a stop (the
        stop request, max_run_s, or the owner's continuous cap AFTER playback) and the restore verified. A stop before
        any playback is 'cancelled'. Everything else (owner/session failure, rejection, unverified restore) is failed."""
        stopped = self.stop_reason is not None or self.stop_requested_unix_s is not None
        if state == 'rejected':
            verdict, reason = 'failed', 'rejected before playback: ' + ('; '.join(owner_errors) or 'no reason given')
        elif state == 'review_required' or not restored:
            verdict, reason = 'failed', 'outputs not verified restored (review): ' + ('; '.join(owner_errors) or str(result_status))
        elif result_status == 'succeeded' and started and stopped:
            verdict = 'succeeded'
            reason = 'continuous run stopped (%s) and outputs restored' % (self.stop_reason or 'stop requested')
        elif not started and (result_status == 'cancelled' or self.stop_requested_unix_s is not None):
            verdict, reason = 'cancelled', 'stopped before any playback was detected'
        else:
            verdict, reason = 'failed', '; '.join(owner_errors) or ('the continuous run ended without a stop' if started
                                                                     else 'no playback was detected')
        settled = (final.get('measured_after') or {}).get('ready')
        if settle is None:
            settle = settle_block(final, 'settled' if settled else 'unsettled' if settled is False else 'unknown')
        return dict(schema=SCHEMA, kind='continuous', verdict=verdict, reason=reason, restored=restored,
                    settled=settle.get('ready'), settle=settle, analysis=dict(analysis or ANALYSIS_NOT_IMPLEMENTED),
                    playback_started_unix_s=self.playback_started_unix_s, period_s=round(self.record_s, 9),
                    periods_played=self.periods_played, stop_reason=self.stop_reason,
                    run_s=self.run_s if self.run_s is not None else (
                        round(self.stop_requested_unix_s - self.playback_started_unix_s, 3)
                        if self.stop_requested_unix_s and self.playback_started_unix_s else None),
                    context=self.context, playback_completed=None, completion_source=None,
                    record_s=round(self.record_s, 6), levels_total=len(self.levels), levels_measured=0,
                    max_abs_error_hz=None, rms_error_hz=None, residual_source=None,
                    stop_requested_unix_s=self.stop_requested_unix_s, stop_state=self.stop_state,
                    stop_forwarded_unix_s=self.stop_forwarded_unix_s, stop_deferred_s=None, auto_finished=False,
                    finish_forwarded_unix_s=None, session_result=result_status, owner_errors=owner_errors, errors=errors)


def settle_block(status, state, *, started_unix_s=None, deadline_unix_s=None, reason=None):
    """outcome.settle: the post-shot settle check of the LOCK (after the verified restore). state: settling (running,
    with a deadline), settled, unsettled (missed its criteria; the lock continues), review_required (the session
    ended in review after the restore), unknown (no verified final state). Never changes the verdict."""
    after = (status or {}).get('measured_after') or {}
    return dict(state=state, started_unix_s=started_unix_s, deadline_unix_s=deadline_unix_s,
                finished_unix_s=None if state == 'settling' else time.time(),
                ready=None if state in ('settling', 'unknown') else after.get('ready'),
                max_abs_error_hz=after.get('max_abs_error_hz'), span_hz=after.get('span_hz'), reason=reason)


def rows_from_session(table):
    """sr422 session residual_table rows -> {level_hz, n, mean_error_hz, max_abs_error_hz, source}."""
    if not table:
        return None
    out = []
    for row in table:
        fw = row.get('fw_mean_hz') is not None
        out.append(dict(level_hz=row.get('level_hz'), n=row.get('n') or 0,
                        mean_error_hz=row.get('fw_mean_hz') if fw else row.get('wm_mean_hz'),
                        max_abs_error_hz=row.get('fw_max_abs_hz') if fw else row.get('wm_max_abs_hz'),
                        source='firmware' if fw else 'wavemeter'))
    return out


def residual_summary(rows, levels_total):
    """levels_measured (levels with >= 1 sample), max |error| and the sample-weighted RMS of the per-level means
    (an RMS over levels, not over raw samples: only per-level means are kept)."""
    rows = [r for r in (rows or []) if (r.get('n') or 0) > 0 and r.get('mean_error_hz') is not None]
    if not rows:
        return dict(levels_measured=0, max_abs_error_hz=None, rms_error_hz=None, source=None)
    n = sum(r['n'] for r in rows)
    rms = math.sqrt(sum(r['n'] * float(r['mean_error_hz']) ** 2 for r in rows) / n)
    worst = max(float(r['max_abs_error_hz']) for r in rows if r.get('max_abs_error_hz') is not None)
    sources = sorted({r.get('source') for r in rows if r.get('source')})
    return dict(levels_measured=len(rows), max_abs_error_hz=worst, rms_error_hz=rms,
                source='+'.join(sources) if sources else None)


def native_rows(lock_dir, start_unix_s, record_s, levels, *, max_bytes=8 << 20):
    """sr1092: the session publishes no residual table; derive per-level rows from the lock job's own ff_sample events
    (wavemeter error against the commanded offset read from the ESP32 mod_raw ADC) inside the detected playback
    window (same rule as the web facade's native_residuals). Read-only; the last <= 8 MB of the event log."""
    if start_unix_s is None or not levels or lock_dir is None:
        return None
    try:
        path = Path(lock_dir) / 'telemetry.events.jsonl'
        size = path.stat().st_size
        with path.open('rb') as stream:
            stream.seek(max(0, size - max_bytes))
            lines = stream.read().splitlines()[1:]
    except (OSError, ValueError):
        return None
    end = float(start_unix_s) + float(record_s) + 1.
    per = {i: [] for i in range(len(levels))}
    # v1.5 (2026-10-23 sweep-speed C3: rms 154 MHz / max 571 MHz from ONE sample straddling the 0 -> -600 MHz jump):
    # a sample whose commanded offset moved by more than 3 level steps since the previous sample (or is not near a
    # level, i.e. on an edge) sees the transition, not the level -> excluded. The post-shot analysis replaces these
    # preliminary numbers when it is ready (outcome.residual_source 'shot_analysis').
    diffs = [abs(b - a) for a, b in zip(levels, levels[1:]) if b != a]
    step = min(diffs) if diffs else max([abs(v) for v in levels] + [1.])
    previous = 0.
    for line in lines:
        if b'"ff_sample"' not in line:
            continue
        try:
            row = json.loads(line)
            stamp = datetime.fromisoformat(row['recorded_utc']).timestamp()
        except (ValueError, KeyError, TypeError):
            continue
        p = row.get('payload') or {}
        if not float(start_unix_s) <= stamp <= end or not p.get('playing') or p.get('error_hz') is None \
                or p.get('commanded_offset_hz') is None:
            continue
        commanded = float(p['commanded_offset_hz'])
        index = min(range(len(levels)), key=lambda i: abs(levels[i] - commanded))
        jumped = abs(commanded - previous) > 3 * step or abs(levels[index] - commanded) > step / 4
        previous = commanded
        if jumped:
            continue
        per[index].append(float(p['error_hz']))
    return [dict(level_hz=levels[i], n=len(v), mean_error_hz=sum(v) / len(v) if v else None,
                 max_abs_error_hz=max(map(abs, v)) if v else None, source='wavemeter') for i, v in per.items()]
