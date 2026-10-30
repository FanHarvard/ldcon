"""Common Web entry for independent LDCon services; no WebRuntime or owners.

Only a local administrator's registry selects upstreams. Browser requests can
select a registered ID and a fixed LDCon route, never a destination URL. All
mutations keep the upstream revision/admission checks and are sent once.
"""
import argparse
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import ipaddress
import json
from pathlib import Path
from ldcon.core.paths import web_assets_dir
import re
import secrets
import time
from urllib.error import HTTPError
from urllib.parse import parse_qsl, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request as HttpRequest, build_opener

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from ldcon.web.dashboard_state import CONTROL_OPERATIONS, display_state
from ldcon.web import dashboard_snapshot

IDENTIFIER = r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}'
ID = IDENTIFIER
MAX_BODY = 2 * 1024 * 1024
MAX_RESPONSE = 16 * 1024 * 1024
ASSETS = web_assets_dir()
# Explicit endpoints and methods. No generic proxy, URL parameter, owner RPC,
# filesystem path, migration header, or redirect forwarding is supported.
ROUTES = {
    'GET': [r'(?:sets|lasers|interface-decks|catalog|config|metric-quantities|jobs|history|retention|failures|reservations|agent-providers|agent-profiles|agent-routing|notification-policy)',
            rf'(?:sets|lasers|interface-decks|jobs|failures|reservations)/{ID}',
            rf'configuration/(?:lasers|assignments|interface-decks)/{ID}',
            rf'sets/{ID}/decks/{ID}', rf'lasers/{ID}/agent-(?:workspace|sessions)',
            rf'lasers/{ID}/(?:operating-mode|adjustment-guidance|control-readback)',
            rf'lasers/{ID}/target(?:/changes(?:/{ID})?)?',
            rf'modulation/{ID}/{ID}',
            rf'jobs/{ID}/(?:telemetry|report|analysis|hot-control|runtime-parameters|window-actions)',
            rf'jobs/{ID}/(?:laser|interface-deck)\.yaml',
            rf'jobs/{ID}/artifacts/(?:grafana-dashboard|grafana-manifest|mode-map|recovery|recovery-plan|recovery-test|operating-point-trace|target-response|measurement|window-center|operation-record)\.json',
            rf'fallback/(?:health|global|(?:card|job)/{ID}/{ID}(?:/{ID})?)'],
    'PUT': [rf'configuration/(?:lasers|assignments|interface-decks)/{ID}',
            rf'lasers/{ID}/(?:operating-mode|adjustment-guidance)',
            rf'lasers/{ID}/agent-workspace', rf'modulation/{ID}/{ID}', r'retention', r'agent-profiles', r'agent-routing', r'notification-policy',
            rf'fallback/(?:global|(?:card|job)/{ID}/{ID}(?:/{ID})?)'],
    'POST': [rf'sets/{ID}/decks/{ID}/start', rf'sets/{ID}/decks/{ID}/cards/{ID}/validate',
             rf'lasers/{ID}/target/changes(?:/{ID}/cancel)?',
             rf'jobs/{ID}/(?:stop|hot-control|runtime-parameters|online-tuning/stop|window-actions)',
             rf'jobs/{ID}/cards/{ID}/start', rf'jobs/{ID}/window-actions/{ID}/cancel',
             rf'modulation/{ID}/{ID}/preview', r'retention/archive', r'reservations',
             rf'reservations/{ID}/cancel', rf'failures/{ID}/questions/(?:{ID}|question:[0-9a-f]{{64}})/answer', rf'failures/{ID}/verify'],
    'PATCH': [rf'jobs/{ID}/window-actions/{ID}'],
}
QUERY_KEYS = {'set_id', 'active_only', 'operation', 'q', 'limit', 'offset',
              'laser_id', 'laser', 'status', 'include_resolved'}


@dataclass(frozen=True)
class Service:
    id: str
    label: str
    origin: str
    state_file: Path
    lasers: tuple = ()

    def capability_prefix(self):
        value = json.loads(self.state_file.read_text())
        path = value.get('path', '')
        if not re.fullmatch(r'/s/[A-Za-z0-9_-]{24,80}/', path):
            raise ValueError('Invalid service state')
        return path.rstrip('/')


