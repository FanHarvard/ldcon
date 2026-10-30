"""Preparation task transport is offline; evidence cannot authorize wider output."""
import copy
import json
from types import SimpleNamespace

import pytest

from ldcon.core.records import atomic_json, read_json
from ldcon.jobs import recovery_decisions as decisions
from ldcon.jobs.ecdl_recovery import parameters
from ldcon.web.agent_task_api import admission_path, pin_invocation
from .test_agent_tasks import task
from .test_ecdl_recovery_card import Plant
from .test_recovery_reference_v2 import config, history


def fixture(task):
    from ldcon.agents.resident_agent import AgentStore
    plant=Plant(); plant.now=12.
    p=parameters({'parameters':config()},dict(execution_mode='agent',execution_profile='specialist',
        agent_card_id='resident',agent_timeout_s=300., perturbations=[]))
    p['history']=[dict(row,frequency_hz=plant.frequency(), outputs=dict(current=.5,temperature=.48,piezo=.46)) for row in history()]
    card=dict(id='prepare',operation='prepare.ecdl_recovery',parameters={},
              agent_execution=dict(mode='required',task_profile='specialist'))
    store=AgentStore(task.root);key=store.key(task.meta,'resident')
    selected=(store,task.meta,key,dict(job=None,session=None,provider=None,status='absent'))
    pin_invocation(task.root,task.meta['id'],card,p,laser_id='laser',meta=task.meta)
    return plant,p,card,selected


def test_preparation_pins_profile_and_deterministic_does_not_call_a_model(task):
    plant,p,card,selected=fixture(task)
    assert read_json(admission_path(task.root,'task1'))['profile']['id']=='specialist'
    assert pin_invocation(task.root,'plain',card,{**p,'execution_mode':'deterministic'},laser_id='laser',meta=task.meta) is None
    out=decisions.run_card(task.directory,dict(id='laser'),card,plant.ports,{**p,'execution_mode':'deterministic'},**plant.arguments)
    assert out['recovery_verified'] is False and not plant.commands


def test_proposal_is_validated_before_same_executor_and_task_authors_explanation(task,monkeypatch):
    plant,p,card,selected=fixture(task)
    instances=[]
    class Task:
        def __init__(self,*a,**kw):self.closed=False;instances.append(self)
        def connect(self):pass
        def propose(self,evidence,actions,budget):
            assert 'current' not in actions and 'propose_parameters' in actions
            assert 'No physical actions' in evidence['instructions']
            assert evidence['files']
            return dict(action='propose_parameters',parameters=dict(parameters={'stable_s':1.},reason='Fresh stable record supports this duration'))
        def explain(self,result):
            assert result['artifact']['verified'] is False
            return dict(text='Unverified proposal only; no perturbation was requested.',author='task model')
        def close(self):self.closed=True
    monkeypatch.setattr('ldcon.agents.task_decisions.TaskDecisions',Task)
    monkeypatch.setattr('ldcon.agents.agent_task._parent_result',lambda *a,**kw:None)
    result=decisions.run_card(task.directory,dict(id='laser'),card,plant.ports,p,resident_selection=selected,**plant.arguments)
    assert result['artifact']['parameters']['stable_s']==1.
    assert result['explanation']['author']=='task model'
    assert not result['recovery_verified'] and not plant.commands and instances[0].closed
    saved=read_json(task.directory/'recovery-parameter-proposal.json')
    assert not saved['applied'] and not saved['verified']
    assert read_json(task.directory/'study-decision.json')['proposal']['action']=='propose_parameters'


@pytest.mark.parametrize('change',[{'fallback':[{'offset':-.2,'dwell_s':0.}]},{'operation_epoch':'different'}])
def test_model_cannot_expand_corridor_or_rewrite_epoch(task,change):
    plant,p,card,selected=fixture(task)
    class Provider:
        def propose(self,*args):return dict(action='propose_parameters',parameters=dict(parameters=change,reason='Try'))
    with pytest.raises(Exception):
        decisions.run_card(task.directory,dict(id='laser'),card,plant.ports,p,resident_selection=selected,
                           decision_provider=Provider(),**plant.arguments)
    assert not plant.commands


def test_pinned_task_cannot_silently_change_execution_settings(task):
    plant,p,card,selected=fixture(task)
    with pytest.raises(ValueError,match='frozen'):
        decisions.run_card(task.directory,dict(id='laser'),card,plant.ports,{**p,'stable_s':10.},
                           resident_selection=selected,**plant.arguments)
    assert not plant.commands


def test_preparation_normalization_does_not_allow_live_recovery_agent_mutation():
    from ldcon.jobs.recovery_procedure import parameters as validate
    with pytest.raises(ValueError,match='Only recovery preparation'):
        validate({'parameters':config()},{'execution_mode':'agent'})
