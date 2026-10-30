"""One managed task session per card, independently of the resident mailbox."""
import json
import math
from pathlib import Path
import time
import uuid

from experiment_core.agent_contracts import execution_policy, choose_profile
from experiment_core.records import Journal, atomic_json, digest

from ldcon.core.interface_cards import CardCancelled

READ_TOOLS = frozenset({'agent_capability_probe', 'agent_task_status', 'agent_work_capabilities', 'laser_metrics',
    'laser_configuration_read', 'laser_settings_read', 'laser_runtime_read',
    'laser_cards', 'laser_card_validate', 'laser_card_job', 'laser_active_jobs',
    'laser_owner_status', 'laser_operating_mode', 'laser_stop_verify', 'window_center_status',
    'laser_log_read', 'laser_files_list', 'laser_files_read', 'laser_files_write',
    'bridge_list',
    'laser_history_read', 'laser_knowledge_read', 'laser_knowledge_propose'})


def parameters(card, overrides):
    defaults = dict(agent_card_id='', prompt='', instruction_profile='research' if card['operation']=='agent.history_review' else 'diagnosis',
                    execution_profile='', request_delegation=True,
                    lookback_s=3600., sample_interval_s=1., timeout_s=1800., monitor_job_id='', evidence_images=[],
                    adjustment_family='auto', adjustment_plan_card_id='agent-monitor', adjustment_objective='',
                    adoption_mode='auto_within_approved')
    p = {**defaults, **card['parameters'], **overrides}
    if set(p) != set(defaults):
        raise ValueError('Unknown task agent parameter')
    from ldcon.core.interface_schema import identifier
    from ldcon.agents.agent_bundle import PROFILES
    from experiment_core.agent_roles import adoption_mode
    adoption_mode(p['adoption_mode'])
    for key in ('agent_card_id', 'execution_profile', 'monitor_job_id', 'adjustment_plan_card_id'):
        if not isinstance(p[key],str):raise ValueError('Invalid '+key)
        if p[key]:
            identifier(p[key])
    if not isinstance(p['prompt'], str) or '\x00' in p['prompt'] or len(p['prompt'].encode()) > 16000:
        raise ValueError('Invalid task prompt')
    # The native-only distribution need not install ECDL parameter adapters.
    if p['adjustment_family'] not in ('auto','current','pi','slow_loop','feedforward'):raise ValueError('Unknown adjustment family')
    if (not isinstance(p['adjustment_objective'],str) or '\x00' in p['adjustment_objective']
            or len(p['adjustment_objective'].encode())>4000):raise ValueError('Invalid adjustment objective')
    if card['operation']=='agent.history_review' and p['instruction_profile']!='research':
        raise ValueError('Historical review cannot grant physical adjustment')
    if not isinstance(p['instruction_profile'],str) or p['instruction_profile'] not in PROFILES or type(p['request_delegation']) is not bool:
        raise ValueError('Invalid task execution selection')
    images=p['evidence_images']
    if not isinstance(images,list) or len(images)>8 or any(not isinstance(v,str) for v in images) or len(set(images))!=len(images):
        raise ValueError('Expected at most eight distinct evidence image paths')
    for value in images:
        path=Path(value)
        if (path.is_absolute() or str(path)!=value or any(ord(c)<32 or ord(c)==127 for c in value) or len(path.parts)<2 or path.parts[0] not in ('cards','scripts','data','reports')
                or any(part.startswith('.') for part in path.parts) or path.suffix.lower() not in ('.png','.jpg','.jpeg')):
            raise ValueError('Evidence image must be a relative PNG/JPEG path in laser assets')
    for key, limit in (('lookback_s', 31536000), ('timeout_s', 86400), ('sample_interval_s', 10)):
        if type(p[key]) not in (int, float) or not math.isfinite(p[key]) or not 0 < p[key] <= limit:
            raise ValueError('Invalid ' + key)
    return p


def _evidence_images(workspace, names):
    import hashlib
    root=Path(workspace);images=[]
    for name in names:
        path=root/name
        if any(v.is_symlink() for v in (path,*path.parents)) or not path.is_file() or not 0<path.stat().st_size<=8*1024*1024:
            raise ValueError('Evidence image must be a bounded regular file without symlinks')
        with path.open('rb') as stream:raw=stream.read(8*1024*1024+1)
        if len(raw)>8*1024*1024:raise ValueError('Evidence image byte budget exceeded')
        images.append(dict(path=str(path),mime_type='image/png' if path.suffix.lower()=='.png' else 'image/jpeg',sha256=hashlib.sha256(raw).hexdigest()))
    return images


