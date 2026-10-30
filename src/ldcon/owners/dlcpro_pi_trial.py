"""Attached host-PI epochs; no chassis writes or competing lock lease.

A foreground command restores trial coefficients, without waiting for optical
settling or restoring an old target. Physical envelopes remain Instrument's job.
"""
from copy import deepcopy
import math
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

    def settings(self):
        return {k: self.owner.control[k] for k in KEYS}

    def view(self):
        a = deepcopy(self.active)
        if a: a.pop('token', None)
        return dict(active=a, last=deepcopy(self.last), revision=self.revision,
                    settings=self.settings() if hasattr(self.owner, 'control') else None)

    def begin(self, identity, target_revision, policy_revision, watchdog_s, target_program=None):
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
        o.read_control()
        token = uuid.uuid4().hex
        self.active = dict(token=token, identity=identity, checkpoint=self.settings(),
            target_revision=o.target_revision, policy_revision=digest(o.policy),
            watchdog_s=watchdog_s, renewed_s=o.clock(), origin_target_hz=o.target,
            target_program=program, program_index=0, program_cycle=0)
        self.revision += 1
        o.audit(dict(event='pi_trial_begin', identity=identity, checkpoint=self.settings()))
        return dict(token=token, **self.view())

    def require(self, token):
        self.poll()
        if not self.active or token != self.active['token']:
            raise ValueError('PI trial is no longer active')
        if self.owner.fault or self.owner.mode != 'lock':
            raise DeviceError('PI lock no longer healthy')
        self.active['renewed_s'] = self.owner.clock()

    def switch(self, values):
        o = self.owner
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
        candidate(values)
        self.owner.read_control()  # Freshness before accepting a new experiment.
        return self.switch(values)

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
        receipt = None if adopt else self.switch(previous['checkpoint'])
        self.last = dict(identity=previous['identity'], state='adopted' if adopt else 'restored',
            reason=reason, checkpoint=previous['checkpoint'], settings=self.settings(),
            origin_target_hz=previous['origin_target_hz'], target_frequency_hz=self.owner.target,
            target_restored=self.owner.target==previous['origin_target_hz'],
            receipt=receipt, confirmed=True, target_revision=self.owner.target_revision)
        self.active = None
        self.owner.audit(dict(event='pi_trial_end', **self.last))
        return deepcopy(self.last)

    def preempt(self, reason):
        return self.end(False, reason) if self.active else None

    def poll(self):
        if not self.active: return
        o, a = self.owner, self.active
        if (o.clock()-a['renewed_s'] > a['watchdog_s'] or o.mode != 'lock'
                or o.target_revision != a['target_revision'] or digest(o.policy) != a['policy_revision']):
            self.preempt('watchdog_or_context_change')
