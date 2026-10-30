"""Filesystem and fake HTTP only: no provider, notification or hardware calls."""
import copy
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from ldcon.agents import agent_workspace, agent_profiles
from ldcon.web import web_server
from ldcon.core.records import atomic_json


def test_history_includes_active_gateway_even_without_database(tmp_path):
    jobs=[dict(id='current',set_id='laser',deck_id='deck',status='running',
        cards=[dict(id='diagnostic-agent',operation='agent.resident',params={'provider':'opencode','role':'gateway'})],
        agent={'session':{'provider':'opencode','session':'session-current','status':'connected'}})]
    rows=agent_workspace.sessions(tmp_path,'laser',jobs)
    assert len(rows)==1 and rows[0]['active'] and rows[0]['display_name']=='Gateway agent'
    assert rows[0]['session']=='session-current'
    assert not (tmp_path/'agents').exists()
    assert agent_workspace.sessions(tmp_path,'other',jobs)==[]


def test_history_old_ids_are_retained_but_labeled_and_current_session_is_updated(tmp_path):
    (tmp_path/'agents').mkdir()
    with sqlite3.connect(tmp_path/'agents/agents.sqlite3') as db:
        db.execute('create table sessions (key,provider,session,status,job,updated)')
        db.execute('insert into sessions values (?,?,?,?,?,?)',
            (json.dumps(['laser','deck','diagnostic-agent']),'opencode','old','disconnected','job',1))
    jobs=[dict(id='job',set_id='laser',cards=[dict(id='diagnostic-agent',operation='agent.resident',params={})],
        agent={'session':{'status':'connected','provider':'opencode','session':'old'}})]
    rows=agent_workspace.sessions(tmp_path,'laser',jobs)
    assert len(rows)==1 and rows[0]['active'] and rows[0]['status']=='connected'
    assert rows[0]['card_id']=='diagnostic-agent' and rows[0]['display_name']=='Gateway agent'


def test_model_catalog_blocks_new_unknown_selections_preserves_existing(tmp_path,monkeypatch):
    monkeypatch.delenv('LDCON_AGENT_PROFILE_REGISTRY',raising=False)
    document=copy.deepcopy(agent_profiles.EMPTY)
    document['profiles']['test']={'provider':'codex','model':'gpt-6-luna','effort':'ultra'}
    with pytest.raises(ValueError,match='catalog'):
        agent_profiles.save(tmp_path,document,None,catalog_required=True)
    document['profiles']['test']['effort']='high'
    before=agent_profiles.save(tmp_path,document,None,catalog_required=True)
    document=before['document'];document['profiles']['test']['model']='future-local-model'
    # Maintenance API can register a newly validated model; the UI must not delete it.
    before=agent_profiles.save(tmp_path,document,before['revision'])
    document=before['document'];document['profiles']['test']['max_turns']=10
    assert agent_profiles.save(tmp_path,document,before['revision'],catalog_required=True)['document']['profiles']['test']['model']=='future-local-model'


def test_removed_reservations_no_extra_mode_credential_and_normal_web_gate(tmp_path,monkeypatch):
    from ldcon.web import operating_mode_api
    app=web_server.create_app(tmp_path,token='x'*32)
    app.state.runtime=object()
    calls=[]
    monkeypatch.delenv('LDCON_MAINTAINER_TOKEN',raising=False)
    monkeypatch.setattr(operating_mode_api,'change',lambda runtime,laser,data:calls.append((laser,data)) or {'ok':True})
    http=TestClient(app);prefix='/s/'+'x'*32
    mode=prefix+'/api/lasers/laser/operating-mode'
    assert http.put(mode,json={},headers={'X-LDCon-Client':'web'}).status_code==200
    assert len(calls)==1
    assert http.put(mode,json={}).status_code==403
    assert http.put(mode,json={},headers={'X-LDCon-Client':'web','Origin':'http://other.invalid'}).status_code==403
    assert len(calls)==1
    for path in ('/reservations','/api/reservations','/notifications'):
        assert http.get(prefix+path).status_code==410
    assert http.post(prefix+'/api/reservations',json={},headers={'X-LDCon-Client':'web'}).status_code==410
    assert http.post(prefix+'/api/sets/laser/decks/deck/start',json={'reservation':{}},headers={'X-LDCon-Client':'web'}).status_code==410
