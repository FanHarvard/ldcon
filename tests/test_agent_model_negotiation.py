"""Exact profile negotiation against simulated official wire schemas, no models."""
import json
from pathlib import Path
import time

import pytest

from ldcon.agents.agent_adapters import SessionAdapter
from ldcon.agents import agent_providers

SESSION = 'ba88d4be-031b-4f27-9ba3-80312dde68bb'
DSH_MODEL = '["deepseek-official","fixture-model"]'


class Wire(SessionAdapter):
    def __init__(self, provider, replies, **parameters):
        super().__init__(dict(provider=provider, workspace='/tmp/laser', instructions='CANONICAL POLICY',
                              instructions_resolved=True, request_timeout_s=1, **parameters), lambda: False)
        self.replies = list(replies)
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
        if result is not None:
            self.events.put(dict(id=message['id'], result=result))


def catalog(model='exact-model'):
    return dict(data=[dict(id=model, model=model, supportedReasoningEfforts=[dict(reasoningEffort='low'), dict(reasoningEffort='high')],
                          defaultReasoningEffort='low', inputModalities=['text','image'])])


def codex_setup(**effective):
    return [('initialize', {}), ('account/read', dict(account=dict(type='chatgpt',email='PRIVATE_EMAIL'), requiresOpenaiAuth=True)),
            ('model/list', catalog()), ('thread/start', dict(thread=dict(id=SESSION), model='exact-model', reasoningEffort='high', **effective))]


def config(model='provider/model', effort='low', effort_id='effort'):
    return [dict(id='model', category='model', type='select', currentValue=model,
                 options=[dict(group='provider', options=[dict(value='provider/model'), dict(value='provider/other'), dict(value=DSH_MODEL)])]),
            dict(id=effort_id, category='thought_level', type='select', currentValue=effort,
                 options=[dict(value='low'), dict(value='high')])]


def acp_setup(provider, options, resume=False):
    caps = dict(sessionCapabilities=dict(resume={})) if provider=='deepseek_harness' else dict(loadSession=True)
    caps['promptCapabilities'] = dict(image=True)
    method = ('session/resume' if provider=='deepseek_harness' else 'session/load') if resume else 'session/new'
    return [('initialize', dict(protocolVersion=1, agentCapabilities=caps)),
            (method, dict(sessionId=SESSION, configOptions=options))]


def acp_answer(wire, _):
    wire.events.put(dict(jsonrpc='2.0', id=100, method='session/request_permission',params=dict(options=[dict(optionId='allow',kind='allow_always')])))
    wire.events.put(dict(method='session/update',params=dict(sessionId=SESSION,update=dict(sessionUpdate='agent_message_chunk',content=dict(type='text',text='answer')))))
    return dict(stopReason='end_turn')


def test_codex_checks_auth_catalog_and_effective_selection_before_any_turn():
    def turn(wire, message):
        assert message['params']['model']=='exact-model'
        assert message['params']['effort']=='high'
        assert 'CANONICAL POLICY' not in message['params']['input'][0]['text']
        wire.events.put(dict(method='turn/completed',params=dict(threadId=SESSION,turn=dict(id='turn',status='completed'))))
        return dict(turn=dict(id='turn'))
    wire=Wire('codex', codex_setup()+[('turn/start',turn)],model='exact-model',effort='high')
    wire.connect('')
    params=next(m['params'] for m in wire.sent if m.get('method')=='thread/start')
    assert params['developerInstructions']=='CANONICAL POLICY'
    assert params['config']['model_reasoning_effort']=='high'
    assert wire.model_info['confirmation']=='server_reported'
    assert wire.model_info['inference_access']=='not_checked'
    assert wire.model_info['input_modalities']==['text','image']
    assert 'PRIVATE_EMAIL' not in json.dumps(wire.transport_status())
    wire.request({'prompt':'do scoped work'})
    assert wire.model_info['inference_access']=='response_confirmed'


def test_codex_missing_auth_stops_before_catalog_or_thread():
    wire=Wire('codex',[('initialize',{}),('account/read',dict(account=None,requiresOpenaiAuth=True))],model='exact-model')
    with pytest.raises(RuntimeError,match='authentication is unavailable'):wire.connect('')
    assert not wire.created
    assert not any(m.get('method') in ('model/list','thread/start','turn/start') for m in wire.sent)
    assert wire.model_info['authentication']=='unavailable'


