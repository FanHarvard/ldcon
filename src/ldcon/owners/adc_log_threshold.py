"""Temporary ESP32 ``log_adc_change_threshold`` during a locked sweep (human approval 2026-10-23 ~04:40Z).

The firmware logs a separate ``adc_raw`` telemetry row only when the physical ADC moved by at least
``log_adc_change_threshold`` counts since the last LOGGED value (servo_controller.cpp PushAdcTelemetry). With 32 counts
the sr422 10 MHz step at hz_per_v 1e10 (19.5 counts) was missed in half of the levels. The post-shot analysis uses
these rows to time each level individually. It is a TELEMETRY setting: it changes nothing in the PI, FF, target or
DAC path, so it is never part of the owner's control expectations and a failure here never faults the owner.

Rule (owner, at the sweep arm):
    s_min   = smallest non-zero |level step| (including centre -> first / last -> centre) x ADC counts per Hz
    sigma   = recent ADC noise (counts) measured by the owner at the centre (sr1092: the FF start-gate window;
              sr422: the armed-idle samples and the calibration spreads)
    thr_lo  = ceil(4 * sqrt(2) * sigma)       (a difference of two noisy samples; ~1e-5 false rows per cycle)
    thr_hi  = floor(0.6 * s_min)
    used    = min(original, max(thr_lo, thr_hi), ) and >= FLOOR_COUNTS; never raised above the original.
              thr_lo > thr_hi -> the steps cannot be logged reliably: used = thr_lo, flag 'steps_below_noise'.
Only when ``used < original`` is anything written. Journal before the write (original, used, sigma, s_min), one
write, read back /config; on every exit (normal restore, abort, unwind, owner restart) one write of the original and
a read-back. ``telemetry_adc_dropped`` (/status) is recorded before and after. Results go to the owner's journal and
an append-only history JSONL that the post-shot analysis reads. LDCON_ADC_LOG_THRESHOLD=off disables it.
"""
import json
import math
import os
import time
from pathlib import Path

KEY = 'log_adc_change_threshold'
FRACTION = 0.6
K_SIGMA = 4 * math.sqrt(2)
FLOOR_COUNTS = 4
MAX_RESTORE_ATTEMPTS = 3
RESTORE_RETRY_S = 2.0
HISTORY_MAX_BYTES = 4 << 20


def enabled():
    return os.environ.get('LDCON_ADC_LOG_THRESHOLD', '').lower() not in ('off', '0', 'false')


def min_step_counts(levels_hz, counts_per_hz):
    values = [0.0] + [float(v) for v in levels_hz] + [0.0]
    steps = [abs(b - a) for a, b in zip(values, values[1:]) if abs(b - a) > 0]
    return (min(steps) * abs(float(counts_per_hz))) if steps else None


def sigma_counts(samples, *, spreads=(), floor=0.5):
    """Sample standard deviation of ADC counts at rest; a range of n=5 samples is ~2.33 sigma (spreads)."""
    xs = [float(x) for x in samples if isinstance(x, (int, float)) and math.isfinite(x)]
    estimates = []
    if len(xs) >= 3:
        mean = sum(xs) / len(xs)
        estimates.append(math.sqrt(sum((x - mean) ** 2 for x in xs) / (len(xs) - 1)))
    estimates += [float(s) / 2.33 for s in spreads if isinstance(s, (int, float)) and math.isfinite(s) and s >= 0]
    return max([floor] + estimates) if estimates else None


def plan(original, levels_hz, counts_per_hz, sigma):
    """The threshold to use (pure). ``change`` is False when nothing must be written."""
    out = dict(key=KEY, original=original, used=original, sigma_counts=sigma, change=False, flags=[],
               fraction=FRACTION, k_sigma=K_SIGMA, floor_counts=FLOOR_COUNTS)
    s_min = min_step_counts(levels_hz, counts_per_hz) if counts_per_hz else None
    out['s_min_counts'] = s_min
    if type(original) is not int or original < 0:
        out['flags'].append('original_unknown')
        return out
    if s_min is None or sigma is None:
        out['flags'].append('no_step_or_noise_estimate')
        return out
    lo = max(FLOOR_COUNTS, int(math.ceil(K_SIGMA * sigma)))
    hi = int(math.floor(FRACTION * s_min))
    out.update(thr_lo=lo, thr_hi=hi)
    used = max(lo, hi)
    if lo > hi:
        out['flags'].append('steps_below_noise')
    used = min(original, used)
    out.update(used=used, change=used < original)
    return out


