"""Pure scalar guards and a durable queue for same-worker midpoint requests.

This module never connects to an owner or an instrument. Queue admission is not
permission to write: the existing worker must recheck its controller and guard
revisions, claim the request, then execute it through its current owner lease.
A persisted running/unknown claim is never replayed after process replacement.
All acquisition timestamps accepted by ScalarGuard are Unix seconds.
"""
from contextlib import contextmanager
import copy
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
import uuid

from ldcon.jobs.window_center import parameters as window_parameters


GUARD_DEFAULTS = dict(enabled=False, agent_enabled=False, conditions=[],
                      combine='any', confirm_samples=3,
                      measurement_max_age_s=5., dither_max_age_s=30.,
                      cooldown_s=300., max_per_hour=3,
                      card_id=None, parameters={})
TERMINAL = frozenset(('succeeded', 'failed', 'cancelled', 'rejected', 'expired'))
BLOCKING = frozenset(('running', 'unknown'))
AUTOMATIC = frozenset(('auto', 'agent'))
OPERATORS = {'>': lambda a, b: a > b, '>=': lambda a, b: a >= b,
             '<': lambda a, b: a < b, '<=': lambda a, b: a <= b,
             '==': lambda a, b: a == b, '!=': lambda a, b: a != b}
DIMENSIONLESS_METRICS = frozenset(('side_peak_height_max', 'side_peak_prominence_max',
    'main_fwhm_fraction', 'fringe_period_correlation', 'fringe_interval_coverage_loss',
    'fringe_gap_mad_fraction', 'profile_noise_max', 'near_main_peak_prominence_max'))


def _known_unit(field):
    name = field.removeprefix('metrics.').removeprefix('fringe_metrics.')
    if name in DIMENSIONLESS_METRICS:
        return '1'
    if name == 'fringe_period_px':
        return 'px'
    return {'frequency_hz': 'Hz', 'error_hz': 'Hz', 'wavelength_nm': 'nm'}.get(field)


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _text(value, name, *, maximum=128, empty=False):
    if (not isinstance(value, str) or (not value and not empty)
            or len(value) > maximum or any(ord(c) < 32 for c in value)):
        raise ValueError('Invalid '+name)
    return value


def _field(value):
    if (not isinstance(value, str) or len(value) > 128
            or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_.]*', value)):
        raise ValueError('Invalid scalar field')
    if any(part.upper() in ('A', 'B', 'C', 'F2') for part in re.split(r'[._]', value)):
        raise ValueError('Retired A/B/C/F2 diagnostic fields cannot trigger window actions')
    return value


def _unit(value):
    value = _text(value, 'scalar unit', maximum=64)
    compact = value.replace(' ', '')
    return {'nm/V': 'nm/V', 'fm/mV': 'fm/mV',
            'dimensionless': '1', 'unitless': '1', '1': '1'}.get(compact, value)


def _midpoint_parameters(value):
    if not isinstance(value, dict):
        raise ValueError('Midpoint parameters must be a mapping')
    p = window_parameters({'parameters': value}, {})
    if p['mode'] != 'center_only':
        raise ValueError('Queued midpoint actions require finite center_only mode')
    for field in p['observables']:
        _field(field)
    if len(_json(p).encode()) > 32768:
        raise ValueError('Midpoint parameters exceed the storage bound')
    return p


