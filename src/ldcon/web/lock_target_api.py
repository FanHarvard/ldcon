"""Lock-target HTTP API for labscript (contract v1, 2026-10-22: preparations/lock-target-api-20261022/API.md).

A thin facade over the existing ``tune.lock_target`` card (``lock_target.run_lock_target``). It never talks to an
owner or instrument and never edits the laser definition itself: POST starts the deck's ``tune.lock_target`` card as
an ordinary job (the same start path as the dashboard), GET maps that job's published progress and final status.

Routes (under the dashboard prefix ``/s/<token>/``; POSTs need ``X-LDCon-Client: labscript`` or ``web``):

  GET  api/lasers/<laser>/target
  POST api/lasers/<laser>/target/changes                       {target_frequency_hz, request_id, timeout_s?}
  GET  api/lasers/<laser>/target/changes?request_id=<id>       (v1.1: lookup after a lost response; 404 if unknown)
  GET  api/lasers/<laser>/target/changes/<change_id>
  POST api/lasers/<laser>/target/changes/<change_id>/cancel    (v1.1: 202 'cancelling' -> final 'cancelled' once the
                                                                owner's ramp stopped; 200 unchanged on a final change)

Settling is the card's criterion only (lock error within tolerance for the last verify_samples samples, ramp ended);
no TEC/current settle condition is added (human decision 2026-10-22).

Persistence (survives a web restart): ``<state>/lock-target-api/<laser>/requests.json`` (request_id -> change_id)
and ``changes/<change_id>.json`` (one record per change). The change id IS the card job id, so the job store is the
source of truth for single flight: a non-terminal ``tune.lock_target`` job of this laser means 409 busy.
"""
from ldcon.core.target_policy import target_bounds

import math
import re
import time
from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse

ROUTE = r'/api/lasers/[A-Za-z0-9][A-Za-z0-9_.-]{0,79}/target/changes(?:/[A-Za-z0-9][A-Za-z0-9_.-]{0,79}/cancel)?'
REQUEST_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}')
OPERATION = 'tune.lock_target'
DEFAULT_TIMEOUT_S = 900.
MAX_TIMEOUT_S = 86400.
FRESH_S = 30.
KEEP_S = 7 * 86400.
FINAL = ('settled', 'failed')
PROGRESS_STATES = {'saved': 'saved', 'ramping': 'ramping', 'verifying': 'verifying',
                   'waiting_for_tec_center': 'verifying'}
FAILED_STATUSES = ('failed', 'interrupted', 'inconclusive', 'completed_with_failures')


class ApiError(Exception):
    def __init__(self, status, code, detail, **extra):
        super().__init__(detail)
        self.status, self.code, self.detail, self.extra = status, code, detail, extra

    def response(self):
        return JSONResponse({'code': self.code, 'detail': self.detail, **self.extra}, status_code=self.status)


def labscript_route(prefix, path):
    """True for the POST routes that accept ``X-LDCon-Client: labscript`` (only these)."""
    return re.fullmatch(re.escape(prefix) + ROUTE, path) is not None


def _now():
    return time.time()


def _op(card):
    return card.get('operation', card.get('card'))


