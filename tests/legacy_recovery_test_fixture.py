# Test-only archive. Never packaged, registered, or used by active recovery.
"""Induce a bounded optical hop and test the recorded directed replay.

This module holds the shared, tested procedure of the ECDL recovery (rule F): validate_context, check_optical_context,
replay_plan (restore or translate the map context, replay the recorded current route) and verify_reference. The
merged card ``prepare.ecdl_recovery`` (ecdl_recovery.py) builds the plan and runs ``perturbation_trials``; the steady
lock (ecdl_mapped_recovery.py) and the standalone ``recover.ecdl_mode_hop`` card execute the same ``replay_plan``.
The old standalone operation ``test.ecdl_recovery`` is retired; ``run_recovery_test`` stays importable for old jobs.

This test starts and ends with native PI disabled. Absence of a measured hop
is inconclusive. Unknown/postwrite failures leave the outputs and lease to the
owner; there is no speculative restore or retry of uncertain writes.
"""
import copy
from dataclasses import asdict

from ldcon.core.contracts import DeviceError
from ldcon.jobs.ecdl_mode_map import BoundedExperiment, checked_artifact, finite, seal
from .legacy_recovery_plan_fixture import SCHEMA as PLAN_SCHEMA, validate_reference


SCHEMA = 'ldcon.ecdl-recovery-test.v1'
DEFAULTS = dict(plan_artifact=None, plan_ref=None, perturbations=[], max_perturbation_fraction=.05,
                sample_interval_s=.5, max_sample_age_s=1., measurement_timeout_s=120.,
                timeout_s=1800., dwell_s=1., max_tec_perturbation_v=.05)
# A perturbation is {axis: current|piezo, offset_fraction} (fraction of the owner's present interval; a hop is expected)
# or {axis: temperature, offset} (TEC axis units, |offset| <= max_tec_perturbation_v): the trial moves the TEC, then runs
# the TRANSLATED recovery without restoring the TEC and verifies against the predicted frequency (context_mode translate).


def parameters(card, overrides):
    p = copy.deepcopy({**DEFAULTS, **card['parameters'], **overrides})
    if set(p) - DEFAULTS.keys():
        raise ValueError('Unknown recovery test parameter')
    if p['plan_artifact'] is not None and not isinstance(p['plan_artifact'], dict):
        raise ValueError('Expected recovery plan artifact')
    validate_reference(p['plan_ref'])
    validate_test(p)
    return p


def validate_test(p):
    for key in ('max_perturbation_fraction', 'sample_interval_s', 'max_sample_age_s',
                'measurement_timeout_s', 'timeout_s', 'dwell_s', 'max_tec_perturbation_v'):
        if not finite(p[key]) or p[key] <= 0:
            raise ValueError('Positive ' + key + ' required')
    if p['max_perturbation_fraction'] > .25 or p['timeout_s'] > 86400 or p['sample_interval_s'] < .05:
        raise ValueError('Invalid finite test budget')
    perturbations = p['perturbations']
    if not isinstance(perturbations, list) or len(perturbations) > 20:
        raise ValueError('Expected at most 20 explicit perturbations')
    for step in perturbations:
        if not isinstance(step, dict):
            raise ValueError('Perturbations require bounded current or PZT offset fractions')
        if step.get('axis') == 'temperature':
            if (set(step) != {'axis', 'offset'} or not finite(step['offset'])
                    or not 0 < abs(step['offset']) <= p['max_tec_perturbation_v']):
                raise ValueError('TEC perturbations require {axis: temperature, offset} with 0 < |offset| <= max_tec_perturbation_v')
        elif (set(step) != {'axis', 'offset_fraction'}
                or step['axis'] not in ('current', 'piezo') or not finite(step['offset_fraction'])
                or not 0 < abs(step['offset_fraction']) <= p['max_perturbation_fraction']):
            raise ValueError('Perturbations require bounded current or PZT offset fractions')


