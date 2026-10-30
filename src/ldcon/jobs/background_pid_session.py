"""Finite existing OnlinePIDTuner composition inside a background owner epoch.

The model chooses an admitted strategy or a typed candidate within owner bounds. Native gain transitions
and restoration stay in the owner, independent of model/provider availability.
"""
import copy
import math
import statistics
from pathlib import Path
import time

from experiment_core.records import Journal,atomic_json,digest,safe_id
from ldcon.core.contracts import DeviceError,MeasurementUnavailable
from ldcon.core.instrument_client import BrokerError
from ldcon.jobs.online_pid import OnlinePIDTuner,parameters
from ldcon.jobs.adaptive_window_session import bounded_json,finite
from ldcon.jobs.adjustment_continuation import AdjustmentContinuation,RECOVERABLE
from ldcon.jobs.measurement_statistics import numeric_summary


def background_parameters(card, overrides, *, require_candidate=False):
    """Shared background schema; candidate values never widen physical policy."""
    source={**card['parameters'],**overrides}
    if source.get('strategy')!='validated_candidates':
        return parameters(card,overrides)
    proposed=source.pop('candidate',None)
    source['strategy']='factor_reduction'
    result=parameters({'parameters':{}},source)
    if result['save_settings']:
        raise ValueError('Validated background candidates must be runtime-only')
    result['strategy']='validated_candidates'
    if proposed is not None:
        from ldcon.owners.flexible_native_trial import candidate
        result['candidate']=candidate(proposed)
    elif require_candidate:
        raise ValueError('A typed native PI candidate is required at admission')
    return result


class _SessionTuner(OnlinePIDTuner):
    def _metrics(self,rows,actuator):
        result=super()._metrics(rows,actuator)
        measured=numeric_summary(r['error'] for _,r in rows)
        result.update(rms=measured['rms'],mean=measured['mean'],statistics={'frequency_error_hz':measured})
        s=actuator.session
        atomic_json(s.directory/('segment-%03d-%s-%03d-measurements.json'%(s.segment,self.phase,self.trial_index)),
                    dict(summary=result,rows=rows,pid=actuator.pid))
        return result

    def _complete(self,status,pid,reason):
        pid.session.comparison_reason=reason
        return super()._complete(status,pid,reason)