def normalize_guard(value):
    """Validate one atomic guard/card configuration; disabled by default."""
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value)-set(GUARD_DEFAULTS):
        raise ValueError('Unknown window guard configuration')
    p = copy.deepcopy({**GUARD_DEFAULTS, **value})
    for key in ('enabled', 'agent_enabled'):
        if type(p[key]) is not bool:
            raise ValueError(key+' must be boolean')
    if p['combine'] not in ('any', 'all'):
        raise ValueError('Guard combine must be any or all')
    for key, maximum in (('confirm_samples', 100), ('max_per_hour', 1000)):
        if type(p[key]) is not int or not 1 <= p[key] <= maximum:
            raise ValueError('Invalid '+key)
    for key in ('measurement_max_age_s', 'dither_max_age_s'):
        if not _finite(p[key]) or not 0 < p[key] <= 3600:
            raise ValueError('Invalid '+key)
    if not _finite(p['cooldown_s']) or not 0 <= p['cooldown_s'] <= 86400:
        raise ValueError('Invalid cooldown_s')
    if not isinstance(p['conditions'], list) or len(p['conditions']) > 32:
        raise ValueError('Expected at most 32 scalar conditions')
    conditions = []
    for condition in p['conditions']:
        if not isinstance(condition, dict) or set(condition) != {'field', 'operator', 'threshold', 'unit'}:
            raise ValueError('Each condition needs field, operator, threshold and unit')
        c = dict(condition)
        c['field'] = _field(c['field'])
        c['unit'] = _unit(c['unit'])
        if not isinstance(c['operator'], str) or c['operator'] not in OPERATORS or not _finite(c['threshold']):
            raise ValueError('Expected a scalar comparison with a finite threshold')
        known = _known_unit(c['field'])
        if known is not None and c['unit'] != known:
            raise ValueError('Declared unit does not match scalar field '+c['field'])
        conditions.append(c)
    if len({_digest(c) for c in conditions}) != len(conditions):
        raise ValueError('Duplicate scalar conditions')
    p['conditions'] = conditions
    if p['enabled'] and not conditions:
        raise ValueError('Enabled guard requires scalar conditions')
    if p['card_id'] is None:
        if p['enabled'] or p['agent_enabled'] or p['parameters']:
            raise ValueError('Select a midpoint card and its parameters atomically')
        if not isinstance(p['parameters'], dict):
            raise ValueError('Midpoint parameters must be a mapping')
    else:
        _text(p['card_id'], 'midpoint card id')
        normalized = _midpoint_parameters(p['parameters'])
        # These are overrides of a registered card in a frozen deck. Filling
        # defaults here would erase its explicitly configured boundary limits.
        p['parameters'] = {key: normalized[key] for key in p['parameters']}
    _json(p)
    return p


def guard_revision(value):
    # Match interface_schema.digest without importing its operation registry.
    return hashlib.sha256(json.dumps(normalize_guard(value), sort_keys=True,
                                     allow_nan=False).encode()).hexdigest()


