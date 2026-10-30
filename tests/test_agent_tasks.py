"""Host/task integration using fake models and the real scoped Toolbox routes."""
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml

from experiment_core.records import Journal, atomic_json, digest
from ldcon.agents import agent_task, agent_profiles, agent_bundle, agent_tools
from ldcon.web import agent_task_api
from ldcon.core.interface_cards import CardCancelled
from ldcon.core.records import read_json
from ldcon.agents.resident_agent import AgentStore


@pytest.fixture
def task(tmp_path, monkeypatch):
    root=tmp_path/'deployment';directory=root/'jobs'/'laser'/'task1';directory.mkdir(parents=True)
    card=dict(id='analyze',operation='agent.task',parameters={},agent_execution=dict(mode='required',task_profile='specialist'))
    meta=dict(id='task1',set_id='laser',deck_id='deck',laser_id='laser',laser=dict(id='laser'),status='running',cards=[card])
    atomic_json(directory/'job.json',meta)
    (directory/'laser.yaml').write_text(yaml.safe_dump(dict(schema_version=1,id='laser',name='Laser',devices={},interfaces={})))
    (directory/'interface-deck.yaml').write_text(yaml.safe_dump(dict(schema_version=1,id='deck',name='Deck',cards=[card])))
    p=agent_task.parameters(card,dict(agent_card_id='resident',prompt='Inspect recorded evidence'))
    atomic_json(directory/'invocation.json',dict(card_id='analyze',parameters=p))
    document={**agent_profiles.EMPTY,'profiles':{'specialist':dict(provider='codex',model='exact-model',effort='high',
        required_tools=['laser_metrics'],max_input_bytes=262144,timeout_s=60,max_turns=2)}}
    agent_profiles.save(root,document,None)
    runtime=SimpleNamespace(root=root,get_job=lambda job:meta if job=='task1' else (_ for _ in ()).throw(FileNotFoundError(job)),directory=lambda job:directory)
    calls=[]
    def api(box,tail,method='GET',payload=None):
        calls.append((tail,method))
        route=urlsplit(tail)
        if route.path.endswith('/permission'):
            args=parse_qs(route.query)
            return agent_task_api.task_permission(runtime,'task1',args['laser_id'][0],args['tool'][0])
        if route.path.endswith('/probe'):return agent_task_api.task_probe(runtime,'task1',payload)
        raise AssertionError(tail)
    monkeypatch.setattr('ldcon.agents.agent_cards._api',api)
    monkeypatch.setattr('ldcon.agents.agent_log_context.collect_log_context',lambda *a,**k:dict(samples=[],coverage='fixture'))
    revoked=[]
    monkeypatch.setattr('ldcon.agents.agent_background.revoke_task',lambda *a:revoked.append(a))
    now=[0.];events=[];instances=[]
    class Adapter:
        def __init__(self,params,cancel):
            self.p=params;self.cancel=cancel;self.closed=False;self.requests=[];instances.append(self)
            self.model_info=dict(resolved_model=params['model'],resolved_effort=params['effort'],confirmation='server_reported',
                input_modalities=['text','image'],input_modalities_confirmation='model_catalog')
        def preflight(self):return dict(binary='fake',version='fixture')
        def connect(self,session):self.session_created('session-fixture');return 'session-fixture'
        def request(self,payload):
            self.requests.append(payload)
            if payload['kind']=='capability_probe':
                nonce=read_json(directory/'agent-probe-challenge.json')['nonce']
                assert nonce in payload['prompt']
                box=agent_tools.Toolbox(self.p['tools_context'])
                with pytest.raises(ValueError,match='phase'):box.call('laser_metrics',{})
                box.call('agent_capability_probe',dict(nonce=nonce))
                return 'Probe completed'
            assert agent_tools.Toolbox(self.p['tools_context']).call('laser_metrics',{})['laser_id']=='laser'
            return 'Completed offline analysis'
        def close(self):self.closed=True
    def run(adapter=Adapter,**kwargs):
        return agent_task.run(directory,p,clock=lambda:now[0],sleep=lambda _:None,cancel=lambda:False,
            emit=lambda *a:events.append(a),adapter_factory=adapter,bridge_registrar=lambda _:dict(status='registered'),**kwargs)
    return SimpleNamespace(root=root,directory=directory,card=card,meta=meta,p=p,run=run,Adapter=Adapter,
                           instances=instances,events=events,now=now,revoked=revoked,api_calls=calls,runtime=runtime)


