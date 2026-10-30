"""Durable DLC target receipts and scalar-only, revision-checked target saves."""
from contextlib import contextmanager, nullcontext
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile

from ldcon.core.interface_schema import load_mapping
from ldcon.jobs.lock_target import edit_target_text


def revision(raw):
    return hashlib.sha256(raw).hexdigest()


@contextmanager
def target_file_guard(path, *, blocking=True):
    """Shared with normal configuration saves; lock is beside the trusted file."""
    path = Path(path)
    with path.with_name('.' + path.name + '.target.lock').open('a+b') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        yield


def configuration_guard(runtime, kind, name):
    """Use alongside runtime.mutex in configuration.save (DLC and other lasers)."""
    from ldcon.core.state_layout import config_file
    from ldcon.core.records import safe_id
    return target_file_guard(config_file(runtime.root, kind, safe_id(name))) if kind == 'lasers' else nullcontext()


def atomic_bytes(path, raw, *, expected=None):
    path = Path(path)
    descriptor, name = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            os.fchmod(stream.fileno(), 0o660)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if expected is not None and revision(path.read_bytes()) != expected:
            raise ValueError('Laser configuration changed before publication')
        os.replace(name, path)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_laser(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError('Trusted laser file must be a regular file')
    raw = path.read_bytes()
    return load_mapping(raw.decode()), revision(raw)


def save_target(path, expected, target):
    """Caller holds target_file_guard from prewrite checks through this save."""
    raw = Path(path).read_bytes()
    if revision(raw) != expected:
        raise ValueError('Laser configuration changed after target acceptance')
    edited = edit_target_text(raw.decode(), target).encode()
    atomic_bytes(path, edited, expected=expected)
    return revision(edited)


def write_record(path, value):
    atomic_bytes(path, (json.dumps(value, allow_nan=False, sort_keys=True) + '\n').encode())
