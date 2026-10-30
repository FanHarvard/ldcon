"""Private, bounded event spool for an interactive Claude resident.

``initialize(root, session, cwd)`` creates one immutable connection identity and
returns its epoch. Never reuse a root for another connection. ``read_events``
atomically consumes the pending events in durable sequence order. The CLI reads
one hook JSON object from stdin and emits no hook decision or model context.
Only ``Stop.last_assistant_message`` is retained as text; prompts are hashed.
This module does not decide whether a turn completed or approve any operation.
"""

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import time
import uuid

from ldcon.agents.failure_policy import URL

EVENTS = frozenset({'SessionStart', 'UserPromptSubmit', 'Stop', 'StopFailure',
                    'SessionEnd', 'PostModelSwitch', 'ModelStatus'})
MAX_INPUT_BYTES = 1024 * 1024
MAX_PROMPT_BYTES = 262144
MAX_MESSAGE_BYTES = 32768
MAX_EVENTS = 64
MAX_STATE_BYTES = 8 * 1024 * 1024
LOCK_TIMEOUT_S = 5.
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
FILE_FLAGS = os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}')


class ProtocolError(RuntimeError):
    """The spool cannot safely represent or consume this connection."""


def _uuid(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def _directory(root, *, private=True):
    """Reject symlinks in every path component, including the private root."""
    path = Path(os.path.abspath(root))
    descriptor = os.open(path.anchor, DIRECTORY_FLAGS)
    try:
        for component in path.parts[1:]:
            child = os.open(component, DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        info = os.fstat(descriptor)
        if private and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
            raise ProtocolError('Hook spool directory must be private and owned by this user')
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _check_file(descriptor, limit):
    info = os.fstat(descriptor)
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
            info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
        raise ProtocolError('Hook spool file is not a private single-link regular file')
    if info.st_size > limit:
        raise ProtocolError('Hook spool file exceeds its size limit')


@contextmanager
def _locked(root):
    directory = _directory(root)
    lock = None
    try:
        lock = os.open('lock', os.O_RDWR | os.O_CREAT | FILE_FLAGS, 0o600, dir_fd=directory)
        _check_file(lock, 0)
        deadline = time.monotonic() + LOCK_TIMEOUT_S
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ProtocolError('Hook spool lock deadline exceeded')
                time.sleep(.01)
        yield directory
    finally:
        if lock is not None:
            os.close(lock)
        os.close(directory)


def _load(directory):
    descriptor = os.open('state.json', os.O_RDONLY | FILE_FLAGS, dir_fd=directory)
    try:
        _check_file(descriptor, MAX_STATE_BYTES)
        with os.fdopen(descriptor, 'rb', closefd=False) as stream:
            raw = stream.read(MAX_STATE_BYTES + 1)
        if len(raw) > MAX_STATE_BYTES:
            raise ProtocolError('Hook spool state exceeds its size limit')
        state = json.loads(raw)
    except (ValueError, UnicodeError) as error:
        raise ProtocolError('Hook spool state is invalid') from error
    finally:
        os.close(descriptor)
    if (not isinstance(state, dict) or state.get('schema_version') != 1 or
            not _uuid(state.get('epoch')) or not _uuid(state.get('session_id')) or
            not isinstance(state.get('cwd'), str) or not os.path.isabs(state['cwd']) or
            type(state.get('next_sequence')) is not int or state['next_sequence'] < 1 or
            type(state.get('faulted')) is not bool or
            not isinstance(state.get('events'), list) or len(state['events']) > MAX_EVENTS):
        raise ProtocolError('Hook spool state has an invalid shape')
    previous = 0
    for event in state['events']:
        if (not isinstance(event, dict) or event.get('epoch') != state['epoch'] or
                type(event.get('sequence')) is not int or
                not previous < event['sequence'] < state['next_sequence']):
            raise ProtocolError('Hook spool sequence or epoch is invalid')
        previous = event['sequence']
    return state


def _save(directory, state):
    data = (json.dumps(state, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8')
    if len(data) > MAX_STATE_BYTES:
        raise ProtocolError('Hook spool state exceeds its size limit')
    temporary = '.state-' + uuid.uuid4().hex
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | FILE_FLAGS,
                         0o600, dir_fd=directory)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, 'state.json', src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass


def initialize(root, session, cwd):
    """Create a connection spool; refuse replacing even a fully consumed spool."""
    if not _uuid(session) or not isinstance(cwd, (str, Path)) or not os.path.isabs(cwd):
        raise ValueError('Canonical session UUID and absolute workspace required')
    cwd = os.path.abspath(cwd)
    if not Path(cwd).is_dir():
        raise ValueError('Existing workspace required')
    root = Path(os.path.abspath(root))
    parent = _directory(root.parent, private=False)
    try:
        try:
            os.mkdir(root.name, mode=0o700, dir_fd=parent)
        except FileExistsError:
            pass
    finally:
        os.close(parent)
    with _locked(root) as directory:
        try:
            os.stat('state.json', dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise ProtocolError('Hook spool already initialized; use a new connection directory')
        state = dict(schema_version=1, epoch=str(uuid.uuid4()), session_id=session,
                     cwd=cwd, next_sequence=1, faulted=False, events=[])
        _save(directory, state)
    return state['epoch']


def _safe_message(message):
    """Bound retained prose and redact transport URLs/credential-like strings."""
    raw = message.encode('utf-8')
    bounded = raw[:MAX_MESSAGE_BYTES].decode('utf-8', errors='ignore')
    bounded = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', bounded)
    bounded = ''.join(c for c in bounded if c in '\n\t' or (ord(c) >= 32 and ord(c) != 127))
    bounded = URL.sub('[URL redacted]', bounded)
    bounded = re.sub(r'\b(?:Bearer|Basic)\s+[A-Za-z0-9+/=._-]+', '[authorization redacted]',
                     bounded, flags=re.I)
    bounded = re.sub(r'\b(?:password|secret|token|api_key|authorization)\s*[:=]\s*'
                     r'(?:"[^"]*"|\x27[^\x27]*\x27|[^\s,;]+)', '[credential redacted]',
                     bounded, flags=re.I)
    encoded = bounded.encode('utf-8')
    return (encoded[:MAX_MESSAGE_BYTES].decode('utf-8', errors='ignore'),
            len(raw) > MAX_MESSAGE_BYTES or len(encoded) > MAX_MESSAGE_BYTES)


def _normalize(state, payload):
    if not isinstance(payload, dict):
        return None, 'invalid_input_shape'
    kind = payload.get('hook_event_name')
    if not isinstance(kind, str) or kind not in EVENTS:
        return None, 'unsupported_event'
    if payload.get('session_id') != state['session_id']:
        return None, 'session_mismatch'
    cwd = payload.get('cwd')
    if not isinstance(cwd, str) or not os.path.isabs(cwd):
        return None, 'cwd_mismatch'
    try:
        if not Path(cwd).resolve(strict=True).is_relative_to(Path(state['cwd']).resolve(strict=True)):
            return None, 'cwd_mismatch'
    except (OSError, ValueError, RuntimeError):
        return None, 'cwd_mismatch'
    if 'epoch' in payload and payload['epoch'] != state['epoch']:
        return None, 'epoch_mismatch'
    event = dict(event=kind, session_id=state['session_id'], cwd=state['cwd'])
    prompt = payload.get('prompt_id')
    if kind in ('UserPromptSubmit', 'Stop', 'StopFailure') and not _uuid(prompt):
        return None, 'missing_or_invalid_prompt_id'
    if prompt is not None:
        if not _uuid(prompt):
            return None, 'invalid_prompt_id'
        event['prompt_id'] = prompt
    if payload.get('agent_id'):
        return None, 'unexpected_subagent_event'
    if kind == 'UserPromptSubmit':
        text = payload.get('prompt')
        if not isinstance(text, str):
            return None, 'invalid_prompt'
        encoded = text.encode('utf-8')
        if len(encoded) > MAX_PROMPT_BYTES:
            return None, 'prompt_too_large'
        event['prompt_sha256'] = hashlib.sha256(encoded).hexdigest()
        # The interactive editor can normalize JSON whitespace. Retain a second
        # digest only for a complete JSON object; never retain or partially
        # extract its content. Duplicate keys/nonfinite values are ambiguous.
        def unique_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError('Duplicate JSON key')
                result[key] = value
            return result
        def reject_constant(value):
            raise ValueError('Nonfinite JSON value')
        try:
            structured = json.loads(text, object_pairs_hook=unique_object,
                                    parse_constant=reject_constant)
            if isinstance(structured, dict):
                canonical = json.dumps(structured, ensure_ascii=True, separators=(',', ':'),
                                       sort_keys=True, allow_nan=False).encode('utf-8')
                if len(canonical) <= MAX_PROMPT_BYTES:
                    event['prompt_json_sha256'] = hashlib.sha256(canonical).hexdigest()
        except (ValueError, TypeError, RecursionError):
            pass
    if kind == 'Stop':
        message = payload.get('last_assistant_message')
        if not isinstance(message, str):
            return None, 'missing_or_invalid_assistant_message'
        event['last_assistant_message'], event['message_truncated'] = _safe_message(message)
        event['stop_hook_active'] = payload.get('stop_hook_active') is True
        event['background_work_present'] = bool(payload.get('background_tasks') or payload.get('session_crons'))
    # Error details may contain credentials; retain only an identifier, never
    # StopFailure's rendered API error in last_assistant_message.
    for key in {'SessionStart': ('source', 'model'), 'ModelStatus': ('model',), 'StopFailure': ('error',),
                'SessionEnd': ('reason',), 'PostModelSwitch': ('from_model', 'to_model')}.get(kind, ()):
        value = payload.get(key)
        if value is not None:
            if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
                return None, 'invalid_' + key
            event[key] = value
    mode = payload.get('permission_mode')
    if mode is not None:
        if mode not in ('default', 'plan', 'acceptEdits', 'auto', 'dontAsk', 'bypassPermissions'):
            return None, 'invalid_permission_mode'
        event['permission_mode'] = mode
    effort = payload.get('effort')
    if isinstance(effort, dict) and effort.get('level') in ('low', 'medium', 'high', 'xhigh', 'max'):
        event['effort'] = dict(level=effort['level'])
    return event, None


def record_model_status(root, payload):
    """Consume official statusLine metadata, not terminal text or model prose.

    Keep only this session's model/effort. Repeated identical UI refreshes are
    coalesced; distinct transitions remain ordered with the completion hooks.
    Nothing here authorizes a tool or acknowledges a submitted request.
    """
    if not isinstance(payload, dict):
        return record_event(root, None, input_error='invalid_model_status')
    model = payload.get('model')
    model = model.get('id') if isinstance(model, dict) else None
    if not isinstance(model, str) or not IDENTIFIER.fullmatch(model):
        return record_event(root, None, input_error='invalid_status_model')
    return record_event(root, dict(hook_event_name='ModelStatus',
        session_id=payload.get('session_id'), cwd=payload.get('cwd'), model=model,
        effort=payload.get('effort')))


def record_event(root, payload, *, input_error=None):
    """Durably record one allowlisted event, or a latched protocol error.

    The returned record contains no submitted prompt. Once faulted, later hooks
    cannot manufacture completion records. Overflow discards pending records in
    favor of one explicit error, so a queued Stop cannot hide the overflow.
    """
    with _locked(root) as directory:
        state = _load(directory)
        if state['faulted']:
            return None
        try:
            event, reason = _normalize(state, payload) if input_error is None else (None, input_error)
        except (UnicodeError, TypeError, ValueError):
            event, reason = None, 'invalid_input_encoding'
        if not reason and event['event'] == 'ModelStatus':
            previous = state.get('last_model_status', {})
            same = all(previous.get(k) == event.get(k) for k in ('model', 'effort'))
            state['last_model_status'] = {**event, 'observed_unix_s': time.time()}
            if same:
                _save(directory, state)
                return event
        if len(state['events']) >= MAX_EVENTS:
            reason = 'spool_overflow'
            state['events'] = []
        if reason:
            kind = payload.get('hook_event_name') if isinstance(payload, dict) else None
            event = dict(event='protocol_error', reason=reason,
                         source_event=kind if isinstance(kind, str) and kind in EVENTS else None,
                         session_id=state['session_id'], cwd=state['cwd'])
            state['faulted'] = True
            state['last_protocol_error'] = dict(reason=reason, source_event=event['source_event'])
        event.update(epoch=state['epoch'], sequence=state['next_sequence'])
        state['next_sequence'] += 1
        state['events'].append(event)
        _save(directory, state)
        return event


def read_events(root):
    """Consume queued records exactly once under the same writer lock.

    Callers must inspect the whole batch for protocol_error before accepting a
    completion. Failure after this consumption is an unknown turn, not replay.
    """
    with _locked(root) as directory:
        state = _load(directory)
        events = state['events']
        if events:
            state['events'] = []
            _save(directory, state)
        return events


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    model_status = len(args) == 2 and args[1] == '--model-status'
    if len(args) != 1 and not model_status:
        print('Interactive hook requires one private spool directory', file=sys.stderr)
        return 1
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        if len(raw) > MAX_INPUT_BYTES:
            record_event(args[0], None, input_error='input_too_large')
        else:
            try:
                payload = json.loads(raw)
            except (ValueError, UnicodeError, RecursionError):
                record_event(args[0], None, input_error='invalid_input_json')
            else:
                if model_status:
                    record_model_status(args[0], payload)
                else:
                    record_event(args[0], payload)
        return 0
    except (OSError, ValueError, ProtocolError):
        # Never expose arbitrary input, filesystem paths, or authentication text.
        print('Interactive hook spool unavailable', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