def test_full_probe_is_real_toolbox_host_roundtrip_and_result_is_correlated(task):
    result=task.run()
    assert result['status']=='succeeded' and result['turns']==2
    assert result['profile']['model']=='exact-model'
    assert result['physical_outcome']=='not_inferred_from_agent_prose'
    receipt=read_json(task.directory/'agent-probe-receipt.json')
    assert receipt['laser_id']=='laser' and receipt['scoped_bundle_read'] is True
    assert receipt['operation_permissions']=='not_probed' and receipt['tool_listing']=='enumerated'
    assert any('/probe' in tail for tail,_ in task.api_calls)
    assert read_json(task.directory/'agent-task-phase.json')['phase']=='closed'
    with pytest.raises(ValueError):agent_tools.Toolbox(task.instances[0].p['tools_context']).call('laser_metrics',{})
    parent=AgentStore(task.root).request(AgentStore.key(task.meta,'resident'),'task-completed:task1')
    payload=json.loads(parent['payload'])
    assert payload['task_id']=='task1' and payload['job_id']=='task1' and payload['bundle_revision']==result['bundle_revision']
    assert read_json(task.directory/'agent-parent-delivery.json')['status']=='queued'
    assert task.instances[0].closed and len(task.revoked)==1
    # Reopening a finished card does not create another model or output notice.
    assert task.run()==result
    assert len(task.instances)==1 and len(task.revoked)==1


@pytest.mark.parametrize('failure',['no_auth','wrong_model','no_probe','budget'])
def test_no_work_turn_without_confirmed_route_probe_and_budget(task,failure):
    class Adapter(task.Adapter):
        def connect(self,session):
            if failure=='no_auth':raise RuntimeError('Authentication unavailable')
            if failure=='wrong_model':self.model_info['resolved_model']='cheaper-model'
            return super().connect(session)
        def request(self,payload):
            if failure=='no_probe':self.requests.append(payload);return 'claims it worked'
            result=super().request(payload)
            if failure=='budget':task.now[0]=61
            return result
    if failure=='budget':
        with pytest.raises(CardCancelled):task.run(Adapter)
    else:assert task.run(Adapter)['status']=='failed'
    assert not any(p['kind']=='card_task' for p in task.instances[0].requests)
    assert read_json(task.directory/'agent-task-phase.json')['phase']=='closed'
    assert read_json(task.directory/'agent-task-status.json')['physical_outcome']=='no_work_turn_started'


def test_unknown_work_is_durable_and_never_automatically_replayed(task):
    class Adapter(task.Adapter):
        def request(self,payload):
            if payload['kind']=='card_task':raise RuntimeError('disconnect after submission')
            return super().request(payload)
    with pytest.raises(RuntimeError,match='disconnect'):task.run(Adapter)
    assert read_json(task.directory/'agent-task-status.json')['status']=='unknown'
    revoked=len(task.revoked)
    with pytest.raises(RuntimeError,match='no automatic replay'):task.run(Adapter)
    assert len(task.instances)==1 and len(task.revoked)==revoked


def test_duplicate_active_runner_cannot_revoke_or_mutate_first_task(task):
    journal=Journal(task.directory/'agent-task.sqlite3')
    try:
        with journal.guard('task','task1'):
            with pytest.raises(Exception,match='Another action'):task.run()
        assert not task.revoked and not task.instances
        assert not (task.directory/'agent-task-phase.json').exists()
    finally:journal.close()


def test_admission_snapshot_pins_exact_model_across_registry_edit(task):
    selected=agent_profiles.resolve(task.root,'specialist')
    path=agent_task_api.admission_path(task.root,'task1');path.parent.mkdir(parents=True)
    atomic_json(path,dict(parameters=task.p,card_id='analyze',profile=selected))
    current=agent_profiles.read(task.root);current['document']['profiles']['specialist']['model']='new-default'
    agent_profiles.save(task.root,current['document'],current['revision'])
    result=task.run()
    assert result['profile']['model']=='exact-model' and task.instances[0].p['model']=='exact-model'


def test_readonly_task_and_probe_cannot_delegate_or_mutate_laser(task):
    class Adapter(task.Adapter):
        def request(self,payload):
            box=agent_tools.Toolbox(self.p['tools_context'])
            for tool in ('agent_task_start','laser_card_start','laser_quality_update','background_adjustment_step','teams_notify'):
                with pytest.raises(ValueError,match='outside'):box.call(tool,{})
            return super().request(payload)
    assert task.run(Adapter)['status']=='succeeded'


