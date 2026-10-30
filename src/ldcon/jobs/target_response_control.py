"""Study-only policy queue and exact-CAS target binding to the parent owner.

No second lease and no direct device path. Ordinary target requests omit the
study token and the owner invalidates the temporary policy before applying them.
"""
import copy
import math
import time
import uuid
from pathlib import Path

from ldcon.core.records import atomic_json,read_json
from ldcon.jobs.response_algorithms import ALGORITHMS, POLICY_OPERATIONS


def _single_response(cards):
    return len(cards)==1 and cards[0].get("operation",cards[0].get("card")) in ALGORITHMS


def _id(value):
    if not isinstance(value,str) or len(value)!=32 or any(c not in '0123456789abcdef' for c in value):
        raise ValueError('Invalid target-response job/request identity')
    return value


def active_study(runtime,job_id,*,cleanup=False):
    _id(job_id)
    job=runtime.get_job(job_id)
    if job['status'] not in (('running','stopping') if cleanup else ('running',)) or not _single_response(job['cards']):
        raise ValueError('A running target-response study is required')
    laser=job['laser']['id']
    active=runtime.active_jobs()
    studies=[j for j in active if j['status'] in ('running','stopping') and (j.get('laser') or {}).get('id')==laser
        and any(c.get('operation',c.get('card')) in ALGORITHMS for c in j['cards'])]
    if len(studies)!=1 or studies[0]['id']!=job_id:raise ValueError('Unambiguous active target-response study required')
    parents=[j for j in active if j['status']=='running' and (j.get('laser') or {}).get('id')==laser
        and any(c.get('operation',c.get('card')) in ('servo.ecdl_steady_lock','servo.native_current') for c in j['cards'])]
    if len(parents)!=1:raise ValueError('Unambiguous running parent lock required')
    return job,parents[0]


def request_policy(runtime,job_id,value):
    fields={'status':{'action'},'restore':{'action','expected_revision'},
            'set':{'action','expected_revision','scale','step_hz','duration_s'}}
    if (not isinstance(value,dict) or value.get('action') not in fields
            or set(value)-({'thermal'} if value.get('action')=='set' else set())!=fields[value['action']]):
        raise ValueError('Unknown study policy fields')
    if value['action']!='status' and (not isinstance(value['expected_revision'],str) or not value['expected_revision']):
        raise ValueError('Explicit owner policy revision required')
    if value['action']=='set':
        if 'thermal' in value and (not isinstance(value['thermal'],dict)
                or set(value['thermal'])!={'tec_gain','supervision_interval_s'}
                or any(type(v) not in (int,float) or not math.isfinite(v) or v<=0 for v in value['thermal'].values())):
            raise ValueError('Thermal study requires finite positive TEC gain and interval')
        if any(type(value[k]) not in (int,float) or not math.isfinite(value[k]) or value[k]<=0
               for k in ('scale','step_hz','duration_s')) or value['duration_s']>900:
            raise ValueError('Finite positive scale/step and duration <=900 seconds required')
    with runtime.mutex:
        job,parent=active_study(runtime,job_id,cleanup=value['action'] in ('status','restore'))
        if job['cards'][0].get('operation',job['cards'][0].get('card')) not in POLICY_OPERATIONS:
            raise ValueError('This algorithm does not admit temporary FF/TEC policy changes')
        directory=runtime.directory(parent['id']);study=runtime.directory(job_id)
        if any(directory.glob('target-ff-policy-request-*.json')):raise ValueError('A policy request is pending; no replay')
        if value['action']!='status' and any(directory.glob('target-ff-policy-claimed-*.json')):
            raise ValueError('A policy request has an unconfirmed result; inspect it before another mutation')
        if any(directory.glob('laser-reload-request-*.json')):raise ValueError('A target/configuration change is pending')
        previous=read_json(study/'target-ff-policy.json',{})
        request=dict(value,request_id=uuid.uuid4().hex,study_id=job_id,parent_id=parent['id'],
            laser_revision=runtime.interface_repository.read('lasers',job['laser']['id'])['revision'],
            expires_unix_s=time.time()+30)
        if value['action']!='status' and (previous.get('status')!='completed'
                or previous.get('parent_id')!=parent['id'] or previous.get('study_id')!=job_id):
            raise ValueError('Read this study parent policy before changing it')
        if value['action'] in ('set','restore'):
            old=previous['result'];request['study_token']=(old.get('session') or {}).get('study_token')
            if value.get('expected_revision')!=old.get('revision'):raise ValueError('Study policy revision changed')
        atomic_json(study/'target-ff-policy-intent.json',request)
        atomic_json(directory/('target-ff-policy-request-'+request['request_id']+'.json'),request)
        return dict(status='queued',request_id=request['request_id'])