def _parent_result(store, meta, p, directory, outcome):
    """Durable idempotent parent delivery; mailbox outage cannot erase a result."""
    from ldcon.core.records import atomic_json
    if not p['agent_card_id']:return
    explanation=outcome.get('explanation') or {}
    payload=dict(kind='task_completed',task_id=meta['id'],job_id=meta['id'],status=outcome['status'],
                 result_excerpt=str(outcome.get('result',outcome.get('message','')))[:8000],
                 result_truncated=len(str(outcome.get('result',outcome.get('message',''))))>8000,
                 result_tool='agent_task_status',profile=(outcome.get('profile') or {}).get('id',''),
                 bundle_revision=outcome.get('bundle_revision'),
                 explanation=explanation if len(json.dumps(explanation))<18000 else {'status':'completed','artifact':'task-explanation.json'},
                 report_author=outcome.get('report_author','task_model'),
                 prompt='Task ended. Relay the task-authored explanation verbatim with its author attribution; do not rewrite, summarize, translate or replace it. If unavailable, state that the task explanation is unavailable and link its receipts. Do not duplicate notifications or perform further work from this completion notice.')
    notice=dict(request_id='task-completed:'+meta['id'],payload=payload,status='pending')
    atomic_json(directory/'agent-parent-delivery.json',notice)
    try:store.enqueue(store.key(meta,p['agent_card_id']),notice['request_id'],payload)
    except Exception:return
    atomic_json(directory/'agent-parent-delivery.json',{**notice,'status':'queued'})