def test_required_images_need_exact_catalog_and_attached_evidence(task):
    current=agent_profiles.read(task.root);current['document']['profiles']['specialist']['required_modalities']=['text','image']
    agent_profiles.save(task.root,current['document'],current['revision'])
    result=task.run()
    assert result['status']=='failed' and 'not been attached' in result['message']
    assert not task.instances[0].requests


def test_bundle_probe_detects_modified_content_and_wrong_laser(tmp_path):
    bundle=agent_bundle.build(tmp_path,'diagnosis',context=dict(laser_id='laser'),evidence={})
    atomic_json(tmp_path/'agent-probe-challenge.json',dict(nonce='nonce',bundle_revision=bundle['bundle_revision']))
    box=SimpleNamespace(directory=tmp_path,_laser_id='other',context={})
    with pytest.raises(ValueError,match='laser mismatch'):agent_bundle.capability_probe(box,dict(nonce='nonce'))
    box._laser_id='laser';bundle['instructions']+='tampered';atomic_json(tmp_path/'agent-bundle.json',bundle)
    with pytest.raises(ValueError,match='integrity'):agent_bundle.capability_probe(box,dict(nonce='nonce'))


def test_parent_mailbox_outage_preserves_result_and_retry_delivers_once(task,monkeypatch):
    original=AgentStore.enqueue
    monkeypatch.setattr(AgentStore,'enqueue',lambda *a,**k:(_ for _ in ()).throw(OSError('offline')))
    result=task.run()
    assert result['status']=='succeeded' and read_json(task.directory/'agent-parent-delivery.json')['status']=='pending'
    monkeypatch.setattr(AgentStore,'enqueue',original)
    assert task.run()==result and len(task.instances)==1
    assert read_json(task.directory/'agent-parent-delivery.json')['status']=='queued'


@pytest.fixture
def runtime(tmp_path,monkeypatch):
    from .test_operation_gateway import configure
    from ldcon.web.web_runtime import WebRuntime
    laser,deck,_=configure(tmp_path)
    deck['cards']=[dict(id='resident',operation='agent.resident',uses=[],parameters={}),
        dict(id='analysis',operation='agent.task',uses=[],parameters={},agent_execution=dict(mode='required',task_profile='specialist'))]
    (tmp_path/'interface-decks'/'fixture-deck.yaml').write_text(yaml.safe_dump(deck))
    monkeypatch.setenv('LDCON_JOB_LAUNCHER','service')
    agent_profiles.save(tmp_path,{**agent_profiles.EMPTY,'profiles':{'specialist':dict(provider='codex',model='exact-model',max_input_bytes=262144)}},None)
    app=WebRuntime(tmp_path)
    parent=app.start_interface_card(app.get_set('fixture'),'fixture-deck','resident',{})
    atomic_json(app.directory(parent['id'])/'progress.json',dict(status='running'))
    yield app,parent
    app.close()


def test_task_api_idempotence_binds_prompt_and_returns_after_source_stops(runtime):
    from ldcon.web.web_runtime import ConflictError
    app,parent=runtime
    value=dict(source_job_id=parent['id'],card_id='analysis',prompt='bounded diagnosis',request_id='repeat',lookback_s=120)
    first=agent_task_api.start(app,value)
    invocation=read_json(app.directory(first['id'])/'invocation.json')
    assert invocation['parameters']['agent_card_id']=='resident'
    assert invocation['parameters']['lookback_s']==120
    admission=read_json(agent_task_api.admission_path(app.root,first['id']))
    assert admission['profile']['model']=='exact-model'
    current=agent_profiles.read(app.root);current['document']['profiles']['specialist']['model']='new-model'
    agent_profiles.save(app.root,current['document'],current['revision'])
    atomic_json(app.directory(parent['id'])/'progress.json',dict(status='cancelled'))
    assert agent_task_api.start(app,value)==first
    with pytest.raises(ConflictError,match='different content'):agent_task_api.start(app,{**value,'prompt':'changed'})
    assert len(list(app.jobs.glob('*/*/launch.request')))==2


