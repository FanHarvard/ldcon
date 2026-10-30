import copy
import json
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from experiment_core.agent_contracts import choose_profile, execution_policy
from ldcon.agents import agent_profiles, agent_routing


@pytest.fixture
def configured(tmp_path):
    profiles = {name:dict(provider=provider,model='exact-'+name,effort='low')
                for name,provider in [('alpha','claude'),('beta','claude'),('omega','claude'),('violet','codex'),('blue','codex')]}
    agent_profiles.save(tmp_path,{**copy.deepcopy(agent_profiles.EMPTY), 'profiles':profiles},None)
    document=dict(schema_version=1,default_difficulty='standard',
        provider_profiles={'claude':dict(basic='omega',standard='alpha',deep='beta'),
                           'codex':dict(basic='violet',standard='violet',deep='violet')},
        gateway=dict(provider='claude',difficulty='basic'),lasers={})
    saved=agent_routing.save(tmp_path,document,None)
    return tmp_path,saved


def test_names_have_no_difficulty_semantics(configured):
    root,record=configured
    policy=execution_policy(dict(mode='required',difficulty='deep'))
    assert choose_profile(policy,provider='claude',matrix=record['document']['provider_profiles'])=='beta'
    assert choose_profile(policy,provider='claude',override='violet',matrix=record['document']['provider_profiles'])=='violet'
    with pytest.raises(ValueError):choose_profile(policy,override='unregistered',matrix=record['document']['provider_profiles'])
    with pytest.raises(ValueError):execution_policy(dict(mode='required',difficulty='deep',task_profile='alpha'))
    assert agent_profiles.resolve(root,resident=True,laser_id='one')['id']=='omega'


def test_persistent_laser_defaults_and_card_selection(configured):
    root,record=configured
    document=copy.deepcopy(record['document']);document['lasers']['one']=dict(provider='codex',difficulty='basic')
    saved=agent_routing.save(root,document,record['revision'])
    assert agent_profiles.resolve(root,resident=True,laser_id='one')['id']=='violet'
    assert agent_profiles.resolve(root,resident=True,laser_id='two')['id']=='omega'
    card=dict(agent_execution=dict(mode='required',difficulty='default'))
    deck=dict(cards=[dict(id='gateway',operation='agent.resident',parameters={})])
    p=dict(agent_card_id='gateway',execution_profile='',request_delegation=True)
    profile,source=agent_profiles.select_task(root,card,p,meta=dict(set_id='one',deck_id='deck',laser_id='one'),deck=deck)
    assert profile['id']=='violet' and source['difficulty']=='standard' and source['routing_revision']==saved['revision']
    # A previously returned admission profile remains immutable when routing changes.
    old=copy.deepcopy(profile);document['provider_profiles']['codex']['standard']='blue'
    agent_routing.save(root,document,saved['revision'])
    assert profile==old
    newer,_=agent_profiles.select_task(root,card,p,meta=dict(set_id='one',deck_id='deck',laser_id='one'),deck=deck)
    assert newer['id']=='blue' and profile['id']=='violet'
    with pytest.raises(ValueError):
        invalid=copy.deepcopy(document);invalid['provider_profiles']['claude']['basic']='violet'
        agent_routing.save(root,invalid,agent_routing.read(root)['revision'])
    with pytest.raises(Exception,match='reload'):
        agent_routing.save(root,document,'stale')


def test_referenced_profile_cannot_be_removed(configured):
    root,_=configured
    snapshot=agent_profiles.read(root);del snapshot['document']['profiles']['alpha']
    with pytest.raises(ValueError,match='absent'):
        agent_profiles.save(root,snapshot['document'],snapshot['revision'])


def test_running_gateway_wins_over_next_connection_default(configured):
    import sqlite3
    root,record=configured
    document=copy.deepcopy(record['document']);document['lasers']['one']=dict(provider='codex',difficulty='basic')
    agent_routing.save(root,document,record['revision'])
    folder=root/'agents';folder.mkdir(exist_ok=True)
    with sqlite3.connect(folder/'agents.sqlite3') as db:
        db.execute('CREATE TABLE sessions (key TEXT, provider TEXT, status TEXT)')
        db.execute('INSERT INTO sessions VALUES (?,?,?)',(json.dumps(['one','deck','gateway'],separators=(',',':')),'claude','ready'))
    profile,provenance=agent_profiles.select_task(root,dict(agent_execution=dict(mode='required',difficulty='standard')),
        dict(agent_card_id='gateway',execution_profile=''),meta=dict(set_id='one',deck_id='deck',laser_id='one'))
    assert profile['id']=='alpha' and provenance['origin']=='connected_gateway'


def test_route_save_and_revision_conflict(configured):
    root,_=configured
    app=FastAPI()
    async def body(request):return await request.json()
    agent_routing.install_routes(app,'',body,root=root)
    client=TestClient(app)
    got=client.get('/api/agent-routing').json()
    got['document']['lasers']['laser']=dict(provider='claude',difficulty='deep')
    result=client.put('/api/agent-routing',json=dict(document=got['document'],expected_revision=got['revision']))
    assert result.status_code==200
    assert client.get('/api/agent-routing').json()['document']['lasers']['laser']['difficulty']=='deep'
    with pytest.raises(Exception,match='reload'):
        client.put('/api/agent-routing',json=dict(document=got['document'],expected_revision=got['revision']))
