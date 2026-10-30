"""Display-only state interpretation. Never used to admit a control action.

An absent job, a stopped controller, and a disabled PI are not evidence that
laser emission is off. Only a fresh explicit output-enable readback proves OFF.
"""
from datetime import datetime
import math
import time


STATE_LABELS = {
    0: ('STOPPED · OUTPUT UNKNOWN', 'gray'),
    1: ('RECOVERING', 'orange'), 2: ('ACQUIRING', 'blue'),
    3: ('LOCKED', 'green'), 4: ('MODE LOST', 'orange'),
    5: ('FAULT', 'red'), 6: ('DEGRADED', 'orange'),
    7: ('WAITING FOR MEASUREMENT', 'orange'), 8: ('TRACKING', 'blue'),
    9: ('UNKNOWN', 'gray'), 10: ('UNKNOWN · PI ON', 'gray'),
    11: ('HELD · PI OFF', 'yellow'), 12: ('UNKNOWN', 'gray'),
    13: ('OFF · OUTPUT DISABLED', 'gray'),
    22: ('DITHER HIGH', 'orange'), 23: ('MODE + DITHER DEGRADED', 'orange'),
}
CONTROL_OPERATIONS = ('servo.ecdl_steady_lock', 'servo.native_current',
                      'servo.current_pid', 'servo.slow_pid',
                      'servo.dlcpro_frequency', 'sweep.dlcpro_frequency')
MAX_DISPLAY_AGE_S = 25


def state_mappings():
    return [dict(type='value', options={str(code): dict(text=label, color=color)
                                      for code, (label, color) in STATE_LABELS.items()})]


def fresh(stamp, now, max_age_s=MAX_DISPLAY_AGE_S):
    try:
        if isinstance(stamp, str):
            stamp = datetime.fromisoformat(stamp.replace('Z', '+00:00')).timestamp()
        return type(stamp) in (int, float) and math.isfinite(stamp) and 0 <= now - stamp <= max_age_s
    except (ValueError, OverflowError):
        return False


def display_state(jobs, *, now=None, owner=None):
    """Merge fresh controller evidence, excluding residents/watchers/monitors.

Owner is optional sanitized evidence with its own recorded_unix_s. Job latest
must have its own timestamp (or time_s agreeing with progress.simulated_s);
progress heartbeats alone must not rejuvenate an old reading.
"""
    now = time.time() if now is None else now
    def result(code, reason, job=None):
        return dict(code=code, state=STATE_LABELS[code][0], reason=reason,
                    job_id=job.get('id') if job else None)
    evidence = None
    if owner and fresh(owner.get('recorded_unix_s'), now):
        evidence = owner
        if evidence.get('read_ok') is not True:
            return result(12, 'Latest owner read is unavailable')
        if evidence.get('owner_fault') is True:
            return result(5, 'Fresh owner fault')
        if evidence.get('output_enabled') is False:
            return result(13, 'Fresh explicit output-disabled readback')
        if evidence.get('native_pi_enabled') is False:
            return result(11, 'Fresh PI-disabled readback; emission is not known to be off')
        if evidence.get('frequency_valid') is False:
            return result(12, 'Latest owner optical input is unavailable')
    controls = [job for job in jobs if any(c.get('operation', c.get('card')) in CONTROL_OPERATIONS
                for c in job.get('cards', [])) and job.get('status') in ('running', 'stopping', 'finishing')]
    if len(controls) != 1:
        return result(12, 'No unique active controller; physical output is unknown')
    job = controls[0]
    if not fresh(job.get('updated_utc'), now):
        return result(12, 'Controller record is stale', job)
    state = job.get('controller_state')
    row = job.get('latest') or {}
    stamp = row.get('recorded_unix_s', row.get('source_acquired_unix_s'))
    same_cycle = (type(row.get('time_s')) in (int, float)
                  and type(job.get('simulated_s')) in (int, float)
                  and 0 <= job['simulated_s'] - row['time_s'] <= 2)
    measured = fresh(stamp, now) if stamp is not None else same_cycle
    if state == 'waiting_for_measurement':
        return result(7, 'Controller is holding while waiting for a measurement', job)
    if not measured:
        return result(12, 'No fresh controller measurement', job)
    if row.get('output_enabled') is False:
        return result(13, 'Fresh explicit output-disabled readback', job)
    if row.get('native_pi_active') is False:
        return result(11, 'Fresh controller reports PI disabled', job)
    from ldcon.web.dither_quality import evaluate
    dither = evaluate(row, job.get('dither_quality_policy'), now)
    mode_bad = state == 'mode_degraded' or row.get('single_mode') is False
    if state in ('locked', 'tracking', 'mode_degraded') and dither['high'] is True:
        value = result(23 if mode_bad else 22,
                       ('Optical quality is degraded; ' if mode_bad else '') + dither['reason'], job)
        return dict(value, dither_quality=dither)
    if mode_bad:
        return dict(result(6, 'Controller reports degraded optical quality', job), dither_quality=dither)
    if row.get('locked') is True:
        return dict(result(3, 'Fresh controller sample satisfies its configured lock criteria', job), dither_quality=dither)
    if state in ('tracking', 'sweeping', 'acquiring', 'recovering', 'confirming_mode_loss'):
        return result({'tracking': 8, 'sweeping': 8, 'acquiring': 2, 'recovering': 1, 'confirming_mode_loss': 4}[state],
                      'Fresh controller state', job)
    if state == 'locked' and row.get('locked') is False and row.get('native_pi_active') is True:
        return result(8, 'PI is active; latest sample is outside the configured lock criteria', job)
    return result(12, 'Fresh data does not establish lock or output-off', job)