def test_task_api_racing_same_id_creates_exactly_one_child(runtime):
    app,parent=runtime
    value=dict(source_job_id=parent['id'],card_id='analysis',prompt='bounded diagnosis',request_id='race')
    barrier=threading.Barrier(3);results=[];errors=[]
    def call():
        try:
            barrier.wait();results.append(agent_task_api.start(app,value))
        except Exception as error:errors.append(error)
    threads=[threading.Thread(target=call) for _ in range(2)]
    for t in threads:t.start()
    barrier.wait()
    for t in threads:t.join(3)
    assert not any(t.is_alive() for t in threads) and not errors
    assert results[0]==results[1]
    assert len(list(app.jobs.glob('*/*/launch.request')))==2


def test_task_api_cannot_create_recursive_child(runtime):
    app,parent=runtime
    first=agent_task_api.start(app,dict(source_job_id=parent['id'],card_id='analysis',prompt='one task',request_id='first'))
    atomic_json(app.directory(first['id'])/'progress.json',dict(status='running'))
    with pytest.raises(ValueError,match='no recursive'):
        agent_task_api.start(app,dict(source_job_id=first['id'],card_id='analysis',prompt='nested',request_id='second'))


def test_task_mounts_keep_journal_and_authority_readonly_with_scoped_writes(task,tmp_path,monkeypatch):
    from ldcon.agents import agent_sandbox
    task.run()
    p=task.instances[0].p
    home=tmp_path/'home';home.mkdir()
    (home/'.local').mkdir()
    authority=home/'authority';authority.mkdir()
    monkeypatch.setattr(Path,'home',lambda:home)
    monkeypatch.setenv('LDCON_ADAPTIVE_ROOT',str(authority))
    monkeypatch.setenv('LDCON_SHARED_ROOTS',json.dumps([str(tmp_path/'shared')]))
    (tmp_path/'shared').mkdir()
    args=agent_sandbox.resident_command(['fixture','transport'],p)
    bindings=[(args[i],args[i+1],args[i+2]) for i in range(len(args)-2) if args[i] in ('--bind','--ro-bind')]
    assert ('--ro-bind',str(task.root),str(task.root)) in bindings
    writable=[source for flag,source,_ in bindings if flag=='--bind']
    assert str(task.root) not in writable and str(home) not in writable
    assert str(task.directory) not in writable
    assert str(task.directory/'agent-tool-state') in writable
    assert str(task.root/'laser-assets'/'laser'/'data') in writable
    assert args[args.index(str(authority))-1]=='--tmpfs'


def test_saved_image_parameter_builds_hash_pinned_native_attachment(task):
    import base64,hashlib
    from .test_agent_model_negotiation import PNG
    image=task.root/'laser-assets'/'laser'/'data'/'recorded.png'
    image.parent.mkdir(parents=True);image.write_bytes(base64.b64decode(PNG))
    task.p['evidence_images']=['data/recorded.png']
    class Adapter(task.Adapter):
        def request(self,payload):
            if payload['kind']=='card_task':
                assert payload['image_inputs']==[dict(path=str(image),mime_type='image/png',sha256=hashlib.sha256(image.read_bytes()).hexdigest())]
            return super().request(payload)
    assert task.run(Adapter)['status']=='succeeded'


@pytest.mark.parametrize('images',[['../elsewhere.png'],['/outside.png'],['data/.hidden.png'],['data/a.svg'],['data/x.png']*9])
def test_image_parameters_cannot_escape_asset_scope(task,images):
    with pytest.raises(ValueError):agent_task.parameters(task.card,dict(evidence_images=images))


def test_generic_admission_helper_pins_before_launch_and_checks_laser(task):
    row=agent_task_api.pin_invocation(task.root,'task1',task.card,task.p,laser_id='laser')
    assert row['profile']['model']=='exact-model'
    changed=agent_profiles.read(task.root);changed['document']['profiles']['specialist']['model']='later-model'
    agent_profiles.save(task.root,changed['document'],changed['revision'])
    assert agent_task_api.pin_invocation(task.root,'task1',task.card,task.p,laser_id='laser')==row
    with pytest.raises(ValueError,match='differs'):
        agent_task_api.pin_invocation(task.root,'task1',task.card,task.p,laser_id='other')
    assert task.run()['profile']['model']=='exact-model'


def test_profile_registry_malformed_defaults_and_symlinks_are_domain_errors(tmp_path):
    for value in ({**agent_profiles.EMPTY,'resident_profile':[]},{**agent_profiles.EMPTY,'schema_version':True}):
        with pytest.raises(ValueError):agent_profiles.validate(value)
    folder=tmp_path/'agents';folder.mkdir()
    (folder/'execution-profiles.lock').symlink_to(tmp_path/'elsewhere')
    with pytest.raises(ValueError,match='lock'):agent_profiles.save(tmp_path,agent_profiles.EMPTY,None)


