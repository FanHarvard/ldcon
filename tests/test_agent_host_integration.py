"""Real admission and frontend routes against temporary files; no devices/models."""
import copy
import threading
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest
import yaml
from ldcon.agents import agent_profiles
from ldcon.web import agent_task_api
from ldcon.agents import notification_policy
from ldcon.core.records import read_json,atomic_json
from ldcon.web.web_runtime import WebRuntime
from ldcon.web.web_fleet import create_fleet_app


def test_fleet_and_all_deployments_share_profiles_and_notification_defaults(tmp_path,monkeypatch):
    monkeypatch.delenv('LDCON_AGENT_PROFILE_REGISTRY',raising=False)
    monkeypatch.delenv('LDCON_NOTIFICATION_POLICY',raising=False)
    monkeypatch.setattr('ldcon.agents.agent_providers.catalog',lambda:[])
    dashboard=tmp_path/'dashboard';dashboard.mkdir()
    registry=dashboard/'services.json'
    atomic_json(registry,dict(services=[dict(id='laser',label='Laser',origin='http://127.0.0.1:1',
        state_file=str(tmp_path/'deployments/laser/web-server.json'))]))
    def never_forward(*a,**kw):raise AssertionError('No control backend call expected')
    app=create_fleet_app(registry,token='a'*32,transport=never_forward)
    client=TestClient(app);prefix=app.state.prefix
    for path in ('/agent-settings','/agent-settings-assets/agent-settings.js',
                 '/services/laser/agent-settings','/services/laser/agent-settings-assets/agent-settings.css'):
        assert client.get(prefix+path).status_code==200
    assert client.get(prefix+'/services/laser/agent-settings-assets/config.json').status_code==404
    policy=notification_policy.defaults();policy['global_policy']['preset']='quiet'
    headers={'X-LDCon-Client':'web'}
    saved=client.put(prefix+'/api/notification-policy',json=dict(document=policy,expected_revision=0),headers=headers)
    assert saved.status_code==200
    for laser in ('laser','future-laser'):
        assert notification_policy.resolve_policy(tmp_path/'deployments'/laser,laser)['effective']['preset']=='quiet'
    profile=copy.deepcopy(agent_profiles.EMPTY)
    documented=agent_profiles.model_options()['providers']['codex'][0]   # validate_selection requires a catalog model
    profile['profiles']['economy']=dict(provider='codex',model=documented['model'],effort=documented['efforts'][0])
    profile['resident_profile']='economy'
    assert client.put(prefix+'/api/agent-profiles',json=dict(document=profile,expected_revision=None),headers=headers).status_code==200
    assert agent_profiles.resolve(tmp_path/'deployments/laser',laser_id='laser',resident=True)['id']=='economy'
    assert client.put(prefix+'/api/notification-policy',json=dict(document=policy,expected_revision=0),headers=headers).status_code==409


def test_generic_card_start_pins_profile_before_publishing_launch(tmp_path,monkeypatch):
    monkeypatch.setenv('LDCON_JOB_LAUNCHER','service')
    monkeypatch.delenv('LDCON_AGENT_PROFILE_REGISTRY',raising=False)
    laser=dict(schema_version=1,id='laser',name='Fixture',allow_output=False,devices={},interfaces={})
    task=dict(id='review',operation='agent.task',uses=[],parameters=dict(prompt='Analyze recorded data'),
              agent_execution=dict(mode='required',task_profile='economy'))
    deck=dict(schema_version=1,id='deck',name='Fixture',interfaces={},cards=[task])
    assignment=dict(schema_version=1,id='laser',laser_id='laser',decks=[dict(deck_id='deck',parameters={})])
    for folder,name,value in (('lasers','laser',laser),('interface-decks','deck',deck),('assignments','laser',assignment)):
        (tmp_path/folder).mkdir();(tmp_path/folder/(name+'.yaml')).write_text(yaml.safe_dump(value))
    profile=copy.deepcopy(agent_profiles.EMPTY)
    profile['profiles']['economy']=dict(provider='codex',model='before-edit',effort='low')
    original=agent_profiles.save(tmp_path,profile,None)
    runtime=WebRuntime(tmp_path)
    try:
        job=runtime.start_interface_card(runtime.get_set('laser'),'deck','review',{},job_id='a'*32)
        path=runtime.directory(job['id'])
        assert (path/'launch.request').exists()
        admission=read_json(agent_task_api.admission_path(tmp_path,job['id']))
        assert admission['profile']['model']=='before-edit'
        profile['profiles']['economy']['model']='after-edit'
        agent_profiles.save(tmp_path,profile,original['revision'])
        assert read_json(agent_task_api.admission_path(tmp_path,job['id']))['profile']['model']=='before-edit'
        assert job['read_only'] is True
    finally:runtime.close()


