"""Device-neutral stationary ECDL card; the owner owns fast feedback and limits.

No addresses, volts, DAC calibration, fringe thresholds or PI gains belong here.
The typed lock interface must provide fail-safe lease/watchdog lifecycle support.
"""
import math
import sys
from pathlib import Path
from ldcon.core.contracts import DeviceError, MeasurementUnavailable, OwnerUnavailable
from ldcon.core.instrument_client import BrokerError
from ldcon.core.interface_cards import CardCancelled

# Owner marker of a one-sample native sequence-regression refusal (ecdl_steady_owner.SEQUENCE_REGRESSED).
SEQUENCE_REGRESSED='Native input sequence regressed'

# owner_outage_timeout_s (design §5, 2026-10-27): how long the running lock tolerates an UNREACHABLE rig owner
# (restart, crash), retrying heartbeat/read with the same lease. The firmware PZT PI keeps running meanwhile (rule B)
# and the owner adopts the lease when it returns. Distinct from the measurement outage (measurement_timeout_s,
# notify-only). Same default (60 s) and ceiling (3600 s) as servo.native_current.
# Recovery uses one history-referenced relative-current-route algorithm. Archived
# map/class artifacts are readable evidence, never active recovery policy.
DEFAULTS=dict(target_frequency_hz=None,tolerance_hz=None,duration_s=None,
              sample_interval_s=1.,measurement_timeout_s=120.,strategy='hold',recovery=None,
              acquisition_tolerance_hz=None,tec_gain=None,tec_ff_paused=None,current_ff_enabled=None,
              window_guard=None,owner_outage_timeout_s=60.)


RETIRED_RECOVERY_PARAMETERS = frozenset({
    'recovery_artifact', 'recovery_ref', 'recovery_plan_artifact', 'recovery_plan_ref',
    'recovery_test_artifact', 'recovery_test_ref', 'mapped_recovery_max_attempts', 'rearm_trim'})


def reject_retired(value):
    retired = RETIRED_RECOVERY_PARAMETERS.intersection(value)
    if retired:
        raise ValueError('Legacy recovery parameters retired: '+', '.join(sorted(retired))+
                         '; migrate to explicit recovery relative_current_route.v1 settings')


def parameters(card,overrides):
    if not isinstance(card['parameters'],dict) or not isinstance(overrides,dict):
        raise ValueError('Parameters must be mappings')
    reject_retired(card['parameters'])
    reject_retired(overrides)
    if (set(card['parameters'])|set(overrides))-DEFAULTS.keys():raise ValueError('Unknown steady-lock parameter')
    p={**DEFAULTS,**card['parameters'],**overrides}
    for key,value in p.items():
        if key=='window_guard':
            from ldcon.jobs.window_actions import normalize_guard
            p[key]=normalize_guard(value)
        elif key=='recovery':
            from ldcon.jobs.ecdl_mode_recovery import parameters as recovery_parameters
            p[key]=None if value is None else recovery_parameters({'parameters':value},{})
        elif key in ('tec_ff_paused','current_ff_enabled'):
            if value is not None and type(value) is not bool:raise ValueError(key+' must be boolean or null')
        elif key=='strategy':
            if value!='hold':raise ValueError('Hidden steady-lock window traversal is retired; use tune.ecdl_window_center or agent.monitor with an adjustment task')
        elif value is None and key in ('target_frequency_hz','tolerance_hz','duration_s','acquisition_tolerance_hz','tec_gain'):continue
        elif type(value) not in (int,float) or not math.isfinite(value) or value<=0:
            raise ValueError(key+' must be finite and positive')
    if not .1<=p['sample_interval_s']<=5:raise ValueError('Steady poll interval must be 0.1..5 seconds')
    if p['owner_outage_timeout_s']>3600:raise ValueError('Owner outage tolerance must be at most one hour')
    return p


def clearly_degraded(row,laser):
    """Report the same optical quality limits used by classification and monitoring.

    This quality annotation does not itself stop native feedback.
    """
    if row.get('maintenance_required'):return True
    try:
        from ldcon.jobs.operating_point import lock_device
        limits=((lock_device(laser)[2].get('control_settings') or {}).get('optical') or {}).get('single_mode_max') or {}
    except Exception:
        limits={}
    metrics=row.get('metrics') or row.get('fringe_metrics') or {}
    return any(type(metrics.get(k)) in (int,float) and metrics[k]>v for k,v in limits.items())


