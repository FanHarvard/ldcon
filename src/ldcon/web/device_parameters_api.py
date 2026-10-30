"""Dashboard routes for device parameters (rule I, 2026-10-22): the catalog and a one-shot change.

    GET  /api/lasers/{laser}/device-parameters[?refresh=true]     -> {instruments: {id: catalog}} for this laser
    POST /api/lasers/{laser}/device-parameters  {changes, approval_id?, dry_run?, request_note?}

The POST runs the SAME ``device_parameters.change`` as the ``tune.device_parameters`` card (plan -> authority ->
registry -> owners), with this laser's approval ledger. It does not hold the runtime mutex (the laser-definition
backend saves through this dashboard's own configuration API). Hardware is only reached through the owners.
"""
import asyncio

from fastapi import HTTPException, Request

from ldcon.core.agent_authority import ApprovalError
from ldcon.core.contracts import DeviceError
from ldcon.core.interface_schema import identifier


def install_routes(app, prefix, body, actor):
    def laser(laser_id):
        runtime = app.state.runtime
        return runtime.interface_repository.read('lasers', identifier(laser_id))['document']

    @app.get(prefix + '/api/lasers/{laser_id}/device-parameters')
    async def catalog(laser_id: str, refresh: str = 'false'):
        from ldcon.jobs.device_parameters import RegistryLink, catalog as read_catalog
        def run():
            try:
                return dict(laser_id=laser_id, instruments=read_catalog(laser(laser_id), RegistryLink(), refresh=refresh.lower() in ('1', 'true', 'yes')))
            except DeviceError as error:
                raise HTTPException(409, 'registry/catalog unavailable: ' + str(error)) from error
        return await asyncio.to_thread(run)

    @app.post(prefix + '/api/lasers/{laser_id}/device-parameters')
    async def change(laser_id: str, request: Request):
        data = await body(request)
        if set(data) - {'changes', 'approval_id', 'dry_run', 'refresh', 'request_id'} or 'changes' not in data:
            raise ValueError('Expected changes, approval_id?, dry_run?, refresh?')
        dry_run = bool(data.pop('dry_run', False))
        request_id = data.pop('request_id', None)
        who = actor(request)

        def run():
            runtime = app.state.runtime
            try:
                return runtime.change_device_parameters(laser_id, data, actor=who or 'dashboard',
                                                        dry_run=dry_run, request_id=request_id)
            except ApprovalError as error:
                raise HTTPException(403, str(error)) from error
            except DeviceError as error:
                raise HTTPException(409, str(error)) from error           # the text names what was already applied
        return await asyncio.to_thread(run)
