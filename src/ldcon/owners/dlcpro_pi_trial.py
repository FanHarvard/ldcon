"""Attached host-PI epochs; no chassis writes or competing lock lease.

A foreground command restores trial coefficients, without waiting for optical
settling or restoring an old target. Physical envelopes remain Instrument's job.
"""
from copy import deepcopy
import math
import sqlite3
import uuid

from ldcon.core.contracts import DeviceError
from ldcon.core.interface_schema import digest

KEYS = frozenset({'kp', 'ki_per_s', 'integral_limit_v'})


def candidate(value):
    if not isinstance(value, dict) or set(value) != KEYS:
        raise ValueError('PI candidate requires kp, ki_per_s and integral_limit_v')
    if any(type(v) not in (float, int) or not math.isfinite(v) for v in value.values()):
        raise ValueError('PI values must be finite')
    if min(value['kp'], value['ki_per_s']) < 0 or value['integral_limit_v'] <= 0:
        raise ValueError('Normalized gains must be nonnegative and integral limit positive')
    return deepcopy(value)


class PITrial:
    def __init__(self, owner):
        self.owner = owner
        self.active = None
        self.last = None
        self.revision = 0
        self.parameter_trial = None

    def settings(self):
        if self.parameter_trial is not None:
            return self.parameter_trial.settings()
        return {k: self.owner.control[k] for k in KEYS}

    def view(self):
        a = deepcopy(self.active)
        if a: a.pop('token', None)
        return dict(active=a, last=deepcopy(self.last), revision=self.revision,
                    settings=self.settings() if hasattr(self.owner, 'control') else None)

    def begin(self, identity, target_revision, policy_revision, watchdog_s, target_program=None, *,
              expected_operating_mode_revision=None, monitor_grant=None, family='pi'):
        o = self.owner
        o._healthy()
        if o.mode != 'lock' or self.active:
            raise ValueError('PI trial needs a running lock and an empty adjustment slot')
        if target_revision != o.target_revision or policy_revision != digest(o.policy):
            raise ValueError('PI context changed before admission')
        if not isinstance(identity, str) or not identity or len(identity) > 128:
            raise ValueError('A bounded task identity is required')
        if type(watchdog_s) not in (int,float) or not math.isfinite(watchdog_s) or not 1 <= watchdog_s <= 60:
            raise ValueError('PI trial heartbeat deadline must be 1..60 seconds')
        program=deepcopy(target_program or dict(offsets_hz=[],max_cycles=0))
        if set(program)!={'offsets_hz','max_cycles'}:raise ValueError('Invalid target program')
        offsets=program['offsets_hz']
        if (not isinstance(offsets,list) or len(offsets)>1000 or type(program['max_cycles']) is not int
                or not 0<=program['max_cycles']<=12):raise ValueError('Invalid finite target program')
        previous=0.
        for value in offsets:
            if type(value) not in (float,int) or not math.isfinite(value) or o.target+value<=0 or abs(value-previous)>o.control['capture_hz']:
                raise ValueError('Target program increment exceeds capture')
            previous=value
        if offsets and (offsets[-1]!=0 or not program['max_cycles']):raise ValueError('Target program must return to origin')
        if family not in ('pi', 'current', 'slow_loop', 'feedforward'):
            raise ValueError('Unknown DLC adjustment family')
        if family != 'pi' and offsets:
            raise ValueError('Parameter adjustment cannot change the optical target')
        supervision = self._admit_supervision(expected_operating_mode_revision, monitor_grant, offsets, family)
        o.read_control()
        helper = None
        if family != 'pi':
            from ldcon.owners.dlcpro_parameter_trial import ParameterTrialSettings
            helper = ParameterTrialSettings(o, family)
        self.parameter_trial = helper
        token = uuid.uuid4().hex
        self.active = dict(token=token, identity=identity, family=family, checkpoint=self.settings(),
            target_revision=o.target_revision, policy_revision=digest(o.policy),
            watchdog_s=watchdog_s, renewed_s=o.clock(), origin_target_hz=o.target,
            target_program=program, program_index=0, program_cycle=0, **supervision)
        self.revision += 1
        o.audit(dict(event='pi_trial_begin', identity=identity, family=family, checkpoint=self.settings()))
        return dict(token=token, **self.view())

    def require(self, token):
        self.poll(force_supervision=True)
        if not self.active or token != self.active['token']:
            raise ValueError('PI trial is no longer active')
        if self.owner.fault or self.owner.mode != 'lock':
            raise DeviceError('PI lock no longer healthy')
        self.active['renewed_s'] = self.owner.clock()

    def switch(self, values):
        o = self.owner
        if self.parameter_trial is not None:
            started = o.clock()
            receipt = self.parameter_trial.switch(values)
            return self._parameter_receipt(receipt, held_started_s=started)
        values = candidate(values)
        # The last owner-confirmed command and its actual accepted acquisition
        # define this boundary. No slow console read or optical settling is
        # required for interruption. The next new sample uses the new gains.
        if not o.applied_observation or not math.isfinite(o.applied_pzt):
            raise ValueError('No confirmed PI output boundary')
        error = o.applied_error_axis
        integral = max(-values['integral_limit_v'], min(values['integral_limit_v'], o.integral))
        base = o.applied_pzt-values['kp']*error-integral
        if not math.isfinite(base): raise ValueError('PI compensation is nonfinite')
        old = self.settings()
        o.base, o.integral = base, integral
        o.control.update(values)
        self.revision += 1
        receipt = dict(confirmed=True, before=old, readback=self.settings(),
            output_before_v=o.applied_pzt, output_after_v=base+values['kp']*error+integral,
            target_revision=o.target_revision, revision=self.revision,
            acquisition_s=o.applied_observation['acquired_s'], switched_s=o.clock(), output_write=False)
        o.audit(dict(event='pi_switch', **receipt))
        return receipt

    def apply(self, token, expected_revision, values):
        self.require(token)
        if expected_revision != self.revision: raise ValueError('PI revision changed')
        if self.parameter_trial is not None:
            self.parameter_trial.validate(values)
        else:
            candidate(values)
        self.owner.read_control()  # Freshness before accepting a new experiment.
        return self.switch(values)

    def _parameter_receipt(self, receipt, *, held_started_s=None):
        if not isinstance(receipt, dict) or receipt.get('confirmed') is not True:
            raise DeviceError('Parameter change was not confirmed')
        now = self.owner.clock()
        if (receipt.get('output_write') is True or held_started_s is not None
                and now-held_started_s > self.owner.policy['max_sample_age_s']):
            # UDP can continue accumulating while an exclusive FF/current/TEC
            # transaction holds PZT. Its backlog is evidence, not an integral
            # correction to replay at the next feedback update.
            self.owner.integral_resume_after_s = now
            receipt = dict(receipt, integral_resume_after_s=now)
        credit = getattr(self.owner, 'credit_waiting_client', None)
        if credit is not None:credit()
        self.revision += 1
        # A confirmed FF trial updates its runtime expected factor so that the
        # instrument's full-output checks use that factor. Only this correlated
        # own change updates the epoch; external policy changes still preempt.
        if self.active is not None:
            self.active['policy_revision'] = digest(self.owner.policy)
        return dict(receipt, revision=self.revision, target_revision=self.owner.target_revision)

    def _restore_checkpoint(self):
        if self.parameter_trial is not None:
            started = self.owner.clock()
            return self._parameter_receipt(self.parameter_trial.restore(), held_started_s=started)
        return self.switch(self.active['checkpoint'])

    def reset(self, token, expected_revision):
        self.require(token)
        if expected_revision != self.revision:
            raise ValueError('Adjustment revision changed')
        return self._restore_checkpoint()

    def step(self, token, expected_revision, index, cycle):
        self.require(token)
        a=self.active;program=a['target_program'];offsets=program['offsets_hz']
        if (type(index) is not int or type(cycle) is not int or expected_revision!=self.owner.target_revision or index!=a['program_index']
                or cycle!=a['program_cycle'] or cycle>=program['max_cycles'] or not offsets):
            raise ValueError('Target program context changed; no replay')
        from ldcon.owners.dlcpro_target_step import apply
        receipt=apply(self.owner,a['origin_target_hz']+offsets[index])
        a['target_revision']=self.owner.target_revision
        a['program_index']+=1
        if a['program_index']==len(offsets):a['program_index']=0;a['program_cycle']+=1
        self.revision+=1
        return receipt

    def restore_target(self,token):
        self.require(token)
        a=self.active
        if self.owner.target==a['origin_target_hz']:return dict(confirmed=True,unchanged=True,target_revision=self.owner.target_revision)
        from ldcon.owners.dlcpro_target_step import apply
        receipt=apply(self.owner,a['origin_target_hz'])
        a['target_revision']=self.owner.target_revision
        self.revision+=1
        return receipt

    def finish(self, token, adopt=False, reason='completed'):
        self.require(token)
        return self.end(adopt, reason)

    def end(self, adopt, reason):
        if not self.active: return self.last
        previous = self.active
        if adopt:
            receipt = self.parameter_trial.commit() if self.parameter_trial is not None else None
            if self.parameter_trial is not None and (not isinstance(receipt, dict) or receipt.get('confirmed') is not True):
                raise DeviceError('Parameter adoption was not confirmed')
        else:
            receipt = self._restore_checkpoint()
        self.last = dict(identity=previous['identity'], state='adopted' if adopt else 'restored',
            reason=reason, family=previous['family'], checkpoint=previous['checkpoint'], settings=self.settings(),
            origin_target_hz=previous['origin_target_hz'], target_frequency_hz=self.owner.target,
            target_restored=self.owner.target==previous['origin_target_hz'],
            receipt=receipt, confirmed=True, target_revision=self.owner.target_revision)
        self.active = None
        self.parameter_trial = None
        self.owner.audit(dict(event='pi_trial_end', **self.last))
        return deepcopy(self.last)

    def preempt(self, reason):
        return self.end(False, reason) if self.active else None

    def _admit_supervision(self, revision, binding, offsets, family):
        if binding is not None and (revision is None or offsets):
            raise ValueError('Monitor PI trials require owner mode and cannot change the optical target')
        mode = getattr(self.owner, 'operating_mode', None)
        if revision is None and mode is not None:
            revision = mode.authority.status()['revision']
        result = {}
        if revision is not None:
            if mode is None:
                raise ValueError('Current owner operating mode is required for monitored PI')
            mode.authority.require_revision(revision)
            result['operating_mode_revision'] = revision
        if binding is not None:
            from ldcon.agents.dlc_monitor_grant import check_grant
            grant = check_grant(self.owner.laser_id, binding)
            if family not in grant['bounds']['families']:
                raise ValueError('Adjustment family is outside this monitor grant')
            result.update(monitor_grant=deepcopy(binding),
                          monitor_deadline_s=self.owner.clock()+grant['bounds']['timeout_s'])
        return result

    def _supervision_reason(self, force):
        a, o = self.active, self.owner
        if 'operating_mode_revision' not in a and 'monitor_grant' not in a:
            return None
        if o.clock() >= a.get('monitor_deadline_s', math.inf):
            return 'monitor_trial_deadline'
        # Fast servo ticks must not read the authorization file or SQLite on
        # every wavelength sample. Effect/adoption RPCs force a fresh check;
        # mode-change RPCs already preempt synchronously under the owner mutex.
        if not force and o.clock()-a.get('last_supervision_check_s', -math.inf)<.2:
            return None
        a['last_supervision_check_s'] = o.clock()
        mode = getattr(o, 'operating_mode', None)
        try:
            if mode is None or mode.authority.status()['revision'] != a.get('operating_mode_revision'):
                return 'operating_mode_changed'
        except (OSError, ValueError, KeyError, sqlite3.Error):
            return 'operating_mode_unavailable'
        if 'monitor_grant' in a:
            from ldcon.agents.dlc_monitor_grant import check_grant
            try:
                grant = check_grant(o.laser_id, a['monitor_grant'])
                if a['family'] not in grant['bounds']['families']:
                    return 'monitor_grant_revoked'
            except (OSError, ValueError, KeyError, TypeError):
                return 'monitor_grant_revoked'
        return None

    def poll(self, *, force_supervision=False):
        if not self.active: return
        o, a = self.owner, self.active
        if self.parameter_trial is not None and o.fault:
            # An uncertain physical effect is not a revocable trial with a
            # known checkpoint return. Retain the slot and lease for inspection.
            a['inspection_required'] = True
            return
        reason = self._supervision_reason(force_supervision)
        if reason:
            self.preempt(reason)
            return
        if (o.clock()-a['renewed_s'] > a['watchdog_s'] or o.mode != 'lock'
                or o.target_revision != a['target_revision'] or digest(o.policy) != a['policy_revision']):
            self.preempt('watchdog_or_context_change')
