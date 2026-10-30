"""Independent ECDL PI trials with cycle-boundary, output-continuous handover."""
import copy
import math

from ldcon.core.contracts import DeviceError
from ldcon.owners.ecdl_native_pid import NativePIDTuning
from ldcon.owners.flexible_native_trial import close_pid, pid_from_candidate, pid_from_config
from ldcon.owners.pi_handover import capability, transact


class IndependentPIDTuning(NativePIDTuning):
    completion_mode = 'verified_handover'

    def __init__(self, native, parameters, *, period=None, handoff_timeout_s=None):
        super().__init__(native, parameters, independent=True)
        capability(self.original)
        self.original_pid = pid_from_config(self.original)
        self.candidate_pid = pid_from_candidate(parameters['candidate'])
        for key in ('kp', 'ki'):
            if self.original_pid[key]*self.candidate_pid[key] <= 0:
                raise ValueError('PI candidate must preserve the commissioned feedback sign')
        self._proof(self.candidate_pid)
        self.desired_pid = copy.deepcopy(self.original_pid)
        self.last_receipt = None
        native.discard_proposals('native PI tuning holds current and TEC')
        self.o.audit('independent_pi_admitted', trial=self.id, candidate=parameters['candidate'],
            completion_mode=self.completion_mode, transition_max_count=None,
            optical_settle_required_for_handoff=False, persistent=False)

    def _proof(self, pid):
        cfg = self.expected
        p = [pid['kp']*(cfg[k]-cfg['target']) for k in ('system_min','system_max')]
        policy = self.o.policies()['piezo']
        lo = max(0., policy.low, self.original['dac_min_count'])
        hi = min(65535., policy.high, self.original['dac_max_count'])
        # Existence of an offset is necessary; the firmware also proves that the
        # CURRENT integer DAC can be reproduced within these same bounds.
        if not math.isfinite(max(p)-min(p)+2*pid['integral_limit_count']) or max(p)-min(p)+2*pid['integral_limit_count'] > hi-lo:
            raise ValueError('PI candidate exceeds the physical PZT output envelope')

    def at_checkpoint(self):
        return close_pid(self.desired_pid, self.original_pid)

    def target(self, pid):
        if self.closed:
            raise ValueError('PI trial is closed')
        if not close_pid(pid, self.original_pid) and not close_pid(pid, self.candidate_pid):
            raise ValueError('Only the admitted independent candidate or its checkpoint is allowed')
        self._proof(pid)
        if not close_pid(pid_from_config(self.expected), pid):
            actual, receipt = transact(self.n, self.expected, pid)
            self.expected = {k:actual[k] for k in self.expected}
            self.last_receipt = receipt
            self.changed_at = self.o.clock()
            self.steps += 1
            self.raw_bound = max(abs(self.expected[k]) for k in ('pid_integral_min','pid_integral_max'))
        self.desired_pid = copy.deepcopy(pid)
        return self.status()

    def ready(self):
        # Ready means the atomic coefficients/output receipt is verified. The
        # comparison worker owns optical observation/settling, never handoff.
        return close_pid(pid_from_config(self.expected), self.desired_pid)

    def advance(self, state, obs):
        for key, value in self.expected.items():
            if not math.isclose(state['servo_config'][key], value, rel_tol=1e-8, abs_tol=1e-12):
                raise DeviceError('External native PID configuration change: '+key)

    def status(self):
        pid = pid_from_config(self.expected)
        return dict(ready=self.ready(), pid=pid, target=copy.deepcopy(self.desired_pid),
            trial=self.id, steps=self.steps, actual_native=copy.deepcopy(self.expected),
            integral_limit_count=pid['integral_limit_count'], last_handover=copy.deepcopy(self.last_receipt),
            config=dict(pid=pid, output_unit='count', measurement_unit='nm', resolution=1.,
                max_sample_gap_s=self.max_gap, transition_max_count=None,
                raw_integral_bounds=[self.expected['pid_integral_min'],self.expected['pid_integral_max']],
                validated_candidates=True, integral_bounds_adjustable=True, raw_integral_bounds_fixed=False,
                completion_mode=self.completion_mode, optical_settle_required_for_handoff=False))

    def save(self, pid):
        raise DeviceError('Independent background PI is runtime-only')

    def finish(self, status):
        if not self.ready() or (status != 'succeeded' and not self.at_checkpoint()):
            raise DeviceError('Independent PI must finish or return to its checkpoint')
        profile = copy.deepcopy(self.original_profile)
        profile['p_sample'] *= self.expected['pid_kp']/self.original['pid_kp']
        profile['g_sample'] *= self.expected['pid_ki']/self.original['pid_ki']
        profile['integral_limit_count'] = pid_from_config(self.expected)['integral_limit_count']
        self.n.p = profile
        self.closed = True
        self.n.discard_proposals('independent PI ended')
        self.o.audit('independent_pi_completed', status=status, saved=False, **self.status())
        return self.status()
