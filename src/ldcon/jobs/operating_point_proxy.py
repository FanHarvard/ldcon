"""Cancel orphaned operating-point waiters through their existing queue only.

No device command, process signal, lock restart, lease takeover or proposal replay
is available here. A dead parent cannot acknowledge a queued cancellation. After
independent clean-owner proof, publish that acknowledgement so even an old proxy
worker exits through its ordinary cancellation/cleanup path.
"""
from datetime import datetime
import fcntl
import json
from pathlib import Path
import re
import time

from ldcon.core.records import atomic_json, read_json

TERMINAL = frozenset(('succeeded', 'failed', 'cancelled', 'interrupted', 'completed', 'completed_with_failures'))
QUEUE = 'operating-point-requests'
RECEIPT = 'operating-point-stop-reconciliation.json'


def _id(value):
    if not isinstance(value, str) or re.fullmatch('[0-9a-f]{32}', value) is None:
        raise ValueError('Invalid proxy, parent or request identity')
    return value


def _json(path, required=True):
    path = Path(path)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError('Proxy evidence cannot use symlinks')
    if not path.exists() and not required:
        return None
    if path.stat().st_size > 2**20:
        raise ValueError('Proxy evidence exceeds its size limit')
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError('Proxy evidence must be a mapping')
    return value


def _operation(meta):
    cards = meta.get('cards', [])
    return cards[0].get('operation', cards[0].get('card')) if len(cards) == 1 else None


def _worker_identity(pid):
    """Unlike general process polling, denied/malformed evidence is not exit."""
    try:
        fields = Path('/proc/%d/stat' % pid).read_text().rsplit(')', 1)[1].split()
    except FileNotFoundError:
        return None
    except IndexError as error:
        raise ValueError('Parent process identity is malformed') from error
    if len(fields) < 20:
        raise ValueError('Parent process identity is incomplete')
    if fields[0] in ('Z', 'X'):
        return None
    return dict(pid=pid, boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                start_ticks=fields[19])


def parent_ended(directory, *, identity_fn=None):
    """True only for a terminal parent with a verified, no-longer-live PID identity."""
    identity_fn = identity_fn or _worker_identity
    directory = Path(directory)
    progress = _json(directory / 'progress.json')
    saved = _json(directory / 'worker-process.json')
    if (type(saved.get('pid')) is not int or saved['pid'] <= 0
            or not isinstance(saved.get('boot_id'), str) or not saved['boot_id']
            or not isinstance(saved.get('start_ticks'), str) or not saved['start_ticks']):
        raise ValueError('Parent worker identity is unverified')
    return progress.get('status') in TERMINAL and identity_fn(saved['pid']) != saved


def waiting_state(parent_directory, requested_s, *, now=time.time):
    parent = read_json(Path(parent_directory) / 'progress.json', {})
    return dict(state='waiting_for_parent', parent_status=parent.get('status', 'unknown'),
                parent_controller_state=parent.get('controller_state'),
                queue_wait_s=max(0., now() - requested_s),
                reason='Parent owns execution; recovery or another admitted operation may be in progress')


