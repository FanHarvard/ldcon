"""Persist empirical branch maps independently of job-log retention.

No map grants permission to start/continue a lock or change a target. Fixed
legacy frequency bands are annotations, never substitute sweep observations.
"""
from pathlib import Path

from ldcon.jobs.ecdl_mode_map import SCHEMA, checked_artifact
from ldcon.core.records import safe_id


def load_for_job(directory, laser):
    """Read retained evidence from this laser only; never create a map or scan.

    Invalid/legacy artifacts remain in the result so the matcher can report why
    they cannot identify a measured branch. Directory and file links are refused.
    """
    import json
    import os
    import stat
    from ldcon.core.laser_assets import _directory
    from ldcon.core.state_layout import asset_parts
    directory = Path(directory)
    laser_id = safe_id(laser['id'])
    root = directory.parent.parent if directory.parent.name == 'jobs' else directory.parent.parent.parent
    parts = (*asset_parts(root, laser_id), 'data', 'branch-maps')
    path = root.joinpath(*parts)
    if not path.exists():
        return []
    result, consumed = [], 0
    with _directory(root, parts) as parent:
        names = sorted(name for name in os.listdir(parent) if name.endswith('.json'))
        if len(names) > 1000:
            raise ValueError('Retained branch-map inventory exceeds its read budget')
        for name in names:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
            with os.fdopen(fd, 'rb') as stream:
                info = os.fstat(stream.fileno())
                consumed += info.st_size
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or consumed > 128 * 1024 * 1024:
                    raise ValueError('Retained branch maps must be bounded regular files')
                try:
                    result.append(json.load(stream))
                except (ValueError, UnicodeError):
                    # A malformed saved record supplies no branch evidence.
                    result.append(dict(schema='invalid-branch-map', source_name=name))
    return result


def record(directory, laser, artifact):
    """Checkpoint one map under its job and its laser, without editing configuration."""
    from ldcon.core.laser_assets import layout, _directory
    from ldcon.core.state_layout import asset_parts
    from ldcon.core.result_reservations import durable_json
    from ldcon.core.interface_schema import digest
    directory = Path(directory)
    job_id, laser_id = safe_id(directory.name), safe_id(laser['id'])
    root = directory.parent.parent if directory.parent.name == 'jobs' else directory.parent.parent.parent
    value = checked_artifact(artifact, SCHEMA)
    context = dict(laser_id=laser_id, job_id=job_id, laser_configuration_sha256=digest(laser))
    if value.get('context') != context or value.get('lock_admission') is not False:
        raise ValueError('Branch map must name this job, laser and frozen configuration')
    # Reuse the single-laser layout's no-symlink directory checks. Definitions
    # and credentials stay out of the map; configuration identity is a digest.
    paths = layout(root, laser_id, create=True)
    parts = (*asset_parts(root, laser_id), 'data', 'branch-maps')
    with _directory(root, parts, create=True):
        path = Path(paths['data']) / 'branch-maps' / (job_id + '.json')
        durable_json(path, value)
    durable_json(directory / 'mode-map.json', value)
    return dict(path=str(path), artifact_sha256=value['artifact_sha256'], status=value.get('status'))
