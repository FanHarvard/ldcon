import copy
import importlib.util
import json
from pathlib import Path

import pytest
import yaml

from ldcon.agents import agent_task, resident_adjustment as runmod
from ldcon.agents.agent_background import route
from ldcon.agents.manual_adjustment_grant import ManualGrant
from ldcon.agents.resident_agent import AgentStore
from experiment_core.records import atomic_json

_spec=importlib.util.spec_from_file_location('gateway_fixture',Path(__file__).with_name('test_agent_background_gateway.py'))
_fixture=importlib.util.module_from_spec(_spec);_spec.loader.exec_module(_fixture)
gateway=_fixture.gateway
route=_fixture.route
_quality_spec=importlib.util.spec_from_file_location('quality_fixture',Path(__file__).with_name('test_adjustment_quality.py'))
_quality=importlib.util.module_from_spec(_quality_spec);_quality_spec.loader.exec_module(_quality)


def manual_setup(gateway):
    runtime,control,grants,grant=gateway
    card=dict(id='adjust',operation='agent.task',agent_execution=dict(mode='resident'),
              parameters=dict(instruction_profile='adjustment',agent_card_id='resident',prompt='Improve dither and side peaks'))
    monitor=dict(id='agent-monitor',operation='agent.monitor',parameters=dict(task_card_id='adjust',
        enabled=False,background={**grant['bounds'],'families':['current']}))
    p=agent_task.parameters(card,dict(adjustment_family='current'))
    directory=runtime.directory('task')
    atomic_json(directory/'invocation.json',dict(card_id='adjust',parameters=p,adjustment_plan=monitor['parameters']))
    (directory/'interface-deck.yaml').write_text(yaml.safe_dump(dict(cards=[card,monitor])))
    return directory,p


def test_manual_grant_without_running_monitor_retains_bounds_and_revocation(gateway):
    runtime,control,grants,original=gateway;directory,p=manual_setup(gateway)
    runtime.jobs['monitor']['status']='cancelled'
    g=ManualGrant(directory,p,runtime.root,'laser');g.renew()
    result=route(runtime,'task')
    assert result['grant_active'] and result['envelope']['half_span_v']==original['bounds']['half_span_v']
    assert route(runtime,'task',dict(action='begin',family='current',request_id='one'))['status']=='submitted'
    g.close()
    assert not route(runtime,'task')['grant_active']
    with pytest.raises(ValueError,match='stopped'):
        route(runtime,'task',dict(action='measure_here',request_id='late'))
    assert json.loads((grants/'monitor.json').read_text())==original


@pytest.mark.parametrize('damage',['bounds','source','task_id','hash'])
def test_manual_grant_does_not_accept_forged_or_wider_approval(gateway,damage):
    runtime,control,grants,_=gateway;directory,p=manual_setup(gateway)
    g=ManualGrant(directory,p,runtime.root,'laser');g.renew()
    value=json.loads(g.path.read_text())
    if damage=='bounds':value['bounds']['half_span_v']=100
    elif damage=='hash':value['parameters_hash']='other'
    else:value[damage]='other'
    atomic_json(g.path,value)
    with pytest.raises(ValueError,match='frozen approved'):route(runtime,'task')
    assert not (control/'adaptive-experiment.json').exists()


def test_manual_assignment_bounds_are_frozen_and_selection_only_narrows(gateway):
    runtime,_,_,_=gateway;directory,p=manual_setup(gateway)
    inv=json.loads((directory/'invocation.json').read_text())
    inv['adjustment_plan']['background']['half_span_v']=.007
    atomic_json(directory/'invocation.json',inv)
    g=ManualGrant(directory,p,runtime.root,'laser');g.renew()
    assert route(runtime,'task')['envelope']['half_span_v']==.007
    with pytest.raises(ValueError):
        route(runtime,'task',dict(action='begin',family='pi',request_id='not-selected'))


