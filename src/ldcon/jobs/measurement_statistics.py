"""Identical finite-value statistics for direct measurements and agent trials.

Sampling windows, timestamps, freshness and eligibility remain the caller's
responsibility; this helper does not turn invalid/missing data into a value.
"""
import math
import statistics


def numeric_summary(values, *, reference=None):
    values=list(values)
    if not values or any(type(v) not in (int,float) or not math.isfinite(v) for v in values):
        raise ValueError('Numeric measurements must be nonempty and finite')
    result=dict(mean=statistics.mean(values),stddev=statistics.pstdev(values),
        minimum=min(values),maximum=max(values),rms=math.sqrt(statistics.mean(v*v for v in values)))
    if reference is not None:
        if type(reference) not in (int,float) or not math.isfinite(reference):
            raise ValueError('Measurement reference must be finite')
        offsets=[v-reference for v in values]
        result.update(mean_offset_hz=statistics.mean(offsets),
            rms_from_target_hz=math.sqrt(statistics.mean(v*v for v in offsets)))
    return result
