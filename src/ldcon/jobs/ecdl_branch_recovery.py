"""Pure, descriptive matching against retained, measured ECDL branch maps.

This classifies an observed branch, not a physical mode number or permission to
enable PI. The target and its capture tolerance deliberately do not enter the
matcher. No observation requests, device writes or exploratory scans occur here.
"""
import copy

from ldcon.jobs.ecdl_mode_map import SCHEMA, checked_artifact, finite


DEFAULTS = dict(max_age_s=86400., map_job_id=None, branch_id=None,
                separation_fraction=.25, min_points=3, confirm_samples=3,
                max_sample_age_s=5., held_tolerances=dict(piezo=0., temperature=0.))


def parameters(value):
    if not isinstance(value, dict) or set(value) - DEFAULTS.keys():
        raise ValueError('Invalid branch_match configuration')
    p = copy.deepcopy({**DEFAULTS, **value})
    for key in ('max_age_s', 'max_sample_age_s'):
        if not finite(p[key]) or p[key] <= 0:
            raise ValueError('branch_match.' + key + ' must be positive')
    if not finite(p['separation_fraction']) or not 0 <= p['separation_fraction'] < .5:
        raise ValueError('branch_match.separation_fraction must be in [0, .5)')
    for key in ('min_points', 'confirm_samples'):
        if type(p[key]) is not int or not 3 <= p[key] <= 100:
            raise ValueError('branch_match.' + key + ' must be 3..100')
    for key in ('map_job_id', 'branch_id'):
        if p[key] is not None and (not isinstance(p[key], str) or not p[key]):
            raise ValueError('branch_match.' + key + ' must be an id or null')
    tolerances = p['held_tolerances']
    if (not isinstance(tolerances, dict) or set(tolerances) != {'piezo', 'temperature'}
            or any(not finite(v) or v < 0 for v in tolerances.values())):
        raise ValueError('branch_match.held_tolerances must define nonnegative piezo and temperature tolerances')
    return p


def _value(policy, key, default=None):
    return policy.get(key, default) if isinstance(policy, dict) else getattr(policy, key, default)


def _distance(value, bounds):
    return max(bounds[0] - value, value - bounds[1], 0.)


def _describe(artifact, row, p, laser_id, now_unix_s, policies):
    mapping = checked_artifact(artifact, SCHEMA)
    if mapping.get('lock_admission') is not False or mapping.get('status') != 'complete':
        raise ValueError('Map is incomplete or is not an observational artifact')
    context = mapping.get('context', {})
    if not laser_id or context.get('laser_id') != laser_id:
        raise ValueError('Map belongs to a different laser or has no laser identity')
    if p['map_job_id'] is not None and context.get('job_id') != p['map_job_id']:
        raise ValueError('Map job does not match the requested reference')
    recorded, started = mapping.get('recorded_unix_s'), mapping.get('started_unix_s')
    if (not finite(now_unix_s) or not finite(recorded) or not finite(started)
            or not 0 <= now_unix_s - recorded <= p['max_age_s']
            or not 0 <= now_unix_s - started <= p['max_age_s'] or started > recorded):
        raise ValueError('Measured branch map is stale or has an invalid acquisition time')
    for axis in ('current', 'piezo', 'temperature'):
        saved_policy = mapping.get('policies', {}).get(axis, {})
        policy = policies.get(axis, {})
        for key in ('identity', 'unit'):
            if _value(policy, key) is not None and _value(policy, key) != _value(saved_policy, key):
                raise ValueError('Map axis identity or unit does not match: ' + axis)
        for candidate in (saved_policy, policy):
            resolution = _value(candidate, 'resolution', 0.) or 0.
            if not finite(resolution) or resolution < 0:
                raise ValueError('Invalid map axis resolution: ' + axis)
        if axis == 'current':
            continue
        reference = mapping.get('held_context', {}).get(axis)
        actual = row.get('outputs', {}).get(axis)
        tolerance = max(p['held_tolerances'][axis], _value(saved_policy, 'resolution', 0.) or 0.,
                        _value(policy, 'resolution', 0.) or 0., 1e-9)
        if not finite(reference) or not finite(actual) or abs(actual - reference) > tolerance:
            raise ValueError('Map held-axis context does not match: ' + axis)
    points, branches = mapping.get('points'), mapping.get('branches')
    if not isinstance(points, list) or not isinstance(branches, list) or not points or not branches:
        raise ValueError('Map contains no measured branches')
    selected = p['branch_id'] or mapping.get('selection', {}).get('branch_id')
    summaries, seen = [], set()
    for branch in branches:
        indices = branch.get('point_indices')
        if (not isinstance(indices, list) or not indices or len(indices) != len(set(indices))
                or any(type(i) is not int or not 0 <= i < len(points) or i in seen for i in indices)):
            raise ValueError('Map branch point references are invalid or ambiguous')
        seen.update(indices)
        measured = [points[i] for i in indices]
        if any(not finite(point.get('frequency_hz')) or point['frequency_hz'] <= 0
               or not finite(point.get('current')) for point in measured):
            raise ValueError('Map branch contains invalid measurements')
        for point in measured:
            recorded_point = point.get('recorded_unix_s')
            if not finite(recorded_point) or not started <= recorded_point <= recorded:
                raise ValueError('Map branch point has no valid acquisition record')
            for axis in ('piezo', 'temperature'):
                value = point.get('outputs', {}).get(axis)
                resolution = mapping.get('policies', {}).get(axis, {}).get('resolution', 0.) or 0.
                if not finite(value) or abs(value - mapping['held_context'][axis]) > max(resolution, 1e-9):
                    raise ValueError('Map point differs from its held-axis context')
        # Derived bands must come from measured samples, never legacy mode labels
        # or a saved frequency_bounds_hz assertion.
        frequencies = [point['frequency_hz'] for point in measured]
        currents = [point['current'] for point in measured]
        summaries.append(dict(id=branch.get('id'), samples=len(measured),
                              distinct_currents=len(set(currents)),
                              frequency_bounds_hz=[min(frequencies), max(frequencies)],
                              current_bounds=[min(currents), max(currents)]))
    ids = [branch['id'] for branch in summaries]
    if any(not isinstance(i, str) or not i for i in ids) or len(set(ids)) != len(ids):
        raise ValueError('Map branch identifiers are missing or ambiguous')
    desired = next((branch for branch in summaries if branch['id'] == selected), None)
    if desired is None or desired['distinct_currents'] < p['min_points']:
        raise ValueError('Map has no selected branch with enough measured points')
    current = row.get('outputs', {}).get('current')
    resolution = max(_value(policies.get('current', {}), 'resolution', 0.) or 0.,
                     mapping.get('policies', {}).get('current', {}).get('resolution', 0.) or 0., 1e-9)
    if not finite(current) or _distance(current, desired['current_bounds']) > resolution:
        raise ValueError('Current lies outside the selected branch measured interval')
    return dict(map_job_id=context.get('job_id'), artifact_sha256=mapping['artifact_sha256'],
                recorded_unix_s=recorded, selected_branch_id=selected, branches=summaries,
                physical_mode_identity_verified=False, lock_admission=False)