def read_registry(path):
    value = json.loads(Path(path).read_text())
    if not isinstance(value, dict) or set(value) - {'services', 'telemetry_endpoint'} or not isinstance(value.get('services'), list):
        raise ValueError('Invalid service registry')
    services = {}
    for row in value['services']:
        if not isinstance(row, dict) or set(row) - {'id', 'label', 'origin', 'state_file', 'lasers'} or not {'id', 'label', 'origin', 'state_file'} <= set(row):
            raise ValueError('Invalid service entry')
        if not isinstance(row['id'], str) or not re.fullmatch(IDENTIFIER, row['id']) or row['id'] in services:
            raise ValueError('Invalid or duplicate service ID')
        if not isinstance(row['label'], str) or not 1 <= len(row['label']) <= 100:
            raise ValueError('Invalid service label')
        address = urlsplit(row['origin'])
        if (address.scheme != 'http' or address.username or address.password or address.path
                or address.query or address.fragment or not address.port
                or not ipaddress.ip_address(address.hostname).is_loopback):
            raise ValueError('Registry origins must be literal loopback HTTP addresses with a port')
        state_file = Path(row['state_file'])
        if not state_file.is_absolute():
            raise ValueError('Service state file must be absolute')
        lasers = row.get('lasers', [])
        if not isinstance(lasers, list) or any(not isinstance(laser, dict) or set(laser) != {'id', 'name'}
                or not isinstance(laser['id'], str) or not re.fullmatch(ID, laser['id'])
                or not isinstance(laser['name'], str) or not 1 <= len(laser['name']) <= 100 for laser in lasers):
            raise ValueError('Invalid configured laser inventory')
        services[row['id']] = Service(row['id'], row['label'], row['origin'], state_file, tuple(lasers))
    return services


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def forward(service, method, route, query='', body=None):
    """One HTTP exchange, with no proxies/redirects/retry or upstream headers."""
    prefix = service.capability_prefix()
    url = service.origin + prefix + '/api/' + route + ('?' + query if query else '')
    headers={'Content-Type': 'application/json', 'X-LDCon-Client': 'web'}
    request = HttpRequest(url, data=body, method=method,headers=headers)
    try:
        response = build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=8)
    except HTTPError as error:
        response = error
    with response:
        payload = response.read(MAX_RESPONSE + 1)
        if len(payload) > MAX_RESPONSE:
            raise ValueError('Upstream response too large')
        content_type = response.headers.get_content_type()
        if content_type not in ('application/json', 'text/html', 'application/yaml'):
            raise ValueError('Unsupported upstream response')
        # Upstream errors are never passed verbatim (they can include URLs).
        if response.status >= 500 or 300 <= response.status < 400:
            raise ValueError('Upstream unavailable')
        # Paths are capabilities. Scrub them even if they occur in error text,
        # job metadata, YAML or reports. Do not expose upstream origins either.
        text = payload.decode('utf-8').replace(service.origin + prefix, '[private service]')
        text = text.replace(prefix, '[private service]').replace(service.origin, '[private service]')
        text = re.sub(r'/s/[A-Za-z0-9_-]{24,80}(?=/|["\s<]|$)', '[private service]', text)
        return response.status, content_type, text.encode()


