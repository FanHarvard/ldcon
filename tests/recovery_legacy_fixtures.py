"""Build archived v1 evidence for historical reader/alignment regression tests only.

This is not an active card runner. Production old recovery entry points reject
these inputs; tests exercise historical evidence without reviving runtime dispatch.
"""
import copy
from ldcon.jobs.ecdl_mode_map import BoundedExperiment, seal
from .legacy_recovery_plan_fixture import DEFAULTS as PLAN_DEFAULTS, build_plan, piezo_hz_per_count_from_profile
from .legacy_recovery_test_fixture import DEFAULTS as TEST_DEFAULTS, perturbation_trials
from .legacy_recovery_executor_fixture import PLAN_FIELDS, PLAN_KEYS


def archived_preparation(ports, supplied, *, laser=None, runtime=None, **arguments):
    p={**PLAN_DEFAULTS, **TEST_DEFAULTS, 'context_mode':'translate', 'verification_timeout_s':600., **supplied}
    if p.get('piezo_hz_per_count') is None and laser:
        from ldcon.jobs.ecdl_mode_map import selected_reference
        p['piezo_hz_per_count']=piezo_hz_per_count_from_profile(laser, selected_reference(p['map_artifact'],p.get('selected_branch_id'))[2]['frequency_hz'])
    plan=build_plan({**{k:p[k] for k in PLAN_KEYS},'timeout_s':p['verification_timeout_s']})
    experiment=BoundedExperiment(ports,p,**arguments)
    initial,trials,verified=perturbation_trials(experiment,plan,p)
    artifact=seal(dict(schema='ldcon.ecdl-recovery.v1',**{k:copy.deepcopy(plan[k]) for k in PLAN_FIELDS},
                      plan_sha256=plan['artifact_sha256'],initial=initial,trials=trials,commands=experiment.commands,
                      verified=verified,hop_cause='unknown',qualification='Historical mock evidence'))
    return dict(status='succeeded' if verified else 'inconclusive',artifact=artifact,verified=verified,recovery_verified=verified)
