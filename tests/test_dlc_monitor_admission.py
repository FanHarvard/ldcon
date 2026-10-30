"""Offline monitor admission tests: no device, provider, or external network."""
from copy import deepcopy
from types import SimpleNamespace
import json

import pytest
import yaml
from experiment_core.records import atomic_json, digest
from ldcon.agents.agent_monitor import parameters as monitor_parameters, snapshot
from ldcon.agents.agent_task import parameters as task_parameters
from ldcon.agents.dlc_pi_plan import freeze_task
from ldcon.agents.dlc_monitor_grant import bind, check_grant, validate_bounds
from ldcon.agents.monitor_quality_binding import conditions


def bounds(**kw):
    return dict(enabled=True,controller_operation='servo.dlcpro_frequency',families=['pi','current','slow_loop','feedforward'],timeout_s=1800.,**kw)


def plan(quality=False):
    return dict(changes=[],apply_mode='coordinated' if quality else 'atomic',
        measurements=dict(wavelength=True,optical_quality=quality,dither=False),baseline_s=0.,wait_s=2.,
        duration_s=20.,history_wait_s=0.,sample_interval_s=.1,minimum_samples=10,
        measurement_max_age_s=5.,max_sample_gap_s=3.,timeout_s=60.)


def test_same_frozen_plan_for_manual_and_monitor_all_families():
    laser=dict(interfaces={'dlc_lock':{'device':'dlc'}},devices={'dlc':{'type':'dlcpro.laser.owner'}})
    cards=[dict(id=f+'-probe',operation='measure.change',parameters=plan(f!='pi')) for f in bounds()['families']]
    task=dict(id='adjust',operation='agent.task',parameters=dict(instruction_profile='adjustment',
        adjustment_measurement_cards={f:f+'-probe' for f in bounds()['families']}))
    p=task_parameters(task,{})
    manual=freeze_task(laser,{'cards':cards},p,{})
    monitored=freeze_task(laser,{'cards':cards},{**p,'monitor_job_id':'monitor'}, {})
    assert manual==monitored and set(manual['measurements'])==set(bounds()['families'])
    cards[1]['parameters']['changes']=[dict(parameter='current_ma',value=80)]
    with pytest.raises(ValueError,match='without predefined'):freeze_task(laser,{'cards':cards},p,{})


def test_no_monitor_target_program_or_invented_family():
    laser=dict(interfaces={'dlc_lock':{'device':'dlc'}},devices={'dlc':{'type':'dlcpro.laser.owner'}})
    task=task_parameters(dict(operation='agent.task',parameters=dict(adjustment_family='pi',adjustment_plan_card_id='steps')),{'monitor_job_id':'m'})
    with pytest.raises(ValueError,match='cannot move'):
        freeze_task(laser,{'cards':[dict(id='steps',operation='measure.batch')]},task,{})
    for bad in ([{}],['piezo'],['pi','pi'],[]):
        value=bounds();value['families']=bad
        with pytest.raises(ValueError):validate_bounds(value)


@pytest.fixture
def context(tmp_path,monkeypatch):
    root=tmp_path/'laser';task=root/'jobs'/'task';parent=task.parent/'monitor'
    task.mkdir(parents=True);parent.mkdir()
    trusted=tmp_path/'authority';trusted.mkdir();monkeypatch.setenv('LDCON_ADAPTIVE_ROOT',str(trusted))
    card=dict(id='agent-monitor',operation='agent.monitor',parameters=dict(task_card_id='adjust',background=bounds()))
    params=monitor_parameters(card,{})
    atomic_json(parent/'job.json',dict(id='monitor'))
    (parent/'laser.yaml').write_text('id: laser\n')
    (parent/'interface-deck.yaml').write_text(yaml.safe_dump(dict(cards=[card])))
    atomic_json(parent/'invocation.json',dict(card_id=card['id'],parameters=params))
    atomic_json(task/'invocation.json',dict(card_id='adjust'))
    path=trusted/'laser'/'monitors'/'monitor.json';path.parent.mkdir(parents=True)
    import time
    grant=dict(enabled=True,bounds=bounds(),generation='generation',parameters_hash=digest(params),last_seen=time.time(),max_age_s=30.)
    atomic_json(path,grant)
    return task,path,grant


def test_parent_binding_rejects_revocation_generation_scope_and_frozen_changes(context):
    task,path,grant=context
    p=dict(monitor_job_id='monitor',timeout_s=1800.)
    binding=bind(task,p,{'id':'laser'})
    assert check_grant('laser',binding)['bounds']==bounds()
    for changed in (dict(enabled=False),dict(generation='other'),dict(last_seen=1),dict(parameters_hash='wrong')):
        atomic_json(path,{**grant,**changed})
        with pytest.raises(ValueError):check_grant('laser',binding)
    atomic_json(path,grant)
    with pytest.raises(ValueError):bind(task,p,{'id':'other'})
    with pytest.raises(ValueError):bind(task,{**p,'timeout_s':1801},{'id':'laser'})
    atomic_json(task/'invocation.json',dict(card_id='other'))
    with pytest.raises(ValueError):bind(task,p,{'id':'laser'})


def test_quality_uses_optical_clock_not_new_frequency_timestamp(monkeypatch):
    box=SimpleNamespace(meta={'set_id':'set'},_laser_id='laser',root='/unused')
    monkeypatch.setattr('ldcon.agents.agent_cards._api',lambda *a,**k:dict(jobs=[
        dict(id='job',set_id='set',laser={'id':'laser'},cards=[{'operation':'servo.dlcpro_frequency'}])]))
    data=dict(source_acquired_unix_s=1000.,integral_v=-9.8,control=dict(integral_limit_v=10.),
              observation=dict(quality=dict(acquired_unix_s=900.,fresh=False,quality_valid=False,metrics={'side_peak_height_max':.4})))
    monkeypatch.setattr('ldcon.agents.agent_log_read.read',lambda *a,**k:dict(data=data))
    got=snapshot(box)['metrics']
    assert got['integral_utilization']['value']==pytest.approx(.98)
    q=got['observation.quality.metrics.side_peak_height_max']
    assert q['acquired_unix_s']==900 and q['valid'] is False
    rule=dict(id='quality-side',metric='observation.quality.metrics.side_peak_height_max',above=.3,clear_below=.27)
    laser=dict(devices={'dlc':{'control_settings':{'optical':{'single_mode_max':{'side_peak_height_max':.2}}}}})
    new=conditions([rule],laser)[0]
    assert new['above']==.2 and new['clear_below']==pytest.approx(.18)