def match_branch(artifacts, row, policy, *, laser_id, now_unix_s, policies=None):
    """Return matched/other_branch/ambiguous/unavailable with measured evidence.

    Select the latest compatible, complete map. A candidate can extend beyond its
    sampled frequency interval by at most a configured fraction (< 1/2) of the
    measured separation to the nearest other branch. A map of only one branch
    supports its measured interval only. Overlap is unknown, not a nearest-label
    guess. Current/PZT/TEC context and map age are checked independently.
    """
    p = parameters(policy)
    unavailable = dict(status='unavailable', branch_reacquired=False, lock_admission=False,
                       physical_mode_identity_verified=False)
    frequency = row.get('frequency_hz')
    if not finite(frequency) or frequency <= 0:
        return dict(unavailable, reason='Fresh finite optical frequency is required')
    eligible, rejected = [], []
    for artifact in artifacts or []:
        try:
            eligible.append(_describe(artifact, row, p, laser_id, now_unix_s, policies or {}))
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError) as error:
            rejected.append(str(error))
    if not eligible:
        return dict(unavailable, reason='A recent measured branch map with matching held context is required',
                    rejected_maps=rejected)
    latest = max(item['recorded_unix_s'] for item in eligible)
    latest_maps = [item for item in eligible if item['recorded_unix_s'] == latest]
    if len({item['artifact_sha256'] for item in latest_maps}) != 1:
        return dict(unavailable, status='ambiguous', reason='Multiple equally recent maps disagree in identity')
    evidence = latest_maps[0]
    candidates, classifications = [], []
    for branch in evidence['branches']:
        bounds = branch['frequency_bounds_hz']
        gaps = [max(other['frequency_bounds_hz'][0] - bounds[1],
                    bounds[0] - other['frequency_bounds_hz'][1], 0.)
                for other in evidence['branches'] if other['id'] != branch['id']]
        allowance = min(gaps) * p['separation_fraction'] if gaps else 0.
        distance = _distance(frequency, bounds)
        classifications.append(dict(branch_id=branch['id'], distance_hz=distance,
                                    measured_separation_hz=min(gaps) if gaps else None,
                                    allowance_hz=allowance))
        if distance <= allowance:
            candidates.append(branch['id'])
    evidence.update(candidate_branch_ids=candidates, frequency_hz=frequency, classifications=classifications)
    if len(candidates) != 1:
        return dict(unavailable, status='ambiguous' if candidates else 'unavailable', evidence=evidence,
                    reason='Measured branch envelopes overlap' if candidates else 'Frequency has no supported measured branch match')
    matched = candidates[0] == evidence['selected_branch_id']
    return dict(status='matched' if matched else 'other_branch', branch_reacquired=matched,
                reason='Unique observed branch match' if matched else 'A different measured branch is observed',
                evidence=evidence, lock_admission=False, physical_mode_identity_verified=False)
