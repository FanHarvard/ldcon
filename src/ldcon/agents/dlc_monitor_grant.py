"""Protected monitor lifetime for attached DLC parameter comparisons.

This grants only explicitly configured, owner-supported adjustment families.
It cannot grant target excursions or changes to physical policy.
The worker binds the immutable monitor invocation; the owner checks its lifetime
again at each effect and can restore without waiting for the model.
"""
from copy import deepcopy
import math
import os
from pathlib import Path
import time

from experiment_core.records import digest, safe_id
from ldcon.core.interface_schema import load_mapping
from ldcon.core.records import read_json


def validate_bounds(value):
    fields = {'enabled', 'controller_operation', 'families', 'timeout_s'}
    if (not isinstance(value, dict) or set(value) != fields
            or type(value['enabled']) is not bool
            or value['controller_operation'] != 'servo.dlcpro_frequency'
            or not isinstance(value['families'],list) or not value['families']
            or any(not isinstance(f,str) for f in value['families'])
            or len(set(value['families']))!=len(value['families'])
            or set(value['families'])-{'pi','current','slow_loop','feedforward'}
            or type(value['timeout_s']) not in (int, float)
            or not math.isfinite(value['timeout_s']) or not 1 <= value['timeout_s'] <= 86400):
        raise ValueError('DLC monitor requires explicit adjustment families and a finite lifetime')
    return deepcopy(value)


def check_grant(laser_id, binding, *, clock=time.time):
    if not isinstance(binding, dict) or set(binding) != {'monitor_id', 'generation', 'parameters_hash'}:
        raise ValueError('Invalid DLC monitor grant binding')
    safe_id(laser_id); safe_id(binding['monitor_id'])
    if any(not isinstance(binding[k], str) or not binding[k] for k in ('generation', 'parameters_hash')):
        raise ValueError('Incomplete DLC monitor generation')
    root = Path(os.environ.get('LDCON_ADAPTIVE_ROOT', ''))
    if not root.is_absolute() or not root.is_dir() or root.resolve() != root:
        raise ValueError('Protected monitor authority is unavailable')
    path = root/laser_id/'monitors'/(binding['monitor_id']+'.json')
    if any(p.is_symlink() for p in (path, path.parent, path.parent.parent)):
        raise ValueError('Monitor authority must not follow symlinks')
    grant = read_json(path, {})
    if not isinstance(grant, dict):raise ValueError('Monitor authority is unavailable')
    bounds = validate_bounds(grant.get('bounds'))
    stamp, age = grant.get('last_seen'), grant.get('max_age_s')
    if (grant.get('enabled') is not True or not bounds['enabled']
            or any(grant.get(k) != binding[k] for k in ('generation', 'parameters_hash'))
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in (stamp, age))
            or not 0 < age <= 180 or not 0 <= clock()-stamp <= age):
        raise ValueError('Monitor grant stopped, changed or expired')
    return grant


def bind(directory, p, laser):
    """Bind a monitor task to its own frozen parent, never model-supplied text."""
    if not p.get('monitor_job_id'):return None
    from ldcon.agents.agent_monitor import parameters
    from ldcon.agents.agent_background import control_root
    directory = Path(directory)
    monitor_id = safe_id(p['monitor_job_id'])
    parent = directory.parent/monitor_id
    if parent.is_symlink():raise ValueError('Monitor job must not be a symlink')
    meta = read_json(parent/'job.json', {})
    laser_id = laser['id']
    source = load_mapping((parent/'laser.yaml').read_text())
    invocation = read_json(parent/'invocation.json', {})
    deck = load_mapping((parent/'interface-deck.yaml').read_text())
    card = next((c for c in deck['cards'] if c['id'] == invocation.get('card_id')), None)
    task = read_json(directory/'invocation.json', {})
    if (meta.get('id') != monitor_id or source['id'] != laser_id
            or not card or card['operation'] != 'agent.monitor'):
        raise ValueError('Monitor belongs to another laser or operation')
    plan = parameters(card, invocation['parameters'])
    bounds = validate_bounds(plan['background'])
    if (plan['task_card_id'] != task['card_id'] or not plan['enabled']
            or not bounds['enabled'] or p['timeout_s'] > bounds['timeout_s']):
        raise ValueError('Task differs from the monitor-approved adjustment plan')
    root = control_root(directory.parent.parent)
    grant = read_json(root/laser_id/'monitors'/(monitor_id+'.json'), {})
    binding = dict(monitor_id=monitor_id, generation=grant.get('generation'), parameters_hash=digest(plan))
    live = check_grant(laser_id, binding)
    if live['bounds'] != bounds:raise ValueError('Monitor bounds differ from its frozen invocation')
    return binding
