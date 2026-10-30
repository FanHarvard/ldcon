"""Pure, acknowledged same-mode window centering and early-warning policy.

No instrument I/O occurs here. Call ``tick`` with qualified owner snapshots,
execute its bounded proposal through the owner, then ``acknowledge`` only on a
confirmed successful write. ``now`` and ``acquired_s`` use the owner monotonic
clock; acquisition Unix timestamps identify distinct source samples.
"""
import copy
import math
import statistics

from ldcon.jobs.window_sampling import WindowSamples


DEFAULTS = dict(controller_card_id='steady-lock', mode='center_only',
                position_offset_fraction=0., initial_interior_search=False, require_single_mode=True,
                average_samples=1, average_max_wait_s=10., average_statistic='mean',
                fast_settle_s=None, slow_observables=[],
                observables=['dither_response'], boundary_limits={}, early_limits={},
                urgent_limits={}, soft_quiet_s=0., max_soft_restudies_per_hour=3,
                step_fraction=.0025, max_span_fraction=.1, max_steps=100,
                timeout_s=3600., measurement_max_age_s=5.,
                temperature_tolerance=.0001, seed_ttl_s=3600., settle_s=1.,
                vote_count=3, vote_window=4, position_warning_fraction=.3,
                minimum_monotonicity=.8, model_max_error_fraction=.2,
                target_frequency_hz=None, hop_threshold_hz=None, measure_dither=True, max_hops=2)

# States in which the policy is stepping or travelling along the current axis. A confirmed hop in one of these
# states is the boundary on the side the policy was moving toward (rule K: an operating-condition fact, not a
# failure); the search is suspended for the lock's recovery and resumed with its measured boundaries.
STEPPING_STATES = ('seek_edge', 'seek_interior', 'return_initial', 'return_origin', 'centering', 'slow_boundary',
                   )



def _number(value):
    return type(value) in (float, int) and math.isfinite(value)


def parameters(card, overrides):
    """Validate card settings without embedding a laser's axis or fringe values."""
    base = card.get('parameters', {})
    if not isinstance(base, dict) or not isinstance(overrides, dict):
        raise ValueError('Window center parameters must be mappings')
    if 'search' in base or 'search' in overrides:
        raise ValueError('Window search variants are retired; select observables and measure_dither explicitly')
    if (set(base) | set(overrides)) - set(DEFAULTS):
        raise ValueError('Unknown window center parameter')
    p = copy.deepcopy({**DEFAULTS, **base, **overrides})
    # Measurement selection is independent of traversal policy. Existing callers
    # selecting only scalar metrics do not implicitly request dither acquisition.
    if 'measure_dither' not in base and 'measure_dither' not in overrides:
        p['measure_dither'] = 'dither_response' in p['observables']
    if type(p['measure_dither']) is not bool:
        raise ValueError('Measure dither must be boolean')
    if not isinstance(p['observables'], list):
        raise ValueError('Expected scalar observable names')
    if p['measure_dither']:
        if 'dither_response' not in p['observables']:
            p['observables'].append('dither_response')
    else:
        p['observables'] = [name for name in p['observables'] if name != 'dither_response']
        p['slow_observables'] = []
        p['fast_settle_s'] = None
        for key in ('boundary_limits', 'early_limits', 'urgent_limits'):
            if isinstance(p[key], dict):
                p[key].pop('dither_response', None)
    if type(p['require_single_mode']) is not bool:
        raise ValueError('Require single mode must be boolean')
    if type(p['average_samples']) is not int or not 1 <= p['average_samples'] <= 1000:
        raise ValueError('Invalid average sample count')
    if not _number(p['average_max_wait_s']) or not 0 < p['average_max_wait_s'] <= 3600:
        raise ValueError('Invalid average maximum wait')
    if p['average_statistic'] not in ('mean', 'median'):
        raise ValueError('Unknown average statistic')
    if p['fast_settle_s'] is not None and (not _number(p['fast_settle_s']) or p['fast_settle_s'] <= 0):
        raise ValueError('Invalid fast settling interval')
    slow=p['slow_observables']
    if (not isinstance(slow,list) or not isinstance(p['observables'],list) or
        any(not isinstance(n,str) for n in slow) or len(set(slow))!=len(slow) or
        any(n!='dither_response' or n not in p['observables'] for n in slow)):
        raise ValueError('Slow observables require timestamped dither_response')
    if slow and (p['fast_settle_s'] is None or not set(p['observables'])-set(slow)):
        raise ValueError('Fast/slow mode needs fast settling and at least one fast observable')
    if p['fast_settle_s'] is not None and not slow:
        raise ValueError('Fast settling requires explicit slow observables')
    if type(p['initial_interior_search']) is not bool:
        raise ValueError('Initial interior search must be boolean')
    if p['mode'] not in ('center_only', 'monitor', 'position'):
        raise ValueError('Unknown window center mode')
    if not _number(p['position_offset_fraction']) or abs(p['position_offset_fraction']) > .25:
        raise ValueError('Invalid position offset fraction')
    if p['mode'] != 'position' and p['position_offset_fraction'] != 0:
        raise ValueError('Position offset requires position mode')
    if not isinstance(p['controller_card_id'], str) or not p['controller_card_id']:
        raise ValueError('Controller card is required')
    obs = p['observables']
    if not isinstance(obs, list) or not 1 <= len(obs) <= 32 or any(not isinstance(n, str) or not n for n in obs):
        raise ValueError('Expected scalar observable names')
    for key in ('boundary_limits', 'early_limits', 'urgent_limits'):
        limits = p[key]
        if not isinstance(limits, dict) or any(k not in obs or not _number(v) for k, v in limits.items()):
            raise ValueError('Limits must name selected finite observables')
    for key in ('step_fraction', 'max_span_fraction', 'timeout_s', 'measurement_max_age_s',
                'temperature_tolerance', 'seed_ttl_s', 'settle_s',
                'position_warning_fraction', 'minimum_monotonicity', 'model_max_error_fraction'):
        if not _number(p[key]) or p[key] <= 0:
            raise ValueError('Invalid '+key)
    if not _number(p['soft_quiet_s']) or not 0 <= p['soft_quiet_s'] <= 3600:
        raise ValueError('Invalid soft quiet interval')
    for key in ('target_frequency_hz', 'hop_threshold_hz'):
        if p[key] is not None and (not _number(p[key]) or p[key] <= 0):
            raise ValueError('Invalid '+key)
    for key in ('max_steps', 'vote_count', 'vote_window', 'max_soft_restudies_per_hour'):
        if type(p[key]) is not int or not 1 <= p[key] <= 10000:
            raise ValueError('Invalid '+key)
    # Hop budget of one finite window action. 0 ends that action at its first hop;
    # steady recovery has a per-attempt deadline, not a lifetime recovery counter.
    if type(p['max_hops']) is not int or not 0 <= p['max_hops'] <= 100:
        raise ValueError('Invalid max_hops')
    if p['vote_count'] > p['vote_window'] or p['vote_window'] > 100:
        raise ValueError('Invalid early-warning vote window')
    if not 0 < p['step_fraction'] <= p['max_span_fraction'] <= 1:
        raise ValueError('Invalid window step or span fraction')
    if p['mode']=='position' and abs(p['position_offset_fraction'])>p['max_span_fraction']:
        raise ValueError('Position offset exceeds configured traversal span')
    if p['timeout_s']>86400:raise ValueError('Window operation exceeds 24 hours')
    if p['position_warning_fraction'] >= .5 or p['minimum_monotonicity'] > 1 or p['model_max_error_fraction'] > .5:
        raise ValueError('Invalid position model limits')
    return p