class Journal:
    """state: {state: applying|applied|restored|failed|restore_failed|unchanged, ...}; history: one JSON line per record
    update (bounded). The state file survives an owner restart: an unrestored 'applying'/'applied' is restored first."""

    def __init__(self, path):
        self.path = Path(path)
        self.history = self.path.with_name(self.path.stem + '.history.jsonl')
        self.record = self._load()

    def _load(self):
        try:
            value = json.loads(self.path.read_text())
            return value if isinstance(value, dict) else None
        except (OSError, ValueError):
            return None

    def save(self, record, *, history=True):
        self.record = dict(record, updated_unix_s=time.time())
        tmp = self.path.with_name('.' + self.path.name + '.tmp')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tmp.open('w') as stream:
            json.dump(self.record, stream, allow_nan=False, default=str)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, self.path)
        if history:
            try:
                if self.history.exists() and self.history.stat().st_size > HISTORY_MAX_BYTES:
                    lines = self.history.read_bytes().splitlines()[-2000:]
                    self.history.write_bytes(b'\n'.join(lines) + b'\n')
                with self.history.open('a') as stream:
                    stream.write(json.dumps(self.record, allow_nan=False, default=str) + '\n')
            except OSError:
                pass
        return self.record

    def needs_restore(self):
        r = self.record or {}
        return r.get('state') in ('applying', 'applied', 'restore_failed') and r.get('original') is not None \
            and int(r.get('restore_attempts') or 0) < MAX_RESTORE_ATTEMPTS


def _int(value):
    return int(value) if isinstance(value, (int, float)) and math.isfinite(value) and float(value).is_integer() else None


def apply(journal, *, write, read_config, read_status=None, levels_hz, counts_per_hz, sigma, context=None):
    """Plan, journal, write once, read back. Never raises; returns the record (also kept in the journal)."""
    context = dict(context or {})
    try:
        if not enabled():
            return dict(state='disabled', key=KEY, reason='LDCON_ADC_LOG_THRESHOLD=off', **context)
        if journal.needs_restore():
            restore(journal, write=write, read_config=read_config, read_status=read_status, reason='before a new arm')
        prior = journal.record or {}
        cfg = read_config()
        current = _int(cfg.get(KEY))
        # an earlier lowering that could not be restored: the TRUE original is the journal's
        original = prior.get('original') if prior.get('state') in ('applying', 'applied', 'restore_failed') else current
        p = plan(original, levels_hz, counts_per_hz, sigma)
        dropped = _dropped(read_status)
        record = dict(p, state='unchanged', planned_unix_s=time.time(), current_before=current,
                      dropped_before=dropped, restore=None, **context)
        if not p['change'] or current == p['used']:
            record['state'] = 'unchanged' if not p['change'] else 'applied'
            if p['change']:
                record['applied_unix_s'] = time.time()
            return journal.save(record)
        record['state'] = 'applying'
        journal.save(record)
        try:
            write({KEY: p['used']})
            back = _int(read_config().get(KEY))
        except Exception as error:      # noqa: BLE001 - telemetry only; the restore path will try the original
            record.update(state='applying', write_error=str(error)[:300], readback=None)
            return journal.save(record)
        record.update(readback=back, applied_unix_s=time.time(),
                      state='applied' if back == p['used'] else 'applying')
        if back != p['used']:
            record['write_error'] = 'readback %s != %s' % (back, p['used'])
        return journal.save(record)
    except Exception as error:          # noqa: BLE001
        return dict(state='failed', key=KEY, error=str(error)[:300], **context)


def restore(journal, *, write, read_config, read_status=None, reason='sweep finished', now=time.time):
    """Write the journal's original once and read it back. Never raises; at most MAX_RESTORE_ATTEMPTS attempts."""
    record = dict(journal.record or {})
    if not journal.needs_restore():
        return record
    last = record.get('last_restore_attempt_unix_s')
    if last is not None and now() - float(last) < RESTORE_RETRY_S:
        return record
    attempts = int(record.get('restore_attempts') or 0) + 1
    record.update(restore_attempts=attempts, last_restore_attempt_unix_s=now(), restore_reason=reason)
    try:
        current = _int(read_config().get(KEY))
        if current != record['original']:
            write({KEY: record['original']})
            current = _int(read_config().get(KEY))
        ok = current == record['original']
        dropped = _dropped(read_status)
        before = record.get('dropped_before')
        record.update(state='restored' if ok else 'restore_failed', restore_readback=current,
                      restored_unix_s=now() if ok else None, dropped_after=dropped,
                      dropped_delta=(dropped - before) if isinstance(dropped, int) and isinstance(before, int) else None,
                      restore=dict(ok=ok, readback=current, attempts=attempts, reason=reason))
    except Exception as error:          # noqa: BLE001
        record.update(state='restore_failed', restore=dict(ok=False, error=str(error)[:300], attempts=attempts, reason=reason))
    return journal.save(record)


def _dropped(read_status):
    if read_status is None:
        return None
    try:
        return _int((read_status() or {}).get('telemetry_adc_dropped'))
    except Exception:                   # noqa: BLE001
        return None


def public(record):
    """The fields reported in owner status / shot outcomes."""
    if not record:
        return None
    keys = ('state', 'key', 'original', 'used', 'sigma_counts', 's_min_counts', 'thr_lo', 'thr_hi', 'flags', 'readback',
            'restore', 'restore_readback', 'dropped_before', 'dropped_after', 'dropped_delta', 'applied_unix_s',
            'restored_unix_s', 'planned_unix_s', 'request_id', 'plan_digest', 'write_error', 'error')
    return {k: record.get(k) for k in keys if k in record}