def _study_record(live,study_id,*,cleanup=False):
    study=live.directory.parent/_id(study_id)
    if study.is_symlink() or any((study/name).is_symlink() for name in ('job.json','progress.json','target-ff-policy.json')):
        raise ValueError('Study evidence must not use symlinks')
    meta=read_json(study/'job.json',{});progress=read_json(study/'progress.json',{})
    if (meta.get('id')!=study_id or (meta.get('laser') or {}).get('id')!=live.laser['id']
            or not _single_response(meta.get('cards',[]))
            or meta['cards'][0].get('operation',meta['cards'][0].get('card')) not in POLICY_OPERATIONS):
        raise ValueError('Target response study identity or laser differs from the parent')
    if progress.get('status',meta.get('status')) not in (('running','stopping') if cleanup else ('running',)):
        raise ValueError('Study ended before policy/target claim')
    if not cleanup and ((study/'cancel.request').exists() or (live.directory/'cancel.request').exists()):
        raise ValueError('Study or parent cancellation takes priority')
    return study


def poll_policy(live,busy):
    """Called by the existing lock worker under its existing owner lease."""
    paths=sorted(live.directory.glob('target-ff-policy-request-*.json'))
    if not paths or busy:return
    path=paths[0]
    if path.is_symlink() or path.stat().st_size>65536:
        path.unlink(missing_ok=True);return
    request=read_json(path,{})
    if not isinstance(request,dict):path.unlink(missing_ok=True);return
    study_id=request.get('study_id','')
    if not isinstance(study_id,str) or len(study_id)!=32 or any(c not in '0123456789abcdef' for c in study_id):
        path.unlink(missing_ok=True);return
    study=live.directory.parent/study_id
    result=dict(status='rejected',request_id=request.get('request_id'),hardware_write=False)
    claimed=None;rpc_entered=False;trusted_study=False
    try:
        action=request.get('action')
        if action not in ('status','set','restore'):raise ValueError('Unknown study policy action')
        _id(request.get('request_id'))
        if path.name!='target-ff-policy-request-'+request['request_id']+'.json' or path.is_symlink():
            raise ValueError('Policy request identity mismatch')
        study=_study_record(live,study_id,cleanup=action in ('status','restore'))
        trusted_study=True
        if request.get('parent_id')!=live.directory.name or request.get('laser_revision')!=live.revision:
            raise ValueError('Parent or laser revision changed')
        expires=request.get('expires_unix_s')
        if type(expires) not in (int,float) or not math.isfinite(expires) or time.time()>expires:
            raise ValueError('Policy request expired')
        if list(live.directory.glob('laser-reload-request-*.json')):raise ValueError('Foreground configuration takes priority')
        if action!='status' and list(live.directory.glob('target-ff-policy-claimed-*.json')):
            raise ValueError('A previous policy result is unconfirmed; no replay')
        port=live.lock or live.port
        # Remove from the runnable queue BEFORE entering the RPC. A crash or
        # lost reply must not replay a set/restore when the worker resumes.
        claimed=path.with_name('target-ff-policy-claimed-'+request['request_id']+'.json')
        path.replace(claimed)
        rpc_entered=True
        if action=='status':receipt=port.target_ff_study_status()
        elif action=='restore':receipt=port.target_ff_study_restore(request['expected_revision'],request['study_token'])
        else:receipt=port.target_ff_study_set(request['expected_revision'],scale=request['scale'],
            step_hz=request['step_hz'],duration_s=request['duration_s'],study_token=request.get('study_token'),
            **({'thermal':request['thermal']} if 'thermal' in request else {}))
        result.update(status='completed',result=receipt,parent_id=live.directory.name,study_id=study_id,observed_unix_s=time.time())
    except Exception as error:
        # A policy set/restore only changes temporary memory, never outputs.
        # Still retain an unknown result and never automatically retry it.
        result.update(status='unknown' if rpc_entered else 'rejected',
                      error_type=type(error).__name__,reason=str(error)[:500])
    result_path=(study/'target-ff-policy.json' if trusted_study else
                 live.directory/('target-ff-policy-rejected-'+path.stem.removeprefix('target-ff-policy-request-')+'.json'))
    atomic_json(result_path,result)
    path.unlink(missing_ok=True)
    if claimed is not None and result['status']=='completed':claimed.unlink(missing_ok=True)