def observable(row, name):
    """Resolve scalar fields from owner rows or persisted study point values."""
    values = row.get('values', {})
    if name in values:
        return values[name]
    if name.startswith('metrics.'):
        return row.get('observation', {}).get('metrics', row.get('metrics', {})).get(name[8:])
    if name in ('current', 'temperature', 'piezo'):
        return row.get('outputs', {}).get(name, row.get(name))
    if name in row:
        return row[name]
    return row.get('observation', {}).get('frequency', {}).get(name)


def fit_position_features(points, observables, *, require_single_mode=True):
    """Fit provisional scalar feature/current relations, never an optical label.

    Each candidate includes its inverse-prediction error in current-axis units
    and pairwise monotonicity. A single map supplies candidates, not proof of
    future drift prediction. Repeated-current points remain independent samples.
    """
    candidates = {}
    qualified = [r for r in points
                 if not r.get('mode_hop', r.get('values', {}).get('mode_hop', False))
                 and (not require_single_mode or r.get('observation', {}).get('single_mode', r.get('values', {}).get('single_mode', True)))]
    # Mixed branches or thermal contexts must not manufacture a monotonic
    # relation. The caller can explicitly fit a separate model for each map.
    contexts = {(r.get('observation', {}).get('mode_id', r.get('mode_id')),
                 observable(r, 'temperature'), r.get('target_frequency_hz'))
                for r in qualified}
    if len(contexts) > 1:
        return dict(candidates={}, provisional=True, map_count=1,
                    warning='mixed optical or thermal map contexts')
    for name in observables:
        pairs = [(observable(r, 'current'), observable(r, name)) for r in qualified
                 if _number(observable(r, 'current')) and _number(observable(r, name))]
        if len(pairs) < 4:
            continue
        xs, ys = zip(*pairs)
        xm, ym = statistics.mean(xs), statistics.mean(ys)
        xx = sum((x-xm)**2 for x in xs)
        if xx <= 0:
            continue
        slope = sum((x-xm)*(y-ym) for x, y in pairs)/xx
        if slope == 0:
            continue
        intercept = ym-slope*xm
        residual = math.sqrt(statistics.mean((y-intercept-slope*x)**2 for x, y in pairs))
        comparable = [(x2-x1)*(y2-y1)*slope for i, (x1, y1) in enumerate(pairs)
                      for x2, y2 in pairs[i+1:] if x1 != x2]
        monotonicity = sum(v > 0 for v in comparable)/len(comparable) if comparable else 0.
        candidates[name] = dict(slope=slope, intercept=intercept, rmse=residual,
                                current_rmse=residual/abs(slope), monotonicity=monotonicity,
                                current_range=[min(xs), max(xs)], sample_count=len(pairs),
                                provisional=True)
    return dict(candidates=candidates, provisional=True, map_count=1)


