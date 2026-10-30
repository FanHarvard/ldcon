"""Laser-specific proposal advice, separate from owner safety and trial grants."""
from ldcon.core.state_layout import asset_parts, root_from_laser_file

from experiment_core.adjustment_guidance import validate as validate_document, snapshot

# Units describe proposals, not a promise that an owner supports every knob.
UNITS = dict(current='V', temperature='V', piezo_center_dac='count',
             kp_count_per_nm='count/nm', ki_count_per_nm_s='count/(nm*s)',
             integral_limit_count='count', p_sample='1', g_sample='1',
             tec_gain='1', tec_dt_cap_s='s', supervision_interval_s='s',
             current_per_temperature='V/V', target_ff_scale='1', target_step_hz='Hz')


def validate(value):
    value = validate_document(value)
    for knob, item in value['knobs'].items():
        if knob not in UNITS:
            raise ValueError('Unknown laser adjustment guidance knob: '+knob)
        if item['kind'] == 'absolute' and item['unit'] != UNITS[knob]:
            raise ValueError('Incorrect physical unit for guidance knob: '+knob)
    return value


def location(root, laser_id):
    from pathlib import Path
    from experiment_core.records import safe_id
    path=Path(root).joinpath(*asset_parts(root, safe_id(laser_id)))/'data'/'adjustment-guidance.json'
    if any(p.is_symlink() for p in (path,*path.parents)):
        raise ValueError('Adjustment guidance must not use symlinks')
    return path


def read(root, laser_id):
    import json
    path=location(root,laser_id)
    raw=path.read_bytes() if path.exists() else b'{"schema_version":1,"knobs":{}}'
    if len(raw)>65536:raise ValueError('Adjustment guidance is too large')
    value=validate(json.loads(raw))
    return dict(snapshot(value),laser_id=laser_id,
                availability='configured' if value['knobs'] else 'not_configured')


def current(runtime, laser_id):
    # Establish scope, then read advice independently of the physical policy.
    runtime.interface_repository.read('lasers',laser_id)
    return read(runtime.root,laser_id)


def for_job(directory, laser_id):
    from ldcon.jobs.laser_live import laser_file
    return read(root_from_laser_file(laser_file(directory,laser_id)),laser_id)


def update(runtime, laser_id, data):
    import fcntl
    import os
    from experiment_core.records import atomic_json
    from ldcon.web.web_runtime import ConflictError
    if not isinstance(data,dict) or set(data)!={'expected_revision','document'}:
        raise ValueError('Expected revision and an advisory document')
    document=validate(data['document'])
    if len(__import__('json').dumps(document).encode())>65536:
        raise ValueError('Adjustment guidance is too large')
    with runtime.mutex:
        runtime.interface_repository.read('lasers',laser_id)
        path=location(runtime.root,laser_id);path.parent.mkdir(parents=True,exist_ok=True)
        descriptor=os.open(path.with_suffix('.lock'),os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        with os.fdopen(descriptor,'a') as stream:
            fcntl.flock(stream,fcntl.LOCK_EX)
            if current(runtime,laser_id)['revision']!=data['expected_revision']:
                raise ConflictError('Adjustment advice changed; read it before saving')
            atomic_json(path,document)
        return current(runtime,laser_id)
