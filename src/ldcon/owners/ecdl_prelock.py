"""ECDL commissioning algorithm; no transports, controller codes or vendor metrics.

Axis coordinates, calibration, limits and slew enforcement belong to the owner
adapter. Optical mode identity must be stable across a TEC walk, not a rounded
frequency or a device's channel number. No method can enable an output or lock.
"""
from dataclasses import dataclass, asdict, field
import math
from typing import Protocol

from ldcon.core.contracts import FrequencySample, DeviceError, MeasurementUnavailable
from ldcon.core.device_conditions import finite
from ldcon.core.interface_cards import CardCancelled, sample_trace


DEFAULTS = dict(target_frequency_hz=None, tolerance_hz=None, desired_mode=None,
                center_fraction=.5, center_tolerance_fraction=.15,
                timeout_s=1800., stable_s=30., max_sample_age_s=1.,
                sample_interval_s=.5, measurement_timeout_s=15.,
                approach_gain=.2, max_tec_excursion_fraction=.1,
                current_step_fraction=.01, current_span_fraction=.15,
                map_steps=8, max_recoveries=5, max_commands=10000,
                map_ttl_s=300., map_context_fraction=.002,
                initial_current_map=False, initial_map_replay_start=False, initial_map_step_fraction=.025,
                initial_mode_capture_hz=2e11, initial_map_start_fraction=0.,
                initial_map_direction='up', initial_map_end_fraction=1.,
                initial_map_conditioning_fractions=[],
                recovery_direction='up', recovery_stable_s=None,
                recovery_preparation_steps=2, mode_hop_threshold_hz=None)


def parameters(card, overrides):
    if not isinstance(card['parameters'], dict) or not isinstance(overrides, dict):
        raise ValueError('Expected ECDL parameter mappings')
    if (set(card['parameters']) | set(overrides)) - DEFAULTS.keys():
        raise ValueError('Unknown ECDL prelock parameter; device settings belong to adapters')
    p = {**DEFAULTS, **card['parameters'], **overrides}
    for key, value in p.items():
        if key=='recovery_direction':
            if value not in (None,'up','down'):raise ValueError('recovery_direction must be up or down')
        elif key in ('recovery_stable_s','mode_hop_threshold_hz') and value is None:continue
        elif key in ('initial_current_map','initial_map_replay_start'):
            if type(value) is not bool:raise ValueError('initial_current_map must be boolean')
        elif key=='initial_map_conditioning_fractions':
            if not isinstance(value,list) or len(value)>100 or any(not 0<=finite(x)<=1 for x in value):
                raise ValueError('Conditioning path must contain at most 100 approved-axis fractions')
        elif key=='initial_map_direction':
            if value not in ('up','down'):raise ValueError('initial_map_direction must be up or down')
        elif key in ('initial_map_start_fraction','initial_map_end_fraction'):
            if not 0<=finite(value)<=1:raise ValueError(key+' must be in [0, 1]')
        elif key == 'desired_mode':
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError('desired_mode must be a nonempty mode identity')
        elif key in ('target_frequency_hz', 'tolerance_hz', 'timeout_s') and value is None:
            continue
        else:
            number = finite(value, key)
            if number <= 0:
                raise ValueError(key + ' must be positive')
    for key in ('map_steps', 'max_recoveries', 'max_commands','recovery_preparation_steps'):
        if type(p[key]) is not int:
            raise ValueError(key + ' must be an integer')
    for key in ('center_fraction', 'center_tolerance_fraction', 'approach_gain',
                'max_tec_excursion_fraction', 'current_step_fraction',
                'current_span_fraction', 'map_context_fraction','initial_map_step_fraction'):
        if p[key] > 1:
            raise ValueError(key + ' must be <= 1')
    if p['center_tolerance_fraction'] >= .5 or (p['timeout_s'] is not None and p['timeout_s'] > 86400):
        raise ValueError('Invalid center tolerance or runtime')
    if (p['timeout_s'] is not None and p['stable_s'] >= p['timeout_s']) or p['sample_interval_s'] < .05:
        raise ValueError('Invalid stability time or sample interval')
    if p['recovery_preparation_steps']>100:raise ValueError('Recovery preparation budget is too large')
    if p['map_steps'] > 100 or p['max_commands'] > 100000 or p['max_recoveries'] > 100:
        raise ValueError('ECDL search budget is too large')
    if p['initial_map_start_fraction']>=p['initial_map_end_fraction']:
        raise ValueError('Current map start must precede its end')
    if math.ceil((p['initial_map_end_fraction']-p['initial_map_start_fraction'])/p['initial_map_step_fraction'])>100:
        raise ValueError('Initial current map has more than 100 intervals')
    return p