def test_opencode_task_keeps_asset_storage_and_isolates_provider_mcp_location(task):
    value=agent_profiles.read(task.root)
    value['document']['profiles']['specialist']['provider']='opencode'
    agent_profiles.save(task.root,value['document'],value['revision'])
    result=task.run()
    assert result['status']=='succeeded'
    p=task.instances[0].p
    assert p['workspace']==str(task.root/'laser-assets'/'laser')
    assert p['transport_workspace']==str(task.directory/'agent-tool-state'/'opencode-cwd')
    assert Path(p['transport_workspace']).is_dir()
    context=read_json(task.directory/'agent-transport-context.json')
    assert context['asset_workspace']==p['workspace'] and context['transport_workspace']==p['transport_workspace']
    assert 'not the resident inbox watcher' in p['instructions']
    assert 'Do not call bridge_read or bridge_send' in p['instructions']
    assert 'bridge_read' not in read_json(p['tools_context'])['task_allowed_tools']
    assert 'bridge_send' not in read_json(p['tools_context'])['task_allowed_tools']


def inherited_profiles(app):
    current=agent_profiles.read(app.root)
    for provider in ('claude','codex','opencode'):
        current['document']['profiles'][provider+'-standard']=dict(provider=provider,model=provider+'-exact',effort='high')
    agent_profiles.save(app.root,current['document'],current['revision'])
    path=app.root/'interface-decks'/'fixture-deck.yaml'
    deck=yaml.safe_load(path.read_text())
    deck['cards'][0]['parameters']['provider']='opencode'
    deck['cards'][1]['agent_execution']=dict(mode='required',provider_profiles={p:p+'-standard' for p in ('claude','codex','opencode')})
    deck['cards'][1]['parameters']['agent_card_id']='resident'
    path.write_text(yaml.safe_dump(deck))
    return deck


def test_web_and_gateway_task_admissions_follow_live_gateway_and_pin_across_switch(runtime):
    app,parent=runtime;deck=inherited_profiles(app)
    store=AgentStore(app.root);key=store.key(parent,'resident')
    store.save(key,'claude','claude-session','ready',parent['id'])
    delegated=agent_task_api.start(app,dict(source_job_id=parent['id'],card_id='analysis',prompt='inspect',request_id='inherit'))
    direct=app.start_interface_card(app.get_set('fixture'),'fixture-deck','analysis',dict(prompt='inspect'))
    for job in (delegated,direct):
        receipt=read_json(agent_task_api.admission_path(app.root,job['id']))
        assert receipt['profile']['id']=='claude-standard'
        assert receipt['profile']['model']=='claude-exact' and receipt['profile']['effort']=='high'
        assert receipt['selection']['origin']=='connected_gateway'
    store.save(key,'opencode','changed-session','ready',parent['id'])
    receipt=read_json(agent_task_api.admission_path(app.root,direct['id']))
    again=agent_task_api.pin_invocation(app.root,direct['id'],deck['cards'][1],receipt['parameters'],laser_id=receipt['laser_id'])
    assert again==receipt


def test_task_explicit_cross_provider_and_disconnected_default(runtime):
    app,parent=runtime;deck=inherited_profiles(app)
    explicit=agent_task_api.start(app,dict(source_job_id=parent['id'],card_id='analysis',prompt='inspect',request_id='override',profile='codex-standard'))
    receipt=read_json(agent_task_api.admission_path(app.root,explicit['id']))
    assert receipt['profile']['provider']=='codex' and receipt['selection']['origin']=='explicit_profile'
    direct=app.start_interface_card(app.get_set('fixture'),'fixture-deck','analysis',dict(prompt='inspect'))
    receipt=read_json(agent_task_api.admission_path(app.root,direct['id']))
    assert receipt['profile']['provider']=='opencode' and receipt['selection']['origin']=='configured_gateway'


def test_misbound_provider_profile_is_rejected_before_launch(runtime):
    app,parent=runtime;inherited_profiles(app)
    current=agent_profiles.read(app.root);current['document']['profiles']['opencode-standard']['provider']='codex'
    agent_profiles.save(app.root,current['document'],current['revision'])
    with pytest.raises(ValueError,match='does not match'):
        app.start_interface_card(app.get_set('fixture'),'fixture-deck','analysis',dict(prompt='inspect'))


