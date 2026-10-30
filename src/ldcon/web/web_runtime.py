"""Single-owner interface-card jobs, immutable snapshots and configuration."""
from ldcon.core.state_layout import job_root

from datetime import datetime, timezone
from functools import wraps
import hashlib
import hmac
import json
import os
from pathlib import Path
from ldcon.core.paths import source_root
import re
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from ldcon.core.deck_yaml import dump_document
from ldcon.core.interface_schema import InterfaceRepository, readiness, digest, load_mapping
from ldcon.web.interface_projection import project_assignment

# v1.4 (2026-10-22): + completed_unsettled (the jobs v4.12 FF proxy's restored-but-unsettled outcome); without it the
# web took such a proxy job as still running and, once its worker exited, rewrote it to 'interrupted'.
TERMINAL = frozenset({'succeeded', 'completed', 'completed_with_failures', 'completed_unsettled', 'failed', 'cancelled', 'interrupted', 'inconclusive'})

from ldcon.core.records import atomic_json, read_json, safe_id, utc_now

class TriggerPending(Exception):
    """The software trigger was accepted (recorded once) but the worker has not confirmed firing yet."""
    def __init__(self, record):
        super().__init__('Software trigger accepted; firing not yet confirmed. GET the job and read shot_sweep.fired')
        self.record = record


class ConflictError(ValueError):
    pass


class SweepBlocked(ConflictError):
    """Sweep API v1.4: 409 with a machine-readable body (code settling|busy, blocking job, deadline)."""
    def __init__(self, payload):
        super().__init__(payload['detail'])
        self.payload = payload

def synchronized(method):
    @wraps(method)
    def locked(self, *args, **kwargs):
        with self.mutex:return method(self, *args, **kwargs)
    return locked


