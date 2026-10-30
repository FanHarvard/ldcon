"""Window-centre search and hops (2026-10-27, sr422 lock 5907ecf5, 21:05-21:35 EDT).

Observed: the fast side-peak search stepped the current down, the laser hopped at the branch edge, the lock's mapped
recovery restored the branch, and ``window_restudy_after_recovery`` restarted the search from scratch -> the same
descent -> the same hop, ten times. Required (rule F: the card is the procedure; rule K: a hop is an operating-
condition fact): a confirmed hop during a window step IS the boundary on that side (kind ``hop``), the search resumes
after the re-arm with its boundaries and turns to the other side, a confirmed threshold boundary is never stepped
toward again, and ``max_hops`` bounds the recoveries one action may cause. No instrument, no deployment.
"""
import math
from types import SimpleNamespace

import pytest

from ldcon.jobs.window_center import WindowCenterPolicy, parameters, DEFAULTS
from ldcon.jobs.ecdl_window_center import WindowCenterSession, same_mode_band_hz

TARGET = 710962838507871.5
ORIGIN = 1.2464
STEP = .0008          # sr422 window-center step (0.8 mV); width 2 V -> step_fraction 0.0004
AXIS = dict(low=0., high=2., max_step=.01, resolution=1e-4, update_interval_s=.1, max_slew_per_s=1.)


class BranchLock:
    """Fast-search lock fake: the branch ends below ``edge_low`` (a hop: -19 GHz, side peak high, single mode lost);
    above ``threshold_high`` the spectrum exceeds the side-peak limit without a hop (a threshold boundary); an optional
    ``edge_high`` hops on the high side too. ``recover`` is the lock's mapped recovery (restores ORIGIN on-branch)."""
    def __init__(self, *, edge_low=1.2380, threshold_high=1.2560, edge_high=None):
        self.t = 100.
        self.current = ORIGIN
        self.pi_active = True
        self.uncertain_output = False
        self.edge_low, self.threshold_high, self.edge_high = edge_low, threshold_high, edge_high
        self.moves = []
        self.recoveries = 0
        self.axis = SimpleNamespace(**AXIS)
        self.session = SimpleNamespace(device={'control_settings': {'steady_lock': {
            'capture_hz': 1e8, 'dither': {'window_s': .2, 'frequency_hz': 10., 'delivery_delay_s': 0.}}}})
        self.off_branch_reads = 0

    def clock(self):
        return self.t

    def wall_clock(self):
        return 100000.+self.t

    @property
    def hopped(self):
        return self.current < self.edge_low or (self.edge_high is not None and self.current > self.edge_high)

    def recovery_ports(self):
        return {'current': SimpleNamespace(policy=self.axis)}

    def response_info(self):
        self.t += .01
        if self.hopped:
            self.off_branch_reads += 1
            frequency = TARGET-1.9e10
            metrics = {'side_peak_height_max': .8}
            single_mode = False
        else:
            frequency = TARGET+(1e6 if int(self.t*100) % 2 else -1e6)
            metrics = {'side_peak_height_max': .5 if self.current > self.threshold_high else .2}
            single_mode = True
        return {'outputs': {'current': self.current, 'temperature': .0947, 'piezo': 29000.},
                'native_pi_active': self.pi_active,
                'observation': {'frequency': {'frequency_hz': frequency, 'acquired_s': self.t,
                                              'acquired_unix_s': self.wall_clock(), 'quality': 'valid'},
                                'mode_id': 'unclassified', 'single_mode': single_mode, 'metrics': metrics,
                                'maintenance_required': False}}

    def response_begin(self):
        pass

    def response_end(self):
        pass

    def response_current(self, value):
        assert self.pi_active and 0 <= value <= 2 and abs(value-self.current) <= AXIS['max_step']+1e-9
        self.current = round(value, 6)
        self.moves.append(self.current)
        return {'applied': True, 'value': self.current}

    def recover(self):
        """The lock's tested procedure: hold, restore the reference outputs, verify the branch, re-arm."""
        self.recoveries += 1
        self.moves_at_recovery = len(self.moves)
        self.current = ORIGIN
        self.off_branch_reads = 0
        self.t += 75.