def create_fleet_app(registry, *, token=None, transport=forward):
    token = token or secrets.token_urlsafe(24)
    if not re.fullmatch(r'[A-Za-z0-9_-]{24,80}', token):
        raise ValueError('Invalid fleet token')
    prefix = '/s/' + token
    @asynccontextmanager
    async def lifespan(app):
        stop = asyncio.Event()
        async def inventory_heartbeat():
            from ldcon.core.telegraf import TelegrafExporter
            exporters = {}
            try:
                while not stop.is_set():
                    try:
                        registered = read_registry(registry)
                        endpoint = json.loads(Path(registry).read_text()).get('telemetry_endpoint')
                        current = set()
                        if endpoint:
                            # Discovery reads Web metadata only. This producer
                            # publishes existence, never lock/off claims.
                            await asyncio.gather(*(overview(service) for service in registered.values()))
                            for service in registered.values():
                                identities = {row['id']: row for row in service.lasers}
                                identities.update({row['id']: row for row in cache.get(service.id, [])})
                                for row in identities.values():
                                    key = (endpoint, service.id, row['id'], row['name'])
                                    current.add(key)
                                    if key not in exporters:
                                        exporters[key] = TelegrafExporter(endpoint, row['name'], simulation=False,
                                            tags=dict(laser_id=row['id'], service_id=service.id, producer='web-fleet-inventory'))
                                    exporters[key].send('inventory', 0, 'inventory', {'inventory_present': 1})
                        for key in set(exporters) - current:
                            exporters.pop(key).close()
                    except (OSError, ValueError, KeyError, TypeError):
                        # Metrics/network failure never affects the upstreams.
                        pass
                    try:
                        await asyncio.wait_for(stop.wait(), 10)
                    except asyncio.TimeoutError:
                        pass
            finally:
                for exporter in exporters.values():
                    exporter.close()
        task = asyncio.create_task(inventory_heartbeat())
        try:
            yield
        finally:
            stop.set()
            await task

    app = FastAPI(title='LDCon fleet', docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    from ldcon.web.gateway_lan import install_links
    install_links(app, registry, ASSETS)
    app.state.prefix = prefix
    cache = {}  # Last discovered identities only; never cached physical states.

    def services():
        try:
            return read_registry(registry)
        except (OSError, ValueError, TypeError, KeyError):
            raise HTTPException(503, 'Service registry is unavailable') from None

    def service_for(identifier):
        service = services().get(identifier)
        if service is None:
            raise HTTPException(404, 'Unknown service')
        return service

    @app.middleware('http')
    async def secure(request, call_next):
        if request.method not in ('GET', 'HEAD'):
            origin = request.headers.get('origin')
            if (request.headers.get('x-ldcon-client') != 'web'
                    or origin and urlsplit(origin).netloc != request.url.netloc
                    or request.headers.get('sec-fetch-site') == 'cross-site'):
                return JSONResponse({'detail': 'Same-origin client header is required'}, status_code=403)
        response = await call_next(request)
        # Framing is used only for our local, same-origin detail UI. No upstream
        # HTML page is embedded; upstream response headers are never forwarded.
        response.headers.update({'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
            'X-Content-Type-Options': 'nosniff',
            'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-src 'self'; frame-ancestors 'self'; base-uri 'none'; form-action 'self'"})
        return response

    @app.get(prefix + '/')
    async def index():
        return FileResponse(ASSETS / 'fleet.html')

    @app.get(prefix + '/assets/{name}')
    async def assets(name: str):
        if name not in ('fleet.js', 'fleet.css', 'app.css', 'workspace.css', 'i18n.js', 'fleet-settings.js', 'teams-settings.js'):
            raise HTTPException(404)
        return FileResponse(ASSETS / name)

    async def fetch_json(service, route, query=''):
        code, content_type, data = await asyncio.to_thread(transport, service, 'GET', route, query)
        if code != 200 or content_type != 'application/json':
            raise ValueError('Read failed')
        return json.loads(data)

    async def overview(service):
        try:
            sets, active = await asyncio.gather(fetch_json(service, 'sets'), asyncio.to_thread(dashboard_snapshot.snapshot, service))
            identities = {row['id']: row for row in service.lasers}
            identities.update({s['id']: dict(id=s['id'], name=s.get('name') or s['id']) for s in sets['sets']})
            identities = list(identities.values())
            cache[service.id] = identities
            controllers = [j for j in active['jobs'] if any(c.get('operation', c.get('card')) in CONTROL_OPERATIONS for c in j.get('cards', []))]
            return [dict(service_id=service.id, service_label=service.label, set_id=s['id'], name=s['name'],
                reachable=True, wavelength=active.get('wavelengths',{}).get(s['id'],{'valid':False}), **display_state([j for j in controllers if j.get('set_id') == s['id']])) for s in identities]
        except (OSError, ValueError, KeyError, TypeError):
            return [dict(service_id=service.id, service_label=service.label, set_id=s['id'], name=s['name'],
                reachable=False, code=12, state='UNKNOWN', reason='Service unavailable; physical state is unknown', job_id=None)
                for s in cache.get(service.id, service.lasers or [dict(id=None, name=service.label)])]

    @app.get(prefix + '/api/workspaces')
    async def workspaces():
        # Navigation is registry metadata, independent of controller health.
        # No job enumeration, upstream Web call, or instrument/owner access.
        return {'workspaces': [dict(service_id=service.id, set_id=laser['id'],
                    name=laser['name'], service_label=service.label)
                for service in services().values() for laser in service.lasers]}

    @app.get(prefix + '/api/fleet')
    async def fleet():
        registered = services()
        rows = await asyncio.gather(*(overview(service) for service in registered.values()))
        return {'lasers': [row for group in rows for row in group],
                'services': [dict(id=s.id, label=s.label) for s in registered.values()]}

    defaults_file = Path(registry).with_name('operations-defaults.json')

    @app.get(prefix + '/api/operations-defaults')
    async def operation_defaults():
        from ldcon.web.operations_settings import read
        try:
            return await asyncio.to_thread(read, defaults_file)
        except (ValueError, OSError):
            raise HTTPException(503, 'Shared operational defaults are unavailable') from None

    @app.put(prefix + '/api/operations-defaults')
    async def save_operation_defaults(request: Request):
        from ldcon.web.operations_settings import save
        from ldcon.web.web_runtime import ConflictError
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > 16384:
                raise HTTPException(413, 'Settings request is too large')
        try:
            payload = json.loads(data)
            roots = [service.state_file.parent for service in services().values()]
            return await asyncio.to_thread(save, defaults_file, payload, roots)
        except ConflictError as error:
            raise HTTPException(409, str(error)) from None
        except (ValueError, TypeError) as error:
            raise HTTPException(422, str(error)) from None
        except OSError:
            raise HTTPException(503, 'Shared operational defaults could not be saved') from None

    @app.get(prefix + '/api/history')
    async def history(q: str = '', limit: int = 25, offset: int = 0):
        if not 1 <= limit <= 100 or not 0 <= offset <= 1000 or len(q) > 1000:
            raise HTTPException(422, 'Invalid history page')
        async def service_history(service):
            rows, total = [], 0
            try:
                values = await asyncio.to_thread(dashboard_snapshot.jobs, service, True)
                values = [j for j in values if q.casefold() in ' '.join(str(j.get(k,'')) for k in ('id','set_name','set_id','deck_id','status','created_utc')).casefold()]
                total = len(values)
                rows = [dict(id=row['id'], service_id=service.id, service_label=service.label,
                    set_id=row.get('set_id'), set_name=row.get('set_name'), deck_id=row.get('deck_id'),
                    status=row.get('status'), created_utc=row.get('created_utc'), archived=row.get('archived',False)) for row in values[:offset+limit]]
                return rows, total, None
            except (OSError, ValueError, KeyError, TypeError):
                return [], 0, service.id
        results = await asyncio.gather(*(service_history(s) for s in services().values()))
        rows = sorted([row for values, _, _ in results for row in values],
                      key=lambda row: (row.get('created_utc') or '', row['service_id'], row['id']), reverse=True)
        return dict(jobs=rows[offset:offset + limit], total=sum(total for _, total, _ in results),
                    unavailable_services=[error for _, _, error in results if error])

    @app.get(prefix + '/services/{service_id}/')
    async def service_index(service_id: str):
        service_for(service_id)
        return FileResponse(ASSETS / 'index.html')

    @app.get(prefix + '/services/{service_id}/assets/{name}')
    async def service_assets(service_id: str, name: str):
        service_for(service_id)
        if name not in {p.name for p in ASSETS.iterdir() if p.suffix in ('.js', '.css')}:
            raise HTTPException(404)
        return FileResponse(ASSETS / name)

    @app.get(prefix + '/services/{service_id}/cards/{card_id}')
    async def help_card(service_id: str, card_id: str):
        service_for(service_id)
        if not re.fullmatch(ID, card_id) or not (ASSETS / 'cards' / (card_id + '.html')).is_file():
            raise HTTPException(404)
        return FileResponse(ASSETS / 'cards' / (card_id + '.html'))

    @app.get(prefix + '/services/{service_id}/agent-settings-assets/{name}')
    async def agent_settings_assets(service_id: str, name: str):
        service_for(service_id)
        if name not in ('agent-settings.js','agent-settings.css'):raise HTTPException(404)
        return FileResponse(ASSETS/name)

    @app.get(prefix + '/services/{service_id}/{page}')
    async def page(service_id: str, page: str):
        service_for(service_id)
        if page in ('reservations','notifications'):raise HTTPException(410,'This per-card settings page has been retired')
        filename = {'help': 'help.html', 'modulation': 'modulation.html',
                    'reservations': 'reservations.html', 'failures': 'failures.html', 'agent-settings':'agent-settings.html'}.get(page)
        if filename is None:
            raise HTTPException(404)
        return FileResponse(ASSETS / filename)

    from ldcon.web.teams_destinations_api import install_fleet as install_teams_destinations
    install_teams_destinations(app, prefix, registry, services)

    @app.get(prefix + '/services/{service_id}/api/jobs/{job_id}/report.pdf')
    async def pdf_report(service_id: str, job_id: str, request: Request):
        """Serve published PDFs only; never compile, query owners, or fall back to HTML."""
        service = service_for(service_id)
        if not re.fullmatch(ID, job_id):raise HTTPException(404)
        if request.url.query:raise HTTPException(422, 'PDF reports have no query arguments')
        def read_pdf():
            root = dashboard_snapshot.root_for(service)
            # Fixed job-scoped paths. The second location is report_pdf's standard build output.
            for path in (root/'jobs'/job_id/'report.pdf', root/'reports'/job_id/'build'/'report.pdf'):
                payload = dashboard_snapshot.read(root, path, limit=MAX_RESPONSE, parser=bytes)
                if isinstance(payload, bytes) and payload.startswith(b'%PDF-'):
                    return payload
            return None
        try:payload = await asyncio.to_thread(read_pdf)
        except (OSError, ValueError):payload = None
        if payload is None:raise HTTPException(404, 'No PDF report has been published for this job')
        return Response(payload, media_type='application/pdf',
                        headers={'Content-Disposition': 'inline; filename="report.pdf"'})

    @app.api_route(prefix + '/services/{service_id}/api/{route:path}', methods=['GET', 'PUT', 'POST', 'PATCH'])
    async def route(service_id: str, route: str, request: Request):
        service = service_for(service_id)
        if request.method == 'GET' and route == 'jobs' and set(request.query_params) <= {'set_id','operation'}:
            rows = await asyncio.to_thread(lambda: list(dashboard_snapshot.study_jobs(service,
                request.query_params.get('set_id'),request.query_params.get('operation'))))
            text=json.dumps({'jobs':rows}).replace(service.origin,'[private service]')
            text=re.sub(r'/s/[A-Za-z0-9_-]{24,80}(?=/|[\"\s<]|$)','[private service]',text)
            return Response(text,media_type='application/json')
        if request.method == 'GET' and route in ('dashboard/snapshot','history'):
            try:
                if route == 'dashboard/snapshot':
                    if request.url.query:raise ValueError('Snapshot has no query arguments')
                    # Reachability is separate from worker evidence. This cheap
                    # endpoint does not scan jobs or query telemetry SQLite.
                    await fetch_json(service, 'config')
                    value = await asyncio.to_thread(dashboard_snapshot.snapshot, service)
                else:
                    query = dict(request.query_params)
                    if set(query)-{'q','limit','offset','set_id'}:raise ValueError('Unsupported history query')
                    value = await asyncio.to_thread(dashboard_snapshot.history, service, query.get('q',''), int(query.get('limit',25)), int(query.get('offset',0)),query.get('set_id'))
                text=json.dumps(value)
                text=text.replace(service.origin,'[private service]').replace(service.capability_prefix(),'[private service]')
                text=re.sub(r'/s/[A-Za-z0-9_-]{24,80}(?=/|["\s<]|$)','[private service]',text)
                return Response(text,media_type='application/json')
            except (OSError, ValueError, KeyError, TypeError):
                return JSONResponse({'detail':'Display snapshot unavailable'},status_code=503)
        if not any(re.fullmatch(pattern, route) for pattern in ROUTES.get(request.method, [])):
            raise HTTPException(404)
        pairs = parse_qsl(request.url.query, keep_blank_values=True)
        target_route = re.fullmatch(rf'lasers/{ID}/target(?:/changes(?:/{ID}(?:/cancel)?)?)?', route)
        if target_route or re.fullmatch(rf'jobs/{ID}/analysis', route):
            from ldcon.web.lock_target_api import REQUEST_ID
            keys = {'request_id'} if request.method == 'GET' and route.endswith('/target/changes') else set()
            if len(pairs) > 1 or any(key not in keys or not REQUEST_ID.fullmatch(value) for key, value in pairs):
                raise HTTPException(422, 'Unsupported query')
        elif len(pairs) > 20 or any(key not in QUERY_KEYS or len(value) > 1000 for key, value in pairs):
            raise HTTPException(422, 'Unsupported query')
        data = None
        if request.method != 'GET':
            chunks = bytearray()
            async for chunk in request.stream():
                chunks.extend(chunk)
                if len(chunks) > MAX_BODY:
                    raise HTTPException(413, 'Request too large')
            try:
                parsed = json.loads(chunks)
                if not isinstance(parsed, dict):
                    raise ValueError()
            except (ValueError, UnicodeError):
                raise HTTPException(422, 'Expected JSON object') from None
            data = bytes(chunks)
        try:
            code, content_type, payload = await asyncio.to_thread(transport, service, request.method, route, urlencode(pairs), data)
        except (OSError, ValueError, KeyError, TypeError):
            message = ('Service unavailable; request outcome is unknown. Inspect the service before retrying.'
                       if request.method != 'GET' else 'Service unavailable')
            return JSONResponse({'detail': message}, status_code=503)
        return Response(payload, status_code=code, media_type=content_type)

    async def agent_settings_body(request: Request):
        raw=bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw)>MAX_BODY:raise HTTPException(413,'Settings request is too large')
        try:value=json.loads(raw)
        except (ValueError,TypeError):raise HTTPException(422,'Expected a JSON object') from None
        if not isinstance(value,dict):raise HTTPException(422,'Expected a JSON object')
        return value

    from ldcon.web.agent_settings_api import install_routes as settings_routes
    from ldcon.agents.agent_profiles import install_routes as profile_routes
    settings_routes(app,prefix,ASSETS,agent_settings_body,root=Path(registry).parent)
    profile_routes(app,prefix,agent_settings_body,root=Path(registry).parent)
    from ldcon.web.web_runtime import ConflictError
    @app.exception_handler(ValueError)
    async def invalid_settings(request, error):
        return JSONResponse({'detail':str(error)},status_code=409 if isinstance(error,ConflictError) else 422)
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--registry', type=Path, required=True)
    parser.add_argument('--state-file', type=Path, required=True)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8086)
    args = parser.parse_args()
    read_registry(args.registry)
    previous = json.loads(args.state_file.read_text()) if args.state_file.exists() else {}
    match = re.fullmatch(r'/s/([A-Za-z0-9_-]{24,80})/', previous.get('path', ''))
    app = create_fleet_app(args.registry, token=match.group(1) if match else None)
    from ldcon.core.records import atomic_json
    args.state_file.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(args.state_file, dict(host=args.host, port=args.port, path=app.state.prefix + '/', backend='fleet'))
    args.state_file.chmod(0o600)
    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, access_log=False)


if __name__ == '__main__':
    main()
