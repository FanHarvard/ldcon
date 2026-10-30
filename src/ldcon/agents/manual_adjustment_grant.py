"""A finite manual task may borrow saved approved bounds, not monitoring duties.

The existing owner grant protocol is reused. No physical limit is expanded and
no monitor is started or enabled. Only the host task worker renews this grant.
"""
import copy
import time
from pathlib import Path

from experiment_core.records import atomic_json, digest, safe_id
from ldcon.core.records import read_json


def plan(directory, p):
    from ldcon.core.interface_schema import load_mapping
    from ldcon.agents.agent_monitor import parameters
    directory=Path(directory)
    invocation=read_json(directory/'invocation.json')
    deck=load_mapping((directory/'interface-deck.yaml').read_text())
    task=next(c for c in deck['cards'] if c['id']==invocation['card_id'])
    if task['operation']!='agent.task' or p['instruction_profile']!='adjustment':
        raise ValueError('Only an adjustment task may request a manual grant')
    card=next((c for c in deck['cards'] if c['id']==p.get('adjustment_plan_card_id','agent-monitor')),None)
    if not card or card['operation']!='agent.monitor':
        raise ValueError('Select a saved approved adjustment plan card')
    # The host freezes assignment overrides as well as the shared deck defaults.
    frozen=invocation.get('adjustment_plan')
    if not isinstance(frozen,dict):raise ValueError('Frozen approved adjustment plan is missing')
    monitor=parameters(card,frozen)
    if monitor['task_card_id']!=task['id'] or not monitor['background']['enabled']:
        raise ValueError('This task has no saved approved adjustment plan')
    bounds=copy.deepcopy(monitor['background'])
    family=p.get('adjustment_family','auto')
    families=bounds.get('families',['current'])
    if family!='auto':
        if family not in families:raise ValueError('Selected adjustment is outside approved bounds')
        bounds['families']=[family]
        bounds['adjustment_parameters']={k:v for k,v in bounds.get('adjustment_parameters',{}).items() if k==family}
        if family!='pi':bounds.pop('pid_parameters',None)
    return bounds


class ManualGrant:
    def __init__(self,directory,p,root,laser_id):
        from ldcon.agents.agent_background import control_root
        self.directory=Path(directory);self.task_id=safe_id(self.directory.name)
        self.bounds=plan(directory,p)
        self.identifier='task-'+self.task_id
        parent=control_root(root)/safe_id(laser_id)/'monitors'
        if any(v.is_symlink() for v in (parent,parent.parent)):
            raise ValueError('Trusted manual grant must not be a symlink')
        parent.mkdir(parents=True,exist_ok=True)
        self.path=parent/(self.identifier+'.json')
        if self.path.exists() or self.path.is_symlink():
            raise ValueError('Manual grant already exists; interrupted tasks are not replayed')
        self.value=dict(source='manual_task',task_id=self.task_id,generation=digest([self.task_id,time.time()]),
            bounds=self.bounds,parameters_hash=digest(self.bounds),max_age_s=30.,enabled=True)
        self.last=0.

    def renew(self):
        now=time.time()
        if now-self.last>=2:
            atomic_json(self.path,{**self.value,'last_seen':now});self.last=now

    def close(self):
        atomic_json(self.path,{**self.value,'last_seen':time.time(),'enabled':False})