@pytest.mark.parametrize('model,effort', [('absent-model','high'), ('exact-model','unsupported')])
def test_codex_unknown_route_has_no_fallback(model,effort):
    wire=Wire('codex',codex_setup()[:3],model=model,effort=effort)
    with pytest.raises(RuntimeError,match='not advertised|not supported'):wire.connect('')
    assert not wire.created
    assert not any(m.get('method')=='thread/start' for m in wire.sent)


@pytest.mark.parametrize('key,value', [('model','wrong-model'),('reasoningEffort','low'),('reasoningEffort',None)])
def test_codex_wrong_effective_profile_rejects_before_first_prompt(key,value):
    replies=codex_setup();replies[-1][1][key]=value
    wire=Wire('codex',replies,model='exact-model',effort='high')
    with pytest.raises(RuntimeError,match='differs|did not confirm'):wire.connect('')
    with pytest.raises(RuntimeError,match='not confirmed'):wire.request({'prompt':'must never start'})
    assert not wire.created and not any(m.get('method')=='turn/start' for m in wire.sent)


@pytest.mark.parametrize('provider', ['opencode','deepseek_harness'])
@pytest.mark.parametrize('resume', [False,True])
def test_acp_negotiates_model_then_new_effort_options_before_prompt(provider,resume):
    model=DSH_MODEL if provider=='deepseek_harness' else 'provider/other'
    effort_id='reasoning_effort' if provider=='deepseek_harness' else 'effort'
    replies=acp_setup(provider,config(effort_id=effort_id),resume)+[
        ('session/set_config_option',dict(configOptions=config(model,effort_id=effort_id))),
        ('session/set_config_option',dict(configOptions=config(model,'high',effort_id))),
        ('session/prompt',acp_answer)]
    wire=Wire(provider,replies,model=model,effort='high')
    wire.connect(SESSION if resume else '')
    assert wire.argv==(['dsh','--profile','acp'] if provider=='deepseek_harness' else ['opencode','acp'])
    choices=[m['params'] for m in wire.sent if m.get('method')=='session/set_config_option']
    assert choices[0]['value']==model and choices[1]['configId']==effort_id
    assert wire.request({'prompt':'inspect'})=='answer'
    assert next(m for m in wire.sent if m.get('id')==100)['result']=={'outcome':{'outcome':'cancelled'}}
    assert wire.model_info['resolved_effort']=='high'
    assert wire.created==[SESSION]
    assert all(m.get('jsonrpc')=='2.0' for m in wire.sent)


@pytest.mark.parametrize('provider', ['opencode','deepseek_harness'])
@pytest.mark.parametrize('which', ['unadvertised','no_config','wrong_result','unsupported_effort'])
def test_acp_refuses_unconfirmed_selection_without_model_turn(provider,which):
    options=config();model='provider/other';effort=''
    if which=='unadvertised':model='not-advertised'
    if which=='no_config':options=[]
    if which=='unsupported_effort':model='provider/model';effort='max'
    replies=acp_setup(provider,options)
    if which=='wrong_result':replies.append(('session/set_config_option',dict(configOptions=config())))
    wire=Wire(provider,replies,model=model,effort=effort)
    with pytest.raises(RuntimeError,match='not advertised|did not confirm'):wire.connect('')
    with pytest.raises(RuntimeError,match='not confirmed'):wire.request({'prompt':'must not run'})
    assert not any(m.get('method')=='session/prompt' for m in wire.sent)
    assert not wire.created


def test_acp_profile_update_fences_future_requests_and_cancels_once():
    def changed(wire,_):
        wire.events.put(dict(method='session/update',params=dict(sessionId=SESSION,update=dict(
            sessionUpdate='config_option_update',configOptions=config('provider/other')))))
        return dict(stopReason='end_turn')
    wire=Wire('opencode',acp_setup('opencode',config())+[('session/prompt',changed)],model='provider/model')
    wire.connect('')
    with pytest.raises(RuntimeError,match='profile changed'):wire.request({'prompt':'once'})
    with pytest.raises(RuntimeError,match='not confirmed'):wire.request({'prompt':'must not replay'})
    assert sum(m.get('method')=='session/prompt' for m in wire.sent)==1
    assert sum(m.get('method')=='session/cancel' for m in wire.sent)==1
    assert wire.model_info['response_confirmed'] is False


