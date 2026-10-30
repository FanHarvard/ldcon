"""Offline supervision: cached evidence, idempotent dispatch and grant revocation."""
import copy
from types import SimpleNamespace

import pytest
from experiment_core.agent_contracts import Conditions
from experiment_core.records import atomic_json
from ldcon.agents import agent_monitor, agent_history
from ldcon.core.interface_cards import CardCancelled
from ldcon.core.records import read_json


def rule(**extra):
    return dict(id='cadence',metric='sample_period_s',above=.2,clear_below=.1,
                persistence_s=2,cooldown_s=10,max_age_s=2,baseline=.04,**extra)


def params(**values):
    return agent_monitor.parameters({'parameters':values},{})


@pytest.mark.parametrize('readings',[
    dict(source_acquired_unix_s=1000.,acquired_s=12.,error_hz=-12e6,native_interval_us=44000),
    dict(source_acquired_unix_s=1000.,acquired_s=1000.,error_hz=-12e6,native_interval_last_s=.044),
])
def test_recorded_card_metrics_use_explicit_acquisition_clock(monkeypatch,readings):
    box=SimpleNamespace(meta={'set_id':'set'},_laser_id='laser',root='/unused')
    monkeypatch.setattr('ldcon.agents.agent_cards._api',lambda *a,**k:dict(jobs=[
        dict(id='job',set_id='set',laser={'id':'laser'},cards=[{'operation':'servo.native_current'}])]))
    monkeypatch.setattr('ldcon.agents.agent_log_read.read',lambda *a,**k:dict(data=readings))
    got=agent_monitor.snapshot(box)['metrics']
    assert got['abs_error_hz']['value']==12e6
    assert got['source_interval_s']['value']==pytest.approx(.044)
    assert got['abs_error_hz']['acquired_unix_s']==1000.
    assert got['source_interval_s']['valid'] is True


def test_monotonic_snapshot_time_never_becomes_fresh_monitor_evidence(monkeypatch):
    box=SimpleNamespace(meta={'set_id':'set'},_laser_id='laser',root='/unused')
    monkeypatch.setattr('ldcon.agents.agent_cards._api',lambda *a,**k:dict(jobs=[
        dict(id='job',set_id='set',laser={'id':'laser'},cards=[{'operation':'servo.native_current'}])]))
    monkeypatch.setattr('ldcon.agents.agent_log_read.read',lambda *a,**k:dict(data={
        'acquired_s':1000.,'error_hz':12e6,'dither_response':.003,
        'dither_source_unix_s':999.,'dither_response_valid':True}))
    got=agent_monitor.snapshot(box)['metrics']
    assert got['abs_error_hz']['valid'] is False
    assert got['abs_error_hz']['acquired_unix_s'] is None
    assert got['dither_response']['valid'] is True
    assert got['dither_response']['acquired_unix_s']==999.


def test_cadence_change_requires_distinct_valid_data_and_preserves_unresolved_state():
    c=Conditions([rule()])
    def feed(now,stamp,value=.06,valid=True):
        return c.observe({'sample_period_s':dict(value=value,acquired_unix_s=stamp,valid=valid)},now)
    assert feed(10,10)==[]
    assert feed(11,10)==[]  # Same cached sample is not new evidence.
    assert feed(12,12)[0]['condition']=='cadence'
    assert feed(13,13,valid=False)==[]
    assert feed(14,14)==[]  # No repeat while unresolved/cooling down.
    assert feed(15,15,value=.04)==[]
    assert feed(26,26,value=.02)==[]  # A faster source also triggers relative change.
    assert feed(28,28,value=.02)[0]['condition']=='cadence'


