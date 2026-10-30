"""No hardware: normal dashboard mode changes retain revision checks and owner authority."""
import json
from threading import RLock
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from ldcon.web import operating_mode_api as api

from ldcon.agents import operating_mode_client as client
from ldcon.agents.agent_tools import clean_environment


@pytest.fixture
def service(tmp_path, monkeypatch):
    root=tmp_path/'deployment';root.mkdir()
    adaptive=tmp_path/'trusted';adaptive.mkdir()
    monkeypatch.setenv('LDCON_ADAPTIVE_ROOT',str(adaptive))
    monkeypatch.setenv('LDCON_MAINTAINER_TOKEN','operator-test-secret')
    laser=dict(id='laser',devices={})
    runtime=SimpleNamespace(root=root,mutex=RLock(),assert_admission_open=lambda:None,
        interface_repository=SimpleNamespace(read=lambda kind,identifier:dict(document=laser)))
    calls=[]
    def owner(laser,action='operating_mode_status',**values):
        calls.append((action,values))
        state=dict(laser_id='laser',mode='measurement',revision=0)
        return state if action=='operating_mode_status' else dict(current_operating_mode=state)
    monkeypatch.setattr(api,'owner_request',owner)
    app=FastAPI();app.state.runtime=runtime
    async def body(request):return await request.json()
    api.install_routes(app,'',body)
    return TestClient(app),runtime,adaptive,calls


def request_body(**values):
    return dict(request_id='operator-request',expected_revision=0,desired_mode='characterization',
                reason='Characterize drift while measurements are idle',**values)


def test_normal_dashboard_mode_change_needs_no_extra_credential(service,monkeypatch):
    http,_,_,calls=service
    monkeypatch.delenv('LDCON_MAINTAINER_TOKEN')
    response=http.put('/api/lasers/laser/operating-mode',json=request_body())
    assert response.status_code==200 and calls[0][0]=='operating_mode_change'


def test_authenticated_request_binds_revision_without_sending_secret_to_owner(service):
    http,_,root,calls=service
    for _ in range(2):
        response=http.put('/api/lasers/laser/operating-mode',json=request_body(),
                          headers={'X-LDCon-Maintainer':'operator-test-secret'})
        assert response.status_code==200
    assert calls[0]==calls[1]
    path=root/'laser/operating-mode/requests/operator-request.json'
    value=json.loads(path.read_text())
    assert value['expected_revision']==0 and value['desired_mode']=='characterization'
    assert 'secret' not in path.read_text() and 'secret' not in json.dumps(calls)
    assert set(calls[0][1])=={'request_id','request_digest'}
    with pytest.raises(ValueError,match='different content'):
        api.change(service[1],'laser',{**request_body(),'desired_mode':'measurement'})


def test_request_cannot_replace_operator_identity_or_follow_symlink(service,tmp_path):
    _,runtime,root,calls=service
    with pytest.raises(ValueError):api.change(runtime,'laser',{**request_body(),'authenticated_operator':'agent'})
    (root/'laser').symlink_to(tmp_path/'deployment',target_is_directory=True)
    with pytest.raises(ValueError,match='symlink'):api.change(runtime,'laser',request_body())
    assert not calls


def test_agent_environment_keeps_provider_key_but_not_owner_or_operator_authority(monkeypatch):
    for key in ('LDCON_MAINTAINER_TOKEN','LDCON_INSTRUMENT_TOKEN','LDCON_TEAMS_WEBHOOK','AWG_BROKER_TOKEN'):
        monkeypatch.setenv(key,'must-not-be-in-agent')
    monkeypatch.setenv('ANTHROPIC_API_KEY','intended-provider-credential')
    result=clean_environment()
    assert all(value!='must-not-be-in-agent' for value in result.values())
    assert result['ANTHROPIC_API_KEY']=='intended-provider-credential'


@pytest.mark.parametrize('kind',['ecdl.rig.owner','esp32servocon.native_current.owner','dlcpro.laser.owner'])
def test_mode_read_is_typed_no_lease_or_hardware_action(monkeypatch,kind):
    laser=dict(id='laser',devices={'owner':dict(type=kind,connection=dict(
        endpoint='http://localhost:12345',instrument='rig',device_id='device',laser_id='laser'))})
    captured=[]
    class Reply:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self,n):return json.dumps(dict(ok=True,result=dict(laser_id='laser',mode='measurement',revision=0))).encode()
    def open_request(request,timeout):
        captured.append(json.loads(request.data));return Reply()
    monkeypatch.setattr(client,'build_opener',lambda *a:SimpleNamespace(open=open_request))
    assert client.request(laser)['revision']==0
    raw=captured[0].get('request',captured[0])
    assert raw['action']=='operating_mode_status' and 'lease' not in raw
    if kind == 'dlcpro.laser.owner':
        assert raw == dict(action='operating_mode_status',laser_id='laser')
        laser['devices']['owner']['connection']['laser_id']='other'
        with pytest.raises(ValueError,match='another laser'):client.request(laser)
        assert len(captured)==1
    with pytest.raises(ValueError):client.request(laser,'claim')


def test_missing_mode_does_not_become_measurement_authority():
    observation=client.observation(dict(id='laser',devices={}))
    assert observation['status']=='unavailable'
    with pytest.raises(ValueError):client.revision(observation)
    assert 'uncertain benefit' in client.instructions(dict(mode='characterization',revision=1))
    assert 'avoid mode hops' in client.instructions(dict(mode='measurement',revision=2))


def test_owner_upgrade_pending_does_not_invent_mode_and_advice_is_independent(service,monkeypatch):
    http,_,_,_=service
    def unavailable(*args,**kwargs):raise ValueError('Unsupported owner action')
    monkeypatch.setattr(api,'owner_request',unavailable)
    r=http.get('/api/lasers/laser/operating-mode')
    assert r.status_code==503 and 'No mode change' in r.json()['detail']
    advice=http.get('/api/lasers/laser/adjustment-guidance')
    assert advice.status_code==200 and advice.json()['advisory_only'] is True


def test_advice_edit_is_cas_metadata_only_without_owner_calls(service):
    http,_,root,calls=service
    route='/api/lasers/laser/adjustment-guidance'
    before=http.get(route).json()
    document=dict(schema_version=1,knobs=dict(current=dict(kind='absolute',step=.0002,unit='V',reason='Local probe')))
    response=http.put(route,json=dict(expected_revision=before['revision'],document=document))
    assert response.status_code==200 and response.json()['document']==document
    assert calls==[] and not list(root.rglob('*.json'))
