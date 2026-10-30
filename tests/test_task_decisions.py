"""Exercise model identity, same-session reporting and cancelled turn isolation."""
import json
import threading
import time
from types import SimpleNamespace

import pytest
from experiment_core.records import atomic_json
from ldcon.agents import task_decisions, agent_profiles, agent_tools
from ldcon.agents.resident_agent import AgentStore
import importlib.util
from pathlib import Path
spec=importlib.util.spec_from_file_location('task_fixture',Path(__file__).with_name('test_agent_tasks.py'))
fixtures=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixtures)
task=fixtures.task


def child(task, monkeypatch, *, wrong=False, block=False, real_registration=False):
    if not real_registration:
        monkeypatch.setattr('ldcon.agents.resident_agent.register_bridge',lambda p:{'status':'registered'})
    store=AgentStore(task.root);key=store.key(task.meta,'resident')
    store.save(key,'opencode','parent-session','ready','parent-job')
    profile=agent_profiles.resolve(task.root,'specialist');profile.update(provider='opencode',max_turns=5)
    class Adapter(task.Adapter):
        def connect(self,session):
            if wrong:self.model_info['resolved_effort']='low'
            agent_tools.handle(agent_tools.Toolbox(self.p['tools_context']),dict(id=1,method='tools/list'))
            return 'child-session'
        def interrupt(self):self.interrupted=True
        def request(self,payload):
            if payload['kind']=='capability_probe':return super().request(payload)
            self.requests.append(payload)
            if block:
                while not self.cancel():time.sleep(.01)
            return 'Task-authored explanation' if payload['kind']=='task_explanation' else '{"action":"finish","parameters":{},"reason":"Enough evidence"}'
    value=task_decisions.TaskDecisions(task.directory,(store,task.meta,key,store.session(key)),profile,
        clock=time.monotonic,cancel=lambda:False,deadline=time.monotonic()+60,adapter_factory=Adapter)
    return value,store,key


def test_confirmed_child_uses_same_session_to_explain_and_keeps_parent(task,monkeypatch):
    c,store,key=child(task,monkeypatch)
    try:
        c.connect();before=store.session(key)
        assert c.adapter.p['task_depth']==1
        assert c.adapter.p['transport_workspace'].endswith('/agent-tool-state/opencode-cwd')
        assert c.request({'kind':'decision','prompt':'Choose'})
        report=c.explain({'physical_outcome':'adopted_by_owner','quality_application':'saved_pending_owner'})
        assert report['text']=='Task-authored explanation' and report['relay']=='verbatim'
        assert report['author']['session']=='child-session'
        assert report['author']['model_info']['resolved_effort']=='high'
        assert store.session(key)==before
        with pytest.raises(ValueError):agent_tools.Toolbox(c.adapter.p['tools_context']).call('laser_card_stop',{'job_id':'servo'})
    finally:c.close()
    assert json.loads((task.directory/'agent-task-phase.json').read_text())['phase']=='closed'


def test_wrong_effort_stops_before_work(task,monkeypatch):
    c,_,_=child(task,monkeypatch,wrong=True)
    try:
        with pytest.raises(ValueError,match='model/effort'):c.connect()
        assert not c.adapter.requests
    finally:c.close()


def test_catalog_waits_for_delayed_provider_discovery_before_first_model_turn(task,monkeypatch):
    c,_,_=child(task,monkeypatch)
    def connect(session):
        threading.Timer(.1,lambda:agent_tools.handle(agent_tools.Toolbox(c.adapter.p['tools_context']),
            dict(id=1,method='tools/list'))).start()
        return 'child-session'
    c.adapter.connect=connect
    try:
        c.connect()
        assert c.adapter.requests[0]['kind']=='capability_probe'
        assert (task.directory/'agent-probe-receipt.json').exists()
    finally:c.close()


def test_stale_catalog_does_not_authorize_a_model_turn(task,monkeypatch):
    c,_,_=child(task,monkeypatch)
    c.adapter.connect=lambda session:'child-session'
    atomic_json(task.directory/'agent-tool-state'/'agent-tool-catalog.json',dict(nonce='old',task_id='task1',laser_id='laser',tools=sorted(task_decisions.TOOLS)))
    monkeypatch.setattr(task_decisions,'CATALOG_TIMEOUT_S',.1)
    try:
        with pytest.raises(ValueError,match='MCP catalog'):c.connect()
        assert not c.adapter.requests
    finally:c.close()


