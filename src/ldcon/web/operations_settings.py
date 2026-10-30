"""Shared, revision-checked operational defaults; never device configuration."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile

BUILTIN = dict(enabled=False, retention_days=30, backup_directory='')
ENVIRONMENT = 'LDCON_OPERATION_DEFAULTS'


def validate_retention(value, roots=(), *, check_paths=True):
    if not isinstance(value, dict) or set(value) != set(BUILTIN):
        raise ValueError('Expected enabled, retention_days and backup_directory')
    days, enabled, folder = value['retention_days'], value['enabled'], value['backup_directory']
    if type(days) is not int or not 1 <= days <= 36500:
        raise ValueError('Retention must be 1..36500 days')
    if type(enabled) is not bool or not isinstance(folder, str):
        raise ValueError('Invalid retention settings')
    if check_paths and (enabled or folder):
        path = Path(folder)
        if not path.is_absolute() or not path.is_dir():
            raise ValueError('Backup must be an existing absolute directory')
        path = path.resolve()
        for root in roots:
            root = Path(root).resolve()
            if path == root or root in path.parents or path in root.parents:
                raise ValueError('Backup must be separate from every state directory')
        if not os.access(path, os.W_OK):
            raise ValueError('Service user cannot write the backup directory')
        folder = str(path)
    return dict(enabled=enabled, retention_days=days, backup_directory=folder)


def read(path):
    path = Path(path)
    if not path.exists():
        return dict(retention=dict(BUILTIN), revision=None)
    raw = path.read_bytes()
    if len(raw) > 16384:
        raise ValueError('Operational defaults file is too large')
    value = json.loads(raw)
    if not isinstance(value, dict) or set(value) != {'retention'}:
        raise ValueError('Invalid operational defaults file')
    # Paths are revalidated by the archiver against its own state directory.
    return dict(retention=validate_retention(value['retention'], check_paths=False),
                revision=hashlib.sha256(raw).hexdigest())


def save(path, payload, roots=()):
    if not isinstance(payload, dict) or set(payload) != {'retention', 'expected_revision'}:
        raise ValueError('Expected retention and expected_revision')
    value = validate_retention(payload['retention'], roots)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # The lock serializes only this settings file, never jobs or instrument owners.
    with path.with_suffix(path.suffix + '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = read(path)
        if current['revision'] != payload['expected_revision']:
            from ldcon.web.web_runtime import ConflictError
            raise ConflictError('Defaults changed. Reload before saving.')
        fd, name = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
        try:
            with os.fdopen(fd, 'w') as stream:
                os.fchmod(stream.fileno(), 0o660)
                json.dump(dict(retention=value), stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, path)
        finally:
            Path(name).unlink(missing_ok=True)
    return read(path)


def shared(runtime=None):
    value = os.environ.get(ENVIRONMENT)
    if value:
        return Path(value)
    # Installed sibling deployment layout: registering a new service is enough.
    # Standalone/test state directories do not implicitly use a host-wide file.
    if runtime is not None:
        root = runtime.root.resolve()
        if root.parent.name in ('deployments', 'lasers'):
            dashboard = root.parent.parent/'dashboard'
            registry = dashboard/'services.json'
            if registry.is_file():
                records = json.loads(registry.read_text()).get('services', [])
                if any(Path(row['state_file']).parent.resolve() == root for row in records):
                    return dashboard/'operations-defaults.json'
    return None