@pytest.fixture
def resident_rig(gateway,monkeypatch):
    runtime,control,grants,_=gateway;directory,p=manual_setup(gateway)
    (directory/'laser.yaml').write_text(yaml.safe_dump(_quality.document()))
    meta=dict(id='task',set_id='laser',deck_id='deck')
    store=AgentStore(runtime.root);key=store.key(meta,'resident')
    store.save(key,'opencode','existing-paid-session','ready','resident-job')
    selected=(store,meta,key,store.session(key));now=[0.];commands=[]
    monkeypatch.setattr('ldcon.agents.agent_history.knowledge_summary',lambda *a:{})
    def api(method,data=None):
        if method=='POST':
            commands.append(copy.deepcopy(data));receipt=route(runtime,'task',data)
            exp=control/'adaptive-experiments/task-task/adaptive-window';(exp/'inbox').mkdir(parents=True,exist_ok=True)
            state='idle' if data['action']=='begin' else 'finished' if data['action']=='adopt' else 'waiting_for_decision'
            atomic_json(exp/'status.json',dict(state=state,decision_revision='evidence-'+str(len(commands))))
            if data['action'] in ('measure_here','move_and_measure','adopt'):
                (exp/'results').mkdir(exist_ok=True)
                result=_quality.measurement(data['request_id'],100+50*len(commands),value=data.get('value',.77),
                    state='adopted' if data['action']=='adopt' else 'measured')
                if data['action']=='adopt':result['adoption']=dict(last=dict(state='adopted'))
                atomic_json(exp/'results'/('result-'+str(len(commands))+'.json'),result)
            return receipt
        return route(runtime,'task')
    kwargs=dict(selected=selected,api=api,clock=lambda:now[0],sleep=lambda dt:now.__setitem__(0,now[0]+dt),cancel=lambda:False,emit=lambda *a:None)
    return directory,p,kwargs,commands,store,key


def test_current_decisions_use_existing_resident_and_correlated_owner_receipts(resident_rig):
    directory,p,kw,commands,store,key=resident_rig
    quality=_quality.outcome();quality['measurement_ids']=['resident-task-2','resident-task-3']
    choices=iter([dict(action='begin',parameters=dict(family='current'),reason='Approved current window'),
        dict(action='measure_here',parameters={},reason='Measure baseline'),
        dict(action='measure_here',parameters={},reason='Repeat with fresh evidence'),
        dict(action='record_quality_outcome',parameters=quality,reason='Adequate with margin; stop exploring'),
        dict(action='adopt',parameters=dict(value=.77),reason='Best measured point')])
    result=runmod.run(directory,p,decide=lambda *a:json.dumps(next(choices)),**kw)
    assert result['status']=='succeeded' and result['session_id']=='existing-paid-session'
    assert [c['action'] for c in commands]==['begin','measure_here','measure_here','adopt']
    assert commands[1]['based_on']=='evidence-1' and commands[2]['based_on']=='evidence-2'
    assert result['owner_result']['latest_result']['adoption']['last']['state']=='adopted'
    assert store.session(key)['job']=='resident-job'
    assert result['quality_application']=='thresholds_unchanged'
    assert result['quality_outcome']['post_adoption_meets_old_with_margin'] is True
    assert result['quality_outcome']['application_status']=='thresholds_unchanged'


@pytest.mark.parametrize('wrapper', [
    '{}',
    "I'll start by reading the request index and then every referenced file.```json\n{}\n```",
])
def test_no_justified_adjustment_does_not_submit_physical_action(resident_rig, wrapper):
    directory,p,kw,commands,_,_=resident_rig
    answer = wrapper.format(json.dumps(dict(action='finish',parameters={},reason='Fresh evidence does not justify changing the point')))
    result=runmod.run(directory,p,decide=lambda *a:answer,**kw)
    assert result['disposition']=='no_adjustment' and commands==[]


def test_ambiguous_decisions_never_submit_physical_action(resident_rig):
    directory,p,kw,commands,_,_=resident_rig
    answer = '```json\n{"action":"begin","parameters":{},"reason":"Start"}\n```' * 2
    with pytest.raises(ValueError):runmod.run(directory,p,decide=lambda *a:answer,**kw)
    assert commands == []


def test_read_status_recovers_local_restart_without_replaying_command(resident_rig):
    from urllib.error import URLError
    directory,p,kw,commands,_,_=resident_rig
    p.update(adjustment_family='pi',monitor_job_id='monitor')
    failures=[0]
    def api(method,data=None):
        if method=='POST':
            commands.append(copy.deepcopy(data))
            return dict(status='submitted')
        if commands and failures[0]<3:
            failures[0]+=1
            raise URLError(ConnectionRefusedError(111,'Local API restarting'))
        return dict(operating_mode=dict(mode='measurement',revision=0),grant_active=True,
            supported_adjustments=['pi'],envelope=dict(families=['pi']),
            status=dict(state='succeeded' if commands else 'not_started',decision_revision='r1'))
    result=runmod.run(directory,p,decide=lambda *a:json.dumps(dict(action='begin',
        parameters=dict(family='pi'),reason='Compare')),**{**kw,'api':api})
    assert result['status']=='succeeded'
    assert failures[0]==3 and [c['action'] for c in commands]==['begin']