def test_task_quality_save_rechecks_phase_and_rejects_non_quality_on_host(tmp_path,monkeypatch):
    before=dict(id='laser',devices=dict(scope=dict(control_settings=dict(optical=dict(
        single_mode_max=dict(side_peak_height_max=.2))))))
    runtime=SimpleNamespace(mutex=threading.RLock(),get_job=lambda _:dict(id='task',laser=dict(id='laser')),
        interface_repository=SimpleNamespace(read=lambda *a:dict(document=before)))
    allowed=[True];calls=[]
    def permission(*args):
        if not allowed[0]:raise ValueError('Task permission phase is inactive')
    monkeypatch.setattr(agent_task_api,'task_permission',permission)
    monkeypatch.setattr('ldcon.core.configuration.save',lambda *a,**k:calls.append((a,k)) or dict(revision='new'))
    after=copy.deepcopy(before);after['devices']['scope']['control_settings']['optical']['single_mode_max']['side_peak_height_max']=.3
    value=dict(yaml=yaml.safe_dump(after),expected_revision='old')
    assert agent_task_api.save_quality(runtime,'task',value)['revision']=='new'
    allowed[0]=False
    with pytest.raises(ValueError,match='inactive'):agent_task_api.save_quality(runtime,'task',value)
    allowed[0]=True;after['allow_output']=True
    with pytest.raises(ValueError,match='quality thresholds'):agent_task_api.save_quality(runtime,'task',dict(value,yaml=yaml.safe_dump(after)))
    assert len(calls)==1


@pytest.mark.parametrize('family',['current','pi'])
def test_web_manual_adjustment_freezes_assigned_plan_before_launch(tmp_path,monkeypatch,family):
    """The actual HTTP Start route; launch files stay in this isolated fixture."""
    from ldcon.web.web_server import create_app
    monkeypatch.setenv('LDCON_JOB_LAUNCHER','service')
    laser=dict(schema_version=1,id='laser',name='Fixture',allow_output=False,devices={},interfaces={})
    task=dict(id='agent-adjust',operation='agent.task',uses=[],agent_execution=dict(mode='resident'),
        parameters=dict(agent_card_id='resident',instruction_profile='adjustment',prompt='Improve recorded quality'))
    bounds=dict(enabled=True,families=[family],timeout_s=180.,handoff_timeout_s=20.)
    if family=='current':bounds.update(half_span_v=.02,max_points=10)
    else:bounds['pid_parameters']=dict(controller_card_id='native-current-servo',strategy='validated_candidates',save_settings=False)
    monitor=dict(id='agent-monitor',operation='agent.monitor',uses=[],parameters=dict(enabled=False,
        agent_card_id='resident',task_card_id='agent-adjust',background=bounds))
    resident=dict(id='resident',operation='agent.resident',uses=[],parameters={})
    deck=dict(schema_version=1,id='deck',name='Fixture',interfaces={},cards=[resident,task,monitor])
    assigned=copy.deepcopy(bounds);assigned['timeout_s']=120.
    assignment=dict(schema_version=1,id='laser',laser_id='laser',decks=[dict(deck_id='deck',parameters={
        'agent-monitor':dict(background=assigned)})])
    for folder,name,value in (('lasers','laser',laser),('interface-decks','deck',deck),('assignments','laser',assignment)):
        (tmp_path/folder).mkdir();(tmp_path/folder/(name+'.yaml')).write_text(yaml.safe_dump(value))
    app=create_app(token='test');runtime=WebRuntime(tmp_path);app.state.runtime=runtime
    import asyncio,httpx
    async def start(payload):
        transport=httpx.ASGITransport(app=app,raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport,base_url='http://test') as client:
            return await client.post('/s/test/api/sets/laser/decks/deck/start',
                headers={'X-LDCon-Client':'web'},json=payload)
    try:
        value=runtime.get_set('laser')
        response=asyncio.run(start(dict(
            expected_revision=value['revision'],card_id='agent-adjust',parameters=dict(adjustment_family=family,
                adjustment_objective='Improve quality within approved bounds'))))
        assert response.status_code==202,response.text
        job=response.json();directory=runtime.directory(job['id'])
        invocation=read_json(directory/'invocation.json')
        assert invocation['adjustment_plan']['background']['timeout_s']==120.
        assert invocation['adjustment_plan']['enabled'] is False
        assert invocation['parameters']['adjustment_family']==family
        assert read_json(agent_task_api.admission_path(tmp_path,job['id']))['profile'] is None
        assert job['status']=='queued' and (directory/'launch.request').is_file()
        assert len(list(runtime.jobs.glob('*/*/launch.request')))==1
    finally:runtime.close()
