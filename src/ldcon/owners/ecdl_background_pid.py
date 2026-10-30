"""Owner adapter for preemptible, runtime-only native PI trials."""
import copy
import math

from ldcon.core.contracts import DeviceError
from ldcon.owners.ecdl_native_pid import NativePIDTuning
from ldcon.jobs.background_pid_session import background_parameters as parameters


def begin(native, request, context, identity, interval):
    p=parameters({'parameters':{}},request['parameters'],require_candidate=True)
    if 'measurement_job_id' in identity:
        from ldcon.owners.background_grants import read_grant
        bounds=read_grant(native.o.laser['id'],identity,lease=native.o.lease)
        approved=parameters({'parameters':{}},bounds.get('pid_parameters',{}))
        if ('pi' not in bounds.get('families',[]) or {k:v for k,v in p.items() if k!='candidate'}!=approved
                or request['handoff_timeout_s']!=bounds['handoff_timeout_s']):
            raise ValueError('Measurement PI parameters differ from the frozen plan')
    if p['strategy'] not in ('factor_reduction','validated_candidates'):
        raise ValueError('Unsupported background native PI strategy')
    if p['save_settings']:
        raise ValueError('Background PI requires save_settings=false')
    # Legacy gain reduction keeps its ramp; independent PI uses an atomic receipt.
    period=math.ceil((.1+1e-9)/interval)*interval
    if p['strategy']=='validated_candidates':
        from ldcon.owners.ecdl_independent_pid import IndependentPIDTuning
        trial=IndependentPIDTuning(native,p,period=period,handoff_timeout_s=request['handoff_timeout_s'])
        steps=None;return_bound=None
    else:
        trial=NativePIDTuning(native,p)
        fraction=1-min(p['gain_factors'])
        steps=sum(math.ceil(fraction*abs(trial.original[key])*bound/trial.step)
                  for key,bound in (('pid_kp',trial.capture_nm),('pid_ki',trial.raw_bound)))
        return_bound=(steps+1)*period+trial.settle
    if return_bound is not None and return_bound>request['handoff_timeout_s']:
        trial.closed=True
        raise ValueError('PI return needs %.6g s at the admitted cadence; handoff budget is %.6g s'%
                         (return_bound,request['handoff_timeout_s']))
    checkpoint={key:trial.original[key] for key in ('pid_kp','pid_ki','pid_kd','pid_integral_min','pid_integral_max')}
    state,obs=native.read_feedback()
    if native.background_context(obs)!=context:raise ValueError('PI admission context changed')
    checkpoint.update(current=state['outputs']['current'],temperature=state['outputs']['temperature'])
    epoch=native.background.begin(request['expected_generation'],context,checkpoint,
                                 None if p['strategy']=='validated_candidates' else request['handoff_timeout_s'],identity,
                                 completion_mode='verified_handover' if p['strategy']=='validated_candidates' else 'deadline')
    native.background.epoch.update(family='pi',strategy=p['strategy'],trial_id=trial.id,poll_interval_s=interval,
        maximum_return_steps=steps,return_bound_s=return_bound,runtime_only=True)
    if 'measurement_job_id' in identity:
        native.background.epoch['expires_at']=native.o.clock()+p['timeout_s']
    native.background.epoch['transition_period_bound_s']=period
    native.pid_tuning=trial
    native.o.audit('background_pi_checkpoint',**native.background.epoch)
    return dict(started=True,epoch=native.background.status()['epoch'],pid_tuning=trial.status(),runtime_only=True)


def validate_axes(native,epoch,state):
    for axis in ('current','temperature'):
        tolerance=max(native.o.policies()[axis].resolution/2,1e-12)
        if abs(state['outputs'][axis]-epoch['checkpoint'][axis])>tolerance:
            native.background.block(axis+' changed during background PI tuning')
            return False
    trial=native.pid_tuning
    if trial is None or trial.id!=epoch['trial_id']:
        native.background.block('Native PI trial identity changed')
        return False
    return True


def request_return(native):
    epoch=native.background.epoch
    trial=native.pid_tuning
    if epoch and epoch.get('family')=='pi' and trial and not trial.closed and not trial.at_checkpoint():
        # Atomic adapter restores coefficients against CURRENT output; legacy
        # adapters still schedule their owner-driven ramp.
        trial.target(copy.deepcopy(trial.original_pid))


