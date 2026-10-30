"""Notification companion: live settings and independent failure observation."""
from ldcon.core.state_layout import config_root

import argparse
from contextlib import asynccontextmanager
import fcntl
import json
from pathlib import Path
from ldcon.core.paths import web_assets_dir
import threading
import time

from fastapi import FastAPI,HTTPException,Request
from fastapi.responses import FileResponse,JSONResponse
from ldcon.agents.card_fallback import FallbackStore,FailureDetector,NotificationDispatcher,read,identifier,job_directory


def create_app(web_root,notification_root,prefix,*,start_monitor=True,prefix_provider=None):
    web_root=Path(web_root);store=FallbackStore(notification_root)
    detector=FailureDetector(web_root,store);dispatcher=NotificationDispatcher(web_root,store)
    stopping=threading.Event();monitor_state=dict(last_scan_unix_s=None,error=None)
    def monitor():
        recovered=False
        while not stopping.is_set():
            try:
                if not recovered:dispatcher.recover_interrupted_delivery();recovered=True
                detector.scan();dispatcher.tick();monitor_state.update(last_scan_unix_s=time.time(),error=None)
            except Exception as error:
                monitor_state['error']=str(error)
                try:store.audit('observer_error',dict(type=type(error).__name__,message=str(error)))
                except Exception:pass  # Notification persistence never controls devices.
            stopping.wait(1.)
    @asynccontextmanager
    async def lifespan(app):
        thread=None
        if start_monitor:thread=threading.Thread(target=monitor,daemon=True);thread.start()
        yield
        stopping.set()
        if thread:thread.join(timeout=2)
    app=FastAPI(lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
    app.state.store=store;app.state.detector=detector;app.state.dispatcher=dispatcher
    prefix=prefix.rstrip('/')
    route_prefix='/s/{capability}' if prefix_provider else prefix
    assets=web_assets_dir()
    @app.middleware('http')
    async def boundary(request,call_next):
        current=prefix_provider() if prefix_provider else prefix
        if not isinstance(current,str) or not current.startswith('/s/') or len(current)<20:return JSONResponse({'detail':'Dashboard capability unavailable'},503)
        if not request.url.path.startswith(current+'/'):return JSONResponse({'detail':'Not found'},404)
        if request.method not in ('GET','HEAD'):
            if request.headers.get('X-LDCon-Client')!='web' or not request.headers.get('Content-Type','').startswith('application/json'):
                return JSONResponse({'detail':'JSON application request required'},403)
            origin=request.headers.get('origin')
            if origin and origin!=str(request.base_url).rstrip('/'):return JSONResponse({'detail':'Same origin required'},403)
        response=await call_next(request)
        response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        response.headers['Referrer-Policy']='no-referrer';response.headers['Cache-Control']='no-store'
        return response
    @app.exception_handler(ValueError)
    async def invalid(request,error):return JSONResponse({'detail':str(error)},422)
    @app.exception_handler(FileNotFoundError)
    async def missing(request,error):return JSONResponse({'detail':'Not found'},404)
    def meta(job):
        value=read(job_directory(web_root,job)/'job.json',{})
        if value.get('id')!=job:raise FileNotFoundError(job)
        return value
    def card_ids(m):return [c['id'] for c in m.get('cards',[])+m.get('online_cards',[]) if c.get('id')]
    def card_exists(set_id,deck,card):
        for kind in ('interface-decks','decks'):
            path=config_root(web_root)/kind/(identifier(deck)+'.yaml')
            if path.exists():
                import yaml
                d=yaml.safe_load(path.read_text())
                if any(c.get('id')==card for c in d.get('cards',[])):return
        # Legacy set definitions embed decks.
        path=web_root/'sets'/(identifier(set_id)+'.yaml')
        if path.exists():
            import yaml
            d=yaml.safe_load(path.read_text())
            if any(x.get('id')==deck and any(c.get('id')==card for c in x.get('cards',[])) for x in d.get('decks',[])):return
        raise FileNotFoundError('Card')
    async def payload(request):
        data=await request.body()
        if len(data)>8192:raise HTTPException(413,'Request too large')
        try:value=json.loads(data)
        except (ValueError,UnicodeDecodeError):raise ValueError('Expected JSON')
        if not isinstance(value,dict) or set(value)!={'config','expected_revision'}:raise ValueError('Expected config and expected_revision')
        if type(value['expected_revision']) is not int or value['expected_revision']<0:raise ValueError('Expected a revision number')
        return value
    @app.get(route_prefix+'/')
    def page():return FileResponse(assets/'fallback.html',media_type='text/html')
    @app.get(route_prefix+'/assets/{name}')
    def asset(name):
        if name not in ('fallback.js','fallback.css','i18n.js','translations.js'):raise FileNotFoundError(name)
        return FileResponse(assets/name)
    def visible(policy):
        from ldcon.agents.fallback_targets import public
        return {**policy,'config':public(policy['config'])} if policy['config']['kind']=='standard' else policy
    @app.get(route_prefix+'/api/global')
    def get_global():return visible(store.get('global',[]))
    @app.put(route_prefix+'/api/global')
    async def put_global(request:Request):
        data=await payload(request)
        if data['config'].get('enabled') or any(data['config'].get(name,{}).get('enabled') for name in ('teams','codex','claude')):raise ValueError('Use notify.teams or agent.resident cards')
        return visible(store.put('global',[],data['config'],data['expected_revision']))
    @app.get(route_prefix+'/api/health')
    def health():return dict(ok=not monitor_state['error'],monitor=monitor_state,notifications=store.recent(20))
    @app.get(route_prefix+'/api/card/{set_id}/{deck}/{card}')
    def get_card(set_id:str,deck:str,card:str):
        identifier(set_id);identifier(card);card_exists(set_id,deck,card)
        return store.get('card',[set_id,deck,card])
    @app.put(route_prefix+'/api/card/{set_id}/{deck}/{card}')
    async def put_card(set_id:str,deck:str,card:str,request:Request):
        identifier(set_id);identifier(card);card_exists(set_id,deck,card);data=await payload(request)
        return store.put('card',[set_id,deck,card],data['config'],data['expected_revision'])
    @app.get(route_prefix+'/api/job/{job}/{card}')
    def get_job(job:str,card:str):
        m=meta(job)
        if card not in card_ids(m):raise FileNotFoundError(card)
        return dict(override=store.get('job',[job,card]),effective=visible(store.effective(m,card)),job=job,card=card,status=read(job_directory(web_root,job)/'progress.json',{}).get('status'))
    @app.put(route_prefix+'/api/job/{job}/{card}')
    async def put_job(job:str,card:str,request:Request):
        m=meta(job)
        if card not in card_ids(m):raise FileNotFoundError(card)
        data=await payload(request)
        store.put('job',[job,card],data['config'],data['expected_revision'])
        return get_job(job,card)
    return app


def main():
    import uvicorn
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--web-root',type=Path,default=Path('data/web'))
    parser.add_argument('--notification-root',type=Path,default=Path('data/card-fallback'))
    parser.add_argument('--server-file',type=Path,default=Path('data/web-server.json'))
    parser.add_argument('--host',default='127.0.0.1');parser.add_argument('--port',type=int,default=8768)
    a=parser.parse_args()
    if a.host not in ('127.0.0.1','::1'):parser.error('Notification service must bind localhost; use the existing dashboard proxy')
    a.notification_root.mkdir(parents=True,exist_ok=True)
    lock=(a.notification_root/'service.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    server=read(a.server_file,{});prefix=server.get('path') or '/s/dashboard-not-ready'
    uvicorn.run(create_app(a.web_root,a.notification_root,prefix,prefix_provider=lambda:read(a.server_file,{}).get('path','').rstrip('/')),host=a.host,port=a.port,log_level='warning')


if __name__=='__main__':main()
