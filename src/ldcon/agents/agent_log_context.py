"""Bounded, read-only log evidence for a laser's periodic agent review.

Snapshots describe recorded worker state, not a fresh instrument observation.
Directory descriptors and O_NOFOLLOW keep collection inside the selected log
tree; neither configuration snapshots nor agent conversations are included.
"""
from ldcon.core.state_layout import job_root


from datetime import datetime
import json
import math
import os
from pathlib import Path
import stat

from ldcon.agents.card_fallback import TERMINAL as FALLBACK_TERMINAL, identifier
from ldcon.agents.failure_policy import sanitize

MAX_JOBS = 12
MAX_CANDIDATES = 256
MAX_DIRECTORY_ENTRIES = 4096
MAX_FILE_BYTES = 65536
MAX_EVENT_BYTES = 16384
MAX_CONTEXT_CHARS = 16000
# An inconclusive measurement is finished even though it is not a notification
# failure. Match the dashboard lifecycle rather than retaining it as active.
TERMINAL = FALLBACK_TERMINAL | {'inconclusive'}
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
FILE_FLAGS = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC


def _timestamp(value):
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError('Log review timestamps require a timezone')
        value = parsed.timestamp()
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError('Log review timestamps must be finite Unix seconds')
    return float(value)


def _recorded_time(value):
    try:
        return _timestamp(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _open_directory(path):
    """Open an absolute directory without following any symlink component."""
    path = Path(os.path.abspath(path))
    descriptor = os.open(path.anchor, DIRECTORY_FLAGS)
    try:
        for part in path.parts[1:]:
            child = os.open(part, DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _read(descriptor, name, *, tail=False):
    """Return at most one bounded regular file; never block on special files."""
    try:
        file_descriptor = os.open(name, FILE_FLAGS, dir_fd=descriptor)
    except FileNotFoundError:
        return None, 'missing'
    except OSError:
        return None, 'unavailable'
    try:
        info = os.fstat(file_descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            return None, 'unsafe_file'
        limit = MAX_EVENT_BYTES if tail else MAX_FILE_BYTES
        if not tail and info.st_size > limit:
            return None, 'size_limit'
        offset = max(0, info.st_size - limit) if tail else 0
        if offset:
            os.lseek(file_descriptor, offset, os.SEEK_SET)
        data = os.read(file_descriptor, limit + (0 if tail else 1))
        if len(data) > limit:
            return None, 'size_limit'
        if tail and offset:
            # The first line may start in the middle of a JSON object.
            data = data.partition(b'\n')[2]
        return data, 'tail_only' if offset else None
    except OSError:
        return None, 'unavailable'
    finally:
        os.close(file_descriptor)


def _json(descriptor, name):
    data, issue = _read(descriptor, name)
    if data is None:
        return None, issue
    try:
        value = json.loads(data)
    except (UnicodeError, ValueError, RecursionError):
        return None, 'invalid_json'
    return value, issue


def _compact(value, limit):
    value = sanitize(value)
    encoded = json.dumps(value, ensure_ascii=False)
    if len(encoded) <= limit:
        return value
    return {'excerpt': encoded[:max(0, limit - 100)], 'truncated': True}


def _directories(descriptor, stats):
    values = []
    with os.scandir(descriptor) as entries:
        for number, entry in enumerate(entries):
            if number >= MAX_DIRECTORY_ENTRIES:
                stats['directory_scan_limited'] = True
                break
            try:
                identifier(entry.name)
                if entry.is_dir(follow_symlinks=False):
                    values.append((entry.stat(follow_symlinks=False).st_mtime, entry.name))
            except (ValueError, OSError):
                continue
    return sorted(values, reverse=True)


def _job(descriptor, path, laser_id, since, until, exclude_job_ids, stats):
    meta, issue = _json(descriptor, 'job.json')
    if not isinstance(meta, dict):
        if issue != 'missing':
            stats['unavailable_jobs'] += 1
        return None
    laser = meta.get('laser', {})
    identity = laser.get('id') if isinstance(laser, dict) else None
    identity = identity or meta.get('laser_id') or meta.get('set_id')
    if identity != laser_id or meta.get('id') != path.name or path.name in exclude_job_ids:
        return None
    cards = meta.get('cards', [])
    cards = cards if isinstance(cards, list) else []
    operations = [c.get('operation', c.get('card', c.get('catalog'))) for c in cards if isinstance(c, dict)]
    if any(isinstance(operation, str) and operation.startswith('agent.') for operation in operations):
        return None
    progress, progress_issue = _json(descriptor, 'progress.json')
    if not isinstance(progress, dict) and not progress_issue:
        progress_issue = 'invalid_shape'
    progress = progress if isinstance(progress, dict) else {}
    created = _recorded_time(meta.get('created_utc'))
    updated = _recorded_time(progress.get('updated_utc'))
    active = progress.get('status') not in TERMINAL
    newest = max((stamp for stamp in (created, updated) if stamp is not None), default=0)
    if not active and newest < since:
        return None
    if created is not None and created > until:
        return None
    result = dict(job_id=path.name, directory=str(path), deck_id=_compact(meta.get('deck_id'), 150),
                  created_utc=sanitize(meta.get('created_utc')), recorded_active=active,
                  cards=[{key: str(sanitize(card[key]))[:100] for key in ('id', 'operation', 'card', 'catalog') if key in card}
                         for card in cards[:16] if isinstance(card, dict)],
                  snapshots_are_latest=True, snapshots={}, paths={}, limitations=[])
    if progress_issue:
        result['limitations'].append('progress.json: ' + progress_issue)
    else:
        result['snapshots']['progress.json'] = _compact(progress, 1400)
        result['paths']['progress.json'] = str(path / 'progress.json')
    for name in ('readings.json', 'summary.json', 'failure-detail.json', 'device-review.json'):
        value, issue = _json(descriptor, name)
        if not isinstance(value, dict) and not issue:
            issue = 'invalid_shape'
        if issue:
            if issue != 'missing':
                result['limitations'].append(name + ': ' + issue)
            continue
        if name == 'summary.json' and isinstance(value, dict):
            value = {key: value[key] for key in ('status', 'message', 'reason', 'result', 'cards') if key in value}
        result['snapshots'][name] = _compact(value, 1400 if name == 'readings.json' else 1000)
        result['paths'][name] = str(path / name)
    events, issue = _read(descriptor, 'telemetry.events.jsonl', tail=True)
    result['events'] = []
    if issue and issue != 'missing':
        result['limitations'].append('telemetry.events.jsonl: ' + issue)
    if events is not None:
        result['paths']['telemetry.events.jsonl'] = str(path / 'telemetry.events.jsonl')
        selected = []
        invalid = 0
        for line in events.splitlines():
            try:
                event = json.loads(line)
                timestamp = _recorded_time(event.get('recorded_utc')) if isinstance(event, dict) else None
                if timestamp is None:
                    invalid += 1
                    continue
                if since <= timestamp <= until and event.get('kind') not in ('trace', 'quality_window', 'noise_window', 'controller_status'):
                    selected.append(_compact(event, 800))
            except (UnicodeError, ValueError, RecursionError):
                invalid += 1
        result['events'] = selected[-6:]
        if len(selected) > 6:
            result['limitations'].append('Only the last six diagnostic events are included')
        if invalid:
            result['limitations'].append('Malformed or untimed event lines omitted: ' + str(invalid))
    # Reduce a busy job before budgeting the whole request. The local paths
    # remain available for a deliberate follow-up read by the scoped agent.
    if len(json.dumps(result, ensure_ascii=False)) > 7000:
        result['events'] = result['events'][-2:]
        result['snapshots'] = {key: _compact(value, 700) for key, value in result['snapshots'].items()}
        result['limitations'].append('Job evidence abbreviated; read listed artifacts for details')
    return active, newest, result


def collect_log_context(root, laser_id, *, since, until, exclude_job_ids=()):
    """Collect one laser's latest snapshots and interval events without writes.

    ``since``/``until`` are Unix seconds or timezone-aware ISO timestamps. Old
    active jobs remain visible. Event and job limits are explicit: an empty
    excerpt never proves that no anomaly occurred. Both per-laser and legacy
    flat layouts are supported, with metadata identity checked in either case.
    """
    identifier(laser_id)
    since, until = _timestamp(since), _timestamp(until)
    if since > until:
        raise ValueError('Log review start must not follow its end')
    exclude_job_ids = frozenset(exclude_job_ids)
    root = Path(os.path.abspath(root))
    result = dict(schema_version=1, laser_id=laser_id,
                  window=dict(since_unix_s=since, until_unix_s=until),
                  paths={'job_logs': str(job_root(root, laser_id))}, jobs=[],
                  limitations=['Recorded snapshots are not fresh hardware observations',
                               'Bounded excerpts are not a complete log scan'],
                  scanned_jobs=0, omitted_jobs=0, unavailable_jobs=0,
                  directory_scan_limited=False)
    try:
        descriptor = _open_directory(root / 'jobs')
    except FileNotFoundError:
        result['limitations'].append('Job log directory does not exist')
        return result
    except OSError:
        result['limitations'].append('Job log directory is unavailable or contains a symlink')
        return result
    candidates = []
    try:
        # Only this laser's nested directory is traversed. Other top-level
        # directories are inspected for a legacy job.json, never descended.
        top = _directories(descriptor, result)
        if not any(name == laser_id for _, name in top):
            try:
                selected = os.stat(laser_id, dir_fd=descriptor, follow_symlinks=False)
                if stat.S_ISDIR(selected.st_mode):
                    top.append((selected.st_mtime, laser_id))
            except OSError:
                pass
        # A large legacy history must not push the selected laser's directory
        # outside the candidate budget.
        top.sort(key=lambda entry: (entry[1] == laser_id, entry[0]), reverse=True)
        for _, name in top[:MAX_CANDIDATES]:
            try:
                child = os.open(name, DIRECTORY_FLAGS, dir_fd=descriptor)
            except OSError:
                continue
            try:
                if name == laser_id:
                    nested = _directories(child, result)
                    for _, job_id in nested[:MAX_CANDIDATES]:
                        try:
                            job_descriptor = os.open(job_id, DIRECTORY_FLAGS, dir_fd=child)
                        except OSError:
                            continue
                        try:
                            result['scanned_jobs'] += 1
                            value = _job(job_descriptor, root / 'jobs' / name / job_id, laser_id,
                                         since, until, exclude_job_ids, result)
                            if value:
                                candidates.append(value)
                        finally:
                            os.close(job_descriptor)
                    result['omitted_jobs'] += max(0, len(nested) - MAX_CANDIDATES)
                else:
                    result['scanned_jobs'] += 1
                    value = _job(child, root / 'jobs' / name, laser_id, since, until, exclude_job_ids, result)
                    if value:
                        candidates.append(value)
            finally:
                os.close(child)
        result['omitted_jobs'] += max(0, len(top) - MAX_CANDIDATES)
    finally:
        os.close(descriptor)
    counts = {}
    for _, _, value in candidates:
        counts[value['job_id']] = counts.get(value['job_id'], 0) + 1
    ambiguous = {key for key, count in counts.items() if count > 1}
    if ambiguous:
        result['limitations'].append('Ambiguous duplicate job IDs omitted: ' + ', '.join(sorted(ambiguous)[:12]))
    candidates.sort(key=lambda candidate: (candidate[0], candidate[1], candidate[2]['job_id']), reverse=True)
    for _, _, value in candidates:
        if value['job_id'] in ambiguous or len(result['jobs']) >= MAX_JOBS:
            result['omitted_jobs'] += 1
            continue
        result['jobs'].append(value)
        if len(json.dumps(result, ensure_ascii=False)) > MAX_CONTEXT_CHARS - 200:
            result['jobs'].pop()
            result['omitted_jobs'] += 1
    if result['omitted_jobs'] or result['directory_scan_limited']:
        result['limitations'].append('Collection limits omitted jobs; inspect scoped log paths if needed')
    return result