class LockTargetApi:
    def __init__(self, runtime, *, clock=_now):
        self.runtime, self.clock = runtime, clock

    # ------------------------------------------------------------------ storage
    def _dir(self, laser_id):
        from ldcon.core.records import safe_id
        path = self.runtime.root / 'lock-target-api' / safe_id(laser_id)
        (path / 'changes').mkdir(parents=True, exist_ok=True)
        return path

    def _requests(self, laser_id):
        from ldcon.core.records import read_json
        value = read_json(self._dir(laser_id) / 'requests.json', {})
        return value if isinstance(value, dict) else {}

    def _record(self, laser_id, change_id):
        from ldcon.core.records import read_json, safe_id
        return read_json(self._dir(laser_id) / 'changes' / (safe_id(change_id) + '.json'), None)

    def _save_record(self, laser_id, record):
        from ldcon.core.result_reservations import durable_json
        durable_json(self._dir(laser_id) / 'changes' / (record['change_id'] + '.json'), record)

    def _save_requests(self, laser_id, mapping):
        from ldcon.core.result_reservations import durable_json
        durable_json(self._dir(laser_id) / 'requests.json', mapping)

    def _prune(self, laser_id, mapping):
        now = self.clock()
        keep = {}
        for request_id, entry in mapping.items():
            if isinstance(entry, dict) and now - float(entry.get('created_unix_s', 0)) <= KEEP_S:
                keep[request_id] = entry
        for path in (self._dir(laser_id) / 'changes').glob('*.json'):
            try:
                if now - path.stat().st_mtime > KEEP_S and path.stem not in {e['change_id'] for e in keep.values()}:
                    path.unlink()
            except OSError:
                pass
        return keep

    # ------------------------------------------------------------------ configuration
    def _laser(self, laser_id):
        from ldcon.core.records import safe_id
        try:
            return self.runtime.interface_repository.read('lasers', safe_id(laser_id))['document']
        except (FileNotFoundError, ValueError):
            raise ApiError(404, 'not_found', 'Unknown laser') from None

    def _assignment(self, laser_id):
        """(projected set, deck definition, lock_target card id) of the assignment that carries this laser's card."""
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
                    if card['operation'] == OPERATION:
                        found.append((value, definition, card['id']))
        if not found:
            raise ApiError(409, 'not_configured', 'No interface deck of this laser carries a tune.lock_target card')
        found.sort(key=lambda f: (f[2] != 'change-target', f[1]['id']))
        return found[0]

    @staticmethod
    def _pins(params):
        return params.get('target_frequency_hz') is not None or params.get('target_wavelength_nm') is not None

    # ------------------------------------------------------------------ jobs
    def _jobs(self, laser_id):
        return [j for j in self.runtime.active_jobs() if (j.get('laser') or {}).get('id') == laser_id]

    def _lock(self, laser, jobs):
        """(lock job or None, its card params, live lock-target.json) for the laser's lock operation."""
        from ldcon.jobs.operating_point import lock_operation
        from ldcon.web.web_runtime import TERMINAL, read_json
        try:
            operation = lock_operation(laser)
        except Exception:
            return None, {}, {}, None
        for job in jobs:
            if job['status'] in TERMINAL or not any(_op(c) == operation for c in job.get('cards', [])):
                continue
            params = (job.get('cards') or [{}])[0].get('params') or {}
            live = read_json(self.runtime.directory(job['id']) / 'lock-target.json', {}) or {}
            return job, params, live, operation
        return None, {}, {}, operation

    def _is_locked(self, lock, live):
        """v1.1 fix (2026-10-22 18:04Z): 'locked' comes from the lock's OWN per-cycle report (lock-target.json, written
        by both lock cards from the owner row: sr422 ecdl_steady row['state'], sr1092 'locked' while the native PI is
        active), not from progress.json controller_state. The sr422 card's controller_state is a display state that
        flips to 'waiting_for_measurement' for ~1.3 s on every wavelength-meter dropout (about every 35 s), and to
        'tracking'/'mode_degraded' -- the lock (firmware PI) keeps running through all of them.
        Fallback when the lock has no fresh report: controller_state == 'locked'."""
        if lock is None:
            return False
        if self._fresh(live) and live.get('state') is not None:
            return live.get('state') == 'locked'
        return lock.get('controller_state') == 'locked'

    def _fresh(self, live):
        stamp = live.get('updated_unix_s')
        return isinstance(stamp, (int, float)) and self.clock() - stamp <= FRESH_S

    # ------------------------------------------------------------------ GET target
    def get_target(self, laser_id):
        from ldcon.jobs.lock_target import laser_target, C_NM
        laser = self._laser(laser_id)
        try:
            block = laser_target(laser)
        except ValueError as error:
            raise ApiError(409, 'not_configured', str(error)) from None
        with self.runtime.mutex:
            jobs = self._jobs(laser_id)
            lock, params, live, operation = self._lock(laser, jobs)
            follows, follow_note = self._follows(laser_id, laser, lock, params, operation)
            change = self._current_change(laser_id, jobs)
        frequency = float(block['frequency_hz']) if block else None
        result = dict(laser=laser_id, frequency_hz=frequency, wavelength_nm=C_NM / frequency if frequency else None,
                      bounds_hz=target_bounds(laser),
                      allowed_change_hz=block.get('allowed_change_hz') if block else None,
                      bounds_reference_hz=frequency,
                      max_change_hz=float(block['max_change_hz']) if block and block.get('max_change_hz') is not None else None,
                      locked=self._is_locked(lock, live),
                      lock_job=lock['id'] if lock else None, follows_laser_target=follows, change=change)
        if lock:
            result.update(lock_state=lock.get('controller_state'), ramp_active=bool(self._fresh(live) and live.get('ramp_active')),
                          ramped_target_hz=live.get('ramped_target_hz') if self._fresh(live) else None)
        # v1.1: informational only, never used for settling (null without a fresh lock report)
        result['measured_frequency_hz'] = live.get('frequency_hz') if lock and self._fresh(live) else None
        # v1.2 (target-ff design, 2026-10-27): informational only; max_change_hz may change after a study adoption,
        # clients re-read it before each step (contract). tec_centering is a report, never a settling condition.
        if lock and self._fresh(live):
            result.update(pzt_headroom_hz=live.get('pzt_headroom_hz'), dac_headroom_hz=live.get('dac_headroom_hz'),
                          integral_fraction=live.get('integral_fraction'), recentre_active=live.get('recentre_active'),
                          tec_centering=live.get('tec_centering'))
        if follow_note:
            result['follow_note'] = follow_note
        return result

    def _follows(self, laser_id, laser, lock, params, operation):
        if lock is not None:
            if self._pins(params):
                return False, 'lock job %s pins its own target; relock with target_frequency_hz null to follow' % lock['id'][:8]
            return True, None
        # Unlocked: the next lock start decides. A deck lock card that pins its target would ignore the saved value.
        try:
            value, definition, _ = self._assignment(laser_id)
        except ApiError:
            return True, None
        for card in definition['document']['cards']:
            if card['operation'] == operation:
                status = definition['card_status'].get(card['id'], {})
                if self._pins(status.get('parameters') or {}):
                    return False, ('assignment card %s pins target_frequency_hz; set it to null so the next lock start '
                                   'follows the laser target' % card['id'])
        return True, None

    def _current_change(self, laser_id, jobs):
        records = []
        for path in (self._dir(laser_id) / 'changes').glob('*.json'):
            from ldcon.core.records import read_json
            record = read_json(path, None)
            if isinstance(record, dict) and 'change_id' in record:
                records.append(record)
        for record in sorted(records, key=lambda r: r.get('created_unix_s', 0), reverse=True):
            try:
                if self.runtime.job_terminal_confirmed(record['change_id']):
                    # Cancelled observers still block while their owner's ramp continues.
                    from ldcon.core.records import read_json
                    progress = read_json(self.runtime.directory(record['change_id']) / 'progress.json', {})
                    if progress.get('status') != 'cancelled' or not self._ramp_active(progress, record):
                        continue
            except FileNotFoundError:
                pass  # Preserve the existing missing-job / uncertain-dispatch grace period.
            state = self._state(laser_id, record)
            if not state['final']:
                return dict(change_id=record['change_id'], request_id=record.get('request_id'), state=state['state'])
        return None

    # ------------------------------------------------------------------ POST change
    def start_change(self, laser_id, body, *, change_id=None):
        if not isinstance(body, dict):
            raise ApiError(400, 'invalid_request', 'Expected a JSON object')
        unknown = set(body) - {'target_frequency_hz', 'request_id', 'timeout_s'}
        if unknown:
            raise ApiError(400, 'invalid_request', 'Unknown fields: ' + ', '.join(sorted(unknown)))
        target = body.get('target_frequency_hz')
        if type(target) not in (int, float) or not math.isfinite(target) or target <= 0:
            raise ApiError(400, 'invalid_request', 'target_frequency_hz must be a positive finite number')
        target = float(target)
        request_id = body.get('request_id')
        if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
            raise ApiError(400, 'invalid_request', 'request_id must be 1..128 characters [A-Za-z0-9_.:-]')
        timeout_s = body.get('timeout_s', DEFAULT_TIMEOUT_S)
        if timeout_s is None:
            timeout_s = DEFAULT_TIMEOUT_S
        if type(timeout_s) not in (int, float) or not math.isfinite(timeout_s) or not 0 < timeout_s <= MAX_TIMEOUT_S:
            raise ApiError(400, 'invalid_request', 'timeout_s must be in (0, 86400]')
        laser = self._laser(laser_id)
        from ldcon.jobs.lock_target import laser_target
        from ldcon.web.web_runtime import ConflictError, TERMINAL
        with self.runtime.mutex:
            mapping = self._requests(laser_id)
            entry = mapping.get(request_id)
            if entry is not None:
                record = self._record(laser_id, entry['change_id'])
                if record is not None:
                    if abs(float(record['target_frequency_hz']) - target) > 1e-3:
                        raise ApiError(409, 'request_conflict', 'request_id was already used for a different target',
                                       change_id=record['change_id'])
                    return 200, self.change_state(laser_id, record['change_id'])
            try:
                block = laser_target(laser)
            except ValueError as error:
                raise ApiError(409, 'not_configured', str(error)) from None
            if block is None or target_bounds(laser) is None:
                raise ApiError(409, 'not_configured', 'laser.target needs frequency_hz, allowed_change_hz and (while locked) max_change_hz')
            jobs = self._jobs(laser_id)
            active = [j for j in jobs if j['status'] not in TERMINAL and any(_op(c) == OPERATION for c in j.get('cards', []))]
            if active:
                raise ApiError(409, 'busy', 'Another target change is in progress', change_id=active[0]['id'])
            pending = self._current_change(laser_id, jobs)
            if pending is not None:
                raise ApiError(409, 'busy', 'Another target change is in progress (%s)' % pending['state'],
                               change_id=pending['change_id'])
            sweeps = [j for j in jobs if j['status'] not in TERMINAL and any(str(_op(c)).startswith('sweep.') for c in j.get('cards', []))]
            if sweeps:
                raise ApiError(409, 'busy', 'A locked sweep job is active; change the target after it ends', job=sweeps[0]['id'])
            lock, params, live, operation = self._lock(laser, jobs)
            if lock is not None:
                if not self._is_locked(lock, live):
                    raise ApiError(409, 'busy', 'The lock job is running but not locked (lock state %s); retry when locked'
                                   % (live.get('state') if self._fresh(live) else lock.get('controller_state')),
                                   lock_job=lock['id'])
                # The deployed target worker discovers the lock from its fresh
                # per-cycle report; display labels (including mode_degraded)
                # must not reject a physically locked target change.
                if self._fresh(live) and live.get('ramp_active'):
                    raise ApiError(409, 'busy', 'The lock is still ramping to the saved target', lock_job=lock['id'])
            follows, note = self._follows(laser_id, laser, lock, params, operation)
            if not follows:
                raise ApiError(409, 'not_following', note)
            lo, hi = target_bounds(laser)
            present = float(block['frequency_hz'])
            if not lo <= target <= hi:
                raise ApiError(422, 'out_of_bounds', 'New target is outside the admitted target range', bounds_hz=[lo, hi])
            if lock is not None and block.get('max_change_hz') is None:
                raise ApiError(409, 'not_configured', 'Locked target changes require max_change_hz')
            if lock is not None and abs(target - present) > float(block['max_change_hz']) * (1 + 1e-12):
                raise ApiError(422, 'step_too_large', '|new - current| exceeds max_change_hz; split the change into steps',
                               max_change_hz=float(block['max_change_hz']), frequency_hz=present)
            value, definition, card_id = self._assignment(laser_id)
            overrides = dict(target_frequency_hz=target, timeout_s=float(timeout_s))
            if lock is not None and params.get('tolerance_hz') is not None:
                overrides['tolerance_hz'] = float(params['tolerance_hz'])   # "settled" = within the lock's tolerance
            import uuid
            change_id = change_id or uuid.uuid4().hex
            now = self.clock()
            record = dict(change_id=change_id, request_id=request_id, laser=laser_id, target_frequency_hz=target,
                          before_hz=present, admitted_bounds_hz=[lo, hi], timeout_s=float(timeout_s), set_id=value['id'], deck_id=definition['id'],
                          card_id=card_id, parameters=overrides, lock_job=lock['id'] if lock else None,
                          created_unix_s=now)
            # Record first (durable), then start with the same id: a crash in between leaves a record whose job never
            # appeared (reported failed), never a job without its request mapping.
            self._save_record(laser_id, record)
            mapping = self._prune(laser_id, mapping)
            mapping[request_id] = dict(change_id=change_id, created_unix_s=now)
            self._save_requests(laser_id, mapping)
            try:
                self.runtime.start_interface_card(value, definition['id'], card_id, overrides, job_id=change_id)
            except (ConflictError, ValueError, FileNotFoundError) as error:
                mapping.pop(request_id, None)
                self._save_requests(laser_id, mapping)
                try:
                    (self._dir(laser_id) / 'changes' / (change_id + '.json')).unlink()
                except OSError:
                    pass
                if isinstance(error, ConflictError):
                    raise ApiError(409, 'conflict', str(error)) from None
                raise ApiError(422, 'refused', str(error)) from None
        return 202, dict(change_id=change_id, state='accepted', laser=laser_id, request_id=request_id,
                         target_frequency_hz=target)

    # ------------------------------------------------------------------ GET change
    def _state(self, laser_id, record):
        """{state, final, job, message, published} for a change record."""
        from ldcon.web.web_runtime import TERMINAL, read_json
        change_id = record['change_id']
        try:
            job = self.runtime.get_job(change_id)
        except (FileNotFoundError, ValueError):
            if self.clock() - float(record.get('created_unix_s', 0)) < 30:
                return dict(state='accepted', final=False, job=None, message=None, published={})
            return dict(state='failed', final=True, job=None, message='The change job was never created', published={})
        published = job.get('lock_target') or {}
        status = job.get('status')
        message = job.get('message')
        if status in TERMINAL:
            if status in ('succeeded', 'completed'):
                result = read_json(self.runtime.directory(change_id) / 'lock-target-result.json', {}) or {}
                note = result.get('note')
                if result.get('locked') is False:
                    note = note or 'laser unlocked: the saved target applies at the next lock start'
                    if record.get('lock_job') and self._job_running(record['lock_job']):
                        # The card saw the lock in a transient state and treated the laser as unlocked; the running
                        # lock still adopts the saved target, but nobody verified it. Never report that as settled.
                        return dict(state='failed', final=True, job=job, published=published, result=result,
                                    message='The target was saved, but the card treated the running lock %s as unlocked '
                                            '(transient lock state); the lock adopts the saved target without '
                                            'verification. Check GET target before the next step.' % record['lock_job'][:8])
                return dict(state='settled', final=True, job=job, message=note or message, published=published,
                            result=result)
            if status == 'cancelled':
                ramp = self._ramp_active(job, record)
                return dict(state='cancelling' if ramp else 'cancelled', final=not ramp, job=job, published=published, ramp_active=ramp,
                            message=(message or 'Cancelled') + ('' if published.get('status') else
                                                                 ' (before the target was saved; nothing changed)'))
            return dict(state='failed', final=True, job=job, message=message, published=published)
        cancelling = status == 'stopping' or bool(record.get('cancel_requested'))
        state = 'cancelling' if cancelling else PROGRESS_STATES.get(published.get('status'), 'accepted')
        if not cancelling and published.get('status') == 'waiting_for_tec_center':
            message = 'Waiting for TEC/PZT center: ' + str((published.get('tec_centering') or {}).get('reason', 'confirmation pending'))
        return dict(state=state, final=False, job=job, message=message, published=published, cancel_requested=cancelling)

    def _job_running(self, job_id):
        from ldcon.web.web_runtime import TERMINAL
        try:
            return self.runtime.get_job(job_id)['status'] not in TERMINAL
        except (FileNotFoundError, ValueError):
            return False

    def _ramp_active(self, job, record):
        """Whether the running lock still ramps (after a cancel the owner finishes its bounded ramp)."""
        from ldcon.web.web_runtime import TERMINAL, read_json
        lock_id = record.get('lock_job')
        if not lock_id:
            return False
        try:
            lock = self.runtime.get_job(lock_id)
        except (FileNotFoundError, ValueError):
            return False
        if lock['status'] in TERMINAL:
            return False
        live = read_json(self.runtime.directory(lock_id) / 'lock-target.json', {}) or {}
        return bool(self._fresh(live) and live.get('ramp_active'))

    def change_state(self, laser_id, change_id):
        from ldcon.core.records import read_json, safe_id
        self._laser(laser_id)
        try:
            safe_id(change_id)
        except ValueError:
            raise ApiError(404, 'not_found', 'Unknown change') from None
        with self.runtime.mutex:
            record = self._record(laser_id, change_id)
            if record is None:
                raise ApiError(404, 'not_found', 'Unknown change')
            state = self._state(laser_id, record)
            published = state['published']
            live = {}
            if record.get('lock_job'):
                try:
                    live = read_json(self.runtime.directory(record['lock_job']) / 'lock-target.json', {}) or {}
                except (FileNotFoundError, ValueError):
                    live = {}
            if not self._fresh(live):
                live = {}
            updated = [s for s in (live.get('updated_unix_s'),) if isinstance(s, (int, float))]
            if state['job'] is not None:
                try:
                    updated.append((self.runtime.directory(change_id) / 'progress.json').stat().st_mtime)
                except OSError:
                    pass
        target = float(record['target_frequency_hz'])
        frequency = live.get('frequency_hz') or published.get('frequency_hz')
        result = dict(change_id=change_id, state=state['state'], target_frequency_hz=target,
                      before_hz=record.get('before_hz'), frequency_hz=frequency,
                      error_hz=(float(frequency) - target) if frequency else None,
                      ramped_target_hz=live.get('ramped_target_hz', published.get('ramped_target_hz')),
                      message=state.get('message'), updated_unix_s=max(updated) if updated else record.get('created_unix_s'),
                      final=state['final'], request_id=record.get('request_id'), laser=laser_id)
        if 'ramp_active' in state:
            result['ramp_active'] = state['ramp_active']
        if state.get('cancel_requested'):
            result['cancel_requested'] = True
        # v1.1: true only when a running lock adopted the target and settled (false for the saved-only/unlocked case)
        result['applied_to_lock'] = state['state'] == 'settled' and (state.get('result') or {}).get('locked') is True
        verify = (state.get('result') or {}).get('verify')
        if verify:
            result['verify'] = verify
        if published.get('tec_centering'):
            result['tec_centering'] = published['tec_centering']
        # v1.2 (2026-10-27): report-only owner fields of the step (headroom, integral usage, hybrid recentre)
        for key in ('pzt_headroom_hz', 'dac_headroom_hz', 'integral_fraction', 'owner_recentre', 'recentre_active'):
            value = live.get(key, published.get(key))
            if value is not None:
                result[key] = value
        return result

    def change_by_request(self, laser_id, request_id):
        self._laser(laser_id)
        if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
            raise ApiError(400, 'invalid_request', 'request_id must be 1..128 characters [A-Za-z0-9_.:-]')
        with self.runtime.mutex:
            entry = self._requests(laser_id).get(request_id)
            if entry is None or self._record(laser_id, entry['change_id']) is None:
                raise ApiError(404, 'not_found', 'Unknown request_id')
        return self.change_state(laser_id, entry['change_id'])

    # ------------------------------------------------------------------ cancel
    def cancel(self, laser_id, change_id):
        from ldcon.web.web_runtime import TERMINAL
        self._laser(laser_id)
        with self.runtime.mutex:
            record = self._record(laser_id, change_id)
            if record is None:
                raise ApiError(404, 'not_found', 'Unknown change')
            if self._state(laser_id, record)['final']:
                return 200, self.change_state(laser_id, change_id)      # v1.1: a final change is left unchanged
            try:
                job = self.runtime.get_job(change_id)
            except (FileNotFoundError, ValueError):
                job = None
            record['cancel_requested'] = True
            self._save_record(laser_id, record)
            if job is not None and job['status'] not in TERMINAL:
                self.runtime.stop(change_id)            # the job stop API (cancel.request); the card raises CardCancelled
        return 202, dict(change_id=change_id, request_id=record.get('request_id'), state='cancelling')


