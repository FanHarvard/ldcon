"""One selected interface card, frozen laser/deck definitions, real wall clock."""
import json
import os
from pathlib import Path
import time
import traceback
from ldcon.core.interface_schema import load_mapping,digest,OPERATION_ALIASES
from ldcon.core.interface_cards import monitor_wavelength,acquire_temperature,CardCancelled
from ldcon.jobs.interface_devices import open_interfaces
from ldcon.core.telemetry import TelemetryStore
from ldcon.core.telegraf import TelegrafExporter
from ldcon.core.records import atomic_json,read_json,utc_now
from ldcon.jobs.progress import ProgressSink,CancelledRun


def normalize_card_result(result):
    """Separate a finished inconclusive procedure from an interrupted worker."""
    if result.get('status') in ('hop_returned','unresolved','invalidated','measurement_incomplete'):
        return {**result,'card_outcome':result['status'],'status':'inconclusive'}
    return result


def _controller_status_code(kind, payload):
    """Retain a trace's explicit display state before the legacy lock fallback."""
    from ldcon.web.grafana_dashboard import STATE_CODES
    if kind not in ('state', 'trace'):
        return None
    code = STATE_CODES.get(payload.get('state'))
    if code is not None or kind == 'state':
        return code
    return 3 if payload.get('locked') else 8


def _quality_baseline_event(baseline, kind, payload):
    """Optional publication failures never change an accepted physical outcome."""
    if baseline is None or kind not in ('quality_adjustment_accepted','ecdl_lock_cycle'):
        return None
    try:
        if kind=='quality_adjustment_accepted':
            return baseline.accepted(payload['family'],payload['action_id'],payload['accepted_unix_s'])
        return baseline.observe(payload)
    except Exception as error:
        # This block has no control calls. Keep a broken diagnostic out of the
        # surrounding lease, write-uncertainty and physical shutdown paths.
        return dict(state='error',persisted=False,reason=type(error).__name__+': '+str(error)[:240])


def _quality_baseline_requires_dither(laser):
    device=laser.get('interfaces',{}).get('lock',{}).get('device')
    settings=laser.get('devices',{}).get(device,{}).get('control_settings',{})
    return settings.get('steady_lock',{}).get('dither',{}).get('verified') is True


def run(directory):
    from ldcon.core.execution_context import invocation_context
    from ldcon.jobs.operation_execution import RunExecution,frozen_inputs
    directory=Path(directory)
    meta=read_json(directory/'job.json',{})
    frozen=frozen_inputs(directory)
    with RunExecution(directory,frozen) as execution:
        previous=execution.begin()
        if previous is not None:return previous
        with invocation_context(directory.name,meta.get('failure_context',[])):
            return _run(directory,execution=execution,frozen=frozen)


