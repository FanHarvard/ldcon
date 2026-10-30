"""Stationary adapter of the single relative-current-route recovery algorithm."""
from ldcon.jobs.recovery_stationary import StationaryRecovery
from ldcon.jobs.ecdl_mode_recovery import parameters as _parameters


def parameters(value):
    return None if value is None else _parameters({'parameters': value}, {})
