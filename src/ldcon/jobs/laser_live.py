"""Worker-side live adoption of a saved laser definition at a safe boundary.

The dashboard saves ``lasers/<laser>.yaml`` and, for every running job of that
laser, writes ``laser-reload-request-<id>.json``. The worker consumes it when
its card reaches a safe boundary (``CardRuntime.poll`` or the native card's
cycle), checks the revision chain and the on-disk file digest, then applies
each changed path according to its live class (``agent_authority.live_class``):

``owner_hot``   forwarded under the worker's lease to the ECDL owner
                (``lock.reload_settings``), which adopts control settings at
                its own boundary; deferred while the controller is busy.
``native_hot``  the native owner was updated by the dashboard
                (``policy_update``); the worker refreshes its policy copy from
                ``describe()`` once the owner reports the new digest.
``snapshot``    the running card keeps the value it read at start (recorded,
                takes effect at the next start).

Results are durable (``laser-reload-result-<id>.json``, ``laser-live.json``,
``laser-reload-history.jsonl``) and emitted as telemetry events. Nothing here
writes hardware; owners keep their own safety checks.
"""
from ldcon.core.state_layout import config_file

import hashlib
import copy
import json
from pathlib import Path
import time
import uuid

from ldcon.core.records import atomic_json, read_json
from ldcon.core.interface_schema import load_mapping, validate_laser, digest

MAX_REQUEST_BYTES = 512 * 1024


def laser_file(directory, laser_id):
    directory = Path(directory)
    root = directory.parent.parent if directory.parent.name == 'jobs' else directory.parent.parent.parent
    return config_file(root, 'lasers', laser_id)


