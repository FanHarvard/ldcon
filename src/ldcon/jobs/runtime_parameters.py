"""Worker-owned, versioned parameter transactions at explicit safe boundaries.

The dashboard writes requests only. A card explicitly declares every injectable
field and calls ``poll`` before planning its next action. This module never opens
an instrument, changes an owner safety envelope, or retries an uncertain write.
"""
from contextlib import contextmanager
import copy
from dataclasses import dataclass
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
import uuid


class ParameterRejected(ValueError):
    """A request was rejected before any physical side effect."""


class PrewriteRejected(ParameterRejected):
    """An owner explicitly confirms that a requested change was not written."""


class UncertainParameterUpdate(RuntimeError):
    """Do not retry or perform rollback writes; normal owner fault handling applies."""


@dataclass(frozen=True)
class ParameterSpec:
    boundaries: tuple = ('between_points',)
    group: str | None = None
    reset_required: bool = False
    effect: str = 'memory'
    maximum: float | None = None
    description: str = ''
    while_busy: bool = False

    def __post_init__(self):
        if (not self.boundaries or any(not isinstance(x, str) or not x for x in self.boundaries)
                or self.effect not in ('memory', 'owner')
                or self.group is not None and (not isinstance(self.group, str) or not self.group)):
            raise ValueError('Invalid runtime parameter specification')
        if type(self.while_busy) is not bool or self.while_busy and self.effect != 'memory':
            raise ValueError('Busy-safe parameters must have memory-only effects')
        if self.maximum is not None and not _finite(self.maximum):
            raise ValueError('Invalid runtime parameter ceiling')

    def describe(self):
        return dict(boundaries=list(self.boundaries), group=self.group,
                    reset_required=self.reset_required, effect=self.effect,
                    maximum=self.maximum, description=self.description, while_busy=self.while_busy)


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _serialized(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_serialized(value).encode()).hexdigest()


def _read(path, default=None):
    try:
        if path.stat().st_size > 1024 * 1024:
            return default
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default


