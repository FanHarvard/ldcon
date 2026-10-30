"""Study decision integration uses real records and a mock provider; no hardware."""
import copy
import json
from pathlib import Path

import pytest

from ldcon.jobs import study_execution as execution, target_response as study
from ldcon.jobs import target_response_decisions as policy
from ldcon.agents.resident_agent import AgentStore
import importlib.util
_spec=importlib.util.spec_from_file_location("study_fixture",Path(__file__).with_name("test_target_response.py"))
_fixture=importlib.util.module_from_spec(_spec);_spec.loader.exec_module(_fixture)
rig, config = _fixture.rig, _fixture.config


def select(rig):
    directory, clock, api, kwargs = rig
    root=directory.parents[2]
    store=AgentStore(root)
    meta=dict(id=directory.name,set_id='test',deck_id='deck')
    (directory/'job.json').write_text(json.dumps(meta))
    parent=directory.parent/'resident';parent.mkdir()
    (parent/'job.json').write_text(json.dumps(dict(set_id='test')))
    (parent/'progress.json').write_text(json.dumps(dict(status='running')))
    key=store.key(meta,'diagnostic-agent');store.save(key,'opencode','ses_fixture','ready','resident')
    return execution.resident(directory,'diagnostic-agent')


def run_agent(rig, provider):
    directory,clock,api,kwargs=rig
    p=config();p['execution_mode']='agent'
    return study.run_card(directory,copy.deepcopy(api.doc),{'id':'target-response'},p,
        resident_selection=select(rig),decision_provider=provider,**kwargs)


class Candidate:
    def propose(self,evidence,actions,budget):
        assert set(actions)=={'propose_profile','inconclusive'}
        assert evidence['files'] and 'physical' in evidence['instructions']
        return dict(action='propose_profile',parameters=dict(profile={k:config()[k] for k in policy.FIELDS},
            reason='Finite-record noise and response comparison',limitations='Synthetic data only'))


def test_agent_evaluates_same_raw_data_and_does_not_change_owner_or_defaults(rig):
    result=run_agent(rig,Candidate());directory,clock,api,_=rig
    assert result['optimization']['status']=='candidate_only'
    assert result['optimization']['all_trials_settled']
    assert not result['optimization']['applied']
    assert [v for _,v in api.writes]==[300e12+20e6,300e12]
    assert 'Agent evaluation' in (directory/'target-response.html').read_text()
    manifest=json.loads((directory/'study-evidence.json').read_text())
    samples=[]
    for item in manifest['files']:
        if 'samples-' in item['path']:
            value=json.loads((directory.parents[2]/'laser-assets/test'/item['path']).read_text())
            samples.extend(value['samples'])
    assert len(samples)==sum(len(t['history']['samples']) for t in result['trials'])
    assert all(x['bytes']<65536 for x in manifest['files'])
    assert (directory/'study-decision.sqlite3').exists()


@pytest.mark.parametrize('fault',['safety','coverage','bad_relation','prose','provider_failure'])
def test_invalid_or_failed_agent_keeps_raw_plots_and_no_extra_writes(rig,fault):
    class Bad(Candidate):
        def propose(self,*a):
            if fault=='provider_failure':raise OSError('provider unavailable')
            if fault=='prose':return {'answer':'Looks fine'}
            p=super().propose(*a)
            if fault=='safety':p['parameters']['profile']['kp']=3
            if fault=='coverage':p['parameters']['profile']['max_gap_s']=.15
            if fault=='bad_relation':p['parameters']['profile']['confirmation_s']=100
            return p
    result=run_agent(rig,Bad())
    assert result['status']=='inconclusive'
    assert result['measurement_status']=='succeeded'
    assert result['optimization']['status']=='failed'
    assert len(rig[2].writes)==2
    assert (rig[0]/'target-response.html').exists()
    assert len(json.loads((rig[0]/'target-response.json').read_text())['trials'])==2


def test_new_operator_target_during_analysis_is_never_restored(rig):
    class Foreground(Candidate):
        def propose(self,*a):
            api=rig[2];api.doc['target']['frequency_hz']+=100e6;api.revision+=1
            return super().propose(*a)
    result=run_agent(rig,Foreground())
    assert result['optimization']['status']=='candidate_only'
    assert rig[2].doc['target']['frequency_hz']==300e12+100e6
    assert len(rig[2].writes)==2


def test_resident_required_before_first_target_change(rig):
    directory,clock,api,kwargs=rig
    (directory/'job.json').write_text(json.dumps(dict(id='study',set_id='test',deck_id='deck')))
    p=config();p['execution_mode']='agent'
    with pytest.raises(ValueError,match='unavailable'):
        study.run_card(directory,api.doc,{'id':'response'},p,**kwargs)
    assert not api.writes


def test_durable_mailbox_response_unknown_and_cancel_no_replay(rig):
    selected=select(rig);store,meta,key,_=selected
    directory,clock,api,_=rig
    provider=execution.ResidentDecisions(directory,selected,clock=clock,sleep=clock.sleep,cancel=lambda:False,publish=lambda _:None)
    def process():
        row=store.claim(key)
        if row:store.finish(row['id'],'unknown','transport disconnected')
    clock.callback=process
    with pytest.raises(ValueError,match='unknown'):
        provider.propose(dict(instructions='Analysis only'),{},dict(deadline=clock()+2))
    assert store.request(key,provider.request_id)['status']=='unknown'
    assert len(store.requests(key))==1
    assert not api.writes


def test_queued_cancellation_never_interrupts_resident(rig):
    selected=select(rig);store,meta,key,_=selected;directory,clock,api,_=rig
    p=execution.ResidentDecisions(directory,selected,clock=clock,sleep=clock.sleep,cancel=lambda:True,publish=lambda _:None)
    with pytest.raises(study.CardCancelled):p.propose(dict(instructions='Analysis only'),{},dict(deadline=clock()+2))
    assert store.request(key,p.request_id)['status']=='cancelled'
    assert store.session(key)['status']=='ready'


def test_json_parser_does_not_guess_or_accept_nonfinite():
    assert execution.parse_proposal('```json\n{"action":"inconclusive","parameters":{"reason":"short"}}\n```')['action']=='inconclusive'
    for text in ('Probably {"a":1}', '{"value": NaN}'):
        with pytest.raises(ValueError):execution.parse_proposal(text)


@pytest.mark.parametrize('text', [
    'I will read the request files.```json\n{"action":"finish","parameters":{}}\n```',
    'Progress complete.\n```json\r\n{"action":"finish","parameters":{}}\r\n```\nNo adjustment justified.',
    '```\n{"action":"finish","parameters":{}}\n```',
])
def test_json_parser_accepts_one_explicit_decision_with_progress(text):
    assert execution.parse_proposal(text) == dict(action='finish', parameters={})


@pytest.mark.parametrize('text', [
    '```json\n{"action":"finish"}',
    '```json\n{"action":"finish"}\n```\n```json\n{"action":"begin"}\n```',
    '{"action":"begin"}\n```json\n{"action":"finish"}\n```',
    '```json\n{"action":"finish"}\n```\n{"action":"begin"}',
    '```python\n{"action":"finish"}\n```',
    'Progress.```json\n{"value":NaN}\n```',
    'Progress.```json\n{"action":"finish"} {"action":"begin"}\n```',
    'Progress.```json\n{"action":\n```',
])
def test_json_parser_rejects_ambiguous_or_incomplete_decisions(text):
    with pytest.raises(ValueError):execution.parse_proposal(text)