class WebRuntime:

    def __init__(self, state_dir, max_concurrent_jobs=12):
        if type(max_concurrent_jobs) is not int or max_concurrent_jobs < 1:
            raise ValueError("max_concurrent_jobs must be a positive integer")
        self.max_concurrent_jobs = max_concurrent_jobs
        self.root = Path(state_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock_file = (self.root/'server.lock').open('a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                self._lock_file.write(b'0'); self._lock_file.flush(); self._lock_file.seek(0)
                msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self._lock_file.close()
            raise RuntimeError('Another web server owns this state directory') from error
        self.jobs = self.root/'jobs'
        self.jobs.mkdir(exist_ok=True)
        self.interface_repository = InterfaceRepository(self.root)
        self.mutex = threading.RLock()
        self.processes = {}
        self._chart_cache = {}
        self._operations = None
        from ldcon.jobs.job_service import JobProcess
        for metadata in self.job_metadata():
            directory=metadata.parent
            if not directory.is_symlink() and not directory.parent.is_symlink():
                self.processes[directory.name]=JobProcess(directory)
        from ldcon.core.configuration_transaction import reconcile_pending
        self.policy_reconciliation = reconcile_pending(self)
        from ldcon.core.result_reservations import Reservations
        self.reservations = Reservations(self)

    def get_set(self, name):
        return project_assignment(self.interface_repository.assignment(safe_id(name)))

    def admission_inhibited(self):
        # Presence alone closes admission, including malformed files/symlinks.
        # Only the migration coordinator removes its own marker after review.
        return os.path.lexists(self.root/'migration-inhibit.json')

    def assert_admission_open(self):
        if self.admission_inhibited():
            raise ConflictError('Migration in progress: new operations and configuration changes are paused; stopping remains available')

    def assert_laser_policy_ready(self, laser_id):
        from ldcon.core.configuration_transaction import pending_transactions
        if any(laser_id is None or row.get('laser_id') in (None,laser_id)
               for row in pending_transactions(self.root)):
            raise ConflictError('Native policy transaction requires reconciliation before device operations; read-only diagnosis and stopping remain available')

    def consume_migration_start(self, token, invocation):
        """Consume one exact coordinator start, never general command authority."""
        path=self.root/'migration-inhibit.json'
        marker=read_json(path,{})
        expected=marker.get('migration_token') if isinstance(marker,dict) else None
        if (path.is_symlink() or not isinstance(token,str) or not isinstance(expected,str)
                or len(expected)<32 or not hmac.compare_digest(token,expected)
                or marker.get('schema_version')!=1):
            raise ConflictError('Invalid migration admission')
        allowed=marker.get('allowed_starts',[])
        matches=[i for i,item in enumerate(allowed) if item==invocation] if isinstance(allowed,list) else []
        if len(matches)!=1:raise ConflictError('Migration start is absent, ambiguous or already consumed')
        marker['allowed_starts']=[item for i,item in enumerate(allowed) if i!=matches[0]]
        # Save BEFORE dispatch: an uncertain response must be inspected, not replayed.
        from ldcon.core.result_reservations import durable_json
        durable_json(path,marker)

    def set_library(self):
        items=[]
        for item in self.interface_repository.list('assignments'):
            try:
                value=self.get_set(item['id']);data=value['document']
                items.append({'id':value['id'],'name':data['name'],'laser':data['laser'],
                    'decks':len(data['decks']),'revision':value['revision'],
                    'runnable':value['runnable'],'blocked_reason':value['blocked_reason']})
            except (ValueError,OSError) as error:
                items.append({'id':item['id'],'error':str(error)})
        return items

    def start_set_deck(self, set_id, deck_id, expected_revision, card_id=None, parameters=None, reservation=None, *, migration_token=None):
        with self.mutex:
            admitted=False
            if migration_token is not None:
                if reservation is not None:raise ConflictError('Migration cannot create reservations')
                self.consume_migration_start(migration_token,dict(set_id=set_id,deck_id=deck_id,
                    card_id=card_id,parameters=parameters or {},expected_revision=expected_revision))
                admitted=True
            else:self.assert_admission_open()
            if reservation is not None:
                if not isinstance(reservation,dict):raise ValueError('Reservation must be a mapping')
                payload={k:v for k,v in reservation.items() if k!='controller_job_id'}
                payload['target']=dict(set_id=set_id,deck_id=deck_id,expected_revision=expected_revision,
                                       card_id=card_id,parameters=parameters or {})
                if 'controller_job_id' in reservation:
                    payload['target']['controller_job_id']=reservation['controller_job_id']
                return self.reservations.create(payload)
            value=self.get_set(set_id)
            if expected_revision != value['revision']:
                raise ConflictError('Configuration changed. Reload before starting.')
            return self.start_interface_card(value,deck_id,card_id,parameters or {},_migration_admitted=admitted)

    def operation_gateway(self):
        with self.mutex:
            if self._operations is None:
                from ldcon.web.laser_operations import OperationGateway
                self._operations = OperationGateway(self, owner_description=self.operation_owner_description)
            return self._operations

    def operation_owner_description(self, laser_id):
        from ldcon.web.operation_observation import describe_owners
        laser = self.interface_repository.read('lasers', safe_id(laser_id))['document']
        return describe_owners(laser)

    def operations(self, laser_id):
        return self.operation_gateway().for_laser(laser_id)

    def _operation_laser(self, job_id):
        job = self.get_job(job_id)
        laser_id = job.get('laser', {}).get('id')
        if not laser_id:
            raise ValueError('Job has no authoritative laser scope')
        return laser_id

    def validate_card(self, set_id, deck_id, card_id, overrides, expected_revision):
        from ldcon.web.laser_operations import TrustedOperationContext
        value = self.get_set(set_id)
        laser_id = value['laser_definition']['document']['id']
        return self.operations(laser_id).preflight(dict(subject_id=laser_id,
            set_id=set_id, deck_id=deck_id, card_id=card_id, parameters=overrides,
            expected_revision=expected_revision), TrustedOperationContext(laser_id))

    def start_interface_card(self, value, deck_id, card_id, overrides, *, job_id=None, reservation=None, _migration_admitted=False):
        from ldcon.core.operation_registry import spec
        definition = next((d for d in value['deck_definitions'] if d['id'] == safe_id(deck_id)), None)
        if definition is None: raise FileNotFoundError(deck_id)
        card_id = card_id or definition['document']['cards'][0]['id']
        card = next((c for c in definition['document']['cards'] if c['id'] == card_id), None)
        if card is None: raise ValueError('Unknown interface card')
        spec(card['operation'])
        job_id = safe_id(job_id) if job_id is not None else uuid.uuid4().hex
        laser_id = value['laser_definition']['document']['id']
        result = self.operation_gateway().call('card.start', laser_id, (value, deck_id, card_id, overrides),
            dict(job_id=job_id, reservation=reservation, _migration_admitted=_migration_admitted),
            request_id=job_id, identity=dict(set_id=value['id'], revision=value['revision'],
                deck_id=deck_id, card_id=card_id, parameters=overrides, reservation=reservation))
        return dict(result, operation_request_id=job_id)

    def _start_interface_card(self, value, deck_id, card_id, overrides, *, job_id=None, reservation=None, _migration_admitted=False):
        # Called while holding the runtime mutex; no instrument I/O in this process.
        if not _migration_admitted:self.assert_admission_open()
        if not isinstance(overrides,dict):raise ValueError('Invocation parameters must be a mapping')
        definition=next((d for d in value['deck_definitions'] if d['id']==safe_id(deck_id)),None)
        if definition is None:raise FileNotFoundError(deck_id)
        deck=definition['document'];card_id=card_id or deck['cards'][0]['id']
        card=next((c for c in deck['cards'] if c['id']==card_id),None)
        if card is None:raise ValueError('Unknown interface card')
        from ldcon.core.interface_schema import RETIRED_OPERATIONS
        if card['operation'] in RETIRED_OPERATIONS:
            raise ValueError('Retired card: '+RETIRED_OPERATIONS[card['operation']])
        from ldcon.core.card_admission import OBSERVERS
        if card['operation'] not in OBSERVERS:
            self.assert_laser_policy_ready(value['laser_definition']['document']['id'])
        if card['operation'] in ('tune.pid_factor_reduction','tune.pid_coordinate_search','study.ecdl_locked_current_map','tune.ecdl_window_center'):
            from ldcon.core.interface_schema import parameters as online_parameters
            p=online_parameters(card,{**definition['invocation_parameters'].get(card_id,{}),**overrides})
            active=[j for j in self.active_jobs(set_id=value['id']) if j['status'] not in TERMINAL and j.get('deck_id')==deck_id
                    and any(c['id']==p['controller_card_id'] for c in j.get('cards',[]))]
            if len(active)==1:
                return self.request_online(active[0]['id'],card_id,p,request_id=job_id)
            if not (card['operation']=='tune.ecdl_window_center' and not p.get('measure_dither',True) and p.get('mode')=='center_only' and not active):
                raise ConflictError('A matching stationary PID lock must be running')
            # rule F (2026-10-22): no lock -> a search without dither measurement runs through its open-loop owner
        laser=value['laser_definition']['document']
        invocation={**definition['invocation_parameters'].get(card_id,{}),**overrides}
        reporting=card['operation']=='report.grafana_dashboard'
        from ldcon.core.card_admission import admission_class,job_owns_control_slot
        admission_jobs=self.active_jobs(set_id=value['id']) if card['operation'] in ('tune.operating_point','measure.change','measure.batch') else ()
        admission=admission_class(laser,card,admission_jobs)
        independent=admission!='exclusive'
        if card['operation'].startswith('sweep.') and card['uses']:
            from ldcon.jobs.native_ff_proxy import feedforward_route
            from ldcon.jobs.ecdl_locked_sweep_proxy import rig_locked_route
            lock_operation=('servo.native_current' if feedforward_route(laser,card['uses'][0])
                            else 'servo.ecdl_steady_lock' if rig_locked_route(laser,card['uses'][0]) else None)
            if lock_operation:
                # The sweep job proxies a session inside the running lock job (labscript stops only the proxy).
                # v1.2: locked = controller_state 'locked' OR the lock's own fresh lock-target.json state 'locked' (the
                # same locked source as the lock-target API; sr422 display states flip on wavemeter dropouts)
                locked=[j for j in self.active_jobs(set_id=value['id']) if j['status'] not in TERMINAL
                        and any(c.get('operation',c.get('card'))==lock_operation for c in j.get('cards',[]))
                        and (j.get('controller_state')=='locked' or self._lock_report_locked(j['id']))]
                if len(locked)!=1:raise ConflictError('A running, locked %s job is required for the staircase'%lock_operation)
                # v1.4 (2026-10-23): one shot per laser INCLUDING the previous shot's settle (labscript TTB fails cleanly)
                from ldcon.web.sweep_guard import sweep_blocker
                blocker=sweep_blocker(self.active_jobs(set_id=value['id']),exclude=job_id)
                if blocker is not None:raise SweepBlocked(blocker)
        reference_source=None
        if reporting:
            from ldcon.web.grafana_dashboard import parameters,resolve_reference
            active_job=next((j for j in self.active_jobs(set_id=value['id']) if j['status'] not in TERMINAL and not j.get('read_only')),None)
            if active_job:
                active_job['runtime_parameters']=read_json(self.directory(active_job['id'])/'runtime-parameters.json',{})
            invocation,reference_source=resolve_reference(parameters(card,invocation),deck,definition['invocation_parameters'],active_job)
        if card['operation']=='tune.ecdl_operating_point':
            from ldcon.jobs.workflow_artifacts import resolve_inputs
            invocation=resolve_inputs(self,laser['id'],invocation)
        status=readiness(laser,deck,card,invocation)
        if not status['runnable']:raise ValueError(' / '.join(status['blocked_reasons']))
        shot_request=status['parameters'].get('shot_sweep')
        if shot_request:
            for previous in self.job_headers(set_id=value['id']):
                if previous.get('sweep_request_id')==shot_request['request_id']:
                    raise ConflictError('Shot request_id has already been submitted; inspect the existing job')
        active=[job_id for job_id,p in self.processes.items() if p.poll() is None]
        for active_job_id in active:
            meta=read_json(self.directory(active_job_id)/'job.json',{})
            if card['operation'] in ('agent.resident','agent.log_watch','agent.monitor') and meta.get('set_id')==value['id'] and meta.get('deck_id')==deck_id and any(c['id']==card_id for c in meta.get('cards',[])):
                raise ConflictError('This resident agent or log watch card is already active')
            if not independent and job_owns_control_slot(meta) and (meta.get('set_id')==value['id'] or meta.get('laser',{}).get('id')==laser['id']):
                raise ConflictError('This laser already has an active card')
        if len(active)>=self.max_concurrent_jobs:raise ConflictError('Concurrent process limit reached')
        job_id=safe_id(job_id) if job_id is not None else uuid.uuid4().hex
        directory=job_root(self.root,safe_id(laser['id']))/job_id;directory.mkdir(parents=True)
        (directory/'laser.yaml').write_text(value['laser_definition']['yaml'])
        (directory/'interface-deck.yaml').write_text(definition['yaml'])
        (directory/'deck.yaml').write_text(definition['yaml'])
        (directory/'set.yaml').write_text(value['yaml'])
        p=status['parameters']
        # Freeze a referenced artifact in the new job. Retention or later config
        # edits cannot replace evidence beneath this execution.
        from ldcon.jobs.workflow_artifacts import freeze_inputs
        saved_parameters, input_artifacts=freeze_inputs(directory,p)
        invocation_payload={'card_id':card_id,'parameters':saved_parameters,
                            'input_artifacts':input_artifacts,'reference_source':reference_source}
        if card['operation']=='agent.task' and p['instruction_profile']=='adjustment':
            from ldcon.agents.dlc_pi_plan import freeze_task
            invocation_payload['adjustment_plan']=freeze_task(laser,deck,p,definition['invocation_parameters'])
        if card['operation'] in ('measure.change','measure.batch'):
            from ldcon.agents.measurement_grant import freeze_measurement_plan, measurement_plan_source
            invocation_payload['measurement_plan']=freeze_measurement_plan(laser)
            invocation_payload['measurement_plan_source']=measurement_plan_source(laser,value['laser_definition']['revision'])
            controllers=[j['id'] for j in admission_jobs if j.get('status') not in TERMINAL and any(c.get('operation',c.get('card')) in ('servo.ecdl_steady_lock','servo.native_current','servo.dlcpro_frequency') for c in j.get('cards',[]))]
            invocation_payload['measurement_controller_job_id']=controllers[0] if len(controllers)==1 else None
        if reporting:invocation_payload['report_card_parameters']=definition['invocation_parameters']
        atomic_json(directory/'invocation.json',invocation_payload)
        projected=next(d for d in value['document']['decks'] if d['id']==deck_id)
        from ldcon.agents.teams_card import parameters as teams_parameters
        from ldcon.agents.failure_policy import select_targets
        backend=os.environ.get('LDCON_JOB_LAUNCHER','detached')
        if backend not in ('service','detached'):raise ValueError('Unknown job launcher')
        meta={'worker_backend':backend,'id':job_id,'deck_id':deck_id,'label':deck['name'],'set_id':value['id'],'set_name':laser['name'],
              'sweep_request_id':shot_request['request_id'] if shot_request else None,
              'laser':{'id':laser['id'],'name':laser['name'],'kind':'interface-laser'},'created_utc':utc_now(),
              'planned_s':p.get('duration_s',p.get('timeout_s')) or 0,'mode':'continuous' if card['operation'] in ('agent.resident','agent.log_watch','agent.monitor') else 'continuous' if card['operation'] in ('servo.current_pid','servo.slow_pid','servo.ecdl_steady_lock','servo.native_current') and p['duration_s'] is None else 'finite','revision':definition['revision'],
              'set_revision':value['revision'],'laser_revision':value['laser_definition']['revision'],
              'cards':[{**c,'params':saved_parameters} for c in projected['cards'] if c['id']==card_id],
              'input_artifacts':input_artifacts,'execution':{'backend':'interface-live'},
              'time_basis':'wall','sample_s':p.get('sample_interval_s',.5),'recovery_budget':0,'read_only':independent,'admission_class':admission,
              'agent_fallbacks':{c['id']:c['fallback']['agent_card_id'] for c in deck['cards'] if c.get('fallback',{}).get('agent_card_id')},
              'teams_fallbacks':{c['id']:{'card_id':c['fallback']['teams_card_id'],'parameters':teams_parameters(next(t for t in deck['cards'] if t['id']==c['fallback']['teams_card_id']),definition['invocation_parameters'].get(c['fallback']['teams_card_id'],{}))} for c in deck['cards'] if c.get('fallback',{}).get('teams_card_id')},
              'agent_teams_cards':{c['id']:teams_parameters(c,definition['invocation_parameters'].get(c['id'],{})) for c in deck['cards'] if c['operation']=='notify.teams'},
              'failure_policy_version':1,
              'failure_targets':select_targets(deck,definition['invocation_parameters']),
              'safety':projected['safety'],'engine':'interface-cards',
              'device_settings':{k:d.get('control_settings',{}) for k,d in laser['devices'].items() if d.get('control_settings')},
              'online_cards':[{**c,'parameters':definition['card_status'][c['id']]['parameters']} for c in deck['cards'] if c['operation'] in ('tune.pid_factor_reduction','tune.pid_coordinate_search','study.ecdl_locked_current_map','tune.ecdl_window_center') and definition['card_status'][c['id']]['parameters']['controller_card_id']==card_id]}
        from ldcon.core.operation_registry import spec
        registration = spec(card['operation'])
        meta['operation_contract'] = dict(schema='ldcon.operation/1', operation_id=registration.operation_id,
            version=registration.version, configured_policy_revision=value['laser_definition']['revision'],
            admission=admission, attachment=registration.execution, cancellation=registration.cancellation,
            physical_authority='owner_revalidation_required')
        if reservation is not None:
            meta['reservation']={k:reservation[k] for k in ('id','reason','prerequisites')}
            meta['failure_context']=json.loads(json.dumps(reservation.get('failure_context',[])))
            meta['reservation']['failure_context']=meta['failure_context']
            atomic_json(directory/'upstream-failures.json',meta['failure_context'])
        if card['operation'] in ('agent.task','agent.history_review','prepare.ecdl_recovery','study.target_response','optimize.settling_criteria','optimize.target_feedforward','optimize.target_thermal'):
            from ldcon.web.agent_task_api import pin_invocation
            pin_invocation(self.root,job_id,card,saved_parameters,laser_id=laser['id'],meta=meta,deck=deck,invocations=definition['invocation_parameters'])
        atomic_json(directory/'job.json',meta);atomic_json(directory/'progress.json',{'status':'queued','simulated_s':0})
        if backend=='service':
            from ldcon.jobs.job_service import JobProcess
            self.processes[job_id]=JobProcess(directory)
            # Last write publishes a fully prepared, immutable job to the launcher.
            (directory/'launch.request').touch()
        else:
            environment=os.environ.copy();environment['PYTHONPATH']=str(source_root())+os.pathsep+environment.get('PYTHONPATH','')
            try:
                with (directory/'worker.log').open('ab') as stream:
                    process=subprocess.Popen([sys.executable,'-m','ldcon.interface_worker',str(directory)],
                        stdin=subprocess.DEVNULL,stdout=stream,stderr=stream,env=environment,
                        start_new_session=True,close_fds=True)
                self.processes[job_id]=process;meta['pid']=process.pid;atomic_json(directory/'job.json',meta)
            except OSError:
                atomic_json(directory/'progress.json',{'status':'failed','message':'Interface worker could not start'});raise
        return self.get_job(job_id)

    def request_online(self,job_id,card_id,overrides,*,request_id=None,reservation=None):
        request_id = request_id or uuid.uuid4().hex
        return self.operation_gateway().call('online.submit', self._operation_laser(job_id),
            (job_id, card_id, overrides), dict(request_id=request_id, reservation=reservation), request_id=request_id)

    def _request_online(self,job_id,card_id,overrides,*,request_id=None,reservation=None):
        from ldcon.jobs.online_pid import TERMINAL as ONLINE_TERMINAL
        from ldcon.core.interface_schema import parameters as online_parameters
        with self.mutex:
            self.assert_admission_open()
            job=self.get_job(job_id)
            self.assert_laser_policy_ready(job.get('laser',{}).get('id'))
            process=self.processes.get(job_id)
            if not process or process.poll() is not None or job['status']!='running' or not job['online_supported']:
                raise ConflictError('Online tuning requires a running stationary PID owner')
            directory=self.directory(job_id)
            definition=load_mapping((directory/'interface-deck.yaml').read_text())
            from ldcon.core.interface_schema import RETIRED_OPERATIONS,OPERATION_ALIASES
            for definition_card in definition['cards']:
                definition_card['operation']=OPERATION_ALIASES.get(definition_card['operation'],definition_card['operation'])
            card=next((c for c in definition['cards'] if c['id']==card_id and c['operation'] in ('tune.pid_factor_reduction','tune.pid_coordinate_search','study.ecdl_locked_current_map','tune.ecdl_window_center')),None)
            if card is None:raise ValueError('Online tuning card is absent from the running deck snapshot')
            card['operation']=OPERATION_ALIASES.get(card['operation'],card['operation'])
            if card['operation'] in RETIRED_OPERATIONS:raise ValueError('Retired card: '+RETIRED_OPERATIONS[card['operation']])
            effective=next((c for c in job.get('online_cards',[]) if c['id']==card_id),card)
            p=online_parameters(effective,overrides)
            live=read_json(directory/'controller-live.json',{})
            if live.get('controller_card_id')!=p['controller_card_id']:raise ConflictError('Online controller context is not ready or does not match')
            if card['operation']=='tune.ecdl_window_center' and (directory/'window-controller.json').exists():
                if p.get('mode')=='position':
                    from ldcon.web.online_position import validate
                    validate(directory,p)
                else:
                    from ldcon.jobs.window_control import enqueue
                    capability=read_json(directory/'window-controller.json',{})
                    enqueue(directory,dict(request_id=request_id or uuid.uuid4().hex,card_id=card_id,parameters=p,
                        capability_revision=capability['capability_revision'],
                        reason=reservation.get('reason','Reserved midpoint action') if reservation else 'Manual midpoint action'))
                    return self.get_job(job_id)
            if any(directory.glob('runtime-parameter-request-*.json')) or any(directory.glob('runtime-parameter-inflight-*.json')):
                raise ConflictError('A runtime parameter transaction is active')
            hot=read_json(directory/'hot-control-status.json',{})
            if (directory/'hot-control-request.json').exists() or hot.get('status') in ('queued','dispatching'):
                raise ConflictError('A hot controller operation is active')
            status=read_json(directory/'online-status.json',{})
            if (directory/'online-request.json').exists() or status and status.get('status') not in ONLINE_TERMINAL:
                raise ConflictError('Online tuning is already queued or active')
            request={'job_id':job_id,'request_id':request_id or uuid.uuid4().hex,'card_id':card_id,
                     'controller_card_id':p['controller_card_id'],'parameters':p,
                     'settings_revision':live['settings_revision'],'expires_unix_s':time.time()+p['timeout_s']}
            if reservation is not None:
                request['reservation']={k:reservation[k] for k in ('id','reason','prerequisites')}
            atomic_json(directory/'online-status.json',{'request_id':request['request_id'],'card_id':card_id,'status':'queued'})
            atomic_json(directory/'online-request.json',request)
            return self.get_job(job_id)

    def hot_control_status(self,job_id):
        self.get_job(job_id)
        return read_json(self.directory(job_id)/'hot-control-status.json',{})

    def runtime_parameters(self,job_id):
        self.get_job(job_id)
        directory=self.directory(job_id)
        live=read_json(directory/'runtime-parameters.json',{'status':'unavailable','schema':{},'parameters':{}})
        results=[read_json(path,{}) for path in directory.glob('runtime-parameter-result-*.json')]
        pending=[read_json(path,{}) for path in directory.glob('runtime-parameter-request-*.json')]
        return {**live,'capabilities':read_json(directory/'runtime-capabilities.json',{}),'last_result':max(results,key=lambda r:r.get('updated_unix_s',0),default=None),
                'pending_requests':pending}

    def laser_live(self,job_id):
        self.get_job(job_id)
        from ldcon.jobs.laser_live import state
        return state(self.directory(job_id))

    def inject_parameters(self,job_id,payload,*,approval_id=None,maintainer_reference=None,maintainer_token=None,actor='dashboard',preauthorized=None):
        if not isinstance(payload, dict):raise ValueError('Runtime patch must be a mapping')
        return self.operation_gateway().call('runtime.patch', self._operation_laser(job_id), (job_id, payload),
            dict(approval_id=approval_id, maintainer_reference=maintainer_reference, maintainer_token=maintainer_token,
                 actor=actor, preauthorized=preauthorized), request_id=payload.get('request_id'))

    def _inject_parameters(self,job_id,payload,*,approval_id=None,maintainer_reference=None,maintainer_token=None,actor='dashboard',preauthorized=None):
        """Queue a versioned runtime patch; safety-class fields need a direct human approval.

        Memory-only operational fields are free. A ``window_guard`` patch is
        classified field by field (traversal span/steps/limits are safety
        ranges, trigger/cooldown/averaging are free). The decision is audited
        in the laser's approval ledger; the worker still revalidates the patch.
        """
        from ldcon.jobs.runtime_parameters import enqueue_runtime_patch,busy_safe_patch
        from ldcon.core.agent_authority import ApprovalLedger,describe_changes,authorize,partition,SAFETY
        with self.mutex:
            self.assert_admission_open()
            job=self.get_job(job_id)
            if job['status']!='running':raise ConflictError('Runtime changes require a running card')
            if payload.get('job_id')!=job_id:raise ValueError('Job context differs')
            directory=self.directory(job_id)
            online=read_json(directory/'online-status.json',{})
            hot=read_json(directory/'hot-control-status.json',{})
            live=read_json(directory/'runtime-parameters.json',{})
            schema=live.get('schema',{})
            if not busy_safe_patch(payload.get('patch'),schema) and ((directory/'online-request.json').exists() or (directory/'hot-control-request.json').exists()
                    or online.get('status') in ('queued','running')
                    or hot.get('status') in ('queued','dispatching')):
                raise ConflictError('Another controller operation is active')
            patch=payload.get('patch') if isinstance(payload.get('patch'),dict) else {}
            current=live.get('parameters',{})
            rows=describe_changes('runtime',{k:current.get(k) for k in patch},{k:patch[k] for k in patch})
            laser_id=job.get('laser',{}).get('id');ledger=ApprovalLedger(self.root,laser_id) if laser_id else None
            scope=dict(kind='runtime',job_id=job_id,card_id=payload.get('card_id'))
            if preauthorized is not None:
                # Mirror of an already authorized assignment save (same values); not consumed again.
                decision={**preauthorized,'mirrored':True}
            else:
                decision=authorize(ledger,kind='runtime',scope=scope,rows=rows,approval_id=approval_id,
                                   maintainer_reference=maintainer_reference,maintainer_token=maintainer_token)
            request=enqueue_runtime_patch(directory,payload)
            if ledger is not None and rows:
                result=dict(job_id=job_id,request_id=payload.get('request_id'),expected_revision=payload.get('expected_revision'),status=request.get('status'))
                if decision.get('authority')=='human_approval' and preauthorized is None:
                    ledger.consume(approval_id,scope=scope,safety_rows=partition(rows)[SAFETY],result=result)
                ledger.audit(dict(event='runtime_patch_queued',actor=actor,changes=rows,result=result,**decision))
            return {**request,'authority':decision}

    def request_hot_control(self,job_id,payload):
        if not isinstance(payload, dict):raise ValueError('Hot control must be a mapping')
        return self.operation_gateway().call('controller.command', self._operation_laser(job_id),
            (job_id, payload), {}, request_id=payload.get('request_id'))

    def _request_hot_control(self,job_id,payload):
        from ldcon.owners.ecdl_hot_control import command
        if not isinstance(payload,dict) or set(payload)!={'request_id','controller_card_id','settings_revision','command'}:
            raise ValueError('Hot control needs id, controller, revision and command')
        if not isinstance(payload['request_id'],str) or not re.fullmatch('[0-9a-f]{32}',payload['request_id']):raise ValueError('Invalid request id')
        c=command(payload['command'])
        with self.mutex:
            self.assert_admission_open()
            job=self.get_job(job_id);process=self.processes.get(job_id)
            if not process or process.poll() is not None or job['status']!='running' or not job['online_supported']:
                raise ConflictError('Hot control requires an active stationary worker')
            if not any(x.get('card')=='servo.ecdl_steady_lock' for x in read_json(self.directory(job_id)/'job.json',{}).get('cards',[])):
                raise ConflictError('Hot control requires the ECDL stationary card')
            directory=self.directory(job_id);live=read_json(directory/'controller-live.json',{})
            if any(payload[k]!=live.get(k) for k in ('controller_card_id','settings_revision')):raise ConflictError('Hot control context changed')
            result=directory/('hot-control-result-'+payload['request_id']+'.json')
            if result.exists():return read_json(result)
            online=read_json(directory/'online-status.json',{})
            hot=read_json(directory/'hot-control-status.json',{})
            if hot.get('request_id')==payload['request_id']:return hot
            if any(directory.glob('runtime-parameter-request-*.json')) or any(directory.glob('runtime-parameter-inflight-*.json')):
                raise ConflictError('A runtime parameter transaction is active')
            if ((directory/'online-request.json').exists() or online and online.get('status') not in ('succeeded','failed','cancelled','rejected')
                or (directory/'hot-control-request.json').exists() or hot and hot.get('status') in ('queued','dispatching')):
                raise ConflictError('Another controller command is queued or running')
            request=dict(payload,job_id=job_id,command=c,expires_unix_s=time.time()+15)
            status=dict(request_id=payload['request_id'],status='queued')
            atomic_json(directory/'hot-control-status.json',status)
            atomic_json(directory/'hot-control-request.json',request)
            return status

    def sweep_trigger(self,job_id,payload,*,wait_s=8.,poll_s=.05):
        # Refusals before publication consume no request ID. The physical shot
        # request is scoped to its immutable parent job.
        self._sweep_trigger_context(job_id, payload)
        return self.operation_gateway().call('sweep.trigger', self._operation_laser(job_id),
            (job_id, payload), dict(wait_s=wait_s, poll_s=poll_s),
            request_id=job_id + ':' + payload['request_id'], identity=dict(job_id=job_id, payload=payload))

    def _sweep_trigger_context(self, job_id, payload, *, existing_only=False):
        if not isinstance(payload,dict):raise ValueError('Trigger payload must be a mapping')
        request_id=str(payload.get('request_id',''));plan_sha256=str(payload.get('plan_sha256',''))
        if not re.fullmatch(r'[0-9a-f]{32}',request_id) or not re.fullmatch(r'[0-9a-f]{64}',plan_sha256):
            raise ValueError('request_id (32 hex) and plan_sha256 (64 hex) are required')
        job=self.get_job(job_id);shot=job.get('shot_sweep') or {}
        directory=self.directory(job_id);path=directory/'sweep-trigger.json'
        if shot.get('request_id')!=request_id or shot.get('plan_sha256')!=plan_sha256:
            raise ConflictError('Shot identity mismatch: this job does not hold that request_id/plan_sha256')
        record=read_json(path,None)
        if record is None:
            if existing_only:raise ConflictError('Trigger intent is absent; prior outcome requires inspection, no replay')
            if job['status'] in TERMINAL or (directory/'cancel.request').exists():raise ConflictError('Shot has been stopped')
            if shot.get('trigger_mode')!='software':raise ConflictError('Shot is not software-triggered (trigger_mode=%s)'%shot.get('trigger_mode'))
            if shot.get('state')!='ready' or not shot.get('armed'):raise ConflictError('Shot is not ready (state=%s, armed=%s)'%(shot.get('state'),shot.get('armed')))
        return request_id, plan_sha256, path, record

    def _sweep_trigger(self,job_id,payload,*,wait_s=8.,poll_s=.05,_existing_only=False):
        """POST /api/jobs/{job}/sweep-trigger {request_id, plan_sha256}: fire the armed software-triggered shot
        at most once. Refused (409) unless shot_sweep.state == 'ready' and armed, on an identity mismatch, or
        after the stop. A repeat for the same request_id returns the same fired record and never fires again."""
        from ldcon.jobs.native_ff_proxy import write_once
        with self.mutex:
            request_id, plan_sha256, path, record = self._sweep_trigger_context(job_id, payload, existing_only=_existing_only)
            if record is None:
                record=write_once(path,dict(request_id=request_id,plan_sha256=plan_sha256,job_id=job_id,requested_unix_s=time.time()))
        deadline=time.monotonic()+wait_s
        while True:
            job=self.get_job(job_id);shot=job.get('shot_sweep') or {}
            result=dict(request_id=request_id,plan_sha256=plan_sha256,job_id=job_id,requested_unix_s=record['requested_unix_s'],
                        forwarded_unix_s=shot.get('trigger_forwarded_unix_s'),fired=bool(shot.get('fired')),fired_unix_s=shot.get('fired_unix_s'),
                        fire_ack_unix_s=shot.get('fire_ack_unix_s'),awg_trigger_ack=shot.get('awg_trigger_ack'),
                        playback_started_unix_s=shot.get('playback_started_unix_s'),state=shot.get('state'),
                        timing_note='host wall-clock stamps; the HTTP request, the SCPI write and the sequencer are not hard-synchronized')
            if result['fired']:return result
            if shot.get('trigger_refused'):raise ConflictError('Trigger refused by the shot: '+str(shot['trigger_refused']))
            if job['status'] in TERMINAL or shot.get('state') in ('restoring','restored','review_required','rejected'):
                raise ConflictError('Shot ended before the trigger fired (state=%s)'%shot.get('state'))
            if time.monotonic()>=deadline:raise TriggerPending(result)
            time.sleep(poll_s)

    def stop_online(self,job_id,request_id=None):
        status = self.get_job(job_id).get('online_tuning') or {}
        current = status.get('request_id')
        if request_id is not None and request_id != current:
            raise ConflictError('Cancellation must name the active subordinate request')
        return self.operation_gateway().call('online.cancel', self._operation_laser(job_id),
            (job_id,), dict(request_id=current), request_id=current or ('idle.' + job_id))

    def _stop_online(self,job_id,request_id=None):
        with self.mutex:
            job=self.get_job(job_id);status=job['online_tuning'] or {}
            if status.get('request_id') != request_id:
                raise ConflictError('Subordinate changed before cancellation; no other request was cancelled')
            if request_id:atomic_json(self.directory(job_id)/'online-cancel.json',{'request_id':request_id})
            return job

    @synchronized
    def telemetry(self, job_id):
        cached=self._chart_cache.get(job_id)
        if cached and time.monotonic()-cached[0]<1:return cached[1]
        directory=self.directory(job_id)
        database=next((directory/name for name in ('telemetry.sqlite3','telemetry.sqlite')
                       if (directory/name).is_file()),None)
        result={'trace':[],'events':[]}
        if database:
            with sqlite3.connect(database.as_uri()+'?mode=ro',uri=True,timeout=.5) as db:
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='telemetry'").fetchone():
                    traces=db.execute('''SELECT simulated_s,payload_json FROM (
                        SELECT simulated_s,payload_json,ROW_NUMBER() OVER (ORDER BY id) n,
                        COUNT(*) OVER () total FROM telemetry WHERE kind='trace'
                        ) WHERE n=1 OR n=total OR n % MAX(1,CAST(total/500 AS INTEGER))=0
                        ORDER BY simulated_s LIMIT 602''').fetchall()
                    result['trace']=[{'time_s':t,**json.loads(payload)} for t,payload in traces]
                    rows=db.execute('''SELECT simulated_s,kind,payload_json FROM telemetry
                        WHERE kind NOT IN ('trace','quality_window','noise_window','export_status')
                        ORDER BY id DESC LIMIT 80''').fetchall()
                    for t,kind,payload in reversed(rows):
                        value=json.loads(payload)
                        if kind=='card_end':value={k:v for k,v in value.items() if k!='result'}
                        result['events'].append({'time_s':t,'kind':kind,'payload':value})
        result['latest']=read_json(directory/'readings.json',{})
        if not result['trace']:result['trace']=read_json(directory/'live-trace.json',[])
        self._chart_cache[job_id]=(time.monotonic(),result)
        return result

    @synchronized
    def close(self):
        # Closing the dashboard never cancels, waits for or signals instrument work.
        if self._operations is not None:
            self._operations.close()
            self._operations = None
        self._lock_file.close()

    def job_metadata(self):
        paths=list(self.jobs.glob('*/job.json'))+list(self.jobs.glob('*/*/job.json'))
        return [p for p in paths if not p.is_symlink() and not p.parent.is_symlink() and not p.parent.parent.is_symlink()]

    @synchronized
    def _lock_report_locked(self, job_id, fresh_s=30.):
        """The lock's own fresh per-cycle report (lock-target.json) says locked."""
        try:
            live = read_json(self.directory(job_id) / 'lock-target.json', {}) or {}
        except Exception:
            return False
        stamp = live.get('updated_unix_s')
        return live.get('state') == 'locked' and isinstance(stamp, (int, float)) and 0 <= time.time() - stamp <= fresh_s

    def directory(self, job_id):
        safe_id(job_id)
        # Resolve this ID directly. Never rescan every job for every row of a list.
        candidates=list(self.jobs.glob(job_id+'/job.json'))+list(self.jobs.glob('*/'+job_id+'/job.json'))
        candidates=[p for p in candidates if not p.is_symlink() and not p.parent.is_symlink() and not p.parent.parent.is_symlink()]
        if len(candidates)!=1:raise FileNotFoundError(job_id)
        directory=candidates[0].parent
        if directory.is_symlink() or directory.parent.is_symlink():raise FileNotFoundError(job_id)
        return directory

    @synchronized
    def get_job(self, job_id):
        directory=self.directory(job_id)
        meta=read_json(directory/'job.json',{})
        state=read_json(directory/'progress.json',{})
        if not isinstance(state,dict):state={}
        state.setdefault('status','unknown')
        process=self.processes.get(job_id)
        if process and process.poll() is not None and state.get('status') not in TERMINAL:
            state={**state,'status':'interrupted','message':'Worker exited without a completed result'}
            atomic_json(directory/'progress.json',state)
        if (directory/'cancel.request').exists() and state.get('status') not in TERMINAL:
            state={**state,'status':'stopping'}
        if meta.get('engine')=='interface-cards' and process and process.poll() is None and state.get('status') in TERMINAL:
            state={**state,'status':'finishing','message':'Worker is releasing resources'}
        if state.get('status') not in TERMINAL and not (directory/'worker-process.json').exists():
            state={**state,'worker_attachment':'pending_or_unverified'}
        elif (directory/'worker-process.json').exists():
            state={**state,'worker_attachment':'attached' if process and process.poll() is None else 'exited'}
        summary=read_json(directory/'summary.json',{})
        compact={k:v for k,v in summary.items() if k not in
                 ('events','cards','deck_snapshot','parameters','limitations','artifact','trace')}
        if summary.get('cards'):
            compact['card_results']={name:{k:v for k,v in result.items() if
                v is None or isinstance(v,(str,int,float,bool)) or
                (isinstance(v,list) and len(v)<=12 and all(isinstance(x,(int,float,str)) for x in v))}
                for name,result in summary['cards'].items() if isinstance(result,dict)}
        online=read_json(directory/'online-status.json',{})
        if state.get('status') in TERMINAL and online and online.get('status') not in ('succeeded','unchanged','failed','cancelled','rejected'):
            online={**online,'status':'unknown' if (directory/'window-controller.json').exists() else 'cancelled','reason':'Stationary lock has ended; inspect operation evidence'}
        from ldcon.agents.teams_card import history as notification_history
        deliveries=notification_history(self.root,job_id)
        if deliveries:compact['notification_deliveries']=deliveries
        from ldcon.web.shot_analysis_api import overlay_job   # sweep API v1.5: outcome.analysis pending -> ready|unavailable
        state=overlay_job(directory,dict(state))
        return {**meta,**state,'summary':compact,'online_tuning':online,
                'live_controller':read_json(directory/'controller-live.json',{}),
                'window_controller':read_json(directory/'window-controller.json',{}),
                'agent':read_json(directory/'agent-status.json',{}),
                'stop_reconciliation':read_json(directory/'operating-point-stop-reconciliation.json',None),
                'agent_watch':read_json(directory/'agent-log-watch-status.json',{}),
                'online_supported':meta.get('engine')=='interface-cards' and any(c.get('card') in ('servo.current_pid','servo.slow_pid','servo.ecdl_steady_lock','servo.native_current') for c in meta.get('cards',[]))}

    def _registry(self):
        from ldcon.web.job_registry import JobRegistry
        if not hasattr(self,'_admission_jobs'):self._admission_jobs=JobRegistry(self)
        return self._admission_jobs

    @synchronized
    def job_headers(self, set_id=None):
        """Small metadata/status only, for evidence and duplicate-ID checks."""
        return [row for row,_ in self._registry().headers() if set_id is None or row.get('set_id')==set_id]

    @synchronized
    def job_terminal_confirmed(self,job_id):
        return self._registry().header(self.directory(job_id))[1]

    @synchronized
    def active_jobs(self, set_id=None):
        """Current control work, including queued/unknown jobs and live cleanup.

        Only these jobs need expanded runtime state. Historical summaries,
        notification delivery history and analysis belong to list_jobs().
        """
        result=[]
        for header,terminal in self._registry().headers():
            if terminal or (set_id is not None and header.get('set_id')!=set_id):continue
            job=self.get_job(header['id'])
            if header['status'] in TERMINAL and job['status'] in TERMINAL:
                job={**job,'status':'finishing','message':'Worker exit is not confirmed'}
            if job['status'] not in TERMINAL:result.append(job)
        return sorted(result,key=lambda j:j.get('created_utc',''),reverse=True)

    @synchronized
    def list_jobs(self, set_id=None, unassigned=False):
        """Expanded history; never use this for control admission."""
        jobs=[self.get_job(p.parent.name) for p in self.job_metadata()]
        if set_id is not None:jobs=[j for j in jobs if j.get('set_id')==set_id]
        if unassigned:jobs=[j for j in jobs if not j.get('set_id')]
        return sorted(jobs,key=lambda j:j.get('created_utc',''),reverse=True)

    def stop(self, job_id):
        result=self.operation_gateway().call('job.cancel', self._operation_laser(job_id),
            (job_id,), {}, request_id=job_id)
        # A repeated accepted cancellation is intentionally not replayed by the
        # gateway. Inspect the orphan proxy after that idempotent boundary too,
        # so the ordinary Web and agent Stop actions can acknowledge a dead
        # parent's queued request without killing any worker or touching outputs.
        from ldcon.jobs.operating_point_proxy import reconcile_stopped_proxy
        with self.mutex:
            reconciliation=reconcile_stopped_proxy(self,job_id)
            if reconciliation is not None:
                return dict(self.get_job(job_id),stop_reconciliation=reconciliation)
        return result

    def _stop_job(self, job_id):
        with self.mutex:
            state=self.get_job(job_id)
            if state['status'] not in TERMINAL:
                (self.directory(job_id)/'cancel.request').touch()
            return self.get_job(job_id)


    def window_status(self, job_id):
        self.get_job(job_id)
        from ldcon.jobs.window_control import state
        return state(self.directory(job_id))

    def request_window(self, job_id, payload, *, source='manual'):
        if not isinstance(payload, dict):raise ValueError('Midpoint request must be a mapping')
        request_id = payload.get('request_id') or uuid.uuid4().hex
        return self.operation_gateway().call('window.submit', self._operation_laser(job_id),
            (job_id, dict(payload, request_id=request_id)), dict(source=source), request_id=request_id)

    def _request_window(self, job_id, payload, *, source='manual'):
        from ldcon.jobs.window_control import enqueue
        self.assert_admission_open()
        if self.get_job(job_id)['status'] != 'running':
            raise ValueError('Midpoint requests require an active steady worker')
        if source not in ('manual', 'agent'):
            raise ValueError('Unsupported midpoint request source')
        return enqueue(self.directory(job_id), payload, source=source)

    def amend_window(self, job_id, request_id, data):
        if not isinstance(data, dict):raise ValueError('Midpoint edit must be a mapping')
        return self.operation_gateway().call('window.amend', self._operation_laser(job_id),
            (job_id, request_id, data), {}, request_id=request_id + ':' + str(data.get('expected_revision')))

    def _amend_window(self, job_id, request_id, data):
        from ldcon.jobs.window_actions import WindowQueue
        from ldcon.jobs.window_control import validate_selection
        self.assert_admission_open()
        if self.get_job(job_id)['status'] != 'running':
            raise ValueError('Midpoint requests require an active steady worker')
        d = self.directory(job_id)
        c = read_json(d/'window-controller.json', {})
        if set(data)-{'expected_revision','card_id','parameters'}:raise ValueError('Unknown midpoint edit fields')
        old = WindowQueue(d).get(request_id)
        if not old or old['source'] != 'manual':raise ValueError('Only unclaimed manual requests can be edited')
        card_id = data.get('card_id', old['card_id'])
        p = validate_selection(dict(cards=c['cards']), c['controller_card_id'],
            dict(c['config'], card_id=card_id, parameters=data.get('parameters', old['parameters'])))
        return WindowQueue(d).amend(request_id, data['expected_revision'], card_id=card_id, parameters=p)

    def cancel_window(self, job_id, request_id, *, expected_revision=None):
        return self.operation_gateway().call('window.cancel', self._operation_laser(job_id),
            (job_id, request_id), dict(expected_revision=expected_revision), request_id=request_id)

    def _cancel_window(self, job_id, request_id, *, expected_revision=None):
        from ldcon.jobs.window_actions import WindowQueue
        # Cleanup is not denied because a policy/observation/admission revision changed.
        self.get_job(job_id)
        return WindowQueue(self.directory(job_id)).request_cancel(request_id, expected_revision=expected_revision)

    def change_device_parameters(self, laser_id, payload, *, actor='dashboard', dry_run=False, request_id=None):
        return self.operation_gateway().call('device.change', laser_id, (laser_id, payload),
            dict(actor=actor, dry_run=dry_run), request_id=request_id)

    def _change_device_parameters(self, laser_id, payload, *, actor='dashboard', dry_run=False):
        from ldcon.jobs.dashboard_api import Dashboard
        from ldcon.jobs.device_parameters import RegistryLink, change
        from ldcon.core.agent_authority import ApprovalLedger
        self.assert_admission_open()
        document = self.interface_repository.read('lasers', safe_id(laser_id))['document']
        return change(document, payload, registry=RegistryLink(), ledger=ApprovalLedger(self.root, laser_id),
                      dashboard=lambda: Dashboard.from_state_dir(self.root), actor=actor, dry_run=dry_run)
