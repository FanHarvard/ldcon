"""Host gateway for approved background current experiments; never raw I/O."""
import fcntl
import json
import math
import os
from pathlib import Path
import time

from experiment_core.records import atomic_json, safe_id
from ldcon.core.records import read_json
from ldcon.agents.operating_mode_client import request as mode_read, revision as mode_revision, describe as owner_describe


def control_root(root):
    value = os.environ.get('LDCON_ADAPTIVE_ROOT')
    if not value:
        raise ValueError('Background adjustment unavailable: trusted adaptive root is not configured')
    path = Path(value)
    if (not path.is_absolute() or not path.is_dir() or path.resolve() != path
            or path.is_relative_to(Path(root).resolve())):
        raise ValueError('Trusted adaptive root must be outside the deployment')
    return path


def revoke_monitor(root, laser_id, monitor_id, generation):
    """Revoke only this monitor's grants. Owner restores independently of model."""
    try:directory = control_root(root)/safe_id(laser_id)
    except ValueError:return
    grant_path=directory/'monitors'/(safe_id(monitor_id)+'.json')
    grant=read_json(grant_path,{})
    if isinstance(grant,dict) and grant.get('generation')==generation:
        atomic_json(grant_path,{**grant,'enabled':False})
    for path in directory.glob('*/background-control.json'):
        value = read_json(path, {})
        if isinstance(value,dict) and value.get('monitor_id') == monitor_id and value.get('generation') == generation:
            atomic_json(path, {**value, 'enabled': False})


def revoke_task(root, laser_id, task_id):
    try:directory = control_root(root)/safe_id(laser_id)
    except ValueError:return
    for selector in directory.glob('*/adaptive-experiment.json'):
        value = read_json(selector, {})
        if not isinstance(value,dict) or value.get('id') != 'task-'+task_id:
            continue
        control = selector.parent/'background-control.json'
        existing = read_json(control, {})
        grant = value.get('background', {})
        if isinstance(existing,dict) and isinstance(grant,dict) and all(existing.get(k) == grant.get(k) for k in ('monitor_id','generation')):
            atomic_json(control, {**existing, 'enabled': False})


CONTROLLERS = frozenset(('servo.ecdl_steady_lock','servo.native_current'))


def _capabilities(operation='servo.ecdl_steady_lock',device=None):
    from ldcon.core.adjustment_registry import capabilities
    return capabilities(operation,device)


def _adopted_capabilities(runtime, operation, laser):
    """Runtime-adjustment capabilities of the profile the rig owner has ADOPTED now (2026-10-28).

    The controller job's laser.yaml is the copy frozen at lock start; a live-adopted envelope change
    (settings_adopt of background_adjustments.*) never reaches it. Read the owner's own report instead.
    Older owners report only their device revision: the saved definition is used when its device digest
    equals that revision (it then is the adopted device). Otherwise the parameter families are reported
    unavailable with the reason (data validity), never from the stale copy.
    """
    rigs=[(k,v) for k,v in laser.get('devices',{}).items() if v.get('type')=='ecdl.rig.owner']
    if operation!='servo.ecdl_steady_lock' or len(rigs)!=1:
        return _capabilities(operation,rigs[0][1] if len(rigs)==1 else None)
    key=rigs[0][0]
    try:
        described=owner_describe(laser)
        reported=described.get('background_capabilities')
        if isinstance(reported,dict) and isinstance(reported.get('supported_adjustments'),list):
            return dict(reported,capability_source=dict(source='owner_adopted',revision=described.get('revision')))
        from ldcon.core.interface_schema import digest
        saved=runtime.interface_repository.read('lasers',laser['id'])['document']['devices'][key]
        if digest(saved)!=described.get('revision'):
            raise ValueError('Saved definition is not the owner-adopted device (adoption pending or refused)')
        return dict(_capabilities(operation,saved),capability_source=dict(source='saved_equals_owner_adopted',revision=described['revision']))
    except (OSError,ValueError,KeyError,TypeError,AttributeError) as error:
        result=_capabilities(operation,None)
        reason='Owner-adopted runtime envelope unavailable ('+type(error).__name__+'); no frozen copy is reported'
        for item in result['parameter_adjustments'].values():item['reason']=reason
        return dict(result,capability_source=dict(source='unavailable',reason=reason))


def _live_status(status):
    return bool(status.get('active') or status.get('state') in
                ('idle','waiting_for_decision','measuring','waiting_for_measurement','waiting_recovery','awaiting_replan'))