def _write(path, value):
    """Unique temporary name also permits independent API readers/writers."""
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            os.chmod(temporary, 0o660)
            stream.write(_serialized(value) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _inbox_lock(directory):
    with (directory / 'runtime-parameters.lock').open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def _validate_request(payload):
    if (not isinstance(payload, dict)
            or set(payload) != {'request_id', 'job_id', 'card_id', 'expected_revision', 'patch'}):
        raise ParameterRejected('Expected request_id, job_id, card_id, expected_revision and patch')
    if not isinstance(payload['request_id'], str) or not re.fullmatch('[0-9a-f]{32}', payload['request_id']):
        raise ParameterRejected('Invalid runtime parameter request id')
    if any(not isinstance(payload[k], str) or not payload[k] for k in ('job_id', 'card_id', 'expected_revision')):
        raise ParameterRejected('Missing runtime parameter context')
    patch = payload['patch']
    if not isinstance(patch, dict) or not patch or any(not isinstance(k, str) for k in patch):
        raise ParameterRejected('A nonempty parameter mapping is required')
    try:
        encoded = _serialized(payload)
    except (ValueError, TypeError) as error:
        raise ParameterRejected('Parameters must be finite JSON values') from error
    if len(encoded.encode()) > 65536:
        raise ParameterRejected('Runtime parameter request exceeds 64 KiB')


def _candidate(parameters, patch, schema):
    unknown = set(patch) - set(schema)
    if unknown:
        raise ParameterRejected('Not injectable: ' + ', '.join(sorted(unknown)))
    groups = {schema[key]['group'] for key in patch if schema[key].get('group')}
    for group in groups:
        required = {key for key, item in schema.items() if item.get('group') == group}
        if not required <= set(patch):
            raise ParameterRejected('Coupled group ' + group + ' requires: ' + ', '.join(sorted(required)))
    candidate = copy.deepcopy(parameters)
    candidate.update(copy.deepcopy(patch))
    for key in patch:
        limit = schema[key].get('maximum')
        if limit is not None and (not _finite(candidate[key]) or candidate[key] > limit):
            raise ParameterRejected(key + ' exceeds the invocation safety ceiling')
    return candidate


def busy_safe_patch(patch, schema):
    return bool(patch) and all(key in schema and schema[key].get('while_busy') is True
        and schema[key].get('effect') == 'memory' for key in patch)


def enqueue_runtime_patch(directory, payload, *, ttl_s=60., wall_clock=time.time):
    """Dashboard/API entry: validate envelope and queue; never call a device.

    Cross-field/device validation is repeated by the worker against its actual
    current revision. Only one outstanding transaction is allowed per worker.
    Reusing an id is idempotent only when the entire original payload is equal.
    """
    directory = Path(directory)
    _validate_request(payload)
    if not _finite(ttl_s) or not 0 < ttl_s <= 600:
        raise ParameterRejected('Runtime request lifetime must be within 600 seconds')
    identity = _digest(payload)
    request_id = payload['request_id']
    with _inbox_lock(directory):
        for name in ('result', 'inflight', 'request'):
            old = _read(directory / ('runtime-parameter-' + name + '-' + request_id + '.json'))
            if old is not None:
                if old.get('request_digest') != identity:
                    raise ParameterRejected('Request id already belongs to different parameters')
                return old
        live = _read(directory / 'runtime-parameters.json', {})
        if live.get('status') != 'running':
            raise ParameterRejected('Worker does not accept runtime parameter changes')
        if any(payload[k] != live.get(k) for k in ('job_id', 'card_id')):
            raise ParameterRejected('Runtime parameter worker context changed')
        if payload['expected_revision'] != live.get('revision'):
            raise ParameterRejected('Runtime parameter revision changed; reload before editing')
        _candidate(live['parameters'], payload['patch'], live['schema'])
        if (live.get('busy') and not busy_safe_patch(payload['patch'], live['schema'])) or any(directory.glob('runtime-parameter-request-*.json')) or any(directory.glob('runtime-parameter-inflight-*.json')):
            raise ParameterRejected('Another controller operation or parameter transaction is active')
        now = wall_clock()
        request = dict(copy.deepcopy(payload), request_digest=identity, status='queued',
                       created_unix_s=now, expires_unix_s=now + ttl_s)
        _write(directory / ('runtime-parameter-request-' + request_id + '.json'), request)
        return request


class RuntimeParameters:
    """A transaction changes the shared parameter mapping, never a copied local.

    ``validator`` validates the *whole* proposed parameter mapping and returns
    its normalized form. ``reset(candidate, changed)`` is a memory-only callback:
    clear proposals, comparison windows and dependent PID state. It must not
    write hardware, retain old proposals, or count elapsed waiting as integration.

    Owner-effect fields additionally require ``apply(candidate, changed)``.
    That callback must make one versioned, atomic owner transaction. It may raise
    PrewriteRejected only after confirmed non-dispatch. Any other exception is
    uncertain: the local map remains unchanged, this inbox becomes blocked and
    the exception propagates to the worker's existing hold/fault/lease handling.
    There are deliberately no physical rollback writes in this module.
    """
    def __init__(self, parameters, *, validator, specs, directory, job_id,
                 card_id, operation, reset=None, apply=None, emit=None,
                 wall_clock=time.time):
        self.parameters = parameters
        self.validator = validator
        self.specs = dict(specs)
        self.directory = Path(directory)
        self.job_id = job_id
        self.card_id = card_id
        self.operation = operation
        self.reset = reset
        self.apply = apply
        self.emit = emit or (lambda kind, value: None)
        self.wall_clock = wall_clock
        self.sequence = 0
        self.status = 'running'
        self.busy = False
        self.boundary = None
        self.observed_source = None
        if (not isinstance(parameters, dict) or not self.specs
                or set(self.specs) - set(parameters)
                or any(not isinstance(s, ParameterSpec) for s in self.specs.values())):
            raise ValueError('Declare only existing, explicitly injectable parameters')
        if any(s.reset_required for s in self.specs.values()) and reset is None:
            raise ValueError('State-dependent parameters require a reset callback')
        if any(s.effect == 'owner' for s in self.specs.values()) and apply is None:
            raise ValueError('Owner parameters require an explicit owner transaction callback')
        if self.validator(copy.deepcopy(parameters)) != parameters:
            raise ValueError('Runtime parameter mapping must already be normalized')
        self._committed = copy.deepcopy(parameters)
        self.revision = self._revision()
        # A replaced/crashed worker must not silently replay claimed work or
        # overwrite a previous revision with the invocation's old parameters.
        if (self.directory / 'runtime-parameters.json').exists() or any(self.directory.glob('runtime-parameter-inflight-*.json')):
            raise ValueError('Runtime state exists; resume requires explicit state reconciliation')
        self.publish()

    def _revision(self):
        return str(self.sequence) + ':' + _digest(self._committed)[:20]

    def publish(self):
        value = dict(protocol_version=1, job_id=self.job_id, card_id=self.card_id,
                     operation=self.operation, revision=self.revision,
                     parameters=copy.deepcopy(self._committed),
                     schema={key: rule.describe() for key, rule in self.specs.items()},
                     status=self.status, busy=self.busy, boundary=self.boundary,
                     observed_source=self.observed_source,
                     updated_unix_s=self.wall_clock())
        _write(self.directory / 'runtime-parameters.json', value)
        return value

    def _finish(self, request, status, reason, **details):
        value = dict(request_id=request['request_id'], request_digest=request.get('request_digest'),
                     job_id=self.job_id, card_id=self.card_id, status=status, reason=reason,
                     expected_revision=request.get('expected_revision'), revision=self.revision,
                     boundary=self.boundary, updated_unix_s=self.wall_clock(), **details)
        _write(self.directory / ('runtime-parameter-result-' + request['request_id'] + '.json'), value)
        self._history(value)
        self.publish()
        self.emit('runtime_parameters', value)
        return value

    def _history(self, value):
        with (self.directory / 'runtime-parameter-history.jsonl').open('a', encoding='utf-8') as stream:
            os.chmod(stream.name, 0o660)
            stream.write(_serialized(value) + '\n')
            stream.flush()
            os.fsync(stream.fileno())

    def observe_external(self, values, *, source, source_revision, boundary):
        """Mirror a trusted owner's already-applied state, without any write.

        Called by the worker at a safe boundary after an authoritative owner
        read. Only owner-managed declared fields are accepted. A changed owner
        revision advances this local revision even if values are unchanged, so
        queued edits from before a recovery/hot-control cannot silently apply.
        This is not an API for users and never calls ``apply`` or ``reset``: the
        owner already performed its guarded transition and discarded proposals.
        """
        if (self.status != 'running' or self.parameters != self._committed
                or not isinstance(source, str) or not source
                or not isinstance(source_revision, str) or not source_revision
                or not isinstance(boundary, str) or not boundary):
            raise UncertainParameterUpdate('Cannot reconcile external parameter state')
        if not isinstance(values, dict) or any(key not in self.specs or self.specs[key].effect != 'owner' for key in values):
            raise ParameterRejected('Only declared owner parameters can be observed')
        before = copy.deepcopy(self._committed)
        candidate = _candidate(before, values, {key: spec.describe() for key, spec in self.specs.items()})
        normalized = self.validator(copy.deepcopy(candidate))
        if normalized != candidate:
            raise ParameterRejected('Observed owner state must already be normalized')
        _serialized(candidate)
        source_context = dict(source=source, revision=source_revision)
        if self.observed_source == source_context and candidate == before:
            return None
        self.parameters.clear()
        self.parameters.update(copy.deepcopy(candidate))
        self._committed = copy.deepcopy(candidate)
        self.sequence += 1
        self.revision = self._revision()
        self.boundary = boundary
        self.observed_source = source_context
        value = dict(status='observed', source=source, source_revision=source_revision,
                     job_id=self.job_id, card_id=self.card_id, revision=self.revision,
                     boundary=boundary, updated_unix_s=self.wall_clock(),
                     changed={key: dict(before=before[key], after=candidate[key])
                              for key in sorted(candidate) if candidate[key] != before[key]},
                     reason='Synchronized already-applied owner state; no output write')
        self._history(value)
        self.publish()
        self.emit('runtime_parameters_observed', value)
        return value

    def poll(self, *, boundary, busy=False):
        """Apply at most one queued patch before a fresh plan or observation.

        A patch that needs another boundary stays queued until that boundary or
        expiry. Busy requests are explicitly rejected instead of becoming an
        unexpected action after a study/recovery finishes.
        """
        if not isinstance(boundary, str) or not boundary:
            raise ValueError('A named safe boundary is required')
        self.boundary = boundary
        self.busy = bool(busy)
        if self.parameters != self._committed:
            self.status = 'uncertain'
            self.publish()
            raise UncertainParameterUpdate('Parameters changed outside their versioned transaction')
        if self.status != 'running':
            raise UncertainParameterUpdate('Runtime parameter processing is blocked')
        self.publish()
        with _inbox_lock(self.directory):
            paths = sorted(self.directory.glob('runtime-parameter-request-*.json'))
            if not paths:
                return None
            path = paths[0]
            request = _read(path)
            identifier = path.stem.removeprefix('runtime-parameter-request-')
            if not re.fullmatch('[0-9a-f]{32}', identifier):
                raise ParameterRejected('Invalid runtime parameter queue filename')
            if not isinstance(request, dict) or request.get('request_id') != identifier:
                request = dict(request_id=identifier, invalid=True)
            patch = request.get('patch')
            valid_boundary = all(boundary in self.specs[key].boundaries
                                 for key in patch if key in self.specs) if isinstance(patch, dict) else True
            expires = request.get('expires_unix_s')
            unexpired = _finite(expires) and self.wall_clock() <= expires
            if not busy and valid_boundary is False and unexpired:
                return None
            claimed = path.with_name('runtime-parameter-inflight-' + identifier + '.json')
            path.replace(claimed)
        try:
            return self._apply_request(request, busy=busy)
        finally:
            # A crash before finishing leaves this claim as evidence. An
            # ordinary return (including uncertainty) has a durable result.
            if (self.directory / ('runtime-parameter-result-' + identifier + '.json')).exists():
                claimed.unlink(missing_ok=True)

    def _apply_request(self, request, *, busy):
        previous = _read(self.directory / ('runtime-parameter-result-' + request['request_id'] + '.json'))
        if previous is not None:
            # A duplicate filesystem delivery cannot overwrite the original
            # acknowledgement or run a second owner transaction.
            if previous.get('request_digest') == request.get('request_digest'):
                return previous
            value = dict(request_id=request['request_id'], status='rejected',
                         reason='Request id already completed with different parameters',
                         revision=self.revision)
            self.emit('runtime_parameters_replay_rejected', value)
            return value
        before = copy.deepcopy(self._committed)
        changed = set()
        try:
            payload = {key: request.get(key) for key in ('request_id', 'job_id', 'card_id', 'expected_revision', 'patch')}
            _validate_request(payload)
            if request.get('request_digest') != _digest(payload):
                raise ParameterRejected('Runtime request integrity mismatch')
            if (request.get('job_id') != self.job_id or request.get('card_id') != self.card_id
                    or request.get('expected_revision') != self.revision):
                raise ParameterRejected('Stale or mismatched runtime parameter context')
            expires = request.get('expires_unix_s')
            if not _finite(expires) or not 0 <= expires - self.wall_clock() <= 600:
                raise ParameterRejected('Runtime parameter request expired')
            if busy and not busy_safe_patch(request['patch'], {k:s.describe() for k,s in self.specs.items()}):
                raise ParameterRejected('Another controller operation is active')
            candidate = _candidate(before, request['patch'], {k: s.describe() for k, s in self.specs.items()})
            candidate = self.validator(copy.deepcopy(candidate))
            if not isinstance(candidate, dict) or set(candidate) != set(before):
                raise ParameterRejected('Validator changed the parameter schema')
            _serialized(candidate)
            changed = {key for key in before if candidate[key] != before[key]}
            if changed - set(request['patch']):
                raise ParameterRejected('Normalization changed a field outside this transaction')
            if not changed:
                return self._finish(request, 'applied', 'No change', changed={})
            for key in changed:
                if self.boundary not in self.specs[key].boundaries:
                    raise ParameterRejected('Unsafe runtime boundary for ' + key)
                limit = self.specs[key].maximum
                if limit is not None and (not _finite(candidate[key]) or candidate[key] > limit):
                    raise ParameterRejected(key + ' exceeds the invocation safety ceiling')
            # Invalidate derived state even when a later pre-write check rejects:
            # losing a comparison window is safe; restoring stale proposals is not.
            if any(self.specs[key].reset_required for key in changed):
                self.reset(copy.deepcopy(candidate), frozenset(changed))
        except Exception as error:
            return self._finish(request, 'rejected', str(error), changed={})
        owner_change = any(self.specs[key].effect == 'owner' for key in changed)
        if owner_change:
            try:
                self.apply(copy.deepcopy(candidate), frozenset(changed))
            except PrewriteRejected as error:
                return self._finish(request, 'rejected', str(error), changed={}, prewrite_confirmed=True)
            except Exception as error:
                self.status = 'uncertain'
                self._finish(request, 'uncertain', str(error), changed={},
                             message='No automatic retry or physical rollback; owner review required')
                raise UncertainParameterUpdate('Owner parameter update outcome is uncertain') from error
        # No await, I/O or callback between local map and revision publication.
        # Cards must read this same mapping on their next fresh planning cycle.
        self.parameters.clear()
        self.parameters.update(copy.deepcopy(candidate))
        self._committed = copy.deepcopy(candidate)
        self.sequence += 1
        self.revision = self._revision()
        return self._finish(request, 'applied', 'Applied at safe boundary',
                            changed={key: dict(before=before[key], after=candidate[key]) for key in sorted(changed)})

    def close(self):
        """Publish termination and reject pending edits without applying them."""
        self.status = 'closed' if self.status == 'running' else self.status
        self.publish()
        with _inbox_lock(self.directory):
            for path in sorted(self.directory.glob('runtime-parameter-request-*.json')):
                request = _read(path)
                if isinstance(request, dict) and re.fullmatch('[0-9a-f]{32}', str(request.get('request_id', ''))):
                    self._finish(request, 'rejected', 'Card ended before a safe boundary', changed={})
                    path.unlink()
