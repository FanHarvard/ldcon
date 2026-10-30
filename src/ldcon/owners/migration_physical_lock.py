"""Prepared-runtime exclusion across a service-account handoff.

This module never creates, replaces, chmods, truncates, or unlinks lock files.
The migration manifest pins both the former owner's inode and a shared inode.
It is deliberately mandatory in the patched, inactive owner runtimes.
"""

import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat


class PhysicalLockSet:
    def __init__(self):
        self.streams = []

    @property
    def closed(self):
        return not self.streams or all(stream.closed for stream in self.streams)

    def close(self):
        for stream in reversed(self.streams):
            stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def _safe_path(path):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('Lock paths must be absolute without traversal')
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError('A lock path or parent is a symlink')
    return path


def file_identity(path):
    """Read metadata only; suitable for preparation without acquiring a lock."""
    path = _safe_path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError('Lock must be a regular file with exactly one link')
    return {'path': str(path), 'device': info.st_dev, 'inode': info.st_ino}


def legacy_paths(identity, uid, kinds):
    if type(uid) is not int or uid < 0:
        raise ValueError('Explicit legacy UID is required')
    if identity.startswith('tcp:'):
        endpoint = identity[4:]
        physical = hashlib.sha256(endpoint.encode()).hexdigest()[:24]
        device = hashlib.sha256(identity.encode()).hexdigest()
        choices = {'physical': f'/tmp/ldcon-physical-{physical}.lock',
                   'device': f'/tmp/ldcon-device-{uid}-{device}.lock'}
    elif identity.startswith('visa:'):
        key = hashlib.sha256(identity[5:].encode()).hexdigest()[:24]
        choices = {'visa': f'/tmp/ldcon-cld1015-{uid}-{key}.lock'}
    elif identity == 'broker':
        choices = {'broker': f'/tmp/ldcon-instruments-{uid}.lock'}
    else:
        raise ValueError('Unknown physical identity')
    try:
        return [choices[kind] for kind in kinds]
    except KeyError:
        raise ValueError('Legacy lock kind does not match physical identity') from None


def canonical_path(identity, directory):
    return str(Path(directory)/(hashlib.sha256(identity.encode()).hexdigest()+'.lock'))


def _manifest():
    name = os.environ.get('LDCON_PHYSICAL_LOCK_MANIFEST')
    if not name:
        raise RuntimeError('Prepared owner requires LDCON_PHYSICAL_LOCK_MANIFEST')
    path = _safe_path(name)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
            raise ValueError('Invalid physical lock manifest')
        value = json.load(stream)
    if value.get('schema_version') != 1 or value.get('ready') is not True:
        raise ValueError('Physical lock manifest is not finalized')
    return value


def acquire(identity, required_kinds):
    manifest = _manifest()
    required = set(legacy_paths(identity, manifest.get('legacy_uid'), required_kinds))
    target_kinds = tuple(kind for kind in required_kinds if kind != 'physical')
    target_required = set(legacy_paths(identity, manifest.get('target_uid'), target_kinds))
    matches = [row for row in manifest.get('locks', []) if row.get('identity') == identity]
    if len(matches) != 1:
        raise ValueError('Physical identity is missing or duplicated in manifest')
    row = matches[0]
    old = row.get('legacy', [])
    target = row.get('target_legacy', [])
    if not required.issubset({item.get('path') for item in old}):
        raise ValueError('Manifest omits a required legacy exclusion domain')
    if not target_required.issubset({item.get('path') for item in target}):
        raise ValueError('Manifest omits a target-UID compatibility exclusion domain')
    # Reject arbitrary aliases: only paths of known legacy implementations are valid.
    kinds = ('physical', 'device') if identity.startswith('tcp:') else (required_kinds[0],)
    allowed = set(legacy_paths(identity, manifest['legacy_uid'], kinds))
    target_allowed = set(legacy_paths(identity, manifest['target_uid'], tuple(kind for kind in kinds if kind != 'physical')))
    if not {item.get('path') for item in old}.issubset(allowed):
        raise ValueError('Unexpected legacy exclusion path')
    if not {item.get('path') for item in target}.issubset(target_allowed):
        raise ValueError('Unexpected target-UID compatibility exclusion path')
    canonical = row.get('canonical', {})
    expected = canonical_path(identity, manifest['canonical_directory'])
    if canonical.get('path') != expected or canonical.get('path') in (allowed | target_allowed):
        raise ValueError('Canonical lock path does not match physical identity')
    records = [canonical, *sorted([*old, *target], key=lambda item: item['path'])]
    result = PhysicalLockSet()
    seen = set()
    try:
        for record in records:
            path = _safe_path(record['path'])
            before = file_identity(path)
            if before != {key: record.get(key) for key in ('path', 'device', 'inode')}:
                raise RuntimeError('Physical lock inode changed; reviewed handoff required')
            key = (before['device'], before['inode'])
            if key in seen:
                raise ValueError('Duplicate physical lock inode')
            seen.add(key)
            fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
            stream = os.fdopen(fd, 'r+')
            result.streams.append(stream)
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino) != key or opened.st_nlink != 1 or not stat.S_ISREG(opened.st_mode):
                raise RuntimeError('Physical lock changed while opening')
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if file_identity(path) != before:
                raise RuntimeError('Physical lock changed while acquiring')
    except BaseException:
        result.close()
        raise
    return result


def acquire_endpoint(address, port, required_kinds=('physical',)):
    return acquire(f'tcp:{address}:{int(port)}', required_kinds)


def acquire_visa(resource):
    if not resource or resource == 'auto':
        raise ValueError('Physical ownership requires a pinned VISA resource')
    return acquire('visa:'+resource, ('visa',))


def acquire_broker():
    return acquire('broker', ('broker',))
