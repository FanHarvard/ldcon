"""Persistent CLI protocol contracts; no model, credential or device access."""
import json
import sqlite3
import time

import pytest

from ldcon.agents.agent_adapters import SessionAdapter
from ldcon.agents import agent_providers, agent_workspace
from ldcon.agents.resident_agent import AgentStore, parameters


UUID = 'ba88d4be-031b-4f27-9ba3-80312dde68bb'


class Wire(SessionAdapter):
    def __init__(self, provider, replies, cancel=lambda: False):
        super().__init__(dict(provider=provider, workspace='/tmp/laser', instructions='Laser is DFB; evidence required.',
                              tools_context='/tmp/tools.json', request_timeout_s=10), cancel)
        self.replies = replies
        self.sent = []
        self.created = []
        self.session_created = self.created.append

    def preflight(self):
        return {}

    def start(self, argv):
        self.argv = argv

    def write(self, message):
        self.sent.append(message)
        if 'method' not in message or 'id' not in message:
            return
        method, result = self.replies.pop(0)
        assert method == message['method']
        if callable(result):
            result = result(self, message)
        self.events.put(dict(id=message['id'], result=result))


def update(session, text):
    return dict(method='session/update', params=dict(sessionId=session, update=dict(
        sessionUpdate='agent_message_chunk', content=dict(type='text', text=text))))


@pytest.mark.parametrize('provider,session', [('claude', UUID), ('codex', UUID), ('opencode', 'ses_ABC123'), ('deepseek_harness', UUID)])
def test_provider_parameters_and_private_session_ids(provider, session):
    card = dict(operation='agent.resident', parameters={})
    p = parameters(card, dict(provider=provider, session_id=session))
    assert p['provider'] == provider and p['session_id'] == session
    with pytest.raises(ValueError):
        parameters(card, dict(provider=provider, session_id='../other'))
    if provider not in ('claude','codex'):
        with pytest.raises(ValueError, match='Remote Control'):
            parameters(card, dict(provider=provider, remote_control=True))


@pytest.mark.parametrize('resume', [False, True])
def test_opencode_acp_persistent_session_mcp_and_replay_isolation(resume):
    session = 'ses_test123'
    def connected(wire, message):
        wire.events.put(update(session, 'OLD REPLAY'))
        assert message['params']['mcpServers'][0]['name'] == 'ldcon'
        assert message['params']['cwd'] == '/tmp/laser'
        return dict(sessionId=session, configOptions=[dict(id='model', currentValue='provider/model')])
    def answer(wire, message):
        assert 'DFB' in message['params']['prompt'][0]['text']
        wire.events.put(update('ses_other', 'UNRELATED'))
        wire.events.put(update(session, 'First '))
        wire.events.put(dict(jsonrpc='2.0', id=90, method='session/request_permission', params={}))
        wire.events.put(update(session, 'answer'))
        return dict(stopReason='end_turn')
    wire = Wire('opencode', [('initialize', dict(protocolVersion=1, agentCapabilities=dict(loadSession=True))),
        ('session/load' if resume else 'session/new', connected), ('session/prompt', answer),
        ('session/prompt', lambda w, _: w.events.put(update(session, 'Second answer')) or dict(stopReason='end_turn'))])
    assert wire.connect(session if resume else '') == session
    assert wire.created == [session]
    assert wire.request(dict(prompt='Inspect current logs')) == 'First answer'
    assert wire.request(dict(prompt='Continue')) == 'Second answer'
    assert all(message['jsonrpc'] == '2.0' for message in wire.sent)
    assert next(x for x in wire.sent if x.get('id') == 90)['result'] == {'outcome': {'outcome': 'cancelled'}}
    assert wire.model_info['resolved_model'] == 'provider/model'
    assert wire.model_info['response_confirmed'] is True
    assert not wire.replies


def test_opencode_does_not_create_new_conversation_when_resume_is_unsupported():
    wire = Wire('opencode', [('initialize', dict(protocolVersion=1, agentCapabilities={}))])
    with pytest.raises(RuntimeError, match='resuming'):
        wire.connect('ses_existing')
    assert not wire.created and len(wire.sent) == 1


@pytest.mark.parametrize('reason', ['cancelled', 'max_tokens', 'refusal'])
def test_incomplete_opencode_turn_is_not_success_or_replayed(reason):
    wire = Wire('opencode', [('session/prompt', dict(stopReason=reason))])
    wire.session = 'ses_current'
    with pytest.raises(RuntimeError, match='did not complete'):
        wire.request(dict(prompt='single request'))
    assert len(wire.sent) == 1 and wire.model_info['response_confirmed'] is False


