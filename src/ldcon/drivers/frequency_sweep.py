"""Pure, sample-exact external-modulation waveform compiler (no hardware I/O)."""
import math
import numpy as np
from ldcon.drivers.staircase import PROFILES, StaircasePlan, validate_channels


def compile_sweep(channels, *, shape, levels, dwell_s, sample_rate_hz,
                  unit="Hz", reference="initial_offset", lead_s=0., tail_s=0.):
    channels = validate_channels(channels)
    if shape not in ("triangle", "staircase") or unit not in ("Hz", "V"):
        raise ValueError("Expected triangle/staircase and Hz/V")
    values = np.asarray(levels, dtype=float)
    if values.ndim != 1 or not 1 <= len(values) <= 10000 or not np.isfinite(values).all():
        raise ValueError("Expected 1..10000 finite levels")
    if shape == "triangle" and (len(values) != 2 or values[0] == values[1]):
        raise ValueError("Triangle requires two distinct endpoints")
    if reference != "initial_offset":
        raise ValueError("Frequency sweeps use shifts relative to the initial AWG offset")
    rate = float(sample_rate_hz)
    if not math.isfinite(rate) or rate <= 0:
        raise ValueError("Invalid sample rate")
    def count(seconds, positive=False):
        seconds = float(seconds)
        if not math.isfinite(seconds) or seconds < 0 or (positive and seconds == 0):
            raise ValueError("Invalid waveform duration")
        if not math.isfinite(seconds * rate):
            raise ValueError("Waveform sample count overflow")
        n = round(seconds * rate)
        if not math.isclose(seconds * rate, n, abs_tol=1e-8, rel_tol=0):
            raise ValueError("Timing must be an integral number of samples")
        if positive and n < 1:
            raise ValueError("Empty waveform segment")
        return n
    dwell, lead, tail = count(dwell_s, True), count(lead_s), count(tail_s)
    total = lead + dwell * len(values) + tail
    # Padding silently changes repetition/shot timing. Reject instead.
    if total < 32 or total % 16:
        raise ValueError("Record must contain >=32 samples and a multiple of 16; adjust sample rate/timing")
    for c in channels:
        size, max_rate = PROFILES[c["profile"]]
        if total > size or rate > max_rate or rate / total < 1e-6:
            raise ValueError("AWG memory/rate/repetition limit exceeded")
    waves = {}
    for c in channels:
        if c["reference"] != reference or c["idle_v"] != 0:
            raise ValueError("Channel must use initial_offset with zero idle shift")
        coefficient = c.get("frequency_hz_per_v")
        if unit == "Hz" and coefficient is None:
            raise ValueError("Measured signed Hz/V calibration is required")
        v = values / coefficient if unit == "Hz" else values.copy()
        if not np.isfinite(v).all():
            raise ValueError("Frequency calibration produces nonfinite voltages")
        body = (np.concatenate((np.linspace(v[0], v[1], dwell, endpoint=False),
                                np.linspace(v[1], v[0], dwell, endpoint=False)))
                if shape == "triangle" else np.repeat(v, dwell))
        samples = np.r_[np.zeros(lead), body, np.zeros(tail)]
        lo, hi = float(samples.min()), float(samples.max())
        amplitude, offset = max(.002, hi-lo), (lo+hi)/2
        if amplitude > c["max_vpp"]:
            raise ValueError("Sweep exceeds approved Vpp")
        codes = np.rint((samples-offset)/(amplitude/2)*32767).astype("<i2")
        waves[c["role"]] = dict(codes=codes, levels=v, amplitude_vpp=amplitude,
                                offset_v=offset, min_v=lo, max_v=hi)
    steps = np.array([], dtype=[("index", "i4")])
    return StaircasePlan(steps, waves, dict(shape=shape, duration_s=total/rate,
        sample_rate_hz=rate, point_count=total, dwell_s=float(dwell_s),
        lead_s=float(lead_s), tail_s=float(tail_s), unit=unit,
        reference=reference, requested_levels=values.tolist(),
        frequency_source="nominal calibrated shift, not measured optical frequency"))