@pytest.mark.parametrize('provider', ['opencode','deepseek_harness'])
@pytest.mark.parametrize('failure', ['disconnect','cancel','timeout','auth'])
def test_acp_failure_cancels_without_replay_or_credential_logging(provider,failure):
    def fail(wire,message):
        if failure=='disconnect':
            wire._stderr='authentication Bearer SECRET_VALUE'
            wire.events.put(None)
        elif failure=='cancel':wire.cancel=lambda: True
        elif failure=='timeout':wire.p['request_timeout_s']=-1
        else:wire.events.put(dict(id=message['id'],error=dict(code=-32000,message='API key SECRET_VALUE unavailable')))
    wire=Wire(provider,[('session/prompt',fail)])
    wire.session=SESSION
    with pytest.raises((RuntimeError,TimeoutError)) as error:wire.request({'prompt':'only once'})
    assert 'SECRET_VALUE' not in str(error.value)
    assert sum(m.get('method')=='session/prompt' for m in wire.sent)==1
    assert sum(m.get('method')=='session/cancel' for m in wire.sent)==1
    assert not wire.model_info['response_confirmed']


def test_preexisting_cancel_never_delivers_task():
    wire=Wire('deepseek_harness',[])
    wire.cancel=lambda:True
    with pytest.raises(RuntimeError,match='stopped'):wire.request({'prompt':'never'})
    assert not wire.sent


@pytest.mark.parametrize('provider,name', [('codex','codex'),('opencode','opencode'),('deepseek_harness','dsh')])
def test_managed_cli_prefix_discovery_never_invokes_cli(tmp_path,monkeypatch,provider,name):
    monkeypatch.setattr(Path,'home',lambda:tmp_path)
    monkeypatch.setattr(agent_providers.shutil,'which',lambda _:None)
    monkeypatch.delenv('LDCON_'+provider.upper()+'_BIN',raising=False)
    path=tmp_path/'.local/share/ldcon-agent-tools/bin'/name
    path.parent.mkdir(parents=True);path.write_text('fixture');path.chmod(0o755)
    assert agent_providers.executable(provider)==str(path)
    monkeypatch.setenv('LDCON_'+provider.upper()+'_BIN','/missing/explicit/cli')
    assert agent_providers.executable(provider) is None


def test_effort_selection_cannot_silently_reset_model():
    wire=Wire('opencode',acp_setup('opencode',config())+[
        ('session/set_config_option',dict(configOptions=config('provider/other'))),
        ('session/set_config_option',dict(configOptions=config('provider/model','high')))],
        model='provider/other',effort='high')
    with pytest.raises(RuntimeError,match='final requested model'):wire.connect('')
    assert not wire.created and not any(m.get('method')=='session/prompt' for m in wire.sent)


def test_requested_profile_mutation_requires_new_connection():
    wire=Wire('opencode',acp_setup('opencode',config()),model='provider/model')
    wire.connect('')
    wire.p['model']='provider/other'
    with pytest.raises(RuntimeError,match='not confirmed'):wire.request({'prompt':'never'})
    assert not any(m.get('method')=='session/prompt' for m in wire.sent)


