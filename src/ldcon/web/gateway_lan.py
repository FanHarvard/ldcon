"""Internal-LAN-only, unauthenticated browser entry to one OpenCode gateway.

The private provider credential never leaves this process. No laser API is used.
A gateway restart is discovered on the next request, without restarting a lock.
"""
import argparse
import asyncio
import base64
from contextlib import asynccontextmanager
import ipaddress
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse, StreamingResponse, RedirectResponse
import httpx

from ldcon.agents.agent_connect import find

MAX_BODY = 16 * 1024 * 1024
HOP = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
       'te', 'trailer', 'transfer-encoding', 'upgrade', 'host', 'authorization',
       'cookie', 'set-cookie', 'www-authenticate', 'content-length'}


def config(path):
    value = json.loads(Path(path).read_text())
    host = ipaddress.ip_address(value['host'])
    networks = [ipaddress.ip_network(n) for n in value['networks']]
    if not host.is_private or host.is_unspecified or host.is_loopback or host.version != 4:
        raise ValueError('An explicit internal IPv4 interface is required')
    if not networks or any(n.version != 4 or not n.is_private or n.prefixlen < 16 for n in networks):
        raise ValueError('Explicit private LAN networks are required')
    if not any(host in n for n in networks):
        raise ValueError('Bind address must belong to the allowed LAN')
    ports = value['lasers']
    if not ports or any(not re.fullmatch(r'[A-Za-z0-9_-]+',k) or type(p) is not int or not 1024 <= p <= 65535 for k,p in ports.items()):
        raise ValueError('Invalid laser port registry')
    if len(set(ports.values())) != len(ports):
        raise ValueError('Each laser needs its own browser origin')
    return value


def allowed(scope, headers, cfg, port, *, mutation=False):
    # Never trust forwarded headers or DNS names. uvicorn proxy headers are off.
    try:
        peer = ipaddress.ip_address(scope['client'][0])
        local = scope['server'][0]
        expected = f"{cfg['host']}:{port}"
        if local != cfg['host'] or headers.get('host') != expected:
            return False
        if not any(peer in ipaddress.ip_network(n) for n in cfg['networks']):
            return False
        origin = headers.get('origin')
        if origin and origin != 'http://' + expected:
            return False
        if headers.get('sec-fetch-site') == 'cross-site':
            return False
        return not mutation or origin == 'http://' + expected
    except (KeyError, ValueError, TypeError):
        return False


def upstream(record):
    url = urlsplit(record['url'])
    if url.scheme != 'http' or url.hostname != '127.0.0.1' or not url.port or url.path or url.query or url.fragment or url.username:
        raise ValueError('Only the registered loopback gateway may be proxied')
    credential = base64.b64encode(('opencode:' + record['password']).encode()).decode()
    return record['url'], 'Basic ' + credential


def headers_for(headers, auth, origin):
    connection = {v.strip().lower() for v in headers.get('connection','').split(',')}
    result = {k:v for k,v in headers.items() if k.lower() not in HOP | connection
              and not k.lower().startswith(('x-forwarded-', 'sec-websocket-'))
              and k.lower() not in {'forwarded','origin','referer','accept-encoding'}}
    result.update(authorization=auth, origin=origin, **{'accept-encoding':'identity'})
    return result


