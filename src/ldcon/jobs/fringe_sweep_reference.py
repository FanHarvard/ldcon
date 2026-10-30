"""Retrospective period references from neighboring low-secondary scan points.

A continuous frequency segment is context evidence, not proof of physical mode
identity. Relative cleanliness and template agreement never certify single mode.
"""
import math
import numpy as np


def _finite(x):
    return type(x) in (int, float) and math.isfinite(x)


def analyze_sweep(points, *, discontinuity_hz):
    if not _finite(discontinuity_hz) or discontinuity_hz <= 0:
        raise ValueError('Positive frequency discontinuity limit required')
    groups = []; group = []; previous = None; previous_context = None
    for index, point in enumerate(points):
        frequency = point.get('frequency_hz')
        outputs = point.get('outputs', {})
        analysis = point.get('fringe_analysis', {})
        context = (outputs.get('temperature'), outputs.get('piezo'), point.get('direction'),
                   analysis.get('period_reference_px'), analysis.get('revision'))
        verified = all(v is not None for v in context) and point.get('quality') == 'valid'
        if not _finite(frequency) or not verified:
            if group: groups.append(group)
            group = []; previous = None; previous_context = None
            continue
        if previous is not None and (abs(frequency - previous) > discontinuity_hz or context != previous_context):
            if group: groups.append(group)
            group = []
        group.append((index, point)); previous = frequency; previous_context = context
    if group: groups.append(group)
    results = []
    for segment, group in enumerate(groups):
        candidates = []
        for index, point in group:
            a = point.get('fringe_analysis', {}); m = point.get('metrics', {})
            period = a.get('height_rank_spacing_median_px')
            mad = a.get('height_rank_spacing_mad_px')
            anchored = a.get('period_reference_px')
            profile = a.get('profile', [])
            keys = ('side_peak_prominence_max', 'side_peak_height_max', 'main_fwhm_fraction')
            if not a.get('period_qualified') or not all(_finite(m.get(k)) for k in keys): continue
            if not all(_finite(v) and v > 0 for v in (period, anchored)): continue
            if not _finite(mad) or mad / period > .05 or abs(period / anchored - 1) > .12: continue
            if len(profile) != 256 or not np.isfinite(profile).all(): continue
            score = m[keys[0]] + m[keys[1]] + 2 * m[keys[2]]
            candidates.append((score, index, point, period))
        # Require an adjacent triple: an isolated apparently clean sample cannot
        # establish a neighboring-pattern reference, especially near a hop.
        windows = [candidates[i:i + 3] for i in range(len(candidates) - 2)
                   if candidates[i + 2][1] - candidates[i][1] == 2]
        if not windows:
            results.append(dict(segment=segment, first_index=group[0][0], last_index=group[-1][0],
                                reference_valid=False, reason='No three adjacent qualified period anchors'))
            continue
        seeds = min(windows, key=lambda w: sum(x[0] for x in w))
        reference = float(np.median([s[3] for s in seeds]))
        templates = [np.asarray(s[2]['fringe_analysis']['profile']) for s in seeds]
        template = np.median(templates, axis=0); template -= template.mean()
        template_norm = np.linalg.norm(template)
        measurements = []
        for index, point in group:
            a = point.get('fringe_analysis', {}); m = point.get('metrics', {})
            profile = np.asarray(a.get('profile', []), dtype=float)
            period = m.get('fringe_period_px')
            valid = bool(a.get('period_qualified') and _finite(period) and len(profile) == 256
                         and np.isfinite(profile).all())
            correlation = None
            if valid:
                centered = profile - profile.mean(); denominator = template_norm * np.linalg.norm(centered)
                if denominator > 0:
                    correlation = float(max(np.dot(template, np.roll(centered, shift)) / denominator
                                            for shift in range(256)))
            measurements.append(dict(index=index, current=point.get('current'),
                reference_comparison_valid=bool(valid and correlation is not None),
                period_fractional_deviation=(period / reference - 1) if valid else None,
                template_correlation=correlation,
                side_peak_prominence_max=m.get('side_peak_prominence_max'),
                side_peak_height_max=m.get('side_peak_height_max'),
                main_fwhm_fraction=m.get('main_fwhm_fraction')))
        results.append(dict(segment=segment, first_index=group[0][0], last_index=group[-1][0],
            reference_valid=True, reference_period_px=reference,
            seed_indices=[s[1] for s in seeds], seed_currents=[s[2].get('current') for s in seeds],
            relative_reference_only=True, single_mode_certified=False, measurements=measurements))
    return dict(revision='neighbor-period-reference-v1', discontinuity_hz=discontinuity_hz,
                interpretation='Frequency-continuous context only; relative clean reference is not single-mode certification',
                rejected_context_or_quality_points=sum(len(g) for g in groups) != len(points), segments=results)