@pytest.mark.parametrize('cancel_after',[None,0.5])
def test_read_status_wait_is_bounded_and_cancellable(cancel_after):
    from urllib.error import URLError
    now=[0.]
    def api(method):
        assert method=='GET'
        raise URLError(ConnectionRefusedError(111,'Restarting'))
    def check():
        if cancel_after is not None and now[0]>=cancel_after:raise runmod.CardCancelled('Stop')
    with pytest.raises(URLError if cancel_after is None else runmod.CardCancelled):
        runmod.read_status(api,check=check,clock=lambda:now[0],
            sleep=lambda dt:now.__setitem__(0,now[0]+dt),emit=lambda *a:None)
    assert now[0]==(5.0 if cancel_after is None else cancel_after)


def test_read_status_does_not_retry_policy_or_invalid_response():
    from urllib.error import HTTPError
    for error in (HTTPError('local',403,'Denied',{},None),ValueError('Invalid JSON')):
        calls=[]
        def api(method):
            calls.append(method)
            raise error
        with pytest.raises(type(error)):
            runmod.read_status(api,check=lambda:None,clock=lambda:0,
                sleep=lambda dt:pytest.fail('No retry'),emit=lambda *a:None)
        assert calls==['GET']


def test_post_connection_failure_is_never_retried(resident_rig):
    from urllib.error import URLError
    directory,p,kw,commands,_,_=resident_rig
    original=kw['api']
    def api(method,data=None):
        if method=='POST':
            commands.append(data)
            raise URLError(ConnectionRefusedError(111,'Connection lost'))
        return original(method,data)
    with pytest.raises(URLError):
        runmod.run(directory,p,decide=lambda *a:json.dumps(dict(action='begin',
            parameters=dict(family='current'),reason='Compare')),**{**kw,'api':api})
    assert len(commands)==1


def test_status_after_reconnect_still_invalidates_changed_mode(resident_rig):
    from urllib.error import URLError
    directory,p,kw,commands,_,_=resident_rig
    original=kw['api'];failed=[False]
    def api(method,data=None):
        if method=='GET' and commands:
            if not failed[0]:
                failed[0]=True
                raise URLError(ConnectionRefusedError(111,'Restarting'))
            value=original('GET');value['operating_mode']['revision']+=1
            return value
        return original(method,data)
    with pytest.raises(runmod.CardCancelled,match='Operating mode changed'):
        runmod.run(directory,p,decide=lambda *a:json.dumps(dict(action='begin',
            parameters=dict(family='current'),reason='Compare')),**{**kw,'api':api})
    assert [c['action'] for c in commands]==['begin']


def test_current_repeated_begin_returns_protocol_error_without_second_admission(resident_rig):
    directory,p,kw,commands,_,_=resident_rig
    quality=_quality.outcome();quality['measurement_ids']=['resident-task-3','resident-task-4']
    choices=iter([
        dict(action='begin',parameters=dict(family='current'),reason='Start comparison'),
        dict(action='begin',parameters=dict(family='current'),reason='Misread idle as not admitted'),
        dict(action='measure_here',parameters={},reason='Measure admitted checkpoint'),
        dict(action='measure_here',parameters={},reason='Repeat checkpoint'),
        dict(action='record_quality_outcome',parameters=quality,reason='Repeated evidence'),
        dict(action='adopt',parameters=dict(value=.77),reason='Retain verified point')])
    seen=[]
    def decide(snapshot,history):
        seen.append(copy.deepcopy(history))
        return json.dumps(next(choices))
    result=runmod.run(directory,p,decide=decide,**kw)
    assert result['status']=='succeeded'
    assert [c['action'] for c in commands]==['begin','measure_here','measure_here','adopt']
    assert seen[2][-1]['effects']=='none'
    assert 'already admitted' in seen[2][-1]['host_validation_rejection']


@pytest.mark.parametrize('family', ['pi', 'slow_loop', 'feedforward'])
def test_parameter_begin_idle_waits_for_autonomous_result(resident_rig, family):
    directory,p,kw,commands,_,_=resident_rig
    p.update(adjustment_family=family, monitor_job_id='monitor')
    decisions=[]
    def api(method, data=None):
        if method=='POST':
            commands.append(copy.deepcopy(data))
            return dict(status='submitted')
        elapsed=kw['clock']()
        state='not_started' if not commands else 'idle' if elapsed<1 else 'tuning' if elapsed<2 else 'succeeded'
        return dict(operating_mode=dict(mode='measurement',revision=0),grant_active=True, supported_adjustments=[family], envelope=dict(families=[family]),
                    status=dict(state=state,decision_revision='same-admission'),
                    latest_result=dict(state='succeeded') if state=='succeeded' else None)
    def decide(*args):
        decisions.append(1)
        assert len(decisions)==1, 'Autonomous parameter comparison must not request a second model action'
        return json.dumps(dict(action='begin',parameters=dict(family=family),reason='One approved comparison'))
    result=runmod.run(directory,p,decide=decide,**{**kw,'api':api})
    assert result['status']=='succeeded'
    assert [c['action'] for c in commands]==['begin']
    assert kw['clock']()>=2