def runtime_specs(p):
    from ldcon.jobs.runtime_parameters import ParameterSpec
    return {key: ParameterSpec(boundaries=('between_points',), maximum=p[key],
                              description='次の独立試行から取得間隔または欠測期限を短縮')
            for key in ('sample_interval_s', 'measurement_timeout_s')}


def validate_context(experiment, plan):
    for name, policy in experiment.policies.items():
        old = plan['policies'][name]
        # Each lease may tighten low/high around a different activation point.
        # This must not reinterpret the absolute recorded route. All route
        # destinations still have to fit the new, owner-approved interval.
        current = asdict(policy)
        if any(old.get(key) != value for key, value in current.items() if key not in ('low', 'high')):
            raise DeviceError('Recovery plan is stale: axis policy/calibration revision differs')
        value = plan['selected_reference']['outputs'][name]
        if not policy.low <= value <= policy.high:
            raise DeviceError('Recovery reference lies outside current owner envelope')
    for step in plan['recipe']:
        policy = experiment.policies[step['axis']]
        if not finite(step['value']) or not policy.low <= step['value'] <= policy.high:
            raise ValueError('Recovery recipe exceeds current owner envelope')


def context_mode(plan):
    return plan.get('context_mode', 'restore')


def plan_translation(experiment, plan):
    """Translate mode (2026-10-27): keep the present TEC/PZT (within context_tolerances of the map context), shift the
    current recipe by current_per_tec_v x dTEC (I = I0 + prior x dTEC, dTEC = present TEC - reference TEC; -0.07 on
    sr422, 2026-10-27), and predict the reference frequency at the present context.
    Refusals (rule K class iii, before any write) name the axis."""
    reference = plan['selected_reference']
    tolerances = plan['context_tolerances']
    deltas = {}
    for axis in ('temperature', 'piezo'):
        deltas[axis] = experiment.expected[axis] - reference['outputs'][axis]
        if abs(deltas[axis]) > tolerances[axis]:
            raise DeviceError('Present %s differs from the map context by %.6g (context tolerance %.6g); translated '
                              'recovery refused' % (axis, deltas[axis], tolerances[axis]))
    temperature_slope = experiment.policies['temperature'].frequency_hz_per_unit
    if deltas['temperature'] and not finite(temperature_slope):
        raise DeviceError('Translated recovery requires the temperature axis frequency_hz_per_unit calibration')
    piezo_slope = plan.get('piezo_hz_per_count')
    if deltas['piezo'] and not finite(piezo_slope):
        raise DeviceError('Translated recovery requires piezo_hz_per_count')
    predicted = (reference['frequency_hz'] + (temperature_slope or 0.) * deltas['temperature']
                 + (piezo_slope or 0.) * deltas['piezo'])
    shift = plan['current_per_tec_v'] * deltas['temperature']
    policy = experiment.policies['current']
    recipe = []
    for step in plan['recipe']:
        if step['action'] == 'restore_context':
            continue
        value = step['value'] + shift
        if policy.resolution:
            value = round(value / policy.resolution) * policy.resolution
        if not policy.low <= value <= policy.high:
            raise DeviceError('Translated recovery recipe (current shift %.6g) leaves the owner interval [%.6g, %.6g]'
                              % (shift, policy.low, policy.high))
        recipe.append(dict(step, value=value))
    expected_outputs = dict(temperature=experiment.expected['temperature'], piezo=experiment.expected['piezo'],
                            current=recipe[-1]['value'] if recipe else reference['outputs']['current'] + shift)
    return dict(context_mode='translate', delta_temperature=deltas['temperature'], delta_piezo=deltas['piezo'],
                current_shift=shift, predicted_frequency_hz=predicted, recipe=recipe,
                expected_outputs=expected_outputs, temperature_hz_per_unit=temperature_slope,
                piezo_hz_per_count=piezo_slope)


