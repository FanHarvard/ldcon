"""Claude 2.1.309 control schemas; no real inference, credentials or devices."""
import json
from types import SimpleNamespace

import pytest

from ldcon.agents.agent_adapters import SessionAdapter
from ldcon.agents.agent_interactive import InteractiveSessionAdapter


MODEL = 'claude-sonnet-5-5'
OTHER = 'claude-opus-5-5'
SESSION = 'ba88d4be-031b-4f27-9ba3-80312dde68bb'


class ClaudeWire(SessionAdapter):
    def __init__(self, **parameters):
        super().__init__(dict(provider='claude', workspace='/tmp', instructions='',
                              model=MODEL, effort='high', request_timeout_s=1,
                              **parameters), lambda: False)
        self.sent = []
        self.applied = dict(model=MODEL, effort='high')
        self.catalog = [dict(value='sonnet', resolvedModel=MODEL,
                             supportedEffortLevels=['low', 'medium', 'high', 'xhigh', 'max'])]
        self.response_model = MODEL
        self.after_turn = None
        self.reject = None
        self.approval = False
        self.user_turns = 0

    def preflight(self):
        return dict(version='2.1.309 (Claude Code)')

    def start(self, argv):
        self.argv = argv

    def write(self, message):
        self.sent.append(message)
        if message['type'] == 'control_request':
            subtype = message['request']['subtype']
            response = dict(subtype='success', request_id=message['request_id'])
            if subtype == self.reject:
                response.update(subtype='error', error='TOKEN_PRIVATE must never be copied')
            elif subtype == 'initialize':
                response['response'] = dict(models=self.catalog, account=dict(email='PRIVATE_EMAIL'))
            elif subtype == 'get_settings':
                response['response'] = dict(applied=dict(self.applied),
                                           effective=dict(secret='TOKEN_PRIVATE'), sources=[])
            else:
                response['response'] = {}
            self.events.put(dict(type='control_response', response=response))
        elif message['type'] == 'user':
            self.user_turns += 1
            self.events.put(dict(type='system', subtype='init', session_id=self.session, model=MODEL))
            if self.approval:
                self.events.put(dict(type='control_request', request_id='permission',
                                     request=dict(subtype='can_use_tool', tool_name='Bash')))
            self.events.put(dict(type='assistant', session_id=self.session,
                                 message=dict(model=self.response_model, role='assistant', content=[])))
            self.events.put(dict(type='result', subtype='success', is_error=False,
                                 session_id=self.session, result='ready',
                                 modelUsage={self.response_model: {}}))
            if self.after_turn:
                self.applied.update(self.after_turn)


def test_claude_confirms_applied_profile_before_first_prompt_and_each_turn():
    wire = ClaudeWire()
    wire.connect('')
    assert wire.argv[wire.argv.index('--model') + 1] == MODEL
    assert wire.argv[wire.argv.index('--effort') + 1] == 'high'
    assert '--fallback-model' not in wire.argv
    assert wire.argv[wire.argv.index('--permission-mode') + 1] == 'auto'
    assert [m.get('request', {}).get('subtype', m['type']) for m in wire.sent] == [
        'initialize', 'get_settings', 'get_settings', 'user', 'get_settings']
    assert wire.model_info['resolved_model'] == MODEL
    assert wire.model_info['resolved_effort'] == 'high'
    assert wire.model_info['confirmation'] == 'server_reported'
    assert wire.model_info['effort_confirmation_source'] == 'claude_control_get_settings_applied'
    assert wire.model_info['configured_effort'] == 'high'
    assert wire.model_info['inference_access'] == 'response_confirmed'
    assert 'TOKEN_PRIVATE' not in json.dumps(wire.transport_status())
    assert 'PRIVATE_EMAIL' not in json.dumps(wire.transport_status())
    assert wire.request(dict(prompt='bounded task')) == 'ready'
    assert wire.user_turns == 2


@pytest.mark.parametrize('field,value', [('model', OTHER), ('effort', 'medium'), ('effort', None)])
def test_claude_rejects_override_or_cap_before_any_prompt(field, value):
    wire = ClaudeWire()
    wire.applied[field] = value
    with pytest.raises(RuntimeError, match='differs|did not confirm'):
        wire.connect('')
    assert not wire.user_turns
    with pytest.raises(RuntimeError, match='not confirmed'):
        wire.request(dict(prompt='never'))


@pytest.mark.parametrize('which', ['unknown', 'disabled', 'unsupported_effort', 'missing_model', 'missing_applied'])
def test_claude_catalog_and_settings_cannot_be_replaced_by_requested_values(which):
    wire = ClaudeWire()
    if which == 'unknown':wire.catalog[0]['resolvedModel'] = OTHER
    if which == 'disabled':wire.catalog[0]['disabled'] = True
    if which == 'unsupported_effort':wire.catalog[0]['supportedEffortLevels'] = ['low']
    if which == 'missing_model':wire.applied.pop('model')
    if which == 'missing_applied':wire.applied = {}
    with pytest.raises(RuntimeError, match='not advertised|did not confirm'):
        wire.connect('')
    assert not wire.user_turns
    assert not wire._profile_confirmed