def fast_parameters(**overrides):
    p = dict(measure_dither=False, require_single_mode=False, step_fraction=.0004, max_span_fraction=.02, max_steps=300, timeout_s=3600.,
             settle_s=.3, observables=['metrics.side_peak_height_max'],
             boundary_limits={'metrics.side_peak_height_max': .35}, initial_interior_search=True)
    p.update(overrides)
    return p


def run(lock, session, *, cycles=400, lock_probe_after=3):
    """The steady worker's cycle: poll the window, then the lock's hop probe (confirm_samples=3) and the recovery
    hooks (before_recovery -> recover -> after_recovery)."""
    for _ in range(cycles):
        lock.t += .35
        session.poll(lock)
        if session.status != 'running':
            break
        if session.needs_recovery or lock.off_branch_reads >= lock_probe_after:
            session.before_recovery()
            lock.recover()
            session.after_recovery()
    return session


def make_session(lock, events, p, steady=None, seed=None):
    return WindowCenterSession({'request_id': 'window-1', 'card_id': 'window-center'}, p, lock, seed or {},
                               lambda kind, payload: events.append((kind, payload)), lambda value: None,
                               steady if steady is not None else {'target_frequency_hz': TARGET},
                               clock=lock.clock, wall_clock=lock.wall_clock)


# --- 1. hop on the low side -> hop boundary -> recovery -> resume on the high side -> centre -------------------------

def test_hop_is_the_low_boundary_and_the_search_resumes_on_the_high_side():
    lock = BranchLock(); events = []
    session = run(lock, make_session(lock, events, fast_parameters()))
    kinds = [k for k, _ in events]
    assert session.status == 'succeeded', session.result
    assert lock.recoveries == 1 and 'window_restudy_after_recovery' not in kinds
    hop = [p for k, p in events if k == 'window_hop_boundary']
    assert len(hop) == 1 and hop[0]['hop_count'] == 1 and hop[0]['max_hops'] == 2 and not hop[0]['budget_exhausted']
    assert hop[0]['hop']['kind'] == 'hop' and hop[0]['hop']['side'] == 'low' and hop[0]['hop']['source'] == 'policy'
    # interval = last on-branch current ... first off-branch current (0.8 mV step across the branch edge at 1.2380)
    assert hop[0]['hop']['interval'] == pytest.approx([1.2376, 1.2384], abs=1e-9)
    assert hop[0]['hop']['frequency_hz'] == pytest.approx(TARGET-1.9e10)
    # the hop boundary was recorded BEFORE the recovery ran
    assert kinds.index('window_hop_boundary') < kinds.index('window_search_resumed_after_recovery')
    resumed = [p for k, p in events if k == 'window_search_resumed_after_recovery'][0]
    assert resumed['next_side'] == 'high' and resumed['policy']['boundaries']['low']['kind'] == 'hop'
    # after the recovery every move went up from the restored point: never toward the known low side again
    after = lock.moves[lock.moves_at_recovery:]
    assert all(v > 1.2384 for v in after)
    end = [p for k, p in events if k == 'window_center_end'][0]
    assert end['boundaries']['low'] == dict(interval=pytest.approx([1.2376, 1.2384], abs=1e-9), kind='hop')
    assert end['boundaries']['high']['kind'] == 'threshold' and end['boundaries']['high']['interval'][0] >= 1.2560
    assert end['hop_count'] == 1 and len(end['hops']) == 1
    assert end['center_definition'] == 'branch_limited_window'
    low, high = end['boundaries']['low']['interval'], end['boundaries']['high']['interval']
    assert end['center'] == pytest.approx((sum(low)/2+sum(high)/2)/2, abs=1e-9)
    assert end['center_verified'] is True if 'center_verified' in end else end['state'] == 'complete'
    assert lock.current == pytest.approx(end['center'], abs=1e-9)


