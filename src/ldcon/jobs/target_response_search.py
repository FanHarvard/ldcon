"""Pure adaptive target-FF / requested-chunk planner over completed round trips.

The caller evaluates EVERY leg with the same fixed settling profile and enforces
owner authority, foreground priority, deadlines and actual round-trip count.
Pass ``criteria_revision`` to verify that each round names that frozen profile.
This module proposes only scale and requested chunk; it never changes criteria,
performs I/O, or claims the owner actually used the requested chunk size.

Round schema::

    {"scale": 1.0, "step_hz": 100e6, "status": "completed",
     "criteria_revision": "fixed-profile-hash",
     "trials": [
       {"direction": "out", "analysis": {"status": "settled",
         "settling_from_request_s": 3.0}, "history": {"integrity_errors": []}},
       {"direction": "back", "analysis": {"status": "settled",
         "settling_from_request_s": 2.8}, "history": {"integrity_errors": []}}],
     "feedforward_identification": {"status": "static_candidate_only",
       "target_feedforward_scale": 1.1,
       "trials": [{"status": "static_estimate", "scale": 1.1,
                   "uncertainty_scale": .03}, ...]}}

Exactly two ordered legs may omit direction (out then back). Explicit false
quality/coverage validity or integrity errors invalidate a round regardless of
its settled label. A qualified ``not_settled`` analysis is a goal failure, while
``inconclusive`` means missing evidence and never justifies a smaller chunk.
"""
import math
import statistics

MIN_SCALE, MAX_SCALE = .5, 2.
MIN_STEP_HZ, MAX_STEP_HZ = 10e6, 100e6
DEFAULT_GOAL_S = 3.
MAX_ROUNDTRIPS, MAX_BUDGET_S = 12, 1200.
_TERMINAL = frozenset(('denied', 'rejected', 'unknown', 'unknown_write', 'uncertain',
    'uncertain_write', 'write_unknown', 'write_uncertain', 'unknown_effect',
    'uncertain_output', 'ownership_lost', 'failed', 'write_failed', 'cancelled',
    'preempted', 'superseded', 'interrupted'))
_COMPLETE = frozenset(('completed', 'succeeded', 'measured', 'inconclusive'))


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _hazard(row):
    if not isinstance(row, dict):
        return None
    if row.get('status') in _TERMINAL:
        return row['status']
    if row.get('uncertain_output') is True or row.get('write_unknown') is True:
        return 'uncertain_write'
    for key in ('command', 'receipt', 'write_receipt', 'owner_result'):
        found = _hazard(row.get(key))
        if found:
            return found
    for leg in row.get('trials', []) if isinstance(row.get('trials', []), list) else []:
        found = _hazard(leg)
        if found:
            return found
    return None


def _qualified(row, goal_s):
    legs = row.get('trials')
    if row.get('status') not in _COMPLETE or not isinstance(legs, list) or len(legs) != 2:
        return dict(qualified=False, reason='missing_completed_outward_or_return_leg')
    directions = [leg.get('direction') for leg in legs if isinstance(leg, dict)]
    if directions not in ([None, None], ['out', 'back'], ['outward', 'return']):
        return dict(qualified=False, reason='outward_and_return_identity_missing')
    times = []
    for leg in legs:
        analysis, history = leg.get('analysis') or {}, leg.get('history') or {}
        if (history.get('integrity_errors') or leg.get('quality_valid') is False
                or analysis.get('quality_valid') is False or analysis.get('coverage_valid') is False
                or history.get('quality_valid') is False or row.get('quality_valid') is False):
            return dict(qualified=False, reason='quality_or_history_invalid')
        state = analysis.get('status')
        if state == 'not_settled':
            times.append(None)
        elif state == 'settled' and _finite(analysis.get('settling_from_request_s')) and analysis['settling_from_request_s'] >= 0:
            times.append(analysis['settling_from_request_s'])
        else:
            return dict(qualified=False, reason='settling_evidence_inconclusive')
    return dict(qualified=True, passes=all(t is not None and t <= goal_s for t in times),
        worst_settle_s=max(times) if all(t is not None for t in times) else None,
        leg_settling_s=times, reason='same_fixed_criterion_both_directions')


