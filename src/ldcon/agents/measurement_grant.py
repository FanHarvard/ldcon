"""Host-issued finite measurement grants, separate from monitor authority files."""
import copy
from pathlib import Path
import time
import uuid

from experiment_core.records import atomic_json, digest, safe_id
from ldcon.core.records import read_json


def freeze_measurement_plan(laser):
    """Freeze only the saved laser plan; no card override or monitor fallback."""
    from ldcon.core.adjustment_plan import approved_adjustment_plan
    if 'measurement_plan' not in laser:
        return None
    plan = approved_adjustment_plan(laser['measurement_plan'])
    return plan if plan['enabled'] else None


def measurement_plan_source(laser, laser_revision):
    """Provenance for an admission snapshot, separate from executable bounds."""
    plan = freeze_measurement_plan(laser)
    if plan is None:
        return None
    return dict(source='laser.measurement_plan', laser_id=laser['id'],
                laser_revision=laser_revision, plan_revision=digest(plan))


def propose_measurement_plan_migration(laser, deck, effective_monitor_overrides=None, *, source_revision=None):
    """Return an offline proposal; never save a config, create a grant or start a card.

    Reusing the old monitor envelope requires explicit review of this proposal.
    Absent/disabled/ambiguous legacy authority is not silently enabled.
    """
    from ldcon.agents.agent_monitor import parameters
    if 'measurement_plan' in laser:
        raise ValueError('Laser already has an explicit measurement plan')
    cards = [c for c in deck.get('cards', []) if c.get('operation') == 'agent.monitor']
    if len(cards) != 1:
        raise ValueError('Migration requires exactly one saved monitor plan')
    plan = parameters(cards[0], effective_monitor_overrides or {})['background']
    if not plan['enabled']:
        raise ValueError('A disabled monitor plan cannot grant measurement authority')
    candidate = copy.deepcopy(laser)
    candidate['measurement_plan'] = copy.deepcopy(plan)
    return dict(proposal_only=True, document=candidate,
                provenance=dict(source='agent.monitor.background', card_id=cards[0]['id'],
                    source_revision=source_revision, plan_revision=digest(plan)),
                requires_human_review=True)


class MeasurementGrant:
    def __init__(self, directory, controller_directory, laser_id, bounds, *, lease_sha256, now=time.time):
        from ldcon.agents.agent_background import control_root
        self.now = now
        directory, controller = Path(directory), Path(controller_directory)
        if directory.parent != controller.parent or directory == controller:
            raise ValueError('Measurement and actual controller must be distinct jobs of one laser')
        job = safe_id(directory.name); controller_id = safe_id(controller.name)
        # The host admitted/froze this plan; card parameters cannot provide it.
        invocation = read_json(directory/'invocation.json', {})
        frozen = invocation.get('measurement_plan')
        if invocation.get('measurement_controller_job_id') != controller_id:
            raise ValueError('Measurement is not bound to this actual controller job')
        if not isinstance(lease_sha256,str) or len(lease_sha256)!=64:
            raise ValueError('A controller lease digest is required')
        if not isinstance(frozen, dict) or frozen != bounds or not bounds.get('enabled'):
            raise ValueError('Frozen approved measurement plan is unavailable')
        root = control_root(directory.parent.parent)/safe_id(laser_id)
        self.directory = root/'measurements'/job
        monitor = 'measure-'+job+'-'+uuid.uuid4().hex
        self.path = root/'monitors'/(monitor+'.json')
        self.control = self.directory/'control.json'
        for path in (root, root/'monitors', root/'measurements', self.directory, self.path, self.control):
            if path.is_symlink():
                raise ValueError('Measurement authority must not use symlinks')
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        previous = read_json(self.control, {})
        if previous.get('enabled'):
            raise ValueError('An unresolved measurement grant already exists; do not replay')
        self.identity = dict(monitor_id=monitor, generation=uuid.uuid4().hex,
            controller_job_id=controller_id, measurement_job_id=job, grant_revision=digest(bounds))
        self.value = dict(source='measurement', schema_version=2, **self.identity,
            controller_lease_sha256=lease_sha256, bounds=copy.deepcopy(bounds), parameters_hash=digest(bounds), max_age_s=30., enabled=True)
        self.renew()

    def renew(self):
        value = dict(self.value, last_seen=self.now())
        atomic_json(self.path, value)
        atomic_json(self.control, value)

    def close(self):
        self.value['enabled'] = False
        self.renew()