class ScalarGuard:
    """Count qualified, distinct acquisitions without issuing a command.

    Missing/invalid conditions clear their own votes. In ``all`` mode a missing
    condition clears the entire vote group. A duplicate cached source never
    earns another vote. Call note_trigger only after durable queue admission;
    durable, shared rate limits are also enforced by WindowQueue.claim.
    """
    def __init__(self, config=None, *, wall_clock=time.time):
        self.wall_clock = wall_clock
        self._trigger_times = []
        self._last_trigger = None
        self._context = None
        self.set_config(config)

    def set_config(self, config):
        normalized = normalize_guard(config)
        revision = guard_revision(normalized)
        changed = revision != getattr(self, 'revision', None)
        self.config = normalized
        self.revision = revision
        if changed:
            self._stamps = [None] * len(self.config['conditions'])
            self.invalidate('configuration changed')
        return changed

    def invalidate(self, reason='measurement unavailable'):
        self._votes = [0] * len(self.config['conditions'])
        self.last_evaluation = dict(status='unknown', reason=str(reason))

    def note_trigger(self, *, now=None):
        now = self.wall_clock() if now is None else now
        if not _finite(now):
            raise ValueError('Finite trigger time is required')
        self._trigger_times = [t for t in self._trigger_times if now-t <= 3600]
        self._trigger_times.append(now)
        self._last_trigger = now
        self.invalidate('request queued')

    @staticmethod
    def _value(row, field):
        if field.startswith(('metrics.', 'fringe_metrics.')):
            observation = row.get('observation', {})
            metrics = row.get('metrics', row.get('fringe_metrics',
                observation.get('metrics', {}) if isinstance(observation, dict) else {}))
            return metrics.get(field.split('.', 1)[1]) if isinstance(metrics, dict) else None
        if field in ('current', 'temperature', 'piezo'):
            outputs = row.get('outputs', {})
            return outputs.get(field) if isinstance(outputs, dict) else None
        value = row
        for part in field.split('.'):
            if not isinstance(value, dict):
                return None
            value = value.get(part)
        return value

    def _sample(self, row, condition, now, optical_stamp):
        field = condition['field']
        value = self._value(row, field)
        if not _finite(value):
            return None
        stamp = optical_stamp
        unit = None
        if field == 'dither_response':
            if row.get('dither_response_valid', row.get('dither_valid')) is not True:
                return None
            if row.get('dither_analysis_status', 'valid') != 'valid':
                return None
            stamp = row.get('dither_source_unix_s')
            if not _finite(stamp) or not 0 <= now-stamp <= self.config['dither_max_age_s']:
                return None
            age = row.get('dither_source_age_s', row.get('dither_age_s'))
            if age is not None and (not _finite(age) or not 0 <= age <= self.config['dither_max_age_s']):
                return None
            unit = row.get('dither_response_unit')
            if not isinstance(unit, str):
                return None
        else:
            units = row.get('metric_units', {})
            name = field.removeprefix('metrics.').removeprefix('fringe_metrics.')
            unit = units.get(field, units.get(name)) if isinstance(units, dict) else None
            if unit is None:
                unit = row.get(field+'_unit')
            if unit is None and field.removeprefix('outputs.') in ('current', 'temperature', 'piezo'):
                axis = field.removeprefix('outputs.')
                outputs = row.get('output_units', {})
                unit = outputs.get(axis) if isinstance(outputs, dict) else None
                if unit is None:
                    unit = row.get(axis+'_unit')
            if unit is None:
                unit = _known_unit(field)
            if unit is None:
                return None
        if unit is not None:
            try:
                unit = _unit(unit)
            except ValueError:
                return None
            target = condition['unit']
            if unit != target:
                scale = {('nm/V', 'fm/mV'): 1000., ('fm/mV', 'nm/V'): .001}.get((unit, target))
                if scale is None:
                    return None
                value *= scale
        return (value, stamp) if _finite(value) else None

    def observe(self, row, *, now=None, context=None):
        now = self.wall_clock() if now is None else now
        if not self.config['enabled']:
            self.invalidate('guard disabled')
            return None
        if context != self._context:
            self.invalidate('controller context changed')
            self._context = copy.deepcopy(context)
        if not isinstance(row, dict) or not _finite(now):
            self.invalidate('invalid observation')
            return None
        observation = row.get('observation', {})
        if not isinstance(observation, dict):
            observation = {}
        frequency = observation.get('frequency', {})
        if not isinstance(frequency, dict):
            frequency = {}
        stamp = row.get('optical_acquired_unix_s', row.get('acquired_unix_s',
            row.get('acquired_s', frequency.get('acquired_unix_s', frequency.get('acquired_s')))))
        # v5.1-hostcheck: quality labels are informational (human decision 2026-10-22); age and lock state remain.
        if (not _finite(stamp) or not 0 <= now-stamp <= self.config['measurement_max_age_s']
                or row.get('state', 'locked') != 'locked'):
            self.invalidate('fresh optical acquisition required')
            return None
        results = []
        missing = False
        new_bad = False
        for i, condition in enumerate(self.config['conditions']):
            sample = self._sample(row, condition, now, stamp)
            if sample is None:
                self._votes[i] = 0
                missing = True
                results.append(dict(condition=condition, status='unknown', votes=0))
                continue
            value, source = sample
            old_source = self._stamps[i]
            bad = OPERATORS[condition['operator']](value, condition['threshold'])
            if old_source is not None and source < old_source:
                self._votes[i] = 0
                bad = False
            elif old_source is None or source > old_source:
                self._votes[i] = min(self.config['confirm_samples'], self._votes[i]+1) if bad else 0
                self._stamps[i] = source
                new_bad |= bad
            elif not bad:
                self._votes[i] = 0
            results.append(dict(condition=condition, value=value, source_unix_s=source,
                                status='bad' if bad else 'good', votes=self._votes[i]))
        if missing and self.config['combine'] == 'all':
            self._votes = [0] * len(self._votes)
            for result in results:
                result['votes'] = 0
        qualified = [r['status'] == 'bad' and r['votes'] >= self.config['confirm_samples'] for r in results]
        confirmed = bool(qualified) and (any(qualified) if self.config['combine'] == 'any' else all(qualified))
        self._trigger_times = [t for t in self._trigger_times if now-t <= 3600]
        limited = (len(self._trigger_times) >= self.config['max_per_hour']
                   or self._last_trigger is not None and now-self._last_trigger < self.config['cooldown_s'])
        self.last_evaluation = dict(status='rate_limited' if limited else 'confirmed' if confirmed else 'observing',
                                    conditions=results, guard_revision=self.revision)
        if not confirmed or not new_bad or limited:
            return None
        return dict(guard_revision=self.revision, acquired_unix_s=stamp,
                    observed_unix_s=now, combine=self.config['combine'],
                    conditions=copy.deepcopy(results))