def test_lock_confirmed_hop_without_policy_detection_is_still_the_boundary():
    """The policy has no same-mode band and the mode id never changes (sr422 'unclassified'): the policy accepts the
    hop frame as a side-peak threshold boundary (as on the hardware) and travels back; the lock's probe then confirms
    the hop and before_recovery re-kinds that just-accepted boundary as the hop (same interval)."""
    lock = BranchLock(); events = []
    steady = {'target_frequency_hz': TARGET}
    lock.session.device['control_settings']['steady_lock'].pop('capture_hz')   # no band from the profile either
    session = make_session(lock, events, fast_parameters(), steady=steady)
    assert session.p['hop_threshold_hz'] is None
    run(lock, session)
    assert session.status == 'succeeded' and lock.recoveries == 1
    hop = [p for k, p in events if k == 'window_hop_boundary'][0]
    assert hop['source'] == 'lock' and hop['hop']['side'] == 'low' and hop['hop']['kind'] == 'hop'
    assert hop['hop']['interval'] == pytest.approx([1.2376, 1.2384], abs=1e-9) and hop['hop']['state'] == 'return_origin'
    end = events[-1][1]
    assert end['boundaries']['low']['kind'] == 'hop' and end['boundaries']['high']['kind'] == 'threshold'
    assert end['center'] is not None


# --- 2. a confirmed threshold boundary stops the descent on that side ---------------------------------------------

class Axis(SimpleNamespace):
    pass


def row(current, side_peak, t, *, frequency=TARGET, mode_id='unclassified', single_mode=True):
    return dict(outputs=dict(current=current, temperature=.0947, piezo=29000.), acquired_s=t, acquired_unix_s=100000.+t,
                observation=dict(frequency=dict(frequency_hz=frequency, acquired_s=t, acquired_unix_s=100000.+t),
                                 mode_id=mode_id, single_mode=single_mode, metrics={'side_peak_height_max': side_peak}))


def drive(policy, current, side_peak, t, **kw):
    """One settled observation at ``current``; returns (state, next current)."""
    state = policy.tick(t, row(current, side_peak, t, **kw))
    action = state.get('action')
    if action:
        policy.acknowledge(action['value'], now=t)
        return state, round(action['value'], 6)
    return state, current


def test_confirmed_threshold_boundary_is_never_stepped_toward_again():
    """Hardware 21:07: after the low boundary the origin looked bad; seek_interior walked down again through the
    boundary. Rule: a side with a measured boundary (threshold or hop) is never stepped toward again; the interior
    search turns to the other side at once."""
    p = parameters(dict(parameters=fast_parameters(target_frequency_hz=TARGET, hop_threshold_hz=1e9)), {})
    policy = WindowCenterPolicy(p, dict(current=ORIGIN, temperature=.0947, piezo=29000.), Axis(**AXIS), 0.)
    t, current, proposals = 0., ORIGIN, []
    bad_origin = False
    for _ in range(200):
        t += .5
        side_peak = .5 if current <= 1.2384 or current >= 1.2560 or (bad_origin and abs(current-ORIGIN) < 1e-9) else .2
        state, nxt = drive(policy, current, side_peak, t)
        if state.get('action'):
            proposals.append((state['state'], nxt, state['action']['reason']))
        if 'low' in state['boundaries']:
            bad_origin = True          # the origin is degraded after the first boundary (as on the hardware)
        current = nxt
        if state.get('done'):
            break
    assert state['state'] == 'complete' and state['done']
    low = state['boundaries']['low']
    assert low == dict(interval=pytest.approx([1.2384, 1.2392], abs=1e-9), kind='threshold')
    first_low = next(i for i, (s, v, r) in enumerate(proposals) if s == 'return_origin')
    # after the low boundary was accepted no proposal went below its inner end (1.2392)
    assert all(v >= 1.2392-1e-9 for s, v, r in proposals[first_low:])
    # the interior search turned UP immediately (the low side is known), not down from the origin
    interior = [(s, v, r) for s, v, r in proposals if s == 'seek_interior']
    assert interior and interior[0][1] > ORIGIN and interior[0][2] == 'seek initial interior above a lower owner boundary'
    assert state['boundaries']['high']['kind'] == 'threshold'
    high = state['boundaries']['high']['interval']
    assert state['center'] == pytest.approx((sum(low['interval'])/2+sum(high)/2)/2, abs=1e-9)