def _static(row):
    fit = row.get('feedforward_identification') or {}
    if fit.get('status') != 'static_candidate_only':
        return None
    if not isinstance(fit.get('trials'), list):
        return None
    estimates = [r for r in fit['trials'] if isinstance(r, dict) and r.get('status') == 'static_estimate']
    if len(estimates) != 2:
        return None
    if any(not _finite(r.get('scale')) or r['scale'] <= 0 or
           not _finite(r.get('uncertainty_scale')) or r['uncertainty_scale'] < 0 for r in estimates):
        return None
    low = max(r['scale']-r['uncertainty_scale'] for r in estimates)
    high = min(r['scale']+r['uncertainty_scale'] for r in estimates)
    if low > high or high-low >= MAX_SCALE-MIN_SCALE:
        return None  # Conflicting directions or an uninformative interval.
    scale = fit.get('target_feedforward_scale', statistics.median(r['scale'] for r in estimates))
    if not _finite(scale) or not min(r['scale'] for r in estimates) <= scale <= max(r['scale'] for r in estimates):
        return None
    # A supported estimate outside the approved scale range supports only a
    # partial correction toward that estimate, never an expanded scale limit.
    return dict(scale=min(MAX_SCALE, max(MIN_SCALE, scale)), interval=[low, high],
                clipped=not MIN_SCALE <= scale <= MAX_SCALE)


