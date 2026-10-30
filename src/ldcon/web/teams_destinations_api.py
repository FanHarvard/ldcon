"""Secret write-only settings API; does not start senders or send test messages."""
import asyncio
import json
from fastapi import HTTPException, Request
from ldcon.agents import teams_destinations as destinations


async def handle(path, inventory, request=None, *, allowed_lasers=None):
    try:
        if request is None:
            return destinations.public(destinations.read(path), inventory)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 16384:
                raise HTTPException(413, 'Destination request is too large')
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            raise HTTPException(422, 'Expected a JSON settings object') from None
        return await asyncio.to_thread(destinations.save, path, payload, inventory, allowed_lasers=allowed_lasers)
    except destinations.Conflict:
        raise HTTPException(409, 'Destinations changed. Reload before saving.') from None
    except PermissionError:
        raise HTTPException(403, 'This page can edit only its own laser destination') from None
    except ValueError:
        # Never echo parser errors, provider URLs, or request input.
        raise HTTPException(422, 'Invalid destination settings. Use a Microsoft Teams/Workflows HTTPS URL; a custom destination needs a saved or new URL.') from None
    except OSError:
        raise HTTPException(503, 'Private destination storage is unavailable') from None


def install_fleet(app, prefix, registry, services):
    from pathlib import Path
    path = destinations.settings_path(Path(registry).parent)
    def inventory(service_id=None):
        rows = {}
        registered = services()
        if service_id is not None:
            if service_id not in registered:
                raise HTTPException(404, 'Unknown service')
            registered = {service_id: registered[service_id]}
        for service in registered.values():
            for laser in service.lasers:
                rows[laser['id']] = dict(laser)
        return list(rows.values())

    @app.get(prefix + '/api/teams-destinations')
    async def get_destinations():
        return await handle(path, inventory())

    @app.put(prefix + '/api/teams-destinations')
    async def put_destinations(request: Request):
        return await handle(path, inventory(), request)

    # Install before the generic upstream proxy. Settings remain in the frontend.
    @app.get(prefix + '/services/{service_id}/api/teams-destinations')
    async def get_laser_destinations(service_id: str):
        return await handle(path, inventory(service_id))

    @app.put(prefix + '/services/{service_id}/api/teams-destinations')
    async def put_laser_destinations(service_id: str, request: Request):
        rows = inventory(service_id)
        return await handle(path, rows, request, allowed_lasers={r['id'] for r in rows})


def install_local(app, prefix):
    def state():
        runtime = app.state.runtime
        rows = [dict(id=r['id'], name=r.get('name') or r['id']) for r in runtime.set_library()]
        return destinations.settings_path(runtime.root), rows

    @app.get(prefix + '/api/teams-destinations')
    async def get_destinations():
        path, rows = state()
        return await handle(path, rows)

    @app.put(prefix + '/api/teams-destinations')
    async def put_destinations(request: Request):
        path, rows = state()
        return await handle(path, rows, request, allowed_lasers={r['id'] for r in rows})