class _CandidateTuner(_SessionTuner):
    """One explicit, owner-proved candidate using existing finite comparison."""
    def tick(self,now,row,pid,actuator):
        settling=False
        if self.active and self.phase=='trial' and not self.finishing and not self.finalizing:
            ready=self._ready(pid)
            if not ready:
                self.candidate_ready_at=None
                self.candidate_measuring=False
            elif self.candidate_ready_at is None:
                # This tick is a fresh accepted acquisition, after every owner
                # configuration step. Transition time is not settling time.
                self.candidate_ready_at=now+self.p['settle_s']
            settling=not ready or now<self.candidate_ready_at
            if not self.candidate_measuring:
                self.rows=[]
                self.window_started=now
                if not settling:self.candidate_measuring=True
        # Keep the shared cancellation, deadline, eligibility and asynchronous
        # restoration checks active even while discarding settling samples.
        super().tick(now,row,pid,actuator)
        if settling and self.active and self.phase=='trial' and not self.finishing:
            # window_started was reset above, so the positive measurement
            # window cannot complete on this temporary row.
            self.rows=[]
            if self.candidate_ready_at is not None:
                self._status('running',settling=True,
                    settle_remaining_s=max(0.,self.candidate_ready_at-now),pid=pid.pid)

    def _healthy(self,row):
        # Quality/display tolerances are not an optimization objective. The
        # adapter has already enforced fresh input and physical owner bounds.
        return not row['limited'] and math.isfinite(row['error'])

    def _comparison_inconclusive(self,metrics,row):
        return metrics['comparison_blocks'] < 6 or metrics['resolved_within_window_drift']

    def _accept(self,metrics,old,row):
        uncertainty=self.p['confidence_multiplier']*math.hypot(old['mse_standard_error'],metrics['mse_standard_error'])
        improvement=old['mse']-metrics['mse']
        accepted=(improvement>max(uncertainty,old['mse']*self.p['minimum_improvement'])
                and metrics['peak']<=old['peak']*1.1
                and metrics['activity_per_s']<=max(old['activity_per_s']*2,old['activity_per_s']+1)
                and metrics['integral_saturation_fraction']<=old['integral_saturation_fraction'])
        self.comparison_session.comparison=dict(baseline=old,trial=metrics,accepted=accepted,
            uncertainty_margin_hz_squared=uncertainty,lock_tolerance_used=False,
            limitation='Six temporal blocks with HAC inflation; finite empirical comparison, not a calibrated confidence guarantee or global optimum')
        return accepted

    def _metrics(self, rows, actuator):
        result=super()._metrics(rows,actuator)
        # Six temporal blocks, not N independent controller samples. This
        # conservative comparison retains target bias in the squared loss.
        start,end=rows[0][0],rows[-1][0]
        duration=max(end-start,1e-12)
        blocks=[[] for _ in range(6)]
        signed=[[] for _ in range(6)]
        for stamp,row in rows:
            index=min(5,int(6*(stamp-start)/duration))
            blocks[index].append(row['error']**2);signed[index].append(row['error'])
        means=[statistics.mean(b) for b in blocks if len(b)>=2]
        signed_means=[statistics.mean(b) for b in signed if len(b)>=2]
        variance=statistics.variance(means) if len(means)>1 else 0.
        if len(means)>2:
            centre=statistics.mean(means)
            variance+=2*sum(max(0.,sum((a-centre)*(b-centre) for a,b in zip(means,means[lag:]))/len(means))
                            for lag in (1,2))
        resolved=False
        if len(signed_means)==6:
            a,b=signed_means[:3],signed_means[3:]
            se=math.sqrt((statistics.variance(a)+statistics.variance(b))/3)
            resolved=abs(statistics.mean(a)-statistics.mean(b))>4*max(se,math.ulp(max(map(abs,signed_means))))
        result.update(mse=statistics.mean(r['error']**2 for _,r in rows),
            comparison_blocks=len(means),
            mse_standard_error=math.sqrt(variance/len(means)) if means else 0.,
            resolved_within_window_drift=resolved,
            activity_per_s=sum(abs(b['output']-a['output']) for (_,a),(_,b) in zip(rows,rows[1:]))/duration,
            objective='target-centred MSE; block uncertainty, peak, actuator activity and saturation constraints',
            lock_tolerance_used=False)
        result['score']=result['mse']
        result['integral_saturation_fraction']=sum(bool(r.get('integral_saturated')) for _,r in rows)/len(rows)
        result['maximum_integral_usage']=max(r['integral_usage'] for _,r in rows)
        s=actuator.session
        self.comparison_session=s
        atomic_json(s.directory/('segment-%03d-%s-%03d-measurements.json'%(s.segment,self.phase,self.trial_index)),
                    dict(summary=result,rows=rows,pid=actuator.pid))
        return result

    def _finalize(self, pid, actuator):
        # This path can increase or decrease P, I and integral contribution.
        self._finish('succeeded' if self.accepted else 'unchanged',pid,
            'Accepted an owner-validated PI/integral candidate' if self.accepted
            else 'No reliable improvement; original settings retained')

    def _trial(self,pid):
        from ldcon.owners.flexible_native_trial import pid_from_candidate
        pid.retune(pid_from_candidate(self.p['candidate']))
        self.phase='trial';self.rows=[];self.window_started=self.clock()
        self.candidate_ready_at=None;self.candidate_measuring=False
        self._status('running',candidate=self.p['candidate'],pid=pid.pid)


class _Refused(ValueError):
    pass