def test_parameter_admission_wait_remains_cancellable(resident_rig):
    directory,p,kw,commands,_,_=resident_rig
    p.update(adjustment_family='pi',monitor_job_id='monitor')
    def api(method,data=None):
        if method=='POST':
            commands.append(copy.deepcopy(data));return dict(status='submitted')
        return dict(operating_mode=dict(mode='measurement',revision=0),grant_active=True,supported_adjustments=['pi'],envelope=dict(families=['pi']),
                    status=dict(state='idle' if commands else 'not_started',decision_revision='same-admission'))
    with pytest.raises(runmod.CardCancelled):
        runmod.run(directory,p,decide=lambda *a:json.dumps(dict(action='begin',parameters=dict(family='pi'),reason='Trial')),
                   **{**kw,'api':api,'cancel':lambda:kw['clock']()>=1})
    assert [c['action'] for c in commands]==['begin']


def test_long_no_action_explanation_is_bounded_without_failed_task(resident_rig):
    directory,p,kw,commands,store,key=resident_rig
    answer=json.dumps(dict(action='finish',parameters={},reason='Healthy; no change needed. '*120))
    sleep=kw['sleep']
    def reply(dt):
        sleep(dt);row=store.claim(key)
        if row:store.finish(row['id'],'completed',answer)
    result=runmod.run(directory,p,**{**kw,'sleep':reply})
    assert result['disposition']=='no_adjustment' and not commands
    assert len(result['reason'])==2000 and result['reason'].endswith('…')
    assert json.loads((directory/'adjustment-answer-1.json').read_text())['answer']==answer


def test_long_reason_does_not_authorize_forbidden_parameters():
    snapshot=dict(status=dict(state='not_started'),supported_adjustments=['current'],envelope=dict(families=['current']))
    with pytest.raises(ValueError,match='Unsupported'):
        runmod.proposal(json.dumps(dict(action='begin',parameters=dict(family='current',half_span_v=50),reason='x'*2500)),snapshot,'current')


def test_replan_waits_for_claim_and_correlated_rejection_without_duplicate(resident_rig):
    directory,p,kw,commands,_,_=resident_rig
    original=kw['api'];state={'current':'not_started','revision':'initial'};pending=[];sleeps=[]
    def api(method,data=None):
        if method=='POST':
            commands.append(copy.deepcopy(data));pending.append(data)
            return dict(status='submitted')
        base=original('GET');base['status']=dict(state=state['current'],decision_revision=state['revision'])
        if 'result' in state:base['latest_result']=state['result']
        return base
    sleep=kw['sleep']
    def wait(dt):
        sleep(dt);sleeps.append(1)
        if pending and len(sleeps)%3==0:
            c=pending.pop(0)
            if c['action']=='begin':state.update(current='awaiting_replan',revision='recovered')
            elif c['action']=='replan':state['result']=dict(id=c['request_id'],state='replan_rejected')
            elif c['action']=='cancel':state.update(current='cancelled',revision='stopped')
    choices=iter([dict(action='begin',parameters=dict(family='current'),reason='Start'),
        dict(action='replan',parameters={},reason='Owner recovered'),
        dict(action='finish',parameters={},reason='No safe next candidate')])
    runmod.run(directory,p,decide=lambda *a:json.dumps(next(choices)),**{**kw,'api':api,'sleep':wait})
    assert [c['action'] for c in commands]==['begin','replan','cancel']
    assert len(sleeps)==9


def test_resident_mailbox_transport_uses_same_session_no_child_provider(resident_rig):
    directory,p,kw,commands,store,key=resident_rig
    sleep=kw['sleep']
    def reply(dt):
        sleep(dt);row=store.claim(key)
        if row:
            payload=json.loads(row['payload']);assert payload['kind']=='adjustment_decision'
            if 'request' in payload:
                from ldcon.core.laser_assets import LaserAssets
                payload=restore_evidence(LaserAssets(directory.parents[1],'laser'),payload['request'])
            assert 'No safety expansion' in payload['prompt']
            store.finish(row['id'],'completed',json.dumps(dict(action='finish',parameters={},reason='No valid current map yet')))
    result=runmod.run(directory,p,**{**kw,'sleep':reply})
    assert result['disposition']=='no_adjustment' and not commands
    assert len(store.requests(key))==1 and store.session(key)['session']=='existing-paid-session'


