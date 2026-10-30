"""Read historical v1 test parameters. The map replay executor is retired."""
import copy

from ldcon.jobs.ecdl_mode_map import finite
from ldcon.jobs.ecdl_recovery_plan import SCHEMA as PLAN_SCHEMA, validate_reference


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
                              description='Archived independent-trial observation interval or missing-data deadline')
            for key in ('sample_interval_s', 'measurement_timeout_s')}


def run_recovery_test(*args, **kwargs):
    raise ValueError('Retired card: prepare.ecdl_recovery; old map replay execution is unavailable')
