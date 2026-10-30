"""Owner-only independent gain transitions with fixed raw-I and no reset.

Every transition is re-proved against the authorized joint envelope. Each write
changes one gain by at most 0.1 unquantized DAC count over the entire accepted
wavelength/raw-integral interval, and requires a new qualified acquisition.
"""
import copy
import math
import time

from ldcon.owners.esp32_native_owner import acceptance_bounds, channel_metadata, envelope, number
from ldcon.core.instrument_client import BrokerError


def gain_limits(policy):
    n = policy['native']
    return n.get('optimization_bounds', dict(kp_abs_max=abs(n['kp_count_per_nm']),
                                               ki_abs_max=abs(n['ki_count_per_nm_s'])))


def validate_gains(policy, pid):
    if not isinstance(pid, dict) or set(pid) != {'kp', 'ki', 'kd'} or pid['kd'] != 0:
        raise ValueError('Native tuning requires finite signed P/I and D=0')
    for key, field in (('kp', 'kp_count_per_nm'), ('ki', 'ki_count_per_nm_s')):
        value = number(pid[key], key)
        if value*policy['native'][field] <= 0 or abs(value) > gain_limits(policy)[key+'_abs_max']:
            raise ValueError('Native '+key+' reverses sign or exceeds its reviewed gain ceiling')


def effective_policy(policy, saved):
    p = copy.deepcopy(policy)
    if saved is None:
        return p
    n = p['native']
    pid = saved['pid']
    validate_gains(policy, pid)
    reference = n['ki_count_per_nm_s']
    if 'policy_integral_limit_count' in saved or 'raw_integral_reference_ki' in saved:
        requested = number(saved.get('policy_integral_limit_count'), 'saved requested integral limit', positive=True)
        reference = number(saved.get('raw_integral_reference_ki'), 'saved raw integral reference gain')
        if reference * n['ki_count_per_nm_s'] <= 0 or not math.isclose(
                requested, n['integral_limit_count'], rel_tol=1e-9, abs_tol=1e-12):
            raise ValueError('Persisted integral output-limit reference changed')
    raw = n['integral_limit_count']/abs(reference)
    if not math.isclose(number(saved['raw_integral_limit'], 'saved raw integral limit', positive=True), raw, rel_tol=1e-9):
        raise ValueError('Persisted raw integral limit changed')
    n.update(kp_count_per_nm=pid['kp'], ki_count_per_nm_s=pid['ki'],
             integral_limit_count=raw*abs(pid['ki']))
    return p


def saved_integral_limit(policy, pid):
    """Bind a new output limit to the retained gains, without writing coefficients."""
    validate_gains(policy, pid)
    saved = dict(pid=copy.deepcopy(pid),
                 raw_integral_limit=policy['native']['integral_limit_count'] / abs(pid['ki']),
                 policy_integral_limit_count=policy['native']['integral_limit_count'],
                 raw_integral_reference_ki=pid['ki'], saved_unix_s=time.time(),
                 source='integral output-limit policy update; coefficients retained')
    effective_policy(policy, saved)
    return saved


def pid_from(cfg):
    return {key: cfg['pid_'+key] for key in ('kp', 'ki', 'kd')}


def config(owner, cfg):
    quantum = 1 << (16-channel_metadata(owner.policy['dac'])['bits'])
    return dict(pid=pid_from(cfg), resolution=float(quantum), max_sample_gap_s=3.,
                adaptive_independent=True, optimization_bounds=gain_limits(owner.policy),
                raw_integral_bounds=[cfg['pid_integral_min'], cfg['pid_integral_max']],
                policy_digest=owner.definition, response_evidence=owner.policy['calibration']['evidence'])