def text_revision(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _copy_path(target, source, path):
    """Copy one acknowledged path; preserve snapshot and deferred values."""
    parts = path.split('.')
    destination = target
    candidate = source
    for key in parts[:-1]:
        destination = destination[int(key)] if isinstance(destination, list) else destination.setdefault(key, {})
        candidate = candidate[int(key)] if isinstance(candidate, list) else candidate[key]
    key = parts[-1]
    if isinstance(destination, list):
        destination[int(key)] = copy.deepcopy(candidate[int(key)])
    elif key in candidate:
        destination[key] = copy.deepcopy(candidate[key])
    else:
        destination.pop(key, None)


class LaserLive:
    def __init__(self, directory, laser, card, emit, *, lock=None, port=None, clock=time.time):
        self.directory = Path(directory)
        self.laser = laser
        self.card = card
        self.emit = emit or (lambda kind, value: None)
        self.lock = lock
        self.port = port
        self.clock = clock
        meta = read_json(self.directory / 'job.json', {})
        self.revision = meta.get('laser_revision')
        self.applied = []
        self.snapshot = []
        self.publish(status='running')

    # ---- bookkeeping ---------------------------------------------------------
    def publish(self, **extra):
        value = dict(supported=True, laser_id=self.laser.get('id'), card_id=self.card['id'],
                     operation=self.card['operation'], revision=self.revision,
                     effective_revision=digest(self.laser),
                     applied=self.applied[-50:], snapshot=self.snapshot[-50:],
                     pending=[p.name for p in sorted(self.directory.glob('laser-reload-request-*.json'))],
                     updated_unix_s=self.clock(), **extra)
        atomic_json(self.directory / 'laser-live.json', value)
        return value

    def _finish(self, request, status, reason, **details):
        value = dict(request_id=request['request_id'], status=status, reason=reason,
                     revision_before=request.get('revision_before'), revision_after=request.get('revision_after'),
                     revision=self.revision, updated_unix_s=self.clock(), **details)
        atomic_json(self.directory / ('laser-reload-result-' + request['request_id'] + '.json'), value)
        with (self.directory / 'laser-reload-history.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + '\n')
        self.emit('laser_reload', value)
        self.publish(status='running', last_result=value)
        return value

    def _device_for(self, interface):
        binding = self.laser.get('interfaces', {}).get(interface)
        return binding.get('device') if isinstance(binding, dict) else None

    # ---- the safe-boundary hook ------------------------------------------------
    def poll(self, boundary, busy=False):
        from ldcon.jobs.target_response_control import poll_policy
        poll_policy(self,busy)
        paths = sorted(self.directory.glob('laser-reload-request-*.json'))
        if not paths:
            return None
        path = paths[0]
        try:
            if path.stat().st_size > MAX_REQUEST_BYTES:
                raise ValueError('Reload request exceeds its size limit')
            request = json.loads(path.read_text(encoding='utf-8'))
            identifier = path.stem.removeprefix('laser-reload-request-')
            if not isinstance(request, dict) or request.get('request_id') != identifier or request.get('job_id') != self.directory.name:
                raise ValueError('Reload request identity mismatch')
        except (OSError, ValueError) as error:
            path.unlink(missing_ok=True)
            return self._finish(dict(request_id=path.stem.removeprefix('laser-reload-request-')), 'rejected', str(error))
        expires = request.get('expires_unix_s')
        if type(expires) not in (int, float) or self.clock() > expires:
            path.unlink(missing_ok=True)
            return self._finish(request, 'rejected', 'Reload request expired before a safe boundary')
        if request.get('revision_before') != self.revision:
            path.unlink(missing_ok=True)
            return self._finish(request, 'rejected', 'Stale reload request: worker laser revision is ' + str(self.revision))
        try:
            on_disk = laser_file(self.directory, self.laser['id']).read_text(encoding='utf-8')
            if text_revision(on_disk) != request.get('revision_after'):
                raise ValueError('Saved laser definition on disk differs from the reload request')
            document = load_mapping(on_disk)
            validate_laser(document)
            if document.get('id') != self.laser['id']:
                raise ValueError('Reload request names another laser')
        except (OSError, ValueError) as error:
            path.unlink(missing_ok=True)
            return self._finish(request, 'rejected', str(error))
        from ldcon.core.agent_authority import describe_changes, live_class, FORBIDDEN
        rows = describe_changes('lasers', self.laser, document)
        for row in rows:
            row['live'] = live_class('lasers', tuple(row['path'].split('.')))
        if any(row['class'] == FORBIDDEN or row['live'] == 'idle_only' for row in rows):
            path.unlink(missing_ok=True)
            return self._finish(request, 'rejected', 'Hard limits or device identity changed; a running card cannot adopt that')
        lock_device = self._device_for('lock')
        native_device = self._device_for('native_current')
        owner_rows = [r for r in rows if r['live'] == 'owner_hot' and lock_device and r['path'].startswith('devices.' + lock_device + '.')]
        native_rows = [r for r in rows if r['live'] == 'native_hot' and native_device and r['path'].startswith('devices.' + native_device + '.')]
        hot_elsewhere = [r for r in rows if r['live'] in ('owner_hot', 'native_hot') and r not in owner_rows and r not in native_rows]
        applied, deferred, snapshot, details = [], [], [], {}
        if owner_rows:
            if self.lock is None or not hasattr(self.lock, 'reload_settings'):
                snapshot += [r['path'] for r in owner_rows]
                details['owner'] = 'this card holds no settings-reload capable lock; values apply at the next start'
            elif busy:
                return None  # stay queued until a non-busy boundary or expiry
            else:
                prefix = 'devices.' + lock_device + '.'
                relative = [r['path'][len(prefix):] for r in owner_rows if r['path'].startswith(prefix)]
                try:
                    result = self.lock.reload_settings(document['devices'][lock_device], relative,
                        decision_id=request.get('authority', {}).get('decision_id'))
                except Exception as error:
                    path.unlink(missing_ok=True)
                    return self._finish(request, 'rejected', 'Owner refused the settings reload: ' + str(error))
                details['owner'] = result
                if result.get('path_base') != 'device':
                    prefix += 'control_settings.'
                applied += [prefix + p for p in result.get('applied', [])]
                deferred += [prefix + p for p in result.get('deferred', [])]
                snapshot += [prefix + p for p in result.get('rejected', {})]
                snapshot += [prefix + p for p in result.get('snapshot', [])]
        if native_rows:
            if self.port is None or not hasattr(self.port, 'refresh_policy'):
                snapshot += [r['path'] for r in native_rows]
                details['native'] = 'no native port on this card; values apply at the next start'
            else:
                expected = document['devices'][native_device]['control_settings']['native_current']
                try:
                    adopted = self.port.refresh_policy(expected)
                except Exception as error:
                    adopted = dict(status='unavailable', reason=str(error))
                details['native'] = adopted
                if adopted.get('status') == 'refreshed':
                    applied += [r['path'] for r in native_rows]
                elif adopted.get('status') == 'pending':
                    if self.clock() < expires:
                        return None  # owner has not adopted the new digest yet; keep waiting
                    path.unlink(missing_ok=True)
                    return self._finish(request, 'rejected', 'Native owner never reported the saved policy digest: ' + str(adopted.get('reason')))
                else:
                    path.unlink(missing_ok=True)
                    return self._finish(request, 'rejected', 'Native policy refresh failed: ' + str(adopted.get('reason')))
        target_rows = [r for r in rows if r['live'] == 'target_hot']
        if target_rows:
            # The laser's ONE target changed (tune.lock_target or a maintainer save): the running lock ramps its owner
            # to it, bounded by the owner's target slew; the card's copies follow the owner's reported ramp.
            setter = getattr(self.lock, 'set_target', None) or getattr(self.port, 'set_target', None)
            new_hz = (document.get('target') or {}).get('frequency_hz')
            if setter is None or not getattr(self, 'follow_laser_target', False):
                snapshot += [r['path'] for r in target_rows]
                details['target'] = ('this card pins its own target_frequency_hz' if setter is not None else
                                     'this card has no live target path') + '; the saved target applies at the next start'
            elif busy:
                return None
            else:
                try:
                    from ldcon.jobs.target_response_control import bound_token
                    token=bound_token(self,request)
                    details['target'] = setter(float(new_hz),**({'target_ff_study_token':token} if token else {}))
                except Exception as error:
                    path.unlink(missing_ok=True)
                    return self._finish(request, 'rejected', 'Owner refused the target change: ' + str(error))
                applied += [r['path'] for r in target_rows]
        snapshot += [r['path'] for r in rows if r['live'] == 'snapshot'] + [r['path'] for r in hot_elsewhere]
        for applied_path in applied:
            _copy_path(self.laser, document, applied_path)
        self.applied += applied
        if deferred:
            # Keep the request AND the old desired revision until every live
            # path is acknowledged. A quiet boundary automatically retries it.
            # Successfully applied paths remain visible without pretending the
            # rest of the saved document is effective in this running worker.
            details['deferred_note'] = 'Pending automatic adoption at the next eligible safe boundary'
            changed = request.get('last_deferred') != deferred or bool(applied)
            request['last_deferred'] = deferred
            atomic_json(path, request)
            if changed:
                return self._finish(request, 'deferred', 'Owner is busy; request retained',
                    applied=applied, deferred=deferred, snapshot=snapshot, details=details, boundary=boundary)
            return None
        self.revision = request['revision_after']
        self.snapshot += snapshot
        path.unlink(missing_ok=True)
        status = 'applied' if applied else 'snapshot'
        return self._finish(request, status, 'Adopted acknowledged live paths at boundary ' + str(boundary), applied=applied,
                            deferred=[], snapshot=snapshot, details=details, boundary=boundary)


def enqueue_reload(directory, *, laser_id, revision_before, revision_after, rows, decision, ttl_s=900., clock=time.time):
    """Dashboard entry: queue one request; a worker without live support is reported, not queued."""
    directory = Path(directory)
    live = read_json(directory / 'laser-live.json', {})
    if not live.get('supported'):
        return dict(status='unsupported_worker', reason='worker predates live laser reload; values apply at the next start')
    if any(directory.glob('laser-reload-request-*.json')):
        return dict(status='rejected', reason='a previous reload request is still pending at the worker; it adopts at its next safe boundary, then save again')
    if live.get('revision') != revision_before:
        return dict(status='rejected', reason='worker laser revision differs from the saved revision chain; save again from the current definition',
                    worker_revision=live.get('revision'))
    now = clock()
    request = dict(request_id=uuid.uuid4().hex, job_id=directory.name, laser_id=laser_id,
                   revision_before=revision_before, revision_after=revision_after,
                   changes=[dict(path=r['path'], old=r.get('old'), new=r.get('new'), **{'class': r['class']}, live=r.get('live'))
                            for r in rows],
                   authority=decision, created_unix_s=now, expires_unix_s=now + ttl_s, status='queued')
    atomic_json(directory / ('laser-reload-request-' + request['request_id'] + '.json'), request)
    return request


def state(directory):
    directory = Path(directory)
    live = read_json(directory / 'laser-live.json', {})
    results = [read_json(p, {}) for p in sorted(directory.glob('laser-reload-result-*.json'))]
    pending = [read_json(p, {}) for p in sorted(directory.glob('laser-reload-request-*.json'))]
    return dict(live=live or dict(supported=False), pending=pending,
                last_result=max(results, key=lambda r: r.get('updated_unix_s', 0), default=None))