def test_seek_edge_toward_a_known_side_turns_to_the_other_side():
    p = parameters(dict(parameters=fast_parameters(target_frequency_hz=TARGET, hop_threshold_hz=1e9)), {})
    policy = WindowCenterPolicy(p, dict(current=ORIGIN, temperature=.0947, piezo=29000.), Axis(**AXIS), 0.)
    policy._set_edge(-1, [1.2376, 1.2384], 'hop')
    state = policy.tick(1., row(ORIGIN, .2, 1.))
    assert state['action']['value'] > ORIGIN and policy.direction == 1
    assert state['action']['reason'] == 'boundary on this side already measured; seek the other boundary'
    assert state['boundaries']['low']['kind'] == 'hop'


def test_no_proposal_crosses_a_measured_boundary():
    p = parameters(dict(parameters=fast_parameters(target_frequency_hz=TARGET, hop_threshold_hz=1e9)), {})
    policy = WindowCenterPolicy(p, dict(current=ORIGIN, temperature=.0947, piezo=29000.), Axis(**AXIS), 0.)
    policy._set_edge(-1, [1.2440, 1.2448], 'threshold')
    state = policy._propose(1., ORIGIN, 1.2456, 'same-mode boundary traversal')      # a probing step stays inside
    assert state['action']['value'] == pytest.approx(1.2456) and state['action']['side'] == -1
    policy.pending = None
    state = policy._propose(1., 1.2448, 1.2430, 'measured operating-window midpoint')  # even one small step cannot cross
    assert state['state'] == 'unresolved' and state['warning'] == 'proposal would cross a measured threshold boundary'


# --- 3. hop budget ------------------------------------------------------------------------------------------------

def test_hop_budget_zero_ends_the_action_at_the_restored_point_with_the_hop_boundary():
    lock = BranchLock(); events = []
    session = run(lock, make_session(lock, events, fast_parameters(max_hops=0)))
    kinds = [k for k, _ in events]
    assert session.status == 'failed' and session.result['reason'] == 'hop budget exhausted'
    assert session.result['done'] is False and session.result['hop_budget_exhausted'] is True and session.result['max_hops'] == 0
    assert session.result['boundaries']['low']['kind'] == 'hop' and 'high' not in session.result['boundaries']
    assert session.result['center'] is None
    assert 'window_hop_budget_exhausted' in kinds and 'window_search_resumed_after_recovery' not in kinds
    assert kinds[-1] == 'window_center_end' and events[-1][1]['reason'] == 'hop budget exhausted'
    # the lock stays locked at the restored point: no window move after the recovery, no return move
    assert lock.recoveries == 1 and lock.current == ORIGIN and lock.pi_active
    assert session.result['return_outcome']['status'] == 'skipped'
    assert len(lock.moves) == lock.moves_at_recovery      # the recovery was the last output change


def test_hop_budget_counts_every_recovery_of_the_action_and_keeps_both_hop_boundaries():
    """Both branch edges hop (low at 1.2380, high at 1.2500): max_hops=1 allows the first recovery; the second hop
    exhausts the budget. Both boundaries (kind hop) and the centre between them are in the end payload."""
    lock = BranchLock(edge_high=1.2500); events = []
    session = run(lock, make_session(lock, events, fast_parameters(max_hops=1)))
    assert lock.recoveries == 2 and session.status == 'failed' and session.result['reason'] == 'hop budget exhausted'
    hops = [p for k, p in events if k == 'window_hop_boundary']
    assert [h['hop']['side'] for h in hops] == ['low', 'high'] and hops[1]['budget_exhausted'] and not hops[0]['budget_exhausted']
    end = events[-1][1]
    assert end['boundaries']['low']['kind'] == 'hop' and end['boundaries']['high']['kind'] == 'hop'
    assert end['boundaries']['high']['interval'] == pytest.approx([1.2496, 1.2504], abs=1e-9)
    assert end['center'] == pytest.approx((1.2380+1.2500)/2, abs=1e-9) and end['done'] is False
    assert end['hop_count'] == 2 and lock.current == ORIGIN


