"""Persistent DLC operating intent, separate from its physical output policy.

Mode changes use the same protected operator artifacts as other laser owners.
The enclosing DLC protocol mutex serializes a change with PI trial effects;
interruption restores coefficients continuously without writing an output.
"""
from pathlib import Path

from experiment_core.records import Journal
from ldcon.owners.operating_mode_owner import OperatingModeOwner


class TrialCoordinator:
    def __init__(self, owner):
        self.owner = owner

    @property
    def epoch(self):
        value = self.owner.pi_trial.active
        if value is None:return None
        return dict(operating_mode_revision=value.get('operating_mode_revision'),
            family=value['family'], state='requires_inspection' if self.owner.fault else 'active')


def attach(protocol, path):
    owner = protocol.owner
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    journal = Journal(path)
    coordinator = TrialCoordinator(owner)
    owner.operating_mode = OperatingModeOwner(journal, owner.laser_id,
        coordinator=lambda: coordinator, interrupt=owner.pi_trial.preempt)
    return owner.operating_mode


def status(owner):
    mode = getattr(owner, 'operating_mode', None)
    return mode.status() if mode is not None else None


def dispatch(protocol, action, request):
    mode = getattr(protocol.owner, 'operating_mode', None)
    if mode is None:
        raise ValueError('DLC operating mode authority is not configured')
    return mode.dispatch(dict(action=action, **request))