def good_reference(row, plan, translation=None):
    reference = plan['selected_reference']
    verification = plan['verification']
    observation = row['observation']
    analysis = row['fringe_analysis']
    context_ok = (analysis.get('revision') == reference['fringe_analysis'].get('revision')
                  and analysis.get('period_reference_px') == reference['fringe_analysis'].get('period_reference_px')
                  and analysis.get('classification_revision') == reference['fringe_analysis'].get('classification_revision'))
    if translation is not None:
        # Translate mode: frequency within tolerance of the PREDICTED reference and the same calibration context;
        # single mode / metrics are reported (optical_quality), not required (rule K).
        return (context_ok and abs(row['frequency_hz'] - translation['predicted_frequency_hz']) <= verification['tolerance_hz'])
    return (row['single_mode'] and not observation['maintenance_required']
            and abs(row['frequency_hz'] - reference['frequency_hz']) <= verification['tolerance_hz']
            and analysis.get('qualified') is True and analysis.get('period_qualified') is True
            and context_ok
            and all(finite(row['metrics'].get(k)) and row['metrics'][k] <= value
                    for k, value in verification['metric_limits'].items()))


def optical_quality(row, plan):
    """Report-only spectral quality of a verified row (rule K)."""
    analysis = row['fringe_analysis']
    limits = plan['verification']['metric_limits']
    return dict(single_mode=row['single_mode'], maintenance_required=row['observation']['maintenance_required'],
                qualified=analysis.get('qualified') is True and analysis.get('period_qualified') is True,
                metric_limits_met=all(finite(row['metrics'].get(k)) and row['metrics'][k] <= v for k, v in limits.items()),
                metrics=copy.deepcopy(row['metrics']))


def check_optical_context(row, plan):
    """Reject stale calibration before a replay writes any axis."""
    actual = row['fringe_analysis']
    expected = plan['selected_reference']['fringe_analysis']
    if expected.get('classification_revision') is not None:
        keys = ('classification_revision',)
    else:
        keys = ('revision', 'period_reference_px')
    for key in keys:
        if actual.get(key) != expected.get(key):
            raise DeviceError('Recovery plan is stale: optical classification context differs: ' + key)


def verify_reference(experiment, plan, *, stage, translation=None, require_outputs=True):
    """Fresh samples at the (restored or translated) reference outputs; the row is annotated with the context mode,
    the predicted and measured frequency and the report-only optical quality. ``require_outputs=False`` verifies
    the branch in place (tune.ecdl_operating_point replay_mode auto/never: the laser already sits on the
    reference branch, possibly at slightly different outputs than the recorded map point)."""
    began = experiment.clock()
    stable = None
    count = 0
    previous = None
    verification = plan['verification']
    reference = plan['selected_reference']
    expected_outputs = reference['outputs'] if translation is None else translation['expected_outputs']
    while experiment.clock() - began < verification['timeout_s']:
        row = experiment.read(stage, require_frequency=True)
        for name, policy in experiment.policies.items():
            if require_outputs and abs(row['outputs'][name] - expected_outputs[name]) > max(policy.resolution, 1e-8):
                raise DeviceError('Recovery reference output differs: ' + name)
        stamp = row['acquired_s']
        if previous is not None and stamp - previous > max(3 * experiment.p['sample_interval_s'], 1.):
            stable = None
            count = 0
        if good_reference(row, plan, translation):
            if stable is None:
                stable = stamp
            count += 1
            if count >= verification['confirm_samples'] and stamp - stable >= verification['stable_s']:
                row['recovery'] = dict(
                    context_mode='restore' if translation is None else 'translate',
                    delta_temperature=0. if translation is None else translation['delta_temperature'],
                    delta_piezo=0. if translation is None else translation['delta_piezo'],
                    predicted_frequency_hz=reference['frequency_hz'] if translation is None else translation['predicted_frequency_hz'],
                    measured_frequency_hz=row['frequency_hz'],
                    optical_quality=optical_quality(row, plan))
                return row
        else:
            stable = None
            count = 0
        previous = stamp
        experiment.wait(experiment.p['sample_interval_s'])
    raise DeviceError('Original optical context did not reappear within recovery deadline')