@pytest.mark.parametrize('provider', ['codex','opencode','deepseek_harness'])
def test_process_stdio_uses_negotiation_and_denies_permission(tmp_path,monkeypatch,provider):
    import sys
    from ldcon.agents import agent_sandbox
    script=tmp_path/'fake_agent.py'
    ledger=tmp_path/'wire.jsonl'
    script.write_text('''import json, sys
provider, ledger = sys.argv[1:]
session = "ba88d4be-031b-4f27-9ba3-80312dde68bb"
def send(value):
 print(json.dumps(value), flush=True)
for line in sys.stdin:
 m=json.loads(line)
 with open(ledger,'a') as f: f.write(json.dumps(m)+'\\n')
 method=m.get('method')
 if not method: continue
 result={}
 if method=='initialize': result={'protocolVersion':1,'agentCapabilities':{}}
 elif method=='account/read': result={'account':None,'requiresOpenaiAuth':False}
 elif method=='model/list': result={'data':[{'model':'fixture','supportedReasoningEfforts':[{'reasoningEffort':'low'}]}]}
 elif method=='thread/start': result={'thread':{'id':session},'model':'fixture','reasoningEffort':'low'}
 elif method=='session/new': result={'sessionId':session,'configOptions':[{'id':'model','type':'select','currentValue':'fixture','options':[{'value':'fixture'}]}]}
 elif method in ('turn/start','session/prompt'):
  if provider=='codex':
   send({'id':91,'method':'item/commandExecution/requestApproval','params':{}})
   send({'method':'item/agentMessage/delta','params':{'threadId':session,'turnId':'turn','delta':'safe reply'}})
   send({'method':'turn/completed','params':{'threadId':session,'turn':{'id':'turn','status':'completed'}}})
   result={'turn':{'id':'turn'}}
  else:
   send({'id':91,'method':'session/request_permission','params':{}})
   send({'method':'session/update','params':{'sessionId':session,'update':{'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':'safe reply'}}}})
   result={'stopReason':'end_turn'}
 if 'id' in m: send({'id':m['id'],'result':result})
''')
    class ProcessWire(SessionAdapter):
        def start(self,argv):
            super().start([sys.executable,'-u',str(script),provider,str(ledger)])
    calls=[]
    def boundary(argv,p):
        calls.append(p['provider'])
        return argv
    monkeypatch.setattr(agent_sandbox,'resident_command',boundary)
    adapter=ProcessWire(dict(provider=provider,workspace=str(tmp_path),instructions='policy',
                            instructions_resolved=True,request_timeout_s=3,model='fixture'),lambda:False)
    adapter._preflight={'binary':sys.executable,'version':'fixture'}
    try:
        adapter.connect('')
        assert adapter.request({'prompt':'test'})=='safe reply'
        deadline=time.monotonic()+1
        while not any(json.loads(v).get('id')==91 for v in ledger.read_text().splitlines()) and time.monotonic()<deadline:
            time.sleep(.01)
    finally:adapter.close()
    messages=[json.loads(v) for v in ledger.read_text().splitlines()]
    approval=next(v for v in messages if v.get('id')==91)
    assert approval['result']==({'decision':'decline'} if provider=='codex' else {'outcome':{'outcome':'cancelled'}})
    assert calls==[provider]
    assert adapter.proc.poll() is not None


PNG = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jR3sAAAAASUVORK5CYII='


def image_spec(path):
    import base64
    import hashlib
    raw=base64.b64decode(PNG)
    path.write_bytes(raw)
    return dict(path=str(path),mime_type='image/png',sha256=hashlib.sha256(raw).hexdigest())


@pytest.mark.parametrize('provider', ['codex','opencode','deepseek_harness'])
def test_image_bytes_are_attached_and_receipts_are_separate_from_vision_proof(tmp_path,provider):
    import base64
    spec=image_spec(tmp_path/'frame.png')
    def answer(wire,message):
        blocks=message['params']['input' if provider=='codex' else 'prompt']
        assert 'image_inputs' not in blocks[0]['text'] and spec['path'] not in blocks[0]['text']
        image=blocks[1]
        assert image['type']=='image'
        if provider=='codex':
            assert image['url']=='data:image/png;base64,'+PNG
            wire.events.put(dict(method='turn/completed',params=dict(threadId=SESSION,turn=dict(id='image-turn',status='completed'))))
            return dict(turn=dict(id='image-turn'))
        assert image['mimeType']=='image/png' and base64.b64decode(image['data'])==base64.b64decode(PNG)
        return dict(stopReason='end_turn')
    wire=Wire(provider,[('turn/start' if provider=='codex' else 'session/prompt',answer)])
    wire.p['workspace']=str(tmp_path);wire.session=SESSION
    wire.model_info['input_modalities']=['text','image']
    wire.request(dict(prompt='Inspect this pattern',image_inputs=[spec]))
    assert wire.image_receipts==[dict(sha256=spec['sha256'],mime_type='image/png',size_bytes=len(base64.b64decode(PNG)),status='response_completed')]
    assert wire.model_info['image_input_confirmation']=='response_completed'
    assert 'vision_verified' not in wire.model_info


