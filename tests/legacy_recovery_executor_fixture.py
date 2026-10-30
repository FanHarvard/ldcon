# Test-only archive. Never packaged, registered, or used by active recovery.
"""Recovery v2 preparation entry point and historical v1 evidence readers.

Active recovery uses recovery_procedure.relative_current_route.v1. The helpers
below that read v1 artifacts are retained for historical scientific records and
legacy alignment evidence; they are not selected by standalone/steady recovery.
"""
import copy

from ldcon.core.contracts import DeviceError
from ldcon.jobs.ecdl_mode_map import BoundedExperiment, checked_artifact, finite, seal
from .legacy_recovery_plan_fixture import (DEFAULTS as PLAN_DEFAULTS, SCHEMA as PLAN_SCHEMA, build_plan,
                                           piezo_hz_per_count_from_profile, validate_reference, validate_translation)
from .legacy_recovery_test_fixture import (DEFAULTS as TEST_DEFAULTS, perturbation_trials, replay_plan,
                                           validate_test)


SCHEMA = 'ldcon.ecdl-recovery.v1'
PLAN_KEYS = ('map_artifact', 'map_ref', 'selected_branch_id', 'selection_author', 'tolerance_hz', 'hop_threshold_hz',
             'stable_s', 'confirm_samples', 'metric_limits', 'context_mode', 'context_tolerances',
             'current_per_tec_v', 'piezo_hz_per_count')
TEST_KEYS = ('perturbations', 'max_perturbation_fraction', 'sample_interval_s', 'max_sample_age_s',
             'measurement_timeout_s', 'dwell_s', 'max_tec_perturbation_v')
# Plan fields carried verbatim into the merged artifact (the artifact is a plan with test evidence attached, so
# replay_plan / verify_reference consume it directly).
PLAN_FIELDS = ('map_sha256', 'branch_id', 'selection_author', 'selected_reference', 'policies', 'direction',
               'recipe', 'source_initial_context', 'verification', 'context_mode', 'context_tolerances',
               'current_per_tec_v', 'piezo_hz_per_count')
# timeout_s: the whole card (plan + every trial), as for every hardware card (BoundedExperiment deadline).
# verification_timeout_s: one verification (artifact verification.timeout_s; the lock's per-recovery deadline).
from ldcon.jobs.recovery_procedure import DEFAULTS as RECOVERY_DEFAULTS
DEFAULTS = {**RECOVERY_DEFAULTS, 'execution': 'prepare'}


def parameters(card, overrides):
    from ldcon.jobs.recovery_procedure import parameters as validate
    card = {**card, 'parameters': {**card.get('parameters', {}), 'execution': 'prepare'}}
    p = validate(card, {**overrides, 'execution': 'prepare'})
    return p


def runtime_specs(p):
    # The approved route/reference rule is immutable within a recovery task.
    return {}


def checked_recovery(value):
    """The merged artifact, hash-checked, with successful test evidence (verified trials: observed hops for
    current/PZT trials, verified translation for TEC trials)."""
    artifact = checked_artifact(value, SCHEMA)
    if not verified_evidence(artifact):
        raise ValueError('Tested recovery requires matching successful actual-hop test evidence (verified: true)')
    return artifact


def verified_evidence(artifact):
    trials = artifact.get('trials')
    if artifact.get('verified') is not True or not trials:
        return False
    for t in trials:
        if t.get('recovery_verified') is not True:
            return False
        if t.get('kind', 'hop') == 'hop' and t.get('hop_observed') is not True:
            return False
    return True


def experiment_parameters(artifact, *, sample_interval_s, measurement_timeout_s, max_sample_age_s=1.):
    """BoundedExperiment parameters of one execution of the tested procedure."""
    verification = artifact['verification']
    return dict(timeout_s=verification['timeout_s'],
                measurement_timeout_s=min(measurement_timeout_s, verification['timeout_s']),
                sample_interval_s=sample_interval_s, max_sample_age_s=max_sample_age_s)


def execute(experiment, artifact):
    """THE tested recovery procedure (rule F): replay the recorded route (restored or translated context) and verify
    the reference. Called by the steady lock's in-lock recovery and by the standalone recover card."""
    return replay_plan(experiment, artifact)


def check_target(artifact, target_frequency_hz):
    if target_frequency_hz is None:
        return
    reference = artifact['selected_reference']['frequency_hz']
    if abs(target_frequency_hz - reference) > artifact['verification']['tolerance_hz']:
        raise ValueError('Laser target %.6g Hz differs from the tested recovery reference %.6g Hz by more than '
                         'tolerance_hz %.6g' % (target_frequency_hz, reference, artifact['verification']['tolerance_hz']))


def run_tested_recovery(ports, p, *, clock, sleep, cancel, emit, **_):
    """Standalone recover.ecdl_mode_hop with recovery_artifact: the tested procedure, unlocked (PI off)."""
    artifact = checked_recovery(p['recovery_artifact'])
    if not ports['loop'].is_open_loop():
        raise DeviceError('Standalone mode recovery requires an open loop (stop the lock card first)')
    check_target(artifact, p.get('target_frequency_hz'))
    experiment = BoundedExperiment(ports, experiment_parameters(artifact,
        sample_interval_s=p['sample_interval_s'], measurement_timeout_s=p['measurement_timeout_s']),
        clock=clock, sleep=sleep, cancel=cancel, emit=emit)
    reference = artifact['selected_reference']
    emit('mode_recovery_start', dict(procedure='tested_recovery', recovery_sha256=artifact['artifact_sha256'],
        context_mode=artifact.get('context_mode', 'restore'), home=dict(experiment.expected),
        map_context={k: reference['outputs'][k] for k in ('temperature', 'piezo')}))
    restored = execute(experiment, artifact)
    final = dict(frequency_hz=restored['frequency_hz'], offset_hz=restored['frequency_hz'] - reference['frequency_hz'],
                 mode_id=restored['mode_id'], single_mode=restored['single_mode'], outputs=restored['outputs'])
    result = dict(status='succeeded', recovered=True, procedure='tested_recovery',
                  recovery_sha256=artifact['artifact_sha256'], branch_id=artifact['branch_id'],
                  recovery=restored['recovery'], home=dict(experiment.expected), path=experiment.commands,
                  final=final, pi_rearmed=False, lock_enabled=False)
    emit('mode_recovery_succeeded', dict(final, recovery=restored['recovery']))
    return result


def run_recovery_card(ports, p, *, runtime=None, laser=None, **arguments):
    """Prepare the common relative-route policy and optionally qualify explicit trials."""
    from ldcon.jobs.recovery_procedure import run
    return run(ports, parameters({'parameters': p}, {}), runtime=runtime, **arguments)
