"""Scoped, idempotent task admission without blocking the resident conversation."""
import fcntl
import time
from pathlib import Path
from experiment_core.records import atomic_json, digest, safe_id
from ldcon.core.records import read_json


def admission_path(root, job_id):
    return Path(root)/'agents'/'task-admissions'/(safe_id(job_id)+'.json')


def pin_invocation(root, job_id, card, parameters, *, laser_id, meta=None, deck=None, invocations=None):
    """Host-only: call before publishing launch.request for every task entrypoint.

    API delegation already publishes this snapshot before its gateway call;
    ordinary card admission uses the same record and exact profile resolution.
    """
    from ldcon.agents.agent_profiles import select_task
    if card['operation'] in ('study.target_response','optimize.settling_criteria','optimize.target_feedforward','optimize.target_thermal'):
        from ldcon.jobs.target_response import parameters as normalize
        if normalize(card,parameters)['execution_mode']!='agent':return None
        if not card.get('agent_execution') and not parameters.get('execution_profile'):return None
    elif card['operation']=='prepare.ecdl_recovery':
        from ldcon.jobs.ecdl_recovery import parameters as normalize
        if normalize(card,parameters)['execution_mode']!='agent':return None
        from ldcon.jobs.recovery_decisions import execution_card
        card=execution_card(card)
    elif card['operation'] in ('agent.task','agent.history_review'):
        from ldcon.agents.agent_task import parameters as normalize
    else:return None
    params=normalize(card,parameters)
    path=admission_path(root,job_id);path.parent.mkdir(parents=True,exist_ok=True)
    if path.is_symlink() or path.parent.is_symlink():raise ValueError('Unsafe task admission path')
    def existing():
        old=read_json(path,{})
        if old:
            if (old.get('parameters')!=params or old.get('card_id')!=card['id'] or old.get('laser_id')!=laser_id):
                raise ValueError('Existing task admission differs from frozen invocation')
        return old
    # Avoid reacquiring the API caller's already-held non-reentrant file lock.
    old=existing()
    if old:return old
    lock_path=path.with_suffix('.lock')
    if lock_path.is_symlink():raise ValueError('Unsafe task admission lock')
    with lock_path.open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        old=existing()
        if old:return old
        selected,selection=select_task(root,card,params,meta=meta or dict(set_id=laser_id,deck_id=''),deck=deck,invocations=invocations)
        record=dict(state='direct',task_id=job_id,laser_id=laser_id,card_id=card['id'],parameters=params,
                    profile=selected,selection=selection,created_unix_s=time.time())
        atomic_json(path,record)
        return record