def replay_plan(experiment, plan):
    """Replay exact discovery history, not just a reverse move to old current.

    restore mode: TEC/PZT back to the map context, then the recorded current route. translate mode: TEC/PZT kept
    (within context_tolerances), current route shifted by the drift prior, verification against the predicted
    frequency. This is THE recovery procedure (rule F) shared by the preparation test, the running lock and the
    standalone recover card.
    """
    validate_context(experiment, plan)
    translation = plan_translation(experiment, plan) if context_mode(plan) == 'translate' else None
    recipe = plan['recipe'] if translation is None else translation['recipe']
    experiment.current_recovery(True)
    check_optical_context(experiment.read('replay optical calibration check', require_frequency=False), plan)
    previous_frequency_requirement = experiment.require_frequency
    experiment.require_frequency = False
    for step in recipe:
        if step['action'] == 'restore_context':
            policy = experiment.policies[step['axis']]
            if abs(experiment.expected[step['axis']] - step['value']) <= max(policy.resolution / 2, 1e-10):
                continue
            experiment.current_recovery(False)
        else:
            experiment.current_recovery(True)
        experiment.move(step['axis'], step['value'], step['action'])
        if step['action'] == 'directed_scan':
            experiment.wait(max(step['dwell_s'], experiment.policies['current'].settle_s))
            experiment.read('recovery directed scan')
    experiment.current_recovery(False)
    experiment.require_frequency = previous_frequency_requirement
    return verify_reference(experiment, plan, stage='recovery optical verification', translation=translation)