class WindowQueue:
    """A bounded durable queue; all API, worker and agent access shares flock.

    Entries are never silently evicted: that would permit old request IDs to
    replay. A full ledger refuses new IDs. Claimed work remains running across
    object/process replacement, blocking every subsequent claim until a known
    terminal result is committed. Unknown outcomes require external review.
    """
    def __init__(self, directory, *, wall_clock=time.time, max_entries=1024, max_pending=64):
        self.directory = Path(directory)
        if not self.directory.is_dir() or self.directory.is_symlink():
            raise ValueError('Window queue requires an existing regular job directory')
        if type(max_entries) is not int or not 1 <= max_entries <= 10000:
            raise ValueError('Invalid queue storage bound')
        if type(max_pending) is not int or not 1 <= max_pending <= min(max_entries, 256):
            raise ValueError('Invalid pending queue bound')
        self.wall_clock = wall_clock
        self.max_entries = max_entries
        self.max_pending = max_pending
        self.path = self.directory/'window-actions.json'
        self.lock_path = self.directory/'window-actions.lock'

    @contextmanager
    def _locked(self):
        flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0)
        descriptor = os.open(self.lock_path, flags, 0o660)
        with os.fdopen(descriptor, 'a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    def _read(self):
        if self.path.is_symlink():
            raise ValueError('Queue state must not be a symbolic link')
        if not self.path.exists():
            return dict(version=1, job_id=self.directory.name, entries={}, rate_events=[])
        if not self.path.is_file() or self.path.stat().st_size > 64*1024*1024:
            raise ValueError('Queue state exceeds the storage bound')
        data = json.loads(self.path.read_text())
        if (not isinstance(data, dict) or data.get('version') != 1
                or data.get('job_id') != self.directory.name
                or not isinstance(data.get('entries'), dict) or not isinstance(data.get('rate_events'), list)
                or len(data['entries']) > 10000 or len(data['rate_events']) > 10000):
            raise ValueError('Invalid durable window queue state')
        for identifier, entry in data['entries'].items():
            if (not isinstance(entry, dict) or entry.get('request_id') != identifier
                    or entry.get('job_id') != self.directory.name
                    or entry.get('status') not in TERMINAL | BLOCKING | {'queued'}):
                raise ValueError('Invalid durable window request')
        return data

    def _write(self, data):
        body = _json(data)+'\n'
        if len(body.encode()) > 64*1024*1024:
            raise ValueError('Queue state exceeds the storage bound')
        temporary = self.path.with_name(self.path.name+'.'+uuid.uuid4().hex+'.tmp')
        try:
            with temporary.open('x') as handle:
                os.chmod(temporary, 0o660)
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(self.path)
            descriptor = os.open(self.directory, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            temporary.unlink(missing_ok=True)

    def _now(self):
        now = self.wall_clock()
        if not _finite(now) or now < 0:
            raise ValueError('Invalid queue clock')
        return now

    @staticmethod
    def _entry(data, request_id):
        if request_id not in data['entries']:
            raise KeyError(request_id)
        return data['entries'][request_id]

    def enqueue(self, request, *, capability_revision=None):
        required = {'request_id', 'card_id', 'controller_card_id', 'parameters', 'source', 'capability_revision'}
        optional = {'job_id', 'guard_revision', 'ttl_s', 'not_before_unix_s', 'reason', 'evidence', 'reservation'}
        if not isinstance(request, dict) or not required <= set(request) or set(request)-required-optional:
            raise ValueError('Invalid window request fields')
        request = copy.deepcopy(request)
        for key in ('request_id', 'card_id', 'controller_card_id', 'capability_revision'):
            _text(request[key], key)
        if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', request['request_id']):
            raise ValueError('Invalid stable window request id')
        if request.get('job_id', self.directory.name) != self.directory.name:
            raise ValueError('Window request belongs to another job')
        if request['source'] not in ('manual', 'auto', 'agent'):
            raise ValueError('Invalid window request source')
        if request['source'] in AUTOMATIC:
            _text(request.get('guard_revision'), 'guard revision')
        elif request.get('guard_revision') is not None:
            _text(request['guard_revision'], 'guard revision')
        request['parameters'] = _midpoint_parameters(request['parameters'])
        if request['parameters']['controller_card_id'] != request['controller_card_id']:
            raise ValueError('Midpoint controller does not match request')
        _text(request.get('reason', ''), 'request reason', maximum=2048, empty=True)
        ttl = request.get('ttl_s', 3600.)
        if not _finite(ttl) or not 0 < ttl <= 86400:
            raise ValueError('Request ttl_s must be within 24 hours')
        if len(_json(request).encode()) > 65536:
            raise ValueError('Window request exceeds the storage bound')
        digest = _digest(request)
        with self._locked():
            data = self._read()
            prior = data['entries'].get(request['request_id'])
            if prior is not None:
                if prior['request_digest'] != digest:
                    raise ValueError('Request id already used with different payload')
                return copy.deepcopy(prior)
            if capability_revision is not None and request['capability_revision'] != capability_revision:
                raise ValueError('Window capability revision changed')
            now = self._now()
            not_before = request.get('not_before_unix_s', now)
            if not _finite(not_before) or not 0 <= not_before <= now+86400 or not_before >= now+ttl:
                raise ValueError('Scheduled time must precede request expiry within 24 hours')
            if len(data['entries']) >= self.max_entries:
                raise ValueError('Window queue durable storage is full')
            if sum(e['status'] == 'queued' for e in data['entries'].values()) >= self.max_pending:
                raise ValueError('Window queue pending limit reached')
            if request['source'] in AUTOMATIC and any(e['source'] in AUTOMATIC and e['status'] in BLOCKING | {'queued'}
                                                     for e in data['entries'].values()):
                raise ValueError('An automatic window action is already queued or active')
            entry = dict(request, job_id=self.directory.name, request_digest=digest, revision=1,
                         status='queued', created_unix_s=now, updated_unix_s=now,
                         not_before_unix_s=not_before, expires_unix_s=now+ttl)
            data['entries'][entry['request_id']] = entry
            self._write(data)
            return copy.deepcopy(entry)

    def list(self):
        with self._locked():
            return copy.deepcopy(sorted(self._read()['entries'].values(),
                                        key=lambda e: (e['created_unix_s'], e['request_id'])))

    def get(self, request_id):
        with self._locked():
            return copy.deepcopy(self._entry(self._read(), request_id))

    def amend(self, request_id, expected_revision, *, card_id=None, parameters=None,
              not_before_unix_s=None, ttl_s=None):
        with self._locked():
            data = self._read()
            entry = self._entry(data, request_id)
            if entry['status'] != 'queued' or entry['source'] != 'manual':
                raise ValueError('Only an unclaimed manual request can be amended')
            if type(expected_revision) is not int or expected_revision != entry['revision']:
                raise ValueError('Queue revision changed')
            now = self._now()
            if now > entry['expires_unix_s']:
                raise ValueError('Expired request cannot be amended')
            if card_id is not None:
                entry['card_id'] = _text(card_id, 'midpoint card id')
            if parameters is not None:
                entry['parameters'] = _midpoint_parameters(parameters)
                if entry['parameters']['controller_card_id'] != entry['controller_card_id']:
                    raise ValueError('Midpoint controller does not match request')
            if ttl_s is not None:
                if not _finite(ttl_s) or not 0 < ttl_s <= 86400:
                    raise ValueError('Request ttl_s must be within 24 hours')
                entry['expires_unix_s'] = now+ttl_s
            if not_before_unix_s is not None:
                if not _finite(not_before_unix_s) or not 0 <= not_before_unix_s <= now+86400:
                    raise ValueError('Invalid scheduled time')
                entry['not_before_unix_s'] = not_before_unix_s
            if entry['not_before_unix_s'] >= entry['expires_unix_s']:
                raise ValueError('Scheduled time must precede request expiry')
            entry['revision'] += 1
            entry['updated_unix_s'] = now
            self._write(data)
            return copy.deepcopy(entry)

    def cancel(self, request_id, expected_revision=None, *, reason='cancelled before claim'):
        with self._locked():
            data = self._read()
            entry = self._entry(data, request_id)
            if entry['status'] == 'cancelled':
                return copy.deepcopy(entry)
            if entry['status'] != 'queued':
                raise ValueError('Claimed work must be cancelled by its active worker')
            if expected_revision is not None and (type(expected_revision) is not int or expected_revision != entry['revision']):
                raise ValueError('Queue revision changed')
            self._finish(entry, 'cancelled', reason=reason)
            self._write(data)
            return copy.deepcopy(entry)

    def request_cancel(self, request_id, expected_revision=None, *, reason='user requested cancellation'):
        """Cancel waiting work or ask its existing worker to stop running work.

        A running entry stays running and blocks the queue until its worker
        confirms release. This method never sends a physical command or turns
        an unknown output into an apparently successful cancellation.
        """
        _text(reason, 'cancellation reason', maximum=4096, empty=True)
        with self._locked():
            data = self._read()
            entry = self._entry(data, request_id)
            if entry['status'] == 'cancelled' or entry['status'] == 'running' and entry.get('cancel_requested'):
                return copy.deepcopy(entry)
            if entry['status'] not in ('queued', 'running'):
                raise ValueError('Only queued or running work accepts a cancellation request')
            if expected_revision is not None and (type(expected_revision) is not int or expected_revision != entry['revision']):
                raise ValueError('Queue revision changed')
            if entry['status'] == 'queued':
                self._finish(entry, 'cancelled', reason=reason)
            else:
                entry.update(cancel_requested=True, cancel_reason=reason,
                             updated_unix_s=self._now(), revision=entry['revision']+1)
            self._write(data)
            return copy.deepcopy(entry)

    def _finish(self, entry, status, *, result=None, reason=None):
        if reason is not None:
            _text(reason, 'completion reason', maximum=4096, empty=True)
        if len(_json(result).encode()) > 65536:
            raise ValueError('Window result exceeds the storage bound')
        entry.update(status=status, result=copy.deepcopy(result), reason=reason,
                     updated_unix_s=self._now(), revision=entry['revision']+1)

    def complete(self, request_id, status, *, result=None, reason=None):
        if status not in TERMINAL | {'unknown'}:
            raise ValueError('Expected terminal or unknown window result')
        with self._locked():
            data = self._read()
            entry = self._entry(data, request_id)
            if entry['status'] in TERMINAL | {'unknown'}:
                if entry['status'] == status and entry.get('result') == result and entry.get('reason') == reason:
                    return copy.deepcopy(entry)
                raise ValueError('A completed or unknown request cannot be overwritten')
            if entry['status'] != 'running':
                raise ValueError('Window result requires an existing claim')
            self._finish(entry, status, result=result, reason=reason)
            self._write(data)
            return copy.deepcopy(entry)

    @staticmethod
    def _rate(data, now, cooldown_s, max_per_hour):
        times = [event['unix_s'] for event in data['rate_events'] if now-event['unix_s'] <= 3600]
        last = data.get('last_automatic_unix_s', max(times) if times else None)
        limited = len(times) >= max_per_hour or last is not None and now-last < cooldown_s
        next_at = max((last+cooldown_s if last is not None else now),
                      (sorted(times)[-max_per_hour]+3600 if len(times) >= max_per_hour else now))
        return dict(limited=bool(limited), count_last_hour=len(times),
                    last_started_unix_s=last,
                    next_eligible_unix_s=next_at)

    def rate_status(self, *, cooldown_s=0., max_per_hour=1000):
        self._rate_parameters(cooldown_s, max_per_hour)
        with self._locked():
            return self._rate(self._read(), self._now(), cooldown_s, max_per_hour)

    @staticmethod
    def _rate_parameters(cooldown_s, max_per_hour):
        if (not _finite(cooldown_s) or not 0 <= cooldown_s <= 86400
                or type(max_per_hour) is not int or not 1 <= max_per_hour <= 10000):
            raise ValueError('Invalid automatic window rate limits')

    def claim(self, *, capability_revision, guard_revision=None, cooldown_s=0., max_per_hour=1000):
        """Claim one eligible item; return None for busy, scheduled or rate-limited work.

        The trusted worker must already have checked the selected registered card,
        current agent authorization, owner readiness and competing operations.
        Revision changes reject stale items before any claim or physical action.
        """
        _text(capability_revision, 'capability revision')
        self._rate_parameters(cooldown_s, max_per_hour)
        with self._locked():
            data = self._read()
            now = self._now()
            dirty = False
            for entry in data['entries'].values():
                if entry['status'] != 'queued':
                    continue
                status = reason = None
                if now >= entry['expires_unix_s']:
                    status, reason = 'expired', 'request deadline reached before claim'
                elif entry['capability_revision'] != capability_revision:
                    status, reason = 'rejected', 'controller capability revision changed'
                elif entry['source'] in AUTOMATIC and entry.get('guard_revision') != guard_revision:
                    status, reason = 'cancelled', 'window guard configuration changed'
                if status:
                    self._finish(entry, status, reason=reason)
                    dirty = True
            if any(entry['status'] in BLOCKING for entry in data['entries'].values()):
                if dirty:
                    self._write(data)
                return None
            entries = sorted(data['entries'].values(), key=lambda e: (e['created_unix_s'], e['request_id']))
            for entry in entries:
                if entry['status'] != 'queued' or now < entry['not_before_unix_s']:
                    continue
                if entry['source'] in AUTOMATIC and self._rate(data, now, cooldown_s, max_per_hour)['limited']:
                    continue
                entry.update(status='running', claimed_unix_s=now, updated_unix_s=now,
                             claim_id=uuid.uuid4().hex, revision=entry['revision']+1)
                if entry['source'] in AUTOMATIC:
                    data['rate_events'] = [event for event in data['rate_events'] if now-event['unix_s'] <= 3600]
                    data['rate_events'].append(dict(request_id=entry['request_id'], unix_s=now, source=entry['source']))
                    data['last_automatic_unix_s'] = now
                self._write(data)
                return copy.deepcopy(entry)
            if dirty:
                self._write(data)
            return None
