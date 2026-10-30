"""Attached hardware sweeps with durable identities and no main-lease renewal."""
from copy import deepcopy
from pathlib import Path
import re
import threading
import time

from experiment_core.records import Journal, digest
from ldcon.owners.owner_projection import SystemPolicyAuthority
from ldcon.owners.dlcpro_awg_plan import configuration, classify_policy_change
from ldcon.owners.dlcpro_awg_sweep import Sweep
from ldcon.jobs.modulation_sweep import validate_request


def identity(value):
    if not isinstance(value,str) or not re.fullmatch(r'[0-9a-f]{32}',value):
        raise ValueError('Expected a lowercase 32-hex request/token identity')
    return value


class Attached:
    def __init__(self, protocol, directory, *, port_factory=None, sleep=time.sleep):
        self.protocol=protocol
        self.o=protocol.owner
        self.directory=Path(directory);self.directory.mkdir(parents=True,exist_ok=True)
        self.journal=Journal(self.directory/'operations.sqlite')
        self.current_id=None
        self.saved=False
        self.sweep=None
        self.error=None
        self.preparation=None
        self.prepared=threading.Event()
        self.prepared.set()
        supplied=protocol.device.get('control_settings',{}).get('awg_sweep')
        try:
            p=configuration(supplied)
        except (ValueError,TypeError,KeyError) as error:
            self.error=str(error)
            return
        # Calibration/safety authority is immutable for this owner generation;
        # target changes do not invalidate it. No broker connection here.
        policy=dict(awg_sweep=p,axes=deepcopy(self.o.policy['axes']),
            ff_enabled=self.o.policy['ff_enabled'],ff_factor_ma_per_v=self.o.policy['ff_factor_ma_per_v'])
        self.physical_policy=deepcopy(policy)
        authority=SystemPolicyAuthority(self.o.laser_id,policy,dependencies={},
            journal=self.journal,scope=self.o.laser_id+'.awg-sweep-policy',
            classify_change=classify_policy_change)
        kwargs={} if port_factory is None else {'port_factory':port_factory}
        self.sweep=Sweep(self.o,p,authority,sleep=sleep,**kwargs)

    def _record(self,request_id):
        try:return self.journal.get(request_id)
        except KeyError:return None

    def active(self):
        return self.sweep is not None and self.sweep.active()

    def public(self, request_id=None):
        if request_id is None:
            return self.sweep.view() if self.sweep else dict(available=False,reason=self.error)
        request_id=identity(request_id)
        if request_id==self.current_id and self.sweep:
            return self.sweep.view()
        record=self._record(request_id)
        if record is None:return None
        result=record.get('result') or {}
        if 'public' in result:return deepcopy(result['public'])
        return dict(request_id=request_id,state='interrupted',ready=False,armed=False,
            restored=False,outputs_uncertain=True,reason='Unfinished durable sweep requires inspection; no replay')

    def _save_terminal(self):
        if not self.sweep or self.active() or self.saved or self.current_id is None:
            return
        status=self.sweep.view()
        full=deepcopy(self.sweep.last)
        if full is not None:full.pop('token',None)
        record=self._record(self.current_id)
        verdict='uncertain' if (status or {}).get('outputs_uncertain') else 'succeeded' if (status or {}).get('verdict')=='succeeded' else 'failed'
        self.journal.transition(self.current_id,record['sequence'],verdict,
            {'kind':'sweep_terminal'},dict(public=status,result=full))
        self.saved=True

    def begin(self, r):
        if self.sweep is None:raise ValueError(self.error or 'DLC AWG is not commissioned')
        if not self.protocol.client:raise PermissionError('A running controller lease is required')
        if (self.o.policy['axes']!=self.physical_policy['axes'] or
                self.o.policy['ff_enabled']!=self.physical_policy['ff_enabled'] or
                self.o.policy['ff_factor_ma_per_v']!=self.physical_policy['ff_factor_ma_per_v']):
            raise ValueError('AWG authority calibration/policy changed; reconfigure before sweep')
        request=deepcopy(r['request']);validate_request(request)
        request_id=identity(request['request_id']);token=identity(r['token'])
        if self.active() or self.preparing():raise ValueError('A hardware sweep is already active')
        if self._record(request_id) is not None:
            raise ValueError('Sweep request already recorded; inspect status instead of replaying')
        self.journal.admit(request_id,self.o.laser_id,dict(request=request,
            token_digest=digest(token),target_revision=r['target_revision'],pzt_scale=r.get('pzt_scale',1.)))
        self.journal.transition(request_id,0,'running',{'kind':'sweep_begin_intent'})
        self.current_id=request_id;self.saved=False
        trial_was_active = self.o.pi_trial.active is not None
        try:
            value=self.sweep.reserve(request,r['target_revision'],token=token)
        except BaseException as error:
            uncertain = bool(trial_was_active or self.o.fault or self.o.instrument.channels[self.o.channel]['fault'])
            self.sweep.last=dict(request=request,state='failed',ready=False,armed=False,
                restored=not uncertain,physical_changed=None if uncertain else False,
                outputs_uncertain=uncertain,verdict='failed',
                reason=('Reservation/preemption requires inspection: ' if uncertain else
                        'Preparation rejected before physical change: ')+str(error))
            self._save_terminal()
            raise
        self.prepared.clear()
        def prepare():
            try:
                self.sweep.prepare(pzt_scale=r.get('pzt_scale',1.))
            except BaseException as error:
                # Sweep records physical uncertainty before it raises. Keep the
                # exact failure available after the asynchronous RPC returns.
                if self.sweep.active():
                    self.sweep._uncertain('Preparation cleanup did not finish; inspect retained resources')
                self.o.audit(dict(event='awg_prepare_failed',request_id=request_id,
                                  error=type(error).__name__,reason=str(error)))
            finally:
                with self.o.mutex:
                    self.preparation=None
                    try:
                        self._save_terminal()
                    finally:
                        self.prepared.set()
        self.preparation=threading.Thread(target=prepare,name='dlc-awg-prepare',daemon=True)
        self.preparation.start()
        return value

    def preparing(self):
        return self.preparation is not None

    def tick(self):
        if self.active() and not self.preparing():
            try:self.sweep.tick()
            finally:self._save_terminal()

    def cancel(self,r,*,resume=True):
        request_id=identity(r['request_id']);token=identity(r['token'])
        record=self._record(request_id)
        if record is None or record['request'].get('token_digest')!=digest(token):
            raise PermissionError('Sweep cancellation identity mismatch')
        if request_id==self.current_id and self.active():
            if self.preparing():
                return self.sweep.request_cancel(resume=resume)
            try:self.sweep.finish('cancelled','Caller cancelled this sweep',resume=resume)
            finally:self._save_terminal()
        return self.public(request_id)

    def stop(self):
        if self.active():
            if self.preparing():
                return self.sweep.request_cancel(resume=False)
            try:self.sweep.finish('cancelled','Main lock stopped',resume=False)
            finally:self._save_terminal()

    def result(self,request_id):
        request_id=identity(request_id)
        record=self._record(request_id)
        if record is None:raise ValueError('Unknown sweep request')
        if record['state'] in ('queued','running'):
            raise ValueError('Sweep has no durable final result yet')
        return deepcopy((record.get('result') or {}).get('result'))


def attach(protocol,directory,*,port_factory=None,sleep=time.sleep):
    if 'awg_sweep' not in protocol.device.get('control_settings',{}):return
    protocol.owner.awg_sweep=Attached(protocol,directory,port_factory=port_factory,sleep=sleep)


def dispatch(protocol,action,r):
    manager=getattr(protocol.owner,'awg_sweep',None)
    if manager is None:raise ValueError('No DLC AWG configuration is registered')
    if action=='awg_sweep_status':return manager.public(r.get('request_id'))
    if action=='awg_sweep_result':return manager.result(r['request_id'])
    if action=='awg_sweep_begin':return manager.begin(r)
    if action=='awg_sweep_heartbeat':
        if not manager.active():return manager.public(r.get('request_id'))
        if r.get('request_id',manager.current_id)!=manager.current_id:raise PermissionError('Sweep request mismatch')
        return manager.sweep.heartbeat(identity(r['token']))
    if action=='awg_sweep_cancel':return manager.cancel(r)
    raise ValueError('Unsupported attached DLC sweep action')


def tick(owner):
    manager=getattr(owner,'awg_sweep',None)
    if manager:manager.tick()
