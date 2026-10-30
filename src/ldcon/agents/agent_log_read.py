"""Bounded same-laser log reads for resident MCP; never contact an owner."""
from ldcon.core.state_layout import job_root

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import time

from ldcon.agents.agent_log_context import DIRECTORY_FLAGS, FILE_FLAGS, _json, _open_directory
from ldcon.agents.failure_policy import SECRET_KEYS, sanitize

KINDS = ('snapshot', 'telemetry', 'telemetry_summary')
FILES = ('progress.json', 'readings.json', 'summary.json', 'failure-detail.json', 'device-review.json', 'operation-record.json')
MAX_ROWS = 200
MAX_OUTPUT = 65536
MAX_WINDOW_S = 86400
QUERY_SECONDS = .25
MAX_PAYLOAD = 16384


def _safe(value, depth=0):
    if depth > 8:
        return '[details omitted]'
    if isinstance(value, dict):
        return {str(sanitize(str(k))): '[redacted]' if SECRET_KEYS.search(str(k)) else _safe(v, depth + 1)
                for k, v in list(value.items())[:100]}
    if isinstance(value, list):
        return [_safe(v, depth + 1) for v in value[:200]]
    return sanitize(value)


@contextmanager
def _job(root, laser_id, job_id):
    # job_id never names a path. Both the root scope and job identity are checked.
    if not isinstance(laser_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', laser_id):
        raise ValueError('Invalid log laser ID')
    if not isinstance(job_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', job_id):
        raise ValueError('Invalid log job ID')
    descriptor = _open_directory(job_root(root, laser_id) / job_id)
    try:
        meta, issue = _json(descriptor, 'job.json')
        if issue or not isinstance(meta, dict) or meta.get('id') != job_id:
            raise ValueError('Job log identity unavailable')
        embedded = meta.get('laser', {})
        identities = [x for x in (meta.get('laser_id'), embedded.get('id') if isinstance(embedded, dict) else None) if x is not None]
        if not identities:
            identities = [meta.get('set_id')]
        if any(identity != laser_id for identity in identities):
            raise ValueError('Job logs belong to a different laser')
        yield descriptor
    finally:
        os.close(descriptor)


def _regular_fd(directory, name, optional=False):
    try:
        fd = os.open(name, FILE_FLAGS, dir_fd=directory)
    except FileNotFoundError:
        if optional:
            return None
        raise
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 2 * 1024 ** 3:
        os.close(fd)
        raise ValueError('Unsafe telemetry file')
    return fd


def _identity(fd):
    info = os.fstat(fd)
    return info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)


def _open_files():
    result = {}
    # MCP is single-threaded. Inspect actual opened DB/WAL descriptors, rather
    # than trusting a path check before sqlite's own open/canonicalization.
    for name in os.listdir('/proc/self/fd'):
        try:
            result[int(name)] = _identity(int(name))
        except (OSError, ValueError):
            pass
    return result


def _verify_sqlite_fds(before, allowed, database_identity):
    opened = [identity for fd, identity in _open_files().items()
              if identity != before.get(fd) and identity[2] == stat.S_IFREG]
    if database_identity not in opened or any(identity not in allowed for identity in opened):
        raise ValueError('SQLite file identity changed or is outside this job')


@contextmanager
def _database(directory):
    guards = []
    connection = None
    try:
        for name in ('telemetry.sqlite3', 'telemetry.sqlite3-journal', 'telemetry.sqlite3-wal', 'telemetry.sqlite3-shm'):
            fd = _regular_fd(directory, name, optional=bool(guards))
            if fd is not None:
                guards.append(fd)
        identities = {_identity(fd) for fd in guards}
        database_identity = _identity(guards[0])
        before = _open_files()
        connection = sqlite3.connect(f'file:/proc/self/fd/{directory}/telemetry.sqlite3?mode=ro', uri=True, timeout=.02)
        _verify_sqlite_fds(before, identities, database_identity)
        connection.execute('PRAGMA query_only=ON')
        connection.execute('PRAGMA trusted_schema=OFF')
        connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 1024 * 1024)
        deadline = time.monotonic() + QUERY_SECONDS
        connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        table = connection.execute("SELECT type,sql FROM sqlite_master WHERE name='telemetry'").fetchone()
        if not table or table[0] != 'table' or not re.match(r'CREATE\s+TABLE\b', table[1] or '', re.I):
            raise ValueError('Telemetry must be an ordinary table')
        columns = {'id', 'run_id', 'recorded_utc', 'simulated_s', 'kind', 'payload_json'}
        def authorize(action, arg1, arg2, database, source):
            if action == sqlite3.SQLITE_SELECT:
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_READ and database == 'main' and arg1 == 'telemetry' and arg2 in columns | {''}:
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_FUNCTION and arg2 in {'julianday', 'count', 'min', 'max', 'length'}:
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY
        connection.set_authorizer(authorize)
        yield connection
        _verify_sqlite_fds(before, identities, database_identity)
    finally:
        if connection is not None:
            connection.close()
        for fd in reversed(guards):
            os.close(fd)


def _window(since, until):
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in (since, until)):
        raise ValueError('Telemetry requires finite since_unix_s and until_unix_s')
    if not 0 <= since <= until or until - since > MAX_WINDOW_S:
        raise ValueError('Telemetry windows must be at most 24 hours')
    return (datetime.fromtimestamp(since, timezone.utc).isoformat(),
            datetime.fromtimestamp(until, timezone.utc).isoformat())