def test_sustained_degradation_repeats_only_with_distinct_fresh_persistent_samples():
    c=Conditions([rule(repeat_s=10)])
    def feed(t,stamp=None,valid=True,value=.06):
        return c.observe({'sample_period_s':dict(value=value,acquired_unix_s=t if stamp is None else stamp,valid=valid)},t)
    assert not feed(10)
    assert feed(12)[0]['repeated'] is False
    for t in range(13,22):assert not feed(t)
    assert not feed(22,stamp=21)
    assert feed(23)[0]['repeated'] is True
    assert not feed(35,valid=False)
    assert not feed(36)
    assert feed(38)[0]['repeated'] is True
    assert not feed(39,value=.04)
    assert not c.state['cadence']['active']


@pytest.mark.parametrize('repeat',[0,-1,float('inf'),True,'900'])
def test_repeat_interval_must_be_explicit_positive_finite(repeat):
    with pytest.raises(ValueError):Conditions([rule(repeat_s=repeat)])


def test_background_schema_requires_explicit_bounded_runtime_only_pi():
    background=dict(enabled=True,families=['pi'],handoff_timeout_s=10,timeout_s=300,
        pid_parameters=dict(controller_card_id='lock',save_settings=False,gain_factors=[.5]))
    original=copy.deepcopy(background)
    got=params(task_card_id='adjust',background=background)
    assert got['background']['pid_parameters']['save_settings'] is False
    assert background==original
    for invalid in (dict(background,pid_parameters=dict(save_settings=True)),
                    dict(background,families=[{}]),dict(background,families=['current'])):
        with pytest.raises(ValueError):params(task_card_id='adjust',background=invalid)


@pytest.fixture
def monitor(tmp_path,monkeypatch):
    root=tmp_path/'deployment';directory=root/'jobs'/'laser'/'monitor';directory.mkdir(parents=True)
    trusted=tmp_path/'trusted';trusted.mkdir()
    monkeypatch.setenv('LDCON_ADAPTIVE_ROOT',str(trusted))
    meta=dict(id='monitor',set_id='set',deck_id='deck',laser=dict(id='laser'))
    atomic_json(directory/'job.json',meta);(directory/'laser.yaml').write_text('id: laser\n')
    now=[1000.];iterations=[0];events=[];calls=[]
    monkeypatch.setattr(agent_monitor.time,'time',lambda:now[0])
    def sleep(dt):now[0]+=dt;iterations[0]+=1
    def forbidden(*a,**kw):calls.append((a,kw));raise AssertionError('Unexpected live API')
    monkeypatch.setattr('ldcon.agents.agent_cards._api',forbidden)
    def run(p,task_start=None,snapshot_fn=None,on_sleep=None,steps=4):
        def wait(dt):
            sleep(dt)
            if on_sleep:on_sleep(iterations[0])
        with pytest.raises(CardCancelled):
            agent_monitor.run(directory,p,clock=lambda:now[0],sleep=wait,
                cancel=lambda:iterations[0]>=steps,emit=lambda *e:events.append(e),
                snapshot_fn=snapshot_fn or (lambda box:dict(jobs=[],metrics={})),task_start=task_start)
        return read_json(directory/'agent-monitor-status.json')
    return SimpleNamespace(root=root,directory=directory,trusted=trusted,run=run,now=now,calls=calls,events=events)


def test_passive_monitor_never_starts_a_lock_and_unknown_task_does_not_duplicate(monitor):
    launched=[]
    def task(card,prompt,state):
        # A worker can finish as failed while its interrupted model turn has
        # an unknown outcome. The inner receipt is authoritative for replay.
        if card is None:return dict(status='failed',task=dict(status='unknown'))
        launched.append((card,prompt));return dict(id='task1')
    state=monitor.run(params(task_card_id='review',interval_s=1),task_start=task)
    assert len(launched)==1 and 'Do not restart intentionally stopped locks' in launched[0][1]
    assert state['task_job_id']=='task1' and not monitor.calls
    grant=read_json(monitor.trusted/'laser/monitors/monitor.json')
    assert grant['enabled'] is False