def _run(directory, *, execution, frozen):
    directory=Path(directory);started=time.monotonic();ports={};status='failed';message=None;result={};runtime=None
    laser=frozen['laser'];deck=frozen['deck'];invocation=frozen['invocation'];card=frozen['card']
    from ldcon.jobs.operation_history import optional_history,optional_call
    history=optional_history(directory,frozen)
    p=invocation['parameters'];sink=ProgressSink(directory);sink.state['active_card']=card['id']
    baseline = None
    if card['operation'] == 'servo.ecdl_steady_lock' and directory.parent.name == 'jobs':
        from ldcon.jobs.quality_baseline import QualityBaseline
        baseline = QualityBaseline(directory.parent.parent/'quality-baseline.json',
            laser_id=laser['id'], controller_job_id=directory.name,
            require_dither=_quality_baseline_requires_dither(laser))
    sink.state['controller_state']='exporting' if card['operation']=='report.grafana_dashboard' else 'monitoring' if card['operation']=='monitor.wavelength' else 'acquiring'
    def cancelled():return (directory/'cancel.request').exists()
    endpoint=laser.get('telemetry',{}).get('telegraf_endpoint')
    endpoint=endpoint or os.environ.get('LDCON_TELEGRAF_ENDPOINT')
    exporter=TelegrafExporter(endpoint,laser['name'],'laserlab',simulation=False,
        tags=dict(laser_id=laser['id'],card=card['id'],operation=card['operation'],deck=deck['id'],job=directory.name)) if endpoint else None
    with TelemetryStore(directory/'telemetry.sqlite3',exporter=exporter) as store:
        run_id=store.start_run({'laser':laser,'deck':deck,'invocation':invocation,'time_basis':'wall'})
        baseline_diagnostic=None
        def emit(kind,payload,*,fields=None,tags=None):
            nonlocal baseline_diagnostic
            elapsed=time.monotonic()-started
            diagnostic=_quality_baseline_event(baseline,kind,payload)
            if diagnostic and diagnostic.get('state')!='absent':
                signature=(diagnostic.get('state'),diagnostic.get('reason'),diagnostic.get('persisted'))
                if signature!=baseline_diagnostic:
                    baseline_diagnostic=signature
                    try:store.record(run_id,elapsed,'quality_baseline_status',diagnostic)
                    except Exception:pass  # Optional diagnostics cannot add a control failure.
            if fields is not None or tags is not None:
                payload={**payload,'_telemetry':dict(fields=fields or {},tags=tags or {})}
            if kind=='command_applied':
                payload={**payload,'action_event':1}
            store.record(run_id,elapsed,kind,payload)
            optional_call(history,'event',kind,payload,elapsed_s=elapsed,run_id=run_id,
                          monotonic_s=time.monotonic())
            if card['operation']!='report.grafana_dashboard' and kind in ('state','trace'):
                code=_controller_status_code(kind,payload)
                if code is not None:store.record(run_id,elapsed,'controller_status',{'controller_state_code':code})
            if kind=='command_applied':
                if 'temperature_c' in payload:sink.readings['temperature_setpoint_c']=payload['temperature_c']
                if 'current_a' in payload:sink.readings['current_setpoint_a']=payload['current_a']
            sink.send(run_id,elapsed,kind,{'time_s':elapsed,**payload} if kind=='trace' else payload)
            from ldcon.core.card_reporting import event_samples
            for sample_kind,sample in event_samples(kind,payload):
                emit(sample_kind,sample)
        try:
            if cancelled():raise CardCancelled('Cancelled before opening interfaces')
            if card['operation']=='servo.ecdl_steady_lock':
                from ldcon.jobs.ecdl_steady import reject_retired
                reject_retired(p)
            from ldcon.jobs.workflow_artifacts import hydrate_inputs
            p=hydrate_inputs(directory,p,invocation.get('input_artifacts',{}))
            from ldcon.core.operation_registry import normalize_parameters
            p=normalize_parameters(card,p)
            if card['operation']=='tune.ecdl_operating_point' and p.get('target_frequency_hz') is None and p.get('target_wavelength_nm') is None:
                # ONE target source (rule F 第 3 例): a null alignment target is the laser target, fixed at start (the
                # runtime copies this value, so it is set before CardRuntime captures the hot parameters).
                from ldcon.jobs.lock_target import resolve_target
                p['target_frequency_hz']=resolve_target(laser,p)
            if card['operation']=='recover.ecdl_mode_hop':
                from ldcon.jobs.recovery_history import load as load_recovery_history
                p=load_recovery_history(directory,laser,p)
            optional_call(history,'parameters_resolved',p)
            from ldcon.agents.operating_mode_client import observation as mode_observation
            mode=mode_observation(laser)
            atomic_json(directory/'operating-mode-at-start.json',mode)
            emit('operating_mode_at_start',mode)
            from ldcon.agents.adjustment_guidance import for_job as guidance_snapshot
            atomic_json(directory/'adjustment-guidance-at-start.json',guidance_snapshot(directory,laser['id']))
            execution.dispatching()
            ports={} if card['operation'] in ('report.grafana_dashboard','plan.ecdl_recovery','agent.resident','agent.request','agent.task','agent.monitor','agent.history_review','notify.teams','analyze.shot_sweep','tune.sweep_tracking','sweep.dlcpro_awg') else open_interfaces(laser,card)
            emit('card_start',{'card':card['id'],'operation':card['operation']})
            from ldcon.core.execution_context import current_context
            for failure in current_context().upstream_failures:
                emit('upstream_failure',failure)
            def make_online(actuator):
                from ldcon.jobs.online_pid import OnlinePIDTuner
                from ldcon.jobs.pi_algorithms import ALGORITHMS as PI_ALGORITHMS, parameters as online_parameters
                def live():
                    atomic_json(directory/'controller-live.json',{'controller_card_id':card['id'],
                        'settings_revision':digest(actuator.config),'config':actuator.config})
                def publish(value):
                    live();atomic_json(directory/'online-status.json',value)
                def receive():
                    path=directory/'online-request.json'
                    if not path.exists():return None
                    request=read_json(path);path.unlink()
                    matching=next((c for c in deck['cards'] if c['id']==request.get('card_id') and c['operation'] in PI_ALGORITHMS),None)
                    if (not matching or request.get('job_id')!=directory.name or request.get('controller_card_id')!=card['id']
                        or request.get('settings_revision')!=digest(actuator.config) or time.time()>request.get('expires_unix_s',0)):
                        publish({'request_id':request.get('request_id'),'card_id':request.get('card_id'),'status':'rejected','reason':'Inactive, expired or mismatched controller request'})
                        return None
                    request['parameters']=online_parameters(matching,request['parameters'])
                    request['operation']=matching['operation']
                    if request['parameters']['controller_card_id']!=card['id']:raise ValueError('Online PID target context does not match the active card')
                    return request
                def cancel_request(request):return read_json(directory/'online-cancel.json',{}).get('request_id')==request['request_id']
                live()
                return OnlinePIDTuner(receive,publish,emit,time.monotonic,cancel_request)
            arguments=dict(clock=time.monotonic,sleep=time.sleep,cancel=cancelled,emit=emit)
            from ldcon.jobs.card_runtime import CardRuntime,SUPPORTED
            if card['operation'] in SUPPORTED:
                runtime=CardRuntime(directory,card,p,emit,laser=laser)
            def steady_runtime(values,lock):
                nonlocal runtime
                runtime=CardRuntime(directory,card,values,emit,lock=lock,laser=laser)
                return runtime
            if card['operation']=='identify.axis_frequency':
                from ldcon.jobs.axis_frequency import run as run_axis_calibration
                result=run_axis_calibration(ports,p,directory,**arguments)
            elif card['operation'] in ('servo.dlcpro_frequency','sweep.dlcpro_frequency'):
                from ldcon.jobs.dlcpro_cards import run as run_dlc_card
                result=run_dlc_card(ports,laser,card,p,**arguments)
            elif card['operation']=='notify.teams':
                from ldcon.agents.teams_card import submit
                result=submit(directory,p)
            elif card['operation']=='acquire.metrics':
                from ldcon.jobs.metric_cards import acquire_metrics
                result=acquire_metrics(ports['metrics'],p,**arguments)
            elif card['operation']=='agent.resident':
                from ldcon.agents.resident_agent import run_resident
                result=run_resident(directory,p,**arguments)
            elif card['operation']=='agent.request':
                from ldcon.agents.resident_agent import submit
                result=submit(directory,p)
            elif card['operation'] in ('agent.task','agent.history_review'):
                from ldcon.agents.agent_task import run as run_task
                result=run_task(directory,p,**arguments)
            elif card['operation']=='agent.monitor':
                from ldcon.agents.agent_monitor import run as run_monitor
                result=run_monitor(directory,p,runtime=runtime,**arguments)
            elif card['operation']=='servo.native_current':
                from ldcon.jobs.native_current_card import run_native_current
                from ldcon.jobs.laser_live import LaserLive
                live=LaserLive(directory,laser,card,emit,port=ports['native_current'])
                from ldcon.jobs.native_ff_proxy import feedforward_route,attachment
                feedforward=attachment(directory,laser,card_cancel=cancelled,emit=emit) if feedforward_route(laser) else None
                from ldcon.jobs.lock_target import resolve_target
                live.follow_laser_target=p.get('target_frequency_hz') is None and p.get('target_wavelength_nm') is None and resolve_target(laser,{}) is not None
                from ldcon.jobs.native_current_card import notify_measurement_timeout
                notifier=lambda notice,outage,_card=card['id']:notify_measurement_timeout(directory,_card,notice,outage)
                result=run_native_current(ports['native_current'],p,online=make_online,live=live,feedforward=feedforward,directory=directory,laser=laser,measurement_notifier=notifier,**arguments)
            elif card['operation']=='tune.native_current':
                from ldcon.jobs.native_current_acquire import run_native_acquire
                result=run_native_acquire(ports['native_current'],p,laser=laser,**arguments)
            elif card['operation']=='sweep.dlcpro_awg':
                from ldcon.jobs.dlcpro_awg_card import run as run_dlc_awg
                def publish_dlc_awg(value):
                    sink.state['shot_sweep']=value
                    if value.get('outcome') is not None:sink.state['outcome']=value['outcome']
                    atomic_json(directory/'progress.json',{**sink.state,'updated_utc':utc_now()})
                result=run_dlc_awg(directory,laser,card,p,publish=publish_dlc_awg,**arguments)
            elif card['operation'].startswith('sweep.') and (__import__('ldcon.jobs.native_ff_proxy',fromlist=['feedforward_route']).feedforward_route(laser,card['uses'][0])
                                                            or __import__('ldcon.jobs.ecdl_locked_sweep_proxy',fromlist=['rig_locked_route']).rig_locked_route(laser,card['uses'][0])):
                # Locked staircase: this job proxies a session inside the running lock job (sr1092: native FF chain
                # AWG -> ESP ADC -> firmware FF; sr422: the ECDL rig owner's locked sweep). labscript holds THIS id.
                from ldcon.jobs.native_ff_proxy import feedforward_route,NativeFFRoute,run_locked_proxy
                from ldcon.jobs.ecdl_locked_sweep_proxy import RigRoute
                def publish_proxy(value):
                    sink.state['shot_sweep']=value
                    if isinstance(value,dict) and value.get('outcome') is not None:
                        # 2026-10-23: the shot's verdict on GET jobs/<proxy> (top level) and in shot_sweep.outcome
                        sink.state['outcome']=value['outcome']
                    atomic_json(directory/'progress.json',{**sink.state,'updated_utc':utc_now()})
                route=NativeFFRoute(laser) if feedforward_route(laser,card['uses'][0]) else RigRoute(laser)
                result=run_locked_proxy(directory,laser,card,p,route,publish=publish_proxy,**arguments)
            elif card['operation'].startswith('sweep.'):
                from ldcon.jobs.modulation_sweep import run_sweep
                def publish_sweep(value):
                    sink.state['shot_sweep']=value
                    atomic_json(directory/'progress.json',{**sink.state,'updated_utc':utc_now()})
                def save_sweep(plan):
                    import numpy as np
                    np.savez_compressed(directory/'sweep-waveforms.npz',
                        **{role:wave['codes'] for role,wave in plan.waves.items()})
                    atomic_json(directory/'sweep-plan.json',dict(metadata=plan.metadata,
                        waves={role:{k:v for k,v in wave.items() if k not in ('codes','levels')}
                               for role,wave in plan.waves.items()}))
                result=run_sweep(ports['modulation'],p,publish=publish_sweep,save_plan=save_sweep,**arguments)
            elif card['operation']=='optimize.sweep_feedforward':
                from ldcon.jobs.sweep_ff_optimization import run_card as run_sweep_ff
                def publish_sweep_ff(value):
                    sink.state['sweep_ff_optimization']=value
                    atomic_json(directory/'progress.json',{**sink.state,'updated_utc':utc_now()})
                result=run_sweep_ff(directory,laser,card,p,publish=publish_sweep_ff,**arguments)
            elif card['operation']=='tune.sweep_tracking':
                # rule F one-shot (2026-10-23): sweep tracking assessment / mechanical recommendation (no ports)
                def publish_tracking(value):
                    sink.state['sweep_tracking']=value
                    atomic_json(directory/'progress.json',{**sink.state,'updated_utc':utc_now()})
                from ldcon.jobs.sweep_tracking import run_card as run_tracking
                result=run_tracking(directory,laser,card,p,publish=publish_tracking,**arguments)
            elif card['operation']=='analyze.shot_sweep':
                # rule F one-shot (2026-10-23): read-only post-shot analysis of a sweep proxy job (no ports)
                def publish_analysis(value):
                    sink.state['shot_analysis']=value
                    atomic_json(directory/'progress.json',{**sink.state,'updated_utc':utc_now()})
                from ldcon.jobs.shot_analysis import run_card as run_shot_analysis
                result=run_shot_analysis(directory,laser,card,p,publish=publish_analysis,**arguments)
            elif card['operation'] in ('measure.change','measure.batch'):
                from ldcon.jobs.measurement_execution import run_card as run_measurement
                result=run_measurement(directory,laser,card,p,**arguments)
            elif card['operation']=='tune.pi_response':
                from ldcon.jobs.pi_response import run_card as run_pi_response
                def publish_pi_response(value):
                    sink.state['pi_response']=value
                    atomic_json(directory/'progress.json',{**sink.state,'updated_utc':utc_now()})
                result=run_pi_response(directory,laser,card,p,publish=publish_pi_response,**arguments)
                result={k:v for k,v in result.items() if k not in ('profile','pi_result')}
            elif card['operation'] in ('measure.target_response','optimize.settling_criteria','optimize.target_feedforward','optimize.target_thermal','optimize.target_step'):
                from ldcon.jobs.target_response import run_card as run_target_response
                def publish_response(value):
                    sink.state['target_response']=value
                    atomic_json(directory/'progress.json',{**sink.state,'updated_utc':utc_now()})
                result=run_target_response(directory,laser,card,p,publish=publish_response,**arguments)
                # Full arrays stay in target-response.json, not the progress/event store.
                from ldcon.jobs.target_response import summary as response_summary
                result=response_summary(result)
            elif card['operation']=='report.grafana_dashboard':
                from ldcon.web.grafana_dashboard import export_dashboard
                result=export_dashboard(directory,laser,p,deck=deck,
                    card_parameters=invocation.get('report_card_parameters'),
                    reference_source=invocation.get('reference_source','invocation'))
            elif card['operation']=='study.ecdl_mode_map':
                from ldcon.jobs.ecdl_mode_map import run_mode_map
                from ldcon.jobs.branch_maps import record as record_branch_map
                result=run_mode_map(ports,p,runtime=runtime,
                    context=dict(laser_id=laser['id'],job_id=directory.name,laser_configuration_sha256=digest(laser)),
                    publish=lambda artifact:record_branch_map(directory,laser,artifact),**arguments)
                result['branch_map']=record_branch_map(directory,laser,result['artifact'])
            elif card['operation']=='prepare.ecdl_recovery':
                # One relative-current-route algorithm: parameter proposal plus optional explicit qualification.
                from ldcon.jobs.recovery_decisions import run_card as prepare_recovery
                result=prepare_recovery(directory,laser,card,ports,p,runtime=runtime,**arguments)
                atomic_json(directory/'recovery.json',result['artifact'])
            elif card['operation']=='tune.ecdl_branch_hop':
                from ldcon.jobs.ecdl_branch_hop import run_branch_hop
                result=run_branch_hop(ports,p,publish=lambda value:atomic_json(directory/'branch-hop.json',value),**arguments)
                result={k:v for k,v in result.items() if k!='artifact'}
            elif card['operation']=='recover.ecdl_mode_hop':
                from ldcon.jobs.ecdl_mode_recovery import run_mode_recovery
                result=run_mode_recovery(ports,p,runtime=runtime,
                    **arguments)
                atomic_json(directory/'mode-recovery.json',result)
            elif card['operation']=='tune.ecdl_window_center':
                # rule F standalone: fast side-peak same-mode midpoint search without a lock (the steady lock runs the
                # same policy as its midpoint action when locked)
                from ldcon.jobs.ecdl_window_center import run_unlocked
                def publish_window(value):
                    sink.state['window_center']={k:v for k,v in value.items() if k not in ('path','points')}
                    atomic_json(directory/'progress.json',{**sink.state,'updated_utc':utc_now()})
                result=run_unlocked(ports,laser,p,publish=publish_window,**arguments)
                atomic_json(directory/'window-center.json',result)
            elif card['operation'] in ('tune.operating_point','tune.lock_target'):
                # Rule F generic cards: locked -> proxied into the running lock job (its own action); unlocked -> own session.
                def publish_point(value):
                    sink.state['operating_point' if card['operation']=='tune.operating_point' else 'lock_target']=value
                    atomic_json(directory/'progress.json',{**sink.state,'updated_utc':utc_now()})
                if card['operation']=='tune.operating_point':
                    from ldcon.jobs.operating_point import run_operating_point
                    result=run_operating_point(directory,laser,card,p,publish=publish_point,**arguments)
                    atomic_json(directory/'operating-point.json',result)
                else:
                    from ldcon.jobs.lock_target import run_lock_target
                    result=run_lock_target(directory,laser,card,p,publish=publish_point,**arguments)
                    atomic_json(directory/'lock-target-result.json',result)
            elif card['operation']=='laser.emission':
                from ldcon.jobs.emission_card import run_emission
                def publish_emission(value):
                    sink.state['emission']=value
                    sink.state['controller_state']=value['phase']
                    atomic_json(directory/'progress.json',{**sink.state,'updated_utc':utc_now()})
                result=run_emission(directory,laser,card,p,publish=publish_emission,**arguments)
                atomic_json(directory/'emission-result.json',result)
            elif card['operation']=='tune.device_parameters':
                # Rule I: one-shot device settings through the registry; the owners check leases/ranges/conflicts.
                def publish_parameters(value):
                    sink.state['device_parameters']=value
                    atomic_json(directory/'progress.json',{**sink.state,'updated_utc':utc_now()})
                from ldcon.jobs.device_parameters import run_device_parameters
                result=run_device_parameters(directory,laser,card,p,publish=publish_parameters,**arguments)
                atomic_json(directory/'device-parameters.json',result)
            elif card['operation']=='tune.ecdl_operating_point':
                from ldcon.jobs.ecdl_operating_point import run_operating_point
                result=run_operating_point(ports,p,runtime=runtime,**arguments)
            elif card['operation']=='study.ecdl_thermal_context':
                from ldcon.jobs.ecdl_thermal_context import run_context_study
                result=run_context_study(ports,p,**arguments)
            elif card['operation']=='study.ecdl_current_response':
                from ldcon.jobs.ecdl_response_study import run_response_study
                result=run_response_study(ports,p,**arguments)
            elif card['operation']=='commission.ecdl_pi':
                from ldcon.jobs.ecdl_pi_commission import run_pi_commission
                result=run_pi_commission(ports['lock'],p,**arguments)
            elif card['operation']=='servo.ecdl_steady_lock':
                from ldcon.jobs.ecdl_steady import run_steady_lock
                from ldcon.jobs.ecdl_online import ECDLOnlineBridge
                binding=laser['interfaces']['lock']
                from ldcon.jobs.lock_target import resolve_target
                # ONE target source: a null card target follows laser.target (and its live changes); the bridge
                # (window centre) and the card share this dict, which the card keeps at the owner's ramped value.
                follow_target=p.get('target_frequency_hz') is None and resolve_target(laser,{}) is not None
                p=dict(p,target_frequency_hz=resolve_target(laser,p)) if p.get('target_frequency_hz') is None else p
                optional_call(history,'parameters_resolved',p)
                study=ECDLOnlineBridge(directory,deck,card['id'],digest(laser['devices'][binding['device']]),emit,steady=p)
                from ldcon.jobs.ecdl_locked_sweep_proxy import rig_locked_route,attachment as sweep_attachment
                sweep=sweep_attachment(directory,laser,card_cancel=cancelled,emit=emit) if rig_locked_route(laser) else None
                from ldcon.jobs.ecdl_steady import notify_measurement_timeout
                notifier=lambda notice,outage,_card=card['id']:notify_measurement_timeout(directory,_card,notice,outage)
                result=run_steady_lock(ports['lock'],p,study=study,runtime_factory=steady_runtime,sweep=sweep,directory=directory,laser=laser,follow_laser_target=follow_target,measurement_notifier=notifier,**arguments)
            elif card['operation']=='monitor.wavelength':result=monitor_wavelength(ports['wavelength'],p,**arguments)
            elif card['operation']=='tune.ecdl_initial_point':
                from ldcon.owners.ecdl_prelock import acquire_ecdl
                result=acquire_ecdl(ports['observation'],ports['piezo'],ports['temperature'],
                                    ports['current'],ports['loop'],p,**arguments)
            elif card['operation'] in ('servo.current_pid','identify.current_response'):
                from ldcon.jobs.current_cards import servo_current,identify_current
                fn=servo_current if card['operation']=='servo.current_pid' else identify_current
                result=fn(ports['wavelength'],ports['current'],p,**arguments,**({'online':make_online} if card['operation']=='servo.current_pid' else {}))
            elif card['operation']=='tune.temperature_once':result=acquire_temperature(ports['wavelength'],ports['temperature'],p,**arguments)
            else:raise ValueError('No independent worker implementation for '+card['operation'])
            if 'artifact' in result:
                result={**{k:v for k,v in result.items() if k!='artifact'},
                        'artifact_sha256':result['artifact']['artifact_sha256']}
            if card['operation']=='tune.ecdl_operating_point' and 'trace' in result:
                atomic_json(directory/'operating-point-trace.json',result['trace'])
                result={**{k:v for k,v in result.items() if k!='trace'},
                        'trace_file':'operating-point-trace.json'}
            result=normalize_card_result(result)
            status=result['status'];message=result.get('message');emit('card_end',{'card':card['id'],'result':result})
        except (CardCancelled,CancelledRun) as error:
            status='cancelled';message=str(error) or 'Stopped; no further output writes'
            store.record(run_id,time.monotonic()-started,'card_cancelled',dict(card=card['id'],message=message))
        except Exception as error:
            message=f'{type(error).__name__}: {error}'
            atomic_json(directory/'failure-detail.json',dict(error_type=type(error).__name__,
                message=str(error),traceback=traceback.format_exc(limit=20)[-12000:]))
            store.record(run_id,time.monotonic()-started,'card_failed',dict(card=card['id'],message=message))
            if hasattr(error,'ecdl_review'):atomic_json(directory/'device-review.json',error.ecdl_review)
        except BaseException:
            execution.interrupted=execution.may_have_effects
            raise
        finally:
            if runtime is not None:
                try:runtime.close()
                except Exception as error:
                    status='failed';message=(message or '')+'; runtime state close failed: '+str(error)
            for name,port in ports.items():
                if getattr(port,'uncertain_output',False):
                    if hasattr(port,'review_details'):
                        atomic_json(directory/'device-review.json',{'interface':name,**port.review_details()})
                    else:
                        t=port.controller.transport
                        atomic_json(directory/'device-review.json',{'interface':name,'endpoint':t.endpoint,
                            'instrument':t.instrument,'lease':t.lease,'message':'Uncertain write; lease retained. Operator review required.'})
                    message=(message or '')+'; uncertain output, instrument lease retained for review'
                try:
                    if hasattr(port,'close'):port.close()
                except Exception as error:
                    status='failed';message=(message or '')+'; close failed: '+str(error)
                    review=read_json(directory/'device-review.json',{})
                    review['lease_unknown']=True
                    review.setdefault('cleanup_errors',[]).append(dict(interface=name,
                        error_type=type(error).__name__,message=str(error)))
                    atomic_json(directory/'device-review.json',review)
            elapsed=time.monotonic()-started
            summary={**result,'status':status,'message':message,'deck_id':deck['id'],'laser_id':laser['id'],
                     'card_id':card['id'],'elapsed_s':elapsed,'time_basis':'wall'}
            if history is not None:summary['operation_record']='operation-record.json'
            summary=execution.finish(summary,review=read_json(directory/'device-review.json'))
            status=summary['status']
            if card['operation']!='report.grafana_dashboard':
                store.record(run_id,elapsed,'controller_status',{'controller_state_code':5 if status=='failed' else 0})
            store.finish(run_id,status)
            atomic_json(directory/'summary.json',summary)
            atomic_json(directory/'progress.json',{**sink.state,'status':status,'simulated_s':elapsed,
                'message':message,'updated_utc':utc_now(),'active_card':None,'operation_outcome':summary['operation_outcome']})
            atomic_json(directory/'readings.json',sink.readings)
            optional_call(history,'finish',summary)
            if status in ('failed','interrupted','completed_with_failures'):
                meta=read_json(directory/'job.json',{})
                if meta.get('failure_policy_version')==1:
                    try:
                        from ldcon.agents.failure_policy import ensure_case
                        root=directory.parent.parent if directory.parent.name=='jobs' else directory.parent.parent.parent
                        ensure_case(root,meta,card['id'],'main',dict(status=status,message=message,
                            source='terminal',outputs_uncertain=(directory/'device-review.json').exists()),directory=directory)
                    except Exception as error:
                        # Failure handling never masks the original control outcome.
                        atomic_json(directory/'failure-dispatch-error.json',dict(error_type=type(error).__name__,
                            message='Failure handling is pending; inspect the original job and notification service'))
    return summary


def main():
    import sys
    from ldcon.jobs.job_service import worker_lifetime
    with worker_lifetime(Path(sys.argv[1])):
        run(Path(sys.argv[1]))


if __name__=='__main__':main()
