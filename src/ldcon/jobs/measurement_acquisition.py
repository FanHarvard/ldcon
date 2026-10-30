"""Shared cancellable acquisition and source-identity rules.

A source clock is supplied explicitly by the caller: a monotonic timestamp is
never compared with Unix time. Measurement selection and optical acceptance
remain algorithm inputs, not implicit behavior of this collection loop.
"""
import math

from ldcon.core.contracts import MeasurementUnavailable


def finite(value):
    return type(value) in (int,float) and math.isfinite(value)


def source_eligible(stamp, *, previous=None, now=None, max_age_s=None,
                    after=None, window_start=None):
    if not finite(stamp):return False
    if previous is not None and stamp<=previous:return False
    if after is not None and stamp<=after:return False
    if now is not None and (not finite(now) or stamp>now):return False
    if max_age_s is not None and (now is None or now-stamp>max_age_s):return False
    if window_start is not None and (not finite(window_start) or window_start>stamp
                                    or (after is not None and window_start<=after)):
        return False
    return True


def collect(acquire, *, clock, pause, check, sample_interval_s,
            duration_s=None, minimum_samples=None, timeout_s=None):
    """Acquire for a duration or until enough explicitly accepted source samples.

    ``acquire`` returns true only for a new accepted sample. ``pause`` may itself
    heartbeat/cancel. No retry of an output operation is performed here.
    """
    if (duration_s is None)==(minimum_samples is None):
        raise ValueError('Select duration or accepted-sample-count acquisition')
    start=clock();count=0
    until=start+duration_s if duration_s is not None else None
    deadline=start+timeout_s if timeout_s is not None else None
    while (clock()<until) if until is not None else (count<minimum_samples):
        check()
        if deadline is not None and clock()>=deadline:
            raise MeasurementUnavailable('Measurement acquisition deadline exceeded')
        if acquire():count+=1
        wait=sample_interval_s
        if until is not None:wait=min(wait,max(0.,until-clock()))
        if deadline is not None:wait=min(wait,max(0.,deadline-clock()))
        pause(wait)
    check()
    return dict(accepted_samples=count,elapsed_s=clock()-start)
