"""Pure waveform compilation; time and voltage units are seconds and volts."""
from dataclasses import dataclass
import math

import numpy as np


PROFILES = {"bk4050b": (32768, 150e6), "siglent_sdg2000x": (8_000_000, 75e6),
            "siglent_sdg6000x": (20_000_000, 300e6)}


def positive(value, name, allow_zero=False):
    value = float(value)
    if not math.isfinite(value) or (value < 0 if allow_zero else value <= 0):
        raise ValueError(f"{name} must be finite and {'nonnegative' if allow_zero else 'positive'}")
    return value


def validate_channels(channels, *, require_limits=True):
    result, addresses, roles = [], set(), set()
    for item in channels:
        c = dict(item)
        required = {"role", "resource", "channel", "profile", "min_v", "max_v", "max_vpp"}
        if not required <= c.keys():
            raise ValueError(f"channel needs {sorted(required)}")
        if c["profile"] not in PROFILES or c["channel"] not in (1, 2):
            raise ValueError("select a supported AWG profile and channel 1 or 2")
        if not str(c["role"]).isidentifier() or c["role"] in roles:
            raise ValueError("channel roles must be unique Python identifiers")
        address = (c["resource"], c["channel"])
        if address in addresses:
            raise ValueError("the same AWG channel cannot have two owners")
        addresses.add(address)
        roles.add(c["role"])
        limits = ("min_v", "max_v", "max_vpp")
        if all(c[key] is None for key in limits):
            if require_limits:
                raise ValueError(f"{c['role']}: set min_v, max_v and max_vpp before requesting a sweep")
        else:
            for key in limits:
                if c[key] is None:
                    raise ValueError("provide all voltage limits, or leave all three unset")
                c[key] = float(c[key])
                if not math.isfinite(c[key]):
                    raise ValueError(f"{key} must be finite")
            if c["min_v"] >= c["max_v"] or c["max_vpp"] < 0.002:
                raise ValueError("invalid channel voltage limits")
        if c.get("broker"):
            from ldcon.core.instrument_client import validate_endpoint
            broker = c["broker"]
            validate_endpoint(broker["endpoint"])
            name = broker["instrument"]
            if not isinstance(name, str):
                raise ValueError("broker instrument must be a configured name or canonical identity")
            if not name.isidentifier():
                from ldcon.owners.instrument_identity import canonical
                canonical(name)
        c.setdefault("reference", "initial_offset")
        if c["reference"] not in ("initial_offset", "absolute"):
            raise ValueError("reference must be initial_offset or absolute")
        c.setdefault("idle_v", 0.0)
        c["idle_v"] = float(c["idle_v"])
        if not math.isfinite(c["idle_v"]):
            raise ValueError("idle_v must be finite")
        if "frequency_hz_per_v" in c:
            c["frequency_hz_per_v"] = float(c["frequency_hz_per_v"])
            c["frequency_reference_v"] = float(c.get("frequency_reference_v", c["idle_v"]))
            if (not math.isfinite(c["frequency_hz_per_v"]) or c["frequency_hz_per_v"] == 0
                    or not math.isfinite(c["frequency_reference_v"])):
                raise ValueError("frequency calibration needs a finite nonzero Hz/V and finite reference voltage")
        elif "frequency_reference_v" in c:
            raise ValueError("frequency_reference_v requires frequency_hz_per_v")
        if c["reference"] == "initial_offset" and c["idle_v"] != 0:
            raise ValueError("relative waveforms must start and finish at zero offset")
        c.setdefault("load", "HZ")
        if c["load"] not in ("HZ", "50"):
            raise ValueError("load must be HZ or 50")
        result.append(c)
    if not result:
        raise ValueError("at least one AWG channel is required")
    return result


@dataclass
class StaircasePlan:
    steps: np.ndarray
    waves: dict
    metadata: dict


