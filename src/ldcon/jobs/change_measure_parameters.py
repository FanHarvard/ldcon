"""Typed runtime parameter transactions through existing owner trial epochs.

This is a host adapter, never a device writer. It preserves the controller job
identity, issues a separate finite measurement grant, and uses only an owner's
advertised transition mode. Unsupported mixed-axis transactions fail preflight.
"""
import copy
import hashlib
import math
from pathlib import Path
import time

from ldcon.core.contracts import DeviceError
from ldcon.core.instrument_client import BrokerError
from ldcon.core.records import read_json
from ldcon.owners.flexible_native_trial import candidate, pid_from_candidate
from ldcon.core.adjustment_registry import PARAMETER_FAMILIES, parameters as parameter_plan
from ldcon.jobs.background_pid_session import background_parameters
from ldcon.agents.measurement_grant import MeasurementGrant

PI = frozenset(('kp_count_per_nm', 'ki_count_per_nm_s', 'integral_limit_count'))


def same(left, right):
    return (set(left) == set(right) and all(type(left[k]) in (int,float) and
        math.isclose(left[k],right[k],rel_tol=1e-8,abs_tol=1e-10) for k in right))


class ParameterMeasurementAdapter:
    def __init__(self, movement, laser, directory, controller_directory, *, clock, sleep, cancel,
                 emit, wall_clock=time.time, grant_factory=MeasurementGrant):
        self.movement, self.laser = movement, laser
        self.directory = Path(directory) if directory else None
        self.controller = Path(controller_directory) if controller_directory else None
        self.clock, self.sleep, self.cancel, self.emit, self.wall_clock = clock, sleep, cancel, emit, wall_clock
        self.grant_factory = grant_factory
        self.native = hasattr(movement, 'port')
        self.lock = getattr(movement, 'lock', None)
        self.grant = self.epoch = self.plan = None
        self.write_unknown = False
        self.confirmed_application = False
        self.explicit_prewrite_refusal = False

    def _status(self):
        if self.native:
            return self.movement.port._pid_read('background_status')
        if self.lock is None or not callable(getattr(self.lock,'background_status',None)):
            raise DeviceError('This owner has no validated runtime parameter transaction binding')
        return self.lock.background_status()

    def _effect(self, action, **values):
        self.write_unknown = True
        self.explicit_prewrite_refusal = False
        port = self.movement.port if self.native else self.lock.session
        try:
            if self.native:
                result = port.mutate(action, **values)
            else:
                result = getattr(self.lock, action)(**values)
        except Exception as error:
            cause = error if isinstance(error,BrokerError) else error.__cause__
            # These broker codes are documented owner prewrite refusals. A
            # generic ValueError/DeviceError is never treated as this proof.
            if (isinstance(cause,BrokerError) and cause.code in ('measurement','range','configuration','busy','request','ownership')
                    and getattr(port,'uncertain_output',True) is False):
                self.write_unknown = False
                self.explicit_prewrite_refusal = True
            raise
        self.write_unknown = False
        return result

    def physical_outcome_on_error(self, error):
        if self.write_unknown:return 'unknown'
        if self.confirmed_application:return 'applied'
        if self.explicit_prewrite_refusal:return 'not_applied'
        return 'unknown'

    def _actual(self, family):
        status = self._status()
        if family != 'pi':
            return status.get('actual_parameters') or {}
        if self.native:
            return {k:v for k,v in (status.get('actual_pid') or {}).items() if k in PI}
        cfg = self.movement.info().get('servo_config') or {}
        if not all(k in cfg for k in ('pid_kp','pid_ki','pid_integral_min','pid_integral_max')):
            raise DeviceError('Current owner PI readback is unavailable')
        return dict(kp_count_per_nm=cfg['pid_kp'],ki_count_per_nm_s=cfg['pid_ki'],
            integral_limit_count=max(abs(cfg['pid_integral_min']),abs(cfg['pid_integral_max']))*abs(cfg['pid_ki']))

    def preflight(self, changes, mode, measurements, planned=None):
        if self.directory is None or self.controller is None or not getattr(self.movement,'locked',False):
            raise DeviceError('Runtime parameter measurements require an identified active controller')
        names = {c['parameter'] for c in changes}
        families = {'pi':PI, **{k:frozenset(v) for k,v in PARAMETER_FAMILIES.items()}}
        matches = [key for key,fields in families.items() if names and names <= fields]
        if len(matches) != 1:
            raise DeviceError('No owner transaction for this parameter/axis combination; split it into explicit batch steps')
        family = matches[0]
        if measurements.get('dither') or measurements.get('optical_quality'):
            if self.native:
                raise DeviceError('Native-current owner has no configured optical-quality/dither measurement binding')
        status = self._status()
        if status.get('owner_fault') or status.get('epoch') or status.get('reservations'):
            raise DeviceError('Owner parameter transaction is busy or faulted')
        bounds = read_json(self.directory/'invocation.json',{}).get('measurement_plan')
        if not isinstance(bounds,dict) or not bounds.get('enabled') or family not in bounds.get('families',[]):
            raise DeviceError('No frozen approved measurement plan for '+family)
        if family not in status.get('supported_adjustments',[]):
            raise DeviceError('Owner does not support '+family+' runtime changes')
        capability = (status.get('capabilities') or {}).get(family) or {}
        if family == 'pi':
            atomic = capability.get('supported') is True and capability.get('completion_mode') == 'verified_handover'
            if mode == 'atomic' and not atomic:
                raise DeviceError('Atomic PI handover is unavailable; explicitly choose owner-coordinated application')
            actual_mode = 'atomic' if atomic else 'coordinated'
        else:
            if capability.get('supported') is not True:
                raise DeviceError('Owner has no validated '+family+' envelope')
            actual_mode = 'atomic'  # Parameters install together under the owner mutex; future outputs remain owner-controlled.
        current = self._actual(family)
        fields = families[family]
        checkpoint = {k:(planned or {}).get(k,current[k]) for k in fields if k in current}
        if family == 'pi' and set(checkpoint) != PI:
            raise DeviceError('Complete current PI readback is required')
        resolved = []
        target = copy.deepcopy(checkpoint)
        for item in changes:
            name = item['parameter']
            if name not in checkpoint or type(checkpoint[name]) not in (int,float):
                raise DeviceError('Finite runtime checkpoint unavailable for '+name)
            target[name] = item.get('value',checkpoint[name]+item.get('offset',0.))
            resolved.append(dict(parameter=name,value=target[name]))
        if family == 'pi':
            candidate(target)
            if any(target[k]*checkpoint[k] <= 0 for k in ('kp_count_per_nm','ki_count_per_nm_s')):
                raise DeviceError('PI changes must preserve the commissioned feedback sign')
            params = background_parameters({'parameters':{}}, dict(bounds.get('pid_parameters') or {},candidate=target),require_candidate=True)
            if params['strategy'] != 'validated_candidates':
                raise DeviceError('The frozen PI plan must admit independent validated candidates')
        else:
            target = {c['parameter']:c['value'] for c in resolved}
            allowed = (status.get('parameter_adjustments') or {}).get(family,{}).get('bounds',{})
            if any(name not in allowed or not allowed[name][0] <= value <= allowed[name][1] for name,value in target.items()):
                raise DeviceError('Runtime parameter candidate exceeds owner-adopted bounds')
            params = parameter_plan(dict((bounds.get('adjustment_parameters') or {}).get(family) or {},candidate=target),family,require_candidate=True)
        return dict(binding='runtime_parameter_owner',family=family,mode=mode,actual_mode=actual_mode,
            changes=resolved,checkpoint=checkpoint,planned={**(planned or {}),**target},candidate=target,
            owner_parameters=params,approved_plan=copy.deepcopy(bounds),measurements=copy.deepcopy(measurements))

    def check(self):
        if self.grant:self.grant.renew()
        if self.epoch:
            status = self._status()
            epoch = status.get('epoch')
            if status.get('owner_fault') or not epoch or epoch.get('token') != self.epoch['token'] or epoch.get('state') != 'active':
                raise DeviceError('Runtime parameter epoch interrupted; inspect the owner handoff receipt')

    def apply(self, plan, sample):
        actual = self._actual(plan['family'])
        if not same({k:actual.get(k) for k in plan['checkpoint']},plan['checkpoint']):
            raise DeviceError('Runtime parameter checkpoint changed after preflight')
        self.plan = plan
        self.confirmed_application = False
        self.explicit_prewrite_refusal = False
        lease=(getattr(self.movement.port,'lease',None) if self.native else getattr(getattr(self.lock,'session',None),'lease',None))
        if not isinstance(lease,str) or not lease:
            raise DeviceError('Current controller lease is unavailable for the measurement grant')
        self.grant = self.grant_factory(self.directory,self.controller,self.laser['id'],plan['approved_plan'],
            lease_sha256=hashlib.sha256(lease.encode()).hexdigest())
        status = self._status()
        started = self.wall_clock()
        result = self._effect('background_begin',family=plan['family'],parameters=plan['owner_parameters'],
            identity=self.grant.identity,expected_generation=status['generation'],
            expected_operating_mode_revision=(status.get('operating_mode') or {}).get('revision'),
            poll_interval_s=.1,handoff_timeout_s=plan['approved_plan']['handoff_timeout_s'])
        self.epoch = result.get('epoch')
        if not self.epoch or self.epoch.get('identity') != self.grant.identity:
            self.write_unknown=True
            raise DeviceError('Owner did not confirm the measurement epoch identity')
        if plan['family']=='pi':
            result=self._effect('background_pid_target',token=self.epoch['token'],pid=pid_from_candidate(plan['candidate']))
        else:
            result=self._effect('background_parameters_target',token=self.epoch['token'])
        end=self.clock()+plan.get('runtime_timeout_s',300.)
        while True:
            self.check()
            if self.cancel() or self.clock()>=end:
                raise DeviceError('Parameter handover interrupted; no mutation will be replayed')
            actual=self._actual(plan['family'])
            if same({k:actual.get(k) for k in plan['candidate']},plan['candidate']):break
            sample();self.sleep(.1)
        self.confirmed_application = True
        return dict(status='applied',mode=plan['actual_mode'],requested_mode=plan['mode'],
            readback={c['parameter']:actual[c['parameter']] for c in plan['changes']},owner_result=result,
            started_unix_s=started,completed_unix_s=self.wall_clock(),
            timestamp_kind='host_request_and_owner_verified_readback',runtime_only=True)

    def finish(self, result):
        """Keep a confirmed requested setting; revoke on interruption, never blind rollback."""
        try:
            if not self.epoch:return
            if self.write_unknown or result.get('physical_outcome')=='unknown':
                return
            status=self._status();epoch=status.get('epoch')
            if not epoch or epoch.get('token')!=self.epoch['token'] or epoch.get('state')!='active':return
            # Measurement insufficiency does not undo a successfully applied operator setting.
            if result.get('status') not in ('succeeded','measurement_incomplete'):
                result['parameter_handoff']=self._effect('background_restore',token=self.epoch['token'],
                    reason='measurement interrupted after confirmed application')
                return
            if self.plan['family']=='pi':
                receipt=self._effect('background_pid_finish',token=self.epoch['token'],status='succeeded')
            else:
                receipt=self._effect('background_adopt',token=self.epoch['token'])
            last=receipt.get('last') or {}
            if receipt.get('epoch') is not None or last.get('token')!=self.epoch['token'] or last.get('state')!='adopted':
                self.write_unknown=True
                raise DeviceError('Runtime parameter completion lacks owner-confirmed retention')
            result['parameter_completion']=receipt
            self.epoch=None
        finally:
            if self.grant:self.grant.close();self.grant=None
