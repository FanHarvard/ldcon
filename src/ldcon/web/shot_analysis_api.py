"""Read side of the post-shot analysis (sweep API v1.5, contract ldcon.shot-analysis/v1). No device access.

- ``overlay_job``: the proxy publishes ``outcome.analysis = {state: pending, ref}`` when it launches the analysis; the
  detached analysis then writes ``<proxy job>/shot-analysis/status.json`` (+ result.json). Every GET of the job (the raw
  ``GET jobs/<id>`` and the facade, which reads the job) shows the CURRENT state from that file: pending -> ready |
  unavailable. When ready, the outcome's residual summary (``rms_error_hz``, ``max_abs_error_hz``,
  ``levels_measured``) comes from the analysis (``residual_source: shot_analysis``); the proxy's preliminary values
  stay in ``preliminary_residuals``. The verdict and every other outcome field never change.
- A pending/running state past its deadline (+5 s) reads ``unavailable`` (the child died or hung), so a client's wait
  always ends.
- Routes: ``GET jobs/<proxy>/analysis`` and ``GET lasers/<laser>/sweeps/<sweep_id>/analysis``.
"""
import time
from pathlib import Path

from ldcon.jobs.shot_analysis import CONTRACT, read_result, read_status, ref_for

STALE_MARGIN_S = 5.
RESIDUAL_KEYS = ('levels_measured', 'max_abs_error_hz', 'rms_error_hz', 'residual_source')


def child_gone(status):
    """True when the recorded analysis pid no longer exists on this host (a status written on another boot or without a
    pid is left to the deadline rule)."""
    import os
    pid = status.get('pid')
    if not isinstance(pid, int) or pid <= 0 or status.get('state') == 'pending' and not status.get('started_unix_s'):
        return False
    try:
        stat = open('/proc/%d/stat' % pid).read()
    except OSError:
        return True
    return stat.rsplit(')', 1)[-1].split()[0] in ('Z', 'X')        # a zombie (not yet reaped) is gone as well


def current_status(directory, *, now=time.time):
    status = read_status(directory)
    if not isinstance(status, dict) or not status.get('state'):
        return None
    if status['state'] in ('pending', 'running') and child_gone(status):
        # 2026-10-23 06:17Z (sr422 2883041...): the child ended without a result (no traceback, no final status)
        return dict(status, state='unavailable', ref=None,
                    reason='the analysis process ended without a result (%s; re-run with the analyze.shot_sweep card)' % status['state'])
    if status['state'] in ('pending', 'running'):
        deadline = status.get('deadline_unix_s')
        if deadline is None and status.get('started_unix_s'):
            deadline = status['started_unix_s'] + 60.
        if deadline is not None and now() > float(deadline) + STALE_MARGIN_S:
            status = dict(status, state='unavailable', ref=None,
                          reason='the analysis did not finish by its deadline (%s)' % status['state'])
    return status


def overlay_outcome(outcome, status):
    if not isinstance(outcome, dict) or not isinstance(status, dict):
        return outcome
    analysis = dict(outcome.get('analysis') or {})
    analysis.update(schema=CONTRACT, state=status['state'], ref=status.get('ref') if status['state'] == 'ready' else
                    (analysis.get('ref') if status['state'] in ('pending', 'running') else None),
                    reason=status.get('reason'))
    if status['state'] == 'running':
        analysis['state'] = 'pending'               # the contract has pending|ready|unavailable
    for key in ('mode', 'deadline_unix_s', 'finished_unix_s', 'runtime_s', 'source', 'quality_ok', 'summary', 't0_unix_s', 'warnings'):
        if status.get(key) is not None:
            analysis[key] = status[key]
    new = dict(outcome, analysis=analysis)
    summary = status.get('summary') or {}
    if status['state'] == 'ready' and summary.get('levels_measured') is not None:      # levels mode only (raw has no residuals)
        new['preliminary_residuals'] = outcome.get('preliminary_residuals') or {k: outcome.get(k) for k in RESIDUAL_KEYS}
        new.update(levels_measured=summary.get('levels_measured'), max_abs_error_hz=summary.get('max_abs_error_hz'),
                   rms_error_hz=summary.get('rms_error_hz'), residual_source='shot_analysis')
    if status.get('adc_log_threshold') is not None or status['state'] == 'ready':
        new['adc_log_threshold'] = status.get('adc_log_threshold')
    return new


def overlay_job(directory, job, *, now=time.time):
    """Fold the analysis state into ``outcome`` (top level and shot_sweep.outcome) of a GET jobs/<id> payload."""
    try:
        if not isinstance(job, dict) or not (job.get('outcome') or (job.get('shot_sweep') or {}).get('outcome')):
            return job
        status = current_status(directory, now=now)
        if status is None:
            return job
        if isinstance(job.get('outcome'), dict):
            job['outcome'] = overlay_outcome(job['outcome'], status)
        shot = job.get('shot_sweep')
        if isinstance(shot, dict) and isinstance(shot.get('outcome'), dict):
            job['shot_sweep'] = dict(shot, outcome=overlay_outcome(shot['outcome'], status))
    except Exception:          # noqa: BLE001 - a read-side overlay never breaks the job GET
        pass
    return job


def analysis_payload(directory, job_id, *, now=time.time):
    """GET .../analysis: {schema, state, ref, reason, ..., result: <ldcon.shot-analysis/v1 document or null>}."""
    status = current_status(directory, now=now)
    if status is None:
        return dict(schema=CONTRACT, proxy_job=job_id, state='unavailable', ref=None,
                    reason='no post-shot analysis exists for this job', result=None)
    out = {k: v for k, v in status.items() if k not in ('pid',)}
    out.update(schema=CONTRACT, proxy_job=job_id, state='pending' if status['state'] == 'running' else status['state'])
    out['ref'] = ref_for(job_id) if out['state'] in ('pending', 'ready') else None
    out['result'] = read_result(directory) if status['state'] == 'ready' else None
    return out


def install_routes(app, prefix):
    import asyncio

    from fastapi.responses import JSONResponse

    def job_directory(job_id):
        runtime = app.state.runtime
        return runtime.directory(job_id)

    @app.get(prefix + '/api/jobs/{job_id}/analysis')
    async def job_analysis(job_id: str):
        try:
            directory = await asyncio.to_thread(job_directory, job_id)
        except (FileNotFoundError, ValueError):
            return JSONResponse({'detail': 'Not found'}, status_code=404)
        return await asyncio.to_thread(analysis_payload, directory, job_id)

    @app.get(prefix + '/api/lasers/{laser_id}/sweeps/{sweep_id}/analysis')
    async def sweep_analysis(laser_id: str, sweep_id: str):
        try:
            directory = await asyncio.to_thread(job_directory, sweep_id)
        except (FileNotFoundError, ValueError):
            return JSONResponse({'code': 'not_found', 'detail': 'Unknown sweep'}, status_code=404)
        from ldcon.jobs.shot_analysis import _read_json
        meta = _read_json(Path(directory) / 'job.json', {}) or {}
        laser = meta.get('laser')
        if (laser.get('id') if isinstance(laser, dict) else laser) != laser_id:
            return JSONResponse({'code': 'not_found', 'detail': 'Unknown sweep'}, status_code=404)
        return await asyncio.to_thread(analysis_payload, directory, sweep_id)