def test_late_decision_after_foreground_is_rejected_without_any_target_restore(resident_rig):
    directory,p,kw,commands,_,_=resident_rig
    first=[True]
    def decide(snapshot,history):
        if first.pop() if first else False:
            return json.dumps(dict(action='begin',parameters=dict(family='current'),reason='Measure'))
        control=directory.parents[2]/'trusted/laser/controller'
        exp=control/'adaptive-experiments/task-task/adaptive-window'
        atomic_json(exp/'status.json',dict(state='foreground_ready',decision_revision='new-target'))
        return json.dumps(dict(action='adopt',parameters=dict(value=.77),reason='Stale suggestion'))
    with pytest.raises(runmod.CardCancelled,match='late proposal'):
        runmod.run(directory,p,decide=decide,**kw)
    assert [c['action'] for c in commands]==['begin']


def test_timeout_withdraws_only_queued_decision_and_revokes_manual_grant(resident_rig):
    directory,p,kw,commands,store,key=resident_rig;p['timeout_s']=1
    with pytest.raises(TimeoutError):runmod.run(directory,p,**kw)
    assert not commands and store.requests(key)[0]['status']=='cancelled'


def test_proposal_cannot_expand_safety_or_act_in_another_family():
    snapshot=dict(status=dict(state='not_started'),supported_adjustments=['current','pi'],envelope=dict(families=['current']))
    for value in [dict(action='begin',parameters=dict(family='pi'),reason='x'),
                  dict(action='begin',parameters=dict(family='current',half_span_v=50),reason='x'),
                  dict(action='target',parameters=dict(value=5),reason='x')]:
        with pytest.raises(ValueError):runmod.proposal(json.dumps(value),snapshot,'current')


def test_raw_evidence_is_losslessly_chunked(tmp_path):
    from ldcon.core.laser_assets import LaserAssets,layout
    layout(tmp_path,'laser',create=True);assets=LaserAssets(tmp_path,'laser')
    value=dict(raw_patterns=[list(range(20000)) for _ in range(5)],dither=dict(value=4.1))
    result=runmod.evidence_files(assets,'data/task-evidence',value)
    assert restore_evidence(assets,result)==value


def restore_evidence(assets,result):
    def restore(node):
        if isinstance(node,dict) and 'path' in node:return json.loads(assets.read(node['path'])['content'])
        if isinstance(node,dict) and 'merge' in node:return {k:v for part in node['merge'] for k,v in restore(part).items()}
        if isinstance(node,dict) and 'concatenate' in node:return sum((restore(x) for x in node['concatenate']),[])
        if isinstance(node,dict) and 'text' in node:return ''.join(restore(x) for x in node['text'])
        if isinstance(node,dict) and 'items' in node:return [restore(x) for x in node['items']]
        if isinstance(node,dict):return {k:restore(v) for k,v in node.items()}
        return node
    return restore(json.loads(assets.read(result['index']['path'])['content']))


@pytest.mark.parametrize('prompt_size',[16528,35000,90000])
def test_large_measurement_and_index_fit_real_mailbox_without_losing_data(tmp_path,prompt_size):
    from ldcon.core.laser_assets import LaserAssets,layout
    layout(tmp_path,'laser',create=True);assets=LaserAssets(tmp_path,'laser')
    # A measured point has many scalar fields as well as raw pattern arrays.
    # Duplicating these 96 file references plus instructions overflowed 32k.
    evidence={**{'metric_'+str(i):i for i in range(95)},
              'raw_patterns':list(range(10000))}
    payload=dict(kind='adjustment_decision',job='task',prompt='x'*prompt_size,evidence=evidence)
    before=copy.deepcopy(payload)
    prefix='data/adjustment-'+'a'*32+'-3'
    packed=runmod.mailbox_payload(assets,prefix,payload)
    assert len(json.dumps(packed,ensure_ascii=False))<=31000
    store=AgentStore(tmp_path);store.enqueue('resident','large-measurement',packed)
    stored=json.loads(store.claim('resident')['payload'])
    if 'request' in stored:
        assert restore_evidence(assets,stored['request'])==payload
    else:
        assert restore_evidence(assets,stored['evidence'])==evidence
        assert stored['prompt'].startswith(payload['prompt'])
    assert payload==before


