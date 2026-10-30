"""Laser dashboard and interface-card API. No direct instrument writes."""
import argparse
import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict
import html
import json
import os
from pathlib import Path
from ldcon.core.paths import web_assets_dir
import re
import secrets
from urllib.parse import urlsplit
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from ldcon.core.deck_yaml import MAX_YAML_BYTES, dump_document
from ldcon.core.interface_schema import CARD_TYPES, readiness
from ldcon.core.interface_cards import INTERFACE_CATALOG
from ldcon.web.web_runtime import ConflictError, TERMINAL, TriggerPending, WebRuntime, atomic_json, read_json, safe_id
from ldcon.core import configuration
from ldcon.web import log_archive


def create_app(state_dir=None, token=None, max_concurrent_jobs=12):
    token=token or secrets.token_urlsafe(24)
    prefix='/s/'+token
    assets=web_assets_dir()
    notification_port=os.environ.get('LDCON_NOTIFICATION_PORT','8768')
    if not re.fullmatch(r'[0-9]{1,5}',notification_port) or not 1<=int(notification_port)<=65535:
        raise ValueError('LDCON_NOTIFICATION_PORT must be an integer in 1..65535')
    notification_endpoint=f'http://127.0.0.1:{int(notification_port)}'

    @asynccontextmanager
    async def lifespan(app):
        app.state.runtime=WebRuntime(state_dir or Path.cwd()/'data/web',max_concurrent_jobs=max_concurrent_jobs)
        stop_maintenance=asyncio.Event()
        async def maintenance():
            while not stop_maintenance.is_set():
                try:await asyncio.to_thread(log_archive.archive_finished,app.state.runtime)
                except Exception as error:
                    atomic_json(app.state.runtime.root/'archive-status.json',{'errors':[str(error)]})
                try:await asyncio.wait_for(stop_maintenance.wait(),3600)
                except asyncio.TimeoutError:pass
        task=asyncio.create_task(maintenance())
        async def dispatch_target_intents():
            while not stop_maintenance.is_set():
                # Target intents survive API restarts. GET handlers never dispatch.
                if (app.state.runtime.root/'lock-target-queue').exists():
                    from ldcon.web.dlcpro_target_api import TargetApi
                    if getattr(app.state,'lock_target_api',None) is None:
                        app.state.lock_target_api=TargetApi(app.state.runtime)
                    try:await asyncio.to_thread(app.state.lock_target_api.tick)
                    except Exception as error:
                        atomic_json(app.state.runtime.root/'target-queue-service-status.json',{'error_type':type(error).__name__})
                try:await asyncio.wait_for(stop_maintenance.wait(),1)
                except asyncio.TimeoutError:pass
        target_intent_task=asyncio.create_task(dispatch_target_intents())
        try:yield
        finally:
            stop_maintenance.set()
            await task
            await target_intent_task
            await asyncio.to_thread(app.state.runtime.close)

    app=FastAPI(title='LDCon laser controller',docs_url=None,redoc_url=None,openapi_url=None,lifespan=lifespan)
    app.state.prefix=prefix

    @app.middleware('http')
    async def headers(request,call_next):
        if request.method not in ('GET','HEAD','OPTIONS'):
            origin=request.headers.get('origin')
            # 'web' everywhere; 'labscript' ONLY on the lock-target change routes (contract v1, 2026-10-22).
            client=request.headers.get('x-ldcon-client')
            from ldcon.web.lock_target_api import labscript_route
            from ldcon.web.sweep_api import labscript_route as sweep_labscript_route
            labscript=request.method=='POST' and (labscript_route(prefix,request.url.path) or sweep_labscript_route(prefix,request.url.path))
            if not (client=='web' or client=='labscript' and labscript) or origin and urlsplit(origin).netloc!=request.url.netloc:
                return JSONResponse({'detail':'Same-origin client header is required'},status_code=403)
        response=await call_next(request)
        response.headers.update({'Cache-Control':'no-store','Referrer-Policy':'no-referrer','X-Content-Type-Options':'nosniff',
            'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'"})
        return response

    @app.exception_handler(ValueError)
    async def invalid(request,error):
        if isinstance(error,ConflictError) and isinstance(getattr(error,'payload',None),dict):
            return JSONResponse({'detail':str(error),**error.payload},status_code=409)      # v1.4 sweep refusal
        from ldcon.web.laser_operations import error_record
        return JSONResponse({'detail':str(error), 'operation_error':getattr(error,'operation_error',
            error_record(error,effect='not_classified'))},status_code=409 if isinstance(error,ConflictError) else 422)
    @app.exception_handler(FileNotFoundError)
    async def missing(request,error):return JSONResponse({'detail':'Not found'},status_code=404)

    async def body(request):
        value=bytearray()
        async for chunk in request.stream():
            value.extend(chunk)
            if len(value)>MAX_YAML_BYTES+16384:raise HTTPException(413,'Request is too large')
        try:data=json.loads(value)
        except (ValueError,UnicodeError) as error:raise ValueError('Invalid JSON request') from error
        if not isinstance(data,dict):raise ValueError('Expected a JSON object')
        return data

    def actor(request):
        # Audit label only; it grants nothing (the ledger, not the header, authorizes).
        value=request.headers.get('X-LDCon-Actor','dashboard')
        return value if isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}',value) else 'dashboard'

    @app.get(prefix+'/',include_in_schema=False)
    def index():return FileResponse(assets/'index.html',media_type='text/html')
    @app.get(prefix+'/assets/{name}',include_in_schema=False)
    def asset(name:str):
        if name not in ('app.js','card-browser.js','gateway-presentation.js','committing-input.js','card-guide.js','app.css','workspace.css','laser-workspace.js','i18n.js','configuration-editor.js','dashboard-state.js','window-actions.js','grafana-help.js','fallback.js','fallback.css','modulation.js','reservations.js','failures.js','teams-settings.js'):raise HTTPException(404)
        return FileResponse(assets/name)
    @app.get(prefix+'/api/metric-quantities')
    def metric_quantities():
        from ldcon.core.metric_quantities import describe
        return {'quantities':describe()}

    @app.get(prefix+'/help',include_in_schema=False)
    def help_page():return FileResponse(assets/'help.html',media_type='text/html')
    @app.get(prefix+'/api/catalog')
    def catalog():
        from ldcon.core.card_reporting import declarations_for
        return {'cards':[{**asdict(c),'report_quantities':declarations_for(c.id),
            'execution_support':'interface-live','example_yaml':dump_document({'id':'example','operation':c.id,'uses':list(CARD_TYPES[c.id]),'parameters':{}})} for c in INTERFACE_CATALOG.values()]}
    @app.get(prefix+'/cards/{card_id}',response_class=HTMLResponse)
    def card_help(card_id:str):
        if card_id not in INTERFACE_CATALOG:raise HTTPException(404)
        return FileResponse(assets/'cards'/(card_id+'.html'),media_type='text/html')
    @app.get(prefix+'/api/config')
    def config():return {'backend':'interface-live','state_directory':str(app.state.runtime.root),
                        'max_concurrent_jobs':app.state.runtime.max_concurrent_jobs,
                        'migration_admission_gate_supported':True,
                        'admission_inhibited':app.state.runtime.admission_inhibited()}
    @app.get(prefix+'/api/sets')
    def sets():return {'sets':app.state.runtime.set_library()}
    @app.get(prefix+'/api/sets/{set_id}')
    def laser_set(set_id:str):return app.state.runtime.get_set(set_id)
    @app.get(prefix+'/modulation',include_in_schema=False)
    def modulation_page():return FileResponse(assets/'modulation.html',media_type='text/html')

    def modulation_store(laser,device):
        from ldcon.core.modulation import MetadataStore,identifier
        identifier(laser);identifier(device)
        definition=app.state.runtime.interface_repository.read('lasers',laser)['document']
        if device not in definition['devices']:raise FileNotFoundError(device)
        return MetadataStore(Path(state_dir or Path.cwd()/'data/web')/'device-metadata')

    @app.get(prefix+'/api/modulation/{laser}/{device}')
    def get_modulation(laser:str,device:str):return modulation_store(laser,device).get(laser,device)

    @app.put(prefix+'/api/modulation/{laser}/{device}')
    async def put_modulation(laser:str,device:str,request:Request):
        store=modulation_store(laser,device);data=await body(request)
        if set(data)!={'calibration','expected_revision'} or not isinstance(data['expected_revision'],str):raise ValueError('Expected calibration and revision')
        return store.put(laser,device,data['calibration'],data['expected_revision'])

    @app.post(prefix+'/api/modulation/{laser}/{device}/preview')
    async def preview_modulation(laser:str,device:str,request:Request):
        from ldcon.core.modulation import convert
        modulation_store(laser,device);data=await body(request)
        if set(data)!={'outputs','calibration'} or not isinstance(data['outputs'],dict):raise ValueError('Expected voltage inputs and calibration')
        return convert(data['outputs'],data['calibration'])

    @app.get(prefix+'/api/lasers')
    def lasers():return {'lasers':app.state.runtime.interface_repository.list('lasers')}
    @app.get(prefix+'/api/agent-providers')
    def agent_providers():
        from ldcon.agents.agent_providers import catalog
        return {'providers':catalog()}
    @app.get(prefix+'/api/lasers/{laser_id}')
    def laser_definition(laser_id:str):return app.state.runtime.interface_repository.read('lasers',laser_id)
    @app.get(prefix+'/api/lasers/{laser_id}/agent-workspace')
    def agent_workspace(laser_id:str):
        from ldcon.agents import agent_workspace as templates
        runtime=app.state.runtime
        runtime.interface_repository.read('lasers',laser_id)
        return templates.read(runtime.root,laser_id)
    @app.put(prefix+'/api/lasers/{laser_id}/agent-workspace')
    async def save_agent_workspace(laser_id:str,request:Request):
        from ldcon.agents import agent_workspace as templates
        data=await body(request)
        if set(data)!={'markdown','expected_revision'}:raise ValueError('Expected Markdown and its revision')
        def save():
            runtime=app.state.runtime
            with runtime.mutex:
                runtime.assert_admission_open()
                runtime.interface_repository.read('lasers',laser_id)
                return templates.save(runtime.root,laser_id,data['markdown'],data['expected_revision'])
        return await asyncio.to_thread(save)
    @app.get(prefix+'/api/lasers/{laser_id}/agent-sessions')
    def agent_sessions(laser_id:str):
        from ldcon.agents import agent_workspace as templates
        runtime=app.state.runtime
        runtime.interface_repository.read('lasers',laser_id)
        return {'laser_id':laser_id,'sessions':templates.sessions(runtime.root,laser_id,runtime.active_jobs())}
    @app.get(prefix+'/api/interface-decks/{deck_id}')
    def deck_definition(deck_id:str):return app.state.runtime.interface_repository.read('interface-decks',deck_id)
    @app.get(prefix+'/api/sets/{set_id}/decks/{deck_id}')
    def assigned_deck(set_id:str,deck_id:str):
        bundle=app.state.runtime.get_set(set_id)
        deck=next((d for d in bundle['deck_definitions'] if d['id']==deck_id),None)
        if deck is None:raise FileNotFoundError(deck_id)
        return {**deck,'set_revision':bundle['revision']}
    @app.get(prefix+'/api/dashboard/snapshot')
    def dashboard_view():
        from types import SimpleNamespace
        from ldcon.web.dashboard_snapshot import snapshot
        return snapshot(SimpleNamespace(state_file=app.state.runtime.root/'web-server.json'))

    @app.get(prefix+'/api/jobs/{job_id}/{snapshot_name}.yaml')
    def snapshot(job_id:str,snapshot_name:str):
        if snapshot_name not in ('laser','set','deck'):raise HTTPException(404)
        return FileResponse(app.state.runtime.directory(job_id)/(snapshot_name+'.yaml'),media_type='application/yaml')
    @app.get(prefix+'/api/interface-decks')
    def decks():return {'decks':app.state.runtime.interface_repository.list('interface-decks')}
    @app.get(prefix+'/api/configuration/{kind}/{name}')
    def read_configuration(kind:str,name:str):
        if kind not in configuration.KINDS:raise HTTPException(404)
        return app.state.runtime.interface_repository.read(kind,name)
    @app.put(prefix+'/api/configuration/{kind}/{name}')
    async def save_configuration(kind:str,name:str,request:Request):
        data=await body(request)
        return await asyncio.to_thread(configuration.save,app.state.runtime,kind,name,data.get('yaml'),data.get('expected_revision'),
            approval_id=data.get('approval_id'),maintainer_reference=data.get('maintainer_reference'),
            maintainer_token=request.headers.get('X-LDCon-Maintainer'),actor=actor(request))
    @app.post(prefix+'/api/sets/{set_id}/decks/{deck_id}/cards/{card_id}/validate')
    async def validate_card(set_id:str,deck_id:str,card_id:str,request:Request):
        data=await body(request)
        return await asyncio.to_thread(app.state.runtime.validate_card, set_id, deck_id, card_id,
                                       data.get('parameters', {}), data.get('expected_revision'))
    @app.post(prefix+'/api/sets/{set_id}/decks/{deck_id}/start',status_code=202)
    async def start_card(set_id:str,deck_id:str,request:Request):
        data=await body(request)
        if data.get('reservation') is not None:raise HTTPException(410,'Result-based reservations have been retired')
        return await asyncio.to_thread(app.state.runtime.start_set_deck,set_id,deck_id,data.get('expected_revision'),data.get('card_id'),data.get('parameters'),data.get('reservation'),migration_token=request.headers.get('X-LDCon-Migration'))
    @app.get(prefix+'/notifications',include_in_schema=False)
    def notification_settings():raise HTTPException(410,'Per-card notification settings are retired. Failures use the gateway agent and configured Teams webhook.')

    @app.api_route(prefix+'/api/fallback/{tail:path}',methods=['GET','PUT'])
    async def notification_api(tail:str,request:Request):
        # Fixed localhost companion only. Never proxy arbitrary URLs or devices.
        from urllib.request import Request as HttpRequest,build_opener,ProxyHandler
        from urllib.error import HTTPError,URLError
        if not re.fullmatch(r'(health|global|(?:card|job)/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?)',tail):
            raise HTTPException(404,'Not found')
        data=None
        if request.method=='PUT':data=json.dumps(await body(request)).encode()
        def forward():
            target=notification_endpoint+prefix+'/api/'+tail
            outgoing=HttpRequest(target,data=data,method=request.method,headers={'Content-Type':'application/json','X-LDCon-Client':'web'})
            try:
                with build_opener(ProxyHandler({})).open(outgoing,timeout=10) as response:return response.status,json.load(response)
            except HTTPError as error:
                try:value=json.loads(error.read())
                except ValueError:value={'detail':'Notification service rejected request'}
                return error.code,value
            except (OSError,URLError):return 503,{'detail':'Notification service is unavailable'}
        code,value=await asyncio.to_thread(forward)
        return JSONResponse(value,status_code=code)

    @app.get(prefix+'/api/jobs')
    def jobs(set_id:str=None,active_only:bool=False,operation:str=None,sweep_request_id:str=None):
        values=(app.state.runtime.active_jobs(set_id) if active_only else app.state.runtime.list_jobs(set_id))
        if sweep_request_id is not None and not re.fullmatch(r'[0-9a-f]{32}',sweep_request_id):raise ValueError('sweep_request_id must be a 32-hex id')
        return {'jobs':[j for j in values if (not active_only or j['status'] not in TERMINAL)
                        and (not operation or any(c.get('operation',c.get('card'))==operation for c in j.get('cards',[])))
                        and (sweep_request_id is None or j.get('sweep_request_id')==sweep_request_id)]}
    @app.get(prefix+'/api/history')
    def history(q:str='',limit:int=25,offset:int=0,set_id:str|None=None):
        if limit not in (10,25,50,100) or offset<0:raise ValueError('Invalid page size or offset')
        with app.state.runtime.mutex:
            by_id={j['id']:j for j in log_archive.archived(app.state.runtime) if 'id' in j}
            by_id.update({j['id']:j for j in app.state.runtime.list_jobs()})
            values=[j for j in by_id.values() if (set_id is None or j.get('set_id')==set_id) and q.casefold() in ' '.join(str(j.get(k,'')) for k in ('id','set_name','set_id','deck_id','status','created_utc')).casefold()]
            values.sort(key=lambda j:j.get('created_utc',''),reverse=True)
            return {'total':len(values),'jobs':values[offset:offset+limit]}
    @app.get(prefix+'/api/jobs/{job_id}')
    def job(job_id:str):return app.state.runtime.get_job(job_id)
    @app.get(prefix+'/api/jobs/{job_id}/telemetry')
    def telemetry(job_id:str):return app.state.runtime.telemetry(job_id)
    @app.get(prefix+'/api/jobs/{job_id}/report',response_class=HTMLResponse)
    def report(job_id:str):
        with app.state.runtime.mutex:
            from ldcon.web.job_report import evidence
            archived=app.state.runtime.root/'archive-index'/(safe_id(job_id)+'.json')
            if archived.is_file() and not archived.is_symlink():
                return HTMLResponse(log_archive.report_html(read_json(archived)))
            runtime=app.state.runtime
            job=runtime.get_job(job_id)
            response_report=runtime.directory(job_id)/'target-response.html'
            if (any(c.get('operation',c.get('card')) in ('measure.target_response','optimize.settling_criteria','optimize.target_feedforward','optimize.target_thermal','optimize.target_step') for c in job.get('cards',[]))
                    and response_report.is_file() and not response_report.is_symlink()):
                return FileResponse(response_report,media_type='text/html')
            return HTMLResponse(log_archive.report_html(job, evidence(runtime.directory(job_id))))
    @app.get(prefix+'/api/jobs/{job_id}/artifacts/{name}')
    def artifact(job_id:str,name:str):
        if name not in ('grafana-dashboard.json','grafana-manifest.json','mode-map.json','recovery.json','recovery-plan.json','recovery-test.json','operating-point-trace.json','target-response.json','measurement.json','window-center.json','operation-record.json'):raise HTTPException(404)
        if app.state.runtime.get_job(job_id)['status'] not in TERMINAL and name!='operation-record.json':raise HTTPException(409,'Export is still running')
        path=app.state.runtime.directory(job_id)/name
        if not path.is_file() or path.is_symlink():raise HTTPException(404)
        return FileResponse(path,media_type='application/json',filename=name)

    @app.post(prefix+'/api/jobs/{job_id}/stop')
    def stop(job_id:str):return app.state.runtime.stop(job_id)
    @app.post(prefix+'/api/jobs/{job_id}/sweep-trigger')
    async def sweep_trigger(job_id:str,request:Request):
        data=await body(request)
        try:return await asyncio.to_thread(app.state.runtime.sweep_trigger,job_id,data)
        except TriggerPending as pending:return JSONResponse({'detail':str(pending),**pending.record},status_code=504)
    @app.post(prefix+'/api/jobs/{job_id}/cards/{card_id}/start',status_code=202)
    async def online(job_id:str,card_id:str,request:Request):
        data=await body(request)
        if data.get('reservation') is not None:raise HTTPException(410,'Result-based reservations have been retired')
        return await asyncio.to_thread(app.state.runtime.request_online,job_id,card_id,data.get('parameters',{}))
    @app.get(prefix+'/api/jobs/{job_id}/hot-control')
    def hot_control_status(job_id:str):
        return app.state.runtime.hot_control_status(job_id)
    @app.post(prefix+'/api/jobs/{job_id}/hot-control',status_code=202)
    async def hot_control(job_id:str,request:Request):
        data=await body(request)
        return await asyncio.to_thread(app.state.runtime.request_hot_control,job_id,data)
    @app.get(prefix+'/api/jobs/{job_id}/laser-live')
    def laser_live(job_id:str):
        return app.state.runtime.laser_live(job_id)
    @app.get(prefix+'/api/jobs/{job_id}/runtime-parameters')
    def runtime_parameters(job_id:str):
        return app.state.runtime.runtime_parameters(job_id)
    @app.post(prefix+'/api/jobs/{job_id}/runtime-parameters',status_code=202)
    async def inject_parameters(job_id:str,request:Request):
        data=await body(request)
        extra={k:data.pop(k) for k in ('approval_id','maintainer_reference') if k in data}
        return await asyncio.to_thread(lambda:app.state.runtime.inject_parameters(job_id,data,
            maintainer_token=request.headers.get('X-LDCon-Maintainer'),actor=actor(request),**extra))
    @app.post(prefix+'/api/jobs/{job_id}/online-tuning/stop')
    def stop_online(job_id:str):return app.state.runtime.stop_online(job_id)
    @app.get(prefix+'/api/retention')
    def retention():return {**log_archive.settings_info(app.state.runtime),'last_run':read_json(app.state.runtime.root/'archive-status.json',{})}
    @app.put(prefix+'/api/retention')
    async def retention_save(request:Request):
        data=await body(request)
        with app.state.runtime.mutex:return log_archive.configure(app.state.runtime,data)
    @app.post(prefix+'/api/retention/archive')
    async def archive():return await asyncio.to_thread(log_archive.archive_finished,app.state.runtime)
    from ldcon.web.window_api import install_routes as install_window_routes
    install_window_routes(app,prefix,body)
    from ldcon.web.operation_api import install_routes as install_operation_routes
    install_operation_routes(app,prefix,body,actor)
    from ldcon.web.operating_mode_api import install_routes as install_mode_routes
    install_mode_routes(app,prefix,body)
    @app.api_route(prefix+'/api/reservations',methods=['GET','POST'])
    @app.api_route(prefix+'/api/reservations/{tail:path}',methods=['GET','POST'])
    @app.get(prefix+'/reservations',include_in_schema=False)
    def retired_reservations(tail:str=''):
        raise HTTPException(410,'Result-based reservations have been retired; existing audit records are retained')
    from ldcon.web.failure_api import install_routes as install_failure_routes
    install_failure_routes(app,prefix,assets,body)
    from ldcon.web.agent_approval_api import install_routes as install_approval_routes
    install_approval_routes(app,prefix,body)
    from ldcon.web.device_parameters_api import install_routes as install_device_parameter_routes
    install_device_parameter_routes(app,prefix,body,actor)
    from ldcon.web.lock_target_api import install_routes as install_lock_target_routes
    install_lock_target_routes(app,prefix,body)
    from ldcon.web.sweep_api import install_routes as install_sweep_routes      # labscript sweep API (contract v1, 2026-10-22)
    install_sweep_routes(app,prefix,body)
    from ldcon.web.shot_analysis_api import install_routes as install_analysis_routes   # sweep API v1.5 post-shot analysis
    install_analysis_routes(app,prefix)
    from ldcon.agents.agent_profiles import install_routes as agent_profile_routes
    agent_profile_routes(app,prefix,body)
    from ldcon.web.agent_task_api import install_routes as agent_task_routes
    agent_task_routes(app,prefix,body)
    from ldcon.jobs.target_response_control import install_routes as target_response_routes
    target_response_routes(app,prefix,body)
    from ldcon.agents.agent_background import install_routes as agent_background_routes
    agent_background_routes(app,prefix,body)
    from ldcon.agents.agent_history import install_routes as agent_history_routes
    agent_history_routes(app,prefix,body)
    from ldcon.web.agent_settings_api import install_routes as agent_settings_routes
    agent_settings_routes(app,prefix,assets,body)
    from ldcon.web.teams_destinations_api import install_local as install_teams_destinations
    install_teams_destinations(app,prefix)
    return app


def main():
    import uvicorn
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host',default='127.0.0.1');parser.add_argument('--port',type=int,default=8767)
    parser.add_argument('--state-dir',type=Path,default=Path.cwd()/'data/web')
    parser.add_argument('--state-file',type=Path,default=Path.cwd()/'data/web-server.json')
    parser.add_argument('--max-concurrent-jobs',type=int,default=12)
    parser.add_argument('--rotate-token',action='store_true')
    args=parser.parse_args();previous=read_json(args.state_file,{})
    match=re.fullmatch(r'/s/([A-Za-z0-9_-]{24,80})/',previous.get('path',''))
    token=match.group(1) if match and not args.rotate_token else None
    app=create_app(args.state_dir,token=token,max_concurrent_jobs=args.max_concurrent_jobs)
    args.state_file.parent.mkdir(parents=True,exist_ok=True)
    atomic_json(args.state_file,{'pid':os.getpid(),'host':args.host,'port':args.port,'path':app.state.prefix+'/','backend':'interface-live'})
    uvicorn.run(app,host=args.host,port=args.port,access_log=False)

if __name__=='__main__':main()
