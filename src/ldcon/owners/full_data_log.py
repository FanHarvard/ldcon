"""Temporary fast ESP32 full-data logging during a sweep (human request 2026-10-23 14:13Z).

Purpose: 1 ms dac_val / mod_raw / system_value records to locate the sr422 response lag (with the PZT FF gain 1.0 only
0.08 of a step arrives in the first 50 ms after the ADC edge). The firmware records a full-data row at most every
``log_full_data_min_interval`` ms (servo_controller.cpp: log_full_data_min_interval_us_; 100 ms in service); rows forced
by a new wavemeter value are governed separately by ``log_forced_full_data_min_interval`` (0 = no thinning) and are left
alone. It is a TELEMETRY setting (no PI/FF/target/DAC effect), so it never faults the owner, never aborts the sweep and
is never written while the owner is in fault.

Same pattern as adc_log_threshold.py: journal before the write (original, requested), one write, read back /config; on
every exit (normal restore, abort, owner restart) one write of the original and a read-back. The telemetry drop counters
of /status are recorded before and after (the 1 kHz rows go through the same telemetry buffer).
Zero selects every servo cycle; with polling_interval=1 this is nominally 1 kHz.
A positive value is a minimum time gate and may skip slightly-early control ticks.
A per-shot option ``full_data_log_ms`` (integer 0..100) requests it; without the option nothing is written.
"""
import math
import time

from ldcon.owners import adc_log_threshold as base

KEY = 'log_full_data_min_interval'          # ms (firmware /config); the original is restored at the end of the sweep
MIN_MS, MAX_MS = 0, 100
DROP_KEYS = ('telemetry_buffer_dropped', 'telemetry_input_dropped', 'telemetry_adc_dropped', 'telemetry_failed_requests')


class Journal(base.Journal):
    """The adc_log_threshold journal (state file + bounded history) for this key."""


def validate_ms(value):
    if value is None:
        return None
    if type(value) is not int or not MIN_MS <= value <= MAX_MS:
        raise ValueError('full_data_log_ms must be an integer %d..%d (ms between full-data rows during the sweep)' % (MIN_MS, MAX_MS))
    return value


def _number(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def _drops(read_status):
    if read_status is None:
        return None
    try:
        status = read_status() or {}
        return {k: status.get(k) for k in DROP_KEYS if k in status}
    except Exception:                   # noqa: BLE001
        return None


def _delta(before, after):
    if not isinstance(before, dict) or not isinstance(after, dict):
        return None
    return {k: after[k] - before[k] for k in DROP_KEYS
            if isinstance(before.get(k), (int, float)) and isinstance(after.get(k), (int, float))}


def apply(journal, *, write, read_config, read_status=None, requested_ms, faulted=False, context=None):
    """Journal, write once, read back. Never raises; returns the record (also kept in the journal)."""
    context = dict(context or {})
    try:
        validate_ms(requested_ms)
        if requested_ms is None:
            return None
        if faulted:
            return dict(state='skipped', key=KEY, requested_ms=requested_ms, reason='owner in fault', **context)
        if journal.needs_restore():
            restore(journal, write=write, read_config=read_config, read_status=read_status, reason='before a new arm')
        prior = journal.record or {}
        current = _number(read_config().get(KEY))
        original = prior.get('original') if prior.get('state') in ('applying', 'applied', 'restore_failed') else current
        record = dict(key=KEY, requested_ms=requested_ms, original=original, used=requested_ms, current_before=current,
                      planned_unix_s=time.time(), drops_before=_drops(read_status), restore=None, **context)
        if original is None:
            record.update(state='failed', error='original %s unknown' % KEY)
            return journal.save(record)
        if current == requested_ms:
            record.update(state='applied' if original != requested_ms else 'unchanged', applied_unix_s=time.time())
            return journal.save(record)
        record['state'] = 'applying'
        journal.save(record)
        try:
            write({KEY: requested_ms})
            back = _number(read_config().get(KEY))
        except Exception as error:      # noqa: BLE001 - telemetry only; the restore path writes the original
            record.update(write_error=str(error)[:300], readback=None)
            return journal.save(record)
        record.update(readback=back, applied_unix_s=time.time(), state='applied' if back == requested_ms else 'applying')
        if back != requested_ms:
            record['write_error'] = 'readback %s != %s' % (back, requested_ms)
        return journal.save(record)
    except Exception as error:          # noqa: BLE001
        return dict(state='failed', key=KEY, requested_ms=requested_ms, error=str(error)[:300], **context)


def restore(journal, *, write, read_config, read_status=None, reason='sweep finished', now=time.time):
    """Write the journal's original once and read it back. Never raises; bounded attempts (adc_log_threshold rules)."""
    record = dict(journal.record or {})
    if not journal.needs_restore():
        return record
    last = record.get('last_restore_attempt_unix_s')
    if last is not None and now() - float(last) < base.RESTORE_RETRY_S:
        return record
    attempts = int(record.get('restore_attempts') or 0) + 1
    record.update(restore_attempts=attempts, last_restore_attempt_unix_s=now(), restore_reason=reason)
    try:
        current = _number(read_config().get(KEY))
        if current != record['original']:
            write({KEY: record['original']})
            current = _number(read_config().get(KEY))
        ok = current == record['original']
        after = _drops(read_status)
        record.update(state='restored' if ok else 'restore_failed', restore_readback=current,
                      restored_unix_s=now() if ok else None, drops_after=after,
                      drops_delta=_delta(record.get('drops_before'), after),
                      restore=dict(ok=ok, readback=current, attempts=attempts, reason=reason))
    except Exception as error:          # noqa: BLE001
        record.update(state='restore_failed', restore=dict(ok=False, error=str(error)[:300], attempts=attempts, reason=reason))
    return journal.save(record)


def public(record):
    """The block reported in the owner status, the outcome (tracking) and the analysis temporary_settings."""
    if not record:
        return None
    keys = ('state', 'key', 'requested_ms', 'original', 'used', 'readback', 'restore', 'restore_readback', 'drops_before',
            'drops_after', 'drops_delta', 'applied_unix_s', 'restored_unix_s', 'planned_unix_s', 'request_id',
            'plan_digest', 'write_error', 'error', 'reason')
    out = {k: record.get(k) for k in keys if k in record}
    out['restored'] = record.get('state') == 'restored' or record.get('state') == 'unchanged'
    return out