def dispatch(native,request,context,state):
    bg=native.background;action=request['action'];token=request['token']
    restoring=action=='background_restore'
    epoch=bg.validate(token,context,restoring=restoring)
    if epoch.get('family')!='pi':raise ValueError('This epoch is not a native PI trial')
    if not validate_axes(native,epoch,state):
        if restoring:return bg.status()
        raise ValueError('Background PI checkpoint context requires reconciliation; no gain effect was applied')
    if not restoring and 'measurement_job_id' in epoch['identity']:
        from ldcon.owners.background_grants import read_grant
        read_grant(native.o.laser['id'],epoch['identity'],lease=native.o.lease)
    trial=native.pid_tuning
    if action=='background_pid_target':
        return dict(bg.status(),pid_tuning=trial.target(request['pid']))
    if restoring:
        if not trial.at_checkpoint():request_return(native)
        if getattr(trial,'completion_mode',None)=='verified_handover':
            state=native.o.hw().snapshot()
            if not validate_axes(native,epoch,state):return bg.status()
        if not trial.ready():return dict(bg.status(),pid_tuning=trial.status())
        status='cancelled'
    elif action=='background_pid_finish':
        status=request['status']
        if status not in ('succeeded','unchanged','failed','cancelled'):
            raise ValueError('Invalid background PI completion')
        if not trial.ready():raise ValueError('Native PI transition has not completed')
        if status!='succeeded' and not trial.at_checkpoint():
            raise ValueError('Unaccepted PI trial must restore original coefficients')
    else:raise ValueError('Unknown native PI background operation')
    # Prove actual coefficients, not just the ramp target or a cached status.
    for key in ('pid_kp','pid_ki','pid_kd','pid_integral_min','pid_integral_max'):
        expected=trial.expected[key] if status=='succeeded' else epoch['checkpoint'][key]
        if not math.isclose(state['servo_config'][key],expected,rel_tol=1e-8,abs_tol=1e-12):
            raise DeviceError('Native PI completion readback mismatch: '+key)
    trial.finish(status)
    receipt=dict(confirmed=True,response_ended=True,native_pi_active=native.active,
                 modified_settings=['pid_kp','pid_ki']+(['pid_integral_min','pid_integral_max','control_offset'] if epoch.get('strategy')=='validated_candidates' else []),raw_integral_bounds_unchanged=epoch.get('strategy')!='validated_candidates',
                 actual_native=copy.deepcopy(trial.expected),runtime_only=True,
                 optical_settle_required_for_handoff=False,
                 handover=copy.deepcopy(getattr(trial,'last_receipt',None)),
                 restart_behavior='Configured gains are restored on a new arm')
    result=bg.complete(token,context,receipt,adopted=status=='succeeded')
    bg.release([r for r in bg.reservations if r.startswith('direct:')])
    native.o.audit('background_pi_completed',**result['last'])
    return dict(bg.status(),pid_tuning=trial.status(),runtime_only=True)


def restore_atomic(native, reason):
    """Restore a PI checkpoint without asking the optical pipeline to settle.

    A confirmed prewrite refusal becomes an inspectable blocked handoff, not an
    endless optical wait or repeated write. Unknown effects retain owner fault.
    """
    from experiment_core.errors import OperationError
    bg=native.background
    epoch=bg.epoch
    if not epoch or epoch.get('completion_mode')!='verified_handover':
        raise ValueError('No atomic PI epoch')
    if native.o.fault or not native.active or native.o.recovery is not None:
        return bg.block('Owner fault, inactive PI or recovery prevents handover')
    # The mode label is historical context only here; no optical evidence is
    # invented. All live authority/lease/target fields must still match.
    context=native.background_context({'mode_id':epoch['context'].get('mode_id')})
    bg.returning(reason)
    try:
        bg.validate(epoch['token'],context,restoring=True)
        state=native.o.hw().snapshot()
        if not validate_axes(native,epoch,state):return bg.status()
        native.pid_tuning.advance(state,{})
        if state['servo_config'].get('pid_multiplier') != 1:
            return bg.block('Native PI is no longer active')
        request_return(native)
        return dispatch(native,dict(action='background_restore',token=epoch['token']),context,state)
    except (OperationError,ValueError,DeviceError,OSError) as error:
        return bg.block('PI handover requires inspection: '+str(error))


def poll_measurement_grant(native):
    """Release an abandoned finite PI measurement through the existing handover.

    This independent owner path needs no model/worker, optical settling, or new
    gain proposal. A faulted/unknown write remains quarantined by restore_atomic.
    """
    epoch=native.background.epoch
    if (not epoch or epoch.get('family')!='pi' or epoch.get('state')!='active'
            or 'measurement_job_id' not in epoch.get('identity',{})):
        return
    reason=None
    try:
        from ldcon.owners.background_grants import read_grant
        read_grant(native.o.laser['id'],epoch['identity'],lease=native.o.lease)
    except (ValueError,OSError,KeyError,TypeError):
        reason='measurement_grant_revoked'
    if native.o.clock()>=epoch.get('expires_at',float('inf')):
        reason='measurement_timeout'
    if reason:
        return restore_atomic(native,reason)


def capabilities(native):
    from ldcon.core.adjustment_registry import capabilities as describe
    result=describe('servo.ecdl_steady_lock',native.o.device)
    cfg=getattr(native,'expected_servo',None) or {}
    result['pi'].update(atomic_handover_available=cfg.get('pi_handover_capability')==1,
        firmware_capability_source='last_owner_config_readback',
        optical_settle_required_for_handoff=False)
    return result