def compile_plan(channels, levels, *, start, dwell, settle, acquire, sample_rate,
                 lead=0.01, tail=0.01, frequency_labels=None):
    channels = validate_channels(channels)
    if set(levels) != {c["role"] for c in channels}:
        raise ValueError("levels must provide exactly one voltage array for every role")
    start = positive(start, "start", True)
    dwell = positive(dwell, "dwell")
    settle = positive(settle, "settle", True)
    acquire = positive(acquire, "acquire")
    rate = positive(sample_rate, "sample_rate")
    lead, tail = positive(lead, "lead"), positive(tail, "tail")
    if settle + acquire > dwell + 1e-12:
        raise ValueError("settling plus acquisition must fit inside a step")
    ticks = []
    for value in (lead, dwell, tail):
        n = round(value * rate)
        if n < 1 or abs(n / rate - value) > 1e-10:
            raise ValueError("lead, dwell and tail must be integer sample periods")
        ticks.append(n)
    tracks = {role: np.asarray(v, dtype=float) for role, v in levels.items()}
    nsteps = len(next(iter(tracks.values())))
    if not nsteps or any(v.ndim != 1 or len(v) != nsteps or not np.isfinite(v).all()
                         for v in tracks.values()):
        raise ValueError("voltage arrays must be finite, nonempty and have equal length")
    count = ticks[0] + nsteps * ticks[1] + ticks[2]
    # All channels use exactly the same point count, including final idle padding.
    count = max(32, 16 * math.ceil(count / 16))
    if any(count > PROFILES[c["profile"]][0] or rate > PROFILES[c["profile"]][1]
           for c in channels):
        raise ValueError("record exceeds an AWG profile's point count or sample rate")
    duration = count / rate
    if 1 / duration < 1e-6:
        raise ValueError("record is below the AWG minimum repetition frequency")
    labels = np.full(nsteps, np.nan) if frequency_labels is None else np.asarray(frequency_labels, float)
    if labels.shape != (nsteps,) or (frequency_labels is not None and not np.isfinite(labels).all()):
        raise ValueError("frequency_labels must have one finite value per step")
    steps = np.zeros(nsteps, dtype=[("index", "<i4"), ("start_s", "<f8"),
                                   ("acquire_start_s", "<f8"), ("acquire_end_s", "<f8"),
                                   ("nominal_frequency_hz", "<f8")])
    steps["index"] = np.arange(nsteps)
    steps["start_s"] = start + lead + np.arange(nsteps) * dwell
    steps["acquire_start_s"] = steps["start_s"] + settle
    steps["acquire_end_s"] = steps["acquire_start_s"] + acquire
    steps["nominal_frequency_hz"] = labels
    waves = {}
    for c in channels:
        values = np.full(count, c["idle_v"], dtype=float)
        values[ticks[0]:ticks[0] + nsteps * ticks[1]] = np.repeat(tracks[c["role"]], ticks[1])
        lo, hi = float(values.min()), float(values.max())
        amplitude = max(0.002, hi - lo)
        if amplitude > c["max_vpp"] + 1e-12:
            raise ValueError(f"{c['role']}: voltage span exceeds max_vpp")
        offset = (hi + lo) / 2
        if c["reference"] == "absolute" and (lo < c["min_v"] or hi > c["max_v"]):
            raise ValueError(f"{c['role']}: voltage outside configured limits")
        codes = np.rint((values - offset) / (amplitude / 2) * 32767).astype("<i2")
        waves[c["role"]] = dict(codes=codes, levels=tracks[c["role"]],
                                amplitude_vpp=amplitude, offset_v=offset,
                                min_v=lo, max_v=hi)
    return StaircasePlan(steps, waves, dict(start_s=start, duration_s=duration,
                        sample_rate_hz=rate, dwell_s=dwell, lead_s=lead,
                        tail_s=duration - lead - nsteps * dwell, acquire_s=acquire,
                        settle_s=settle, point_count=count,
                        synchronization="one common rising edge; independent AWG clocks",
                        frequency_source="nominal labels only; match wavemeter plateaus offline"))