def install_routes(app, prefix, body):
    def api():
        value = getattr(app.state, 'lock_target_api', None)
        if value is None or value.runtime is not app.state.runtime:
            from ldcon.web.dlcpro_target_api import TargetApi
            value = app.state.lock_target_api = TargetApi(app.state.runtime)
        return value

    import asyncio

    async def guarded(function, *args):
        try:
            return await asyncio.to_thread(function, *args)
        except ApiError as error:
            return error

    @app.get(prefix + '/api/lasers/{laser_id}/target')
    async def get_target(laser_id: str):
        value = await guarded(api().get_target, laser_id)
        return value.response() if isinstance(value, ApiError) else value

    @app.post(prefix + '/api/lasers/{laser_id}/target/changes')
    async def post_change(laser_id: str, request: Request):
        try:
            data = await body(request)
        except ValueError as error:
            return ApiError(400, 'invalid_request', str(error)).response()
        value = await guarded(api().start_change, laser_id, data)
        if isinstance(value, ApiError):
            return value.response()
        status, payload = value
        return JSONResponse(payload, status_code=status)

    @app.get(prefix + '/api/lasers/{laser_id}/target/changes')
    async def find_change(laser_id: str, request_id: str = None):
        value = await guarded(api().change_by_request, laser_id, request_id)
        return value.response() if isinstance(value, ApiError) else value

    @app.get(prefix + '/api/lasers/{laser_id}/target/changes/{change_id}')
    async def get_change(laser_id: str, change_id: str):
        value = await guarded(api().change_state, laser_id, change_id)
        return value.response() if isinstance(value, ApiError) else value

    @app.post(prefix + '/api/lasers/{laser_id}/target/changes/{change_id}/cancel')
    async def cancel_change(laser_id: str, change_id: str):
        value = await guarded(api().cancel, laser_id, change_id)
        if isinstance(value, ApiError):
            return value.response()
        status, payload = value
        return JSONResponse(payload, status_code=status)