def changed_adoption(last, family):
    import math
    """Compare the actual completed owner readback with this epoch's checkpoint."""
    if not isinstance(last,dict) or last.get('state')!='adopted':return False
    receipt,checkpoint=last.get('receipt') or {},last.get('checkpoint') or {}
    if receipt.get('confirmed') is not True or receipt.get('response_ended') is not True:return False
    if family=='pi':
        actual=receipt.get('actual_native') or {}
        keys=('pid_kp','pid_ki','pid_kd','pid_integral_min','pid_integral_max')
        if any(not finite(actual.get(k)) or not finite(checkpoint.get(k)) for k in keys):return False
        # Same tolerance as both owner completion readback checks.
        return any(not math.isclose(actual[k],checkpoint[k],rel_tol=1e-8,abs_tol=1e-12) for k in keys)
    if family in ('slow_loop','feedforward'):
        actual=receipt.get('actual_parameters');original=checkpoint.get('parameters')
        keys={'tec_gain','tec_dt_cap_s','supervision_interval_s','piezo_center_dac','current_per_temperature'}
        if (not isinstance(actual,dict) or not isinstance(original,dict) or
                set(actual)!=keys or set(original)!=keys):return False
        if any(value is not None and not finite(value) for row in (actual,original) for value in row.values()):return False
        return actual!=original  # Owner runtime parameter installation uses exact values.
    return False


class _PIDPort:
    def __init__(self,session,state):
        self.session=session
        self.update(state)

    def update(self,state):
        self.state=copy.deepcopy(state)
        self.pid=copy.deepcopy(state['pid']);self.config=copy.deepcopy(state['config'])

    def ready(self):return self.state['ready']

    def retune(self,pid):
        self.session._fence()
        result=self.session._effect('target',lambda:self.session.lock.background_pid_target(self.session.epoch['token'],pid))
        self.update(result['pid_tuning'])

    def save_pid(self,pid):
        raise _Refused('Persistent background PI adoption is unavailable')

    def finish(self,status):
        self.session._fence()
        result=self.session._effect('finish',lambda:self.session.lock.background_pid_finish(self.session.epoch['token'],status))
        self.session._completed(result,status)


