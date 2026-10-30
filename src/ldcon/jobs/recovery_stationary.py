"""Stationary adapter for branch-independent current-route recovery."""
import copy
from collections import deque

from ldcon.core.contracts import DeviceError, MeasurementUnavailable
from ldcon.core.interface_cards import CardCancelled
from ldcon.jobs.ecdl_mode_map import BoundedExperiment
from ldcon.jobs.recovery_reference import infer, finite
from ldcon.jobs.recovery_procedure import parameters, execute


class StationaryRecovery:
    def __init__(self, lock, steady, *, clock, sleep, cancel, emit, **unused):
        self.lock, self.steady = lock, steady
        self.clock, self.sleep, self.cancel, self.emit = clock, sleep, cancel, emit
        from ldcon.jobs.ecdl_mode_recovery import validate
        self.p = validate(steady['recovery'])
        self.history = deque(maxlen=100000)
        self.epoch = 0
        self.intentional = False
        self.target_ramping = False
        self.target = steady['target_frequency_hz']
        self.reference = None
        self.pending_reference = None
        self.hop_count = 0
        self.last_stamp = None
        self.armed = False
        self.recoveries = 0
        self.recorder = unused.get("recorder")

    def set_target(self, value):
        if value != self.target:
            self.target = value
            self.epoch += 1
            self.history.clear()
            self.reference = None
            self.pending_reference = None
            self.hop_count = 0
            self.emit('recovery_reference_invalidated', dict(reason='new target', operation_epoch=str(self.epoch)))

    def invalidate(self, reason):
        self.epoch += 1
        self.history.clear()
        self.reference = None
        self.pending_reference = None
        self.hop_count = 0
        if self.recorder:self.recorder.invalidate()
        self.emit('recovery_reference_invalidated', dict(reason=reason, operation_epoch=str(self.epoch)))

    def prime(self):
        # Starting the lock does not require a map or pre-existing recovery proof.
        self.emit('recovery_reference_pending', dict(reason='waiting for stable observations in current operation epoch'))

    def probe(self, *, update_reference=True, intentional_operation=False):
        if self.cancel():
            raise CardCancelled('Recovery monitor cancelled')
        self.set_target(self.steady['target_frequency_hz'])
        try:
            row = self.lock.response_info()
        except (MeasurementUnavailable, OSError):
            self.hop_count = 0
            return False
        sample = row['observation']['frequency']
        stamp = sample.get('acquired_s')
        frequency = sample.get('frequency_hz')
        if (not finite(stamp) or not 0 <= self.clock()-stamp <= self.p['max_sample_age_s']
                or not finite(frequency) or frequency <= 0 or sample.get('unit') != 'Hz'
                or self.last_stamp is not None and stamp <= self.last_stamp):
            return False
        if self.last_stamp is not None and stamp-self.last_stamp > self.p['reference']['max_gap_s']:
            self.hop_count = 0
        self.last_stamp = stamp
        if self.recorder:
            self.recorder.observe(dict(acquired_s=stamp,frequency_hz=frequency,outputs=row['outputs']),
                                  self.target,intentional=not update_reference or intentional_operation)
        # Every intentional operation is a boundary. A controller caller freezes
        # updates during sweeps/adjustments; never learn that transient as home.
        if intentional_operation:
            if not self.intentional:self.invalidate('intentional target or sweep operation')
            self.intentional = True
            return False
        self.intentional = False
        if not update_reference:
            # A finite adjustment may need its original measured checkpoint to
            # recover a genuine trial hop. Freeze it; do not learn trial points.
            self.history.append(dict(acquired_s=stamp, frequency_hz=frequency, outputs=row['outputs'],
                                     operation_epoch=str(self.epoch), intentional_operation=True))
            self.hop_count = 0
            return False
        if self.reference is not None and abs(frequency-self.reference['frequency_hz']) > self.p['hop_threshold_hz']:
            self.hop_count += 1
            if self.pending_reference is None:
                self.pending_reference = copy.deepcopy(self.reference)
            return self.hop_count >= self.p['confirm_samples']
        self.hop_count = 0
        self.pending_reference = None
        self.history.append(dict(acquired_s=stamp, frequency_hz=frequency,
                                 outputs=copy.deepcopy(row['outputs']), operation_epoch=str(self.epoch)))
        while self.history and stamp-self.history[0]['acquired_s'] > self.p['reference']['lookback_s']:
            self.history.popleft()
        try:
            self.reference = infer(self.history, stamp, str(self.epoch), self.p['reference'])
        except ValueError:
            pass
        return False

    def recover(self, reason):
        reference = copy.deepcopy(self.pending_reference or self.reference)
        if reference is None or reference['operation_epoch'] != str(self.epoch):
            raise DeviceError('No stable recovery reference for this operation epoch')
        if self.clock()-reference['end_s'] > self.p['reference']['lookback_s']:
            raise DeviceError('Recovery reference expired')
        target = self.target
        if self.armed:
            held = self.lock.hold('relative current-route recovery: '+reason)
            self.armed = False
            self.emit('ecdl_lock_held', held)
        def interrupted():
            return self.cancel() or self.steady['target_frequency_hz'] != target
        experiment = BoundedExperiment(self.lock.recovery_ports(), self.p, clock=self.clock,
            sleep=self.sleep, cancel=interrupted, emit=self.emit)
        result = execute(experiment, self.p, reference)
        self.recoveries += 1
        tolerance = self.steady.get('acquisition_tolerance_hz') or self.steady['tolerance_hz']
        # Returning to an observed frequency is not authority to override the
        # configured target or relax the native PI capture contract.
        if abs(result['final']['frequency_hz']-target) > tolerance:
            self.emit('recovery_returned_without_capture', result)
            raise DeviceError('Frequency recovered, but PI target capture is not satisfied; outputs held')
        if interrupted():
            raise CardCancelled('Recovery superseded before PI rearm')
        self.lock.check_ready(self.steady['strategy'])
        self.lock.arm(target, tolerance, self.steady['strategy'])
        self.armed = True
        self.hop_count = 0
        self.pending_reference = None
        result['pi_rearmed'] = True
        self.emit('ecdl_recovery_verified', result)
