"""A location reset must not discard gateway authority or replay requests."""
import json

import pytest

from ldcon.agents import agent_opencode_mcp as mcp
from ldcon.agents.agent_interactive import RequestDeferred
from ldcon.agents.agent_opencode_http import OpenCodeSharedAdapter


def parameters(laser='sr422'):
    return dict(workspace='/lasers/' + laser, tools_context='/lasers/' + laser + '/jobs/test/context.json')


def test_configuration_belongs_to_one_server_not_shared_environment(monkeypatch):
    base = {'OPENCODE_CONFIG_CONTENT': json.dumps(dict(model='existing/model', mcp={'other': {'enabled': False}}))}
    before = dict(base)
    a = json.loads(mcp.environment(parameters(), base)['OPENCODE_CONFIG_CONTENT'])
    b = json.loads(mcp.environment(parameters('sr1092'), base)['OPENCODE_CONFIG_CONTENT'])
    assert base == before
    assert a['model'] == 'existing/model'
    assert a['mcp']['servers']['other'] == {'enabled': False}
    assert a['experimental']['location_idle_timeout'] is False
    assert a['mcp']['servers']['ldcon']['command'][-1] == parameters()['tools_context']
    assert b['mcp']['servers']['ldcon']['command'][-1] == parameters('sr1092')['tools_context']
    assert a['mcp']['servers']['ldcon']['cwd'] == parameters()['workspace']
    assert mcp.environment({}, base) == base  # no gateway authority for no-tools probes


@pytest.mark.parametrize('config', ['[]', '{bad', '{"mcp":false}', '{"mcp":{"servers":[]}}'])
def test_invalid_inline_configuration_does_not_silently_drop_settings(config):
    with pytest.raises(ValueError):mcp.environment(parameters(), {'OPENCODE_CONFIG_CONTENT': config})


def test_existing_v2_options_and_unrelated_servers_survive():
    content = dict(mcp={'timeout':'8 seconds', 'servers':{'other':{'enabled':False}}}, experimental={'todo':False})
    result = json.loads(mcp.environment(parameters(), {'OPENCODE_CONFIG_CONTENT':json.dumps(content)})['OPENCODE_CONFIG_CONTENT'])
    assert result['mcp']['timeout'] == '8 seconds'
    assert result['experimental']['todo'] is False
    assert result['mcp']['servers']['other'] == {'enabled': False}


def test_native_async_location_recreation_waits_without_registering_or_calling_tools(monkeypatch):
    statuses = [[], [{'name':'ldcon','status':{'status':'pending'}}], [{'name':'ldcon','status':{'status':'connected'}}]]
    calls=[]
    def api(method, path):
        calls.append((method,path));return {'data':statuses.pop(0)}
    monkeypatch.setattr(mcp.time,'sleep',lambda _:None)
    mcp.wait_connected(api,parameters(),lambda:False,lambda:True)
    assert not statuses
    assert all(method=='GET' and path.startswith('/api/mcp?') for method,path in calls)


@pytest.mark.parametrize('state',['disabled','failed','needs_auth','needs_client_registration'])
def test_terminal_native_state_is_not_overridden(state):
    calls=[]
    def api(method,path):
        calls.append(method);return {'data':[{'name':'ldcon','status':{'status':state,'error':'SECRET'}}]}
    with pytest.raises(RuntimeError,match='inspect native MCP status') as exc:
        mcp.wait_connected(api,parameters(),lambda:False,lambda:True)
    assert calls == ['GET'] and 'SECRET' not in str(exc.value)


def test_cancel_and_deadline_never_submit_or_repair(monkeypatch):
    def api(*args):raise AssertionError('No request expected')
    with pytest.raises(RuntimeError,match='stopped'):mcp.wait_connected(api,parameters(),lambda:True,lambda:True)
    with pytest.raises(RuntimeError,match='timed out'):mcp.wait_connected(api,parameters(),lambda:False,lambda:True,timeout=0)


def test_unavailable_tools_defer_only_before_prompt(monkeypatch):
    adapter=OpenCodeSharedAdapter(dict(provider='opencode',**parameters()),lambda:False)
    adapter.busy=lambda:False;adapter.alive=lambda:True
    calls=[]
    def api(method,path,body=None):
        calls.append((method,path));return {'data':[{'name':'ldcon','status':{'status':'failed'}}]}
    adapter.api=api
    with pytest.raises(RequestDeferred,match='no new input delivered'):
        adapter.request({'prompt':'read status'})
    assert all(method=='GET' for method,path in calls)
    assert not adapter.quarantined