def plan(rounds, *, goal_s=DEFAULT_GOAL_S, initial_scale=1., elapsed_s=0.,
         max_roundtrips=MAX_ROUNDTRIPS, budget_s=MAX_BUDGET_S,
         criteria_revision=None, step_resolution_hz=1e6):
    """Return one proposal, best measured passing candidate, or inconclusive.

    Selection is lexicographic: largest requested chunk, then smallest worst
    out/back request-to-settling time. FF is assessed before reducing a failed
    chunk. Chunk refinement bisects a measured pass/fail bracket; it is not an
    exhaustive grid. Missing evidence gets at most one identical repeat.
    ``elapsed_s`` must be measured from the ORIGINAL host task start.
    """
    if not isinstance(rounds, list) or any(not isinstance(r, dict) for r in rounds):
        raise ValueError('Completed round records must be a list of objects')
    if not _finite(goal_s) or goal_s <= 0:
        raise ValueError('A positive fixed settling goal is required')
    if criteria_revision is not None and (not isinstance(criteria_revision, str) or not criteria_revision):
        raise ValueError('Criterion revision must be a nonempty stable string identifier')
    if not _finite(initial_scale) or not MIN_SCALE <= initial_scale <= MAX_SCALE:
        raise ValueError('Initial scale must lie in the approved .5–2 range')
    if (type(max_roundtrips) is not int or not 1 <= max_roundtrips <= MAX_ROUNDTRIPS
            or not _finite(budget_s) or not 0 < budget_s <= MAX_BUDGET_S
            or not _finite(elapsed_s) or elapsed_s < 0
            or not _finite(step_resolution_hz) or not 0 < step_resolution_hz <= MAX_STEP_HZ-MIN_STEP_HZ):
        raise ValueError('Planner budgets may only narrow 12 round trips / 20 minutes')
    base = dict(goal_s=goal_s, criteria_revision=criteria_revision,
        criteria_verification='explicit_revision' if criteria_revision is not None else 'caller_fixed_profile_contract',
        remaining_roundtrips=max(0, max_roundtrips-len(rounds)),
        remaining_s=max(0., budget_s-elapsed_s),
        scope='requested chunk; actual owner chunk and physical authority remain host responsibilities')
    def stop(reason, best=None):
        if best is None:
            return dict(base, action='inconclusive', parameters={}, reason=reason)
        row, assessment, index = best
        return dict(base, action='select', parameters=dict(scale=row['scale'], step_hz=row['step_hz']),
            round_index=index, worst_settle_s=assessment['worst_settle_s'], reason=reason,
            qualification='Best measured passing candidate; not proof of a global optimum or future stability')
    def propose(scale, step, reason):
        return dict(base, action='propose_trial', parameters=dict(scale=scale, step_hz=step), reason=reason)
    for row in rounds:
        if (not _finite(row.get('scale')) or not MIN_SCALE <= row['scale'] <= MAX_SCALE
                or not _finite(row.get('step_hz')) or not MIN_STEP_HZ <= row['step_hz'] <= MAX_STEP_HZ):
            raise ValueError('Recorded trial is outside the approved scale/chunk envelope')
        if row.get('criteria_revision') is not None and not isinstance(row['criteria_revision'], str):
            raise ValueError('Criterion revision must be a stable string identifier')
        hazard = _hazard(row)
        if hazard:
            return stop('Terminal '+hazard+' outcome: no further trial, selection or automatic replay')
    revisions = {r.get('criteria_revision') for r in rounds if r.get('criteria_revision') is not None}
    if (len(revisions) > 1 or (revisions and any(r.get('criteria_revision') is None for r in rounds))
            or (criteria_revision is not None and any(r.get('criteria_revision') != criteria_revision for r in rounds))):
        return stop('Fixed settling criterion revision changed or is missing; results cannot be ranked together')
    assessments = [_qualified(row, goal_s) for row in rounds]
    passing = [(r, a, i) for i, (r, a) in enumerate(zip(rounds, assessments)) if a.get('passes')]
    best = min(passing, key=lambda item:(-item[0]['step_hz'], item[1]['worst_settle_s'], item[2])) if passing else None
    if best and best[0]['step_hz'] == MAX_STEP_HZ:
        return stop('100 MHz meets the unchanged goal in both directions; do not reduce the step', best)
    if len(rounds) >= max_roundtrips or elapsed_s >= budget_s:
        return stop('Original round-trip or elapsed-time budget exhausted', best)
    if not rounds:
        return propose(initial_scale, MAX_STEP_HZ, 'Start at the largest approved requested step, 100 MHz, with the existing FF scale')
    latest, quality = rounds[-1], assessments[-1]
    if not quality['qualified']:
        repeated = sum(r['scale'] == latest['scale'] and r['step_hz'] == latest['step_hz']
                       and not a['qualified'] for r, a in zip(rounds, assessments))
        if repeated >= 2:
            return stop('Repeated missing/invalid evidence; do not infer a smaller useful step', best)
        return propose(latest['scale'], latest['step_hz'],
            'Repeat identical scale and step for fresh complete out/back evidence; a history gap does not justify shrinking')
    at_step = [(r, a, i) for i, (r, a) in enumerate(zip(rounds, assessments))
               if r['step_hz'] == latest['step_hz'] and a['qualified']]
    if not quality['passes']:
        fitted = next(((r, _static(r)) for r, _, _ in reversed(at_step) if _static(r) is not None), None)
        if fitted:
            _, fit = fitted
            matched = any(abs(r['scale']-fit['scale']) <= 1e-9 or fit['interval'][0] <= r['scale'] <= fit['interval'][1]
                          for r, _, _ in at_step)
            if not matched:
                return propose(fit['scale'], latest['step_hz'],
                    'Test the two-direction static FF estimate before reducing the requested step'+
                    ('; use the approved boundary as a partial correction' if fit['clipped'] else ''))
        else:
            unique = {r['scale'] for r, _, _ in at_step}
            numeric = [(r, a) for r, a, _ in at_step if a['worst_settle_s'] is not None]
            if len(unique) < 2:
                return stop('Goal failed, but no qualified static FF estimate or measured FF comparison supports another scale or a smaller step', best)
            if len(unique) == 2 and len(numeric) >= 2:
                ordered = sorted(numeric, key=lambda item:item[1]['worst_settle_s'])
                faster, slower = ordered[0], ordered[-1]
                if slower[1]['worst_settle_s']-faster[1]['worst_settle_s'] >= .1:
                    delta = faster[0]['scale']-slower[0]['scale']
                    candidate = min(MAX_SCALE, max(MIN_SCALE, faster[0]['scale']+math.copysign(min(abs(delta), .25), delta))) if delta else faster[0]['scale']
                    if all(abs(candidate-s) > 1e-9 for s in unique):
                        return propose(candidate, latest['step_hz'],
                            'One bounded FF continuation supported by improved measured worst-direction settling; no exhaustive scale grid')
        # Either an admissible static estimate was tested/consistent, or at
        # least two measured FF settings failed. Only now can chunk size change.
    failed_steps = sorted({r['step_hz'] for r, a in zip(rounds, assessments) if a['qualified'] and not a['passes']})
    if best:
        low = best[0]['step_hz']
        higher_failures = [step for step in failed_steps if step > low]
        high = min(higher_failures) if higher_failures else MAX_STEP_HZ
        if high-low <= step_resolution_hz:
            return stop('Largest passing requested step is resolved to the declared chunk search resolution', best)
        step = (low+high)/2 if higher_failures else MAX_STEP_HZ
        return propose(best[0]['scale'], step, 'Increase the passing step toward the nearest measured failing step; preserve the fixed settling criterion')
    if latest['step_hz'] <= MIN_STEP_HZ:
        return stop('Minimum approved requested step still fails after FF assessment')
    numeric = [(r, a) for r, a, _ in at_step if a['worst_settle_s'] is not None]
    chosen = min(numeric, key=lambda item:item[1]['worst_settle_s'])[0]['scale'] if numeric else latest['scale']
    return propose(chosen, max(MIN_STEP_HZ, latest['step_hz']/2),
        'The round trip misses the goal after FF evidence/comparison; reduce the requested step within 10–100 MHz')
