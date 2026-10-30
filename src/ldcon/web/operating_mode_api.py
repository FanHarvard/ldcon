"""Dashboard operator changes, read-only mode visibility for every caller."""
import asyncio
import time

from fastapi import HTTPException, Request
from experiment_core.records import atomic_json, digest, safe_id

from ldcon.agents.agent_background import control_root
from ldcon.agents.operating_mode_client import request as owner_request
from ldcon.core.records import read_json


def change(runtime, laser_id, data):
    from experiment_core.operating_mode import validate_change_request
    required = {'request_id', 'expected_revision', 'desired_mode', 'reason'}
    if not isinstance(data, dict) or set(data) != required:
        raise ValueError('Expected request_id, expected_revision, desired_mode and reason')
    safe_id(laser_id); safe_id(data['request_id'])
    with runtime.mutex:
        runtime.assert_admission_open()
        laser = runtime.interface_repository.read('lasers', laser_id)['document']
        directory = control_root(runtime.root)/laser_id/'operating-mode'/'requests'
        for parent in (directory, *directory.parents):
            if parent.is_symlink():raise ValueError('Operating mode request path must not contain symlinks')
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory/(data['request_id']+'.json')
        if path.is_symlink():raise ValueError('Operating mode request must not be a symlink')
        old = read_json(path, {})
        value = dict(schema_version=1, laser_id=laser_id, **data,
                     authenticated_operator='dashboard-operator', issued_unix_s=time.time())
        validate_change_request(value)
        if old:
            if any(old.get(k) != value[k] for k in required | {'laser_id'}):
                raise ValueError('Operating mode request ID already binds different content')
            value = old
        else:
            atomic_json(path, value)
        # Retry is the same durable request ID; only the owner decides its outcome.
        return owner_request(laser, 'operating_mode_change', request_id=value['request_id'],
                             request_digest=digest(value))


def install_routes(app, prefix, body):
    from ldcon.web.control_readback import ControlReadback
    readback = ControlReadback()

    @app.get(prefix+'/api/lasers/{laser_id}/control-readback')
    def control_readback(laser_id: str):
        laser = app.state.runtime.interface_repository.read('lasers', safe_id(laser_id))['document']
        return readback.read(laser)

    @app.get(prefix+'/api/lasers/{laser_id}/operating-mode')
    def status(laser_id: str):
        laser = app.state.runtime.interface_repository.read('lasers', safe_id(laser_id))['document']
        try:return owner_request(laser)
        except (OSError, ValueError, KeyError, TypeError):
            raise HTTPException(503, 'Owner operating mode is unavailable; check owner connectivity and release. No mode change is permitted.') from None

    @app.get(prefix+'/api/lasers/{laser_id}/adjustment-guidance')
    def guidance(laser_id: str):
        from ldcon.agents.adjustment_guidance import current
        return current(app.state.runtime, safe_id(laser_id))

    @app.put(prefix+'/api/lasers/{laser_id}/adjustment-guidance')
    async def save_guidance(laser_id: str, request: Request):
        from ldcon.agents.adjustment_guidance import update
        return await asyncio.to_thread(update, app.state.runtime, safe_id(laser_id), await body(request))

    @app.put(prefix+'/api/lasers/{laser_id}/operating-mode')
    async def update(laser_id: str, request: Request):
        # Uses the same capability URL and same-origin client gate as other Web operations.
        return await asyncio.to_thread(change, app.state.runtime, laser_id, await body(request))
