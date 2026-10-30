"""Opt-in residual PI during an externally timed, Fine2-verified staircase."""
from copy import deepcopy

from ldcon.core.contracts import DeviceError, MeasurementUnavailable
from ldcon.core.interface_schema import digest
from ldcon.owners.dlcpro_awg_tracking_guard import admit, interior, verify_base
from ldcon.owners.dlcpro_awg_tracking_report import summarize


class TrackingStop(DeviceError):
    """Known prewrite/data failure: stop the waveform once; never chase a branch."""


class Tracking:
    def __init__(self, owner, config, session, snapshot):
        self.o, self.config, self.s = owner, config, session
        self.origin = deepcopy(snapshot)
        self.policy, self.control = deepcopy(owner.policy), deepcopy(owner.control)
        self.revision = digest(owner.policy)
        self.integral = 0.; self.previous = None; self.epoch = None
        self.last_sample_s = None; self.playing = False
        self.last_valid = owner.clock(); self.last_write = float('-inf')
        self.active = False
        self.io_budget = config.get('tracking_io_budget_s', config['adc_max_interval_s'])
        session['tracking_samples'] = []
        session['tracking'] = dict(enabled=True, active=False, writes=0, qualified=False,
            effective_target_hz=None, frozen_reason='preparing', integral_v=0.)

    def admit(self, proof):
        self.proof = admit(proof, self.origin, self.policy, self.o.instrument.hardware, self.control)
        return self.proof

    def start(self, sample):
        self.slope = self.policy['axes']['piezo']['frequency_hz_per_unit']
        error = (self.s['origin_target_hz']-sample.frequency_hz)/self.slope
        self.bias = -self.control['kp']*error
        self.active = True; self.last_valid = self.o.clock()
        self.s['tracking'].update(active=True, frozen_reason='waiting for verified plateau')

    def freeze(self, reason):
        self.previous = None
        self.s['tracking'].update(frozen_reason=reason, integral_v=self.integral)

    def finish(self):
        self.active = False
        self.freeze('sweep ended; tracking disabled before cleanup')
        self.s['tracking'].update(active=False, **summarize(
            self.s, self.policy, self.control['tolerance_hz']))

    def _context(self):
        o, s = self.o, self.s
        state = o.instrument._state(o.channel, o.lease)
        if (not self.active or s.get('cancel_requested') or o.mode != 'awg_sweep'
                or o.target_revision != s['origin_target_revision'] or o.fault
                or digest(o.policy) != self.revision or state['revision'] != self.revision
                or o.control != self.control):
            raise TrackingStop('Tracking controller context changed')
        now = o.clock()
        if (now-o.last_client > o.policy['client_timeout_s']
                or now-s['renewed_s'] > self.config['watchdog_s']):
            raise TrackingStop('Tracking controller heartbeat expired')
        held = o.instrument.confirmed_outputs[o.channel]
        for axis in ('temperature', 'current'):
            if held[axis] != self.origin['hardware'][axis]['setpoint']:
                raise TrackingStop('Tracking output anchor changed')

    def tick(self, adc):
        if not self.active:
            return
        self._context()
        now = self.o.clock()
        if self.s['trigger_s'] is not None and not self.playing:
            self.playing = True; self.last_valid = now
        if self.playing and now-self.last_valid > self.policy['measurement_timeout_s']:
            self.o.fault = 'Tracking control frequency deadline exceeded'
            raise TrackingStop(self.o.fault)
        epoch = interior(self.s, self.config, adc)
        if epoch is None:
            self.epoch = None
            self.freeze('ambiguous edge or idle interval')
            return
        if self.epoch is None or self.epoch['index'] != epoch['index']:
            self.epoch = dict(epoch, confirmed_after_s=adc['completed_s'])
            self.freeze('waiting for a second ADC observation in this plateau')
            return
        # Leave one existing ADC budget before the earliest possible edge. A
        # slow acknowledged write is diagnosed afterward; its full-wave physical
        # envelope remains safe even if an edge occurred during that operation.
        if now+self.io_budget >= epoch['right_s']:
            self.freeze('approaching an ambiguous edge')
            return
        try:
            self._correct(adc, epoch)
        except MeasurementUnavailable as error:
            self.freeze(str(error))
            if now-self.last_valid > self.policy['measurement_timeout_s']:
                self.o.fault = 'Tracking control frequency deadline exceeded'
                raise TrackingStop(self.o.fault) from error

    def _correct(self, adc, epoch):
        o = self.o
        with o.instrument.mutex, o.instrument.driver.control_transaction():
            self._context()
            try:
                sample = o.read_control(target=epoch['target_hz'])
            except OSError as error:
                raise MeasurementUnavailable('Read-only tracking wavelength unavailable') from error
            except MeasurementUnavailable:
                raise
            except (DeviceError, ValueError, TypeError, AttributeError) as error:
                raise TrackingStop('Invalid tracking wavelength read: '+str(error)) from error
            now = o.clock()
            if (now-adc['completed_s'] > self.config['adc_max_interval_s']
                    or now+self.io_budget >= epoch['right_s']
                    or not self.epoch['confirmed_after_s'] < sample.acquired_s < epoch['right_s']):
                raise MeasurementUnavailable('Wavelength and ADC do not share a fresh plateau epoch')
            if self.last_sample_s is not None and sample.acquired_s <= self.last_sample_s:
                raise MeasurementUnavailable('No new plateau wavelength acquisition')
            error_hz = epoch['target_hz']-sample.frequency_hz
            if abs(error_hz) > self.control['capture_hz']:
                raise TrackingStop('Frequency left the verified plateau capture; no branch chase')
            self.last_valid = now
            if now-self.last_write < self.policy['axes']['piezo']['update_interval_s']:
                return
            elapsed = 0. if self.previous is None else sample.acquired_s-self.previous['acquired_s']
            if elapsed > self.policy['max_sample_age_s']:
                elapsed = 0.
            error = error_hz/self.slope
            integral = max(-self.control['integral_limit_v'], min(self.control['integral_limit_v'],
                self.integral+self.control['ki_per_s']*error*elapsed))
            origin = self.proof['tracking']['origin_base_v']
            low, high = self.proof['tracking']['base_command_bounds_v']
            desired = origin+self.bias+self.control['kp']*error+integral
            applied = max(low, min(high, desired))
            if (desired > high and error > 0) or (desired < low and error < 0):
                integral = self.integral  # No integration farther into saturation.
                desired = origin+self.bias+self.control['kp']*error+integral
                applied = max(low, min(high, desired))
            verify_base(applied, self.proof)
            self._context()
            o._validate_frequency(sample, target=epoch['target_hz'])
            before = o.instrument.confirmed_outputs[o.channel]['piezo']
            row = dict(index=epoch['index'], target_hz=epoch['target_hz'],
                acquired_s=sample.acquired_s, acquired_unix_s=sample.acquired_unix_s,
                frequency_hz=sample.frequency_hz, error_hz=error_hz,
                adc_started_s=adc['started_s'], adc_completed_s=adc['completed_s'],
                base_before_v=before, requested_base_v=applied, integral_v=integral,
                saturated=applied != desired, write_started_s=o.clock(), verified=False)
            self.s['tracking_samples'].append(row)
            if applied != before:
                def fresh_before_send():
                    self._context()
                    o._validate_frequency(sample, target=epoch['target_hz'])
                    if (o.clock()-adc['started_s'] >= self.config['adc_max_interval_s']
                            or o.clock()+self.io_budget >= epoch['right_s']):
                        raise MeasurementUnavailable('Tracking intent expired before physical send')
                # Do not catch this as missing input. An unknown mutation faults
                # the instrument and must never be followed by automatic cleanup.
                applied = o.instrument._write(o.channel, o.lease,
                    o.instrument.driver.path(o.channel, 'pc:voltage-set'), applied,
                    self.policy['axes']['piezo']['readback_tolerance'], compact=True,
                    prewrite=fresh_before_send)
                row['write_timing'] = dict(o.instrument.last_write_timing)
                self.s['tracking']['writes'] += 1
            verify_base(applied, self.proof)
            row.update(verified=True, applied_base_v=applied, write_completed_s=o.clock())
            self.integral = integral; self.previous = row; self.last_write = o.clock()
            self.last_sample_s = sample.acquired_s
            self.s['tracking'].update(effective_target_hz=epoch['target_hz'],
                plateau_index=epoch['index'], frozen_reason=None, integral_v=integral,
                correction_v=applied-origin, error_hz=error_hz)
            o.audit(dict(event='awg_tracking_correction', **row))
            if (o.clock()-adc['started_s'] > self.io_budget
                    or o.clock() >= epoch['right_s']):
                raise TrackingStop('Verified tracking write exceeded the ADC/plateau timing budget')
