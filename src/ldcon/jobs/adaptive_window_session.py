"""Bounded point experiments attached to the stationary worker's existing lease.

Only the trusted host writes the selector/inbox. A model runs elsewhere and
returns a proposal; this poller never invokes a model, waits for a decision, or
opens a device transport. Core intents fence every owner effect. Scientific
evidence retains complete simultaneous Fizeau arrays, not folded substitutes.
"""
import copy
import json
import math
import os
from pathlib import Path
import statistics
import time

from jsonschema.exceptions import ValidationError

from experiment_core.errors import (OperationError, PrewriteRefusal,
                                    StaleContext, UncertainWrite)
from experiment_core.execution import Context, Executor, Operation, Registry, decide
from experiment_core.ownership import Grant
from experiment_core.records import Journal, atomic_json, digest, safe_id

from ldcon.core.contracts import DeviceError, MeasurementUnavailable
from ldcon.core.instrument_client import BrokerError
from ldcon.jobs.adjustment_continuation import AdjustmentContinuation,RECOVERABLE


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def bounded_json(path):
    """A local proposal is small and must not select another filesystem object."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        raw = stream.read(16385)
    if len(raw) > 16384:
        raise ValueError('Adaptive proposal exceeds 16 KiB')
    return json.loads(raw)


class _ReadyProposal:
    """Core provider containing an already received message; no provider I/O."""
    def __init__(self, command):
        self.command = command

    def propose(self, measurement, capabilities, context):
        return {'action': self.command['action'],
                'parameters': {'value': self.command['value']} if 'value' in self.command else {}}


class AdaptiveWindowSession(AdjustmentContinuation):
    def __init__(self, directory, lock, *, half_span_v, emit=lambda *args: None,
                 timeout_s=1800., max_points=64, point_timeout_s=180.,
                 decision_timeout_s=300., require_dither=True,
                 clock=time.monotonic, wall_clock=time.time, expected_system_id=None,
                 background=None, foreground_probe=lambda: False, poll_interval_s=1., continuation=False):
        self._continuation_init(continuation)
        if not finite(half_span_v) or half_span_v <= 0:
            raise ValueError('An explicit positive experiment half span is required')
        if not finite(timeout_s) or not 0 < timeout_s <= 86400:
            raise ValueError('Experiment deadline must be within 86400 seconds')
        if type(max_points) is not int or not 1 <= max_points <= 1000:
            raise ValueError('A finite point budget of 1..1000 is required')
        if any(not finite(v) or not 0 < v <= 600 for v in (point_timeout_s, decision_timeout_s)):
            raise ValueError('Point and decision deadlines must be within 600 seconds')
        if type(require_dither) is not bool:
            raise ValueError('require_dither must be boolean')
        if background is not None:
            if (not isinstance(background,dict) or not {'monitor_id','generation','handoff_timeout_s'}<=set(background)
                    or set(background)-{'monitor_id','generation','handoff_timeout_s','grant_revision','expected_operating_mode_revision'}
                    or not all(isinstance(background[k],str) and background[k] for k in ('monitor_id','generation'))
                    or not finite(background['handoff_timeout_s']) or not 0<background['handoff_timeout_s']<=600):
                raise ValueError('Background identity and explicit bounded handoff timeout are required')
            if not callable(getattr(lock,'background_begin',None)):
                raise ValueError('Owner has no background checkpoint capability')
        self.background=copy.deepcopy(background)
        self.foreground_probe=foreground_probe
        self.poll_interval_s=poll_interval_s
        self.epoch=None
        self.returning=None
        self.return_deadline=None
        self.pending_measurement=None
        self.restore_sequence=0
        self.cancel_command=None
        self.last_restoration=None
        self.directory = Path(directory) / 'adaptive-window'
        for name in ('inbox', 'claimed', 'results'):
            path = self.directory / name
            if path.is_symlink():
                raise ValueError('Adaptive directories must not be symlinks')
            path.mkdir(parents=True, exist_ok=True)
        self.lock, self.emit = lock, emit
        self.expected_system_id = expected_system_id
        self.clock, self.wall_clock = clock, wall_clock
        self.half_span, self.max_points = half_span_v, max_points
        self.point_timeout, self.decision_timeout = point_timeout_s, decision_timeout_s
        self.require_dither = require_dither
        self.deadline = clock() + timeout_s
        self.active = False
        self.effect_revision = 0
        self.command = self.context = self.begin_context = None
        self.origin = self.temperature = self.target = None
        self.last_decision = clock()
        self.frames = []
        self.points = 0
        self.measuring = False
        self.write_unknown = False
        self.journal = Journal(self.directory / 'execution.sqlite3')
        admission = dict(half_span_v=half_span_v, timeout_s=timeout_s, max_points=max_points,
                         point_timeout_s=point_timeout_s, decision_timeout_s=decision_timeout_s,
                         require_dither=require_dither,background=background,continuation=continuation)
        self.blocked = not self.journal.admit('session', 'adaptive', admission)
        self.decision_revision = digest([admission, str(self.directory), self.wall_clock()])
        if not self.blocked:
            self.journal.transition('session', 0, 'running', {'kind': 'attached'})
        self.registry = Registry()
        empty = {'type': 'object', 'additionalProperties': False}
        self.registry.register(Operation('ldcon.adaptive.begin', '1', empty,
            {'type': 'object', 'required': ['started'], 'properties': {'started': {'const': True}}},
            frozenset({'current'}), lambda owner, child, p: owner._begin(),
            lambda owner, child: owner._end()))
        self.registry.register(Operation('ldcon.adaptive.current', '1',
            {'type': 'object', 'properties': {'value': {'type': 'number'}},
             'required': ['value'], 'additionalProperties': False},
            {'type': 'object', 'required': ['applied'], 'properties': {'applied': {'type': 'boolean'}}},
            frozenset({'current'}), lambda owner, child, p: owner._current(p['value']),
            lambda owner, child: owner._end()))
        self.executor = Executor(self.registry, self.journal, self)
        self._status('reconciliation_required' if self.blocked else 'idle')

    def _status(self, state, **extra):
        if self._continuation_waiting():state=self.continuation['state']
        atomic_json(self.directory / 'status.json', dict(state=state, active=self.active,
            decision_revision=self.decision_revision, origin=self.origin,
            half_span_v=self.half_span, points=self.points, max_points=self.max_points,
            require_dither=self.require_dither,
            background=self.background,epoch=self.epoch,returning=self.returning,
            expires_unix_s=self.wall_clock()+max(0., self.deadline-self.clock()),
            request_id=self.command['id'] if self.command else None,
            **{'segment':self.segment,'continuation':self.continuation,**extra}))

    def _result(self, state, **extra):
        if self.command:
            row = dict(id=self.command['id'], command=copy.deepcopy(self.command), state=state,
                       recorded_unix_s=self.wall_clock(), **extra)
            request_id = 'command.' + digest(self.command['id'])[:32]
            old = self.journal.get(request_id)
            self.journal.transition(request_id, old['sequence'],
                'uncertain' if self.write_unknown else 'succeeded' if state in ('measured','adopted') else 'inconclusive',
                {'kind': 'point_result', 'state': state}, row)
            atomic_json(self.directory / 'results' / (self.command['id'] + '.json'), row)
            self.emit('adaptive_window_result', row)
            self.decision_revision = digest([self.decision_revision, row])
        self.command = None
        self.measuring = False
        self.last_decision = self.clock()
        retryable=self.continuation_enabled and not self.blocked and state in ('rejected','stale_decision','prewrite_rejected')
        self._status('waiting_for_decision' if self.active or retryable else state)

    def _lease_fingerprint(self):
        lease = getattr(getattr(self.lock, 'session', None), 'lease', None)
        if not isinstance(lease, str) or not lease:
            raise StaleContext('The parent owner lease is absent')
        return digest(lease)

    def _bind(self, row):
        owner = row.get('owner_context') or {}
        if set(owner) != {'system_id', 'generation', 'policy_revision'} or not all(
                isinstance(value, str) and value for value in owner.values()):
            raise StaleContext('Fresh owner context is required')
        if self.expected_system_id is not None and owner['system_id'] != self.expected_system_id:
            raise StaleContext('Owner belongs to a different laser')
        if self.context is None:
            self.owner_context = copy.deepcopy(owner)
            grant = Grant(owner['system_id'], 'ecdl.' + owner['system_id'], owner['generation'],
                          self._lease_fingerprint(), owner['policy_revision'], 'session', frozenset({'current'}))
            self.context = Context(grant, self.deadline, self.max_points * (2 if self.background else 1) + 1, clock=self.clock)
        if owner != self.owner_context or self._lease_fingerprint() != self.context.grant.lease_id:
            raise StaleContext('Parent owner, policy or lease changed; choose a new session')

    def validate(self, grant, operation, parameters):
        self.context.check()
        if self.write_unknown or self.lock.uncertain_output:
            raise UncertainWrite('Parent output is uncertain')
        if grant.lease_id != self._lease_fingerprint():
            raise StaleContext('Parent lease changed')
        # response_current itself checks its authenticated lease, fresh input,
        # exact physical context, current limits and all existing write policy.

    def validate_cancel(self, grant, operation):
        if self.write_unknown or self.lock.uncertain_output or grant.lease_id != self._lease_fingerprint():
            raise UncertainWrite('Cannot clean up a replaced or uncertain parent')

    def quarantine(self, grant):
        self.write_unknown = True
        self.blocked = True

    def _begin(self):
        if self.background and self.foreground_probe():
            raise PrewriteRefusal('Foreground reservation excludes background admission')
        self.effect_revision += 1
        try:
            if self.background:
                status=self.lock.background_status()
                if self.continuation_enabled and self.original_generation is not None and status['generation']!=self.original_generation:
                    raise StaleContext('Foreground generation changed before epoch admission')
                receipt=self.lock.background_begin(expected_generation=status['generation'],
                    expected_operating_mode_revision=self.background.get('expected_operating_mode_revision'),
                    identity={k:self.background[k] for k in ('monitor_id','generation')},
                    half_span_v=self.half_span,handoff_timeout_s=self.background['handoff_timeout_s'],
                    poll_interval_s=self.poll_interval_s)
                self.epoch=receipt['epoch']
                self._remember_epoch(self.epoch)
                atomic_json(self.directory/'checkpoint.json',self.epoch)
            else:
                receipt = self.lock.response_begin()
        except MeasurementUnavailable as error:
            if self.lock.uncertain_output:
                raise
            raise PrewriteRefusal(str(error)) from error
        except BrokerError as error:
            if self.background and not self.lock.uncertain_output and error.code in ('range','configuration'):
                raise PrewriteRefusal(str(error)) from error
            raise
        self.active = True
        return receipt

    def _current(self, value):
        if self.background and self.foreground_probe():
            self.preempt('foreground')
            raise PrewriteRefusal('Foreground reservation fenced the current proposal')
        self.effect_revision += 1
        try:
            return (self.lock.background_current(self.epoch['token'],value) if self.background
                    else self.lock.response_current(value))
        except (MeasurementUnavailable, BrokerError) as error:
            if not self.lock.uncertain_output and (isinstance(error, MeasurementUnavailable) or
                    error.code in (('range','configuration') if self.background else ('range',))):
                raise PrewriteRefusal(str(error)) from error
            raise

    def _end(self):
        self.effect_revision += 1
        receipt = self.lock.response_end()
        if not isinstance(receipt, dict) or receipt.get('ended') is not True:
            raise UncertainWrite('Owner did not confirm response cleanup')
        self.active = False
        return dict(confirmed=True, disposition='retained_last_output', owner_receipt=receipt)

    def _execute(self, operation, parameters, suffix):
        child = self.context.child('effect.'+digest([self.command['id'], suffix])[:40],
                                   {'current'}, steps=1, deadline=self.deadline)
        if suffix == 'begin':
            self.begin_context = child
        result = self.executor.execute(child, operation, '1', parameters)
        if result['state'] == 'uncertain':
            raise DeviceError('Adaptive owner effect is uncertain; no automatic replay')
        if result['state'] != 'succeeded':
            raise MeasurementUnavailable(result['result']['error']['message'])
        return result['result']['value']

    def close(self, reason='cancelled'):
        if self._continuation_waiting():
            self._end_continuation(reason);return
        if self.write_unknown or self.lock.uncertain_output:
            self.write_unknown = self.blocked = True
            self._result('unknown', disposition='no_cleanup_after_uncertain_effect')
            return
        if self.background and self.active:
            if reason in ('parent_stopped','branch_lost'):
                self.lock.background_abandon(reason)
                self.active=False
                self._result(reason,disposition='retained_for_parent_safety_or_recovery')
                self.blocked=True
                return
            self.preempt(reason)
            return
        if self.active:
            try:
                self.executor.cancel(self.begin_context, 'ldcon.adaptive.begin', '1')
            except Exception:
                self.write_unknown = self.blocked = True
                self._result('unknown', disposition='cleanup_requires_reconciliation')
                raise
        self._result(reason, disposition='retained_last_output')
        self.blocked = True
        if self.context:
            self.context.cancelled = True
        old = self.journal.get('session')
        if old['state'] == 'running':
            self.journal.transition('session', old['sequence'], 'cancelled',
                {'kind': 'child_closed', 'reason': reason}, {'reason': reason, 'parent_lease_released': False})

    def before_recovery(self):
        if self.continuation_enabled:return self._before_recovery('branch_lost')
        if self.blocked:return
        self.close('branch_lost')
        self._status('branch_lost_requires_new_session')

    def reconcile_parent_arm(self):
        """End a blocked child's bookkeeping after its owner ended that epoch.

        A return can encounter a changed branch before the parent confirms its
        hop. The child is already terminal by the time recovery succeeds. Do
        not revive it or call this a checkpoint restoration; only release its
        stale active flag using the owner's exact superseding-arm receipt.
        """
        if (not self.background or not self.active or not self.blocked or not self.epoch
                or self.write_unknown or self.lock.uncertain_output):
            return False
        try:receipt=self.lock.background_status()
        except (OSError,MeasurementUnavailable):
            # Optional bookkeeping must not stop an already recovered lock.
            # foreground() retries this read-only proof on a later cycle.
            return False
        last=receipt.get('last') or {}
        import hashlib
        lease=getattr(getattr(self.lock,'session',None),'lease',None)
        if (receipt.get('owner_fault') or receipt.get('native_pi_active') is not True
                or receipt.get('epoch') is not None or not isinstance(lease,str)
                or self.epoch.get('context',{}).get('lease')!=hashlib.sha256(lease.encode()).hexdigest()
                or last.get('state')!='superseded_by_parent_arm'
                or last.get('token')!=self.epoch.get('token')
                or last.get('context')!=self.epoch.get('context')
                or last.get('identity')!=self.epoch.get('identity')
                or last.get('generation')!=self.epoch.get('generation')):
            return False
        evidence=dict(disposition='superseded_by_parent_arm',owner_receipt=receipt,
                      interrupted_evidence=copy.deepcopy(self.pending_measurement),
                      cleanup_writes=False,automatic_continuation=False)
        atomic_json(self.directory/'terminal-parent-arm-reconciliation.json',evidence)
        if self.cancel_command:
            stop=self.cancel_command
            identifier='command.'+digest(stop['id'])[:32]
            old=self.journal.get(identifier)
            if old['state']=='running':
                row=dict(id=stop['id'],state='cancelled',**evidence)
                self.journal.transition(identifier,old['sequence'],'cancelled',
                    {'kind':'parent_arm_reconciled'},row)
                atomic_json(self.directory/'results'/(stop['id']+'.json'),row)
            self.cancel_command=None
        self.active=False;self.epoch=None;self.returning=None;self.return_deadline=None
        self.pending_measurement=None;self.measuring=False
        if self.context:self.context.cancelled=True
        self._result('cancelled',**evidence)
        old=self.journal.get('session')
        if old['state']=='running':
            self.journal.transition('session',old['sequence'],'cancelled',
                {'kind':'parent_arm_reconciled'},evidence)
        self._status('cancelled',reason='Owner superseded this terminal child epoch during parent recovery')
        self.emit('background_parent_arm_reconciled',dict(disposition='superseded_by_parent_arm',
            cleanup_writes=False,automatic_continuation=False))
        return True

    def check_deadlines(self):
        if self._check_continuation():return
        if self.blocked:
            return
        if self.background and self.active:
            owner=self.lock.background_status()
            if owner.get('owner_fault'):
                self.write_unknown=self.blocked=True
                raise DeviceError('Background owner fault requires reconciliation: '+str(owner['owner_fault']))
            epoch=owner.get('epoch')
            if epoch and epoch['token']==self.epoch['token'] and epoch['state']!='active' and not self.returning:
                self.preempt('foreground' if owner.get('reservations') else epoch.get('reason','owner_fenced'))
            if self.returning:
                self._return_step()
                return
        if self.clock() >= self.deadline:
            self.close('experiment_timeout')
        elif self.command and self.measuring and self.clock()-self.started > self.point_timeout:
            self.close('point_timeout')
        elif (self.active or self.background and self.points) and not self.command and self.clock()-self.last_decision > self.decision_timeout:
            self.close('decision_timeout')

    def preempt(self, reason='foreground'):
        """Fence proposals immediately; restoration advances without model/dither waits."""
        if self._continuation_waiting():
            self._end_continuation(reason);return
        if not self.background:
            return
        if self.returning and self.returning!='epoch_completed':
            return
        self.returning=reason
        if self.return_deadline is None:
            self.return_deadline=self.clock()+self.background['handoff_timeout_s']
        self.decision_revision=digest([self.decision_revision,'revoked',reason])
        self._status('returning_before_foreground' if reason=='foreground' else 'stopping_restoring')
        if not self.active:
            self._finish_return(None)

    def _background_effect(self, action):
        # Each attempt has a fresh durable intent. Only an explicit owner
        # prewrite refusal permits another attempt; unknown calls never replay.
        self.restore_sequence+=1
        identifier='return.'+digest([self.epoch['token'],self.restore_sequence])[:40]
        self.journal.admit(identifier,'adaptive',dict(action=action,token=self.epoch['token']))
        self.journal.transition(identifier,0,'running',{'kind':'intent'})
        self.effect_revision+=1
        try:
            receipt=(self.lock.background_adopt(self.epoch['token']) if action=='adopt' else
                     self.lock.background_restore(self.epoch['token'],self.returning))
        except (MeasurementUnavailable,BrokerError) as error:
            if not self.lock.uncertain_output and (isinstance(error,MeasurementUnavailable) or error.code in ('range','configuration')):
                self.journal.transition(identifier,1,'inconclusive',{'kind':'prewrite_refusal'},{'reason':str(error)})
                raise MeasurementUnavailable(str(error)) from error
            self.write_unknown=self.blocked=True
            self.journal.transition(identifier,1,'uncertain',{'kind':'unknown'},{'reason':str(error)})
            raise
        except Exception as error:
            self.write_unknown=self.blocked=True
            self.journal.transition(identifier,1,'uncertain',{'kind':'unknown'},{'reason':str(error)})
            raise
        self.journal.transition(identifier,1,'succeeded',{'kind':'confirmed'},receipt)
        return receipt

    def _return_step(self):
        if not self.active:return
        if self.write_unknown or self.lock.uncertain_output:
            raise DeviceError('Background effect requires reconciliation; no return replay')
        if self.clock()>self.return_deadline:
            self.blocked=True
            self._status('reconciliation_required',reason='Checkpoint restoration deadline exceeded')
            self.emit('background_reconciliation_required',dict(reason='Checkpoint restoration deadline exceeded',native_lock_stop_requested=False))
            return
        try:
            receipt=self._background_effect('restore')
        except MeasurementUnavailable as error:
            self._status('stopping_restoring',reason=str(error))
            return
        epoch=receipt.get('epoch')
        if epoch and epoch.get('state')=='reconciliation_required':
            self.blocked=True
            self._status('reconciliation_required',reason=epoch.get('reason'))
            self.emit('background_reconciliation_required',dict(reason=epoch.get('reason'),native_lock_stop_requested=False))
            return
        if epoch is None:
            last=receipt.get('last') or {}
            if last.get('token')!=self.epoch['token'] or last.get('state')!='restored':
                self.write_unknown=self.blocked=True
                raise DeviceError('Owner did not confirm this checkpoint restoration')
            self.active=False
            self._finish_return(receipt)

    def _finish_return(self, receipt):
        reason=self.returning
        self.last_restoration=copy.deepcopy(receipt)
        self.epoch=None;self.returning=None;self.return_deadline=None
        if self.cancel_command:
            stop=self.cancel_command;self.cancel_command=None
            identifier='command.'+digest(stop['id'])[:32]
            old=self.journal.get(identifier)
            row=dict(id=stop['id'],state='cancelled',disposition='checkpoint_restored',restoration=receipt)
            self.journal.transition(identifier,old['sequence'],'cancelled',{'kind':'cancel_confirmed'},row)
            atomic_json(self.directory/'results'/(stop['id']+'.json'),row)
        if reason=='epoch_completed' and self.pending_measurement is not None:
            evidence=self.pending_measurement;self.pending_measurement=None
            self._result('measured',**evidence,disposition='checkpoint_restored',restoration=receipt)
            self._status('waiting_for_decision')
        else:
            evidence=self.pending_measurement;self.pending_measurement=None
            continued=reason in RECOVERABLE and self._interrupt(reason,evidence=dict(restoration=receipt,interrupted_evidence=evidence))
            self._result(reason,disposition='checkpoint_restored',restoration=receipt,interrupted_evidence=evidence)
            if continued:
                self.continuation['revision']=self.decision_revision
                self._status(self.continuation['state'])
                return
            self.blocked=True
            if self.context:self.context.cancelled=True
            old=self.journal.get('session')
            if old['state']=='running':
                self.journal.transition('session',old['sequence'],'cancelled',{'kind':'background_closed'},
                    {'reason':reason,'parent_lease_released':False})
            self._status('foreground_ready' if reason=='foreground' else reason)

    def _claim(self, path):
        command = bounded_json(path)
        safe_id(path.stem)
        if (not isinstance(command, dict) or set(command)-{'id', 'action', 'value', 'based_on'}
                or command.get('id') != path.stem or command.get('action') not in
                ('measure_here', 'move_and_measure', 'finish', 'cancel', 'adopt')):
            raise ValueError('Invalid adaptive command')
        if command['action']=='adopt' and not self.background:
            raise ValueError('Adoption is available only for background epochs')
        if command['action'] not in ('move_and_measure','adopt') and 'value' in command:
            raise ValueError('Only a movement accepts a target value')
        claimed = self.directory / 'claimed' / path.name
        if claimed.exists():
            if bounded_json(claimed) != command:
                raise ValueError('Duplicate ID carries different content')
            path.unlink()
            return None
        os.replace(path, claimed)
        request_id = 'command.' + digest(command['id'])[:32]
        if not self.journal.admit(request_id, 'adaptive', command):
            raise UncertainWrite('Command already admitted; no replay')
        self.journal.transition(request_id, 0, 'running', {'kind': 'command_claimed'})
        return command

    def poll(self, *, busy=False,row=None):
        if self._poll_continuation():return
        self.check_deadlines()
        if self.blocked or self._continuation_waiting():
            return
        if self.returning:return
        try:
            paths = sorted((self.directory / 'inbox').glob('*.json'))[:64]
            if self.command:
                for path in paths:
                    candidate = bounded_json(path)
                    if not isinstance(candidate, dict) or candidate.get('action') != 'cancel':
                        continue
                    stop = self._claim(path)
                    if stop:
                        self.close('cancelled')
                        if self.background:
                            # Preserve the in-flight command until restoration
                            # finishes; cancel receipt records pending cleanup.
                            atomic_json(self.directory/'results'/(stop['id']+'.json'),
                                dict(id=stop['id'],state='stopping_restoring'))
                            self.cancel_command=stop
                            return
                        self.command = stop
                        self._result('cancelled', disposition='retained_last_output')
                        return
                self._measure()
                return
            if busy or not paths:
                return
            self.command = self._claim(paths[0])
            if self.command is None:
                return
            if self.command['action'] == 'cancel':
                self.close('cancelled')
                return
            if self.command.get('based_on') != self.decision_revision:
                self._result('stale_decision', reason='Refresh status and replan from current evidence')
                return
            if self.command['action'] == 'finish':
                self.close('finished')
                return
            self._start()
        except MeasurementUnavailable as error:
            if self.write_unknown or self.lock.uncertain_output:
                raise
            if self.command and self.measuring:
                self._status('waiting_for_measurement', reason=str(error))
            elif self.command:
                if self.background and self.active:
                    self.preempt('prewrite_rejected')
                else:
                    self._result('prewrite_rejected', reason=str(error))
        except (ValueError, KeyError, TypeError, OperationError, ValidationError) as error:
            if self.write_unknown or self.lock.uncertain_output:
                raise DeviceError('Adaptive output requires owner reconciliation') from error
            if self.command:
                self._result('rejected', reason=str(error))
            else:
                self.emit('adaptive_command_rejected', {'error_type': type(error).__name__})
                self.close('invalid_command')

    def _start(self):
        self.measuring = False
        if self.points >= self.max_points:
            self._result('point_budget_exhausted')
            self.close('point_budget_exhausted')
            return
        row = self.lock.response_info(include_patterns=True)
        self._bind(row)
        if not self.active and not self._reference_before_begin(row,self.lock.background_status() if self.background else {}):return
        if not row.get('native_pi_active'):
            raise DeviceError('Adaptive measurement requires active native PI')
        current = row['outputs']['current']
        axes = self.lock.recovery_ports()
        axis, temperature_axis = axes['current'].policy, axes['temperature'].policy
        self.current_tolerance = max(axis.resolution/2, 1e-12)
        self.temperature_tolerance = max(temperature_axis.resolution/2, 1e-12)
        origin = current if self.origin is None else self.origin
        empty = {'type': 'object', 'additionalProperties': False}
        actions = {'measure_here': empty, 'move_and_measure': {'type': 'object',
            'properties': {'value': {'type': 'number', 'minimum': max(axis.low, origin-self.half_span),
                                   'maximum': min(axis.high, origin+self.half_span)}},
            'required': ['value'], 'additionalProperties': False}}
        if self.background:actions['adopt']=copy.deepcopy(actions['move_and_measure'])
        proposal = decide(_ReadyProposal(self.command), {'point': row}, actions, self.context)
        target = proposal['parameters'].get('value', current)
        if not finite(target):
            raise ValueError('A finite current target is required')
        if self.command['action'] in ('move_and_measure','adopt') and axis.resolution:
            target = round(target/axis.resolution)*axis.resolution
        if self.segment_baseline_required and self.command['action']!='measure_here':
            raise ValueError('Measure a fresh baseline in this segment before moving or adopting')
        if not max(axis.low, origin-self.half_span)-1e-10 <= target <= min(axis.high, origin+self.half_span)+1e-10:
            raise ValueError('Rounded target exceeds the admitted interval')
        if axis.max_step and abs(target-current) > axis.max_step+1e-10:
            raise ValueError('Target exceeds existing owner step policy')
        if axis.max_slew_per_s and abs(target-current) > axis.max_slew_per_s*axis.update_interval_s+1e-10:
            raise ValueError('Target exceeds existing owner per-update slew policy')
        if not self.active:
            receipt = self._execute('ldcon.adaptive.begin', {}, 'begin')
            if self.origin is None:self.origin=current
            self.temperature = row['outputs']['temperature']
            self.minimum_dwell = receipt.get('minimum_dwell_s', axis.settle_s) if self.require_dither else axis.settle_s
            if not finite(self.minimum_dwell) or self.minimum_dwell < axis.settle_s:
                raise DeviceError('Invalid owner settling duration')
        if abs(row['outputs']['temperature']-self.temperature) > self.temperature_tolerance:
            self._result('context_changed')
            self.close('context_changed')
            return
        if abs(target-current) > self.current_tolerance:
            receipt = self._execute('ldcon.adaptive.current', {'value': target}, 'move')
            if receipt.get('applied') is not True:
                self._result('prewrite_rejected', receipt=receipt)
                return
            if not finite(receipt.get('value')) or abs(receipt['value']-target) > self.current_tolerance:
                self._result('partial_move_requires_replan', receipt=receipt, requested=target)
                self.close('partial_move_requires_replan')
                return
        else:
            self.context.consume()  # Measurements also consume the finite experiment budget.
        self.points += 1
        self.target, self.started = target, self.clock()
        self.after = self.clock() + axis.settle_s
        self.after_unix = self.wall_clock() + axis.settle_s
        self.frames, self.measuring = [], True
        self._status('measuring', target=target, post_settle_unix_s=self.after_unix)

    def _measure(self):
        if self.clock() <= self.after:
            return
        row = self.lock.response_info(include_patterns=True)
        self._bind(row)
        if not row.get('native_pi_active'):
            raise DeviceError('Native PI stopped during adaptive measurement')
        if (abs(row['outputs']['current']-self.target) > self.current_tolerance or
                abs(row['outputs']['temperature']-self.temperature) > self.temperature_tolerance):
            self.close('context_changed')
            return
        observation = row['observation']
        sample = observation['frequency']
        acquired = sample.get('acquired_s')
        stamp = sample.get('acquired_unix_s')
        if not finite(acquired) or not self.after < acquired <= self.clock() or self.clock()-acquired > 1:
            raise MeasurementUnavailable('A fresh post-settle optical acquisition is required')
        if not finite(stamp):
            raise MeasurementUnavailable('A source acquisition identifier is required')
        patterns = (observation.get('fringe_analysis') or {}).get('raw_patterns')
        if not isinstance(patterns, list) or not patterns or any(
                not isinstance(p, dict) or not p.get('pixels') or
                not all(finite(value) for value in p['pixels']) for p in patterns):
            raise MeasurementUnavailable('Complete finite raw interferograms are required')
        if not self.frames or stamp > self.frames[-1]['observation']['frequency']['acquired_unix_s']:
            self.frames.append(copy.deepcopy(row))
            self.frames = self.frames[-5:]
        if len(self.frames) < 5 or self.clock()-self.started < self.minimum_dwell:
            return
        if len({frame['observation']['mode_id'] for frame in self.frames}) != 1:
            raise MeasurementUnavailable('Changing branch; parent recovery retains authority')
        fit = None
        if self.require_dither:
            fit = self.lock.response_dither_info()
            start, end = fit.get('dither_source_start_unix_s'), fit.get('dither_source_unix_s')
            if (fit.get('dither_response_valid') is not True or not finite(fit.get('dither_response'))
                    or not finite(start) or not finite(end) or not self.after_unix < start <= end <= self.wall_clock()+1):
                raise MeasurementUnavailable('Wholly post-settle valid dither fit required')
        metrics = {}
        for key in observation.get('metrics', {}):
            values = [frame['observation'].get('metrics', {}).get(key) for frame in self.frames]
            if all(finite(value) for value in values):
                metrics[key] = statistics.median(values)
        evidence=dict(target=self.target,outputs=row['outputs'],optical_frames=self.frames,
                      metrics_median5=metrics,dither=fit,post_settle_unix_s=self.after_unix,
                      single_mode_claim='classifier observations only')
        if self.command['action']=='measure_here':self.segment_baseline_required=False
        if self.background:
            if self.command['action']=='adopt' and not self.foreground_probe():
                receipt=self._background_effect('adopt')
                last=receipt.get('last') or {}
                if receipt.get('epoch') is not None or last.get('token')!=self.epoch['token'] or last.get('state')!='adopted':
                    raise DeviceError('Owner did not confirm candidate adoption')
                self.active=False;self.epoch=None
                self._result('adopted',**evidence,disposition='adopted',adoption=receipt)
                before=last.get('checkpoint',{}).get('current')
                after=last.get('receipt',{}).get('current')
                if finite(before) and finite(after) and abs(after-before)>self.current_tolerance:
                    self.emit('quality_adjustment_accepted', dict(family='current',
                        action_id=last['token'], accepted_unix_s=self.wall_clock()))
                self.close('finished')
            else:
                self.pending_measurement=evidence
                self.preempt('foreground' if self.foreground_probe() else 'epoch_completed')
        else:
            self._result('measured',**evidence)


class AdaptiveExperimentManager:
    """Trusted per-job selection; a child cannot replace an active experiment."""
    def __init__(self, directory, lock, *, laser_id=None, control_root=None, **kwargs):
        self.lock, self.kwargs = lock, kwargs
        self.job_directory=Path(directory)
        self.reservations=set()
        self.dispatched=set()
        self.owner_pending=False
        self.directory = None
        self.enabled = False
        self.disabled_reason = 'LDCON_ADAPTIVE_ROOT is not configured'
        self.session = self.selection = self.selection_error = None
        root_value = control_root if control_root is not None else os.environ.get('LDCON_ADAPTIVE_ROOT')
        if not root_value:
            return
        try:
            job = Path(directory).resolve()
            deployment = next((p.parent for p in job.parents if p.name == 'jobs'), job)
            root = Path(root_value)
            if not root.is_absolute() or not root.is_dir() or root.resolve() != root:
                raise ValueError('Adaptive root must be an existing absolute non-symlink directory')
            if root == deployment or deployment in root.parents:
                raise ValueError('Adaptive root must be outside the agent-writable deployment tree')
            safe_id(laser_id)
            safe_id(job.name)
            self.directory = root / laser_id / job.name
            if any(path.is_symlink() for path in (root / laser_id, self.directory)):
                raise ValueError('Adaptive namespace must not be a symlink')
            self.directory.mkdir(parents=True, exist_ok=True)
            self.kwargs['expected_system_id'] = laser_id
            self.enabled, self.disabled_reason = True, None
            atomic_json(self.directory/'protocol.json',dict(schema_version=1,continuation=self._supports_continuation()))
        except (ValueError, OSError) as error:
            self.disabled_reason = str(error)
            self.kwargs.get('emit', lambda *a: None)('adaptive_root_rejected', {'reason': self.disabled_reason})

    def _supports_continuation(self):
        try:return self.lock.background_status().get('idle_foreground_fencing') is True
        except (AttributeError,ValueError,OSError,DeviceError,BrokerError):return False

    @property
    def active(self):
        return bool(self.owner_pending or self.session and self.session.active)

    @property
    def effect_revision(self):
        return (str(self.session.directory), self.session.effect_revision) if self.session else (None, 0)

    def foreground_ids(self):
        """Observe pending requests without claiming/rejecting their original IDs.

        File queues are observed at each lock cycle and immediately before an
        epoch effect/adoption. Direct owner requests fence atomically at dispatch.
        """
        patterns=('operating-point-requests/pending/*.json','ff-requests/pending/*.json',
                  'runtime-parameter-request-*.json','laser-reload-request-*.json',
                  'online-request.json','hot-control-request.json')
        pending={str(path.relative_to(self.job_directory)) for pattern in patterns
                 for path in self.job_directory.glob(pattern)}
        if (self.job_directory/'window-actions.json').exists():
            from ldcon.jobs.window_actions import WindowQueue,BLOCKING
            try:
                pending.update('window-action:'+row['request_id'] for row in WindowQueue(self.job_directory).list()
                               if row['status'] in BLOCKING|{'queued'})
            except (ValueError,OSError):
                # An unreadable activity ledger cannot prove an idle grant.
                pending.add('window-actions:reconciliation_required')
        return pending

    def _background_enabled(self, background):
        if not isinstance(background,dict) or not all(isinstance(background.get(k),str) and background[k]
                for k in ('monitor_id','generation')):return False
        path=self.directory/'background-control.json'
        if not path.exists():return False
        try:control=bounded_json(path)
        except (ValueError,OSError):return False
        return (isinstance(control,dict) and control.get('enabled') is True
                and all(control.get(k)==background[k] for k in ('monitor_id','generation'))
                and self._monitor_grant_valid(background))

    def _monitor_grant_valid(self,background):
        try:
            monitor=safe_id(background['monitor_id'])
            path=self.directory.parent/'monitors'/(monitor+'.json')
            if path.parent.is_symlink():return False
            grant=bounded_json(path)
        except (ValueError,OSError):return False
        now=self.kwargs.get('wall_clock',time.time)()
        return (isinstance(grant,dict) and grant.get('enabled') is True
                and isinstance(grant.get('bounds'),dict) and grant['bounds'].get('enabled') is True
                and grant.get('generation')==background['generation']
                and isinstance(background.get('grant_revision'),str) and bool(background['grant_revision'])
                and grant.get('parameters_hash')==background['grant_revision']
                and finite(grant.get('last_seen')) and finite(grant.get('max_age_s'))
                and 0<grant['max_age_s']<=180 and 0<=now-grant['last_seen']<=grant['max_age_s'])

    def foreground(self):
        """Reserve first, restore independently, then let original queues execute."""
        session=self.session
        if not session or not session.background:return True
        if session.blocked and session.active and hasattr(session,'reconcile_parent_arm'):
            session.reconcile_parent_arm()
        if session.blocked and not session.active:
            # Reopening a journal never proves that its old owner epoch ended.
            # Preserve foreground requests until explicit reconciliation; do not
            # turn an unknown old effect into a fresh cleanup replay.
            owner=self.lock.background_status()
            if owner.get('owner_fault'):raise DeviceError('Background owner requires reconciliation')
            self.owner_pending=owner.get('epoch') is not None
        pending=self.foreground_ids()
        added=pending-self.reservations
        removed=self.reservations-pending
        if added:self.lock.background_reserve(sorted(added))
        if removed:self.lock.background_release(sorted(removed))
        self.dispatched-=removed
        self.reservations=pending
        if pending and not session.blocked:session.preempt('foreground')
        if not self._background_enabled(session.background) and not session.blocked:
            session.preempt('background_disabled')
        return not self.active

    def _foreground_probe(self):
        self.foreground()
        return bool(self.reservations or self.session and self.session.returning)

    def foreground_dispatch(self, kind):
        """Timestamp dispatch of original queued IDs, after confirmed cleanup."""
        if not self.session or not self.session.background or self.session.active:return
        prefixes={'point':('operating-point-requests/',),'sweep':('ff-requests/',),
                  'runtime':('runtime-parameter-','laser-reload-'),
                  'study':('online-request','hot-control-request','window-action:')}
        identifiers={r for r in self.reservations if r.startswith(prefixes[kind])}-self.dispatched
        if not identifiers:return
        self.dispatched|=identifiers
        record=dict(request_ids=sorted(identifiers),kind=kind,
                    foreground_dispatch_started_at=self.kwargs.get('clock',time.monotonic)(),
                    foreground_dispatch_started_unix_s=self.kwargs.get('wall_clock',time.time)(),
                    session=str(self.session.directory),restoration=self.session.last_restoration,
                    disposition='checkpoint_restored' if self.session.last_restoration else 'no_uncommitted_epoch')
        atomic_json(self.directory/'background-handoff.json',record)
        self.kwargs.get('emit',lambda *a:None)('background_foreground_dispatch',record)

    def poll(self, *, busy=False,row=None):
        if not self.enabled:
            return
        try:
            self._select(busy)
        except (ValueError, TypeError, OSError, DeviceError) as error:
            reason = str(error)
            if reason != self.selection_error:
                self.kwargs.get('emit', lambda *a: None)('adaptive_selector_rejected', {'reason': reason})
                self.selection_error = reason
        if self.session:
            self.foreground()
            self.session.poll(busy=busy or bool(self.reservations),row=row)

    def allows_owner_slow_actions(self):
        """Only parameter comparisons retain the normal, owner-bounded outer loop."""
        from ldcon.core.adjustment_registry import PARAMETER_FAMILIES
        session=self.session
        if not session or getattr(session,'family',None) not in PARAMETER_FAMILIES:return False
        if not session.active or session.blocked or session.returning or self.reservations:return False
        state=self.lock.background_status()
        epoch=state.get('epoch') or {}
        return bool(not state.get('reservations') and epoch.get('state')=='active'
                    and epoch.get('token')==(session.epoch or {}).get('token'))

    def _select(self, busy):
        path = self.directory / 'adaptive-experiment.json'
        if busy or self.active or not path.exists():
            return
        selection = bounded_json(path)
        allowed = {'id', 'family','pid_parameters','adjustment_parameters','half_span_v', 'timeout_s', 'max_points', 'point_timeout_s', 'decision_timeout_s', 'require_dither','background','continuation'}
        if not isinstance(selection, dict) or not {'id'} <= set(selection) <= allowed:
            raise ValueError('Invalid adaptive selector')
        if selection.get('continuation') and not self._supports_continuation():
            raise ValueError('This owner does not support continuation foreground fencing')
        family=selection.get('family','current')
        from ldcon.core.adjustment_registry import FAMILIES,PARAMETER_FAMILIES
        if family not in FAMILIES:raise ValueError('Unsupported background adjustment family')
        if family=='current' and ('half_span_v' not in selection or 'pid_parameters' in selection or 'adjustment_parameters' in selection):raise ValueError('Invalid current selector')
        if family=='pi' and (set(selection)-{'continuation'}!={'id','family','pid_parameters','background'}):raise ValueError('Invalid background PI selector')
        if family in PARAMETER_FAMILIES and set(selection)-{'continuation'}!={'id','family','adjustment_parameters','background'}:
            raise ValueError('Invalid runtime parameter selector')
        safe_id(selection['id'])
        if selection.get('background') and not self._background_enabled(selection['background']):
            return
        if selection.get('background') and self.foreground_ids():return
        if self.selection and selection['id'] == self.selection['id'] and selection != self.selection:
            raise ValueError('Session admission is immutable; choose a new ID')
        if selection != self.selection:
            session_type=AdaptiveWindowSession
            if family=='pi':
                from ldcon.jobs.background_pid_session import BackgroundPIDSession
                session_type=BackgroundPIDSession
            if family in PARAMETER_FAMILIES:
                from ldcon.jobs.background_parameter_session import BackgroundParameterSession
                session_type=BackgroundParameterSession
            candidate = session_type(self.directory / 'adaptive-experiments' / selection['id'],
                self.lock, **{k: v for k, v in selection.items() if k not in ('id','family')},
                **({'family':family} if family in PARAMETER_FAMILIES else {}),
                foreground_probe=self._foreground_probe,**self.kwargs)
            if self.session:
                if self.session.background and not self.session.blocked:self.session.close('superseded')
                self.session.journal.close()
            self.session, self.selection, self.selection_error = candidate, selection, None

    def check_deadlines(self):
        if self.session:
            self.foreground()
            self.session.check_deadlines()

    def before_recovery(self):
        if self.session:
            return self.session.before_recovery()

    def after_recovery(self, ticket):
        if self.session and ticket:self.session.after_recovery(ticket)
        if self.session and hasattr(self.session,'reconcile_parent_arm'):
            self.session.reconcile_parent_arm()

    def close(self, reason='cancelled'):
        if self.session:
            self.session.close(reason)