@pytest.mark.parametrize('subtype', ['initialize', 'get_settings'])
def test_unsupported_control_protocol_fails_closed_without_error_payload(subtype):
    wire = ClaudeWire()
    wire.reject = subtype
    with pytest.raises(RuntimeError) as error:
        wire.connect('')
    assert 'TOKEN_PRIVATE' not in str(error.value)
    assert not wire.user_turns


def test_claude_profile_change_between_turns_prevents_task_delivery():
    wire = ClaudeWire()
    wire.connect('')
    wire.applied['effort'] = 'low'
    with pytest.raises(RuntimeError, match='requested effort'):
        wire.request(dict(prompt='never'))
    assert wire.user_turns == 1
    assert wire.model_info['confirmation'] == 'changed'
    assert wire.model_info['response_confirmed'] is False
    with pytest.raises(RuntimeError, match='not confirmed'):
        wire.request(dict(prompt='never retry'))


def test_claude_profile_change_during_turn_invalidates_result_without_replay():
    wire = ClaudeWire()
    wire.connect('')
    wire.after_turn = dict(effort='low')
    with pytest.raises(RuntimeError, match='requested effort'):
        wire.request(dict(prompt='once'))
    assert wire.user_turns == 2
    assert wire.model_info['response_confirmed'] is False


def test_claude_settings_read_failure_invalidates_old_confirmation():
    wire = ClaudeWire()
    wire.connect('')
    wire.reject = 'get_settings'
    with pytest.raises(RuntimeError, match='did not confirm get_settings'):
        wire.request(dict(prompt='never'))
    assert wire.user_turns == 1
    assert wire.model_info['confirmation'] == 'unconfirmed'
    assert not wire.model_info['response_confirmed']
    assert not wire._profile_confirmed


def test_claude_model_fallback_interrupts_and_does_not_confirm_response():
    wire = ClaudeWire()
    wire.connect('')
    wire.response_model = OTHER
    with pytest.raises(RuntimeError, match='model changed'):
        wire.request(dict(prompt='once'))
    assert wire.user_turns == 2
    assert wire.sent[-1]['request']['subtype'] == 'interrupt'
    assert wire.model_info['confirmation'] == 'changed'
    assert not wire.model_info['response_confirmed']


def test_claude_permission_requests_are_denied():
    wire = ClaudeWire()
    wire.approval = True
    wire.connect('')
    replies = [m['response'] for m in wire.sent if m['type'] == 'control_response']
    assert replies == [dict(subtype='success', request_id='permission',
                            response=dict(behavior='deny', message='Interactive permission requires the operator'))]


def test_claude_resume_remains_pinned_and_requires_new_applied_confirmation():
    wire = ClaudeWire()
    assert wire.connect(SESSION) == SESSION
    assert wire.argv[wire.argv.index('--resume') + 1] == SESSION
    assert '--session-id' not in wire.argv
    assert wire.model_info['resolved_effort'] == 'high'


def test_claude_preflight_only_checks_supported_flags_and_version(tmp_path, monkeypatch):
    from ldcon.agents import agent_adapters, agent_providers
    binary = tmp_path / 'claude'
    binary.write_text('fixture')
    binary.chmod(0o755)
    monkeypatch.setattr(agent_providers, 'executable', lambda _: str(binary))
    flags = '--input-format --output-format --permission-mode --tools --allowedTools --append-system-prompt --system-prompt-snapshot --resume --session-id --strict-mcp-config --mcp-config --model --effort'
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout=flags if argv[-1] == '--help' else '2.1.309 (Claude Code)')
    monkeypatch.setattr(agent_adapters.subprocess, 'run', run)
    wire = SessionAdapter(dict(provider='claude', model=MODEL, effort='xhigh'), lambda: False)
    wire.preflight()
    assert calls == [[str(binary), '--help'], [str(binary), '--version']]
    assert wire.model_info['resolved_effort'] is None
    assert wire.model_info['confirmation'] == 'unconfirmed'


def interactive_wire(monkeypatch, events, **parameters):
    from ldcon.agents import agent_interactive_hooks
    wire = InteractiveSessionAdapter(dict(provider='claude', model=MODEL, effort='high',
                                           request_timeout_s=1, **parameters), lambda: False)
    wire.session = SESSION
    wire.alive = lambda: True
    monkeypatch.setattr(agent_interactive_hooks, 'read_events', lambda _: events)
    return wire