def start(runtime, value):
    from ldcon.core.interface_schema import parameters
    from ldcon.agents.agent_profiles import select_task
    from ldcon.web.web_runtime import ConflictError
    required={'source_job_id','card_id','prompt','request_id'}
    if not isinstance(value,dict) or not required<=value.keys() or set(value)-required-{'profile','lookback_s'}:
        raise ValueError('Invalid task admission')
    for name in ('source_job_id','card_id','request_id'):safe_id(value[name])
    if not isinstance(value['prompt'],str) or len(value['prompt'].encode())>16000:
        raise ValueError('Invalid task prompt')
    semantic={**value,'profile':value.get('profile','')}
    if not isinstance(semantic['profile'],str):raise ValueError('Invalid task profile')
    if semantic['profile']:safe_id(semantic['profile'])
    identifier=digest([value['source_job_id'],value['request_id']])[:32]
    path=admission_path(runtime.root,identifier)
    path.parent.mkdir(parents=True,exist_ok=True)
    if path.parent.is_symlink() or path.is_symlink():raise ValueError('Unsafe task admission path')
    lock_path=path.with_suffix('.lock')
    if lock_path.is_symlink():raise ValueError('Unsafe task admission lock')
    with lock_path.open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        old=read_json(path,{})
        if old:
            if old.get('request')!=semantic:raise ConflictError('Task request ID binds different content')
            if old.get('state')!='accepted':raise ConflictError('Prior task admission is unconfirmed; no automatic replay')
            return old['receipt']
        with runtime.mutex:
            source=runtime.get_job(value['source_job_id'])
            if source.get('status')!='running':raise ValueError('Task source is not active')
            sources=[c for c in source.get('cards',[]) if c.get('operation',c.get('card')) in ('agent.resident','agent.monitor')]
            if len(sources)!=1:raise ValueError('Only one resident or monitor may delegate; no recursive tasks')
            parent=sources[0];operation=parent.get('operation',parent.get('card'))
            bundle=runtime.get_set(source['set_id'])
            deck=next((d for d in bundle['deck_definitions'] if d['id']==source['deck_id']),None)
            card=next((c for c in deck['document']['cards'] if c['id']==value['card_id']),None) if deck else None
            if not card or card['operation'] not in ('agent.task','agent.history_review'):
                raise ValueError('Select a registered task composition card')
            override=dict(prompt=value['prompt'],request_delegation=True)
            if value.get('profile'):override['execution_profile']=value['profile']
            if 'lookback_s' in value:override['lookback_s']=value['lookback_s']
            if operation=='agent.monitor':
                override['monitor_job_id']=source['id']
                if parent.get('params',{}).get('agent_card_id'):override['agent_card_id']=parent['params']['agent_card_id']
            else:override['agent_card_id']=parent['id']
            params=parameters(card,{**deck.get('invocation_parameters',{}).get(card['id'],{}),**override})
            selected,selection=select_task(runtime.root,card,params,meta=source,deck=deck['document'],invocations=deck['invocation_parameters'])
            admission=dict(state='launching',request=semantic,task_id=identifier,source_job_id=source['id'],
                           laser_id=bundle['laser_definition']['document']['id'],parameters=params,
                           card_id=card['id'],profile=selected,selection=selection,created_unix_s=time.time())
            atomic_json(path,admission)
            try:receipt=runtime.start_interface_card(bundle,source['deck_id'],card['id'],override,job_id=identifier)
            except BaseException:
                atomic_json(path,{**admission,'state':'unknown'})
                raise
            atomic_json(path,{**admission,'state':'accepted','receipt':receipt})
            return receipt


def task_permission(runtime, job_id, laser_id, tool):
    from ldcon.core.interface_schema import load_mapping
    job=runtime.get_job(safe_id(job_id));directory=runtime.directory(job_id)
    if job.get('status')!='running' or job.get('laser',{}).get('id')!=laser_id:
        raise ValueError('Task is inactive or belongs to another laser')
    invocation=read_json(directory/'invocation.json',{})
    deck=load_mapping((directory/'interface-deck.yaml').read_text())
    card=next((c for c in deck['cards'] if c['id']==invocation.get('card_id')),None)
    if not card or card['operation'] not in ('agent.task','agent.history_review','prepare.ecdl_recovery','study.target_response','optimize.settling_criteria','optimize.target_feedforward','optimize.target_thermal'):
        raise ValueError('Expected a frozen task invocation')
    phase=read_json(directory/'agent-task-phase.json',{})
    if (phase.get('task_id')!=job_id or phase.get('laser_id')!=laser_id or phase.get('depth')!=1
            or phase.get('expires_unix_s',0)<=time.time() or phase.get('phase') not in ('probe','work')):
        raise ValueError('Task permission phase is inactive')
    allowed=['agent_capability_probe'] if phase['phase']=='probe' else phase['allowed_tools']
    if tool not in allowed:raise ValueError('Tool is outside the current task phase')
    return dict(task_id=job_id,laser_id=laser_id,phase=phase['phase'],allowed=True)