@pytest.mark.parametrize('failure',['outside','hash','symlink','parent_symlink','directory','large','mime','relative','unsupported'])
def test_image_validation_rejects_before_any_prompt(tmp_path,failure):
    workspace=tmp_path/'workspace';workspace.mkdir()
    spec=image_spec(workspace/'frame.png')
    wire=Wire('deepseek_harness',[]);wire.p['workspace']=str(workspace);wire.session=SESSION
    wire.model_info['input_modalities']=['text','image']
    if failure=='outside':spec=image_spec(tmp_path/'outside.png')
    elif failure=='hash':spec['sha256']='0'*64
    elif failure=='symlink':
        link=workspace/'link.png';link.symlink_to(workspace/'frame.png');spec['path']=str(link)
    elif failure=='parent_symlink':
        folder=workspace/'folder';folder.mkdir();spec=image_spec(folder/'frame.png')
        link=workspace/'link';link.symlink_to(folder,target_is_directory=True);spec['path']=str(link/'frame.png')
    elif failure=='directory':spec['path']=str(workspace)
    elif failure=='large':
        with (workspace/'frame.png').open('wb') as f:f.truncate(9*1024*1024)
    elif failure=='mime':spec['mime_type']='image/jpeg'
    elif failure=='relative':spec['path']='frame.png'
    elif failure=='unsupported':wire.model_info['input_modalities']=['text']
    with pytest.raises((ValueError,RuntimeError)):wire.request(dict(prompt='must not run',image_inputs=[spec]))
    assert wire.sent==[]
    assert not wire.model_info['response_confirmed']


def test_run_directory_image_is_allowed_and_claude_never_claims_attachment(tmp_path):
    workspace=tmp_path/'workspace';workspace.mkdir()
    run=tmp_path/'run';run.mkdir()
    spec=image_spec(run/'frame.png')
    wire=Wire('opencode',[('session/prompt',dict(stopReason='end_turn'))])
    wire.p.update(workspace=str(workspace),tools_context=str(run/'context.json'))
    wire.model_info['input_modalities']=['text','image']
    wire.request(dict(image_inputs=[spec]))
    claude=Wire('claude',[])
    claude.p.update(wire.p,provider='claude')
    with pytest.raises(RuntimeError,match='no verified image'):claude.request(dict(image_inputs=[spec]))
    assert not claude.sent


def test_codex_cancellation_interrupts_identified_turn_without_replay():
    wire=Wire('codex',[('turn/start',dict(turn=dict(id='active-turn'))),('turn/interrupt',{})])
    wire.session=SESSION
    wire.cancel=lambda:wire._active_turn=='active-turn'
    with pytest.raises(RuntimeError,match='stopped'):wire.request(dict(prompt='only once'))
    assert [v['method'] for v in wire.sent]==['turn/start','turn/interrupt']
    assert wire.sent[-1]['params']==dict(threadId=SESSION,turnId='active-turn')
    assert not wire.model_info['response_confirmed']


def test_dsh_preflight_reads_only_help_and_version(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from ldcon.agents import agent_adapters
    binary=tmp_path/'dsh';binary.write_text('fixture');binary.chmod(0o755)
    monkeypatch.setattr(agent_providers,'executable',lambda _:str(binary))
    calls=[]
    def run(argv,**kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0,stdout='Usage --profile' if argv[-1]=='--help' else '0.2.1-rc.2')
    monkeypatch.setattr(agent_adapters.subprocess,'run',run)
    adapter=SessionAdapter(dict(provider='deepseek_harness'),lambda:False)
    assert adapter.preflight()['version']=='0.2.1-rc.2'
    assert calls==[[str(binary),'--help'],[str(binary),'--version']]
    assert adapter.model_info['authentication']=='not_checked'