def test_repeated_degradation_queued_while_busy_is_dropped_when_recovered(monitor):
    launched=[]
    def snapshot(box):
        now=monitor.now[0]
        return dict(metrics={'x':dict(value=2 if now<1010 else 0,valid=True,acquired_unix_s=now)})
    def task(card,prompt,state):
        if card:
            launched.append(card);return dict(id='task1')
        return dict(status='running' if monitor.now[0]<1012 else 'succeeded')
    condition=dict(id='x',metric='x',above=1,clear_below=.5,persistence_s=0,cooldown_s=2,max_age_s=3,repeat_s=2)
    state=monitor.run(params(task_card_id='adjust',interval_s=0,conditions=[condition]),
                      task_start=task,snapshot_fn=snapshot,steps=8)
    assert launched==['adjust'] and state['pending_events']==[]


def test_lost_admission_response_reuses_identical_payload_despite_prompt_change(monitor):
    p=params(task_card_id='review',interval_s=1,prompt='Original objective')
    submitted=[]
    def task(card,prompt,state):
        if card is None:return dict(status='running')
        submitted.append(copy.deepcopy(state['pending_admission']))
        if len(submitted)==1:raise OSError('Reply lost')
        return dict(id='task1')
    state=monitor.run(p,task_start=task,on_sleep=lambda count:p.update(prompt='Edited objective'))
    assert len(submitted)==2 and submitted[0]==submitted[1]
    assert submitted[0]['payload']['lookback_s']==p['lookback_s']
    assert state['sequence']==1 and 'pending_admission' not in state


def test_disabling_monitor_revokes_live_grant_while_task_is_busy(monitor):
    p=params(task_card_id='adjust',background=dict(enabled=True,half_span_v=.001,
        max_points=5,handoff_timeout_s=2,timeout_s=30))
    grants=[]
    def task(card,prompt,state):return dict(id='task1') if card else dict(status='running')
    def after(count):
        grants.append(read_json(monitor.trusted/'laser/monitors/monitor.json'))
        if count==1:p['enabled']=False
    monitor.run(p,task_start=task,on_sleep=after)
    assert grants[0]['enabled'] is True and grants[1]['enabled'] is False
    assert grants[0]['bounds']['half_span_v']==.001
    assert all(g['generation']==grants[0]['generation'] for g in grants)
    assert not monitor.calls


@pytest.mark.parametrize('failure_at',['snapshot','task_status'])
def test_temporary_api_restart_keeps_monitor_alive_and_revokes_effect_grant(monitor,failure_at):
    p=params(task_card_id='adjust',interval_s=3600,background=dict(enabled=True,half_span_v=.1,
        max_points=20,handoff_timeout_s=20,timeout_s=300))
    snapshots=[0];statuses=[0];starts=[];seen=[]
    def snapshot(box):
        snapshots[0]+=1
        if failure_at=='snapshot' and snapshots[0]==2:raise OSError('simulated API restart')
        return dict(metrics={},jobs=[])
    def task(card,prompt,state):
        if card:
            starts.append(card);return dict(id='task1')
        statuses[0]+=1
        if failure_at=='task_status' and statuses[0]==1:raise OSError('simulated status outage')
        return dict(status='running')
    def after(_):seen.append(read_json(monitor.trusted/'laser/monitors/monitor.json')['enabled'])
    state=monitor.run(p,task_start=task,snapshot_fn=snapshot,on_sleep=after,steps=5)
    assert starts==['adjust'] and seen[:3]==[True,False,True]
    assert state['task_job_id']=='task1'
    assert any(e[0]=='agent_monitor_data_recovered' for e in monitor.events)
    assert not monitor.calls


def test_history_waits_for_data_volume_and_does_not_use_failure_only_selection(monitor):
    tasks=[]
    def snap(box):
        return dict(metrics={'x':dict(value=1,valid=True,acquired_unix_s=monitor.now[0])})
    def task(card,prompt,state):
        if not card:return dict(status='running')
        tasks.append((card,prompt));return dict(id='history1')
    monitor.run(params(interval_s=0,history_task_card_id='research',history_interval_s=60,
                       history_min_new_samples=3),task_start=task,snapshot_fn=snap)
    assert len(tasks)==1 and tasks[0][0]=='research' and 'normal and failed periods' in tasks[0][1]


