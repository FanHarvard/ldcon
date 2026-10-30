"""Fixed response identities survive worker dispatch and narrow policy routing."""
import copy
import json
from pathlib import Path
import socket
import sys

import pytest
import yaml
from ldcon.core.records import atomic_json,read_json
from ldcon.core.operation_registry import normalize_parameters,spec
from ldcon.jobs.response_algorithms import ALGORITHMS,POLICY_OPERATIONS
from ldcon.jobs import interface_worker,target_response,target_response_control as control

sys.path.insert(0,str(Path(__file__).resolve().parent))
from test_target_response_control import rig,STUDY,PARENT,policy,set_policy,request_binding


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*a,**k):raise AssertionError('No network/hardware in this regression')
    monkeypatch.setattr(socket.socket,'connect',forbidden)
    monkeypatch.setattr(socket,'create_connection',forbidden)
    monkeypatch.delenv('LDCON_TELEGRAF_ENDPOINT',raising=False)


def selected_card(operation):
    p={'shift_hz':100e6} if operation=='optimize.target_step' else {}
    return dict(id='response',operation=operation,uses=[],parameters=p)


def replace_operation(rig,operation):
    runtime,live,port,documents,laser=rig
    documents[STUDY]['cards']=[dict(operation=operation)]
    atomic_json(runtime.directory(STUDY)/'job.json',documents[STUDY])
    return runtime,live,port


@pytest.mark.parametrize('operation',ALGORITHMS)
def test_response_identity_cannot_switch_goal_or_execution_at_runtime(operation):
    card=selected_card(operation);p=normalize_parameters(card,{})
    goal,execution,search=ALGORITHMS[operation]
    assert p['optimization_goal']==goal and p['execution_mode']==execution
    assert spec(operation).parameter_validator is not None
    assert normalize_parameters(card,p)==p
    for key,value in [('optimization_goal','settling_and_feedforward' if goal!='settling_and_feedforward' else 'settling_criteria'),
                      ('execution_mode','deterministic' if execution=='agent' else 'agent')]:
        with pytest.raises(ValueError,match='different operation'):normalize_parameters(card,{key:value})
    if not search:
        with pytest.raises(ValueError,match='optimize.target_step'):normalize_parameters(card,{'shift_hz':100e6})


@pytest.mark.parametrize('operation',ALGORITHMS)
def test_every_response_identity_passes_laser_scoped_active_study(rig,operation):
    runtime,live,port=replace_operation(rig,operation)
    study,parent=control.active_study(runtime,STUDY)
    assert study['cards'][0]['operation']==operation and parent['id']==PARENT
    assert port.calls==[]
    if operation in POLICY_OPERATIONS:
        assert control._study_record(live,STUDY)==runtime.directory(STUDY)
    else:
        with pytest.raises(ValueError):control._study_record(live,STUDY)


@pytest.mark.parametrize('operation',sorted(POLICY_OPERATIONS))
def test_policy_algorithms_use_existing_scoped_queue_and_bound_token(rig,operation):
    runtime,live,port=replace_operation(rig,operation)
    receipt=set_policy(rig)
    assert receipt['status']=='completed' and port.calls==['status','set']
    assert control.bound_token(live,request_binding(receipt))=='d'*32
    status=policy(rig,dict(action='restore',expected_revision=receipt['result']['revision']))
    assert status['status']=='completed' and port.calls==['status','set','restore']


@pytest.mark.parametrize('operation',sorted(set(ALGORITHMS)-POLICY_OPERATIONS))
def test_measurement_algorithms_never_get_ff_mutation_via_public_or_worker_route(rig,operation):
    runtime,live,port=replace_operation(rig,operation)
    payload=dict(action='set',expected_revision='owner:0',scale=1.,step_hz=100e6,duration_s=20.)
    with pytest.raises(ValueError,match='does not admit'):
        control.request_policy(runtime,STUDY,payload)
    assert not list(live.directory.glob('target-ff-policy-request-*.json'))
    # The effect boundary must also validate operation identity, not trust that
    # a previous host queued only an authorized algorithm.
    rid='f'*32
    queued=dict(payload,request_id=rid,study_id=STUDY,parent_id=PARENT,
        laser_revision='laser:0',expires_unix_s=130.)
    atomic_json(live.directory/('target-ff-policy-request-'+rid+'.json'),queued)
    control.poll_policy(live,False)
    assert port.calls==[]
    results=list(live.directory.glob('target-ff-policy-rejected-*.json'))
    assert len(results)==1 and read_json(results[0],{})['status']=='rejected'
    # A stale policy artifact from a previous operation must not confer rights.
    atomic_json(runtime.directory(STUDY)/'target-ff-policy.json',dict(status='completed',study_id=STUDY,parent_id=PARENT,
        result=dict(revision='owner:1',session={'study_token':'d'*32})))
    forged=request_binding(read_json(runtime.directory(STUDY)/'target-ff-policy.json',{}))
    with pytest.raises(ValueError):control.bound_token(live,forged)


@pytest.mark.parametrize('operation',ALGORITHMS)
def test_actual_worker_dispatch_keeps_response_algorithm_identity(operation,tmp_path,monkeypatch):
    card=selected_card(operation)
    (tmp_path/'laser.yaml').write_text(yaml.safe_dump({'id':'fixture','name':'Fixture'}))
    (tmp_path/'interface-deck.yaml').write_text(yaml.safe_dump({'id':'fixture','cards':[card]}))
    (tmp_path/'invocation.json').write_text(json.dumps({'card_id':'response','parameters':card['parameters']}))
    import ldcon.agents.operating_mode_client as mode
    monkeypatch.setattr(mode,'observation',lambda _:dict(mode='characterization',revision='mode:1'))
    opened=[];calls=[]
    monkeypatch.setattr(interface_worker,'open_interfaces',lambda laser,c:opened.append(c['operation']) or {})
    def run(directory,laser,c,p,**kwargs):
        calls.append((c['operation'],copy.deepcopy(p)))
        return dict(status='succeeded',trials=[dict(samples=['large-raw-waveform'])])
    monkeypatch.setattr(target_response,'run_card',run)
    summary=interface_worker.run(tmp_path)
    assert summary['status']=='succeeded',summary
    assert len(calls)==1 and calls[0][0]==operation and opened==[operation]
    goal,execution,search=ALGORITHMS[operation]
    assert calls[0][1]['optimization_goal']==goal and calls[0][1]['execution_mode']==execution
    assert 'trials' not in summary