def test_catalog_receipt_is_not_a_successful_model_probe(task,monkeypatch):
    c,_,_=child(task,monkeypatch)
    c.adapter.request=lambda payload:'Claims readiness without calling a tool'
    try:
        with pytest.raises(ValueError,match='evidence-tool probe'):c.connect()
        assert (task.directory/'agent-tool-state'/'agent-tool-catalog.json').exists()
        assert not (task.directory/'agent-tool-catalog.json').exists()
        assert not (task.directory/'agent-probe-receipt.json').exists()
    finally:c.close()


def test_gateway_disconnect_does_not_cancel_card_decisions_or_authored_result(task,monkeypatch):
    c,store,key=child(task,monkeypatch)
    try:
        c.connect()
        store.save(key,'opencode','parent-session','disconnected','parent-job')
        assert c.request({'kind':'decision','prompt':'Evaluate recorded evidence'})
        report=c.explain({'physical_outcome':'unchanged'})
        assert report['status']=='completed' and report['author']['session']=='child-session'
        assert store.session(key)['status']=='disconnected'
    finally:c.close()


def test_host_registers_before_model_grant_without_exposing_bridge_mutation(task,monkeypatch):
    monkeypatch.setenv('LDCON_AGENTBRIDGE_PUBLIC',str(task.root/'public-hub-fixture'))
    calls=[]
    def bridge(box,args,**kwargs):
        calls.append(args)
        assert box.context['agent_role']=='task' and 'task_depth' not in box.context
        return json.dumps({'id':box.context['bridge_id']})
    monkeypatch.setattr(agent_tools.Toolbox,'bridge',bridge)
    c,_,_=child(task,monkeypatch,real_registration=True)
    try:
        assert len(calls)==1 and calls[0][0]=='register'
        c.connect()
        with pytest.raises(ValueError,match='outside this task grant'):
            agent_tools.Toolbox(c.adapter.p['tools_context']).call('bridge_register',{'summary':'not a model capability'})
        assert len(calls)==1
    finally:c.close()


def test_foreground_tick_cancels_pending_model_without_waiting(task,monkeypatch):
    c,_,_=child(task,monkeypatch,block=True)
    try:
        c.connect();start=time.monotonic()
        def tick():
            if time.monotonic()-start>.05:raise RuntimeError('foreground preemption')
        with pytest.raises(RuntimeError,match='foreground'):c.request({'kind':'decision'},tick=tick)
        assert time.monotonic()-start<1 and c.adapter.interrupted
    finally:c.close()


def test_monitor_quality_binding_preserves_unrelated_limits_and_hysteresis():
    from ldcon.agents.monitor_quality_binding import conditions
    spec=importlib.util.spec_from_file_location('quality_fixture',Path(__file__).with_name('test_adjustment_quality.py'))
    q=importlib.util.module_from_spec(spec);spec.loader.exec_module(q)
    document=q.document
    rules=[dict(id='quality-side',metric='fringe_metrics.side_peak_height_max',above=.2,clear_below=.18,persistence_s=60),
        dict(id='frequency',metric='abs_error_hz',above=10e6,clear_below=5e6)]
    doc=document();doc['devices']['rig']['control_settings']['optical']['single_mode_max']['side_peak_height_max']=.35
    result=conditions(rules,doc)
    assert result[0]['above']==.35 and result[0]['clear_below']==pytest.approx(.315)
    assert result[0]['persistence_s']==60 and result[1]==rules[1] and rules[0]['above']==.2


def test_gateway_role_exposes_dispatch_but_not_tuning_or_configuration(task):
    from ldcon.agents.resident_agent import prepare_context
    from ldcon.agents.gateway_role import TOOLS
    from ldcon.agents.agent_tools import Toolbox
    store=AgentStore(task.root)
    p=prepare_context(task.directory,dict(provider='opencode',workspace='',role='gateway'),store,task.meta,'gateway')
    box=Toolbox(p['tools_context'])
    assert {'laser_card_start','laser_card_stop','agent_task_start','agent_task_status'}<=TOOLS
    for name in ['laser_quality_update','laser_configuration_update','background_adjustment_step','laser_files_write']:
        with pytest.raises(ValueError,match='grant'):box.call(name,{})


def test_result_parent_can_be_absent_without_a_new_gateway_session(task):
    from ldcon.jobs.study_execution import decision_parent
    selected=decision_parent(task.directory,'gateway')
    assert selected[3]['status']=='absent' and selected[3]['session'] is None
    assert selected[0].session(selected[2]) is None