def read(root, laser_id, *, job_id, kind='snapshot', filename=None, since_unix_s=None,
         until_unix_s=None, limit=100, after_id=0, event_kind=None):
    if kind not in KINDS or type(limit) is not int or not 1 <= limit <= MAX_ROWS:
        raise ValueError('Invalid log kind or row limit')
    if type(after_id) is not int or not 0 <= after_id < 2 ** 63:
        raise ValueError('Invalid telemetry cursor')
    if event_kind is not None and (not isinstance(event_kind, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,96}', event_kind)):
        raise ValueError('Invalid telemetry event kind')
    result = dict(laser_id=laser_id, job_id=job_id, kind=kind, read_at_unix_s=time.time(),
                  recorded_evidence_only=True, fresh_owner_observation=False,
                  values_redacted_and_bounded=True)
    with _job(root, laser_id, job_id) as directory:
        if kind == 'snapshot':
            if filename not in FILES or since_unix_s is not None or until_unix_s is not None or after_id or event_kind is not None:
                raise ValueError('Snapshot requires an allowed filename and no telemetry filter')
            value, issue = _json(directory, filename)
            if issue:
                return dict(result, status='unavailable', filename=filename, reason=issue)
            value = _safe(value)
            raw = json.dumps(value, ensure_ascii=False)
            truncated = len(raw.encode()) > MAX_OUTPUT - 2048
            if truncated:
                value = {'excerpt': raw.encode()[:MAX_OUTPUT // 4].decode('utf-8', errors='ignore'), 'truncated': True}
            return dict(result, status='ok', filename=filename, latest_snapshot=True,
                        coverage='latest_recorded_snapshot', data=value, truncated=truncated)
        if filename is not None or (kind == 'telemetry_summary' and after_id):
            raise ValueError('Invalid telemetry filename or summary cursor')
        since, until = _window(since_unix_s, until_unix_s)
        result.update(window=dict(since_unix_s=since_unix_s, until_unix_s=until_unix_s), event_kind=event_kind)
        where = 'julianday(recorded_utc)>=julianday(?) AND julianday(recorded_utc)<=julianday(?)'
        params = [since, until]
        if event_kind is not None:
            where += ' AND kind=?'
            params.append(event_kind)
        try:
            with _database(directory) as database:
                if kind == 'telemetry_summary':
                    rows = database.execute('SELECT kind,count(*),min(recorded_utc),max(recorded_utc) FROM telemetry WHERE ' + where + ' GROUP BY kind ORDER BY kind LIMIT 129', params).fetchall()
                else:
                    rows = database.execute('SELECT id,run_id,recorded_utc,simulated_s,kind,CASE WHEN length(payload_json)<=? THEN payload_json ELSE NULL END FROM telemetry WHERE ' + where + ' AND id>? ORDER BY id LIMIT ?',
                                            [MAX_PAYLOAD, *params, after_id, limit + 1]).fetchall()
        except sqlite3.Error:
            return dict(result, status='unavailable', coverage='unconfirmed',
                        reason='telemetry_unavailable_or_query_budget', query_budget_s=QUERY_SECONDS)
        if kind == 'telemetry_summary':
            complete = len(rows) <= 128
            return dict(result, status='ok', coverage='all_recorded_rows_in_window' if complete else 'kind_count_limit',
                        complete=complete, total_rows=sum(row[1] for row in rows) if complete else None,
                        kind_counts=[dict(event_kind=str(_safe(k))[:96], rows=n,
                                          first_recorded_utc=str(_safe(a))[:64], last_recorded_utc=str(_safe(b))[:64])
                                     for k, n, a, b in rows[:128]])
        records = []
        used = len(json.dumps(result).encode()) + 2048
        for identifier, run_id, recorded, elapsed, event, raw in rows[:limit]:
            issue = None
            try:
                payload = _safe(json.loads(raw)) if raw is not None else None
                if raw is None:
                    issue = 'payload_size_limit'
            except (ValueError, UnicodeError, RecursionError):
                payload, issue = None, 'invalid_payload_json'
            record = dict(id=identifier, run_id=_safe(run_id), recorded_utc=_safe(recorded),
                          elapsed_s=_safe(elapsed), event_kind=_safe(event), payload=payload)
            if issue:
                record['limitation'] = issue
            size = len(json.dumps(record, ensure_ascii=False).encode())
            if used + size > MAX_OUTPUT:
                break
            used += size
            records.append(record)
        more = len(rows) > len(records)
        return dict(result, status='ok', coverage='page_of_recorded_rows' if more else 'remaining_recorded_rows_in_window',
                    records=records, returned_rows=len(records), has_more=more,
                    after_id=after_id, next_after_id=records[-1]['id'] if records else after_id,
                    row_limit=limit, byte_limit=MAX_OUTPUT)


def audit(resident_directory, result):
    """Append metadata only; never save returned log bodies or credentials."""
    record = {key: result[key] for key in (
        'laser_id', 'job_id', 'kind', 'filename', 'window', 'event_kind', 'status',
        'coverage', 'returned_rows', 'total_rows', 'has_more', 'after_id', 'next_after_id',
        'complete', 'truncated', 'read_at_unix_s') if key in result}
    record.update(tool='laser_log_read', recorded_utc=datetime.now(timezone.utc).isoformat())
    raw = (json.dumps(record, ensure_ascii=False, allow_nan=False) + '\n').encode()
    directory = _open_directory(resident_directory)
    fd = None
    try:
        fd = os.open('agent-log-read-events.jsonl', os.O_WRONLY | os.O_APPEND | os.O_CREAT |
                     os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, 0o660, dir_fd=directory)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError('Unsafe resident log-read audit')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if os.write(fd, raw) != len(raw):
            raise OSError('Incomplete resident log-read audit')
        os.fsync(fd)
    finally:
        if fd is not None:
            os.close(fd)
        os.close(directory)
