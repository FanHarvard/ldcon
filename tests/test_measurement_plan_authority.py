"""Finite measurement authority is explicit laser configuration, independent of monitors."""
import copy
import hashlib
from types import SimpleNamespace

import pytest
import yaml
from experiment_core.records import atomic_json,digest
from ldcon.agents.measurement_grant import (
    MeasurementGrant,freeze_measurement_plan,measurement_plan_source,
    propose_measurement_plan_migration,
)
from ldcon.core.adjustment_plan import approved_adjustment_plan
from ldcon.core.agent_authority import SAFETY,classify,describe_changes,live_class
from ldcon.core.interface_schema import validate_laser
from ldcon.owners.background_grants import read_grant


def plan():
    return dict(enabled=True,families=['pi'],handoff_timeout_s=30.,timeout_s=300.,
                pid_parameters=dict(strategy='validated_candidates',save_settings=False))


def laser():
    return dict(schema_version=1,id='fixture',name='Fixture',allow_output=False,
                devices={},interfaces={},measurement_plan=plan())


def monitor_deck(background=None):
    return dict(cards=[dict(id='watch',operation='agent.monitor',parameters={
        'task_card_id':'adjust','background':background if background is not None else plan()})])


def test_plan_validates_without_monitor_or_task_and_freezes_owned_copy():
    document=laser();original=copy.deepcopy(document)
    assert validate_laser(document)==original
    bounds=freeze_measurement_plan(document)
    assert bounds==approved_adjustment_plan(plan())
    assert measurement_plan_source(document,'config:1')==dict(
        source='laser.measurement_plan',laser_id='fixture',laser_revision='config:1',plan_revision=digest(bounds))
    document['measurement_plan']['timeout_s']=600.
    assert bounds['timeout_s']==300.


def test_absent_disabled_or_monitor_only_plan_gives_no_authority():
    assert freeze_measurement_plan({}) is None
    assert freeze_measurement_plan({'measurement_plan':dict(enabled=False)}) is None
    assert freeze_measurement_plan(monitor_deck()) is None
    assert measurement_plan_source({'id':'fixture'},'config:1') is None


@pytest.mark.parametrize('update',[
    {'pid_parameters':dict(strategy='validated_candidates',save_settings=True)},
    {'pid_parameters':dict(strategy='validated_candidates',save_settings=False,candidate=dict(kp_count_per_nm=1.,ki_count_per_nm_s=1.,integral_limit_count=1.))},
    {'families':['unknown']},{'timeout_s':float('inf')},{'candidate':{'ki_count_per_nm_s':1.}},
    {'pid_parameters':None},{'families':['pi'],'enabled':'yes'},
])
def test_invalid_saved_envelope_rejected_before_grant(update):
    document=laser();document['measurement_plan'].update(update)
    with pytest.raises((ValueError,TypeError)):validate_laser(document)


@pytest.mark.parametrize('path',[
    ('measurement_plan',),('measurement_plan','enabled'),('measurement_plan','families'),
    ('measurement_plan','pid_parameters','gain_factors'),
    ('measurement_plan','adjustment_parameters','slow_loop','bounds','tec_gain','0'),
    ('measurement_plan','adjustment_parameters','feedforward','bounds','current_per_temperature','1'),
    ('measurement_plan','pid_parameters','settle_s'),
])
def test_all_envelope_paths_require_approval_even_operating_names(path):
    assert classify('lasers',path)==SAFETY
    assert live_class('lasers',path)=='snapshot'


def test_agent_cannot_install_or_widen_plan_without_human_approval(monkeypatch):
    from ldcon.agents import agent_configuration
    before=laser();before.pop('measurement_plan')
    calls=[]
    def api(box,route,*args,**kwargs):
        calls.append((route,args));return dict(revision='config:1',document=before)
    monkeypatch.setattr(agent_configuration,'_api',api)
    box=SimpleNamespace(_laser_id='fixture',meta={'set_id':'set'})
    response=agent_configuration.call(box,'laser_configuration_update',dict(
        expected_revision='config:1',yaml=yaml.safe_dump(laser())))
    assert response['status']=='requires_human_approval' and response['applied'] is False
    assert len(calls)==1 and response['free']==[] and response['safety']
    assert all(row['class']==SAFETY for row in describe_changes('lasers',before,laser()))


def test_migration_is_explicit_proposal_and_never_mutates_source():
    document=laser();document.pop('measurement_plan');deck=monitor_deck()
    original=(copy.deepcopy(document),copy.deepcopy(deck))
    proposal=propose_measurement_plan_migration(document,deck,source_revision='assignment:7')
    assert (document,deck)==original
    assert proposal['proposal_only'] and proposal['requires_human_review']
    assert proposal['document']['measurement_plan']==approved_adjustment_plan(plan())
    assert proposal['provenance']['source_revision']=='assignment:7'
    assert proposal['provenance']['plan_revision']==digest(proposal['document']['measurement_plan'])
    with pytest.raises(ValueError,match='already'):propose_measurement_plan_migration(proposal['document'],deck)
    with pytest.raises(ValueError,match='exactly one'):propose_measurement_plan_migration(document,{'cards':[]})
    with pytest.raises(ValueError,match='disabled'):
        propose_measurement_plan_migration(document,monitor_deck(dict(enabled=False)))


def test_host_frozen_plan_issues_scoped_grant_without_monitor(tmp_path,monkeypatch):
    authority=tmp_path/'authority';authority.mkdir()
    monkeypatch.setenv('LDCON_ADAPTIVE_ROOT',str(authority))
    jobs=tmp_path/'deployment'/'jobs';child=jobs/'measurement';controller=jobs/'controller'
    child.mkdir(parents=True);controller.mkdir()
    document=laser();validate_laser(document);bounds=freeze_measurement_plan(document)
    atomic_json(child/'invocation.json',dict(measurement_plan=bounds,measurement_controller_job_id='controller',
        measurement_plan_source=measurement_plan_source(document,'config:1')))
    grant=MeasurementGrant(child,controller,'fixture',bounds,lease_sha256=hashlib.sha256(b'lease').hexdigest())
    assert read_grant('fixture',grant.identity,lease='lease')==bounds
    grant.close()
    with pytest.raises(ValueError,match='revoked'):read_grant('fixture',grant.identity,lease='lease')


def test_catalog_runtime_readiness_requires_explicit_plan_not_monitor(monkeypatch):
    from ldcon.jobs import change_measure_adapter as adapter
    monkeypatch.setattr(adapter,'axis_map',lambda laser:{'axes':{}})
    document=laser();document['allow_output']=True
    document['devices']={'rig':{'type':'ecdl.rig.owner'}}
    document['interfaces']={'lock':{'device':'rig'}}
    card={'operation':'measure.change'}
    request=dict(changes=[dict(parameter='kp_count_per_nm',value=2.)],measurements={},apply_mode='coordinated')
    assert adapter.readiness(document,card,request)==[]
    document.pop('measurement_plan')
    assert any('measurement_plan for pi' in reason for reason in adapter.readiness(document,card,request))
    document['measurement_plan']=dict(plan(),families=['current'],half_span_v=.001,max_points=2)
    assert any('measurement_plan for pi' in reason for reason in adapter.readiness(document,card,request))
    document['measurement_plan']=plan()
    document['measurement_plan']['pid_parameters']['strategy']='factor_reduction'
    assert any('validated_candidates' in reason for reason in adapter.readiness(document,card,request))
    assert adapter.readiness(document,card,dict(request,changes=[]))==[]