def test_live_deck_exception_is_limited_to_future_task_routing():
    from types import SimpleNamespace
    from ldcon.core.configuration import _task_execution_only_deck
    before=dict(id='deck',cards=[dict(id='adjust',operation='agent.task',uses=[],
        parameters=dict(instruction_profile='adjustment',execution_profile='old'),agent_execution=dict(mode='required',task_profile='old')),
        dict(id='lock',operation='servo.ecdl_steady_lock',parameters=dict(gain=1))])
    runtime=SimpleNamespace(interface_repository=SimpleNamespace(read=lambda *a:dict(document=before)))
    after=copy.deepcopy(before);after['cards'][0]['parameters']['execution_profile']='';after['cards'][0]['agent_execution']=dict(mode='resident')
    assert _task_execution_only_deck(runtime,'deck',after)
    for mutate in [lambda d:d['cards'][1]['parameters'].update(gain=2),
                   lambda d:d['cards'][0]['parameters'].update(instruction_profile='research'),
                   lambda d:d['cards'].reverse(),lambda d:d.update(id='other')]:
        bad=copy.deepcopy(after);mutate(bad);assert not _task_execution_only_deck(runtime,'deck',bad)


@pytest.mark.parametrize('outcome',['applied','pending','unknown'])
def test_current_quality_application_uses_host_route_once_and_reports_owner_confirmation(resident_rig,outcome):
    directory,p,kw,commands,_,_=resident_rig
    quality=_quality.outcome();quality['measurement_ids']=['resident-task-2','resident-task-3']
    quality['changes']={_quality.PATH:.35}
    choices=iter([dict(action='begin',parameters=dict(family='current'),reason='Start'),
        dict(action='measure_here',parameters={},reason='Baseline'),
        dict(action='measure_here',parameters={},reason='Repeat'),
        dict(action='record_quality_outcome',parameters=quality,reason='History-aware operational criterion'),
        dict(action='adopt',parameters=dict(value=.77),reason='Verified best practical point')])
    calls=[]
    def quality_api(method,data=None):
        calls.append((method,data))
        if method=='CONFIG':return dict(revision='live-CAS')
        if method=='POST':
            assert data['expected_revision']=='live-CAS'
            assert commands[-1]['action']=='adopt'
            if outcome=='unknown':raise OSError('Reply lost')
            return dict(status='saved_pending_owner',applied=False)
        return dict(status='applied',applied=True,owner_effective_revision='owner-new') if outcome=='applied' else dict(status='saved_pending_owner',applied=False)
    result=runmod.run(directory,p,decide=lambda *a:json.dumps(next(choices)),quality_api=quality_api,**kw)
    assert len([c for c in calls if c[0]=='POST'])==1
    assert result['quality_outcome']['applied'] is (outcome=='applied')
    assert result['quality_application']=={'applied':'applied','pending':'saved_pending_owner','unknown':'unconfirmed_no_retry'}[outcome]
    assert result['status']=='succeeded'  # Physical adoption and quality-save disposition are distinct.


def test_mode_change_while_first_decision_runs_discards_answer_and_replans(resident_rig,monkeypatch):
    directory,p,kw,commands,_,_=resident_rig
    mode=[dict(mode='measurement',revision=0)]
    monkeypatch.setattr('ldcon.agents.agent_background.mode_read',lambda laser:dict(mode[0]))
    seen=[]
    def decide(snapshot,history):
        seen.append(snapshot['operating_mode']['mode'])
        if len(seen)==1:
            mode[0]=dict(mode='characterization',revision=1)
            return json.dumps(dict(action='begin',parameters=dict(family='current'),reason='Obsolete decision'))
        return json.dumps(dict(action='finish',parameters={},reason='New mode read; no justified comparison in this fixture'))
    result=runmod.run(directory,p,decide=decide,**kw)
    assert seen==['measurement','characterization'] and not commands
    assert result['physical_outcome']=='no_action_submitted'
    second=json.loads((directory/'adjustment-input-2.json').read_text())
    assert 'uncertain benefit' in second['prompt'] and second['evidence']['snapshot']['operating_mode']['revision']==1