def test_study_profile_admission_is_independent_of_deterministic_measurement(task):
    from ldcon.jobs.target_response import parameters
    card=dict(id='response',operation='optimize.settling_criteria',parameters={},agent_execution=dict(mode='required',task_profile='specialist'))
    deterministic=parameters(card,{})
    assert agent_task_api.pin_invocation(task.root,'measure',card,deterministic,laser_id='laser',meta=task.meta) is None
    p=parameters(card,dict(execution_mode='agent'))
    receipt=agent_task_api.pin_invocation(task.root,'study',card,p,laser_id='laser',meta=task.meta)
    assert receipt['profile']['id']=='specialist' and receipt['parameters']['execution_mode']=='agent'


def test_target_response_deck_accepts_agent_policy_without_changing_executor():
    from ldcon.core.interface_schema import validate_deck,card_interfaces
    card=dict(id='response',operation='optimize.settling_criteria',uses=list(card_interfaces('optimize.settling_criteria')),parameters={},
              agent_execution=dict(mode='required',provider_profiles={'claude':'claude-standard'}))
    deck=dict(schema_version=1,id='study',name='Study',interfaces=card_interfaces('optimize.settling_criteria'),cards=[card])
    validate_deck(deck)
    assert card['operation']=='optimize.settling_criteria'


@pytest.mark.parametrize('explicit', ['', 'codex-standard'])
def test_difficulty_admission_through_real_web_and_monitor_routes(runtime, explicit):
    from ldcon.agents import agent_routing
    app,parent=runtime;deck=inherited_profiles(app)
    document=dict(schema_version=1,default_difficulty='standard',provider_profiles={
        p:dict.fromkeys(('basic','standard','deep'),p+'-standard') for p in ('claude','codex','opencode')},
        gateway=dict(provider='claude',difficulty='basic'),lasers={})
    agent_routing.save(app.root,document,None)
    deck['cards'][1]['agent_execution']=dict(mode='required',difficulty='standard')
    (app.root/'interface-decks/fixture-deck.yaml').write_text(yaml.safe_dump(deck))
    # This is the same start path used by the monitor. No worker is launched:
    # the fixture writes launch requests to its temporary directory only.
    meta=read_json(app.directory(parent['id'])/'job.json')
    meta['cards'][0]['card']='agent.monitor'
    meta['cards'][0]['params']={'agent_card_id':'resident'}
    atomic_json(app.directory(parent['id'])/'job.json',meta)
    delegated=agent_task_api.start(app,dict(source_job_id=parent['id'],card_id='analysis',
        prompt='inspect',request_id='difficulty',profile=explicit))
    direct=app.start_interface_card(app.get_set('fixture'),'fixture-deck','analysis',
        dict(prompt='inspect',execution_profile=explicit))
    for job in (delegated,direct):
        receipt=read_json(agent_task_api.admission_path(app.root,job['id']))
        assert receipt['profile']['id']==(explicit or 'claude-standard')
        assert receipt['selection']['routing_snapshot']==document


def test_difficulty_worker_uses_admitted_table_after_settings_change(task):
    from ldcon.agents import agent_routing
    task.card['agent_execution']=dict(mode='required',difficulty='default')
    deck=dict(schema_version=1,id='deck',name='Deck',cards=[task.card,
        dict(id='resident',operation='agent.resident',parameters={})])
    (task.directory/'interface-deck.yaml').write_text(yaml.safe_dump(deck))
    matrix=dict(codex=dict.fromkeys(('basic','standard','deep'),'specialist'))
    agent_routing.save(task.root,dict(schema_version=1,default_difficulty='standard',provider_profiles=matrix,
        gateway=dict(provider='codex',difficulty='basic'),lasers={}),None)
    receipt=agent_task_api.pin_invocation(task.root,'task1',task.card,task.p,laser_id='laser',meta=task.meta,deck=deck)
    registry=agent_profiles.read(task.root)
    registry['document']['profiles']['replacement']=dict(provider='codex',model='different-model')
    agent_profiles.save(task.root,registry['document'],registry['revision'])
    routing=agent_routing.read(task.root)
    routing['document']['provider_profiles']['codex']['standard']='replacement'
    agent_routing.save(task.root,routing['document'],routing['revision'])
    result=task.run()
    assert result['status']=='succeeded'
    assert result['profile']['id']=='specialist'
    assert receipt['selection']['routing_snapshot']['provider_profiles']['codex']['standard']=='specialist'
