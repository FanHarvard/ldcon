"""Explicit single-laser storage, with read/write compatibility for legacy roots.

A bounded marker selects the layout; directory names never imply authority.
New deployments keep one laser's data under one root. Owners remain independent
processes and shared instrument state is not part of this layout.
"""
import json
import os
from pathlib import Path
import re
import stat

MARKER = 'laser-layout.json'
SCHEMA = 'ldcon.single-laser-layout.v1'


def single_laser(root, laser_id=None):
    path = Path(root) / MARKER
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 4096:
            raise ValueError('Invalid laser layout marker')
        value = json.load(stream)
    if (not isinstance(value, dict) or set(value) != {'schema', 'laser_id'}
            or value['schema'] != SCHEMA or not isinstance(value['laser_id'], str)
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', value['laser_id'])):
        raise ValueError('Invalid laser layout marker')
    if laser_id is not None and value['laser_id'] != laser_id:
        raise ValueError('Laser does not belong to this state root')
    return value['laser_id']


def config_root(root):
    return Path(root) / 'config' if single_laser(root) else Path(root)


def config_file(root, kind, name):
    if kind in ('lasers', 'assignments'):
        single_laser(root, name)
    return config_root(root) / kind / (name + '.yaml')


def asset_parts(root, laser_id):
    return () if single_laser(root, laser_id) else ('laser-assets', laser_id)


def workspace_parts(root, laser_id):
    return ('agents', 'workspace') if single_laser(root, laser_id) else (*asset_parts(root, laser_id), 'agent')


def job_root(root, laser_id):
    base = Path(root) / 'jobs'
    return base if single_laser(root, laser_id) else base / laser_id


def knowledge_root(root, laser_id):
    base = Path(root) / 'agents' / 'knowledge'
    return base if single_laser(root, laser_id) else base / laser_id


def root_from_laser_file(path):
    path = Path(path)
    old = path.parent.parent
    if old.name == 'config' and single_laser(old.parent, path.stem):
        return old.parent
    return old