def test_mode_change_after_admission_stops_old_plan_without_new_movement(resident_rig,monkeypatch):
    directory,p,kw,commands,_,_=resident_rig
    mode=[dict(mode='measurement',revision=0)]
    monkeypatch.setattr('ldcon.agents.agent_background.mode_read',lambda laser:dict(mode[0]))
    api=kw['api']
    def switching_api(method,data=None):
        result=api(method,data)
        if method=='POST' and data['action']=='begin':mode[0]=dict(mode='characterization',revision=1)
        return result
    with pytest.raises(runmod.CardCancelled,match='Operating mode changed'):
        runmod.run(directory,p,decide=lambda *a:json.dumps(dict(action='begin',parameters=dict(family='current'),reason='Compare')),
                   **{**kw,'api':switching_api})
    assert [c['action'] for c in commands]==['begin']
    summary=json.loads((directory/'resident-adjustment-summary.json').read_text())
    assert 'Grant revoked' in summary['cleanup']


@pytest.mark.parametrize('bad', [
    '{"action":"finish","parameters":{},"reason":"No trial justified"}}',
    '{"action":[],"parameters":{},"reason":"Malformed action"}',
    '{"action":"begin","parameters":{"family":"current","half_span_v":50},"reason":"Invalid authority"}',
])
def test_locally_rejected_reply_is_corrected_without_owner_call(resident_rig,bad):
    directory,p,kw,commands,_,_=resident_rig
    seen=[]
    def decide(snapshot,history):
        seen.append(copy.deepcopy(history))
        if len(seen)==1:return bad
        assert history[-1]['effects']=='none'
        assert history[-1]['host_validation_rejection']
        return json.dumps(dict(action='finish',parameters={},reason='No justified trial'))
    result=runmod.run(directory,p,decide=decide,**kw)
    assert result['status']=='succeeded' and result['disposition']=='no_adjustment'
    assert commands==[] and len(seen)==2


def test_malformed_replan_is_returned_to_same_model_without_repeating_begin(resident_rig):
    directory,p,kw,commands,_,_=resident_rig
    p.update(adjustment_family='pi',monitor_job_id='monitor')
    candidate=dict(kp_count_per_nm=-100,ki_count_per_nm_s=-21069,integral_limit_count=60)
    replies=iter([
        dict(action='begin',parameters=dict(family='pi',pi_candidate=candidate),reason='Compare'),
        dict(action='replan',parameters=dict(pi_candidate=candidate),reason='Incorrect key'),
        dict(action='replan',parameters=dict(candidate=candidate),reason='Correct key after rejection')])
    def api(method,data=None):
        if method=='POST':commands.append(copy.deepcopy(data));return dict(status='submitted')
        state='not_started' if not commands else 'awaiting_replan' if len(commands)==1 else 'unchanged'
        return dict(operating_mode=dict(mode='measurement',revision=0),grant_active=True,
            supported_adjustments=['pi'],envelope=dict(families=['pi']),
            status=dict(state=state,decision_revision='r'+str(len(commands))))
    def decide(snapshot,history):
        if len(history)==2:
            assert 'candidate' in history[-1]['host_validation_rejection']
            assert history[-1]['effects']=='none'
        return json.dumps(next(replies))
    result=runmod.run(directory,p,decide=decide,**{**kw,'api':api})
    assert result['status']=='succeeded'
    assert [c['action'] for c in commands]==['begin','replan']
    assert commands[-1]['candidate']==candidate and commands[-1]['based_on']=='r1'


def test_repeated_invalid_replies_are_bounded_and_never_submit(resident_rig):
    directory,p,kw,commands,_,_=resident_rig
    calls=[]
    def decide(*a):calls.append(1);return '{}'
    with pytest.raises(ValueError,match='Three consecutive'):
        runmod.run(directory,p,decide=decide,**kw)
    assert len(calls)==3 and commands==[]


def test_measured_finish_without_quality_review_is_bounded_and_incomplete(resident_rig):
    directory,p,kw,commands,_,_=resident_rig
    original=kw['api'];terminal=[None];decisions=[]
    def api(method,data=None):
        if method=='POST' and data['action']=='cancel':
            commands.append(copy.deepcopy(data));terminal[0]=data
            return dict(status='submitted')
        if method=='GET' and terminal[0]:
            return dict(operating_mode=dict(mode='measurement',revision=0),
                status=dict(state='cancelled',decision_revision='done',active=False),
                latest_result=dict(id=terminal[0]['request_id'],state='cancelled',disposition='checkpoint_restored'))
        return original(method,data)
    replies=iter([
        dict(action='begin',parameters=dict(family='current'),reason='Inspect quality'),
        dict(action='measure_here',parameters={},reason='Current evidence'),
        dict(action='finish',parameters={},reason='Does not pass the old limit'),
        dict(action='finish',parameters={},reason='Insufficient comparable history; no reliable threshold proposal')])
    def decide(snapshot,history):
        if len(history)==3:
            assert history[-1]['effects']=='none'
            assert 'best practical' in history[-1]['host_validation_rejection']
            assert [c['action'] for c in commands]==['begin','measure_here']
        decisions.append(1)
        return json.dumps(next(replies))
    result=runmod.run(directory,p,decide=decide,**{**kw,'api':api})
    assert len(decisions)==4
    assert [c['action'] for c in commands]==['begin','measure_here','cancel']
    assert result['status']=='inconclusive' and result['disposition']=='quality_review_incomplete'
    assert not result['quality_objective_completed']


