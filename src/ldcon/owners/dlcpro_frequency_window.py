"""Control-input plausibility, separate from commanded target authority.

The active target is the reference, not a cached startup frequency. Raw source
packets remain available for diagnosis; rejected frequencies never drive PI.
"""
from copy import deepcopy

from ldcon.core.contracts import MeasurementUnavailable
from ldcon.core.target_policy import laser_target, target_bounds


class FrequencyWindow:
    def __init__(self, target_policy, audit):
        self.laser = {'target': deepcopy(target_policy)} if target_policy is not None else {}
        laser_target(self.laser)
        self.audit = audit
        self.rejected = 0
        self.last_rejection = None
        self.held = False
        self.last_key = None

    def bounds(self, target, *, max_error_hz=None):
        bounds = target_bounds(self.laser, reference_hz=target)
        if max_error_hz is not None and target is not None:
            lock_bounds = [target-max_error_hz, target+max_error_hz]
            if bounds is None:
                return lock_bounds
            return [max(bounds[0], lock_bounds[0]), min(bounds[1], lock_bounds[1])]
        return bounds

    def contains(self, sample, target, *, max_error_hz=None):
        bounds = self.bounds(target, max_error_hz=max_error_hz)
        return bounds is None or bounds[0] <= sample.frequency_hz <= bounds[1]

    def check(self, sample, target, *, max_error_hz=None):
        if not self.contains(sample, target, max_error_hz=max_error_hz):
            key = (sample.acquired_s, sample.sequence)
            if key != self.last_key:
                self.rejected += 1
                self.last_key = key
            self.last_rejection = dict(frequency_hz=sample.frequency_hz,
                acquired_s=sample.acquired_s, acquired_unix_s=sample.acquired_unix_s,
                sequence=sample.sequence, source_id=sample.source_id,
                target_frequency_hz=target,
                bounds_hz=self.bounds(target, max_error_hz=max_error_hz),
                lock_max_error_hz=max_error_hz)
            if not self.held:
                self.audit(dict(event='control_frequency_range_hold', **self.last_rejection))
            self.held = True
            raise MeasurementUnavailable('Control frequency outside laser target range or lock input window')
        if self.held:
            self.audit(dict(event='control_frequency_range_resumed',
                frequency_hz=sample.frequency_hz, acquired_s=sample.acquired_s,
                target_frequency_hz=target, rejected_samples=self.rejected))
        self.held = False

    def view(self, target, *, max_error_hz=None):
        return dict(bounds_hz=self.bounds(target, max_error_hz=max_error_hz),
                    target_bounds_hz=self.bounds(target), lock_max_error_hz=max_error_hz,
                    held=self.held,
                    rejected_samples=self.rejected, last_rejection=deepcopy(self.last_rejection))