def perturbation_trials(experiment, plan, p, *, initial_replay=True):
    """The test half of the procedure: for each explicit perturbation, perturb -> replay_plan -> verify.
    Returns (initial verification row, trials, verified). Used by the merged recovery card, whose laser sits at the
    end of the map scan: the first execution of the procedure (initial_replay) brings it to the reference and is the
    initial verification. The retired test runner expected the laser already at the reference (initial_replay=False)."""
    validate_context(experiment, plan)
    reference = plan['selected_reference']
    translate = context_mode(plan) == 'translate'
    # Resolve and validate all deliberate excursions before the first output.
    destinations = []
    for step in p['perturbations']:
        policy = experiment.policies[step['axis']]
        if step['axis'] == 'temperature':
            if not translate:
                raise ValueError('TEC perturbations test the translated recovery; they require context_mode translate')
            destination = experiment.expected['temperature'] + step['offset']
            if abs(destination - reference['outputs']['temperature']) > plan['context_tolerances']['temperature']:
                raise ValueError('TEC perturbation would leave the context tolerance of the translated recovery')
        else:
            destination = reference['outputs'][step['axis']] + step['offset_fraction'] * policy.span
        if not policy.low <= destination <= policy.high:
            raise ValueError('Perturbation would leave owner-approved interval')
        destinations.append((step['axis'], destination))
    # A stale map must never authorize a deliberate hop from an unknown branch.
    if initial_replay:
        initial = replay_plan(experiment, plan)
    else:
        initial = verify_reference(experiment, plan, stage='initial test optical verification',
                                   translation=plan_translation(experiment, plan) if translate else None)
    verification = plan['verification']
    trials = []
    for trial, (axis, destination) in enumerate(destinations):
        experiment.between_points()
        experiment.current_recovery(axis == 'current')
        perturbation_samples = [experiment.read('before deliberate perturbation')]
        experiment.observation_observer = perturbation_samples.append
        experiment.move(axis, destination, 'intentional hop perturbation' if axis != 'temperature' else 'TEC drift perturbation')
        experiment.wait(max(p['dwell_s'], experiment.policies[axis].settle_s))
        samples = []
        # Confirmation samples must all be new, valid, consistently displaced.
        for _ in range(verification['confirm_samples']):
            row = experiment.read('intentional hop confirmation')
            samples.append(row)
            experiment.wait(p['sample_interval_s'])
        experiment.observation_observer = None
        abrupt = any(abs(b['frequency_hz'] - a['frequency_hz']) > verification['hop_threshold_hz']
                     for a, b in zip(perturbation_samples, perturbation_samples[1:]))
        hop = abrupt and all(abs(r['frequency_hz'] - reference['frequency_hz']) > verification['hop_threshold_hz']
                  for r in samples)
        # Even when no hop occurred, replay validates the final safe context;
        # that is not counted as proof of recovery from a real hop. A TEC trial
        # (translate mode) proves the translated recovery at a drifted context
        # instead: TEC kept, recipe shifted, frequency at the predicted value.
        restored = replay_plan(experiment, plan)
        translation_trial = axis == 'temperature'
        verified = (abs(restored['frequency_hz'] - restored['recovery']['predicted_frequency_hz']) <= verification['tolerance_hz']
                    and restored['recovery']['delta_temperature'] != 0.) if translation_trial else hop
        result = dict(trial=trial, axis=axis, destination=destination,
                      kind='translation' if translation_trial else 'hop',
                      context_mode=restored['recovery']['context_mode'],
                      hop_observed=hop, confirmation_samples=samples,
                      abrupt_transition_observed=abrupt,
                      restored=restored, recovery_verified=verified,
                      translation={k: restored['recovery'][k] for k in ('delta_temperature', 'delta_piezo',
                                   'predicted_frequency_hz', 'measured_frequency_hz')},
                      status=('translation_verified' if translation_trial else 'verified') if verified
                             else ('translation_unverified' if translation_trial else 'inconclusive_no_hop'))
        trials.append(result)
        experiment.emit('ecdl_recovery_test_trial', dict(trial=trial, axis=axis, kind=result['kind'],
            destination=destination, hop_observed=hop, abrupt_transition_observed=abrupt,
            context_mode=result['context_mode'], translation=result['translation'],
            recovery_verified=verified, status=result['status'], restored_outputs=restored['outputs'],
            restored_frequency_hz=restored['frequency_hz']))
    verified = bool(trials) and all(t['recovery_verified'] for t in trials)
    return initial, trials, verified


def run_recovery_test(ports, p, **arguments):
    """Retired standalone operation test.ecdl_recovery (kept importable; prepare.ecdl_recovery runs the same trials)."""
    validated = parameters(dict(parameters=p), {})
    p.clear()
    p.update(validated)
    if p['plan_artifact'] is None or not p['perturbations']:
        raise ValueError('An explicit recovery plan and perturbations are required')
    plan = checked_artifact(p['plan_artifact'], PLAN_SCHEMA)
    experiment = BoundedExperiment(ports, p, **arguments)
    initial, trials, verified = perturbation_trials(experiment, plan, p, initial_replay=False)
    artifact = seal(dict(schema=SCHEMA, plan_sha256=plan['artifact_sha256'],
                         map_sha256=plan['map_sha256'], branch_id=plan['branch_id'],
                         initial=initial, trials=trials, commands=experiment.commands,
                         verified=verified, hop_cause='unknown',
                         qualification='Fresh optical context reproduced; physical longitudinal mode number not independently measured'))
    experiment.emit('ecdl_recovery_test_artifact', dict(schema=SCHEMA,
        artifact_sha256=artifact['artifact_sha256'], plan_sha256=artifact['plan_sha256'],
        branch_id=artifact['branch_id'], trials=len(trials), verified=verified))
    return dict(status='succeeded' if verified else 'inconclusive', artifact=artifact,
                recovery_verified=verified, lock_enabled=False, outputs=dict(experiment.expected))