def run(directory, p, *, clock, sleep, cancel, emit, adapter_factory=None, bridge_registrar=None, image_inputs=None):
    from ldcon.agents.resident_agent import context, prepare_context, register_bridge
    from ldcon.agents.agent_adapters import SessionAdapter
    from ldcon.agents.agent_profiles import select_task
    from ldcon.agents.agent_bundle import build, turn_origin
    from ldcon.agents.agent_log_context import collect_log_context
    from ldcon.core.interface_schema import load_mapping
    from ldcon.core.records import read_json
    from ldcon.web.agent_task_api import admission_path
    from experiment_core.errors import Conflict
    directory=Path(directory)
    store,meta=context(directory);root=store.root.parent
    invocation=read_json(directory/'invocation.json')
    deck=load_mapping((directory/'interface-deck.yaml').read_text())
    card=next(c for c in deck['cards'] if c['id']==invocation['card_id'])
    # Freeze the complete normalized request, including wall-time and scope.
    request=dict(parameters=dict(p),card_id=card['id'],policy=card.get('agent_execution'))
    journal=Journal(directory/'agent-task.sqlite3')
    adapter=None;owned=False;work_started=False;params=None;outcome=None;turns=0
    started=clock();deadline=started+p['timeout_s'];selected=None;bundle=None
    cancelled=lambda:cancel() or clock()>=deadline
    def checkpoint():
        if cancelled():raise CardCancelled('Task cancelled or wall-time budget expired')
        if adapter:adapter.p['request_timeout_s']=max(.01,deadline-clock())
    def phase(value,allowed=()):
        atomic_json(directory/'agent-task-phase.json',dict(task_id=meta['id'],laser_id=params['laser_id'],depth=1,
            phase=value,allowed_tools=sorted(allowed),expires_unix_s=time.time()+max(0,deadline-clock())))
    try:
        with journal.guard('task',meta['id']):
            try:previous=journal.get('task')
            except KeyError:previous=None
            if previous:
                if previous['request']['request']!=request:raise Conflict('Task ID binds different request content')
                if previous['state'] in ('succeeded','failed','cancelled'):
                    _parent_result(store,meta,p,directory,previous['result'])
                    return previous['result']
                raise RuntimeError('Task was interrupted or remains active; no automatic replay')
            policy=execution_policy(card.get('agent_execution',dict(mode='required',task_profile=p['execution_profile'])))
            admission=read_json(admission_path(root,meta['id']),{})
            if admission:
                if admission.get('parameters')!=p or admission.get('card_id')!=card['id']:
                    raise ValueError('Task admission does not match its frozen invocation')
                selected=admission.get('profile')
                routing=admission.get('selection',{}).get('routing_snapshot') or {}
                name=choose_profile(policy,requested=p['request_delegation'],override=p['execution_profile'],provider=(selected or {}).get('provider',''),
                                    matrix=routing.get('provider_profiles'),default_difficulty=routing.get('default_difficulty','standard'))
                if (selected or {}).get('id')!=name:raise ValueError('Task admission profile differs from card policy')
            else:
                selected,_=select_task(root,card,p,meta=meta,deck=deck)
                name=selected['id'] if selected else None
            journal.admit('task','agent',dict(request=request,profile=selected))
            owned=True
            journal.transition('task',0,'running',{'kind':'task_admitted'})
            if name is None:
                if not p['agent_card_id']:raise ValueError('Resident execution requires an agent card')
                if p['instruction_profile']=='adjustment':
                    from ldcon.agents.resident_adjustment import run as adjust
                    work_started=True
                    outcome=adjust(directory,p,clock=clock,sleep=sleep,cancel=cancel,emit=emit)
                    journal.transition('task',1,outcome['status'],{'kind':'resident_adjustment_completed'},outcome)
                    atomic_json(directory/'agent-task-status.json',outcome)
                    return outcome
                request_id='task-resident:'+meta['id']
                store.enqueue(store.key(meta,p['agent_card_id']),request_id,dict(kind='card_task',job=meta['id'],prompt=p['prompt']))
                outcome=dict(status='succeeded',disposition='delegated_to_resident',request_id=request_id,
                    message='Resident task queued; execution not confirmed',output_writes=0)
                journal.transition('task',1,'succeeded',{'kind':'resident_queued'},outcome)
                atomic_json(directory/'agent-task-status.json',outcome)
                return outcome
            if p['instruction_profile']=='adjustment':
                from ldcon.jobs.study_execution import decision_parent
                from ldcon.agents.task_decisions import TaskDecisions
                from ldcon.agents.resident_adjustment import run as adjust
                parent=decision_parent(directory,p['agent_card_id'])
                child=TaskDecisions(directory,parent,selected,clock=clock,cancel=cancel,
                    deadline=deadline,adapter_factory=adapter_factory)
                try:
                    child.connect(checkpoint)
                    work_started=True
                    if (invocation.get('adjustment_plan') or {}).get('kind') in ('dlc_pi_change_measure','dlc_pi_target_response'):
                        from ldcon.agents.dlc_pi_adjustment import run as adjust
                    outcome=adjust(directory,p,clock=clock,sleep=sleep,cancel=cancel,emit=emit,
                        selected=parent,decision_session=child)
                    outcome['explanation']=child.explain(outcome)
                    outcome.update(profile=selected,session_id=child.session,
                        model_info=dict(child.adapter.model_info),report_author='task_model')
                    journal.transition('task',journal.get('task')['sequence'],outcome['status'],
                        {'kind':'delegated_adjustment_completed'},outcome)
                    atomic_json(directory/'agent-task-status.json',outcome)
                    _parent_result(store,meta,p,directory,outcome)
                    return outcome
                except Exception as task_error:
                    child.explain(dict(status='failed',error_type=type(task_error).__name__,
                        reason=str(task_error),evidence=read_json(directory/'resident-adjustment-summary.json',{})))
                    raise
                finally:child.close()
            deadline=started+min(p['timeout_s'],selected['timeout_s'])
            if selected['max_turns']<2:raise ValueError('Task budget must include capability probe and work turn')
            checkpoint()
            params=dict(provider=selected['provider'],model=selected['model'],effort=selected['effort'],
                        workspace='',remote_control=False,request_timeout_s=max(.01,deadline-clock()))
            params=prepare_context(directory,params,store,meta,'task-'+meta['id'],task_context=True)
            if image_inputs is None:image_inputs=_evidence_images(params['workspace'],p['evidence_images'])
            registration=(bridge_registrar or register_bridge)(params)
            atomic_json(directory/'bridge-registration.json',registration)
            scope='background_adjustment' if p['instruction_profile']=='adjustment' else 'read_only'
            allowed=set(READ_TOOLS)
            if scope=='background_adjustment':allowed|={'background_adjustment_status','background_adjustment_step','laser_quality_update'}
            from ldcon.agents.agent_tools import TOOLS
            allowed&={t['name'] for t in TOOLS}
            missing=set(selected['required_tools'])-allowed
            if missing:raise ValueError('Required task tools are outside this task scope: '+', '.join(sorted(missing)))
            params['task_depth']=1
            tools_path=Path(params['tools_context']);tools_context=read_json(tools_path)
            tools_context.update(task_depth=1,task_scope=scope,task_id=meta['id'],monitor_job_id=p['monitor_job_id'],task_allowed_tools=sorted(allowed))
            atomic_json(tools_path,tools_context)
            (directory/'agent-tool-state').mkdir(exist_ok=True)
            if params['provider']=='opencode':
                # OpenCode 2.1 ACP registers MCP by (directory, server name),
                # so a resident and task must not share that transport location.
                transport=directory/'agent-tool-state'/'opencode-cwd'
                if any(path.is_symlink() for path in (transport,*transport.parents)):
                    raise ValueError('Task transport directory must not be a symlink')
                transport.mkdir(exist_ok=True)
                params['transport_workspace']=str(transport.absolute())
            atomic_json(directory/'agent-transport-context.json',dict(provider=params['provider'],
                asset_workspace=params['workspace'],transport_workspace=params.get('transport_workspace',params['workspace']),
                mcp_context=params['tools_context'],task_id=meta['id']))
            phase('probe',allowed)
            now=time.time()
            evidence=collect_log_context(root,params['laser_id'],since=now-p['lookback_s'],until=now,exclude_job_ids=(meta['id'],))
            if image_inputs:evidence={**evidence,'image_artifacts':image_inputs}
            from ldcon.agents.agent_history import knowledge_summary
            context_value=dict(laser_id=params['laser_id'],profile=selected,parent_card=p['agent_card_id'],task_id=meta['id'],
                source_job_id=admission.get('source_job_id'),
                instruction_profile=p['instruction_profile'],scope=scope,objective=p['prompt'],knowledge=knowledge_summary(root,params['laser_id']))
            bundle=build(directory,p['instruction_profile'],context=context_value,evidence=evidence,max_input_bytes=selected['max_input_bytes'])
            task_role=('This is a bounded task session, not the resident inbox watcher. '
                'Resident startup, hub inbox polling, bridge registration and peer-message forwarding belong to the resident. '
                'Do not call bridge_read or bridge_send or perform those resident duties. '
                'Use only actually advertised tools within this task allowlist: '+', '.join(sorted(allowed))+'. '
                'If a required scoped tool is absent, report the missing capability and do not substitute a native command or direct API. '
                'The asset workspace remains '+params['workspace']+'. '
                'The provider transport directory is private task state, not the storage destination for shared laser results.')
            params['instructions']=bundle['instructions']+'\n\n'+params['instructions']+'\n\nTask role and tool scope:\n'+task_role
            params['instructions_resolved']=True
            import hashlib
            atomic_json(directory/'agent-effective-instructions.json',dict(sha256=hashlib.sha256(params['instructions'].encode()).hexdigest(),
                bytes=len(params['instructions'].encode()),bundle_revision=bundle['bundle_revision'],text=params['instructions']))
            # Input budgets include fixed context. Token/cost and native tool-step
            # ceilings are not claimed from this request-count budget.
            if len(params['instructions'].encode())+bundle['input_bytes']+len(p['prompt'].encode())>selected['max_input_bytes']:
                raise ValueError('Full task input exceeds its byte budget')
            adapter=(adapter_factory or SessionAdapter)(params,cancelled)
            checkpoint();checked=adapter.preflight();checkpoint()
            atomic_json(directory/'agent-task-status.json',dict(state='connecting',profile=selected,parent_card=p['agent_card_id'],task_id=meta['id'],preflight=checked,depth=1))
            adapter.session_created=lambda session:atomic_json(directory/'agent-task-session.json',dict(session_id=session,provider=selected['provider'],profile_revision=selected['profile_revision']))
            session=adapter.connect('');checkpoint();info=adapter.model_info
            if info.get('resolved_model')!=selected['model']:raise RuntimeError('Task effective model does not match its pinned profile')
            if selected['effort'] and info.get('resolved_effort')!=selected['effort']:raise RuntimeError('Task effective effort does not match its pinned profile')
            if info.get('confirmation')!='server_reported':raise RuntimeError('Task effective profile is unconfirmed')
            if 'image' in selected['required_modalities']:
                if info.get('input_modalities_confirmation')!='model_catalog' or 'image' not in info.get('input_modalities',[]):
                    raise RuntimeError('Required image modality lacks an exact-model capability catalog')
                if not image_inputs:raise RuntimeError('Required image evidence has not been attached; task deferred')
            nonce=uuid.uuid4().hex
            atomic_json(directory/'agent-probe-challenge.json',dict(nonce=nonce,bundle_revision=bundle['bundle_revision']))
            turn_origin(directory,'probe','capability_probe',task_id=meta['id'])
            checkpoint();turns+=1
            adapter.request(dict(kind='capability_probe',prompt='Call agent_capability_probe exactly once with nonce '+nonce+'. Do not perform other actions. This is a local, non-actuating tool check.'))
            checkpoint();receipt=read_json(directory/'agent-probe-receipt.json',{})
            if (receipt.get('nonce')!=nonce or receipt.get('bundle_revision')!=bundle['bundle_revision']
                    or receipt.get('laser_id')!=params['laser_id']
                    or not set(selected['required_tools'])<=set(receipt.get('tools',[]))):
                raise RuntimeError('Required scoped MCP probe did not complete; task not started')
            phase('work',allowed)
            atomic_json(directory/'agent-task-status.json',dict(state='running',session_id=session,profile=selected,model_info=info,
                capability_receipt=receipt,permission_readiness='per_operation_host_and_owner_checks_required',depth=1))
            turn_origin(directory,meta['id'],'card_task',task_id=meta['id'])
            checkpoint();turns+=1;work_started=True
            payload=dict(kind='card_task',task_id=meta['id'],prompt=p['prompt'],bundle_revision=bundle['bundle_revision'],**bundle['input'])
            if image_inputs:payload['image_inputs']=image_inputs
            result=adapter.request(payload)
            checkpoint()
            if not isinstance(result,str):raise RuntimeError('Task adapter did not return a textual result')
            outcome=dict(status='succeeded',message='Task agent completed; physical outcomes require owner receipts',result=result,
                task_id=meta['id'],parent_card=p['agent_card_id'],source_job_id=admission.get('source_job_id'),
                session_id=session,profile=selected,model_info=dict(adapter.model_info),bundle_revision=bundle['bundle_revision'],elapsed_s=clock()-started,
                turns=turns,budget_enforcement=dict(wall_time='bounded',turns='submitted_requests',tokens='not_supported',native_tool_steps='not_supported'),
                physical_outcome='not_inferred_from_agent_prose')
            journal.transition('task',journal.get('task')['sequence'],'succeeded',{'kind':'completed'},outcome)
            atomic_json(directory/'agent-task-status.json',outcome)
            _parent_result(store,meta,p,directory,outcome)
            try:emit('agent_task_completed',dict(task_id=meta['id'],profile=name,session_id=session))
            except Exception:atomic_json(directory/'agent-task-event-delivery.json',dict(status='pending',task_id=meta['id']))
            return outcome
    except BaseException as error:
        if not owned:raise
        old=journal.get('task')
        if old['state']=='running':
            from ldcon.agents.resident_adjustment import HostProtocolStop
            # A host protocol stop between correlated owner results, with the
            # owner showing no active trial/epoch/return, leaves no uncertain
            # physical write: a known inconclusive outcome, not 'unknown'
            # (which pauses the monitor in task_requires_inspection; 2026-10-28).
            protocol=(isinstance(error,HostProtocolStop) and error.physical_outcome_known
                      and not (isinstance(error,CardCancelled) or cancelled()))
            state=('cancelled' if isinstance(error,CardCancelled) or cancelled() else 'inconclusive' if protocol
                   else 'unknown' if work_started else 'failed')
            outcome=dict(status=state,reason=type(error).__name__,message=str(error),task_id=meta['id'],profile=selected,
                         turns=turns,physical_outcome=('no_owner_command_in_flight' if protocol else
                             'requires_inspection' if work_started else 'no_work_turn_started'))
            if protocol:outcome.update(disposition='host_protocol_stop',owner_state=error.owner_state)
            journal.transition('task',old['sequence'],state,{'kind':'interrupted'},outcome)
            atomic_json(directory/'agent-task-status.json',outcome)
            explanation=read_json(directory/'task-explanation.json',{})
            if explanation:outcome.update(explanation=explanation,report_author='task_model')
            atomic_json(directory/'agent-task-status.json',outcome)
            _parent_result(store,meta,p,directory,outcome)
            if state in ('failed','inconclusive') and isinstance(error,Exception):return outcome
        raise
    finally:
        if owned:
            cleanup=[]
            if params:
                try:phase('closed')
                except Exception as error:cleanup.append(dict(component='phase_close',reason=type(error).__name__))
                try:turn_origin(directory,meta['id'],'card_task',active=False,task_id=meta['id'])
                except Exception as error:cleanup.append(dict(component='request_close',reason=type(error).__name__))
            if adapter:
                try:adapter.close()
                except Exception as error:cleanup.append(dict(component='transport',reason=type(error).__name__))
            if params:
                from ldcon.agents.agent_background import revoke_task
                try:revoke_task(root,params['laser_id'],meta['id'])
                except Exception as error:cleanup.append(dict(component='background_grant',reason=type(error).__name__))
            from ldcon.agents.notification_policy import finish_task
            try:finish_task(root,meta['id'])
            except Exception as error:cleanup.append(dict(component='notification',reason=type(error).__name__))
            if cleanup:atomic_json(directory/'agent-task-cleanup.json',dict(status='requires_inspection',errors=cleanup))
        journal.close()