def candidate_proof(owner, cfg, pid, *, authority_reference=None):
    """Pure calculation; a rejected proposal performs no device I/O or write."""
    validate_gains(owner.policy, pid)
    raw = max(abs(cfg['pid_integral_min']), abs(cfg['pid_integral_max']))
    candidate = copy.deepcopy(owner.policy)
    candidate['native'].update(kp_count_per_nm=pid['kp'], ki_count_per_nm_s=pid['ki'],
                               integral_limit_count=raw*abs(pid['ki']))
    proof = envelope(candidate, cfg['target'], cfg['control_offset'])
    owner._check_envelope(proof)
    if authority_reference is not None:
        # Preserve usable integral correction at zero wavelength error, with
        # 10% raw-I reserve. Wide receiver-range P is NOT steady authority.
        # Quantization is firmware truncation, then the effective DAC shift.
        shift = 16-channel_metadata(owner.policy['dac'])['bits']
        p = owner.policy['preserved']
        ff = [p['multiplier_control']*(x-p['baseline_control']) for x in (p['mod_valid_min'], p['mod_valid_max'])]
        def reachable(gain, offset):
            radius = .9*raw*abs(gain)
            return [math.floor(offset-radius) >> shift, math.floor(offset+radius) >> shift]
        original = [reachable(authority_reference['ki'], cfg['control_offset']+x) for x in ff]
        proposed = [reachable(pid['ki'], cfg['control_offset']+x) for x in ff]
        proof['quantized_integral_authority'] = dict(reference=original, candidate=proposed,
                                                     usable_raw_fraction=.9, error_nm=0.)
        # Endpoint quantization alone cannot prove every intermediate ADC phase
        # when feedforward varies. A nondecreasing radius proves all phases;
        # reductions require a fixed feedforward offset for this strategy.
        if ff[0] != ff[1] and abs(pid['ki']) < abs(authority_reference['ki']):
            raise ValueError('Varying feedforward requires nondecreasing integral authority; interior quantization is not proved for a reduction')
        if any(c[0] > a[0] or c[1] < a[1] for a,c in zip(original,proposed)):
            raise ValueError('Candidate loses quantized integral correction authority with 10% reserve')
    return proof


class NativePITuning:
    def __init__(self, owner, before, strategy='factor_reduction'):
        self.owner = owner
        self.original = copy.deepcopy(before['servo_config'])
        self.target = pid_from(self.original)
        self.changed = []
        self.started = time.time()
        self.last_acquisition = None
        self.saved = False
        self.strategy = strategy

    def state(self):
        cfg = self.owner.expected_config or self.original
        pid = pid_from(cfg)
        ready = all(math.isclose(pid[k], self.target[k], rel_tol=1e-10, abs_tol=1e-12) for k in pid)
        return dict(pid=pid, target=copy.deepcopy(self.target), ready=ready,
                    config=config(self.owner, cfg), changed_steps=len(self.changed), saved=self.saved,
                    strategy=self.strategy)

    def set_target(self, pid):
        try:
            validate_gains(self.owner.policy, pid)
            if self.strategy == 'factor_reduction':
                ratios = [pid[k]/self.original['pid_'+k] for k in ('kp', 'ki')]
                if not 0 < ratios[0] <= 1 or not math.isclose(*ratios, rel_tol=1e-9):
                    raise ValueError('Legacy trial must scale both original gains by the same factor at most one')
            candidate_proof(self.owner, self.original, pid,
                authority_reference=pid_from(self.original) if self.strategy == 'adaptive_coordinate' else None)
        except (ValueError, BrokerError) as exc:
            raise BrokerError('range', str(exc)) from exc
        self.target = copy.deepcopy(pid)
        return self.state()

    def step(self):
        owner = self.owner
        if self.state()['ready']:
            return self.state()
        before = owner._read_before(require_input=True)
        cfg = before['servo_config']
        # A comparison dropout never resets integration or replays a proposal.
        stamp = owner.last_optical
        if (stamp == self.last_acquisition
                or owner.clock()-owner.last_write < owner.policy['update_interval_s']):
            raise BrokerError('measurement', 'Fresh acquisition and native gain update interval are required')
        for key in ('pid_integral_min', 'pid_integral_max', 'target', 'control_offset',
                    'system_min', 'system_max', 'pid_multiplier'):
            if cfg[key] != self.original[key]:
                raise owner._latch('Native PID reference/raw-integral bounds changed during tuning')
        raw = max(abs(cfg['pid_integral_min']), abs(cfg['pid_integral_max']))
        wave = acceptance_bounds(owner.policy['native'], 299792458e9/cfg['target'])
        errors = max(abs(x-cfg['target']) for x in wave)
        # Safe endpoints can have an unsafe mixed intermediate. Release
        # contribution budget before increasing another independent gain.
        keys = sorted((('kp', errors), ('ki', raw)),
                      key=lambda item: abs(self.target[item[0]]) > abs(cfg['pid_'+item[0]]))
        for key, sensitivity in keys:
            previous, desired = cfg['pid_'+key], self.target[key]
            if math.isclose(previous, desired, rel_tol=1e-10, abs_tol=1e-12):
                continue
            limit = .1/max(sensitivity, 1e-30)
            value = previous + max(-limit, min(limit, desired-previous))
            candidate = {**pid_from(cfg), key:value}
            candidate_proof(owner, cfg, candidate)
            owner._write({'pid_'+key: value}, before, 'bounded native PID gain transition')
            self.last_acquisition = stamp
            self.changed.append(dict(key=key, before=previous, after=value,
                                     maximum_delta_count=abs(value-previous)*sensitivity))
            break
        return self.state()

    def save(self, pid):
        state = self.state()
        if not state['ready'] or pid != state['pid']:
            raise BrokerError('busy', 'Native gain transition must complete before saving')
        previous = self.owner.saved_pid
        self.owner.saved_pid = dict(pid=copy.deepcopy(pid),
            raw_integral_limit=max(abs(self.original['pid_integral_min']), abs(self.original['pid_integral_max'])),
            saved_unix_s=time.time(), source='measured online comparison in current lock worker')
        if previous and 'policy_integral_limit_count' in previous:
            for key in ('policy_integral_limit_count', 'raw_integral_reference_ki'):
                self.owner.saved_pid[key] = previous[key]
        effective_policy(self.owner.policy, self.owner.saved_pid)
        try:
            self.owner._persist()
        except OSError as exc:
            raise self.owner._latch('Native accepted-gain persistence failed: '+str(exc)) from exc
        self.saved = True
        return self.state()

    def finish(self, status):
        if status not in ('succeeded', 'unchanged', 'failed', 'cancelled', 'rejected'):
            raise BrokerError('request', 'Invalid native PID result')
        state = self.state()
        if not state['ready'] or (status not in ('succeeded', 'unchanged') and state['pid'] != pid_from(self.original)):
            raise BrokerError('busy', 'Native restoration must complete before ending comparison')
        self.owner.pid_history.append(dict(started_unix_s=self.started, completed_unix_s=time.time(),
            status=status, original=pid_from(self.original), final=state['pid'], saved=self.saved,
            strategy=self.strategy, changes=self.changed))
        self.owner.pid_history = self.owner.pid_history[-20:]
        try:
            self.owner._persist()
        except OSError as exc:
            raise self.owner._latch('Native PID evidence persistence failed: '+str(exc)) from exc
        self.owner.pid_tuning = None
        return state