@pytest.mark.parametrize('resume', [False, True])
def test_codex_app_server_scoped_context_write_policy_and_turn_identity(resume):
    def turn(wire, message):
        assert message['params']['threadId'] == UUID
        wire.events.put(dict(method='item/agentMessage/delta', params=dict(threadId=UUID, turnId='old', delta='old')))
        wire.events.put(dict(id=80, method='item/commandExecution/requestApproval', params={}))
        wire.events.put(dict(method='item/agentMessage/delta', params=dict(threadId=UUID, turnId='turn', delta='result')))
        wire.events.put(dict(method='turn/completed', params=dict(threadId=UUID, turn=dict(id='turn', status='completed'))))
        return dict(turn=dict(id='turn'))
    wire = Wire('codex', [('initialize', {}), ('account/read', dict(account=None,requiresOpenaiAuth=False)),
        ('model/list',dict(data=[])), ('thread/resume' if resume else 'thread/start', dict(thread=dict(id=UUID), model='configured-model')), ('turn/start', turn)])
    assert wire.connect(UUID if resume else '') == UUID
    args = next(v['params'] for v in wire.sent if v.get('method') in ('thread/start','thread/resume'))
    assert args['sandbox'] == 'workspace-write' and args['approvalPolicy'] == 'on-request'
    assert 'DFB' in args['developerInstructions']
    assert args['config']['mcp_servers']['ldcon']['required'] is True
    assert wire.request({'prompt': 'read evidence'}) == 'result'
    assert next(x for x in wire.sent if x.get('id') == 80)['result']['decision'] == 'decline'


def test_cancellation_and_timeout_never_repeat_requests():
    wire = Wire('opencode', [], cancel=lambda: True)
    with pytest.raises(RuntimeError, match='stopped'):
        wire.receive(time.monotonic() + 1)
    wire.cancel = lambda: False
    with pytest.raises(TimeoutError):
        wire.receive(time.monotonic() - 1)
    assert wire.sent == []


def test_codex_idle_remote_turn_blocks_dispatch_and_updates_connection_status():
    wire=Wire('codex',[]);wire.session=UUID
    wire.events.put(dict(method='remoteControl/status/changed',params=dict(status='connected')))
    wire.events.put(dict(method='turn/started',params=dict(threadId=UUID,turn=dict(id='remote-turn'))))
    assert wire.activity()=='busy'
    assert wire.transport_status()['remote_control']['connectivity_confirmed'] is True
    wire.events.put(dict(method='turn/completed',params=dict(threadId='other',turn=dict(id='remote-turn'))))
    assert wire.activity()=='busy'
    wire.events.put(dict(id=91,method='item/commandExecution/requestApproval',params={}))
    wire.events.put(dict(method='turn/completed',params=dict(threadId=UUID,turn=dict(id='remote-turn'))))
    assert wire.activity()=='ready'
    assert wire.sent==[dict(id=91,result=dict(decision='decline'))]
    assert not wire.replies  # No model turn, replay, or second server was started.


def test_switching_provider_preserves_each_conversation_and_unknown_requests(tmp_path):
    store = AgentStore(tmp_path)
    key = store.key(dict(set_id='laser', deck_id='deck'), 'resident')
    store.save(key, 'claude', UUID, 'disconnected', 'old-job')
    store.enqueue(key, 'request-1', {'prompt': 'only once'})
    assert store.claim(key)['id'] == 'request-1'
    store.recover(key)
    store.save(key, 'opencode', 'ses_test', 'connected', 'new-job')
    reopened = AgentStore(tmp_path)
    assert reopened.session(key, 'claude')['session'] == UUID
    assert reopened.session(key, 'opencode')['session'] == 'ses_test'
    assert reopened.session(key)['provider'] == 'opencode'
    assert reopened.session(key, 'codex') is None
    assert reopened.request(key, 'request-1')['status'] == 'unknown'
    assert reopened.claim(key) is None
    assert len(agent_workspace.sessions(tmp_path, 'laser')) == 2


def test_old_database_migrates_without_replacing_session(tmp_path):
    root = tmp_path/'agents'; root.mkdir()
    key = json.dumps(['laser', 'deck', 'resident'], separators=(',', ':'))
    with sqlite3.connect(root/'agents.sqlite3') as db:
        db.execute('CREATE TABLE sessions(key TEXT PRIMARY KEY,provider TEXT,session TEXT,status TEXT,job TEXT,updated REAL)')
        db.execute('INSERT INTO sessions VALUES(?,?,?,?,?,?)', (key, 'codex', UUID, 'disconnected', 'job', 1.))
    assert AgentStore(tmp_path).session(key, 'codex')['session'] == UUID