@dataclass(frozen=True)
class AxisPolicy:
    unit: str
    low: float
    high: float
    max_step: float
    max_slew_per_s: float
    update_interval_s: float
    settle_s: float
    frequency_hz_per_unit: float | None
    identity: str
    revision: str
    resolution: float = 0.

    @property
    def span(self):
        return self.high - self.low

    def validate(self):
        for name in ('low', 'high', 'max_step', 'max_slew_per_s', 'update_interval_s', 'settle_s'):
            finite(getattr(self, name), name)
        if self.span <= 0 or min(self.max_step, self.max_slew_per_s, self.update_interval_s) <= 0 or self.settle_s < 0:
            raise ValueError('Invalid axis policy')
        if any(not isinstance(value, str) or not value for value in (self.unit, self.identity, self.revision)):
            raise ValueError('Axis unit, identity and calibration revision are required')
        if self.frequency_hz_per_unit is not None:
            if finite(self.frequency_hz_per_unit) == 0:
                raise ValueError('Axis tuning slope cannot be zero')
        if finite(self.resolution, 'resolution') < 0:
            raise ValueError('Axis resolution must be nonnegative')


class AxisPort(Protocol):
    policy: AxisPolicy
    def readback(self) -> float: ...
    def move(self, value: float, duration_s: float) -> None:
        """Owner must enforce bounds, rate, readback and joint device constraints."""
        ...


@dataclass(frozen=True)
class ModeObservation:
    frequency: FrequencySample
    single_mode: bool
    mode_id: str | None
    quality_valid: bool
    # Lower is better. The monitor owns calibration and metric construction.
    impurity: float | None
    maintenance_required: bool = False
    metrics: dict = field(default_factory=dict)
    fringe_analysis: dict = field(default_factory=dict)


class OpticalModePort(Protocol):
    def read_mode(self) -> ModeObservation: ...


class OpenLoopPort(Protocol):
    def is_open_loop(self) -> bool: ...