@pytest.mark.parametrize('disposition,correlated,active,expected',[
    ('retained_last_output',True,False,'succeeded'),
    ('checkpoint_restored',True,False,'succeeded'),
    ('retained_last_output',False,False,'cancelled'),
    ('superseded_by_parent_arm',True,False,'cancelled'),
    ('checkpoint_restored',True,True,'cancelled'),
])
def test_finish_requires_correlated_clean_owner_completion(resident_rig,disposition,correlated,active,expected):
    directory,p,kw,commands,_,_=resident_rig
    choices=iter([dict(action='begin',parameters=dict(family='current'),reason='Compare'),
                  dict(action='finish',parameters={},reason='No reliable improvement; retain checkpoint')])
    def api(method,data=None):
        if method=='POST':commands.append(copy.deepcopy(data));return dict(status='submitted')
        state='not_started' if not commands else 'idle' if len(commands)==1 else 'cancelled'
        return dict(operating_mode=dict(mode='measurement',revision=0),grant_active=True,
            supported_adjustments=['current'],envelope=dict(families=['current']),
            status=dict(state=state,decision_revision=str(len(commands)),active=active if len(commands)>1 else False),
            latest_result=dict(id=commands[-1]['request_id'] if correlated and commands else 'other',
                state='cancelled',disposition=disposition))
    result=runmod.run(directory,p,decide=lambda *a:json.dumps(next(choices)),**{**kw,'api':api})
    assert result['status']==expected
    assert [c['action'] for c in commands]==['begin','cancel']
    if expected=='succeeded':
        assert result['disposition']=='unchanged' and 'No reliable improvement' in result['reason']


@pytest.mark.parametrize('status', ['unavailable', 'rejected'])
def test_explicit_read_refusal_before_any_command_is_known_inconclusive(resident_rig, status):
    directory,p,kw,commands,_,_=resident_rig
    calls=[]
    def api(method,data=None):
        calls.append(method)
        assert method=='GET'
        return dict(status=status,reason='One running supported controller is required; no lock is started')
    kw['api']=api
    def decide(*args):
        pytest.fail('No decision should be requested without a controller')
    result=runmod.run(directory,p,decide=decide,**kw)
    assert result['status']=='inconclusive'
    assert result['physical_outcome']=='no_action_submitted'
    assert result['disposition']=='adjustment_unavailable_before_command'
    assert calls==['GET'] and commands==[]
    summary=json.loads((directory/'resident-adjustment-summary.json').read_text())
    assert summary['decisions']==[] and 'Grant revoked' in summary['cleanup']


def test_read_refusal_after_command_is_not_downgraded_to_no_action(resident_rig):
    directory,p,kw,commands,_,_=resident_rig
    real=kw['api']
    def api(method,data=None):
        if method=='GET' and commands:
            return dict(status='unavailable',reason='Controller disappeared after submission')
        return real(method,data)
    kw['api']=api
    with pytest.raises(ValueError,match='disappeared') as caught:
        runmod.run(directory,p,decide=lambda *args:json.dumps(dict(action='begin',
            parameters=dict(family='current'),reason='Test approved trial')),**kw)
    assert not isinstance(caught.value,runmod.PreCommandUnavailable)
    assert len(commands)==1
    assert not (directory/'resident-adjustment-result.json').exists()


def test_lost_command_reply_remains_uncertain_and_is_not_retried(resident_rig):
    directory,p,kw,commands,_,_=resident_rig
    real=kw['api'];attempts=[]
    def api(method,data=None):
        if method=='POST':
            attempts.append(data)
            raise OSError('Reply lost after send')
        return real(method,data)
    kw['api']=api
    with pytest.raises(OSError,match='Reply lost'):
        runmod.run(directory,p,decide=lambda *args:json.dumps(dict(action='begin',
            parameters=dict(family='current'),reason='Test approved trial')),**kw)
    assert len(attempts)==1
    assert not (directory/'resident-adjustment-result.json').exists()