def route(runtime, task_id, data=None):
    if data is not None and not isinstance(data, dict):
        raise ValueError('Invalid background step')
    from ldcon.core.interface_schema import load_mapping
    task_dir = runtime.directory(safe_id(task_id))
    task = runtime.get_job(task_id)
    invocation = read_json(task_dir/'invocation.json')
    params = invocation['parameters']
    monitor_id = params.get('monitor_job_id')
    if (params.get('instruction_profile') != 'adjustment'
            or not any(c.get('operation',c.get('card'))=='agent.task' for c in task.get('cards',[]))):
        raise ValueError('Task has no background adjustment grant')
    laser_id = load_mapping((task_dir/'laser.yaml').read_text())['id']
    manual=not monitor_id
    if manual:
        monitor_id='task-'+task_id
        monitor=task
    else:
        monitor_dir = runtime.directory(safe_id(monitor_id))
        monitor = runtime.get_job(monitor_id)
        if (monitor.get('set_id') != task.get('set_id')
                or load_mapping((monitor_dir/'laser.yaml').read_text())['id'] != laser_id
                or not any(c.get('operation', c.get('card')) == 'agent.monitor' for c in monitor.get('cards', []))):
            raise ValueError('Monitor grant belongs to another laser or operation')
    root=control_root(runtime.root)
    grant_path=root/safe_id(laser_id)/'monitors'/(safe_id(monitor_id)+'.json')
    if any(path.is_symlink() for path in (grant_path,grant_path.parent,grant_path.parent.parent)):
        raise ValueError('Trusted monitor grant must not be a symlink')
    grant=read_json(grant_path,{})
    if not isinstance(grant,dict):grant={}
    if manual:
        from ldcon.agents.manual_adjustment_grant import plan
        from experiment_core.records import digest
        expected=plan(task_dir,params)
        if (grant.get('source')!='manual_task' or grant.get('task_id')!=task_id
                or grant.get('bounds')!=expected or grant.get('parameters_hash')!=digest(expected)):
            raise ValueError('Manual grant does not match the frozen approved plan')
    updated=grant.get('last_seen');max_age=grant.get('max_age_s')
    live_grant=bool(task.get('status')=='running' and monitor.get('status')=='running'
        and grant.get('enabled') is True and isinstance(grant.get('bounds'),dict) and grant['bounds'].get('enabled') is True
        and isinstance(grant.get('generation'),str) and grant['generation']
        and isinstance(grant.get('parameters_hash'),str) and grant['parameters_hash']
        and type(updated) in (int,float) and math.isfinite(updated)
        and type(max_age) in (int,float) and math.isfinite(max_age) and 0<max_age<=180
        and 0<=time.time()-updated<=max_age)
    # Runtime parameter receipt, rather than editable model text, determines the
    # enabled envelope. Physical range/step/slew validation remains in the owner.
    bounds = grant.get('bounds',{})
    jobs = runtime.active_jobs(set_id=task['set_id'])
    controllers = [j for j in jobs if j.get('status') == 'running' and any(
        c.get('operation', c.get('card')) in CONTROLLERS for c in j.get('cards', []))]
    if len(controllers) != 1:
        return dict(status='unavailable', reason='One running supported controller is required; no lock is started',
                    supported_adjustments=[],unsupported_adjustments=['current','pi','slow_loop','feedforward'])
    controller = controllers[0]
    operation=next(c.get('operation',c.get('card')) for c in controller['cards']
                   if c.get('operation',c.get('card')) in CONTROLLERS)
    laser=load_mapping((runtime.directory(safe_id(controller['id']))/'laser.yaml').read_text())
    if laser['id']!=laser_id:
        raise ValueError('Controller belongs to another laser')
    capabilities=_adopted_capabilities(runtime,operation,laser)
    directory = root/safe_id(laser_id)/safe_id(controller['id'])
    if directory.parent.is_symlink() or directory.is_symlink() or not directory.is_dir():
        raise ValueError('Controller has not enabled the trusted adaptive manager')
    selection_id = 'task-'+task_id
    experiment = directory/'adaptive-experiments'/selection_id/'adaptive-window'
    status = read_json(experiment/'status.json', {})
    mode_laser=load_mapping((runtime.directory(safe_id(controller['id']))/'laser.yaml').read_text())
    operating_mode=mode_read(mode_laser) if data is None or data.get('action')!='cancel' else None
    if data is None:
        from ldcon.agents.adjustment_guidance import current as current_guidance
        guidance=current_guidance(runtime,laser_id)
        results = sorted((experiment/'results').glob('*.json'), key=lambda p:p.stat().st_mtime)
        latest = read_json(results[-1], {}) if results else None
        return dict(status=status or dict(state='not_started'), latest_result=latest, operating_mode=operating_mode, adjustment_guidance=guidance,
                    checkpoint=read_json(experiment/'checkpoint.json', {}),
                    handoff=read_json(directory/'background-handoff.json',{}),
                    controller_job_id=controller['id'],envelope=bounds,grant_active=live_grant,
                    **capabilities)
    if not isinstance(data, dict) or set(data)-{'action','request_id','value','based_on','family','pi_candidate','parameter_candidate','candidate','expected_operating_mode_revision'}:
        raise ValueError('Invalid background step')
    action = data.get('action')
    safe_id(data.get('request_id'))
    if action not in ('begin','measure_here','move_and_measure','adopt','cancel','replan'):
        raise ValueError('Unsupported background step')
    if 'family' in data and action!='begin':raise ValueError('Adjustment family is selected only at begin')
    if 'candidate' in data and action!='replan':raise ValueError('A continuation candidate is valid only at replan')
    if 'pi_candidate' in data and (action!='begin' or data.get('family')!='pi'):
        raise ValueError('PI candidate is selected only at PI begin')
    from ldcon.core.adjustment_registry import FAMILIES,PARAMETER_FAMILIES,parameters as trial_parameters
    family=data.get('family','current')
    if 'parameter_candidate' in data and (action!='begin' or family not in PARAMETER_FAMILIES):
        raise ValueError('Runtime parameter candidate is selected only at slow-loop/feedforward begin')
    if family not in FAMILIES:raise ValueError('Unsupported background adjustment family')
    if action=='begin' and family not in capabilities['supported_adjustments']:
        raise ValueError('This controller does not support the requested adjustment family')
    if action=='begin' and params.get('adjustment_family','auto') not in ('auto',family):
        raise ValueError('Adjustment family differs from this task selection')
    if action in ('move_and_measure','adopt'):
        if type(data.get('value')) not in (int,float) or not math.isfinite(data['value']):
            raise ValueError('Current movement/adoption requires a finite numeric value')
    elif 'value' in data:raise ValueError('This background action accepts no current value')
    if action!='cancel' and not live_grant:
        raise ValueError('Monitor/task grant is stopped, disabled or stale; no new background action')
    if action!='cancel':
        expected=data.get('expected_operating_mode_revision')
        if type(expected) is not int or expected != mode_revision(operating_mode):
            raise ValueError('Operating mode changed or is missing; refresh owner mode before planning')
    descriptor=os.open(directory/'gateway.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    with os.fdopen(descriptor,'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        selector_path = directory/'adaptive-experiment.json'
        old = read_json(selector_path, {})
        if old and old.get('id') != selection_id:
            other = read_json(directory/'adaptive-experiments'/safe_id(old['id'])/'adaptive-window/status.json', {})
            if not other or _live_status(other):
                raise ValueError('Another adaptive task owns this controller')
        if action == 'begin':
            if family not in bounds.get('families',['current']):
                raise ValueError('Adjustment family is outside this monitor grant')
            selected=dict(id=selection_id,background=dict(monitor_id=monitor_id,generation=grant['generation'],
                handoff_timeout_s=bounds['handoff_timeout_s'],grant_revision=grant['parameters_hash'],
                expected_operating_mode_revision=data['expected_operating_mode_revision']))
            if read_json(directory/'protocol.json',{}).get('continuation') is True:
                selected['continuation']=True
            if family=='pi':
                from ldcon.jobs.background_pid_session import background_parameters
                approved=bounds.get('pid_parameters',{})
                p=background_parameters({'parameters':approved},
                    {'candidate':data['pi_candidate']} if 'pi_candidate' in data else {},require_candidate=True)
                if p['save_settings']:
                    raise ValueError('Background PI requires explicit save_settings=false')
                if p['strategy']=='validated_candidates' and operation not in ('servo.native_current','servo.ecdl_steady_lock'):
                    raise ValueError('This controller does not support explicit PI candidates')
                controller_ids={c.get('id',c.get('card_id')) for c in controller.get('cards',[]) if c.get('operation',c.get('card'))==operation}
                if p['controller_card_id'] not in controller_ids:
                    raise ValueError('Approved PI parameters name another controller card')
                selected.update(family='pi',pid_parameters=p)
            elif family in PARAMETER_FAMILIES:
                plan=dict((bounds.get('adjustment_parameters') or {}).get(family,{}) or {})
                if 'parameter_candidate' in data:plan['candidate']=data['parameter_candidate']
                p=trial_parameters(plan,family,require_candidate=True)
                selected.update(family=family,adjustment_parameters=p)
            else:
                selected.update(half_span_v=bounds['half_span_v'],timeout_s=bounds['timeout_s'],
                                max_points=bounds['max_points'],require_dither=True)
            if old and old.get('id') == selection_id:
                if old != selected:raise ValueError('Background admission changed; do not replace an existing session')
                control=read_json(directory/'background-control.json',{})
                if not control.get('enabled') or not all(control.get(k)==selected['background'][k] for k in ('monitor_id','generation')):
                    return dict(status='revoked',experiment_id=selection_id,message='This immutable session cannot be revived; create a new task')
                if status and not _live_status(status):return dict(status='already_terminal',experiment_id=selection_id,experiment_status=status)
                return dict(status='already_submitted', experiment_id=selection_id)
            atomic_json(directory/'background-control.json', dict(enabled=True, monitor_id=monitor_id, generation=grant['generation']))
            atomic_json(selector_path, selected)
            return dict(status='submitted', experiment_id=selection_id, message='Await owner admission; no measurement yet')
        if old.get('id') != selection_id or not (experiment/'inbox').is_dir():
            raise ValueError('Background experiment is not admitted yet')
        if old.get('background',{}).get('monitor_id')!=monitor_id:
            raise ValueError('Experiment belongs to another monitor')
        if action!='cancel' and old['background'].get('expected_operating_mode_revision')!=mode_revision(operating_mode):
            raise ValueError('Operating mode changed after admission; cleanup remains available')
        if old.get('family') in ('pi',*PARAMETER_FAMILIES) and action not in ('cancel','replan'):
            raise ValueError('Parameter comparison runs autonomously; inspect status or cancel this admitted trial')
        if status and not _live_status(status):
            if action=='cancel':return dict(status='already_terminal',experiment_status=status)
            raise ValueError('Background experiment is terminal; no late proposal may act')
        control=read_json(directory/'background-control.json',{})
        if action!='cancel' and (not control.get('enabled') or not all(
                control.get(k)==old['background'].get(k)==({'monitor_id':monitor_id,'generation':grant['generation']})[k]
                for k in ('monitor_id','generation')) or old['background'].get('grant_revision')!=grant['parameters_hash']):
            raise ValueError('Background intervention generation was revoked')
        command = dict(id=data['request_id'], action=action, based_on=data.get('based_on'))
        if 'value' in data:command['value'] = data['value']
        if action=='replan':
            if old.get('continuation') is not True or status.get('state')!='awaiting_replan':
                raise ValueError('Owner has not confirmed a recoverable continuation')
            if 'candidate' in data:
                if old.get('family')=='pi':
                    from ldcon.jobs.background_pid_session import background_parameters
                    checked=background_parameters({'parameters':bounds['pid_parameters']},{'candidate':data['candidate']},require_candidate=True)
                elif old.get('family') in PARAMETER_FAMILIES:
                    checked=trial_parameters({**bounds['adjustment_parameters'][old['family']],'candidate':data['candidate']},old['family'],require_candidate=True)
                else:raise ValueError('Current replan requires a fresh baseline, not a candidate override')
                command['candidate']=checked['candidate']
        # Immutable ID, no command replay even after the worker claimed it.
        for folder in ('inbox','claimed'):
            previous = read_json(experiment/folder/(command['id']+'.json'))
            if previous:
                if previous != command:raise ValueError('Background command ID conflicts')
                return dict(status='already_submitted', request_id=command['id'])
        if action!='cancel' and (not isinstance(command['based_on'],str) or command['based_on'] != status.get('decision_revision')):
            raise ValueError('Background decision evidence changed')
        atomic_json(experiment/'inbox'/(command['id']+'.json'), command)
        return dict(status='submitted', request_id=command['id'], message='Queued; inspect result and owner receipt')


def call(box, name, args):
    from ldcon.agents.agent_cards import _api
    if box.context.get('task_scope') != 'background_adjustment':
        raise ValueError('Only a bounded adjustment task may use this route')
    tail = 'agent-tasks/'+box.meta['id']+'/background'
    return _api(box, tail) if name == 'background_adjustment_status' else _api(box, tail, 'POST', args)


def install_routes(app, prefix, body):
    from fastapi import Request
    @app.get(prefix+'/api/agent-tasks/{task_id}/background')
    def status(task_id: str):return route(app.state.runtime, task_id)

    @app.post(prefix+'/api/agent-tasks/{task_id}/background')
    async def step(task_id: str, request: Request):return route(app.state.runtime, task_id, await body(request))
