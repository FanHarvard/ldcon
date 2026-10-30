"""Workspace navigation uses registry identities, never control service calls."""
import json
from fastapi.testclient import TestClient
from ldcon.web.web_fleet import create_fleet_app


def test_workspace_inventory_is_dynamic_and_does_not_contact_services(tmp_path):
    registry=tmp_path/'services.json'
    def row(ident):
        return dict(id=ident,label=ident,origin='http://127.0.0.1:12345',
            state_file=str(tmp_path/(ident+'.missing.json')),lasers=[dict(id=ident,name=ident.title())])
    registry.write_text(json.dumps(dict(services=[row('sr422'),row('sr1092')])))
    def forbidden(*args,**kwargs):raise AssertionError('Navigation cannot contact a laser process')
    app=create_fleet_app(registry,token='x'*32,transport=forbidden)
    client=TestClient(app);route=app.state.prefix+'/api/workspaces'
    response=client.get(route)
    assert response.status_code==200
    assert {v['set_id'] for v in response.json()['workspaces']}=={'sr422','sr1092'}
    assert not any(v in response.text for v in ('origin','state_file','127.0.0.1','missing.json'))
    registry.write_text(json.dumps(dict(services=[row('sr422'),row('sr1092'),row('new-laser')])))
    assert len(client.get(route).json()['workspaces'])==3
    assert client.post(route,headers={'X-LDCon-Client':'web'}).status_code==405
    assert client.get('/s/'+'y'*32+'/api/workspaces').status_code==404
    registry.write_text('invalid')
    assert client.get(route).status_code==503
