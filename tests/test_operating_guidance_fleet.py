"""Synthetic fleet routing; no upstream connections or control operations."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from ldcon.web.web_fleet import create_fleet_app, forward, Service


@pytest.fixture
def fleet(tmp_path):
    state=tmp_path/'web-server.json';state.write_text(json.dumps(dict(path='/s/'+'u'*32+'/')))
    registry=tmp_path/'services.json';registry.write_text(json.dumps(dict(services=[dict(id='sr422',label='Sr422',origin='http://127.0.0.1:12345',state_file=str(state))])))
    calls=[]
    def transport(service,method,route,query='',body=None,**extra):
        calls.append(dict(service=service.id,method=method,route=route,extra=extra))
        return 200,'application/json',b'{"ok":true}'
    return TestClient(create_fleet_app(registry,token='f'*32,transport=transport)),calls


def test_only_explicit_routes_and_normal_dashboard_gate(fleet):
    client,calls=fleet;root='/s/'+'f'*32+'/services/sr422/api/'
    for tail in ('operating-mode','adjustment-guidance'):
        assert client.get(root+'lasers/sr422/'+tail).status_code==200
        response=client.put(root+'lasers/sr422/'+tail,json={},headers={'X-LDCon-Client':'web','X-LDCon-Maintainer':'test-operator'})
        assert response.status_code==200 and 'test-operator' not in response.text
    assert calls[1]['extra']=={}
    assert calls[3]['extra']=={}
    assert client.post(root+'lasers/sr422/operating-mode',json={},headers={'X-LDCon-Client':'web'}).status_code==404
    assert client.put(root+'lasers/sr422/operating-mode',json={}).status_code==403
    assert client.put(root+'lasers/sr422/operating-mode',json={},headers={'X-LDCon-Client':'web','Origin':'http://untrusted.invalid'}).status_code==403
    assert len(calls)==4


def test_question_ids_are_forwarded_after_url_decoding(fleet):
    client,calls=fleet;root='/s/'+'f'*32+'/services/sr422/api/'
    question='question:'+'a'*64
    route='failures/case-1/questions/'+question.replace(':','%3A')+'/answer'
    response=client.post(root+route,json={'answer':'Reply'},headers={'X-LDCon-Client':'web'})
    assert response.status_code==200
    assert calls[0]['route'].endswith(question+'/answer')
    assert client.post(root+'failures/case-1/questions/question%3Ainvalid/answer',json={},headers={'X-LDCon-Client':'web'}).status_code==404