def save_target(runtime,job_id,value):
    from ldcon.core import configuration
    from ldcon.core.interface_schema import load_mapping
    from ldcon.agents.agent_configuration import changes
    if not isinstance(value,dict) or set(value)!={'yaml','expected_revision'}:raise ValueError('Expected exact target CAS')
    with runtime.mutex:
        job,parent=active_study(runtime,job_id);laser=job['laser']['id']
        if job['cards'][0].get('operation',job['cards'][0].get('card')) not in POLICY_OPERATIONS:
            raise ValueError('This algorithm does not admit temporary FF/TEC policy changes')
        current=runtime.interface_repository.read('lasers',laser)
        if current['revision']!=value['expected_revision']:raise ValueError('Laser configuration superseded')
        if current['document'].get('allow_output') is not True:raise ValueError('Laser output writes are disabled')
        candidate=load_mapping(value['yaml'])
        if set(changes(current['document'],candidate))!={('target','frequency_hz')}:
            raise ValueError('Study route permits one canonical target change only')
        from ldcon.jobs.lock_target import check_change
        check_change(current['document'],candidate['target']['frequency_hz'])
        policy=read_json(runtime.directory(job_id)/'target-ff-policy.json',{})
        token=(policy.get('result',{}).get('session') or {}).get('study_token')
        if (policy.get('status')!='completed' or policy.get('parent_id')!=parent['id']
                or policy.get('study_id')!=job_id or not token):
            raise ValueError('Verified temporary owner policy required')
        binding=dict(study_id=job_id,parent_id=parent['id'],study_token=token)
        return configuration.save(runtime,'lasers',laser,value['yaml'],value['expected_revision'],
            actor='target-response:'+job_id,target_response_binding=binding)


def bound_token(live,request):
    binding=request.get('authority',{}).get('target_response_binding')
    if binding is None:return None
    if not isinstance(binding,dict) or set(binding)!={'study_id','parent_id','study_token'}:
        raise ValueError('Invalid study target binding')
    study=_study_record(live,binding['study_id'])
    if (binding['parent_id']!=live.directory.name or request.get('job_id')!=live.directory.name
            or request.get('laser_id')!=live.laser['id'] or request.get('revision_before')!=live.revision
            or [row.get('path') for row in request.get('changes',[])]!=['target.frequency_hz']):
        raise ValueError('Study target binding was superseded')
    policy=read_json(study/'target-ff-policy.json',{})
    token=(policy.get('result',{}).get('session') or {}).get('study_token')
    if (policy.get('status')!='completed' or policy.get('parent_id')!=live.directory.name
            or policy.get('study_id')!=binding['study_id'] or not token or token!=binding['study_token']):
        raise ValueError('Study target policy token changed')
    return token


def install_routes(app,prefix,body):
    import asyncio
    from fastapi import Request
    @app.post(prefix+'/api/target-response/{job_id}/adopt')
    async def adopt(job_id:str, request:Request):
        from ldcon.jobs.study_adoption import apply
        return await asyncio.to_thread(apply,app.state.runtime,job_id,await body(request))

    @app.post(prefix+'/api/target-response/{job_id}/policy')
    async def policy(job_id:str,request:Request):
        return await asyncio.to_thread(request_policy,app.state.runtime,job_id,await body(request))
    @app.get(prefix+'/api/target-response/{job_id}/policy')
    def status(job_id:str):
        active_study(app.state.runtime,job_id,cleanup=True)
        return read_json(app.state.runtime.directory(job_id)/'target-ff-policy.json',{})
    @app.put(prefix+'/api/target-response/{job_id}/target')
    async def target(job_id:str,request:Request):
        return await asyncio.to_thread(save_target,app.state.runtime,job_id,await body(request))