def test_snapshot_uses_acquisition_stamp_and_invalid_dither_is_not_fresh(monkeypatch,tmp_path):
    rows=[dict(id='lock',set_id='set',laser=dict(id='laser'),cards=[dict(operation='servo.ecdl_steady_lock')])]
    monkeypatch.setattr('ldcon.agents.agent_cards._api',lambda *a:dict(jobs=rows))
    monkeypatch.setattr('ldcon.agents.agent_log_read.read',lambda *a,**k:dict(content={
        'acquired_unix_s':10,'current':.5,'dither_response':5,'dither_source_unix_s':8,'dither_response_valid':False,
        'unknown':dict(value=1)}))
    result=agent_monitor.snapshot(SimpleNamespace(root=tmp_path,_laser_id='laser',meta=dict(set_id='set')))
    assert result['metrics']['current']['acquired_unix_s']==10
    assert not result['metrics']['dither_response']['valid']
    monkeypatch.setattr('ldcon.agents.agent_log_read.read',lambda *a,**k:dict(content={'current':.5}))
    assert not agent_monitor.snapshot(SimpleNamespace(root=tmp_path,_laser_id='laser',meta=dict(set_id='set')))['metrics']['current']['valid']


def test_history_stratifies_time_and_records_only_proposed_knowledge(tmp_path,monkeypatch):
    calls=[]
    def collect(root,laser,**kw):calls.append(kw);return dict(statuses=['normal','failed'],complete=False)
    monkeypatch.setattr(agent_history,'collect_log_context',collect)
    value=agent_history.history(tmp_path,'laser',10,50,windows=4)
    assert [v['since'] for v in calls]==[10,20,30,40] and not value['complete']
    record=dict(hypothesis='Cadence may explain residual error',evidence=['job-a'],counterevidence=['job-b'],
                applicability='Same source cadence and calibration',validation_plan='Held-out replay before any physical trial')
    first=agent_history.propose(tmp_path,'laser',record,task_id='task1')
    second=agent_history.propose(tmp_path,'laser',record,task_id='task1')
    assert first==second and first['status']=='proposed'
    summary=agent_history.knowledge_summary(tmp_path,'laser')
    assert len(summary['records'])==1 and summary['authority']=='none'
    with pytest.raises(ValueError):agent_history.propose(tmp_path,'laser',{**record,'status':'validated'},task_id='task1')


def test_parameter_comparison_plan_and_authority_are_explicit():
    from ldcon.core.agent_authority import classify,SAFETY
    plan=dict(objective='frequency_rms_hz',window_s=20.,min_samples=10,settle_s=5.,max_sample_gap_s=3.,
        timeout_s=300.,minimum_improvement_fraction=.1,maximum_cadence_fraction=.2,minimum_tec_excursion_v=0.)
    background=dict(enabled=True,families=['slow_loop'],handoff_timeout_s=20.,timeout_s=300.,adjustment_parameters=dict(slow_loop=plan))
    got=params(task_card_id='adjust',background=background)
    assert got['background']==background
    for extra in (dict(candidate=dict(tec_gain=.1)),dict(timeout_s=30.),dict(min_samples=2)):
        with pytest.raises(ValueError):params(task_card_id='adjust',background={**background,'adjustment_parameters':dict(slow_loop={**plan,**extra})})
    for path in [('enabled',),('bounds','tec_gain'),('bounds','supervision_interval_s'),('max_excursion','current')]:
        assert classify('lasers',('devices','rig','control_settings','background_adjustments','slow_loop',*path))==SAFETY
    assert classify('assignments',('decks','0','parameters','monitor','background','adjustment_parameters','slow_loop','window_s'))==SAFETY