def task_probe(runtime, job_id, value):
    from ldcon.agents.agent_tools import Toolbox
    from ldcon.agents.agent_bundle import capability_probe
    if not isinstance(value,dict) or set(value)!={'laser_id','nonce'}:raise ValueError('Invalid task probe')
    permission=task_permission(runtime,job_id,value['laser_id'],'agent_capability_probe')
    if permission['phase']!='probe':raise ValueError('Task probe phase is closed')
    # This endpoint is a non-actuating host read/write. It never changes phase.
    return capability_probe(Toolbox(runtime.directory(job_id)/'agent-tools-context.json'),{'nonce':value['nonce']})


def save_quality(runtime, job_id, value):
    """Recheck task scope at host dispatch, not only before an HTTP round trip."""
    from ldcon.core.interface_schema import load_mapping
    from ldcon.agents.agent_configuration import changes,quality_change
    from ldcon.core import configuration
    if not isinstance(value,dict) or set(value)!={'yaml','expected_revision'}:
        raise ValueError('Expected a revision-checked quality-only save')
    with runtime.mutex:
        job=runtime.get_job(safe_id(job_id));laser_id=job['laser']['id']
        task_permission(runtime,job_id,laser_id,'laser_quality_update')
        before=runtime.interface_repository.read('lasers',laser_id)['document']
        after=load_mapping(value['yaml'])
        differences=changes(before,after)
        if not differences or any(not quality_change(path,before,after) for path in differences):
            raise ValueError('Task configuration route accepts existing quality thresholds only')
        return configuration.save(runtime,'lasers',laser_id,value['yaml'],value['expected_revision'],actor='agent-task:'+job_id)


def call(box, name, args):
    from ldcon.agents.agent_cards import _api
    if name == 'agent_task_start':
        if box.context.get('task_depth', 0):
            raise ValueError('Task agents cannot recursively delegate')
        payload = dict(source_job_id=box.meta['id'], **args)
        return _api(box, 'agent-tasks/start', 'POST', payload)
    job_id = safe_id(args['job_id'])
    result = _api(box, 'agent-tasks/'+job_id)
    if result.get('laser_id') != box._laser_id:
        raise ValueError('Task belongs to another laser')
    return result


def install_routes(app, prefix, body):
    from fastapi import Request
    @app.post(prefix+'/api/agent-tasks/start')
    async def task_start(request: Request):return start(app.state.runtime, await body(request))

    @app.get(prefix+'/api/agent-tasks/{job_id}/permission')
    def permission(job_id: str, laser_id: str, tool: str):
        return task_permission(app.state.runtime,job_id,laser_id,tool)

    @app.post(prefix+'/api/agent-tasks/{job_id}/probe')
    async def probe(job_id: str, request: Request):
        return task_probe(app.state.runtime,job_id,await body(request))

    @app.put(prefix+'/api/agent-tasks/{job_id}/quality')
    async def quality(job_id: str,request: Request):
        import asyncio
        return await asyncio.to_thread(save_quality,app.state.runtime,job_id,await body(request))

    @app.post(prefix+'/api/agent-tasks/{job_id}/resident-quality')
    async def resident_quality(job_id: str, request: Request):
        import asyncio
        from ldcon.agents.resident_quality_apply import apply
        return await asyncio.to_thread(apply,app.state.runtime,job_id,await body(request))

    @app.get(prefix+'/api/agent-tasks/{job_id}/resident-quality')
    def resident_quality_status(job_id: str, request_id: str):
        from ldcon.agents.resident_quality_apply import status
        return status(app.state.runtime,job_id,request_id)

    @app.get(prefix+'/api/agent-tasks/{job_id}')
    def task_status(job_id: str):
        job = app.state.runtime.get_job(job_id)
        directory = app.state.runtime.directory(job_id)
        return dict(job_id=job_id, laser_id=job.get('laser', {}).get('id'), status=job['status'],
                    task=read_json(directory/'agent-task-status.json', {}),
                    monitor=read_json(directory/'agent-monitor-status.json', {}))
