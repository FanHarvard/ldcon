"""Standalone branch-independent recovery; legacy class/map execution is retired."""
from ldcon.jobs.recovery_procedure import DEFAULTS, parameters as _parameters, run


def parameters(card, overrides):
    # These identify this public executor, not an inferred observation policy.
    card = {**card, 'parameters': {'execution':'recover', 'execution_mode':'deterministic',
                                 **card.get('parameters', {})}}
    p = _parameters(card, overrides)
    if p['execution'] != 'recover' or p['execution_mode'] != 'deterministic' or p['perturbations']:
        raise ValueError('Standalone recovery cannot execute preparation or qualification perturbations')
    return p


def validate(value, *, require_target=True):
    return _parameters({'parameters': parameters({'parameters': value}, {})}, {}, require_configured=True)


def run_mode_recovery(ports, p, **arguments):
    return run(ports, validate(p), **arguments)
