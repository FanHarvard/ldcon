"""Finite, device-neutral same-mode current study with explicit recovery recipes.

Coordinates are fractions of the owner-approved interval. All hardware I/O,
limits and native PI handovers remain in the one instrument owner.
"""
import copy
import math
import re

from ldcon.core.contracts import DeviceError, MeasurementUnavailable
from ldcon.core.interface_cards import CardCancelled


DEFAULTS = dict(target_frequency_hz=None, tolerance_hz=None,
                observables=['dither_response', 'frequency_hz', 'mode_hop'],
                offsets=[0., -.01, -.02, -.01, 0., .01, .02, .01, 0.],
                step_fraction=.0025, dwell_s=25., sample_interval_s=1.,
                measurement_timeout_s=120., timeout_s=1800.,
                hop_threshold_hz=5e7, hop_confirm_samples=3,
                recovery_on=['mode_hop'], recovery_recipes=[], max_recoveries=4,
                recovery_stable_s=5., recovery_tolerance_hz=None,
                recovery_metric_limits={}, resume_after_recovery=True)


def positive(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def parameters(card, overrides):
    if not isinstance(card['parameters'], dict) or not isinstance(overrides, dict):
        raise ValueError('Study parameters must be mappings')
    if (set(card['parameters']) | set(overrides)) - DEFAULTS.keys():
        raise ValueError('Unknown response study parameter')
    p = copy.deepcopy({**DEFAULTS, **card['parameters'], **overrides})
    for key in ('target_frequency_hz', 'tolerance_hz', 'step_fraction', 'dwell_s',
                'sample_interval_s', 'measurement_timeout_s', 'timeout_s',
                'hop_threshold_hz', 'recovery_stable_s', 'recovery_tolerance_hz'):
        if p[key] is None and key in ('target_frequency_hz', 'tolerance_hz', 'recovery_tolerance_hz'):
            continue
        if not positive(p[key]):raise ValueError('Invalid '+key)
    if not .1 <= p['sample_interval_s'] <= 5 or p['timeout_s'] > 86400:
        raise ValueError('Invalid sample interval or timeout')
    if not 0 < p['step_fraction'] <= .25:raise ValueError('Invalid current step fraction')
    if not isinstance(p['offsets'], list) or not 2 <= len(p['offsets']) <= 200:
        raise ValueError('Expected 2..200 current offsets')
    for offset in p['offsets']:
        if type(offset) not in (int, float) or not math.isfinite(offset) or abs(offset) > .25:
            raise ValueError('Current offsets must be in [-0.25, 0.25]')
    for key in ('hop_confirm_samples', 'max_recoveries'):
        if type(p[key]) is not int or not 1 <= p[key] <= 100:raise ValueError('Invalid '+key)
    for key in ('resume_after_recovery',):
        if type(p[key]) is not bool:raise ValueError('Expected boolean '+key)
    names = {'dither_response', 'frequency_hz', 'wavelength_nm', 'error_hz',
             'current', 'piezo', 'temperature', 'single_mode', 'mode_hop'}
    if (not isinstance(p['observables'], list) or not 1 <= len(p['observables']) <= 32
        or any(not isinstance(n, str) or not (n in names or re.fullmatch(r'metrics\.[A-Za-z][A-Za-z0-9_]*', n)) for n in p['observables'])):
        raise ValueError('Unknown observable; use a scalar name or metrics.<name>')
    if not isinstance(p['recovery_on'], list) or set(p['recovery_on']) - {'mode_hop', 'multimode'}:
        raise ValueError('Unknown recovery trigger')
    limits = p['recovery_metric_limits']
    if not isinstance(limits, dict) or any(not isinstance(k, str) or type(v) not in (int,float) or not math.isfinite(v) or v < 0 for k, v in limits.items()):
        raise ValueError('Invalid recovery metric upper limits')
    recipes = p['recovery_recipes']
    if not isinstance(recipes, list) or len(recipes) > 10:raise ValueError('Invalid recovery recipes')
    for recipe in recipes:
        if not isinstance(recipe, list) or not 1 <= len(recipe) <= 20:raise ValueError('Invalid recovery steps')
        for step in recipe:
            if not isinstance(step, dict):raise ValueError('Expected typed recovery step')
            action = step.get('action')
            allowed = {'restore_piezo': {'action'}, 'restore_temperature': {'action'}, 'move_current': {'action', 'offset'},
                       'sweep_current': {'action', 'start_offset', 'end_offset', 'step_fraction', 'settle_s'},
                       'wait': {'action', 'seconds'}}
            if action not in allowed or set(step) != allowed[action]:raise ValueError('Invalid typed recovery step')
            for key, value in step.items():
                if key == 'action':continue
                if 'offset' in key:
                    if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > .5:
                        raise ValueError('Invalid recovery offset')
                elif not positive(value):raise ValueError('Invalid recovery step value')
            if action == 'sweep_current':
                if step['start_offset'] == step['end_offset'] or step['step_fraction'] > .25:
                    raise ValueError('Recovery scan needs a direction and bounded steps')
                if math.ceil(abs(step['end_offset']-step['start_offset'])/step['step_fraction']) > 500:
                    raise ValueError('Recovery scan exceeds 500 points')
    return p


class ResponseStudy:
    def __init__(self, ports, p, *, clock, sleep, cancel, emit):
        self.ports = ports; self.lock = ports['lock']; self.p = p
        self.clock = clock; self.sleep = sleep; self.cancel = cancel; self.emit = emit
        self.started = clock(); self.armed = False; self.rows = []; self.recoveries = 0
        self.segment = 0; self.last_stamp = None

    def check(self):
        if self.cancel():raise CardCancelled('Response study cancelled; no blind return')
        if self.clock()-self.started > self.p['timeout_s']:raise DeviceError('Response study timeout')
        if self.armed:self.lock.heartbeat()

    def wait(self, seconds):
        end = self.clock()+seconds
        while self.clock() < end:
            self.check()
            remaining=end-self.clock()
            if remaining<=0:break
            self.sleep(min(self.p['sample_interval_s'], remaining))

    def info(self):
        began = self.clock()
        while True:
            self.check()
            try:
                row = self.lock.response_info()
                stamp = row['observation']['frequency'].get('acquired_unix_s')
                if stamp is None:stamp = row['observation']['frequency']['acquired_s']
                if self.last_stamp is not None and stamp <= self.last_stamp:
                    raise MeasurementUnavailable('Repeated study acquisition')
                self.last_stamp = stamp
                return row
            except MeasurementUnavailable as error:
                self.emit('measurement_rejected', dict(stage='response study', reason=str(error)))
                if self.clock()-began >= self.p['measurement_timeout_s']:raise
                self.wait(self.p['sample_interval_s'])

    def move_open(self, axis, target):
        port = self.ports[axis]; policy = port.policy
        if policy.resolution:target = round(target/policy.resolution)*policy.resolution
        if not policy.low <= target <= policy.high:raise DeviceError('Recovery target outside approved interval')
        while True:
            self.check()
            current = self.info()['outputs'][axis]
            if abs(current-target) <= max(policy.resolution/2, 1e-10):return
            self.wait(policy.update_interval_s)
            maximum = min(policy.max_step, policy.max_slew_per_s*policy.update_interval_s)
            delta = max(-maximum, min(maximum, target-current))
            if policy.resolution:
                delta = math.copysign(math.floor((abs(delta)+1e-12)/policy.resolution)*policy.resolution, delta)
            if not delta:raise DeviceError('Recovery step below axis resolution')
            # Only a confirmed pre-write measurement rejection can be retried.
            try:port.move(current+delta, policy.update_interval_s)
            except MeasurementUnavailable:
                if port.uncertain_output:raise
                continue
            self.emit('command_applied', dict(interface=axis, value=current+delta, phase='recovery'))
            self.wait(policy.settle_s)

    def recovery_state(self, enabled):
        start=self.clock()
        while True:
            self.check()
            try:
                port=self.ports['current']
                return port.begin_recovery() if enabled else port.end_recovery()
            except MeasurementUnavailable as error:
                if any(getattr(port,'uncertain_output',False) for port in self.ports.values()):raise
                if self.clock()-start>=self.p['measurement_timeout_s']:raise
                self.emit('ecdl_response_recovery_wait',dict(reason=str(error),stage='recovery state qualification'))
                self.wait(self.p['sample_interval_s'])


    def arm(self):
        self.lock.check_ready('hold'); self.armed = True
        tolerance=(self.p['recovery_tolerance_hz'] or self.p['tolerance_hz']) if self.recoveries else self.p['tolerance_hz']
        self.lock.arm(self.p['target_frequency_hz'], tolerance, 'hold')
        requirements=self.lock.response_begin()
        if self.p['dwell_s']<requirements['minimum_dwell_s']:
            raise DeviceError('Study dwell is shorter than owner settling and passive acquisition')
        self.emit('state',dict(state='mapping',segment=self.segment))

    def hold(self, reason):
        if self.armed:
            result = self.lock.hold(reason); self.armed = False
            self.emit('ecdl_response_hold', result)

    def good_reference(self, row):
        obs = row['observation']; sample = obs['frequency']; frequency = sample['frequency_hz']
        tolerance = self.p['recovery_tolerance_hz'] or self.p['tolerance_hz']
        # v5.1-hostcheck (human decision 2026-10-22): wavemeter quality/status/aligned labels are informational.
        return (type(frequency) in (int, float)
                and math.isfinite(frequency) and obs['single_mode']
                and not obs['maintenance_required'] and obs['mode_id'] == self.mode_id
                and abs(frequency-self.reference_frequency) <= tolerance
                and all(type(obs['metrics'].get(k)) in (int, float)
                        and math.isfinite(obs['metrics'][k]) and obs['metrics'][k] <= v
                        for k, v in self.p['recovery_metric_limits'].items()))

    def verify_reference(self):
        start = self.clock(); stable = None; count = 0;previous_stamp=None
        other_since=None;other_count=0
        while self.clock()-start < self.p['measurement_timeout_s']:
            row = self.info()
            # Compare only in the original physical context, never combine branches.
            for axis in ('piezo', 'temperature', 'current'):
                tolerance = max(self.ports[axis].policy.resolution, 1e-8)
                if abs(row['outputs'][axis]-self.reference[axis]) > tolerance:
                    raise DeviceError('Original-mode verification context differs: '+axis)
            if self.good_reference(row):
                stamp = self.last_stamp
                if previous_stamp is not None and stamp-previous_stamp > max(3*self.p['sample_interval_s'],1.):
                    stable=None;count=0
                if stable is None:stable = stamp; count = 0
                count += 1
                if stamp-stable >= self.p['recovery_stable_s'] and count >= 3:return row
            else:stable = None; count = 0
            # A fresh, persistent *different* branch after a settled directed
            # approach should advance to the next recipe, not wait through the
            # full missing-measurement budget. Small same-label drift and read
            # rejection are not this evidence.
            if self.lost(row) and row['observation']['mode_id']!=self.mode_id:
                if other_since is None:other_since=self.last_stamp
                other_count+=1
                if (other_count>=self.p['hop_confirm_samples']
                    and self.last_stamp-other_since>=self.p['recovery_stable_s']):
                    raise MeasurementUnavailable('Directed approach remained on a different optical branch')
            else:other_since=None;other_count=0
            previous_stamp=self.last_stamp
            self.wait(self.p['sample_interval_s'])
        raise MeasurementUnavailable('Original optical branch was not verified')

    def recover(self, reason):
        self.recoveries += 1
        if not getattr(self,'stationary',False):self.emit('state',dict(state='recovering',reason=reason))
        self.emit('ecdl_response_recovery_start', dict(reason=reason, attempt=self.recoveries, segment=self.segment))
        self.hold('response study recovery')
        if self.p['max_recoveries'] is not None and self.recoveries > self.p['max_recoveries']:
            raise DeviceError('Response study recovery budget exceeded')
        for number, recipe in enumerate(self.p['recovery_recipes']):
            self.emit('ecdl_response_recovery_recipe', dict(recipe=number, steps=recipe, reference=self.reference))
            try:
                for step in recipe:
                    self.check(); action = step['action']
                    if action in ('restore_piezo','restore_temperature'):
                        self.recovery_state(False)
                        axis='piezo' if action=='restore_piezo' else 'temperature'
                        self.move_open(axis, self.reference[axis])
                    elif action == 'wait':self.wait(step['seconds'])
                    else:
                        self.recovery_state(True)
                        if action == 'move_current':
                            self.move_open('current', self.origin+step['offset']*self.width)
                        else:
                            a = self.origin+step['start_offset']*self.width
                            b = self.origin+step['end_offset']*self.width
                            self.move_open('current', a) # preparation is distinct from directed scan
                            count = math.ceil(abs(b-a)/(step['step_fraction']*self.width))
                            for i in range(1, count+1):
                                self.move_open('current', a+(b-a)*i/count)
                                self.wait(step['settle_s'])
                                sample = self.info()
                                self.emit('ecdl_response_recovery_scan', dict(recipe=number, direction='up' if b>a else 'down',
                                    outputs=sample['outputs'], observation=sample['observation']))
                self.verify_reference()
            except MeasurementUnavailable as error:
                if any(getattr(port,'uncertain_output',False) for port in self.ports.values()):raise
                self.emit('ecdl_response_recovery_unverified', dict(recipe=number, reason=str(error)))
                continue
            self.recovery_state(False)
            self.segment += 1
            self.arm()
            self.emit('ecdl_response_recovered', dict(segment=self.segment, outputs=self.reference,
                criterion='verified branch and configured optical limits; PI rearm acknowledged', pi_rearmed=True))
            return
        raise DeviceError('Original mode could not be verified by configured recovery recipes')

    def lost(self, row):
        obs = row['observation']; sample = obs['frequency']; frequency = sample['frequency_hz']
        return (type(frequency) in (int, float) and math.isfinite(frequency)   # quality label informational (v5.1-hostcheck)
                and (abs(frequency-self.p['target_frequency_hz']) > self.p['hop_threshold_hz']
                     or obs['mode_id'] != self.mode_id))

    def record(self, row, point, direction, hop, response=None, event='ecdl_response_point'):
        obs = row['observation']; frequency = obs['frequency']['frequency_hz']
        base = dict(**row['outputs'], frequency_hz=frequency,
                    wavelength_nm=299792458e9/frequency if frequency and frequency > 0 else None,
                    error_hz=frequency-self.p['target_frequency_hz'] if frequency else None,
                    single_mode=obs['single_mode'], mode_hop=hop, dither_response=response)
        values = {name: obs['metrics'].get(name[8:]) if name.startswith('metrics.') else base[name]
                  for name in self.p['observables']}
        result = dict(point=point, segment=self.segment, direction=direction, values=values,
                      outputs=row['outputs'], observation=obs, mode_hop=hop,
                      elapsed_s=self.clock()-self.started)
        self.rows.append(result)
        fields = {name.replace('.', '_'): value for name, value in values.items()
                  if type(value) in (int, float, bool) and (type(value) is bool or math.isfinite(value))}
        self.emit(event, result, fields=fields, tags=dict(direction=direction, segment=str(self.segment)))
        self.emit('trace', dict(frequency_hz=frequency, wavelength_nm=base['wavelength_nm'],
                  error_hz=-base['error_hz'] if base['error_hz'] is not None else None,
                  outputs=row['outputs'], fringe_metrics=obs['metrics'], single_mode=obs['single_mode'],
                  locked=self.armed and not hop, acquired_s=self.last_stamp))

    def run(self):
        try:
            first = self.info(); self.reference = dict(first['outputs']); self.origin = self.reference['current']
            self.mode_id = first['observation']['mode_id']
            self.reference_frequency = first['observation']['frequency']['frequency_hz']
            policy = self.ports['current'].policy; self.width = policy.high-policy.low
            if not self.good_reference(first):raise DeviceError('Study requires a verified single-mode initial point')
            if abs(self.reference_frequency-self.p['target_frequency_hz']) > self.p['tolerance_hz']:
                raise DeviceError('Study initial point outside target tolerance')
            # Validate every planned and recovery destination before the first write.
            offsets = list(self.p['offsets'])+[0.]
            for recipe in self.p['recovery_recipes']:
                for step in recipe:
                    offsets += [value for key, value in step.items() if 'offset' in key]
            if any(not policy.low <= self.origin+x*self.width <= policy.high for x in offsets):
                raise DeviceError('Study or recovery destination exceeds approved current interval')
            if self.width*self.p['step_fraction'] < policy.resolution:
                raise DeviceError('Study step below current resolution')
            self.emit('ecdl_response_reference', dict(outputs=self.reference, observation=first['observation'], parameters=self.p,
                      current_unit=policy.unit, current_interval=[policy.low, policy.high]))
            self.arm(); previous = self.origin
            for point, offset in enumerate(self.p['offsets']):
                target = self.origin+offset*self.width
                if policy.resolution:target = round(target/policy.resolution)*policy.resolution
                direction = 'held' if abs(target-previous)<=max(policy.resolution/2,1e-10) else 'up' if target>previous else 'down'
                stable = None; hop_count = 0; recovered = False
                while True:
                    row = self.info(); obs = row['observation']
                    temperature = self.ports['temperature'].policy
                    if abs(row['outputs']['temperature']-self.reference['temperature']) > max(temperature.resolution, 1e-8):
                        raise DeviceError('TEC changed during same-mode study')
                    hop_count = hop_count+1 if self.lost(row) else 0
                    hop = hop_count >= self.p['hop_confirm_samples']
                    multimode = not obs['single_mode'] or obs['maintenance_required']
                    reason = 'mode_hop' if hop else 'multimode' if multimode and not hop_count and 'multimode' in self.p['recovery_on'] else None
                    if reason:
                        self.record(row, point, direction, hop, event='ecdl_response_boundary')
                        if reason not in self.p['recovery_on']:raise DeviceError('Mode loss without configured recovery')
                        self.recover(reason);recovered = True;break
                    if hop_count:
                        self.wait(self.p['sample_interval_s']);continue # no changes on unconfirmed hop
                    try:
                        native=self.lock.read_cycle()
                    except MeasurementUnavailable as error:
                        self.emit('ecdl_response_native_wait',dict(reason=str(error),point=point))
                        stable=None;self.wait(self.p['sample_interval_s']);continue
                    if native['state']!='locked':
                        raise DeviceError('Native response-study supervision lost: '+native.get('reason',native['state']))
                    self.emit('ecdl_response_sample',dict(point=point,segment=self.segment,direction=direction,
                        outputs=row['outputs'],observation=obs,acquired_s=self.last_stamp))
                    current = row['outputs']['current']
                    if abs(current-target) > max(policy.resolution/2, 1e-10):
                        delta = max(-self.width*self.p['step_fraction'], min(self.width*self.p['step_fraction'], target-current))
                        try:self.lock.response_current(current+delta)
                        except MeasurementUnavailable:
                            self.wait(self.p['sample_interval_s']);continue
                        stable = None
                    else:
                        if stable is None:stable = self.clock()
                        if self.clock()-stable >= self.p['dwell_s']:
                            try:response = self.lock.response_dither() if 'dither_response' in self.p['observables'] else None
                            except MeasurementUnavailable as error:
                                self.emit('ecdl_response_dither_wait', dict(reason=str(error), point=point))
                                self.wait(self.p['sample_interval_s']);continue
                            self.record(row, point, direction, False, response);break
                    self.wait(self.p['sample_interval_s'])
                if recovered and not self.p['resume_after_recovery']:break
                previous = self.origin if recovered else target
            # Return and verify through an explicit recipe, rather than assuming
            # a closed-loop wavelength means the original mode was preserved.
            self.recover('final original-mode verification')
            return dict(status='succeeded', points=self.rows, recoveries=self.recoveries,
                        lock_enabled=False, original_mode_verified=True)
        finally:
            self.hold('response study exit; hold actual DAC')


def run_response_study(ports, p, **arguments):
    return ResponseStudy(ports, p, **arguments).run()