class BackgroundPIDSession(AdjustmentContinuation):
    family='pi'

    def _parameters(self, raw, lock):
        p=background_parameters({'parameters':{}},raw,require_candidate=True)
        if p['strategy']=='validated_candidates' and not getattr(lock,'supports_validated_candidates',False):
            raise ValueError('This owner adapter does not support validated PI/integral candidates')
        if p['strategy'] not in ('factor_reduction','validated_candidates') or p['save_settings']:
            raise ValueError('Background PI requires a supported bounded strategy and save_settings=false')
        return p

    def __init__(self,directory,lock,*,pid_parameters,background,emit=lambda *a:None,
                 clock=time.monotonic,wall_clock=time.time,expected_system_id=None,
                 foreground_probe=lambda:False,poll_interval_s=1.,continuation=False):
        self._continuation_init(continuation)
        self.p=self._parameters(pid_parameters,lock)
        if (not isinstance(background,dict) or not {'monitor_id','generation','handoff_timeout_s'}<=set(background)
                or set(background)-{'monitor_id','generation','handoff_timeout_s','grant_revision','expected_operating_mode_revision'}
                or not all(isinstance(background.get(k),str) and background[k] for k in ('monitor_id','generation'))
                or not finite(background['handoff_timeout_s']) or not 0<background['handoff_timeout_s']<=600):
            raise ValueError('Background PI identity and explicit handoff deadline are required')
        self.directory=Path(directory)/'adaptive-window'
        for name in ('inbox','claimed','results'):
            path=self.directory/name
            if path.is_symlink():raise ValueError('Background PI directories must not be symlinks')
            path.mkdir(parents=True,exist_ok=True)
        self.lock,self.emit,self.clock,self.wall_clock=lock,emit,clock,wall_clock
        self.background=copy.deepcopy(background);self.expected_system_id=expected_system_id
        self.foreground_probe,self.poll_interval_s=foreground_probe,poll_interval_s
        self.active=False;self.blocked=False;self.write_unknown=False;self.effect_revision=0
        self.epoch=None;self.returning=None;self.return_deadline=None;self.last_restoration=None
        self.cancel_ids=[]
        self.deadline=clock()+self.p['timeout_s'];self.last_stamp=None;self.port=None;self.tuner=None
        self.journal=Journal(self.directory/'execution.sqlite3')
        admission=dict(family=self.family,parameters=self.p,background=background,continuation=continuation)
        self.blocked=not self.journal.admit('session','adaptive',admission)
        self.decision_revision=digest([admission,str(self.directory),wall_clock()])
        if not self.blocked:self.journal.transition('session',0,'running',{'kind':'attached_pi'})
        self._status('reconciliation_required' if self.blocked else 'idle')

    def _status(self,state,**extra):
        if self._continuation_waiting():state=self.continuation['state']
        atomic_json(self.directory/'status.json',dict(family=self.family,state=state,active=self.active,
            background=self.background,epoch=self.epoch,returning=self.returning,runtime_only=True,
            decision_revision=self.decision_revision,expires_unix_s=self.wall_clock()+max(0.,self.deadline-self.clock()),
            **{'segment':self.segment,'continuation':self.continuation,**extra}))

    def _publish(self,value):
        atomic_json(self.directory/'pid-progress.json',value)
        self.emit('background_pid_progress',value)
        if not self.blocked:self._status('tuning',progress=value)

    def _effect(self,kind,call):
        self.effect_revision+=1
        identifier='effect.%d'%self.effect_revision
        self.journal.admit(identifier,'adaptive',dict(kind=kind,epoch=self.epoch))
        self.journal.transition(identifier,0,'running',{'kind':'intent'})
        try:receipt=call()
        except (MeasurementUnavailable,BrokerError) as error:
            if not self.lock.uncertain_output and (isinstance(error,MeasurementUnavailable) or error.code in ('range','configuration')):
                self.journal.transition(identifier,1,'inconclusive',{'kind':'prewrite_refusal'},{'reason':str(error)})
                if isinstance(error,MeasurementUnavailable):raise
                raise _Refused(str(error)) from error
            self.write_unknown=self.blocked=True
            self.journal.transition(identifier,1,'uncertain',{'kind':'unknown'},{'reason':str(error)})
            raise
        except Exception as error:
            self.write_unknown=self.blocked=True
            self.journal.transition(identifier,1,'uncertain',{'kind':'unknown'},{'reason':str(error)})
            raise
        self.journal.transition(identifier,1,'succeeded',{'kind':'confirmed'},receipt)
        return receipt

    def _fence(self):
        if self.foreground_probe() or self.returning:
            self.preempt('foreground')
            raise _Refused('Foreground activity fenced the PI proposal')

    def _start(self):
        self._fence()
        info=self.lock.response_info()
        if self.expected_system_id and (info.get('owner_context') or {}).get('system_id')!=self.expected_system_id:
            raise _Refused('Owner belongs to another laser')
        status=self.lock.background_status()
        if not self._reference_before_begin(info,status):return
        result=self._effect('begin',lambda:self.lock.background_begin(family='pi',parameters=self.p,
            expected_generation=status['generation'],
                    expected_operating_mode_revision=self.background.get('expected_operating_mode_revision'),poll_interval_s=self.poll_interval_s,
            handoff_timeout_s=self.background['handoff_timeout_s'],
            identity={k:self.background[k] for k in ('monitor_id','generation')}))
        self.epoch=result['epoch'];self.active=True
        self._remember_epoch(self.epoch)
        if not self._reference_after_begin():return
        atomic_json(self.directory/'checkpoint.json',self.epoch)
        self.port=_PIDPort(self,result['pid_tuning'])
        pending=[dict(request_id=self.directory.parent.name,card_id='background-pi',parameters=self.p)]
        tuner_class=_CandidateTuner if self.p['strategy']=='validated_candidates' else _SessionTuner
        if self.p['strategy']=='validated_candidates':
            # The comparator's loop count is one; gain_factors do not select or scale this candidate.
            pending[0]['parameters']=dict(self.p,gain_factors=[.5])
        self.tuner=tuner_class(lambda:pending.pop() if pending else None,self._publish,self.emit,self.clock)

    def _completed(self,receipt,status):
        last=receipt.get('last') or {}
        expected='adopted' if status=='succeeded' else 'restored'
        if receipt.get('epoch') is not None or last.get('token')!=self.epoch['token'] or last.get('state')!=expected:
            self.write_unknown=self.blocked=True
            raise DeviceError('Owner did not confirm background PI completion')
        self.active=False;self.blocked=True
        if expected=='restored':self.last_restoration=copy.deepcopy(receipt)
        reason=self.returning or ('not_improved' if status=='unchanged' else 'comparison_inconclusive' if status=='failed' else None)
        result=dict(state=status,family=self.family,runtime_only=True,owner_receipt=receipt,
                    reason=reason,comparison_reason=getattr(self,'comparison_reason',None),
                    comparison=getattr(self,'comparison',None),
                    restart_behavior='Not saved; next arm follows configured parameters and retained-reference rules')
        atomic_json(self.directory/'results'/(self.family+'.json'),result)
        atomic_json(self.directory/('segment-%03d-%s-result.json'%(self.segment,self.family)),result)
        for identifier in self.cancel_ids:
            atomic_json(self.directory/'results'/(identifier+'.json'),dict(result,id=identifier))
        self.emit('background_'+self.family+'_result',result)
        if status == 'succeeded' and changed_adoption(last,self.family):
            self.emit('quality_adjustment_accepted', dict(family=self.family,
                action_id=last['token'], accepted_unix_s=self.wall_clock()))
        self.epoch=None;self.returning=None
        if reason in RECOVERABLE and self._interrupt(reason,evidence=result):return
        old=self.journal.get('session')
        if old['state']=='running':
            self.journal.transition('session',old['sequence'],'succeeded' if status in ('succeeded','unchanged') else 'cancelled',
                {'kind':'owner_confirmed_pi_completion'},result)
        self.epoch=None;self.returning=None
        self._status(status,result=result)

    def preempt(self,reason='foreground'):
        if self._continuation_waiting():
            self._end_continuation(reason);return
        if self.blocked or self.returning:return
        self.returning=reason
        self.return_deadline=(None if self.epoch and self.epoch.get('completion_mode')=='verified_handover'
                              else self.clock()+self.background['handoff_timeout_s'])
        self.decision_revision=digest([self.decision_revision,'revoked',reason])
        self._status('returning_before_foreground' if reason=='foreground' else 'stopping_restoring')
        if not self.active:
            self.blocked=True;self._status(reason)

    def _restore(self):
        if self.return_deadline is not None and self.clock()>self.return_deadline:
            self.blocked=True;self._status('reconciliation_required',reason='PI restoration deadline exceeded');return
        try:receipt=self._effect('restore',lambda:self.lock.background_restore(self.epoch['token'],self.returning))
        except (MeasurementUnavailable,_Refused) as error:
            self._status('stopping_restoring',reason=str(error));return
        epoch=receipt.get('epoch')
        if epoch and epoch.get('state')=='reconciliation_required':
            self.blocked=True;self._status('reconciliation_required',reason=epoch.get('reason'));return
        if epoch is None:self._completed(receipt,'cancelled')

    def check_deadlines(self):
        if self._check_continuation():return
        if self.blocked:return
        if self.active:
            status=self.lock.background_status()
            if status.get('owner_fault') or self.lock.uncertain_output:
                self.write_unknown=self.blocked=True
                raise DeviceError('Background PI owner state requires reconciliation')
            epoch=status.get('epoch')
            last=status.get('last') or {}
            if epoch is None and last.get('token')==self.epoch['token'] and last.get('state')=='restored':
                self._completed(status,'cancelled');return
            if epoch and epoch.get('state')!='active':self.preempt('foreground' if status.get('reservations') else epoch.get('reason','owner_fenced'))
            if self.returning:self._restore();return
        if self.clock()>=self.deadline:
            self.preempt('experiment_timeout')

    def poll(self,*,busy=False,row=None):
        if self._poll_continuation():return
        self.check_deadlines()
        if self.blocked or self.returning or self._continuation_waiting():return
        for path in sorted((self.directory/'inbox').glob('*.json')):
            try:
                safe_id(path.stem)
                command=bounded_json(path)
                valid=isinstance(command,dict) and command.get('id')==path.stem and set(command)<={'id','action','based_on'}
                claimed=self.directory/'claimed'/path.name
                if claimed.exists():path.unlink();continue
                path.replace(claimed)
                if valid and command.get('action')=='cancel':
                    self.cancel_ids.append(command['id'])
                    atomic_json(self.directory/'results'/path.name,dict(id=command['id'],state='stopping_restoring'))
                    self.preempt('cancelled');return
                raise ValueError('PI trials run the admitted comparison autonomously; only cancellation is accepted')
            except (ValueError,TypeError,OSError) as error:
                self.emit('background_pid_command_rejected',dict(reason=str(error)))
                # No command can gain authority by breaking this optional inbox.
                self.preempt('invalid_command');return
        try:
            if not self.active:
                if busy:return
                self._start()
            if not self.active or self.returning or self.blocked:return
            self._sample(row)
        except MeasurementUnavailable as error:
            self._status('waiting_for_measurement',reason=str(error))
        except _Refused as error:
            if self.active:self.preempt('prewrite_rejected')
            else:self.blocked=True;self._status('rejected',reason=str(error))

    def _sample(self,row):
        if row is None or not row.get('pid_tuning'):return
        self.port.update(row['pid_tuning'])
        stamp=row.get('acquired_s')
        if not finite(stamp) or self.last_stamp is not None and stamp<=self.last_stamp:return
        if self.last_stamp is not None and stamp-self.last_stamp>self.port.config['max_sample_gap_s']:
            self.tuner.restart_window('Accepted optical acquisition gap; acquire a complete new comparison')
        self.last_stamp=stamp
        tolerance=row.get('tolerance_hz')
        if not finite(tolerance) or tolerance<=0:raise _Refused('Parent lock tolerance is required for PI comparison')
        data=dict(error=row['error_hz'],filtered_error=row['error_hz'],output=row['outputs']['piezo'],
            limited=not row['pid_tuning']['eligible'],control_deadband=tolerance,requested_tolerance=tolerance)
        if self.p['strategy']=='validated_candidates':
            observation=row['pid_tuning'].get('integral_observation',{})
            # Only the supported adapter's fresh, in-bound raw-integral sample
            # can lift the legacy saturation veto. Unknown evidence still blocks.
            valid=(observation.get('valid') is True and finite(observation.get('usage'))
                   and 0<=observation['usage']<=1+1e-9)
            data.update(limited=not valid,integral_usage=observation.get('usage'),
                        integral_saturated=observation.get('saturated') is True)
        self.tuner.tick(self.clock(),data,self.port,self.port)

    def before_recovery(self):
        if self.continuation_enabled:return self._before_recovery('branch_lost')
        if not self.blocked:self.close('branch_lost')

    def close(self,reason='cancelled'):
        if self._continuation_waiting():
            self._end_continuation(reason);return
        if self.write_unknown or self.lock.uncertain_output:
            self.blocked=True;self._status('reconciliation_required');return
        if reason in ('parent_stopped','branch_lost') and self.active:
            self.lock.background_abandon(reason);self.active=False;self.blocked=True
            self._status(reason,disposition='parent_safety_or_recovery_owns_outputs');return
        self.preempt(reason)
