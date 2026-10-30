"""Sweep API v1.4 (+ v1.5.1: a running continuous triangle is a non-final sweep proxy -> 409 busy) (human, 2026-10-23): one locked-staircase shot per laser at a time, INCLUDING its post-shot settle.

While a previous sweep proxy job of the laser is not final (still settling, or otherwise running), a new sweep is
refused with a machine-readable 409 -- on the raw jobs path (start_interface_card) and on the sweep facade (ARM) -- so
that labscript's transition_to_buffered fails the shot instead of queueing it behind the settle.

  code 'settling'  the previous shot's outcome is published (BLACS may have finished it) and the lock is settling;
                   ``settle_deadline_unix_s`` is the proxy's bound for the settle check.
  code 'busy'      a previous sweep proxy job is active before its outcome (arming/armed/playing/restoring);
                   ``deadline_unix_s`` is the proxy's own lifetime bound.
"""
import math
import time

TERMINAL = frozenset({'succeeded', 'completed', 'completed_with_failures', 'completed_unsettled', 'failed', 'cancelled',
                      'interrupted', 'inconclusive'})


def _sweep_job(job):
    return any(str(c.get('operation', c.get('card'))).startswith('sweep.') for c in job.get('cards', []))


def sweep_blocker(jobs, *, now=None, exclude=None):
    """The blocking sweep proxy job among ``jobs`` (one laser), as a JSON-ready dict, or None."""
    now = time.time() if now is None else now
    for job in jobs:
        if job.get('id') == exclude or job.get('status') in TERMINAL or not _sweep_job(job):
            continue
        shot = job.get('shot_sweep') or {}
        outcome = job.get('outcome') or shot.get('outcome')
        settle = (outcome or {}).get('settle') or {}
        if outcome and settle.get('state') == 'settling':
            deadline = settle.get('deadline_unix_s')
            code, detail = 'settling', 'The previous sweep on this laser is still settling (its outcome is published)'
        else:
            deadline = shot.get('proxy_deadline_unix_s')
            code, detail = 'busy', 'A previous sweep on this laser is still active'
            if shot.get('trigger_mode') == 'continuous' or (outcome or {}).get('kind') == 'continuous':
                # v1.5.1: a running continuous triangle blocks shots (stop it first: POST .../cancel)
                detail = 'A continuous triangle is running on this laser; stop it first'
        retry = max(1, int(math.ceil(deadline - now))) if isinstance(deadline, (int, float)) else 5
        return dict(code=code, detail=detail, blocking_job=job['id'], blocking_state=shot.get('state') or job.get('status'),
                    blocking_verdict=(outcome or {}).get('verdict'), deadline_unix_s=deadline,
                    settle_deadline_unix_s=deadline if code == 'settling' else None, retry_after_s=retry)
    return None
