"""Shared recover/observe/replan gate for finite adjustment tasks.

No method here writes to a device. Parent recovery and epoch restoration retain
that authority. A recoverable outcome is evidence, never permission to replay.
"""
import copy
import hashlib
import math

from experiment_core.records import atomic_json, digest, safe_id
from ldcon.core.contracts import MeasurementUnavailable
from ldcon.core.records import read_json


RECOVERABLE = frozenset(('point_timeout', 'prewrite_rejected',
    'partial_move_requires_replan', 'not_improved', 'incomparable_source_cadence',
    'insufficient_thermal_excitation', 'frequency_regressed','comparison_inconclusive'))
WAITING = frozenset(('waiting_recovery', 'awaiting_replan'))


class AdjustmentContinuation:
    def _lease_fingerprint(self):
        lease=getattr(getattr(self.lock,'session',None),'lease',None)
        if not isinstance(lease,str) or not lease:raise ValueError('Parent lease is absent')
        return hashlib.sha256(lease.encode()).hexdigest()

    def _continuation_init(self, enabled):
        if type(enabled) is not bool:raise ValueError('Continuation must be boolean')
        self.continuation_enabled=enabled
        self.continuation=None
        self.segment=0
        self.original_context=None
        self.original_checkpoint=None
        self.original_generation=None
        self.segment_baseline_required=False

    def _remember_epoch(self, epoch):
        if not self.continuation_enabled or self.original_context is not None:return
        self.original_context=copy.deepcopy(epoch['context'])
        self.original_checkpoint=copy.deepcopy(epoch['checkpoint'])
        self.original_generation=epoch['generation']

    def _continuation_waiting(self):
        return bool(self.continuation and self.continuation['state'] in WAITING)

    def _interrupt(self, reason, *, recovery=False, evidence=None):
        if not self.continuation_enabled or self.original_context is None:return False
        if self.write_unknown or self.lock.uncertain_output:return False
        old=self.journal.get('session')
        if old['state']!='running' or self.clock()>=self.deadline:return False
        if getattr(self,'points',0)>=getattr(self,'max_points',float('inf')):return False
        partial={k:copy.deepcopy(getattr(self,k,None)) for k in
                 ('command','target','frames','pending_measurement','rows','baseline','comparison','epoch')}
        tuner=getattr(self,'tuner',None)
        if tuner is not None:
            partial['pid_comparison']={k:copy.deepcopy(getattr(tuner,k,None)) for k in
                ('rows','phase','best_metrics','original_pid','best_pid','trial_index','accepted')}
        self.active=False;self.blocked=False
        self.frames=[];self.pending_measurement=None;self.measuring=False
        self.returning=None;self.return_deadline=None
        self.decision_revision=digest([self.decision_revision,'interrupted',reason,self.segment])
        self.continuation=dict(schema_version=1,state='waiting_recovery' if recovery else 'awaiting_replan',
            reason=reason,segment=self.segment,revision=self.decision_revision,
            recovery_confirmed=not recovery,evidence=copy.deepcopy(evidence),partial_evidence=partial,
            message='No replay. Wait for verified cleanup and fresh context, then choose a new plan.')
        atomic_json(self.directory/('segment-%03d-outcome.json'%self.segment),self.continuation)
        atomic_json(self.directory/('segment-%03d-outcome-%s.json'%(self.segment,self.decision_revision[:16])),self.continuation)
        self._status(self.continuation['state'],continuation=self.continuation)
        self.emit('adjustment_interrupted',self.continuation)
        return True

    def _end_continuation(self, reason):
        if not self._continuation_waiting():return
        self.continuation={**self.continuation,'state':'terminal','stop_reason':reason}
        self.blocked=True
        self.decision_revision=digest([self.decision_revision,'terminal',reason])
        old=self.journal.get('session')
        if old['state']=='running':
            self.journal.transition('session',old['sequence'],'cancelled',
                {'kind':'continuation_ended','reason':reason},{'reason':reason})
        self._status(reason,continuation=self.continuation)

    def _fresh_continuation_context(self):
        bg=self.lock.background_status()
        if self.write_unknown or self.lock.uncertain_output or bg.get('owner_fault'):
            raise ValueError('Owner fault or uncertain effect')
        if bg.get('reservations') or self.foreground_probe():
            raise ValueError('Foreground operation takes priority')
        if self.blocked or not self._continuation_waiting() or self.journal.get('session')['state']!='running':
            raise ValueError('Continuation authority was revoked')
        if bg.get('epoch') is not None:raise MeasurementUnavailable('Owner cleanup is not complete')
        if bg.get('generation')!=self.original_generation:
            raise ValueError('Foreground generation changed')
        if hasattr(self.lock,'continuation_info'):
            info=self.lock.continuation_info(self.original_context)
            context=info['context']
        else:
            info=self.lock.response_info()
            context={**info.get('owner_context',{}),
                'lease':hashlib.sha256(self.lock.session.lease.encode()).hexdigest(),
                'target':info.get('target_frequency_hz'),
                'mode_id':info.get('observation',{}).get('mode_id')}
            sample=info.get('observation',{}).get('frequency',{})
            acquired=sample.get('acquired_s')
            if type(acquired) not in (int,float) or not math.isfinite(acquired) or not 0<=self.clock()-acquired<=max(1.,3*self.poll_interval_s):
                raise MeasurementUnavailable('Fresh owner observation is required')
        if context!=self.original_context:raise ValueError('Target, branch, owner, policy or lease changed')
        if info.get('native_pi_active') is not True:
            raise MeasurementUnavailable('Verified active PI is required')
        # A foreground operation can complete between reads without leaving
        # a reservation or a different target behind.
        after=self.lock.background_status()
        if after.get('generation')!=self.original_generation or after.get('reservations'):
            raise ValueError('Foreground generation changed during observation')
        if after.get('epoch') is not None or after.get('owner_fault') or self.lock.uncertain_output:
            raise ValueError('Owner state changed during observation')
        outputs=info.get('outputs',{})
        if getattr(self,'family','current')=='current':
            value=outputs.get('current')
            if type(value) not in (int,float) or self.origin is None or not self.origin-self.half_span<=value<=self.origin+self.half_span:
                raise ValueError('Recovered point is outside the original task interval')
        elif getattr(self,'family',None) in ('slow_loop','feedforward'):
            # These owner loops move autonomously. Do not recenter their
            # excursion limits on a different recovered current/TEC point.
            if not self._original_outputs_match(outputs):
                raise ValueError('Autonomous-loop checkpoint changed; a new approved task is required')
        return dict(context_revision=digest(context),outputs=outputs,
            native_pi_active=True,verified_unix_s=self.wall_clock())

    def _original_outputs_match(self, outputs):
        checkpoint=self.original_checkpoint.get('outputs',self.original_checkpoint)
        for axis in ('current','temperature'):
            tolerance=max(self.lock.recovery_ports()[axis].policy.resolution/2,1e-12)
            if axis not in checkpoint or axis not in outputs or abs(outputs[axis]-checkpoint[axis])>tolerance:return False
        return True

    def _reference_before_begin(self, info, bg):
        """Fence the gap between agent approval and actual epoch admission."""
        if not self.continuation_enabled or self.original_context is None:return True
        if hasattr(self.lock,'continuation_info'):
            context=self.lock.continuation_info(self.original_context)['context']
        else:
            context={**info.get('owner_context',{}),
                'lease':hashlib.sha256(self.lock.session.lease.encode()).hexdigest(),
                'target':info.get('target_frequency_hz'),
                'mode_id':info.get('observation',{}).get('mode_id')}
        if (context!=self.original_context or bg.get('generation')!=self.original_generation
                or bg.get('reservations') or getattr(self,'family',None) in ('slow_loop','feedforward')
                and not self._original_outputs_match(info.get('outputs',{}))):
            self.close('context_changed');return False
        return True

    def _reference_after_begin(self):
        if not self.continuation_enabled or self.segment==0:return True
        if (self.epoch['context']!=self.original_context or self.epoch['generation']!=self.original_generation
                or getattr(self,'family',None) in ('slow_loop','feedforward')
                and not self._original_outputs_match(self.epoch['checkpoint'].get('outputs',{}))):
            self.preempt('context_changed');return False
        return True

    def after_recovery(self, ticket):
        if not ticket or not self._continuation_waiting() or self.continuation['revision']!=ticket:return
        if self.continuation['state']!='waiting_recovery':return
        # Only the parent invokes this after its recovery/PI rearm returns.
        self.continuation['recovery_confirmed']=True
        self._check_continuation()

    def _check_continuation(self):
        if not self._continuation_waiting():return False
        if self.clock()>=self.deadline:
            self._end_continuation('experiment_timeout');return True
        if getattr(self,'points',0)>=getattr(self,'max_points',float('inf')):
            self._end_continuation('point_budget_exhausted');return True
        if self.foreground_probe():
            self._end_continuation('foreground');return True
        if self.blocked or not self._continuation_waiting():return True
        if not self.continuation['recovery_confirmed']:return True
        try:proof=self._fresh_continuation_context()
        except (MeasurementUnavailable,OSError):
            self.continuation['state']='waiting_recovery'
            self._status('waiting_recovery',continuation=self.continuation);return True
        except (ValueError,AttributeError,TypeError) as error:
            self._end_continuation('context_changed');return True
        if self.blocked or not self._continuation_waiting():return True
        self.continuation.update(state='awaiting_replan',proof=proof)
        self._status('awaiting_replan',continuation=self.continuation)
        return True

    def _before_recovery(self, reason):
        # A later parent hop must never revive a user-stopped/finished task.
        if (not self.continuation_enabled or self.blocked or self.original_context is None
                or self.write_unknown or self.lock.uncertain_output):return None
        foreground=self.foreground_probe()
        if self.blocked:return None
        stopped=self.returning if self.returning not in ({None,'epoch_completed'}|RECOVERABLE) else None
        if stopped or foreground:
            stop_reason=stopped or 'foreground'
            if self.active:self.lock.background_abandon(stop_reason)
            self.active=False;self.epoch=None;self.returning=None;self.return_deadline=None
            self.close(stop_reason)
            return None
        old_epoch=copy.deepcopy(self.epoch)
        if self.active:self.lock.background_abandon(reason)
        self.active=False;self.epoch=None
        evidence=dict(reason=reason,segment=self.segment,previous_epoch=old_epoch,disposition='parent_recovery_owns_outputs')
        if self._interrupt(reason,recovery=True,evidence=evidence):
            if hasattr(self,'_result'):
                self._result(reason,disposition='retained_for_parent_safety_or_recovery')
                self.continuation['revision']=self.decision_revision
                self._status('waiting_recovery')
            return self.continuation['revision']
        self.blocked=True
        terminal='experiment_timeout' if self.clock()>=self.deadline else 'point_budget_exhausted'
        old=self.journal.get('session')
        if old['state']=='running':
            self.journal.transition('session',old['sequence'],'cancelled',
                {'kind':'continuation_budget_exhausted'},{'reason':terminal,'parent_recovery_owns_outputs':True})
        if hasattr(self,'_result'):self._result(terminal,disposition='parent_recovery_owns_outputs')
        self._status(terminal)
        return None

    def _accept_replan(self, command):
        if not self._continuation_waiting() or self.continuation['state']!='awaiting_replan':
            raise ValueError('No verified continuation is available')
        if command.get('based_on')!=self.decision_revision:
            raise ValueError('Stale continuation proposal')
        if not self.continuation['recovery_confirmed']:raise ValueError('Recovery has not completed')
        proof=self._fresh_continuation_context()
        if self.clock()>=self.deadline:raise ValueError('Task deadline expired')
        self.segment+=1;self.segment_baseline_required=True
        self.continuation=None
        self.decision_revision=digest([self.decision_revision,'replan',self.segment,command['id']])
        self.last_decision=self.clock()
        atomic_json(self.directory/('segment-%03d-start.json'%self.segment),
            dict(command=command,proof=proof,revision=self.decision_revision,
                 deadline=self.deadline,points=getattr(self,'points',None),origin=getattr(self,'origin',None)))
        self.emit('adjustment_replanned',dict(segment=self.segment,request_id=command['id']))

    def _poll_continuation(self):
        if not self._continuation_waiting():return False
        self._check_continuation()
        if self.blocked:return True
        for path in sorted((self.directory/'inbox').glob('*.json')):
            # Queue processing never interprets an old move as a new plan.
            from ldcon.jobs.adaptive_window_session import bounded_json
            try:
                safe_id(path.stem)
                command=bounded_json(path)
                if not isinstance(command,dict) or command.get('id')!=path.stem:
                    raise ValueError('Invalid continuation command identity')
                if command.get('action')!='cancel' and self.continuation['state']!='awaiting_replan':continue
                claimed=self.directory/'claimed'/path.name
                if claimed.exists():path.unlink();continue
                path.replace(claimed)
            except (ValueError,TypeError,OSError):
                self.emit('adjustment_replan_rejected',dict(reason='Unreadable or malformed continuation command'))
                self._end_continuation('invalid_command');return True
            if command.get('action')=='cancel':
                self._end_continuation('cancelled');return True
            try:
                if command.get('action')!='replan':raise ValueError('New replan decision required')
                if set(command)-{'id','action','based_on','candidate'}:raise ValueError('Unexpected replan fields')
                if getattr(self,'family','current')=='current' and 'candidate' in command:raise ValueError('Current continuation has no candidate override')
                candidate=command.get('candidate')
                if candidate is not None:
                    checked=self._parameters({**self.p,'candidate':candidate},self.lock)
                self._accept_replan(command)
                if candidate is not None:self.p=checked
                if hasattr(self,'tuner'):
                    self.tuner=None;self.port=None;self.last_stamp=None
                    self.rows=[];self.last_source=None;self.last_native_seq=None
                    self.phase='baseline';self.baseline=None;self.comparison=None
                self._status('idle')
                atomic_json(self.directory/'results'/path.name,dict(id=command['id'],state='replanned',segment=self.segment))
            except MeasurementUnavailable:
                # Metadata-only request: retain it until fresh verification is
                # available. No physical command has been submitted or replayed.
                try:claimed.replace(path)
                except OSError:self._end_continuation('invalid_command')
            except (ValueError,TypeError,KeyError) as error:
                atomic_json(self.directory/'results'/path.name,dict(id=command['id'],state='replan_rejected',reason=str(error)))
                self.emit('adjustment_replan_rejected',dict(request_id=command['id'],reason=str(error)))
            break
        return True