def acquire_ecdl(observation, piezo, temperature, current, loop, p, *, clock, sleep, cancel, emit):
    """Return an initial point and path map, holding the successful outputs.

    All output writes pass a single gate; stale/cached measurements, cancellation,
    loop closure, timeout and telemetry failures prevent further writes. Errors
    propagate: no speculative rollback or automatic output switching is attempted.
    """
    p = parameters({'parameters': p}, {})
    if p['target_frequency_hz'] is None or p['tolerance_hz'] is None:
        raise ValueError('Target frequency and tolerance are required')
    axes = dict(piezo=piezo, temperature=temperature, current=current)
    for axis in axes.values():
        axis.policy.validate()
    if temperature.policy.frequency_hz_per_unit is None:
        raise ValueError('Calibrated temperature tuning slope is required')
    policies = {name: axis.policy for name, axis in axes.items()}
    started = clock(); deadline = started + p['timeout_s'] if p['timeout_s'] is not None else math.inf
    writes = 0; last_acquisition = None; desired = p['desired_mode']
    points = []; routes = []; active_path = None; recoveries = 0; known_bias=None
    branch_reference=None
    origin = {}; last_write = {name: started for name in axes}; recovering=False

    def begin_recovery():
        nonlocal recovering
        if hasattr(current,'begin_recovery'):current.begin_recovery()
        recovering=True

    def end_recovery():
        nonlocal recovering
        if hasattr(current,'end_recovery'):current.end_recovery()
        recovering=False

    # v5.1-hostcheck (human decision 2026-10-22): wavemeter quality/status/aligned labels are informational: a finite Hz reading of the current cycle (age on acquired_s, which is the host receive time
    # when the source clock is not trusted/aligned).
    def fresh(o):
        s=o.frequency
        return s.unit=='Hz' and type(s.acquired_s) in (int,float) and 0<=clock()-s.acquired_s<=p['max_sample_age_s']

    def frequency_valid(o):
        f=o.frequency.frequency_hz
        return fresh(o) and type(f) in (int,float) and math.isfinite(f) and f>0

    def check():
        if cancel():
            raise CardCancelled('ECDL acquisition stopped; outputs held')
        if clock() >= deadline:
            raise DeviceError('ECDL acquisition timed out; outputs held')
        if loop.is_open_loop() is not True:
            raise DeviceError('ECDL acquisition requires open loop; outputs held')
        if any(axis.policy != policies[name] for name, axis in axes.items()):
            raise DeviceError('Axis calibration or limits changed during ECDL acquisition')

    def pause(duration):
        until = min(deadline, clock() + duration)
        while clock() < until:
            check()
            remaining=until-clock()
            if remaining>0:sleep(min(p['sample_interval_s'],remaining))
        check()

    def outputs():
        values = {}
        for name, axis in axes.items():
            value = finite(axis.readback(), name)
            if not axis.policy.low <= value <= axis.policy.high:
                raise DeviceError(name + ' readback outside device envelope')
            values[name] = value
        return values

    def read(stage, require_frequency=False, allow_timeout=False):
        nonlocal last_acquisition,branch_reference
        waiting = clock()
        while True:
            check()
            try:
                o = observation.read_mode()
            except DeviceError as error:
                o = None; reason = str(error)
            else:
                row = sample_trace(o.frequency, clock(), p['max_sample_age_s'])
                stamp = o.frequency.acquired_unix_s if o.frequency.acquired_unix_s is not None else o.frequency.acquired_s
                good = ((frequency_valid(o) or recovering and not require_frequency and fresh(o)) and type(o.single_mode) is bool
                        and type(o.maintenance_required) is bool
                        and ((type(o.impurity) in (int, float) and math.isfinite(o.impurity) and o.impurity>=0)
                             or recovering and o.impurity is None and o.single_mode is False)
                        and (last_acquisition is None or stamp > last_acquisition))
                reason = None if good else 'Invalid, stale or repeated optical mode observation'
            if reason is None:
                check(); values = outputs()
                # A slow actuator readback must not make the optical sample stale.
                if not fresh(o) or (not recovering and not frequency_valid(o)):
                    raise DeviceError('Optical observation expired during readback')
                last_acquisition = stamp
                if (not recovering and frequency_valid(o) and o.single_mode and not o.maintenance_required
                        and branch_matches(o)):
                    branch_reference=o.frequency.frequency_hz
                entry = dict(time_s=clock(), stage=stage, frequency_hz=o.frequency.frequency_hz,
                             acquired_s=stamp, mode_id=o.mode_id, single_mode=o.single_mode,
                             impurity=o.impurity, metrics=dict(o.metrics), fringe_analysis=dict(o.fringe_analysis), maintenance_required=o.maintenance_required, outputs=values)
                emit('ecdl_observation', entry)
                emit('trace', {**row, 'mode_id':o.mode_id,'single_mode':o.single_mode,
                               'mode_impurity':o.impurity,'fringe_metrics':dict(o.metrics),'maintenance_required':o.maintenance_required,
                               'outputs':values,'output_units':{k:a.policy.unit for k,a in axes.items()}})
                if active_path is not None:
                    points.append(entry)
                return o
            emit('measurement_rejected', dict(stage=stage, reason=reason))
            if clock() - waiting >= p['measurement_timeout_s']:
                if allow_timeout:return None
                raise DeviceError('ECDL mode measurement timed out; outputs held')
            # The console labels edge-of-visit exposures as overlap. Re-read
            # promptly for a correctly attributed frame; never reuse its value.
            pause(min(.05,p['sample_interval_s'],p['measurement_timeout_s']))

    def branch_matches(o):
        return (p['mode_hop_threshold_hz'] is None or branch_reference is None
                or o.frequency.frequency_hz is not None and abs(o.frequency.frequency_hz-branch_reference)<=p['mode_hop_threshold_hz'])

    def good(o):
        return frequency_valid(o) and o.single_mode and not o.maintenance_required and branch_matches(o)

    def tec_bounds():
        radius=temperature.policy.span*p['max_tec_excursion_fraction']
        return (max(temperature.policy.low,origin['temperature']-radius),
                min(temperature.policy.high,origin['temperature']+radius))

    def reachable(o):
        if not frequency_valid(o):return False
        present=outputs()['temperature'];slope=temperature.policy.frequency_hz_per_unit
        shifts=[(edge-present)*slope for edge in tec_bounds()]
        error=p['target_frequency_hz']-o.frequency.frequency_hz
        return min(shifts)-p['tolerance_hz']<=error<=max(shifts)+p['tolerance_hz']

    def move(name, target):
        nonlocal writes
        axis = axes[name]; policy = axis.policy
        if recovering and name!='current':raise DeviceError('TEC and piezo are frozen during current mapping')
        if not policy.low <= finite(target) <= policy.high:
            raise DeviceError('Candidate outside ' + name + ' envelope')
        if policy.resolution:
            quantum=policy.resolution
            first=math.ceil((policy.low-1e-12)/quantum)
            last=math.floor((policy.high+1e-12)/quantum)
            if first>last:raise DeviceError('No representable output inside the approved envelope')
            target=max(first,min(last,round(target/quantum)))*quantum
        for _ in range(p['max_commands']):
            check(); before = outputs()[name]
            if abs(target - before) <= max(1e-12, policy.span * 1e-12, policy.resolution*.500001):
                return
            pause(max(0, last_write[name] + policy.update_interval_s - clock()))
            read('before ' + name + ' move')
            before = outputs()[name]
            dt = policy.update_interval_s
            delta = min(abs(target - before), policy.max_step, policy.max_slew_per_s * dt)
            candidate = before + math.copysign(delta, target - before)
            if policy.resolution:
                quantum=policy.resolution
                candidate=round(candidate/quantum)*quantum
                if abs(candidate-before)>delta+1e-12:
                    candidate=before+math.copysign(math.floor(delta/quantum)*quantum,target-before)
                if abs(candidate-before)<quantum*.5:
                    raise DeviceError('Device resolution prevents the requested bounded move')
            if writes >= p['max_commands']:
                raise DeviceError('ECDL command budget exhausted; outputs held')
            emit('command_requested', dict(interface=name, value=candidate, unit=policy.unit, duration_s=dt))
            check()
            try:
                axis.move(candidate, dt)
            except MeasurementUnavailable:
                # Explicit owner rejection before physical I/O, never an unknown
                # write outcome. Hold, reacquire, and compute a new proposal.
                pause(p['sample_interval_s']);read('measurement recovery before output')
                continue
            writes += 1; last_write[name] = clock()
            emit('command_applied', dict(interface=name, value=outputs()[name], unit=policy.unit))
            if active_path is not None:
                active_path.append(dict(axis=name, value=candidate))
        raise DeviceError('Actuator failed to reach the requested value')

    def settled(stage, duration):
        until = clock() + duration
        o = read(stage)
        while clock() < until:
            pause(min(p['sample_interval_s'], until - clock())); o = read(stage)
        return o

    def verify(stage, require_target=False, require_reachable=False, stable_s=None):
        stable_s=p['stable_s'] if stable_s is None else stable_s
        def acceptable(o):
            return (good(o) and (not require_reachable or reachable(o)) and
                    (not require_target or abs(p['target_frequency_hz'] - o.frequency.frequency_hz) <= p['tolerance_hz']))
        began=clock();limit=began+max(4*stable_s,p['measurement_timeout_s'])
        start=None;previous=None;count=0;max_gap=0.
        minimum=math.ceil(stable_s/p['sample_interval_s'])+1
        while clock()<limit:
            if recovering:
                probe=read(stage,allow_timeout=True)
                # A missing wavelength must not prevent escape from this point.
                if probe is None or probe.frequency.frequency_hz is None:return None
                o=probe if frequency_valid(probe) else read(stage,require_frequency=True,allow_timeout=True)
            else:o=read(stage,require_frequency=True,allow_timeout=True)
            if o is None or not acceptable(o):
                return None
            stamp=o.frequency.acquired_unix_s if o.frequency.acquired_unix_s is not None else o.frequency.acquired_s
            gap=0. if previous is None else stamp-previous
            if start is None or gap>p['max_sample_age_s']:
                if start is not None:emit('verification_restarted',dict(stage=stage,valid_sample_gap_s=gap))
                start=stamp;count=0;max_gap=0.;gap=0.
            previous=stamp;count+=1;max_gap=max(max_gap,gap)
            if stamp-start>=stable_s and count>=minimum:
                emit('ecdl_stability_verified',dict(stage=stage,valid_samples=count,acquisition_span_s=stamp-start,
                    max_valid_sample_gap_s=max_gap,wall_elapsed_s=clock()-began))
                return o
            pause(p['sample_interval_s'])
        emit('verification_incomplete',dict(stage=stage,valid_samples=count,wall_elapsed_s=clock()-began))
        return None

    def recover(o):
        nonlocal active_path, recoveries
        recoveries += 1
        if recoveries > p['max_recoveries']:
            raise DeviceError('ECDL recovery budget exhausted; outputs held')
        begin_recovery()
        context = outputs(); baseline = context['current']; current_policy = current.policy
        emit('state', dict(state='mapping_current', tec_held=context['temperature'], piezo_held=context['piezo']))
        active_path = []
        def learned(restored):
            nonlocal active_path
            route=dict(source_mode=o.mode_id,source_frequency_hz=o.frequency.frequency_hz,
                       target_mode=desired,start=context,end=outputs(),time_s=clock(),
                       path=list(active_path),search_direction=p['recovery_direction'],calibration={k:asdict(a.policy) for k,a in axes.items()})
            routes.append(route);emit('ecdl_route_learned',route)
            active_path=None;end_recovery();return restored
        if p['recovery_direction'] is not None:
            # Preparation may go backwards; the search itself has one direction.
            # A failed candidate never causes a hidden return/reversal.
            direction=1 if p['recovery_direction']=='up' else -1
            increment=p['current_step_fraction']*current_policy.span
            distance=p['current_span_fraction']*current_policy.span
            start=max(current_policy.low,min(current_policy.high,
                baseline-direction*p['recovery_preparation_steps']*increment))
            emit('ecdl_recovery_preparation',dict(direction=p['recovery_direction'],baseline=baseline,
                start=start,reference_frequency_hz=branch_reference,tec_held=context['temperature']))
            move('current',start);settled('directional recovery preparation',current_policy.settle_s)
            position=outputs()['current']
            emit('ecdl_recovery_search_start',dict(direction=p['recovery_direction'],start=position,
                reference_frequency_hz=branch_reference))
            stable=p['stable_s'] if p['recovery_stable_s'] is None else p['recovery_stable_s']
            for step in range(p['map_steps']):
                candidate=max(current_policy.low,min(current_policy.high,
                    position+direction*increment))
                if abs(candidate-baseline)>distance or abs(candidate-position)<=max(current_policy.resolution/2,1e-12):
                    emit('ecdl_map_limit',dict(direction=direction,kind='directional_search_limit',value=candidate));break
                emit('ecdl_recovery_candidate',dict(direction=direction,value=candidate,baseline=baseline,
                    procedure='directional_search'))
                move('current',candidate);position=outputs()['current']
                settled('directional recovery candidate',current_policy.settle_s)
                restored=verify('directional recovery validation',require_reachable=True,stable_s=stable)
                emit('ecdl_map_trial',dict(direction=direction,excursion=position,baseline=baseline,
                    restored=bool(restored),path=list(active_path),procedure='directional_search'))
                if restored:return learned(restored)
            active_path=None
            raise DeviceError('No verified current recovery path inside directional search budget; outputs held')
        # Replay only measured-frequency-compatible routes in the same calibrated local context.
        for route in list(routes):
            compatible = (clock() - route['time_s'] <= p['map_ttl_s']
                and frequency_valid(o) and route.get('source_frequency_hz') is not None
                and abs(route['source_frequency_hz']-o.frequency.frequency_hz)<=5*p['tolerance_hz']
                and all(abs(context[k] - route['start'][k]) <= axes[k].policy.span * p['map_context_fraction']
                        for k in axes))
            if compatible:
                emit('ecdl_route_replay', dict(route=route))
                for command in route['path']:
                    move(command['axis'], command['value'])
                settled('route replay settling', current_policy.settle_s)
                restored = verify('route replay validation',require_reachable=True)
                if restored:
                    active_path = None
                    end_recovery()
                    return restored
                move('current', baseline)
                settled('failed replay return', current_policy.settle_s)
        # A recorded approach is an exploratory candidate at the new TEC value,
        # never proof of branch identity. Revalidate frequency, cleanliness and
        # TEC reach before retaining its bias or advancing temperature.
        if known_bias is not None and p['initial_map_conditioning_fractions']:
            emit('ecdl_conditioning_recovery',dict(stage='started',context=context))
            for fraction in p['initial_map_conditioning_fractions']:
                value=(current_policy.low+fraction*current_policy.span if p['initial_map_direction']=='up'
                       else current_policy.high-fraction*current_policy.span)
                move('current',value);settled('conditioning recovery waypoint',current_policy.settle_s)
            move('current',known_bias);settled('conditioning recovery bias',current_policy.settle_s)
            restored=verify('conditioning recovery validation',require_reachable=True)
            emit('ecdl_conditioning_recovery',dict(stage='validated' if restored else 'failed',outputs=outputs()))
            if restored:return learned(restored)
            move('current',baseline);settled('failed conditioning return',current_policy.settle_s)
        # Alternating growing round trips distinguish a coordinate from its history.
        for step in range(1, p['map_steps'] + 1):
            for direction in (-1, 1):
                distance = min(step * p['current_step_fraction'], p['current_span_fraction']) * current_policy.span
                candidate = baseline + direction * distance
                if not current_policy.low <= candidate <= current_policy.high:
                    emit('ecdl_map_limit', dict(direction=direction, kind='device_limit', value=candidate))
                    continue
                emit('ecdl_recovery_candidate',dict(direction=direction,value=candidate,baseline=baseline))
                move('current', candidate)
                settled('current excursion', current_policy.settle_s)
                restored = verify('current candidate validation',require_reachable=True)
                if not restored:
                    move('current', baseline)
                    settled('current return', current_policy.settle_s)
                    restored = verify('current return validation',require_reachable=True)
                emit('ecdl_map_trial', dict(direction=direction, excursion=candidate, baseline=baseline,
                                           restored=bool(restored), path=list(active_path)))
                if restored:
                    # Keep a validated bias, never restore an unverified old one.
                    return learned(restored)
        active_path = None
        raise DeviceError('No verified current recovery path inside mapping budget; outputs held')

    def initial_map():
        """Map the permitted current interval with TEC/PZT frozen; rank windows.

        Split windows on a frequency discontinuity. A window is only a measured
        interval in this scan direction, not a proof of longitudinal mode identity.
        """
        nonlocal active_path,desired,known_bias
        begin_recovery();active_path=[]
        policy=current.policy;windows=[];window=[];previous=None;scan_points=[]
        start_fraction=p['initial_map_start_fraction']
        end_fraction=p['initial_map_end_fraction']
        count=math.ceil((end_fraction-start_fraction)/p['initial_map_step_fraction'])
        for fraction in p['initial_map_conditioning_fractions']:
            value=policy.low+fraction*policy.span if p['initial_map_direction']=='up' else policy.high-fraction*policy.span
            move('current',value)
            o=settled('current map conditioning',policy.settle_s)
            emit('ecdl_map_conditioning_point',dict(current=outputs()['current'],fraction=fraction,
                 frequency_hz=o.frequency.frequency_hz,impurity=o.impurity,held=outputs()))
        emit('state',dict(state='initial_current_map',held=outputs(),points=count+1))
        for n in range(count+1):
            fraction=start_fraction+n/count*(end_fraction-start_fraction)
            value=policy.low+fraction*policy.span if p['initial_map_direction']=='up' else policy.high-fraction*policy.span
            move('current',value)
            o=settled('initial current map settling',policy.settle_s)
            if o.frequency.frequency_hz is not None and not frequency_valid(o):
                o=read('current map frequency validation',require_frequency=True,allow_timeout=True) or o
            clean=frequency_valid(o) and o.single_mode and not o.maintenance_required
            clean=clean and abs(o.frequency.frequency_hz-p['target_frequency_hz'])<=p['initial_mode_capture_hz']
            entry=dict(outputs=outputs(),quality=o.frequency.quality,current=outputs()['current'],direction=p['initial_map_direction'],frequency_hz=o.frequency.frequency_hz,
                       clean=bool(clean),impurity=o.impurity,mode_id=o.mode_id,metrics=dict(o.metrics),fringe_analysis=dict(o.fringe_analysis))
            scan_points.append(entry)
            emit('ecdl_initial_map_point',entry)
            # Adjacent clean samples must also have modest frequency separation.
            if not clean or previous is not None and abs(o.frequency.frequency_hz-previous)>p['initial_mode_capture_hz']*.1:
                if window:windows.append(window)
                window=[]
            if clean:window.append(entry);previous=o.frequency.frequency_hz
            else:previous=None
        from ldcon.jobs.fringe_sweep_reference import analyze_sweep
        emit('ecdl_sweep_reference',analyze_sweep(scan_points,discontinuity_hz=p['initial_mode_capture_hz']*.1))
        if window:windows.append(window)
        windows=[w for w in windows if len({x['current'] for x in w})>=2]
        for w in windows:
            emit('ecdl_current_window',dict(low=min(w[0]['current'],w[-1]['current']),high=max(w[0]['current'],w[-1]['current']),direction=p['initial_map_direction'],samples=w))
        if not windows:raise DeviceError('No resolved clean near-target current window; outputs held; inspect current map')
        # Widest resolved window first, then proximity to the target.
        windows.sort(key=lambda w:(-abs(w[-1]['current']-w[0]['current']),
            min(abs(x['frequency_hz']-p['target_frequency_hz']) for x in w)))
        reachable_windows=0
        for w in windows:
            selected=w[len(w)//2]
            shift=[(edge-origin['temperature'])*temperature.policy.frequency_hz_per_unit
                   for edge in (temperature.policy.low,temperature.policy.high)]
            reachable=[x for x in w if min(shift)-p['tolerance_hz']<=p['target_frequency_hz']-x['frequency_hz']<=max(shift)+p['tolerance_hz']]
            if not reachable:
                emit('ecdl_window_unreachable',dict(low=w[0]['current'],high=w[-1]['current'],reason='Target outside calibrated TEC reach'))
                continue
            reachable_windows+=1
            selected=min(reachable,key=lambda x:abs(x['current']-(w[0]['current']+w[-1]['current'])/2))
            if p['initial_map_replay_start']:
                # Re-enter the branch using its actual discovery path, rather
                # than assuming the window edge resets its hysteretic state.
                emit('ecdl_initial_approach_replay',dict(direction=p['initial_map_direction'],
                    start=scan_points[0]['current'],target=selected['current']))
                for fraction in p['initial_map_conditioning_fractions']:
                    value=policy.low+fraction*policy.span if p['initial_map_direction']=='up' else policy.high-fraction*policy.span
                    move('current',value);settled('window replay conditioning',policy.settle_s)
                for point in scan_points:
                    move('current',point['current']);settled('window discovery path replay',policy.settle_s)
                    if point['current']==selected['current']:break
            else:
                # Legacy short return; direction alone does not reproduce history.
                move('current',w[0]['current']);settled('window return edge',policy.settle_s)
                move('current',selected['current']);settled('window center return',policy.settle_s)
            desired=selected['mode_id']
            result=verify('initial current window validation',require_reachable=True)
            if result:
                # Returning along a hysteretic path can select another branch.
                # Reachability must use the returned measurement, not the map.
                if not min(shift)-p['tolerance_hz']<=p['target_frequency_hz']-result.frequency.frequency_hz<=max(shift)+p['tolerance_hz']:
                    emit('ecdl_window_unreachable',dict(low=min(w[0]['current'],w[-1]['current']),high=max(w[0]['current'],w[-1]['current']),reason='Returned branch outside calibrated TEC reach'))
                    continue
                emit('ecdl_current_window_selected',dict(low=min(w[0]['current'],w[-1]['current']),high=max(w[0]['current'],w[-1]['current']),direction=p['initial_map_direction'],center=outputs()['current'],mode_id=desired,returned_frequency_hz=result.frequency.frequency_hz,reference_frequency_hz=selected['frequency_hz']))
                known_bias=outputs()['current']
                active_path=None;end_recovery();return result
        if not reachable_windows:
            raise DeviceError('No mapped clean current window within calibrated TEC reach; outputs held')
        raise DeviceError('Mapped current windows did not reproduce; outputs held')

    check(); origin.update(outputs())
    begin_recovery()
    first = read('initial point')
    if desired is None and not p['initial_current_map']:
        if not frequency_valid(first) or not first.single_mode:
            raise DeviceError('Initial state is multimode; specify desired_mode before mapping')
        desired = first.mode_id
    center = piezo.policy.low + p['center_fraction'] * piezo.policy.span
    band = p['center_tolerance_fraction'] * piezo.policy.span
    emit('ecdl_prelock_start', dict(target_frequency_hz=p['target_frequency_hz'], desired_mode=desired,
                                  origin=origin, calibration={k: asdict(a.policy) for k, a in axes.items()}))
    if p['initial_current_map']:
        first=initial_map()
    elif not good(first):
        first=recover(first)
    else:end_recovery()
    if abs(origin['piezo'] - center) > band:
        move('piezo', center)
        settled('piezo centering', piezo.policy.settle_s)
    current_obs = read('before TEC approach')
    while True:
        check()
        if not good(current_obs) or not reachable(current_obs):
            current_obs = recover(current_obs)
        error = p['target_frequency_hz'] - current_obs.frequency.frequency_hz
        if abs(error) <= p['tolerance_hz']:
            final = verify('initial operating point validation', require_target=True)
            if final and abs(p['target_frequency_hz'] - final.frequency.frequency_hz) <= p['tolerance_hz']:
                if abs(outputs()['piezo'] - center) > band:
                    raise DeviceError('Piezo left the center band; outputs held')
                result = dict(status='succeeded', target_frequency_hz=p['target_frequency_hz'],
                              final_frequency_hz=final.frequency.frequency_hz, desired_mode=desired,
                              outputs=outputs(), output_units={k:a.policy.unit for k,a in axes.items()},
                              output_writes=writes, recoveries=recoveries, map_points=points, routes=routes,
                              lock_enabled=False)
                emit('ecdl_initial_point', result)
                return result
            current_obs = read('after target validation'); continue
        slope = temperature.policy.frequency_hz_per_unit
        before = outputs()['temperature']
        low,high=tec_bounds()
        delta = p['approach_gain'] * error / slope
        limit = min(temperature.policy.max_step,
                    temperature.policy.max_slew_per_s * temperature.policy.update_interval_s)
        candidate = max(low, min(high, before + math.copysign(min(abs(delta), limit), delta)))
        if temperature.policy.resolution:
            quantum=temperature.policy.resolution
            candidate=round(candidate/quantum)*quantum
            if abs(candidate-before)<quantum*.5 and limit+1e-12>=quantum:
                candidate=before+math.copysign(quantum,delta)
            candidate=max(low,min(high,candidate))
        if abs(candidate - before) <= max(1e-12, temperature.policy.span * 1e-12):
            raise DeviceError('TEC excursion exhausted before acquisition; outputs held')
        emit('state', dict(state='approaching_tec', error_hz=error))
        move('temperature', candidate)
        # Observe throughout settling; stop TEC progression on any mode loss.
        until = clock() + temperature.policy.settle_s
        while True:
            pause(p['sample_interval_s']); current_obs = read('TEC settling')
            if not good(current_obs) or clock() >= until:
                break
