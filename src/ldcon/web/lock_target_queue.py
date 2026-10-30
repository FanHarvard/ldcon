"""Accept bounded target intents in every operational state.

Admission and physical application are separate. Explicit pre-admission busy
responses become durable queued intents. GET never dispatches. Only the existing
target card writes configuration or invokes the owner's bounded ramp. A crash
during dispatch is unresolved, never an invitation to retry a physical action.
"""
from ldcon.core.target_policy import target_bounds
from contextlib import contextmanager
import fcntl
import math
from pathlib import Path
import uuid

from ldcon.web.lock_target_api import (ApiError, LockTargetApi, REQUEST_ID,
                              DEFAULT_TIMEOUT_S, MAX_TIMEOUT_S)


class QueuedLockTargetApi(LockTargetApi):
    def _root(self, laser):
        from ldcon.core.records import safe_id
        root = self.runtime.root / 'lock-target-queue' / safe_id(laser)
        root.mkdir(parents=True, exist_ok=True)
        return root

    @contextmanager
    def _guard(self, laser):
        with self.runtime.mutex:
            if getattr(self, '_guarded_laser', None) == laser:
                yield
                return
            with (self._root(laser) / 'dispatch.lock').open('a+b') as stream:
                fcntl.flock(stream, fcntl.LOCK_EX)
                previous = getattr(self, '_guarded_laser', None)
                self._guarded_laser = laser
                try:
                    yield
                finally:
                    self._guarded_laser = previous

    def _entries(self, laser):
        from ldcon.core.records import read_json
        values = [read_json(p, {}) for p in self._root(laser).glob('*.json')]
        if any(not isinstance(v, dict) or not v.get('change_id') for v in values):
            raise ValueError('Invalid target queue record; inspect before dispatch')
        return sorted(values, key=lambda r: (r['created_unix_s'], r['change_id']))

    def _save(self, laser, value):
        from ldcon.core.result_reservations import durable_json
        value['updated_unix_s'] = self.clock()
        durable_json(self._root(laser) / (value['change_id'] + '.json'), value)

    def _intent(self, laser, change_id):
        return next((v for v in self._entries(laser) if v['change_id'] == change_id), None)

    def _validate_intent(self, laser_id, body):
        if not isinstance(body, dict) or set(body) - {'target_frequency_hz', 'request_id', 'timeout_s'}:
            raise ApiError(400, 'invalid_request', 'Expected target_frequency_hz, request_id and optional timeout_s')
        target = body.get('target_frequency_hz')
        timeout = body.get('timeout_s', DEFAULT_TIMEOUT_S)
        timeout = DEFAULT_TIMEOUT_S if timeout is None else timeout
        rid = body.get('request_id')
        if type(target) not in (int, float) or not math.isfinite(target) or target <= 0:
            raise ApiError(400, 'invalid_request', 'target_frequency_hz must be positive and finite')
        if not isinstance(rid, str) or not REQUEST_ID.fullmatch(rid):
            raise ApiError(400, 'invalid_request', 'Invalid request_id')
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= MAX_TIMEOUT_S:
            raise ApiError(400, 'invalid_request', 'Invalid timeout_s')
        from ldcon.jobs.lock_target import laser_target
        try:
            laser = self._laser(laser_id)
            block = laser_target(laser)
        except ValueError as error:
            raise ApiError(409, 'not_configured', str(error)) from None
        if not block or target_bounds(laser) is None:
            raise ApiError(409, 'not_configured', 'Target bounds and maximum change must be configured')
        bounds = target_bounds(laser)
        if not bounds[0] <= target <= bounds[1]:
            raise ApiError(422, 'out_of_bounds', 'New target is outside the admitted target range', bounds_hz=bounds)
        lock, _, _, _ = self._lock(laser, self._jobs(laser_id))
        if lock is not None and block.get('max_change_hz') is None:
            raise ApiError(409, 'not_configured', 'Locked target changes require max_change_hz')
        if lock is not None and abs(target - block['frequency_hz']) > block['max_change_hz'] * (1 + 1e-12):
            raise ApiError(422, 'step_too_large', 'Split the change into bounded steps',
                           max_change_hz=block['max_change_hz'], frequency_hz=block['frequency_hz'])
        return dict(target_frequency_hz=float(target), request_id=rid, timeout_s=float(timeout))

    def _payload(self, laser, entry):
        # A durable dispatch may have returned just before a process died.
        # Recover its receipt by the SAME id; do not start it a second time.
        if self._record(laser, entry['change_id']) is not None:
            return super().change_state(laser, entry['change_id'])
        phase = entry['phase']
        state = 'unknown' if phase in ('dispatching', 'dispatched', 'unknown') else phase
        return dict(laser=laser, request_id=entry['body']['request_id'], change_id=entry['change_id'],
                    target_frequency_hz=entry['body']['target_frequency_hz'], state=state,
                    final=state in ('cancelled', 'failed'), applied_to_lock=False,
                    message=entry.get('message'), waiting_reason=entry.get('waiting_reason'),
                    updated_unix_s=entry['updated_unix_s'])

    def _waiting(self, laser):
        return [v for v in self._entries(laser) if not self._payload(laser, v)['final']]

    def start_change(self, laser_id, body):
        with self._guard(laser_id):
            # Request identity is checked before bounds against a possibly newer target.
            rid = body.get('request_id') if isinstance(body, dict) else None
            previous = next((v for v in self._entries(laser_id) if v['body']['request_id'] == rid), None)
            if previous:
                if body.get('target_frequency_hz') != previous['body']['target_frequency_hz']:
                    raise ApiError(409, 'request_conflict', 'request_id already identifies a different target')
                return 200, self._payload(laser_id, previous)
            if isinstance(rid, str) and rid in self._requests(laser_id):
                return super().start_change(laser_id, body)
            body = self._validate_intent(laser_id, body)
            waiting = self._waiting(laser_id)
            reason = 'earlier_target_request'
            if not waiting:
                try:
                    return super().start_change(laser_id, body)
                except ApiError as error:
                    if error.code != 'busy':
                        raise
                    reason = 'controller_busy'
            if len(waiting) >= 64:
                raise ApiError(429, 'queue_full', 'Target queue is full; inspect or cancel pending requests')
            admitted_laser = self._laser(laser_id)
            entry = dict(change_id=uuid.uuid4().hex, body=body, phase='queued',
                         admitted_bounds_hz=target_bounds(admitted_laser),
                         bounds_reference_hz=admitted_laser['target']['frequency_hz'],
                         created_unix_s=self.clock(), expires_unix_s=self.clock() + body['timeout_s'],
                         waiting_reason=reason, message='Target accepted; waiting for the current operation')
            self._save(laser_id, entry)
            return 202, self._payload(laser_id, entry)

    def change_state(self, laser_id, change_id):
        with self._guard(laser_id):
            self._laser(laser_id)
            entry = self._intent(laser_id, change_id)
            return self._payload(laser_id, entry) if entry else super().change_state(laser_id, change_id)

    def change_by_request(self, laser_id, request_id):
        with self._guard(laser_id):
            for entry in self._entries(laser_id):
                if entry['body']['request_id'] == request_id:
                    return self._payload(laser_id, entry)
        return super().change_by_request(laser_id, request_id)

    def get_target(self, laser_id):
        value = super().get_target(laser_id)
        with self._guard(laser_id):
            waiting = self._waiting(laser_id)
            value['queued_changes'] = [self._payload(laser_id, e) for e in waiting]
            value['queue_supported'] = True
            if value['change'] is None and waiting:
                p = self._payload(laser_id, waiting[0])
                value['change'] = {k: p[k] for k in ('change_id', 'request_id', 'state')}
        return value

    def cancel(self, laser_id, change_id):
        with self._guard(laser_id):
            entry = self._intent(laser_id, change_id)
            if entry is None or self._record(laser_id, change_id) is not None:
                # Base cancel delegates to change_state; release the file lock first.
                pass
            else:
                if entry['phase'] == 'queued':
                    entry.update(phase='cancelled', message='Cancelled before dispatch; no target was changed')
                    self._save(laser_id, entry)
                return 200, self._payload(laser_id, entry)
        return super().cancel(laser_id, change_id)

    def tick(self):
        root = self.runtime.root / 'lock-target-queue'
        if not root.exists():
            return
        for folder in sorted(root.iterdir()):
            if folder.is_dir() and not folder.is_symlink():
                self._tick_laser(folder.name)

    def _tick_laser(self, laser):
        with self._guard(laser):
            waiting = self._waiting(laser)
            if not waiting:
                return
            entry = waiting[0]
            if entry['phase'] != 'queued':
                return  # Unknown dispatch blocks later intents; never replay.
            if self.clock() >= entry['expires_unix_s']:
                entry.update(phase='failed', message='Target request expired before dispatch; no target was changed')
                self._save(laser, entry)
                return
            bounds = entry.get('admitted_bounds_hz')
            if bounds is not None and not bounds[0] <= entry['body']['target_frequency_hz'] <= bounds[1]:
                entry.update(phase='failed', message='Target is outside its original admission range; no target was changed')
                self._save(laser, entry)
                return
            entry['phase'] = 'dispatching'
            self._save(laser, entry)  # Durable claim BEFORE the ordinary card start.
            try:
                super().start_change(laser, entry['body'], change_id=entry['change_id'])
            except ApiError as error:
                if error.code == 'busy':
                    entry.update(phase='queued', waiting_reason='controller_busy',
                                 message='Target accepted; waiting for the current operation')
                else:
                    entry.update(phase='failed', message='Target was not dispatched: ' + error.code)
            except Exception:
                entry.update(phase='unknown', message='Dispatch outcome is unknown; inspect this request before continuing')
            else:
                entry['phase'] = 'dispatched'
            self._save(laser, entry)