class WindowCenterPolicy:
    """A finite center traversal; monitor mode starts another on early warning.

    A seed's low/high are measured boundary intervals, not safety rails. Unknown
    or expired bounds never create a midpoint. Seed context includes temperature,
    mode_id, target_frequency_hz and monotonic measured_s. The caller remains
    responsible for source-quality qualification and fatal owner exceptions.
    """
    def __init__(self, p, origin, policy, now, seed=None):
        self.p = parameters(dict(parameters=p), {})
        self.samples=WindowSamples(self.p,observable)
        self.hop=None
        self.fast_slow=bool(self.p['slow_observables'])
        self.decision_evidence=None
        self.last_move_s=now
        self.slow_side=None
        self.slow_outer=None
        self.origin = dict(origin)
        self.policy = policy
        self.started = now
        self.state = 'initializing'
        self.steps = 0
        self.pending = None
        self.expected = None
        self.confirmed_current = origin['current']
        self.last_stamp = None
        self.last_now = now
        self.ready_at = now
        self.direction = -1
        self.first_direction = -1
        self.last_good = None
        self.edges = {}
        self.edge_kinds = {}          # side -> 'threshold' (selected limits / single mode) or 'hop' (branch edge)
        self.hops = []                # every hop boundary record of this search (kept across recoveries)
        self.hop_open = False         # a hop recorded and not yet resumed (no second record by the lock)
        self.on_branch_currents = []  # last distinct currents whose rows passed the branch checks
        self.last_direction = None    # sign of the last acknowledged move
        self.last_step_side = None    # sign of the last acknowledged boundary-probing step (not a travel)
        self.resync_current = False   # after a recovery: adopt the restored current as confirmed
        self.contradicted_edges = []
        self.seed = copy.deepcopy(seed or {})
        self.features = self.seed.get('feature_model', {})
        self.mode_id = self.seed.get('mode_id')
        self.temperature = self.seed.get('temperature', self.origin['temperature'])
        self.votes = []
        self.quiet_until = now
        self.soft_scan_times = []
        self.points = []
        self.invalid_reason = None
        self.midpoint = None
        self.center_interval = None
        self.last_position = None
        self.scan_origin = self.origin['current']
        self.interior_anchor = self.scan_origin if p['initial_interior_search'] else None
        self.interior_direction = -1
        if self.seed:
            if not _number(self.seed.get('measured_s')) or not 0 <= now-self.seed['measured_s'] <= self.p['seed_ttl_s']:
                self.invalid_reason = 'seed expired or lacks a monotonic acquisition time'
            elif self.seed.get('qualification') is not None and self.seed['qualification'] != self.qualification():
                self.invalid_reason = 'seed qualification differs'
            elif self.p['target_frequency_hz'] is not None and self.seed.get('target_frequency_hz') != self.p['target_frequency_hz']:
                self.invalid_reason = 'seed target differs'
            else:
                for side, name in ((-1, 'low'), (1, 'high')):
                    bound = self.seed.get(name)
                    kind = 'threshold'
                    if isinstance(bound, dict):
                        kind = bound.get('kind', kind)
                        bound = bound.get('interval')
                    if bound is not None:
                        if not isinstance(bound, (tuple, list)) or len(bound) != 2 or not all(_number(x) for x in bound) or bound[0] > bound[1]:
                            raise ValueError('Expected ordered measured boundary interval')
                        if kind not in ('threshold', 'hop'):
                            raise ValueError('Unknown seed boundary kind')
                        self.edges[side] = list(bound)
                        self.edge_kinds[side] = kind
                if self.edges and any(not policy.low <= x <= policy.high for b in self.edges.values() for x in b):
                    raise ValueError('Seed boundary outside owner-approved axis interval')
                self._center()
        if self.invalid_reason:
            self.edges = {}
            self.edge_kinds = {}
            self.features = {}
        self.state = 'centering' if self.midpoint is not None else 'seek_edge'

    # ------------------------------------------------------------------ measured boundaries (kinds, known sides)
    @staticmethod
    def _side_name(side):
        return 'low' if side < 0 else 'high'

    def _boundaries(self):
        return {self._side_name(k): dict(interval=list(v), kind=self.edge_kinds.get(k, 'threshold'))
                for k, v in self.edges.items()}

    def _next_side(self):
        """The first side without a measured boundary (the usual order: first direction, then the other)."""
        for side in (self.first_direction, -self.first_direction):
            if side not in self.edges:
                return side
        return None

    def _crosses_edge(self, value, side):
        """True when a move toward ``side`` would pass the inner end of that side's measured boundary."""
        edge = self.edges.get(side)
        if edge is None or not side:
            return False
        return value < edge[1]-1e-12 if side < 0 else value > edge[0]+1e-12

    def _set_edge(self, side, interval, kind):
        """A boundary can move inward, never outward: the inner-most measurement of a side wins."""
        old = self.edges.get(side)
        if old is not None and (old[1] > interval[1] if side < 0 else old[0] < interval[0]):
            return False
        self.edges[side] = list(interval)
        self.edge_kinds[side] = kind
        return True

    def _note_on_branch(self, current):
        if not self.on_branch_currents or abs(current-self.on_branch_currents[-1]) > max(self.policy.resolution/2, 1e-10):
            self.on_branch_currents = (self.on_branch_currents+[current])[-2:]

    def _record_hop(self, current, *, reason, source, side=None, mode_after=None, frequency_hz=None):
        """A confirmed hop during a window step IS the boundary on the side the policy was moving toward: interval =
        last on-branch current ... first off-branch current, kind 'hop'. Recorded before the recovery runs."""
        if side is None and self.state == 'return_origin' and self.direction in self.edges:
            # the lock confirmed the hop while the policy was travelling back from a boundary it had just accepted
            # by its metrics (sr422 21:07: side peak 0.76 at the -19 GHz frame): that boundary WAS the hop
            side = self.direction
            interval = list(self.edges[side])
        else:
            side = side or self.last_step_side or self.last_direction or self.direction
            inner = next((c for c in reversed(self.on_branch_currents) if abs(c-current) > max(self.policy.resolution/2, 1e-10)), None)
            if inner is None or (side < 0 and inner < current) or (side > 0 and inner > current):
                inner = current
            interval = sorted((inner, current))
        adopted = self._set_edge(side, interval, 'hop')
        self.hop = dict(side=self._side_name(side), interval=interval, kind='hop', adopted=adopted, reason=reason,
                        source=source, mode_before=self.mode_id, mode_after=mode_after, current=current,
                        frequency_hz=frequency_hz, start_current=self.origin['current'], steps=self.steps,
                        state=self.state)
        self.hops.append(dict(self.hop))
        self.hop_open = True
        self.midpoint = None
        self.center_interval = None
        return self.hop

    @property
    def resumable(self):
        """True while the policy is stepping, travelling or waiting for the lock's recovery of a hop it saw."""
        return self.state in STEPPING_STATES or (self.state == 'recovery_required' and self.hop_open)

    def suspend_for_recovery(self, current, *, reason, source='lock'):
        """The lock confirmed a hop (or the policy asked for the recovery): record the hop boundary once, drop the
        pending proposal and wait. Returns the hop record, or None when the search is not resumable."""
        if not self.resumable:
            return None
        if not self.hop_open:
            self._record_hop(current, reason=reason, source=source)
        self.pending = None
        self.expected = None
        self.samples.reset()
        self.state = 'suspended'
        self._center()   # informational while suspended (both sides known); centering starts only on resume
        return self.hop

    def resume_after_recovery(self, now):
        """The lock re-armed on the original branch at its restored point: keep the measured boundaries, adopt the
        restored current, and turn to the other side (or to the midpoint when both sides are known)."""
        if self.state != 'suspended':
            raise ValueError('No suspended window search to resume')
        self.hop_open = False
        self.pending = None
        self.expected = None
        self.resync_current = True
        self.samples.reset()
        self.decision_evidence = None
        self.votes = []
        self.last_good = None
        settle = self.p['fast_settle_s'] if self.fast_slow else self.p['settle_s']
        self.ready_at = now+settle
        self.last_move_s = now
        self._center()
        if self.midpoint is not None:
            self.state = 'centering'
        elif self._next_side() is None:
            self.state = 'unresolved'
            return self._result(warning='measured boundary intervals overlap', resumed=True)
        else:
            self.state = 'return_origin'
        return self._result(resumed=True, next_side=self._side_name(self._next_side()) if self.midpoint is None else 'midpoint')

    def _adopt_restored_point(self, current):
        """A restored current beyond a measured boundary contradicts that boundary (the branch edge moved, e.g. a
        different PZT after the recovery): drop it, report it, and measure that side again."""
        for side in list(self.edges):
            if self._crosses_edge(current, side):
                self.contradicted_edges.append(dict(side=self._side_name(side), interval=list(self.edges[side]),
                                                    kind=self.edge_kinds.get(side, 'threshold'), restored_current=current))
                del self.edges[side]
                self.edge_kinds.pop(side, None)
                self.midpoint = None
                self.center_interval = None
        if self.state == 'centering' and self.midpoint is None:
            self.state = 'return_origin'

    def _turn_or_center(self, now, current, reason):
        """Never step toward a side whose boundary is known: take the other side, or the midpoint."""
        side = self._next_side()
        if side is None:
            self._center()
            if self.midpoint is None:
                self.state = 'unresolved'
                return self._result(warning='measured boundary intervals overlap')
            self.state = 'centering'
            if abs(current-self.midpoint) <= max(self.policy.resolution/2, 1e-10):
                return self._result()
            return self._propose(now, current, self.midpoint, 'both window boundaries measured; return to midpoint')
        self.direction = side
        self.state = 'seek_edge'
        self.last_good = current
        step = (self.policy.high-self.policy.low)*self.p['step_fraction']
        return self._propose(now, current, current+side*step, reason)

    def qualification(self):
        return dict(require_single_mode=self.p['require_single_mode'],
                    boundary_limits=copy.deepcopy(self.p['boundary_limits']),
                    slow_observables=list(self.p['slow_observables']))

    def _center(self):
        lo, hi = self.edges.get(-1), self.edges.get(1)
        if lo is None or hi is None or lo[1] >= hi[0]:
            self.midpoint = None
            self.center_interval = None
            return
        self.midpoint = (statistics.mean(lo)+statistics.mean(hi))/2
        self.center_interval = [(lo[0]+hi[0])/2, (lo[1]+hi[1])/2]

    def _result(self, **extra):
        return dict(state=self.state, done=self.state in ('complete', 'unresolved', 'invalidated'), algorithm='current_window', measure_dither=self.p['measure_dither'],
                    boundaries=self._boundaries(), hops=copy.deepcopy(self.hops), hop_count=len(self.hops),
                    contradicted_boundaries=copy.deepcopy(self.contradicted_edges),
                    center=self.midpoint, center_interval=self.center_interval, steps=self.steps,
                    feature_position=self.last_position, provisional=True,
                    center_definition=('branch_limited_window' if 'hop' in self.edge_kinds.values() else
                                       'single_mode_window' if self.p['require_single_mode'] else 'selected_limits_window'),
                    decision_evidence=self.decision_evidence, **extra)

    def _invalidate(self, reason, recovery=False):
        self.pending = None
        self.expected = None
        self.samples.reset()
        self.edges = {}
        self.edge_kinds = {}
        self.midpoint = None
        self.center_interval = None
        self.features = {}
        self.votes = []
        self.state = 'recovery_required' if recovery else 'invalidated'
        return self._result(warning=reason, recovery_required=recovery)

    TRAVEL_REASONS = ('near boundary confirmed; seek opposite boundary', 'cross measured interior toward opposite boundary',
                      'both window boundaries measured; return to midpoint', 'measured operating-window midpoint')

    def _propose(self, now, current, target, reason):
        width = self.policy.high-self.policy.low
        amount = min(width*self.p['step_fraction'], self.policy.max_step)
        value = current+max(-amount, min(amount, target-current))
        resolution = self.policy.resolution
        if resolution:
            delta = math.floor((abs(value-current)+1e-12)/resolution)*resolution
            value = round((current+math.copysign(delta, value-current))/resolution)*resolution
        if abs(value-current) <= max(resolution/2, 1e-12):
            self.state = 'unresolved'
            return self._result(warning='window movement below axis resolution')
        if (self.steps >= self.p['max_steps'] or not self.policy.low <= value <= self.policy.high
            or abs(value-(self.interior_anchor if self.interior_anchor is not None else self.scan_origin)) > width*self.p['max_span_fraction']+1e-12):
            self.state = 'unresolved'
            return self._result(warning='window traversal reached a finite step, span or owner limit')
        side = 1 if value > current else -1
        if self._crosses_edge(value, side):
            # safety net of the known-side rule: no proposal of any state passes a measured boundary
            self.state = 'unresolved'
            return self._result(warning='proposal would cross a measured %s boundary' % self.edge_kinds.get(side, 'threshold'))
        self.pending = dict(axis='current', value=value, reason=reason, side=side)
        return self._result(action=dict(self.pending))

    def acknowledge(self, value, now=None):
        """Commit a confirmed write; optional monotonic now starts its settling clock."""
        if self.pending is None or not _number(value) or abs(value-self.pending['value']) > max(self.policy.resolution/2, 1e-10):
            raise ValueError('No matching confirmed window proposal')
        self.expected = value
        self.steps += 1
        self.last_direction = self.pending.get('side') or self.last_direction
        if self.pending.get('reason') not in self.TRAVEL_REASONS:
            self.last_step_side = self.last_direction
        settle=max(self.p['fast_settle_s'] if self.fast_slow else self.p['settle_s'],getattr(self.policy,'settle_s',0.))
        self.ready_at = None if now is None else now+settle
        self.last_move_s=now
        self.samples.reset()
        self.decision_evidence=None
        self.pending = None

    def _hop(self, now, current, obs, frequency, reason):
        """Branch identity or frequency band lost. During a window step (STEPPING_STATES) the hop is the boundary on
        that side and the search waits for the lock's recovery (resumed afterwards). No window move is issued
        after branch loss. Outside a step the same recovery handoff applies."""
        if self.state in STEPPING_STATES:
            self._record_hop(current, reason=reason, source='policy', mode_after=obs.get('mode_id'),
                             frequency_hz=frequency.get('frequency_hz'))
            self.pending = None
            self.expected = None
            self.samples.reset()
            self.state = 'recovery_required'
            return self._result(recovery_required=True, warning=reason, hop=self.hop)
        return self._invalidate(reason, recovery=True)

    def _bad(self, obs, selected, limits):
        return ((self.p['require_single_mode'] and not obs.get('single_mode',False)) or
                any(_number(selected.get(k)) and selected[k]>=v for k,v in limits.items()))

    @property
    def needs_slow(self):
        # A selected slow metric must qualify every visited point. Otherwise
        # a dither-only boundary can never trigger slow-boundary confirmation.
        return 'dither_response' in self.p['observables']

    def _accept_edge(self, now, current, interval):
        # a confirmed threshold boundary is final for this action on its side: the policy never steps toward
        # it again (rule of 2026-10-27; a hop boundary already on this side stays unless the new one is inner)
        self._set_edge(self.direction,interval,'threshold')
        if -self.direction in self.edges:
            self._center()
            if self.midpoint is None:
                self.state='unresolved'
                return self._result(warning='measured boundary intervals overlap')
            self.state='centering'
            if abs(current-self.midpoint)<=max(self.policy.resolution/2,1e-10):
                return self._result()
            return self._propose(now,current,self.midpoint,'both window boundaries measured; return to midpoint')
        self.state='return_origin'
        if abs(current-self.scan_origin)<=max(self.policy.resolution/2,1e-10):
            self.direction=-self.direction;self.state='seek_edge';self.last_good=None
            step=(self.policy.high-self.policy.low)*self.p['step_fraction']
            return self._propose(now,current,current+self.direction*step,'inner point is origin; seek opposite boundary')
        return self._propose(now,current,self.scan_origin,'near boundary confirmed; seek opposite boundary')

    def _slow_boundary(self, now, row, current, selected, urgent):
        if urgent or self._bad(row['observation'],selected,self.p['boundary_limits']):
            # A slow metric can move the measured boundary inward, never expand it.
            self.slow_outer=current
            step=(self.policy.high-self.policy.low)*self.p['step_fraction']
            return self._propose(now,current,current-self.slow_side*step,'slow boundary rejected; verify one step inward')
        self.direction=self.slow_side
        return self._accept_edge(now,current,sorted((self.slow_outer,current)))

    def _position(self, row):
        if self.midpoint is None:
            return None
        width = statistics.mean(self.edges[1])-statistics.mean(self.edges[-1])
        predictions = []
        for name, model in self.features.get('candidates', {}).items():
            if name not in self.p['observables']:
                continue
            y = observable(row, name)
            if (not _number(y) or not _number(model.get('slope')) or model['slope'] == 0
                or not _number(model.get('monotonicity')) or model['monotonicity'] < self.p['minimum_monotonicity']
                or not _number(model.get('current_rmse')) or model['current_rmse'] > width*self.p['model_max_error_fraction']):
                continue
            predicted = (y-model['intercept'])/model['slope']
            domain = model.get('current_range', [self.policy.low, self.policy.high])
            if not domain[0] <= predicted <= domain[1]:
                continue
            predictions.append(predicted)
        if not predictions or max(predictions)-min(predictions) > width*self.p['model_max_error_fraction']:
            return None
        position = statistics.median(predictions)
        return dict(current_equivalent=position, offset_fraction=(position-self.midpoint)/width,
                    features=len(predictions), provisional=True)

    def tick(self, now, row):
        self.last_now = now
        # A proposal without acknowledgement is discarded, not counted as a
        # visited point. A subsequent fresh row may create a new proposal.
        self.pending = None
        if self.state in ('complete', 'unresolved', 'invalidated', 'recovery_required'):
            return self._result(recovery_required=self.state == 'recovery_required')
        if self.state == 'suspended':
            return self._result(suspended=True, warning='window search suspended for the original-branch recovery')
        if now-self.started > self.p['timeout_s']:
            self.state = 'unresolved'
            return self._result(warning='window center deadline reached')
        obs = row.get('observation', {})
        frequency = obs.get('frequency', {})
        acquired = row.get('acquired_s', frequency.get('acquired_s'))
        stamp = row.get('acquired_unix_s', frequency.get('acquired_unix_s', acquired))
        if (not _number(acquired) or not 0 <= now-acquired <= self.p['measurement_max_age_s']
            or not _number(stamp) or (self.last_stamp is not None and stamp <= self.last_stamp)):
            return self._result(warning='new fresh window observation required')
        self.last_stamp = stamp
        # v5.1-hostcheck: the quality label is informational; only a finite frequency is required.
        if not _number(frequency.get('frequency_hz')):
            return self._result(warning='window optical frequency unavailable', quality_unavailable=True)
        if self.invalid_reason:
            return self._invalidate(self.invalid_reason)
        outputs = row['outputs']
        current = outputs['current']
        if any(not _number(outputs.get(k)) for k in ('current', 'temperature', 'piezo')):
            return self._result(warning='finite owner output readbacks required')
        if abs(outputs['temperature']-self.temperature) > self.p['temperature_tolerance']+1e-12:
            return self._invalidate('TEC context changed')
        if self.mode_id is None:
            self.mode_id = obs.get('mode_id')
        if self.mode_id is None or obs.get('mode_id') != self.mode_id:
            return self._hop(now, current, obs, frequency, 'optical branch changed')
        target = self.p['target_frequency_hz']
        threshold = self.p['hop_threshold_hz']
        if target is not None and threshold is not None and abs(frequency['frequency_hz']-target) > threshold:
            return self._hop(now, current, obs, frequency, 'frequency outside same-mode study band')
        self._note_on_branch(current)
        if self.resync_current:
            # first fresh on-branch row after the lock's recovery: the restored current is the confirmed point
            self.resync_current = False
            self.expected = None
            self.confirmed_current = current
            self._adopt_restored_point(current)
        if self.expected is not None:
            if abs(current-self.expected) > max(self.policy.resolution/2, 1e-10):
                return self._result(warning='awaiting confirmed current readback')
            self.confirmed_current = current
            self.expected = None
        elif abs(current-self.confirmed_current) > max(self.policy.resolution/2, 1e-10):
            return self._invalidate('current changed without an acknowledged window move')
        if self.ready_at is None:
            self.last_move_s=now  # No write clock supplied: use the later confirmed observation.
            self.ready_at = now+(self.p['fast_settle_s'] if self.fast_slow else self.p['settle_s'])
        if now < self.ready_at:
            return self._result()
        names = self.p['observables']
        raw = {name: observable(row,name) for name in names}
        urgent = self._bad(obs,raw,self.p['urgent_limits'])
        # A single urgent optical sample is never diluted by averaging.
        fast_bad=self.fast_slow and self._bad(obs,{k:v for k,v in raw.items() if k not in self.p['slow_observables']},self.p['boundary_limits'])
        if self.state=='slow_boundary' and (urgent or fast_bad):
            self.samples.reset()
            return self._slow_boundary(now,row,current,raw,True)
        if urgent:
            self.samples.reset()
            selected=raw
        else:
            if self.fast_slow and 'dither_response' in names:
                start=row.get('dither_source_start_s')
                if not _number(raw.get('dither_response')) or not _number(start) or not self.last_move_s<start<=now:
                    return self._result(warning='awaiting wholly post-move slow fit')
            averaged=self.samples.add(now,row,names,
                slow_after_s=self.last_move_s if ('dither_response' in names and (self.fast_slow or self.p['average_samples']>1)) else None)
            if averaged is None:
                return self._result(warning='awaiting distinct selected window samples')
            row=averaged
            selected={name:observable(row,name) for name in names}
        if any(not _number(v) for v in selected.values()):
            if urgent and not self.p['initial_interior_search']:
                # Rule K (2026-10-27): spectral degradation is reported; the search ends unresolved and no
                # original-branch recovery is requested (a hop is still recovery_required above).
                self.state = 'unresolved'
                return self._result(warning='spectral degradation with unavailable auxiliary feature', degraded=True)
            return self._result(warning='selected window feature unavailable')
        self.decision_evidence=dict(current=current,acquired_s=acquired,values=dict(selected),aggregation=row.get('aggregation'),urgent_bypass=urgent)
        if self.state=='slow_boundary':
            return self._slow_boundary(now,row,current,selected,urgent)
        self.points.append(dict(outputs=dict(outputs), values=selected,
                                acquired_s=acquired, mode_id=self.mode_id, observed_mode_id=obs.get('mode_id'),
                                frequency_hz=frequency.get('frequency_hz'), state=self.state))
        if self.state == 'centering':
            if abs(current-self.midpoint) <= max(self.policy.resolution/2, 1e-10):
                if self.p['require_single_mode'] and not obs.get('single_mode', False):
                    self.state = 'unresolved'
                    return self._result(warning='center reached but clean single mode was not confirmed')
                if self._bad(obs,selected,self.p['boundary_limits']):
                    self.state='unresolved'
                    return self._result(warning='center reached outside selected window limits')
                self.state = 'monitoring' if self.p['mode'] == 'monitor' else 'complete'
                self.votes = []
                self.quiet_until = now+self.p['soft_quiet_s']
                return self._result(center_verified=True)
            return self._propose(now, current, self.midpoint, 'measured operating-window midpoint')
        if self.state == 'monitoring':
            self.last_position = self._position(row)
            # A true spectral degradation bypasses optional soft quiet/voting.
            urgent = self._bad(obs, selected, self.p['urgent_limits'])
            if urgent:
                return self._invalidate('operating spectrum degraded at verified center', recovery=True)
            if now < self.quiet_until:
                self.votes = []
                return self._result(soft_quiet_remaining_s=self.quiet_until-now)
            causes = {('observable',k) for k,v in self.p['early_limits'].items() if selected[k] >= v}
            position_side = None
            if (self.last_position is not None and
                    abs(self.last_position['offset_fraction']) >= self.p['position_warning_fraction']):
                position_side = 1 if self.last_position['offset_fraction'] > 0 else -1
                causes.add(('position',position_side))
            self.votes = (self.votes+[causes])[-self.p['vote_window']:]
            confirmed = {cause for cause in set().union(*self.votes)
                         if sum(cause in vote for vote in self.votes) >= self.p['vote_count']}
            if not confirmed:
                return self._result()
            self.soft_scan_times[:]=[t for t in self.soft_scan_times if now-t<3600.]
            if len(self.soft_scan_times)>=self.p['max_soft_restudies_per_hour']:
                return self._result(soft_budget_exhausted=True,
                    warning='soft proxy restudy budget reached; hard spectral monitoring remains active')
            self.soft_scan_times.append(now)
            # Only a confirmed position-side vote supplies a direction. Small
            # near-center noise must not choose the side of a scalar warning.
            self.direction = (position_side if position_side is not None and
                              ('position',position_side) in confirmed else -1)
            self.first_direction = self.direction
            self.scan_origin = current
            self.edges = {}
            self.edge_kinds = {}
            self.midpoint = None
            self.center_interval = None
            self.state = 'seek_edge'
            self.last_good = None
            self.started = now
        if self.state == 'return_origin':
            if abs(current-self.scan_origin) > max(self.policy.resolution/2, 1e-10):
                return self._propose(now, current, self.scan_origin, 'cross measured interior toward opposite boundary')
            side = self._next_side()
            if side is None:
                # both boundaries known (e.g. resumed after a hop with the other side measured before)
                return self._turn_or_center(now, current, 'both window boundaries measured; return to midpoint')
            self.state = 'seek_edge'
            self.direction = side
            self.last_good = None
        boundary = any(_number(selected.get(k)) and selected[k] >= v for k, v in self.p['boundary_limits'].items())
        # Same-branch multimode is a boundary, not a mode hop. Reverse toward
        # safe interior while leaving native feedback supervision to the owner.
        boundary = boundary or (self.p['require_single_mode'] and not obs.get('single_mode', False))
        if self.state == 'return_initial':
            if abs(current-self.interior_anchor)>max(self.policy.resolution/2,1e-10):
                return self._propose(now,current,self.interior_anchor,'return to initial point before opposite interior search')
            self.state='seek_interior'
        if not boundary and self.state=='seek_interior':
            # A newly measured clean interior starts the two-sided window from here; the original finite travel
            # envelope and the boundaries already measured (threshold or hop) remain: the scan takes the first
            # side still unknown and never steps toward a known one.
            self.scan_origin=current;self.state='seek_edge';self.last_good=None
            side=self._next_side()
            if side is None:
                return self._turn_or_center(now,current,'both window boundaries measured; return to midpoint')
            self.direction=side;self.first_direction=side
        if boundary and self.last_good is not None:
            if self.fast_slow:
                self.slow_side=self.direction;self.slow_outer=current
                self.state='slow_boundary';self.samples.reset()
                return self._propose(now,current,self.last_good,'fast boundary found; verify slow fit at inner point')
            return self._accept_edge(now,current,sorted((self.last_good,current)))
        if boundary:
            if self.p['initial_interior_search']:
                self.state='seek_interior'
                width=self.policy.high-self.policy.low
                amount=min(width*self.p['step_fraction'],self.policy.max_step)
                target=current+self.interior_direction*amount
                span=width*self.p['max_span_fraction']
                # the interior search is bounded by the owner interval, the span AND the boundaries already measured
                # on that side (2026-10-27: it used to walk through a measured low boundary into the branch edge)
                if (not self.policy.low<=target<=self.policy.high or abs(target-self.interior_anchor)>span+1e-12
                        or self.interior_direction in self.edges):
                    if self.interior_direction<0:
                        self.interior_direction=1;self.state='return_initial'
                        if abs(current-self.interior_anchor)<=max(self.policy.resolution/2,1e-10):
                            self.state='seek_interior'
                            if 1 in self.edges:
                                self.state='unresolved'
                                return self._result(warning='no clean same-mode interior between the measured boundaries')
                            return self._propose(now,current,current+amount,'seek initial interior above a lower owner boundary')
                        return self._propose(now,current,self.interior_anchor,'reverse bounded initial interior search')
                    self.state='unresolved'
                    return self._result(warning='no clean same-mode interior within the approved search envelope')
                return self._propose(now,current,target,'seek a clean same-mode interior before measuring both boundaries')
            # Without a qualified interior, a high metric at the initial point
            # cannot establish which side's boundary it represents.
            urgent = self._bad(obs,selected,self.p['urgent_limits'])
            if self.p['mode'] == 'monitor' and not urgent:
                self.state = 'waiting_interior'
                return self._result(warning='waiting for a measured same-mode interior')
            self.state = 'unresolved'
            return self._result(warning='boundary sign without a measured same-mode interior')
        self.state = 'seek_edge'
        self.last_good = current
        if self.direction in self.edges:
            # known-side rule: a side with a measured boundary (threshold or hop) is never stepped toward again
            return self._turn_or_center(now, current, 'boundary on this side already measured; seek the other boundary')
        step = (self.policy.high-self.policy.low)*self.p['step_fraction']
        return self._propose(now, current, current+self.direction*step, 'same-mode boundary traversal')
