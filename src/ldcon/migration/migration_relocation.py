"""Prepare inert full-relocation snapshots without controlling a running service.

This deliberately has no cutover operation. Live snapshots are evidence, not a
replacement for an owner-specific held-output handoff and a final stopped sync.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import stat
import tempfile
import time

import yaml

MODE = 'full-relocation-preparation-only'
KINDS = {'state', 'history', 'assets', 'scripts', 'configuration', 'owner_config'}
ARCHIVE_KINDS = {'state', 'history'}
LAUNCH_MARKERS = {'launch.request', 'launch.claim', 'cancel.request', 'worker-process.json'}


def _absolute(value):
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('Migration paths must be absolute and normalized')
    for parent in [path, *path.parents]:
        if parent.is_symlink():
            raise ValueError('Migration paths cannot traverse symlinks: ' + str(parent))
    return path


def _relative(value):
    path = PurePosixPath(value)
    if not value or path.is_absolute() or any(part in ('..', '.') for part in value.split('/')):
        raise ValueError('Artifact destination must be a normalized relative path')
    return path


def _hash(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def _save(path, value):
    path = Path(path)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    path.chmod(0o660)


def _pointer(value, pointer, replacement=None, *, replace=False):
    if not isinstance(pointer, str) or not pointer.startswith('/'):
        raise ValueError('Configuration path fields require explicit JSON pointers')
    parts = [part.replace('~1', '/').replace('~0', '~') for part in pointer[1:].split('/')]
    parent = value
    for part in parts[:-1]:
        parent = parent[int(part)] if isinstance(parent, list) else parent[part]
    key = int(parts[-1]) if isinstance(parent, list) else parts[-1]
    original = parent[key]
    if replace:
        if not isinstance(original, str):
            raise ValueError('Only an existing string path field may be relocated')
        _absolute(replacement)
        parent[key] = replacement
    return original


def _configuration(path):
    if path.suffix not in ('.json', '.yaml', '.yml'):
        raise ValueError('Operational configuration must be JSON or YAML: ' + str(path))
    raw = path.read_bytes()
    value = json.loads(raw) if path.suffix == '.json' else yaml.safe_load(raw)
    return value, hashlib.sha256(raw).hexdigest()


def _validate_request(request, output):
    if request.get('schema_version') != 1 or request.get('mode') != MODE:
        raise ValueError('Expected version 1 explicit preparation-only relocation request')
    target = _absolute(request['target_state_root'])
    release = _absolute(request['target_release_root'])
    if not str(target).startswith('/opt/') or not str(release).startswith('/opt/'):
        raise ValueError('Full relocation targets must be under /opt')
    output = _absolute(output)
    if request.get('path_mappings'):
        raise ValueError('Use explicit per-source path_rewrites JSON pointers, never global path replacement')
    destinations = []
    sources = []
    for item in request['sources']:
        kind = item['kind']
        if kind not in KINDS:
            raise ValueError('Unknown relocation artifact kind')
        source = _absolute(item['source'])
        if not source.exists():
            raise ValueError('Relocation source does not exist: ' + str(source))
        if source == output or source in output.parents or output in source.parents:
            raise ValueError('Preparation output and source must not overlap')
        destination = _relative(item['destination'])
        if any(destination == old or old in destination.parents or destination in old.parents for old in destinations):
            raise ValueError('Relocation artifact destinations overlap')
        if kind in ARCHIVE_KINDS and destination.parts[0] != 'migration-snapshots':
            raise ValueError('Live state/history must remain inert under migration-snapshots')
        if kind not in ARCHIVE_KINDS and set(destination.parts) & {'jobs', 'agents', 'notifications', 'reservations'}:
            raise ValueError('Operational runtime queues cannot be seeded from live copies')
        destinations.append(destination)
        sources.append({**item, 'source': str(source), 'destination': str(destination)})
    return output, target, release, sources


def _sqlite_backup(source, destination):
    # SQLite's read-only online backup includes committed WAL pages. Copying only
    # the .sqlite3 file can silently omit committed requests and failure cases.
    deadline = time.monotonic() + 30
    def progress(*unused):
        if time.monotonic() > deadline:
            raise TimeoutError('Consistent SQLite snapshot did not finish within 30 seconds')
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=5)) as incoming:
        with closing(sqlite3.connect(destination)) as outgoing:
            incoming.backup(outgoing, pages=256, progress=progress, sleep=.02)
            outgoing.execute('PRAGMA journal_mode=DELETE')
            if outgoing.execute('PRAGMA quick_check').fetchone() != ('ok',):
                raise ValueError('SQLite snapshot integrity check failed')


def _copy(source, destination, spec):
    kind = spec['kind']
    before = source.stat()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open('rb') as stream:
        sqlite = stream.read(16) == b'SQLite format 3\x00'
    method = 'copy'
    references = []
    source_config_sha256 = None
    if kind in ('configuration', 'owner_config'):
        value, source_config_sha256 = _configuration(source)
        rewrites = spec.get('path_rewrites', {})
        for pointer, replacement in rewrites.items():
            _pointer(value, pointer, replacement, replace=True)
        for pointer in sorted(set(rewrites) | set(spec.get('reference_pointers', []))):
            item = _pointer(value, pointer)
            if not isinstance(item, str) or not item.startswith('/'):
                raise ValueError('Runtime path reference is not an absolute path at ' + pointer)
            references.append({'pointer': pointer, 'path': item})
        content = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
                   if source.suffix == '.json' else yaml.safe_dump(value, allow_unicode=True, sort_keys=False))
        destination.write_text(content)
        method = 'configuration_path_rewrite'
    elif sqlite:
        _sqlite_backup(source, destination)
        method = 'sqlite_online_backup'
    else:
        shutil.copyfile(source, destination)
    after = source.stat()
    changed = (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino)
    if changed and kind in ('configuration', 'owner_config'):
        raise ValueError('Operational configuration changed during snapshot: ' + str(source))
    destination.chmod(0o770 if kind == 'scripts' and before.st_mode & 0o111 else 0o660)
    return {'source': str(source), 'method': method, 'sha256': _hash(destination),
            'bytes': destination.stat().st_size, 'source_changed_during_copy': changed,
            'source_mtime_ns': after.st_mtime_ns, 'references': references,
            'source_config_sha256': source_config_sha256}


def prepare_relocation(request, output, *, apply=False):
    """Inventory or stage explicit copies. Never install, stop, enable or start.

    ``output/state`` mirrors ``target_state_root``. State/history sources always
    live under ``migration-snapshots`` so old launch requests cannot be consumed.
    Operational converted decks can subsequently be added by the conversion
    step; this manifest covers only the files this function writes.
    """
    output, target, release, sources = _validate_request(request, output)
    records = []
    skipped = []
    directories = []
    for spec in sources:
        source = Path(spec['source'])
        paths = sorted(source.rglob('*')) if source.is_dir() else [source]
        if source.is_dir():
            directories.append(spec['destination'])
        for path in paths:
            if path.is_symlink():
                raise ValueError('Source artifact contains a symlink: ' + str(path))
            relative = path.relative_to(source) if source.is_dir() else Path()
            destination = str(PurePosixPath(spec['destination']) / relative)
            mode = path.stat().st_mode
            if stat.S_ISDIR(mode):
                directories.append(destination)
                continue
            if not stat.S_ISREG(mode):
                skipped.append({'source': str(path), 'reason': 'runtime_special_file_not_copied'})
                continue
            if path.name.endswith(('-wal', '-shm', '-journal')):
                base = path.with_name(path.name.rsplit('-', 1)[0])
                if base.is_file():
                    with base.open('rb') as stream:
                        if stream.read(16) == b'SQLite format 3\x00':
                            skipped.append({'source': str(path), 'reason': 'included_via_sqlite_online_backup'})
                            continue
            if spec['kind'] not in ARCHIVE_KINDS and path.name in LAUNCH_MARKERS:
                raise ValueError('Live launch/process marker cannot enter operational state')
            records.append({'path': destination, 'source': str(path), 'kind': spec['kind'],
                            'bytes': path.stat().st_size,
                            'source_spec': sources.index(spec)})
    manifest = {'schema_version': 1, 'mode': MODE, 'created_utc': datetime.now(timezone.utc).isoformat(),
                'applied': False, 'cutover_allowed': False, 'final_resync_required': True,
                'target_state_root': str(target), 'target_release_root': str(release),
                'staged_state': str(output / 'state'), 'sources': sources,
                'owner_handoffs': request.get('owner_handoffs', []), 'skipped': skipped,
                'files': records, 'file_count': len(records),
                'estimated_source_bytes': sum(record['bytes'] for record in records),
                'deployments': request.get('deployments', []),
                'request_sha256': hashlib.sha256(json.dumps(request, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
                'blockers': ['owner-specific held-output handoff has not been executed',
                             'live source snapshots require final synchronization after the authorized stop',
                             'historical runtime queues and process identities are not activation state'] + list(request.get('readiness_blockers', []))}
    if not apply:
        return manifest
    if (output / 'state').exists() or (output / 'relocation-manifest.json').exists():
        raise ValueError('Preparation output already contains a relocation snapshot')
    output.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix='.relocation-', dir=output))
    try:
        for directory in directories:
            (temporary / directory).mkdir(parents=True, exist_ok=True)
        for record in records:
            record.update(_copy(Path(record['source']), temporary / record['path'], sources[record['source_spec']]))
        for directory in [temporary, *(path for path in temporary.rglob('*') if path.is_dir())]:
            directory.chmod(0o2770)
        temporary.rename(output / 'state')
        manifest['applied'] = True
        _save(output / 'relocation-manifest.json', manifest)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return manifest


def dry_run_prepared(manifest_path):
    """Verify the snapshot and references, without any network or service action."""
    manifest_path = _absolute(manifest_path)
    manifest = json.loads(manifest_path.read_text())
    if (manifest.get('mode') != MODE or manifest.get('schema_version') != 1
            or manifest.get('cutover_allowed') is not False or not manifest.get('applied')):
        raise ValueError('Not an applied preparation-only relocation manifest')
    stage = _absolute(manifest['staged_state'])
    if stage != manifest_path.parent / 'state':
        raise ValueError('Manifest stage is outside its preparation directory')
    target = _absolute(manifest['target_state_root'])
    errors = []
    missing = []
    checked = 0
    for record in manifest['files']:
        relative = _relative(record['path'])
        path = _absolute(stage / relative)
        if not path.is_file() or _hash(path) != record['sha256']:
            errors.append({'path': record['path'], 'reason': 'snapshot hash mismatch or absent'})
            continue
        checked += 1
        if record['kind'] not in ARCHIVE_KINDS and path.name in LAUNCH_MARKERS:
            errors.append({'path': record['path'], 'reason': 'operational launch marker'})
        for reference in record.get('references', []):
            value = Path(reference['path'])
            if value == target or target in value.parents:
                local = stage / value.relative_to(target)
                if not local.exists():
                    # Report location, never a configuration value or credential.
                    missing.append({'path': record['path'], 'pointer': reference['pointer']})
    for path in stage.rglob('*'):
        if path.is_symlink():
            errors.append({'path': str(path.relative_to(stage)), 'reason': 'symlink in prepared state'})
        if path.name in LAUNCH_MARKERS and path.relative_to(stage).parts[0] != 'migration-snapshots':
            errors.append({'path': str(path.relative_to(stage)), 'reason': 'operational launch marker'})
    return {'mode': MODE, 'snapshot_integrity_ok': not errors, 'files_verified': checked,
            'missing_staged_references': missing, 'errors': errors, 'cutover_allowed': False,
            'final_resync_required': True, 'readiness_blockers': list(manifest.get('blockers', [])),
            'services_changed': False, 'hardware_commands_sent': False}
