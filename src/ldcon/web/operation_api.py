"""Common operation HTTP views; legacy routes remain compatibility adapters."""
import asyncio

from ldcon.web.laser_operations import TrustedOperationContext


def install_routes(app, prefix, body, actor):
    def port(laser_id):
        return app.state.runtime.operations(laser_id)

    from fastapi import Request

    @app.get(prefix + '/api/lasers/{laser_id}/operations')
    async def describe(laser_id: str):
        return await asyncio.to_thread(port(laser_id).describe, laser_id)

    @app.post(prefix + '/api/lasers/{laser_id}/operations/validate')
    async def preflight(laser_id: str, request: Request):
        return await asyncio.to_thread(port(laser_id).preflight, await body(request),
                                       TrustedOperationContext(laser_id, actor(request) or 'dashboard'))

    @app.post(prefix + '/api/lasers/{laser_id}/operations', status_code=202)
    async def submit(laser_id: str, request: Request):
        return await asyncio.to_thread(port(laser_id).submit, await body(request),
                                       TrustedOperationContext(laser_id, actor(request) or 'dashboard'))

    @app.get(prefix + '/api/lasers/{laser_id}/operations/{run_id}/{request_id}')
    async def status(laser_id: str, run_id: str, request_id: str):
        return await asyncio.to_thread(port(laser_id).status, laser_id, run_id, request_id)

    @app.post(prefix + '/api/lasers/{laser_id}/operations/{run_id}/{request_id}/cancel')
    async def cancel(laser_id: str, run_id: str, request_id: str, request: Request):
        return await asyncio.to_thread(port(laser_id).cancel, laser_id, run_id, request_id,
                                       TrustedOperationContext(laser_id, actor(request) or 'dashboard'))