def reconcile_orphan_proxy(directory, parent_directory, current_laser, *, snapshot_fn=None,
                           identity_fn=None, sleep=time.sleep, now=time.time):
    """Supported maintenance/API entry point; returns a proof or a refusal.

    The child must already have an explicit cancel.request. The durable request
    must still be pending or have a preserved claimed record. Unknown legacy
    claims without the original request are deliberately not guessed.
    """
    from ldcon.agents.agent_owner_status import snapshot, check_stop, ECDL, NATIVE, CLD
    from ldcon.core.interface_schema import load_mapping
    snapshot_fn, identity_fn = snapshot_fn or snapshot, identity_fn or _worker_identity
    directory, parent_directory = Path(directory), Path(parent_directory)
    try:
        if directory.is_symlink() or parent_directory.is_symlink() or directory.parent != parent_directory.parent:
            raise ValueError('Parent and proxy must be real jobs in the same laser root')
        child_id, parent_id = _id(directory.name), _id(parent_directory.name)
        if not (directory / 'cancel.request').is_file():
            raise ValueError('An explicit proxy cancellation must be requested first')
        child, parent = _json(directory / 'job.json'), _json(parent_directory / 'job.json')
        progress = _json(directory / 'progress.json')
        if child.get('id') != child_id or parent.get('id') != parent_id or _operation(child) != 'tune.operating_point':
            raise ValueError('Only the identified operating-point proxy can be reconciled')
        laser_id = current_laser['id']
        if any((meta.get('laser') or {}).get('id') != laser_id for meta in (child, parent)):
            raise ValueError('Proxy and parent laser identity differs')
        if _operation(parent) not in ('servo.ecdl_steady_lock', 'servo.native_current'):
            raise ValueError('The identified parent is not a supported lock')
        link = progress.get('operating_point') or {}
        if link.get('lock_job') != parent_id:
            raise ValueError('Proxy progress does not identify this parent')
        rid = _id(link.get('request_id'))
        queue = parent_directory / QUEUE
        if queue.is_symlink():
            raise ValueError('Queue must not be a symlink')
        # Serialize independent Web/CLI reconciliation attempts. This is not a
        # worker/hardware lock and does not grant any physical authority.
        with (directory / 'operating-point-stop.lock').open('a+b') as guard:
            fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
            previous = _json(directory / RECEIPT, required=False)
            if previous is not None:
                if previous.get('request_id') != rid or previous.get('parent_job_id') != parent_id:
                    raise ValueError('Prior reconciliation identity differs')
                return previous if previous.get('status') == 'acknowledged' else dict(
                    status='requires_inspection', reason='Prior reconciliation intent is unfinished; no replay')
            if not parent_ended(parent_directory, identity_fn=identity_fn):
                raise ValueError('Parent is not terminal with confirmed worker exit')
            pending, claimed = queue / 'pending' / (rid + '.json'), queue / 'claimed' / (rid + '.json')
            candidates = [path for path in (pending, claimed) if path.exists()]
            if len(candidates) != 1:
                raise ValueError('One original pending/claimed request is required; unknown legacy outcomes need inspection')
            source = candidates[0]
            request = _json(source)
            invocation = _json(directory / 'invocation.json')
            if (request.get('request_id') != rid or request.get('job_id') != child_id
                    or request.get('card_id') != invocation.get('card_id')
                    or not isinstance(request.get('parameters'), dict)):
                raise ValueError('Queue request does not match this child invocation')
            from ldcon.jobs.operating_point import validate
            expected = validate(invocation['parameters'])
            def locked(moves):
                return [dict(move, mode='locked_reference') for move in moves]
            if expected['schedule'] is not None:
                expected = {**expected, 'schedule': [dict(step, moves=locked(step['moves'])) for step in expected['schedule']]}
            else:
                expected = {**expected, 'moves': locked(expected['moves'])}
            if request['parameters'] != expected:
                raise ValueError('Queued parameters differ from the frozen proxy invocation')
            status_path = queue / (rid + '.status.json')
            old_status = _json(status_path, required=False)
            if old_status and old_status.get('status') in TERMINAL | {'rejected'}:
                return dict(status='already_terminal', request_id=rid, queue_status=old_status)
            if source == pending and old_status is not None:
                raise ValueError('Pending request has an execution status; its outcome requires inspection')
            laser_path = parent_directory / 'laser.yaml'
            if laser_path.is_symlink() or laser_path.stat().st_size > 2**20:
                raise ValueError('Frozen parent laser definition is unavailable')
            frozen = load_mapping(laser_path.read_text())
            if frozen.get('id') != laser_id:
                raise ValueError('Frozen parent laser identity differs')
            devices = {key: value for key, value in frozen.get('devices', {}).items()
                       if value.get('type') in (ECDL, NATIVE, CLD)}
            current = current_laser.get('devices', {})
            if not devices or any(key not in current or value.get('type') != current[key].get('type')
                    or value.get('connection') != current[key].get('connection') for key, value in devices.items()):
                raise ValueError('Current and parent owner bindings differ')
            for device in devices.values():
                if device['type'] == NATIVE:
                    binding = device.get('control_settings', {}).get('native_current', {}).get('cld_interlock')
                    if not binding or not any(d['type'] == CLD and d.get('connection') == binding for d in devices.values()):
                        raise ValueError('All related native bias owners must be independently verified')
            parent_progress = _json(parent_directory / 'progress.json')
            terminal_s = datetime.fromisoformat(parent_progress['updated_utc']).timestamp()
            first = snapshot_fn(devices)
            if not first or any(row.get('fresh') is not True or row.get('fault') for row in first):
                raise ValueError('Fresh fault-free owner observations are required')
            sleep(1.1)
            second = snapshot_fn(devices)
            if len(first) != len(devices) or len(second) != len(devices):
                raise ValueError('Owner observation coverage is incomplete')
            expected_owners = [(key, value['type']) for key, value in devices.items()]
            if any([(row.get('device_id'), row.get('owner_type')) for row in rows] != expected_owners
                   for rows in (first, second)):
                raise ValueError('Owner observation identity or order differs')
            proof = check_stop(first, second, terminal_s)
            if proof['status'] != 'verified':
                raise ValueError('PI-off, fixed DAC and all owner lease releases were not independently verified')
            if not parent_ended(parent_directory, identity_fn=identity_fn) or _json(source) != request:
                raise ValueError('Parent or queue changed during inspection')
            if _json(status_path, required=False) != old_status:
                raise ValueError('Queue outcome changed during inspection')
            receipt = dict(schema='ldcon.operating-point-stop/1', status='prepared', job_id=child_id,
                parent_job_id=parent_id, request_id=rid, recorded_unix_s=now(),
                parent_terminal=parent_progress, parent_worker=_json(parent_directory / 'worker-process.json'),
                child_worker=_json(directory / 'worker-process.json', required=False), request=request,
                previous_queue_status=old_status, request_location=source.parent.name,
                physical_outcome='not_executed' if source == pending else 'unknown_prior_effects_current_state_verified_idle',
                owner_proof=proof, owner_observations=[first, second], hardware_writes=False,
                process_signal_sent=False, proposal_replayed=False)
            atomic_json(directory / RECEIPT, receipt)
            (queue / (rid + '.cancel')).touch()
            if source == pending:
                retired = queue / (rid + '.cancelled-request.json')
                if retired.exists():
                    raise ValueError('A retired request already exists; inspect without replay')
                source.replace(retired)
            atomic_json(status_path, dict(status='cancelled', unix_s=now(), request_id=rid,
                job_id=child_id, reason='Parent ended; cancellation acknowledged after independent idle-owner verification',
                physical_outcome=receipt['physical_outcome'], reconciliation_receipt=str(directory / RECEIPT)))
            receipt.update(status='acknowledged', acknowledged_unix_s=now(),
                note='Proxy may now exit normally; earlier parent failure and unknown effects are retained. No output was restored.')
            atomic_json(directory / RECEIPT, receipt)
            return receipt
    except (OSError, ValueError, KeyError, TypeError) as error:
        return dict(status='not_reconciled', reason=str(error), hardware_writes=False, process_signal_sent=False)


def reconcile_stopped_proxy(runtime, job_id):
    directory = runtime.directory(job_id)
    meta = read_json(directory / 'job.json', {})
    if _operation(meta) != 'tune.operating_point':
        return None
    progress = read_json(directory / 'progress.json', {})
    if progress.get('status') in TERMINAL:
        return read_json(directory / RECEIPT, None)
    link = progress.get('operating_point') or {}
    if not link.get('lock_job'):
        return None  # Open-loop cards retain ordinary cooperative cleanup.
    try:
        parent = runtime.directory(_id(link['lock_job']))
        current = runtime.interface_repository.read('lasers', meta['laser']['id'])['document']
        return reconcile_orphan_proxy(directory, parent, current)
    except (OSError, ValueError, KeyError, TypeError) as error:
        return dict(status='not_reconciled', reason=str(error), hardware_writes=False)