def notify_measurement_timeout(directory,card_id,payload,outage):
    """One NON-BLOCKING measurement-outage notice (v3.1-hostcheck): resident inbox AND deck Teams card, no failure
    case, nothing that holds reservations (measurement_notice.notify_non_blocking). Never raises."""
    from ldcon.jobs.measurement_notice import notify_non_blocking
    return notify_non_blocking(directory,card_id,'measurement_timeout','outage-%d'%outage,payload)


def run_steady_lock(lock,p,*,clock,sleep,cancel,emit,study=None,runtime_factory=None,sweep=None,
                    directory=None,laser=None,follow_laser_target=False,measurement_notifier=None):
    """Revalidate the initial point, arm native feedback, supervise, then hold.

    Missing optical reads never authorize slow actuator changes. The owner keeps
    a native lease watchdog even if this worker exits without running finally.
    Automatic original-mode recovery is opt-in with explicit bounded recipes.
    Otherwise a persistent loss holds and ends.

    Rule F actions (2026-10-22): operating-point requests filed by ``tune.operating_point`` run here under this
    lease at a cycle boundary; a live change of the laser's ONE target (``tune.lock_target``) reaches the owner
    through the laser reload hook (``LaserLive`` -> ``lock.set_target``) and this card follows the owner's bounded
    ramp (``row['ramped_target_hz']``) in its own copies (recovery/window-centre share ``p``).
    """
    reject_retired(p)
    if p.get('recovery') is not None:
        from ldcon.jobs.ecdl_mode_recovery import validate as validate_recovery
        validate_recovery(p['recovery'])
    if cancel():raise CardCancelled('Stopped before ECDL lock acquisition')
    if p['target_frequency_hz'] is None or p['tolerance_hz'] is None:
        raise DeviceError('Target frequency and tolerance are required (card parameter or laser.target)')
    if hasattr(lock, 'prepare_runtime'):
        emit('runtime_capabilities', lock.prepare_runtime(p))
    lock.check_ready(p['strategy'])
    armed=False;started=clock();waiting=None;confirming=None;cycles=0;recovery=None
    timeout_notified=False;outages=0   # v5.1-hostcheck: one measurement_timeout notice per outage
    owner_unreachable_since=None       # bounded owner-restart tolerance (design §5, 2026-10-27)
    point_queue=None;adaptive=None
    def owner_wait(error):
        """The rig owner did not answer (restart/crash): keep the lease, write nothing, retry for a bounded time.
        Returns True while waiting; raises DeviceError at the limit."""
        nonlocal owner_unreachable_since
        if owner_unreachable_since is None:
            owner_unreachable_since=clock()
            emit('owner_wait',dict(reason=str(error),native_pi_active=armed,timeout_s=p['owner_outage_timeout_s']))
        elif clock()-owner_unreachable_since>=p['owner_outage_timeout_s']:
            raise DeviceError('ECDL rig owner unreachable for %.0f s (owner_outage_timeout_s %.0f): %s; no write '
                              'attempted, firmware PZT PI state unknown to this worker'
                              %(clock()-owner_unreachable_since,p['owner_outage_timeout_s'],error))
        return True
    def owner_recovered():
        nonlocal owner_unreachable_since
        if owner_unreachable_since is not None:
            # The owner answered with the same lease (it adopted the running lock after its restart).
            emit('owner_recovered',dict(duration_s=clock()-owner_unreachable_since,native_pi_active=armed))
            owner_unreachable_since=None
    if directory is not None and laser is not None:
        from ldcon.jobs.operating_point import OperatingPointQueue
        point_queue=OperatingPointQueue(directory)
        from ldcon.jobs.adaptive_window_session import AdaptiveExperimentManager
        adaptive=AdaptiveExperimentManager(directory,lock,laser_id=laser['id'],emit=emit,clock=clock,
                                           poll_interval_s=p['sample_interval_s'])
    def auxiliary_busy():
        return bool((adaptive and adaptive.active) or (study and (
            study.freeze_recovery_reference or study.recovery_requested
            or getattr(study,'window',None) and study.window.status=='running'
            or getattr(study,'tuner',None) and study.tuner.active)))
    from ldcon.jobs.recovery_history import Recorder
    recovery_history = Recorder(laser['id'], emit, p.get('recovery')) if laser else None
    def publish_target(row):
        if directory is None:return
        from ldcon.core.records import atomic_json
        import time as _time
        atomic_json(Path(directory)/'lock-target.json',dict(target_frequency_hz=p['target_frequency_hz'],
            ramped_target_hz=row.get('ramped_target_hz',p['target_frequency_hz']),ramp_active=bool(row.get('target_ramp_active')),
            frequency_hz=row.get('frequency_hz'),error_hz=row.get('error_hz'),single_mode=row.get('single_mode'),
            mode_id=row.get('mode_id'),degraded=clearly_degraded(row,laser),
            tec_centering=row.get('tec_centering'),
            # target-ff design (2026-10-27): report-only PZT room and the LDC term of the owner's ramp
            pzt_headroom_hz=row.get('pzt_headroom_hz'),
            ldc_feedforward={k:(row.get('target_feedforward') or {}).get(k) for k in ('ldc','ldc_applied_v','ldc_share_hz','ldc_writes','ldc_skipped')}
                            if row.get('target_feedforward') else None,
            state=row.get('state'),updated_unix_s=_time.time()))
    try:
        if p['recovery']:
            from ldcon.jobs.recovery_stationary import StationaryRecovery
            recovery=StationaryRecovery(lock,p,clock=clock,sleep=sleep,cancel=cancel,emit=emit,recorder=recovery_history)
            recovery.prime()
        # Mark before arm: an unknown arm result also requires owner review/hold.
        armed=True
        lock.arm(p['target_frequency_hz'],p['acquisition_tolerance_hz'] or p['tolerance_hz'],p['strategy'])
        if recovery:recovery.armed=True
        runtime=runtime_factory(p,lock) if runtime_factory else None
        if runtime is not None and getattr(runtime,'live',None) is not None:
            runtime.live.follow_laser_target=bool(follow_laser_target)
        emit('state',dict(state='locked',strategy=p['strategy']))
        reported_state='locked'
        while p['duration_s'] is None or clock()-started<p['duration_s']:
            if cancel():raise CardCancelled('ECDL stationary lock stopped')
            try:lock.heartbeat()
            except OwnerUnavailable as error:
                # Owner process unreachable (restart/crash): bounded wait with the same lease, no write. Only an
                # owner that answers with a fault or without this lease (BrokerError) ends the card.
                owner_wait(error)
                sleep(p['sample_interval_s'])
                continue
            owner_recovered()
            if adaptive:adaptive.check_deadlines()
            foreground_ready=adaptive.foreground() if adaptive else True
            if point_queue is not None and foreground_ready:
                if adaptive:adaptive.foreground_dispatch('point')
                # Operating-point request (tune.operating_point proxy): the SAME procedure as the standalone card,
                # run as this lock's own action under its lease at this cycle boundary; one request per cycle.
                from ldcon.jobs.operating_point import lock_action,ECDLLockedAdapter
                point_busy=auxiliary_busy()
                outcome=lock_action(point_queue,lambda:ECDLLockedAdapter(lock,laser,directory=directory,heartbeat=lock.heartbeat),laser,
                    clock=clock,sleep=sleep,cancel=cancel,emit=emit,lock_target_hz=p['target_frequency_hz'],busy=point_busy)
                if outcome is not None:
                    if recovery:recovery.invalidate('foreground operating-point action')
                    elif recovery_history:recovery_history.invalidate()
                    emit('operating_point_action',outcome);waiting=None;confirming=None
                    continue
            if sweep and foreground_ready:
                if adaptive:adaptive.foreground_dispatch('sweep')
                # Locked staircase requested by a proxy job: runs to completion here under this lease (the owner
                # keeps its own sampler and watchdog; the session heartbeats). One request per cycle boundary.
                outcome=sweep(lock,online_busy=auxiliary_busy())
                if outcome is not None:
                    if recovery:recovery.invalidate('foreground sweep completed')
                    elif recovery_history:recovery_history.invalidate()
                    emit('locked_sweep_result',outcome);waiting=None;confirming=None
                    continue
            if runtime and foreground_ready:
                if adaptive:adaptive.foreground_dispatch('runtime')
                runtime.poll(boundary='steady_cycle',busy=auxiliary_busy())
            if study and foreground_ready and not (adaptive and adaptive.active):
                if adaptive:adaptive.foreground_dispatch('study')
                study.poll(lock)
            hop=recovery.probe(update_reference=not (getattr(study,'freeze_recovery_reference',False)
                or adaptive and adaptive.active),intentional_operation=recovery.target_ramping) if recovery else False
            if recovery and (hop or getattr(study,'recovery_requested',False)):
                recovery_ticket=adaptive.before_recovery() if adaptive else None
                if study and hasattr(study,'before_recovery'):study.before_recovery()
                recovery.recover('confirmed original-mode frequency/identity loss' if hop else 'window requested original-mode requalification')
                if adaptive:adaptive.after_recovery(recovery_ticket)
                if study and hasattr(study,'after_recovery'):study.after_recovery()
                waiting=None;confirming=None;reported_state='locked'
                continue
            try:row=lock.read_cycle()
            except MeasurementUnavailable as error:
                if isinstance(error,OwnerUnavailable):
                    # Not a measurement outage: the owner went away between the heartbeat and the read.
                    owner_wait(error)
                elif SEQUENCE_REGRESSED in str(error):
                    # v3.1-hostcheck: the owner refused ONE sample because the native sequence went backwards (it
                    # re-baselines). Reported only; never starts or advances the measurement-outage timer.
                    emit('native_sequence_regressed',dict(reason=str(error)[:300]))
                else:
                    if waiting is None:
                        waiting=clock();emit('state',dict(state='waiting_for_measurement'))
                        reported_state='waiting_for_measurement'
                    emit('measurement_rejected',dict(stage='stationary lock',reason=str(error)))
                    if study and hasattr(study,'invalidate'):study.invalidate('Optical measurement dropout')
                    if clock()-waiting>=p['measurement_timeout_s'] and not timeout_notified:
                        # v5.1-hostcheck (human decision 2026-10-22): the firmware consumes the wavemeter data itself, so
                        # the host neither stops the job nor holds the PI. One NON-BLOCKING notice per outage to the
                        # resident inbox and the deck Teams card; keep waiting/locking; re-armed after a locked read.
                        timeout_notified=True;outages+=1
                        notice=dict(code='MEASUREMENT_TIMEOUT',source='measurement_timeout',job_state='running',
                            message=('Stationary lock: no accepted optical measurement for %.0f s (last: %s). The job keeps '
                                     'running and the native PI stays enabled (not held). Inspect the wavemeter/owner input; '
                                     'do not stop the healthy worker unless a human decides so.')%(clock()-waiting,str(error)[:300]),
                            waiting_s=clock()-waiting,measurement_timeout_s=p['measurement_timeout_s'],outage=outages,
                            last_rejection=str(error)[:500])
                        delivery=None
                        if measurement_notifier is not None:
                            try:delivery=measurement_notifier(notice,outages)
                            except Exception as notify_error:
                                delivery=dict(routing_status='error',error='%s: %s'%(type(notify_error).__name__,notify_error))
                        emit('measurement_timeout_notified',dict(notice,delivery=delivery))
            else:
                if row['state']!='locked':
                    if recovery and row.get('reason')=='frequency outside native capture band':
                        emit('state',dict(state='confirming_mode_loss'))
                        reported_state='confirming_mode_loss'
                        # v5.1-hostcheck: the capture-loss confirmation has its own window (a preceding measurement
                        # outage no longer ends the job, so it must not pre-expire this K check).
                        if confirming is None:confirming=clock()
                        if waiting is None:waiting=clock()
                        if clock()-confirming>=p['measurement_timeout_s']:
                            raise DeviceError('Unconfirmed capture loss timed out; holding native feedback')
                        sleep(p['sample_interval_s'])
                        continue
                    raise DeviceError('Native ECDL lock lost: '+row.get('reason',row['state']))
                # A trusted experiment selector is opt-in. The existing worker
                # retains the lease, heartbeat and hop-recovery authority.
                adaptive_changed=False
                if adaptive and not getattr(recovery,'hop_count',0):
                    before_adaptive=adaptive.effect_revision
                    child_busy=bool((study and (study.freeze_recovery_reference or study.recovery_requested
                        or getattr(study,'window',None) and study.window.status=='running'
                        or getattr(study,'tuner',None) and study.tuner.active)) or row.get('target_ramp_active'))
                    adaptive.poll(busy=child_busy,row={**row,'tolerance_hz':p['tolerance_hz']})
                    adaptive_changed=adaptive.effect_revision!=before_adaptive
                # ONE target: an owner-side target change (laser.target via tune.lock_target) is reported per cycle.
                # The card's copy (shared with the window-centre bridge) and the recovery follow it; during the
                # owner's bounded ramp the hop test uses the ramped value so a legitimate ramp is never a "hop".
                final_target=row.get('target_frequency_hz');ramped=row.get('ramped_target_hz',final_target)
                if final_target is not None and abs(final_target-p['target_frequency_hz'])>1e-6:
                    emit('lock_target_changed',dict(previous_hz=p['target_frequency_hz'],target_frequency_hz=final_target))
                    p['target_frequency_hz']=final_target
                if recovery is not None:
                    recovery.set_target(p['target_frequency_hz'])
                    recovery.target_ramping=bool(row.get('target_ramp_active'))
                publish_target(row)
                optical_good=row.get('single_mode') is not False and not row.get('maintenance_required',False)
                display_state='mode_degraded' if not optical_good else 'tracking' if abs(row.get('error_hz',0))>p['tolerance_hz'] else 'locked'
                if waiting is not None or reported_state!=display_state:
                    emit('state',dict(state=display_state,measurement_resumed=waiting is not None))
                reported_state=display_state
                if timeout_notified:emit('measurement_timeout_recovered',dict(outage=outages))
                timeout_notified=False
                waiting=None;confirming=None;cycles+=1;emit('ecdl_lock_cycle',row)
                if recovery_history:recovery_history.observe(row,p['target_frequency_hz'],intentional=auxiliary_busy())
                if row.get('frequency_hz'):
                    emit('trace',dict(frequency_hz=row['frequency_hz'],
                        wavelength_nm=299792458e9/row['frequency_hz'],error_hz=row.get('error_hz'),
                        acquired_s=row.get('acquired_s'),
                        source_acquired_unix_s=row.get('source_acquired_unix_s'),
                        native_interval_last_s=row.get('native_interval_last_s'),
                        outputs=row.get('outputs',{}),fringe_metrics=row.get('metrics',{}),
                        single_mode=row.get('single_mode'),locked=display_state=='locked',
                        **{k:v for k,v in row.items() if k.startswith('dither_') or k.startswith('tec_center_')}))
                if study and not (adaptive and adaptive.active) and row.get('study_point'):study.point(row['study_point'])
                if study and not (adaptive and adaptive.active) and hasattr(study,'cycle'):study.cycle(row,p['tolerance_hz'])
                # Planner operates inside the one owner; no raw device command
                # or device calibration is supplied by this reusable card.
                suppress_actions=adaptive_changed or bool(adaptive and adaptive.active and not adaptive.allows_owner_slow_actions())
                for action in ([] if suppress_actions else row.get('actions',[])):
                    if cancel():raise CardCancelled('ECDL stationary lock stopped')
                    result=lock.adjust(action)
                    emit('command_applied' if result.get('applied',True) else 'command_deferred',
                         dict(interface=action['axis'],proposal_reason=action['reason'],**result))
            remaining=p['sample_interval_s']
            if p['duration_s'] is not None:remaining=min(remaining,max(0.,p['duration_s']-(clock()-started)))
            if remaining:sleep(remaining)
        return dict(status='succeeded',cycles=cycles,elapsed_s=clock()-started,
                    lock_enabled=False,exit_behavior='owner-verified DAC hold')
    finally:
        try:
            try:
                if adaptive:adaptive.close('parent_stopped')
            finally:
                if study and hasattr(study,'close'):study.close()
        finally:
            if armed:
                try:held=lock.hold('card exit')
                except (OSError,BrokerError) as error:
                    # The owner is unreachable at exit (restart/crash) or refuses the hold (fault/ownership). The
                    # lease stays retained for review and the owner's own heartbeat deadline holds the lock; keep the
                    # PRIMARY failure visible instead of masking it with the exit-hold error (a normal end still
                    # fails on it).
                    emit('ecdl_lock_hold_failed',dict(reason=str(error),code=getattr(error,'code',None),
                                                      unreachable=isinstance(error,OSError)))
                    if sys.exc_info()[1] is None:raise
                else:emit('ecdl_lock_held',held)