def test_provider_discovery_does_not_probe_login_or_execute(monkeypatch):
    monkeypatch.setattr(agent_providers, 'executable', lambda p: '/bin/test' if p == 'codex' else None)
    values = agent_providers.catalog()
    assert [p['id'] for p in values] == ['claude', 'codex', 'opencode', 'deepseek_harness']
    assert [p['available'] for p in values] == [False, True, False, False]
    assert all(p['authentication'] == 'not_checked' for p in values)


@pytest.mark.parametrize('provider', ['claude', 'codex', 'opencode', 'deepseek_harness'])
def test_every_provider_uses_filesystem_boundary_before_start(tmp_path, monkeypatch, provider):
    from ldcon.agents import agent_sandbox, agent_adapters
    calls = []
    adapter = SessionAdapter(dict(provider=provider, workspace=str(tmp_path)), lambda: False)
    adapter._preflight = {'binary': '/fixture/cli'}
    def boundary(argv, params):
        assert params['provider'] == provider
        return ['/usr/bin/bwrap', '--', *argv]
    def popen(argv, **kwargs):
        calls.append(argv)
        raise OSError('Test stops before launching a process')
    monkeypatch.setattr(agent_sandbox, 'resident_command', boundary)
    monkeypatch.setattr(agent_adapters.subprocess, 'Popen', popen)
    with pytest.raises(RuntimeError, match='could not start'):
        adapter.start([provider, 'transport'])
    assert calls == [['/usr/bin/bwrap', '--', '/fixture/cli', 'transport']]


def test_laser_characteristics_are_persistent_context_not_a_numeric_override(tmp_path):
    from ldcon.web.web_runtime import ConflictError
    original = agent_workspace.read(tmp_path, 'laser')
    assert 'ECDL / DBR / DFB' in original['markdown']
    saved = agent_workspace.save(tmp_path, 'laser', '# DFB laser\nMeasured sensitivity: see report.', original['revision'])
    assert agent_workspace.read(tmp_path, 'laser')['markdown'] == saved['markdown']
    with pytest.raises(ConflictError):
        agent_workspace.save(tmp_path, 'laser', '# stale edit', original['revision'])
    assert not (tmp_path/'lasers').exists()