def create_app(cfg, laser, *, resolver=find, transport=None):
    port = cfg['lasers'][laser]
    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False,
                                    timeout=httpx.Timeout(15,read=None), limits=httpx.Limits(max_connections=64)) as client:
            app.state.client=client
            yield
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    async def resolve():
        try:
            record = await asyncio.to_thread(resolver, laser)
            return record, *upstream(record)
        except (OSError, ValueError, RuntimeError, KeyError):
            raise HTTPException(503, 'No live OpenCode gateway for this laser. Start its OpenCode gateway in LDCon, then reload. No process was started or stopped.') from None

    @app.api_route('/{path:path}', methods=['GET','HEAD','POST','PUT','PATCH','DELETE','OPTIONS'])
    async def proxy(request: Request, path: str):
        if not allowed(request.scope, request.headers, cfg, port, mutation=request.method not in ('GET','HEAD','OPTIONS')):
            raise HTTPException(403, 'Internal LAN and same-origin access only')
        if path == '_ldcon':
            record, _, _ = await resolve()
            return {'laser':laser,'session_id':record['session_id'],'shared_server':True}
        try:
            record, base, auth = await resolve()
        except HTTPException:
            if request.method == 'GET' and 'text/html' in request.headers.get('accept',''):
                return HTMLResponse('<!doctype html><html lang="en"><title>Gateway unavailable</title><h1>'+laser+' gateway unavailable</h1><p>Start this laser’s OpenCode gateway in LDCon, then reload. Claude/Codex gateways use their own remote connection.</p><p>No laser, lock or gateway was started or stopped.</p></html>',status_code=503)
            raise
        if not path and request.method == 'GET' and 'text/html' in request.headers.get('accept',''):
            session=record['session_id']
            if not re.fullmatch(r'[A-Za-z0-9_-]+',session):raise HTTPException(503,'Invalid session identity')
            public=f"http://{cfg['host']}:{port}"
            key=base64.urlsafe_b64encode(public.encode()).decode().rstrip('=')
            return RedirectResponse('/server/'+key+'/session/'+session,status_code=307)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw)>MAX_BODY:raise HTTPException(413,'Request too large')
        # Construct origin + raw path, never urljoin (// would change the host).
        raw_path=request.scope.get('raw_path',b'/').decode('ascii')
        query=request.scope.get('query_string',b'').decode('ascii')
        target=base+raw_path+('?' + query if query else '')
        headers=headers_for(request.headers,auth,base)
        try:
            req=app.state.client.build_request(request.method,target,headers=headers,content=bytes(raw))
            response=await app.state.client.send(req,stream=True)
        except httpx.HTTPError:
            raise HTTPException(502,'Gateway transport failed. The request may have been delivered; inspect the conversation before resubmitting. Nothing was retried.') from None
        filtered={k:v for k,v in response.headers.items() if k.lower() not in HOP}
        if 'location' in filtered:
            location=filtered['location']
            if location.startswith(base+'/'):filtered['location']=location[len(base):]
            elif urlsplit(location).netloc:
                await response.aclose()
                raise HTTPException(502,'External gateway redirect was not followed')
        filtered.update({'cache-control':'no-store','referrer-policy':'no-referrer','x-content-type-options':'nosniff'})
        async def body():
            try:
                async for chunk in response.aiter_raw():yield chunk
            finally:await response.aclose()
        return StreamingResponse(body(),status_code=response.status_code,headers=filtered)

    @app.websocket('/{path:path}')
    async def websocket(ws: WebSocket, path: str):
        if not allowed(ws.scope,ws.headers,cfg,port,mutation=True):
            await ws.close(code=1008);return
        from websockets.asyncio.client import connect
        try:
            _,base,auth=await resolve()
            query=ws.scope.get('query_string',b'').decode('ascii')
            url=base.replace('http:','ws:',1)+ws.scope['raw_path'].decode('ascii')+('?' + query if query else '')
            protocols=[p.strip() for p in ws.headers.get('sec-websocket-protocol','').split(',') if p.strip()]
            async with connect(url,additional_headers={'Authorization':auth},origin=base,
                               subprotocols=protocols or None,proxy=None,max_size=MAX_BODY) as remote:
                await ws.accept(subprotocol=remote.subprotocol)
                async def upload():
                    while True:
                        message=await ws.receive()
                        if message['type']=='websocket.disconnect':return
                        await remote.send(message.get('text') if message.get('text') is not None else message['bytes'])
                async def download():
                    async for message in remote:
                        if isinstance(message,bytes):await ws.send_bytes(message)
                        else:await ws.send_text(message)
                tasks=[asyncio.create_task(upload()),asyncio.create_task(download())]
                try:await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in tasks:task.cancel()
                    await asyncio.gather(*tasks,return_exceptions=True)
        except Exception:
            # Never log provider payloads/credentials or replay a WS connection.
            try:await ws.close(code=1011)
            except RuntimeError:pass
    return app


def install_links(app, registry, assets):
    """Stable fleet URL; config is local-admin-only, never supplied by a browser."""
    from fastapi.responses import RedirectResponse
    @app.get('/gateway-chat/{laser}')
    async def open_chat(laser: str, request: Request):
        try:
            cfg=config(Path(registry).parent/'gateway-lan.json')
            if laser not in cfg['lasers']:raise ValueError('Unknown laser')
        except (OSError,ValueError,KeyError):
            raise HTTPException(503,'LAN gateway chat is not configured for this laser') from None
        # The fleet port differs; enforce its actual local bind address and LAN.
        if not allowed(request.scope,request.headers,cfg,request.url.port or 80):
            raise HTTPException(403,'Internal LAN access only')
        return RedirectResponse(f"http://{cfg['host']}:{cfg['lasers'][laser]}/",status_code=307)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--laser',required=True)
    args=parser.parse_args();cfg=config(args.config)
    if args.laser not in cfg['lasers']:parser.error('Laser is not registered')
    import uvicorn
    uvicorn.run(create_app(cfg,args.laser),host=cfg['host'],port=cfg['lasers'][args.laser],
                proxy_headers=False,access_log=False)


if __name__=='__main__':main()