def dispatch(owner, request):
    if not owner.active or not owner.prepared:
        raise BrokerError('request', 'Native PID comparison requires the existing active lease')
    action = request['action']
    if action == 'pid_check':
        try:
            cfg = owner.expected_config
            reference = pid_from(owner.pid_tuning.original) if owner.pid_tuning else pid_from(cfg)
            proof = candidate_proof(owner, cfg, request.get('pid'), authority_reference=reference)
            return dict(admissible=True, proof=proof)
        except (ValueError, TypeError, KeyError, BrokerError) as exc:
            return dict(admissible=False, reason=str(exc))
    if action == 'pid_state':
        if owner.pid_tuning:
            return owner.pid_tuning.state()
        cfg = owner.expected_config
        return dict(pid=pid_from(cfg), target=pid_from(cfg), ready=True, config=config(owner, cfg))
    if action == 'pid_begin':
        if owner.pid_tuning:
            raise BrokerError('busy', 'A native PID comparison is already active')
        strategy = request.get('strategy', 'factor_reduction')
        if strategy not in ('factor_reduction', 'adaptive_coordinate'):
            raise BrokerError('request', 'Unknown native optimization strategy')
        before = owner._read_before(require_input=True)
        owner.pid_tuning = NativePITuning(owner, before, strategy)
        return owner.pid_tuning.state()
    session = owner.pid_tuning
    if session is None:
        raise BrokerError('request', 'Begin a native PID comparison first')
    if action == 'pid_target':
        return session.set_target(request.get('pid'))
    if action == 'pid_step':
        return session.step()
    if action == 'pid_save':
        return session.save(request.get('pid'))
    return session.finish(request.get('status'))
