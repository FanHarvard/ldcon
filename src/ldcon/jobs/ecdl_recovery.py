"""Preparation entry point for the branch-independent recovery v2 algorithm."""
from ldcon.jobs.recovery_procedure import DEFAULTS as RECOVERY_DEFAULTS, checked_recovery

SCHEMA = 'ldcon.ecdl-recovery.v2'
DEFAULTS = {**RECOVERY_DEFAULTS, 'execution': 'prepare'}


def parameters(card, overrides):
    from ldcon.jobs.recovery_procedure import parameters as validate
    card = {**card, 'parameters': {**card.get('parameters', {}), 'execution': 'prepare'}}
    return validate(card, {**overrides, 'execution': 'prepare'})


def runtime_specs(p):
    # Route/reference policy stays frozen within a finite preparation invocation.
    return {}


def run_recovery_card(ports, p, *, runtime=None, laser=None, **arguments):
    from ldcon.jobs.recovery_procedure import run
    return run(ports, parameters({'parameters': p}, {}), runtime=runtime, **arguments)


def run_tested_recovery(*args, **kwargs):
    raise ValueError('Legacy recovery execution retired; migrate to relative_current_route.v1')