def test_opencode_storage_exception_does_not_reopen_shared_local_code(tmp_path, monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace
    from ldcon.agents import agent_sandbox, agent_tools
    from ldcon.core import site_bindings
    home=tmp_path/'home'; (home/'.local').mkdir(parents=True)
    root=tmp_path/'laser'; root.mkdir()
    public=tmp_path/'public'; public.mkdir()
    shared=tmp_path/'code'; shared.mkdir()
    monkeypatch.setattr(Path,'home',lambda:home)
    monkeypatch.setattr(agent_tools,'Toolbox',lambda _:SimpleNamespace(root=root))
    monkeypatch.setattr(site_bindings,'path',lambda _:public)
    monkeypatch.setattr(site_bindings,'shared_roots',lambda:[shared])
    for key in ('XDG_DATA_HOME','XDG_STATE_HOME'):monkeypatch.delenv(key,raising=False)
    args=agent_sandbox.resident_command(['opencode','acp'],dict(provider='opencode',workspace=str(root),tools_context='unused'))
    readonly=args.index(str(home/'.local'))
    writable=args.index(str(home/'.local/share/opencode'))
    assert args[readonly-1]=='--ro-bind' and args[writable-1]=='--bind' and writable>readonly
    monkeypatch.setenv('XDG_DATA_HOME',str(shared))
    with pytest.raises(ValueError,match='data/state'):
        agent_sandbox.resident_command(['opencode','acp'],dict(provider='opencode',workspace=str(root),tools_context='unused'))


def test_opencode_tasks_register_mcp_in_distinct_locations_without_moving_assets(tmp_path):
    # Model the installed ACP contract: MCP.add is keyed by location + name,
    # and replaces any earlier context under that key, not by session ID.
    registrations={};assets=tmp_path/'laser-assets';assets.mkdir()
    def make(job):
        directory=tmp_path/'jobs'/job;transport=directory/'agent-tool-state'/'opencode-cwd'
        transport.mkdir(parents=True)
        context=directory/'agent-tools-context.json';context.write_text('{}')
        def connected(wire,message):
            p=message['params'];server=p['mcpServers'][0]
            registrations[(p['cwd'],server['name'])]=server['args'][-1]
            return dict(sessionId='ses_'+job,configOptions=[dict(id='model',currentValue='provider/model')])
        adapter=Wire('opencode',[('initialize',dict(protocolVersion=1,agentCapabilities={})),('session/new',connected)])
        adapter.p.update(workspace=str(assets),tools_context=str(context),task_depth=1,transport_workspace=str(transport))
        return adapter,context
    first,a=make('task_a');second,b=make('task_b')
    first.connect('');second.connect('')
    assert first.p['workspace']==second.p['workspace']==str(assets)
    assert len(registrations)==2 and set(registrations.values())=={str(a),str(b)}
    assert all(str(assets)!=location for location,_ in registrations)


def test_process_cwd_uses_isolated_transport_but_sandbox_receives_asset_workspace(tmp_path,monkeypatch):
    from ldcon.agents import agent_adapters, agent_sandbox
    assets=tmp_path/'assets';assets.mkdir()
    state=tmp_path/'task'/'agent-tool-state'/'opencode-cwd';state.mkdir(parents=True)
    p=dict(provider='opencode',workspace=str(assets),tools_context=str(state.parents[1]/'agent-tools-context.json'),
           task_depth=1,transport_workspace=str(state))
    adapter=SessionAdapter(p,lambda:False);adapter._preflight={'binary':'/fixture/cli'}
    def boundary(argv,params):
        assert params['workspace']==str(assets)
        return argv
    def popen(argv,**kwargs):
        assert kwargs['cwd']==state
        raise OSError('Stop before actual launch')
    monkeypatch.setattr(agent_sandbox,'resident_command',boundary)
    monkeypatch.setattr(agent_adapters.subprocess,'Popen',popen)
    with pytest.raises(RuntimeError,match='could not start'):adapter.start(['opencode','acp'])


def test_task_transport_cannot_point_to_another_job_or_symlink(tmp_path):
    state=tmp_path/'task'/'agent-tool-state'/'opencode-cwd';state.mkdir(parents=True)
    p=dict(provider='opencode',workspace=str(tmp_path),tools_context=str(tmp_path/'task'/'agent-tools-context.json'),
           task_depth=1,transport_workspace=str(state))
    adapter=SessionAdapter(p,lambda:False)
    assert adapter.transport_workspace()==str(state)
    p['transport_workspace']=str(tmp_path)
    with pytest.raises(ValueError,match='job-owned'):adapter.transport_workspace()
    p['transport_workspace']=str(state);state.rmdir();state.symlink_to(tmp_path,target_is_directory=True)
    with pytest.raises(ValueError,match='job-owned'):adapter.transport_workspace()


def test_remote_control_defaults_can_follow_selected_provider():
    card=dict(operation='agent.resident',parameters={})
    assert parameters(card,{})['remote_control'] is None
    assert parameters(card,dict(provider='codex',remote_control=True))['remote_control'] is True
    assert parameters(card,dict(provider='claude',remote_control=False))['remote_control'] is False
    with pytest.raises(ValueError):parameters(card,dict(remote_control='true'))


def test_codex_remote_uses_same_server_and_reports_observed_state():
    wire=Wire('codex',[('initialize',{}),('remoteControl/enable',dict(status='connecting',environmentId=None)),
        ('account/read',dict(account=None,requiresOpenaiAuth=False)),('model/list',dict(data=[])),
        ('thread/resume',dict(thread=dict(id=UUID),model='configured-model'))])
    wire.p['remote_control']=True;wire.remote_control['requested']=True
    assert wire.connect(UUID)==UUID
    assert wire.argv==['codex','app-server']  # still goes through the existing sandbox
    assert wire.sent[0]['params']['capabilities']['experimentalApi'] is True
    assert next(x for x in wire.sent if x.get('method')=='remoteControl/enable')['params']=={'ephemeral':True}
    assert wire.transport_status()['remote_control']['status']=='connecting'
    assert not wire.transport_status()['remote_control']['connectivity_confirmed']
    wire.events.put(dict(method='remoteControl/status/changed',params=dict(status='connected',environmentId='env-test',pairingCode='SECRET')))
    wire.receive(time.monotonic()+1)
    status=wire.transport_status()['remote_control'];assert status['connectivity_confirmed']
    assert 'SECRET' not in json.dumps(status)
    assert not any(x.get('method') in ('turn/start','remoteControl/pairing/start') for x in wire.sent)


def test_codex_remote_rejection_is_visible_without_false_connected_claim():
    wire=Wire('codex',[]);wire.p['remote_control']=True
    def rpc(method,params):
        if method=='remoteControl/enable':raise RuntimeError('Rejected')
        return {'initialize':{},'account/read':dict(account=None,requiresOpenaiAuth=False),
                'model/list':dict(data=[]),'thread/start':dict(thread=dict(id=UUID),model='configured-model')}[method]
    wire.rpc=rpc;wire.connect('')
    assert wire.transport_status()['remote_control']['status']=='unavailable'
    assert not wire.transport_status()['remote_control']['connectivity_confirmed']
