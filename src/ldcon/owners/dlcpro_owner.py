"""DLC instrument authority and laser control, kept separate even in one host.

No server starts on import. Tests supply an in-memory driver and optical source.
One Instrument owns the entire chassis; LaserOwner owns one leased channel and
its laser policy. An ambiguous write faults the channel and retains its lease.
"""
from copy import deepcopy
from dataclasses import asdict
import math
import threading
import time
import uuid

from ldcon.core.contracts import DeviceError, MeasurementUnavailable
from ldcon.core.interface_schema import digest
from ldcon.drivers.dlcpro import DLCPro, UncertainDLCWrite
from ldcon.owners.ecdl_prelock import AxisPolicy
from ldcon.owners.dlcpro_ff import mode as ff_mode, validate_binding, require_preserved, preserved_currents, inward_piezo_return


def number(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError('Expected finite number')
    return float(value)


def interval(bounds):
    if not isinstance(bounds, list) or len(bounds) != 2 or number(bounds[0]) >= number(bounds[1]):
        raise ValueError('Expected increasing bounds')
    return bounds


def inside(value, bounds):
    low, high = interval(bounds)
    if not low <= number(value) <= high:
        raise ValueError('Value outside approved bounds')


def scan_frequency_tolerance(config):
    """Explicit characterization allowance; exact verification remains default."""
    value = number(config.get('scan_frequency_readback_tolerance_hz', 0.))
    if value < 0:
        raise ValueError('Negative native scan frequency readback tolerance')
    return value


def policy_check(policy, hardware):
    ff_mode(policy)
    for axis, (_, _, unit) in DLCPro.AXES.items():
        p = policy['axes'][axis]
        bounds = interval(p['bounds'])
        for endpoint in bounds:
            inside(endpoint, hardware['axes'][axis]['bounds'])
        if p['unit'] != unit:
            raise ValueError('DLC axis unit mismatch')
        for key in ('readback_tolerance', 'settle_s'):
            if number(p[key]) < 0:
                raise ValueError('Negative tolerance/time')
        if number(p['update_interval_s']) <= 0:
            raise ValueError('Nonpositive interval')
        if p['frequency_hz_per_unit'] is not None and number(p['frequency_hz_per_unit']) == 0:
            raise ValueError('Zero calibration')
    interval(policy['ff_factor_bounds_ma_per_v'])
    if type(policy['ff_enabled']) is not bool:
        raise ValueError('FF enabled must be boolean')
    inside(policy['ff_factor_ma_per_v'], policy['ff_factor_bounds_ma_per_v'])
    for key in ('max_sample_age_s', 'measurement_timeout_s', 'client_timeout_s'):
        if number(policy[key]) <= 0:
            raise ValueError('Nonpositive freshness deadline')
    if policy.get('lock_input_max_error_hz') is not None:
        if number(policy['lock_input_max_error_hz']) <= 0:
            raise ValueError('Positive lock input frequency window required')
    for key, value in policy['quality_limits'].items():
        if not isinstance(key, str):
            raise ValueError('Quality metric needs a name')
        number(value)
    return deepcopy(policy)


def policy_from_device(device):
    """Compose the laser's two classified sections without duplicating bounds."""
    policy=deepcopy(device['control_settings']['policy'])
    safety=device['absolute_conditions']
    if 'quality_limits' in policy:
        raise ValueError('Quality thresholds belong only in optical.single_mode_max')
    policy['quality_limits']=deepcopy(device['control_settings']['optical']['single_mode_max'])
    for axis in DLCPro.AXES:
        if 'bounds' in policy['axes'][axis]:
            raise ValueError('Put axis bounds only in absolute_conditions')
        policy['axes'][axis]['bounds']=deepcopy(safety['axes'][axis]['bounds'])
    if 'ff_factor_bounds_ma_per_v' in policy:
        raise ValueError('Put FF allowed bounds only in absolute_conditions')
    policy['ff_factor_bounds_ma_per_v']=deepcopy(safety['ff_factor_bounds_ma_per_v'])
    return policy


class Instrument:
    def __init__(self, driver, hardware, *, audit=lambda event: None):
        self.driver = driver
        self.hardware = deepcopy(hardware)
        self.audit = audit
        self.channels = {}
        self.mutex = threading.RLock()
        self.readback = None
        self.confirmed_outputs = {}
        self.last_write_timing = None
        # Hardware configuration is not a laser document. Mandatory channel
        # ownership and vendor output-filter commissioning are explicit.
        for channel, config in hardware['channels'].items():
            if str(int(channel)) != str(channel) or not config['laser_id']:
                raise ValueError('Invalid hardware channel assignment')
            if config.get('output_filters_verified') is not True:
                raise ValueError('DLC hardware slew/output filters need commissioning')
            if config.get('ff_reference_v') is not None:
                number(config['ff_reference_v'])
            scan_frequency_tolerance(config)

    def claim(self, channel, laser_id, policy):
        with self.mutex:
            config = self.hardware['channels'][str(channel)]
            if config['laser_id'] != laser_id or channel in self.channels:
                raise DeviceError('DLC channel unavailable for this laser')
            checked = policy_check(policy, self.hardware)
            validate_binding(checked, config)
            token = uuid.uuid4().hex
            self.channels[channel] = dict(lease=token, laser_id=laser_id, policy=checked,
                                          revision=digest(checked), fault=None)
            self.audit(dict(event='claim', channel=channel, laser_id=laser_id))
            return token

    def _state(self, channel, token):
        state = self.channels.get(channel)
        if state is None or state['lease'] != token:
            raise DeviceError('DLC ownership lost')
        if state['fault']:
            raise DeviceError('DLC channel quarantined: ' + state['fault'])
        return state

    def snapshot(self, channel, token):
        with self.mutex:
            self._state(channel, token)
            result = self.driver.snapshot(channel)
            self.confirmed_outputs[channel] = {axis:result[axis]['setpoint'] for axis in DLCPro.AXES}
            return result

    def servo_piezo(self, channel, token, feedback):
        """PI-only write: absolute PZT bounds, fresh feedback and verified I/O.

        FF/current envelopes are checked at admission and for explicit moves,
        not re-read/re-evaluated in every main-servo cycle.
        """
        with self.mutex, self.driver.control_transaction():
            state = self._state(channel, token)
            value, prewrite = feedback()
            inside(value, state['policy']['axes']['piezo']['bounds'])
            inside(value, self.hardware['axes']['piezo']['bounds'])
            prewrite()
            return self._write(channel, token, self.driver.path(channel, 'pc:voltage-set'),
                               value, state['policy']['axes']['piezo']['readback_tolerance'], compact=True)

    def center_temperature(self, channel, token, delta, prewrite):
        """Recenter from the last verified temperature command, without a bulk poll."""
        with self.mutex, self.driver.control_transaction():
            state = self._state(channel, token)
            target = self.confirmed_outputs[channel]['temperature'] + delta
            inside(target, state['policy']['axes']['temperature']['bounds'])
            inside(target, self.hardware['axes']['temperature']['bounds'])
            prewrite()
            return self._write(channel, token, self.driver.path(channel, 'tc:temp-set'),
                               target, state['policy']['axes']['temperature']['readback_tolerance'])

    def _envelope(self, channel, policy, piezo_bounds, current_offset, *, enabled=None, factor=None, snapshot=None, scan_checked=False, allow_inward_return=False):
        enabled = policy['ff_enabled'] if enabled is None else enabled
        factor = policy['ff_factor_ma_per_v'] if factor is None else factor
        config = self.hardware['channels'][str(channel)]
        validate_binding(policy, config)
        if ff_mode(policy) == 'preserve_existing':
            if enabled != policy['ff_enabled'] or factor != policy['ff_factor_ma_per_v']:
                raise ValueError('Preserved FF cannot be reconfigured')
            if not scan_checked and self.driver.read(f'laser{channel}:scan:enabled') is not False:
                raise DeviceError('Preserved FF lock requires native scan stopped')
            snap = self.driver.snapshot(channel) if snapshot is None else snapshot
            for pzt in piezo_bounds:
                inside(pzt, policy['axes']['piezo']['bounds'])
            if not (allow_inward_return and inward_piezo_return(policy['axes']['piezo']['bounds'], snap, piezo_bounds)):
                inside(snap['piezo']['measured'], policy['axes']['piezo']['bounds'])
            for current in preserved_currents(policy, snap, piezo_bounds, current_offset):
                inside(current, policy['axes']['current']['bounds'])
                inside(current, self.hardware['axes']['current']['bounds'])
            return
        reference = config['ff_reference_v']
        for pzt in piezo_bounds:
            inside(pzt, policy['axes']['piezo']['bounds'])
            current = current_offset + (factor * (pzt - reference) if enabled else 0.)
            inside(current, policy['axes']['current']['bounds'])
            inside(current, self.hardware['axes']['current']['bounds'])

    def _write(self, channel, token, path, value, tolerance=0., *, compact=False,
               prewrite=None):
        state = self._state(channel, token)
        tolerance = number(tolerance)
        if tolerance < 0:
            raise ValueError('Negative DLC write readback tolerance')
        started = time.monotonic()
        timing = dict(channel=channel, outcome='intent', intent_audit_s=None,
                      prewrite_check_s=None, driver_s=None, verified_audit_s=None,
                      total_s=None)
        self.last_write_timing = timing
        try:
            self.audit(dict(event='write_intent', channel=channel, parameter=path, value=value,
                            readback_tolerance=tolerance))
        finally:
            timing['intent_audit_s'] = time.monotonic()-started
            timing['total_s'] = time.monotonic()-started
        # This callback must be read-only. Revalidate only after the intent is
        # durable, so a slow audit cannot make a previously fresh proposal stale
        # before send. Every rejection here precedes any driver mutation.
        checked = time.monotonic()
        try:
            if prewrite is not None:
                prewrite()
        except Exception as error:
            timing.update(outcome='not_applied', prewrite_check_s=time.monotonic()-checked,
                          total_s=time.monotonic()-started)
            self.audit(dict(event='write_reviewed_not_applied', channel=channel,
                            parameter=path, value=value, reason='prewrite check rejected',
                            exception_type=type(error).__name__, write_timing=dict(timing)))
            timing['total_s'] = time.monotonic()-started
            raise
        timing['prewrite_check_s'] = time.monotonic()-checked
        sent = time.monotonic()
        timing['outcome'] = 'writing'
        try:
            result = self.driver.write(path, value, tolerance=tolerance, compact=compact)
        except Exception as error:
            state['fault'] = 'write outcome uncertain'
            timing.update(outcome='uncertain', driver_s=time.monotonic()-sent,
                          total_s=time.monotonic()-started)
            self.audit(dict(event='write_uncertain', channel=channel, parameter=path,
                            write_timing=dict(timing)))
            timing['total_s'] = time.monotonic()-started
            raise UncertainDLCWrite('DLC write outcome uncertain; lease retained') from error
        timing.update(outcome='verified', driver_s=time.monotonic()-sent,
                      total_s=time.monotonic()-started)
        verified = time.monotonic()
        try:
            self.audit(dict(event='write_verified', channel=channel, parameter=path, value=result,
                            requested_value=value, readback_tolerance=tolerance,
                            write_timing=dict(timing)))
        finally:
            # This duration is available to the caller's next audit event via
            # last_write_timing; it cannot be known inside the event it times.
            timing['verified_audit_s'] = time.monotonic()-verified
            timing['total_s'] = time.monotonic()-started
        for axis, (setting, _, _) in DLCPro.AXES.items():
            if path == self.driver.path(channel, setting):
                self.confirmed_outputs.setdefault(channel, {})[axis] = result
        return result

    def move(self, channel, token, axis, value, *, prewrite=None, feedback=None):
        with self.mutex:
            state = self._state(channel, token)
            policy = state['policy']
            if axis not in DLCPro.AXES:
                raise ValueError('Unknown DLC axis')
            if feedback is None:inside(value, policy['axes'][axis]['bounds'])
            before = self.snapshot(channel, token)
            if before['tec_fault']:
                raise DeviceError('DLC TEC fault')
            if before['ff_enabled'] != policy['ff_enabled'] or before['ff_factor_ma_per_v'] != policy['ff_factor_ma_per_v']:
                raise DeviceError('DLC FF readback differs from adopted policy')
            if ff_mode(policy)=='preserve_existing' and self.driver.read(f'laser{channel}:scan:enabled') is not False:
                raise DeviceError('Preserved FF lock requires native scan stopped')
            # Compute PI only after blocking hardware readbacks. This callback
            # consumes a new acquisition, not a re-dated old proposal.
            if feedback is not None:value,prewrite=feedback()
            inside(value, policy['axes'][axis]['bounds'])
            pzt = value if axis == 'piezo' else before['piezo']['setpoint']
            current = value if axis == 'current' else before['current']['setpoint']
            self._envelope(channel, policy, [pzt, pzt], current,snapshot=before,scan_checked=True,
                           allow_inward_return=axis=='piezo' and feedback is None)
            path = self.driver.path(channel, DLCPro.AXES[axis][0])
            if prewrite is not None:prewrite()
            return self._write(channel, token, path, value, policy['axes'][axis]['readback_tolerance'])

    def configure_ff(self, channel, token):
        with self.mutex:
            state = self._state(channel, token)
            policy = state['policy']
            snap = self.snapshot(channel, token)
            if ff_mode(policy) == 'preserve_existing':
                require_preserved(policy, snap)
                self._envelope(channel, policy, [snap['piezo']['setpoint']]*2, snap['current']['setpoint'])
                return snap
            if snap['ff_enabled'] == policy['ff_enabled'] and snap['ff_factor_ma_per_v'] == policy['ff_factor_ma_per_v']:
                return snap
            pzt = snap['piezo']['setpoint']
            current = snap['current']['setpoint']
            # Validate all intermediate states BEFORE the first write.
            for enabled, factor in ((False, snap['ff_factor_ma_per_v']),
                                    (False, policy['ff_factor_ma_per_v']),
                                    (policy['ff_enabled'], policy['ff_factor_ma_per_v'])):
                self._envelope(channel, policy, [pzt, pzt], current, enabled=enabled, factor=factor)
            self._write(channel, token, self.driver.path(channel, 'cc:feedforward-enabled'), False)
            self._write(channel, token, self.driver.path(channel, 'cc:feedforward-factor'), policy['ff_factor_ma_per_v'])
            self._write(channel, token, self.driver.path(channel, 'cc:feedforward-enabled'), policy['ff_enabled'])
            return self.snapshot(channel, token)

    def scan(self, channel, token, *, center, amplitude, frequency, shape, enabled):
        with self.mutex:
            state = self._state(channel, token)
            prefix = f'laser{channel}:scan:'
            if not enabled:
                return self._write(channel, token, prefix + 'enabled', False)
            if ff_mode(state['policy']) == 'preserve_existing':
                raise ValueError('Native scan needs separate FF commissioning; preserve mode supports static lock only')
            if shape not in ('triangle', 'sine') or min(number(amplitude), number(frequency)) <= 0:
                raise ValueError('Invalid native scan')
            config = self.hardware['channels'][str(channel)]
            inside(frequency, config['scan_frequency_bounds_hz'])
            frequency_tolerance = scan_frequency_tolerance(config)
            snap = self.snapshot(channel, token)
            if snap['tec_fault'] or snap['ff_enabled'] != state['policy']['ff_enabled'] or snap['ff_factor_ma_per_v'] != state['policy']['ff_factor_ma_per_v']:
                raise DeviceError('Scan environment/FF mismatch')
            self._envelope(channel, state['policy'], [center-amplitude/2, center+amplitude/2], snap['current']['setpoint'])
            if self.driver.read(prefix+'enabled') is not False:
                raise DeviceError('Native scan already enabled')
            # The output selector is installation-specific; never guess its integer.
            for name, value in (('output-channel', config['pzt_scan_output_channel']),
                                ('signal-type', 1 if shape == 'triangle' else 0),
                                ('frequency', frequency), ('amplitude', amplitude),
                                ('offset', center), ('hold', False)):
                applied = self._write(channel, token, prefix+name, value,
                    frequency_tolerance if name == 'frequency' else 0.)
                if name == 'frequency':
                    actual_frequency = applied
                    try:
                        inside(actual_frequency, config['scan_frequency_bounds_hz'])
                        if actual_frequency <= 0:
                            raise ValueError('Nonpositive applied scan frequency')
                    except ValueError as error:
                        state['fault'] = 'Applied scan frequency outside admission; scan remains disabled'
                        self.audit(dict(event='scan_frequency_rejected', channel=channel,
                            requested_value=frequency, actual_value=actual_frequency,
                            readback_tolerance=frequency_tolerance))
                        raise DeviceError(state['fault']) from error
            if self.driver.read(prefix+'unit') != 'V':
                raise DeviceError('Native scan output is not calibrated PZT volts')
            self._write(channel, token, prefix+'enabled', True)
            return dict(enabled=True, shape=shape, center_v=center, amplitude_vpp=amplitude,
                requested_frequency_hz=frequency, applied_frequency_hz=actual_frequency,
                applied_period_s=1/actual_frequency,
                frequency_readback_tolerance_hz=frequency_tolerance)

    def scan_center(self, channel, token, center, amplitude, *, prewrite=None):
        with self.mutex:
            state = self._state(channel, token)
            if ff_mode(state['policy']) == 'preserve_existing':
                raise ValueError('Native scan center is unavailable in preserved FF lock mode')
            snap = self.snapshot(channel, token)
            self._envelope(channel, state['policy'], [center-amplitude/2, center+amplitude/2], snap['current']['setpoint'])
            if prewrite is not None:prewrite()
            return self._write(channel, token, f'laser{channel}:scan:offset', center,
                               state['policy']['axes']['piezo']['readback_tolerance'])

    def project(self, channel, token, expected_revision, policy):
        with self.mutex:
            state = self._state(channel, token)
            if expected_revision != state['revision']:
                raise ValueError('Stale policy projection')
            checked = policy_check(policy, self.hardware)
            if ff_mode(checked) != ff_mode(state['policy']):
                raise ValueError('FF mode change requires a new idle owner lease')
            if ff_mode(checked) == 'preserve_existing' and any(
                    checked[k] != state['policy'][k] for k in ('ff_enabled','ff_factor_ma_per_v')):
                raise ValueError('Preserved FF settings cannot be changed through policy reload')
            snap = self.snapshot(channel, token)
            for axis in DLCPro.AXES:
                inside(snap[axis]['setpoint'], checked['axes'][axis]['bounds'])
            self._envelope(channel, checked, [snap['piezo']['setpoint']]*2, snap['current']['setpoint'])
            state.update(policy=checked, revision=digest(checked))
            return state['revision']

    def release(self, channel, token):
        with self.mutex:
            self._state(channel, token)
            if self.driver.read(f'laser{channel}:scan:enabled') is not False:
                raise DeviceError('Native scan must be confirmed stopped before release')
            del self.channels[channel]
            self.audit(dict(event='release', channel=channel))


class CycleMean:
    """Time-weighted complete cycles, with acquisition timestamps and gap checks.

    A first sample after the boundary is retained for interpolation. Incomplete
    cycles and duplicate source timestamps cannot produce a correction.
    """
    def __init__(self, start, period, max_gap_s):
        self.start, self.period, self.max_gap_s = start, period, max_gap_s
        self.points = []

    def add(self, acquired_s, frequency_hz):
        if self.points and acquired_s <= self.points[-1][0]:
            return None
        self.points.append((acquired_s, frequency_hz))
        end = self.start + self.period
        if acquired_s < end:
            return None
        points = self.points
        valid = len(points) >= 3 and points[0][0] <= self.start and not getattr(self, 'invalid_input', False)
        area = 0.
        for (a, fa), (b, fb) in zip(points, points[1:]):
            left, right = max(a, self.start), min(b, end)
            if left >= right:
                continue
            if b-a > self.max_gap_s:
                valid = False
            fl = fa + (fb-fa)*(left-a)/(b-a)
            fr = fa + (fb-fa)*(right-a)/(b-a)
            area += (fl+fr)*(right-left)/2
        result = dict(start_s=self.start, end_s=end, valid=valid,
                      mean_frequency_hz=area/self.period if valid else None,
                      samples=len(points), gap_limit_s=self.max_gap_s)
        # A gap spanning several cycles is not several independent measurements.
        while self.start + self.period <= acquired_s:
            self.start += self.period
        self.points = points[-2:]
        self.invalid_input = False
        return result


class LaserOwner:
    """Host PI/scan lifecycle. Physical limits are projected to Instrument.

    tick is called by the owner service, never by Web UI. On missing data PI and
    outer integration hold. Fatal/uncertain writes inhibit every further write.
    """
    def __init__(self, instrument, channel, laser_id, policy, observation, *, clock=time.monotonic,
                 audit=lambda event: None, approve=lambda old, new, evidence: False, target_policy=None):
        self.instrument, self.channel, self.laser_id = instrument, channel, laser_id
        self.policy = policy_check(policy, instrument.hardware)
        self.observation, self.clock, self.audit, self.approve = observation, clock, audit, approve
        self.lease = instrument.claim(channel, laser_id, self.policy)
        self.mutex = threading.RLock()
        self.mode = 'idle'
        self.fault = None
        self.target = None
        from ldcon.owners.dlcpro_frequency_window import FrequencyWindow
        self.frequency_window = FrequencyWindow(target_policy, audit)
        self.target_revision = 0
        self.last_acquisition = None
        self.last_valid = clock()
        self.last_client = clock()
        self.integral = 0.
        self.last_tec = clock()
        self.last_observation = None
        self.events = []
        self.scan_spec = None
        self.scan_receipt = None
        self.pending_policy = None
        self.last_pi_write = float('-inf')
        self.applied_pzt = float('nan')
        self.applied_observation = None
        self.applied_error_axis = 0.
        from ldcon.owners.dlcpro_pi_trial import PITrial
        self.pi_trial = PITrial(self)

    def snapshot(self, *, cached=False):
        readback = None
        if cached and self.instrument.readback is not None:
            snap, readback = self.instrument.readback.read(self.channel)
            outputs = deepcopy(self.instrument.confirmed_outputs.get(self.channel, {}))
        else:
            snap = self.instrument.snapshot(self.channel, self.lease)
            outputs = {axis:snap[axis]['setpoint'] for axis in DLCPro.AXES}
        observation=deepcopy(self.last_observation)
        # Read display feedback after slow console readbacks as well. Preserve
        # the acquisition timestamp; this is not a new control/PI receipt.
        if hasattr(self.observation,'read_frequency'):
            try:
                sample=self.read_control();quality=self._quality();metrics=quality.get('metrics',{})
                valid=bool(quality.get('fresh') and quality.get('quality_valid') and
                    all(k in metrics and type(metrics[k]) in (int,float) and math.isfinite(metrics[k]) for k in self.policy['quality_limits']))
                observation=dict(acquired_s=sample.acquired_s,acquired_unix_s=sample.acquired_unix_s,
                    sequence=sample.sequence,frequency_hz=sample.frequency_hz,source_id=sample.source_id,
                    source_quality=sample.quality,metrics=metrics,quality=quality,quality_valid=valid,
                    degraded=any(metrics[k]>v for k,v in self.policy['quality_limits'].items()) if valid else None)
            except (MeasurementUnavailable,OSError):pass
        return dict(outputs=outputs, hardware=snap, hardware_readback=readback,
                    open_loop=self.mode=='idle', mode=self.mode, pi_enabled=self.mode=='lock',
                    fault=self.fault, policy_revision=digest(self.policy), target_frequency_hz=self.target,
                    target_revision=self.target_revision, observation=observation,
                    control=deepcopy(getattr(self, 'control', None)), pi_trial=self.pi_trial.view(),
                    scan_receipt=deepcopy(self.scan_receipt),
                    integral_v=self.integral,
                    last_control_observation=deepcopy(self.last_observation),
                    control_frequency_window=self.frequency_window.view(self.target, max_error_hz=self._lock_input_limit()),
                    observed_s=self.clock(),
                    frequency_input=self.observation.input_snapshot() if hasattr(self.observation,'input_snapshot') else {'transport':'HTTP optical frame'})

    def heartbeat(self):
        self.last_client = self.clock()

    def _healthy(self):
        if self.fault:
            raise DeviceError('DLC laser owner requires inspection')

    def _lock_input_limit(self):
        # Open-loop branch searches and hardware sweeps retain their own
        # admissions. This tighter window protects the active host PI only.
        return self.policy.get('lock_input_max_error_hz') if self.mode == 'lock' else None

    def _validate_frequency(self, f, *, target=None):
        if f.unit != 'Hz' or f.frequency_hz is None or number(f.frequency_hz) <= 0 or not 0 <= self.clock()-f.acquired_s <= self.policy['max_sample_age_s']:
            raise MeasurementUnavailable('No fresh control frequency')
        if not f.acquisition_known:
            raise MeasurementUnavailable('Source acquisition clock is not verified')
        self.frequency_window.check(f, self.target if target is None else target,
                                    max_error_hz=self._lock_input_limit())
        return f

    def read_control(self, *, target=None):
        if hasattr(self.observation,'read_frequency'):
            return self._validate_frequency(self.observation.read_frequency(), target=target)
        return self.read_optical(target=target).frequency

    def read_optical(self, *, target=None):
        o = self.observation.read_mode()
        self._validate_frequency(o.frequency, target=target)
        if not o.quality_valid:
            raise MeasurementUnavailable('Optical quality measurement unavailable')
        for name in self.policy['quality_limits']:
            if name not in o.metrics:
                raise MeasurementUnavailable('Configured optical metric unavailable: '+name)
            number(o.metrics[name])
        return o

    def _quality(self):
        if hasattr(self.observation,'quality_snapshot'):
            return self.observation.quality_snapshot()
        o=self.read_optical()
        return dict(fresh=True,quality_valid=o.quality_valid,metrics=dict(o.metrics),
                    acquired_s=o.frequency.acquired_s,single_mode=o.single_mode,mode_id=o.mode_id)

    def move(self, axis, value):
        self._healthy()
        if self.mode != 'idle':
            raise DeviceError('Direct axis change requires idle owner')
        return self.instrument.move(self.channel, self.lease, axis, value)

    def start_lock(self, target, settings):
        with self.mutex:
            self._healthy()
            if self.mode != 'idle':
                raise DeviceError('Owner already active')
            self._check_control(settings)
            frequency = self.read_control(target=number(target))
            self.target = number(target)
            if self.target <= 0:
                raise ValueError('Target must be positive')
            if abs(frequency.frequency_hz-self.target) > settings['capture_hz']:
                raise ValueError('Outside configured PI capture interval')
            snap = self.snapshot()
            if not snap['hardware']['emission'] or snap['hardware']['tec_fault'] or not snap['hardware']['pzt_enabled'] or not snap['hardware']['tec_enabled']:
                raise DeviceError('DLC emission/TEC not ready')
            self.instrument.configure_ff(self.channel, self.lease)
            self.control = deepcopy(settings)
            self.base = snap['outputs']['piezo']
            self.applied_pzt = self.base
            self.applied_error_axis = 0.
            self.last_observation = dict(frequency_hz=frequency.frequency_hz, acquired_s=frequency.acquired_s)
            self.applied_observation = deepcopy(self.last_observation)
            self.integral = 0.
            self.last_acquisition = frequency.acquired_s
            self.last_valid = self.last_tec = self.last_client = self.clock()
            self.target_revision += 1
            self.mode = 'lock'
            self.audit(dict(event='lock_start', target_frequency_hz=self.target, settings=settings))

    def _check_control(self, p):
        for key in ('kp', 'ki_per_s', 'integral_limit_v', 'capture_hz', 'tolerance_hz',
                    'tec_gain', 'tec_period_s', 'pzt_center_v'):
            number(p[key])
        if min(p['kp'], p['ki_per_s'], p['tec_gain']) < 0 or min(p['integral_limit_v'], p['capture_hz'], p['tolerance_hz'], p['tec_period_s']) <= 0:
            raise ValueError('Invalid control settings')
        inside(p['pzt_center_v'], self.policy['axes']['piezo']['bounds'])
        for axis in ('piezo', 'temperature'):
            if self.policy['axes'][axis]['frequency_hz_per_unit'] is None:
                raise ValueError('Signed axis calibration required')

    def set_target(self, target, expected_revision):
        with self.mutex:
            self._healthy()
            if self.mode != 'lock' or expected_revision != self.target_revision:
                raise ValueError('Target context changed or not locked')
            number(target)
            if target <= 0 or abs(target-self.target) > self.control['capture_hz']:
                raise ValueError('Target update outside reviewed capture range')
            self.pi_trial.preempt('foreground_target')
            from ldcon.owners.dlcpro_target_step import apply
            apply(self,target)
            self.heartbeat()
            return self.target_revision

    def start_scan(self, target, spec):
        with self.mutex:
            self._healthy()
            if self.mode != 'idle':
                raise DeviceError('Scan requires idle owner; no implicit lock stop')
            self._check_control(spec)
            for key in ('period_s', 'span_hz', 'max_gap_s', 'boundary_tolerance_s'):
                if number(spec[key]) <= 0:
                    raise ValueError('Nonpositive scan setting')
            if spec['shape'] not in ('triangle', 'sine') or spec['center_lock'] not in (True, False):
                raise ValueError('Invalid scan mode')
            if spec['max_gap_s'] >= spec['period_s']/2:
                raise ValueError('Scan cycle cannot be observed with this gap limit')
            config = self.instrument.hardware['channels'][str(self.channel)]
            highest_frequency = 1/spec['period_s'] + scan_frequency_tolerance(config)
            if spec['max_gap_s'] >= .5/highest_frequency:
                raise ValueError('Scan cycle cannot be observed at the tolerated frequency')
            frequency = self.read_control(target=number(target))
            self.target = number(target)
            if self.target <= 0:
                raise ValueError('Target must be positive')
            snap = self.snapshot()
            if not snap['hardware']['emission'] or snap['hardware']['tec_fault'] or not snap['hardware']['pzt_enabled'] or not snap['hardware']['tec_enabled']:
                raise DeviceError('DLC emission/TEC not ready')
            self.instrument.configure_ff(self.channel, self.lease)
            self.scan_spec = deepcopy(spec)
            self.control = deepcopy(spec)
            self.scan_center = snap['outputs']['piezo']
            self.scan_amplitude = spec['span_hz']/abs(self.policy['axes']['piezo']['frequency_hz_per_unit'])
            self.scan_receipt = self.instrument.scan(self.channel, self.lease, center=self.scan_center,
                amplitude=self.scan_amplitude, frequency=1/spec['period_s'], shape=spec['shape'], enabled=True)
            start = self.clock()
            self.cycle = CycleMean(start, self.scan_receipt['applied_period_s'], spec['max_gap_s'])
            self.cycle.add(frequency.acquired_s,frequency.frequency_hz)
            self.last_acquisition = frequency.acquired_s
            self.last_valid = self.last_client = self.last_tec = start
            self.mode = 'scan'
            self.target_revision += 1

    def stop(self):
        with self.mutex:
            self._healthy()
            awg=getattr(self,'awg_sweep',None)
            if awg:
                awg.stop()
                if awg.preparing():
                    return self.snapshot(cached=True)
            if self.mode == 'scan':
                self.instrument.scan(self.channel, self.lease, center=0, amplitude=0, frequency=0, shape='triangle', enabled=False)
            self.pi_trial.preempt('lock_stop')
            self.mode = 'idle'
            self.integral = 0.
            self.scan_spec = None
            if self.pending_policy is not None:self._adopt_pending()
            self.audit(dict(event='hold', target_revision=self.target_revision))
            return self.snapshot()

    def reload(self, expected_revision, policy, evidence):
        with self.mutex:
            self._healthy()
            if expected_revision != digest(self.policy):
                raise ValueError('Stale policy revision')
            checked = policy_check(policy, self.instrument.hardware)
            if not self.approve(deepcopy(self.policy), checked, evidence):
                raise PermissionError('Exact policy/calibration changes require trusted approval')
            if self.mode != 'idle':
                # Range/quality changes may be adopted at the next fresh control
                # boundary. A new physical calibration/FF context cannot reuse
                # an active PI integrator or in-flight scan conversion.
                changed_context = ff_mode(checked) != ff_mode(self.policy) or any(checked[k] != self.policy[k] for k in ('ff_enabled','ff_factor_ma_per_v')) or any(checked['axes'][a]['frequency_hz_per_unit'] != self.policy['axes'][a]['frequency_hz_per_unit'] for a in DLCPro.AXES)
                if changed_context:
                    raise ValueError('Calibration/FF context change requires idle boundary')
                self.pending_policy = checked
                return dict(status='pending', revision=digest(checked))
            self.instrument.project(self.channel, self.lease, expected_revision, checked)
            self.policy = checked
            self.audit(dict(event='policy_adopted', revision=digest(checked)))
            return digest(checked)

    def _adopt_pending(self):
        checked = self.pending_policy
        if self.mode == 'scan':
            snap=self.snapshot()
            self.instrument._envelope(self.channel, checked,
                [self.scan_center-self.scan_amplitude/2,self.scan_center+self.scan_amplitude/2],
                snap['outputs']['current'])
        self.instrument.project(self.channel,self.lease,digest(self.policy),checked)
        self.policy=checked
        self.pending_policy=None
        self.audit(dict(event='policy_adopted',revision=digest(checked)))

    def _tec(self, pzt, now):
        if now-self.last_tec < self.control['tec_period_s']:
            return
        # Quality warnings are recorded, never a veto on recentering.
        axes = self.policy['axes']
        delta = (pzt-self.control['pzt_center_v'])*axes['piezo']['frequency_hz_per_unit']/axes['temperature']['frequency_hz_per_unit']*self.control['tec_gain']
        target = self.instrument.confirmed_outputs[self.channel]['temperature']+delta
        if not axes['temperature']['bounds'][0] <= target <= axes['temperature']['bounds'][1]:
            self.audit(dict(event='tec_centering_deferred',reason='approved temperature bound'))
            self.last_tec = now
            return
        self.instrument.center_temperature(self.channel, self.lease, delta, self.read_control)
        self.last_tec = now

    def _advance_frequency(self, sample, quality, now):
        previous_acquisition=self.last_acquisition
        stamp, frequency = sample.acquired_s, sample.frequency_hz
        dt = 0. if self.last_acquisition is None else stamp-self.last_acquisition
        # Long missing intervals must never become an integral jump.
        if dt > self.policy['max_sample_age_s']:
            dt = 0.
        self.last_acquisition, self.last_valid = stamp, now
        metrics=quality.get('metrics',{})
        valid=bool(quality.get('fresh') and quality.get('quality_valid') and
                   all(k in metrics and type(metrics[k]) in (int,float) and math.isfinite(metrics[k]) for k in self.policy['quality_limits']))
        self.last_observation = dict(acquired_s=stamp,acquired_unix_s=sample.acquired_unix_s,
            sequence=sample.sequence,frequency_hz=frequency,source_id=sample.source_id,
            source_quality=sample.quality,metrics=metrics,quality=quality,quality_valid=valid,
            degraded=any(metrics[k]>v for k,v in self.policy['quality_limits'].items()) if valid else None)
        if self.pending_policy is not None and self.mode == 'lock':
            self._adopt_pending()
        slope = self.policy['axes']['piezo']['frequency_hz_per_unit']
        if self.mode == 'scan':
            result = self.cycle.add(stamp, frequency)
            if result is None:return
            self.events.append(result)
            self.audit(dict(event='scan_cycle', **result))
            if not result['valid'] or now-result['end_s'] > self.scan_spec['boundary_tolerance_s']:
                return
            if self.pending_policy is not None:self._adopt_pending()
            if self.scan_spec['center_lock']:
                delta = self.control['kp']*(self.target-result['mean_frequency_hz'])/slope
                new_center = self.scan_center+delta
                def fresh_cycle():
                    self.read_control()
                    if self.clock()-result['end_s'] > self.scan_spec['boundary_tolerance_s']:
                        raise MeasurementUnavailable('Scan cycle expired before center write')
                try:self.instrument.scan_center(self.channel, self.lease, new_center, self.scan_amplitude,
                    prewrite=fresh_cycle)
                except ValueError:
                    self.audit(dict(event='scan_center_deferred',reason='approved envelope'))
                    return
                self.scan_center = new_center
                self.cycle = CycleMean(self.cycle.start+self.cycle.period,self.cycle.period,self.scan_spec['max_gap_s'])
            self._tec(self.scan_center, now)
        else:
            trial={}
            def feedback():
                fresh=self.read_control()
                if previous_acquisition is not None and fresh.acquired_s<=previous_acquisition:
                    raise MeasurementUnavailable('No new feedback acquisition')
                error_axis=(self.target-fresh.frequency_hz)/slope
                elapsed=0. if previous_acquisition is None else fresh.acquired_s-previous_acquisition
                integral_error=error_axis*elapsed if elapsed<=self.policy['max_sample_age_s'] else 0.
                if previous_acquisition is not None and hasattr(self.observation,'samples_after'):
                    samples=[s for s in self.observation.samples_after(previous_acquisition) if s.acquired_s<=fresh.acquired_s]
                    cursor=previous_acquisition;integral_error=0.
                    for s in samples:
                        # A bad packet may already have been replaced by a good
                        # latest sample. Never integrate it from the UDP ring.
                        if not self.frequency_window.contains(s, self.target, max_error_hz=self._lock_input_limit()):
                            integral_error=0.;break
                        gap=s.acquired_s-cursor
                        if not 0<gap<=self.policy['max_sample_age_s']:
                            integral_error=0.;break
                        integral_error+=(self.target-s.frequency_hz)/slope*gap;cursor=s.acquired_s
                    if cursor!=fresh.acquired_s:integral_error=0.
                integral=max(-self.control['integral_limit_v'],min(self.control['integral_limit_v'],self.integral+self.control['ki_per_s']*integral_error))
                desired=self.base+self.control['kp']*error_axis+integral
                low,high=self.policy['axes']['piezo']['bounds'];applied=max(low,min(high,desired))
                trial.update(sample=fresh,integral=integral,desired=desired,applied=applied)
                return applied,lambda:self._validate_frequency(fresh)
            self.instrument.servo_piezo(self.channel, self.lease, feedback)
            fresh=trial['sample'];applied=trial['applied']
            self.last_acquisition,self.last_valid=fresh.acquired_s,self.clock()
            self.last_observation.update(acquired_s=fresh.acquired_s,acquired_unix_s=fresh.acquired_unix_s,
                sequence=fresh.sequence,frequency_hz=fresh.frequency_hz,source_id=fresh.source_id,source_quality=fresh.quality)
            self.last_pi_write=self.clock()
            self.applied_pzt=applied
            self.applied_error_axis=(self.target-fresh.frequency_hz)/slope
            self.applied_observation=deepcopy(self.last_observation)
            if applied==trial['desired']:self.integral=trial['integral']
            self._tec(applied, now)
            self.audit(dict(event='lock_cycle',**self.last_observation,target_frequency_hz=self.target,
                locked=abs(self.target-fresh.frequency_hz)<=self.control['tolerance_hz'],pzt_v=applied))

    def tick(self):
        with self.mutex:
            from ldcon.owners.dlcpro_target_changes import tick as target_tick
            target_tick(self)
            from ldcon.owners.dlcpro_awg_protocol import tick as awg_tick
            awg_tick(self)
            self.pi_trial.poll()
            if self.mode not in ('lock','scan') or self.fault:return
            now = self.clock()
            try:
                if now-self.last_client > self.policy['client_timeout_s']:
                    self.stop();self.audit(dict(event='client_timeout_hold'));return
                try:sample=self.read_control()
                except (MeasurementUnavailable,OSError):sample=None
                if sample is None or (self.last_acquisition is not None and sample.acquired_s<=self.last_acquisition):
                    if now-self.last_valid > self.policy['measurement_timeout_s']:
                        self.stop();self.fault='control frequency deadline exceeded'
                    return
                if self.mode=='lock' and now-self.last_pi_write<self.policy['axes']['piezo']['update_interval_s']:
                    return
                try:quality=self._quality()
                except (MeasurementUnavailable,OSError):
                    quality=dict(fresh=False,quality_valid=False,metrics={},reason='optical quality unavailable')
                samples=(self.observation.samples_after(self.last_acquisition)
                    if self.mode=='scan' and hasattr(self.observation,'samples_after') else [sample])
                for value in samples:
                    if self.last_acquisition is not None and value.acquired_s<=self.last_acquisition:continue
                    if not self.frequency_window.contains(value, self.target, max_error_hz=self._lock_input_limit()):
                        if self.mode=='scan':self.cycle.invalid_input=True
                        continue
                    self._advance_frequency(value,quality,self.clock())
            except MeasurementUnavailable:
                # Explicit prewrite freshness refusal; no integrator or output replay.
                return
            except Exception as error:
                self.fault = type(error).__name__+': '+str(error)
                self.audit(dict(event='owner_fault', reason=self.fault))
                raise