def test_interactive_launches_selected_flags_but_does_not_invent_effort_evidence(tmp_path, monkeypatch):
    from ldcon.core import site_bindings
    wire = interactive_wire(monkeypatch, [dict(event='SessionStart', session_id=SESSION, model=MODEL)],
                            workspace=str(tmp_path), tools_context=str(tmp_path / 'context.json'),
                            bridge_id='test', instructions='', compatibility_probe=True)
    wire.preflight = lambda: {}
    wire.start = lambda argv: setattr(wire, 'argv', argv)
    wire.wait_result = lambda: 'ready'
    monkeypatch.setattr(site_bindings, 'shared_roots', lambda: [])
    wire.connect(SESSION)
    assert wire.argv[wire.argv.index('--model') + 1] == MODEL
    assert wire.argv[wire.argv.index('--effort') + 1] == 'high'
    assert wire.model_info['configured_effort'] == 'high'
    assert wire.model_info['resolved_effort'] is None
    assert wire.model_info['effort_confirmation'] == 'unconfirmed'
    assert wire.transport_status()['model_info']['resolved_model'] == MODEL


@pytest.mark.parametrize('kind', ['SessionStart', 'PostModelSwitch'])
def test_interactive_mismatched_runtime_model_quarantines_scheduling(monkeypatch, kind):
    event = dict(event=kind, session_id=SESSION, model=OTHER, to_model=OTHER)
    wire = interactive_wire(monkeypatch, [event])
    with pytest.raises(RuntimeError, match='requested profile'):
        wire.poll()
    assert wire.quarantined
    assert wire.model_info['confirmation'] == 'changed'
    assert not wire.model_info['response_confirmed']


def test_interactive_profile_edit_cannot_silently_reuse_resident(monkeypatch):
    wire = interactive_wire(monkeypatch, [])
    wire.p['effort'] = 'low'
    with pytest.raises(RuntimeError, match='execution profile changed'):
        wire.request(dict(prompt='never'))
    assert wire.quarantined


def test_claude_control_process_uses_scoped_tools_and_boundaries(tmp_path, monkeypatch):
    import sys
    from ldcon.agents import agent_sandbox
    monkeypatch.setenv('LDCON_SHARED_ROOTS', json.dumps([str(tmp_path / 'readonly-source')]))
    script = tmp_path / 'fixture_cli.py'
    ledger = tmp_path / 'wire.jsonl'
    script.write_text('''import json, sys
ledger = sys.argv[1]
def send(value):
 print(json.dumps(value), flush=True)
for line in sys.stdin:
 value=json.loads(line)
 with open(ledger, 'a') as f:f.write(json.dumps(value)+'\\n')
 if value['type']=='control_request':
  subtype=value['request']['subtype']
  result=({'models':[{'value':'sonnet','resolvedModel':'claude-sonnet-5-5','supportedEffortLevels':['high']}]} if subtype=='initialize'
          else {'applied':{'model':'claude-sonnet-5-5','effort':'high'}})
  send({'type':'control_response','response':{'subtype':'success','request_id':value['request_id'],'response':result}})
 elif value['type']=='user':
  send({'type':'control_request','request_id':'permission','request':{'subtype':'can_use_tool','tool_name':'Bash'}})
  send({'type':'assistant','session_id':value['session_id'],'message':{'model':'claude-sonnet-5-5'}})
  send({'type':'result','subtype':'success','is_error':False,'session_id':value['session_id'],'result':'fixture response','modelUsage':{'claude-sonnet-5-5':{}}})
''')
    class ProcessWire(SessionAdapter):
        def start(self, argv):
            self.launch_argv = argv
            super().start([sys.executable, '-u', str(script), str(ledger)])
    boundary_calls = []
    def boundary(argv, params):
        boundary_calls.append(params)
        return argv
    monkeypatch.setattr(agent_sandbox, 'resident_command', boundary)
    wire = ProcessWire(dict(provider='claude', workspace=str(tmp_path), instructions='',
                            tools_context=str(tmp_path / 'context.json'), request_timeout_s=3,
                            model=MODEL, effort='high'), lambda: False)
    wire._preflight = dict(binary=sys.executable, version='fixture')
    try:
        wire.connect('')
        assert wire.request(dict(prompt='fixture task')) == 'fixture response'
        assert wire.model_info['resolved_effort'] == 'high'
    finally:
        wire.close()
    assert len(boundary_calls) == 1
    config = json.loads(wire.launch_argv[wire.launch_argv.index('--mcp-config') + 1])
    assert config['mcpServers']['ldcon']['args'][-1] == str(tmp_path / 'context.json')
    assert wire.launch_argv[wire.launch_argv.index('--permission-mode') + 1] == 'auto'
    assert '--dangerously-skip-permissions' not in wire.launch_argv
    assert '--strict-mcp-config' in wire.launch_argv
    assert '--allowedTools' in wire.launch_argv
    messages = [json.loads(line) for line in ledger.read_text().splitlines()]
    permissions = [m['response']['response']['behavior'] for m in messages if m['type'] == 'control_response']
    assert permissions == ['deny', 'deny']
    assert wire.proc.poll() is not None
