"""Explicit offline measurement plans; never imported by production code."""
from copy import deepcopy


def measurement_parameters(**overrides):
    """Complete test plan, with each test overriding the values it exercises."""
    return deepcopy(dict(
        changes=[], apply_mode='ordered',
        measurements=dict(wavelength=True, optical_quality=False, dither=False),
        baseline_s=0., wait_s=0., duration_s=10., history_wait_s=2.,
        sample_interval_s=.1, minimum_samples=3, measurement_max_age_s=5.,
        max_sample_gap_s=2., timeout_s=300., target_frequency_hz=None,
    ) | overrides)


def measurement_batch(steps, *, timeout_s=3600.):
    return dict(steps=deepcopy(steps), timeout_s=timeout_s)