def test_max_hops_parameter_validation_and_default():
    assert DEFAULTS['max_hops'] == 2
    assert parameters(dict(parameters={}), {})['max_hops'] == 2
    assert parameters(dict(parameters={'max_hops': 0}), {})['max_hops'] == 0
    for bad in (-1, 2.0, '2', 101):
        with pytest.raises(ValueError, match='Invalid max_hops'):
            parameters(dict(parameters={'max_hops': bad}), {})


# --- 4. same-mode band fallback (why the policy could not see the -19 GHz hop) -------------------------------------

def test_same_mode_band_uses_current_recovery_or_capture_not_retired_artifacts():
    profile = {'steady_lock': {'capture_hz': 1e8}}
    assert same_mode_band_hz({'recovery': {'hop_threshold_hz': 5e7}}, profile) == 5e7
    assert same_mode_band_hz({'recovery': None}, profile) == 1e8
    assert same_mode_band_hz({}, {'steady_lock': {}}) is None
    for key in ('recovery_artifact', 'recovery_ref'):
        for value in (None, {'verification': {'hop_threshold_hz': 5e9}}):
            with pytest.raises(ValueError, match='Legacy recovery artifacts'):
                same_mode_band_hz({'recovery': None, key: value}, profile)
    lock = BranchLock()
    session = make_session(lock, [], fast_parameters(), steady={'target_frequency_hz': TARGET,
                       'recovery': {'hop_threshold_hz': 5e9}})
    assert session.p['hop_threshold_hz'] == 5e9


# --- 5. standard (dither) search: a hop during a step keeps the boundaries (no invalidation) -----------------------

def test_standard_search_hop_during_a_step_is_a_hop_boundary_not_an_invalidation():
    p = parameters(dict(parameters=dict(step_fraction=.0004, max_span_fraction=.02, settle_s=.3, observables=['dither_response'],
                                        boundary_limits={'dither_response': 1.}, target_frequency_hz=TARGET, hop_threshold_hz=1e9)), {})
    policy = WindowCenterPolicy(p, dict(current=ORIGIN, temperature=.0947, piezo=29000.), Axis(**AXIS), 0.)
    t, current = 0., ORIGIN
    for _ in range(3):
        t += .5
        r = row(current, .2, t); r['dither_response'] = .1
        state = policy.tick(t, r)
        policy.acknowledge(state['action']['value'], now=t); current = round(state['action']['value'], 6)
    t += .5
    r = row(current, .2, t, frequency=TARGET-1.9e10); r['dither_response'] = .1
    state = policy.tick(t, r)
    assert state['recovery_required'] and state['state'] == 'recovery_required' and policy.resumable
    assert state['boundaries']['low'] == dict(interval=pytest.approx([current, current+STEP], abs=1e-9), kind='hop')
    assert policy.suspend_for_recovery(current, reason='lock') is state['hop'] and len(policy.hops) == 1   # no second record
    resumed = policy.resume_after_recovery(t+75.)
    assert resumed['resumed'] and resumed['next_side'] == 'high' and policy.state == 'return_origin'
    t += 76.
    r = row(ORIGIN, .2, t); r['dither_response'] = .1
    state = policy.tick(t, r)                      # restored point adopted without 'current changed' invalidation
    assert state['state'] == 'seek_edge' and state['action']['value'] > ORIGIN and policy.direction == 1


def test_restored_point_beyond_a_measured_boundary_contradicts_it():
    p = parameters(dict(parameters=fast_parameters(target_frequency_hz=TARGET, hop_threshold_hz=1e9)), {})
    policy = WindowCenterPolicy(p, dict(current=ORIGIN, temperature=.0947, piezo=29000.), Axis(**AXIS), 0.)
    policy.state = 'seek_edge'; policy.direction = 1; policy.last_direction = 1
    policy._note_on_branch(1.2480)
    policy.suspend_for_recovery(1.2488, reason='lock')
    assert policy.edges[1] == [1.2480, 1.2488]
    policy.resume_after_recovery(1.)
    state = policy.tick(2., row(1.2520, .2, 2.))   # the lock restored a point above the measured high boundary
    assert 1 not in policy.edges and state['contradicted_boundaries'][0]['side'] == 'high'
    assert state['contradicted_boundaries'][0]['restored_current'] == 1.2520
