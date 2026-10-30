import json
from pathlib import Path

import pytest

from experiment_core.records import atomic_json
from ldcon.agents.agent_background import route as raw_route,revoke_monitor,revoke_task


def route(runtime, task_id, data=None):
    # Existing fixtures represent a caller that read measurement revision 0.
    if data is not None:data={'expected_operating_mode_revision':0, **data}
    return raw_route(runtime, task_id, data)


class Runtime:
    def __init__(self,root):
        self.root=root
        self.jobs={}

    def directory(self,identifier):return self.root/'jobs'/identifier
    def get_job(self,identifier):return self.jobs[identifier]
    def active_jobs(self,*,set_id):
        return [j for j in self.jobs.values() if j['set_id']==set_id and j['status']=='running']


@pytest.fixture
def gateway(tmp_path,monkeypatch):
    monkeypatch.setattr('ldcon.agents.agent_background.mode_read',lambda laser:dict(laser_id=laser['id'],mode='measurement',revision=0))
    runtime=Runtime(tmp_path/'deployment')
    from types import SimpleNamespace
    runtime.interface_repository=SimpleNamespace(read=lambda kind,identifier:dict(document=dict(id=identifier)))
    for identifier,operation,params in (
            ('task','agent.task',dict(instruction_profile='adjustment',monitor_job_id='monitor')),
            ('monitor','agent.monitor',dict(sample_interval_s=2)),
            ('controller','servo.ecdl_steady_lock',{})):
        runtime.jobs[identifier]=dict(id=identifier,set_id='laser-set',status='running',cards=[dict(operation=operation)])
        directory=runtime.directory(identifier);directory.mkdir(parents=True)
        atomic_json(directory/'invocation.json',dict(parameters=params))
        (directory/'laser.yaml').write_text('id: laser\n')
    host=tmp_path/'trusted';control=host/'laser/controller';control.mkdir(parents=True)
    grants=host/'laser/monitors';grants.mkdir()
    bounds=dict(enabled=True,half_span_v=.01,max_points=8,timeout_s=100,handoff_timeout_s=10)
    grant=dict(generation='generation',bounds=bounds,enabled=True,last_seen=100.,max_age_s=30.,parameters_hash='approved')
    atomic_json(grants/'monitor.json',grant)
    monkeypatch.setenv('LDCON_ADAPTIVE_ROOT',str(host))
    monkeypatch.setattr('ldcon.agents.agent_background.time.time',lambda:100.)
    return runtime,control,grants,grant


def admitted(gateway):
    runtime,control,grants,grant=gateway
    result=route(runtime,'task',dict(action='begin',request_id='begin'))
    assert result['experiment_id']=='task-task'
    experiment=control/'adaptive-experiments/task-task/adaptive-window'
    (experiment/'inbox').mkdir(parents=True)
    atomic_json(experiment/'status.json',dict(state='waiting_for_decision',active=False,decision_revision='fresh'))
    return experiment


def test_exact_task_selector_and_trusted_bounds_ignore_editable_monitor_status(gateway):
    runtime,control,grants,grant=gateway
    atomic_json(runtime.directory('monitor')/'agent-monitor-status.json',dict(enabled=True,background=dict(half_span_v=1000)))
    experiment=admitted(gateway)
    selector=json.loads((control/'adaptive-experiment.json').read_text())
    assert selector['half_span_v']==.01 and selector['background']['generation']=='generation'
    status=route(runtime,'task')
    assert status['grant_active'] and status['controller_job_id']=='controller'
    assert status['supported_adjustments']==['current','pi']
    assert status['unsupported_adjustments']==['slow_loop','feedforward']
    result=route(runtime,'task',dict(action='adopt',request_id='adopt',value=.77,based_on='fresh'))
    assert result['status']=='submitted'
    assert json.loads((experiment/'inbox/adopt.json').read_text())==dict(id='adopt',action='adopt',value=.77,based_on='fresh')


def test_continuation_requires_worker_capability_and_verified_revision(gateway):
    runtime,control,_,_=gateway
    atomic_json(control/'protocol.json',dict(schema_version=1,continuation=True))
    experiment=admitted(gateway)
    selector=json.loads((control/'adaptive-experiment.json').read_text())
    assert selector['continuation'] is True
    with pytest.raises(ValueError,match='recoverable'):
        route(runtime,'task',dict(action='replan',request_id='early',based_on='fresh'))
    atomic_json(experiment/'status.json',dict(state='awaiting_replan',active=False,decision_revision='recovered'))
    with pytest.raises(ValueError,match='evidence'):
        route(runtime,'task',dict(action='replan',request_id='stale',based_on='fresh'))
    with pytest.raises(ValueError,match='candidate'):
        route(runtime,'task',dict(action='replan',request_id='unsafe',based_on='recovered',candidate=dict(max_span=99)))
    r=route(runtime,'task',dict(action='replan',request_id='new-plan',based_on='recovered'))
    assert r['status']=='submitted'
    assert json.loads((control/'adaptive-experiment.json').read_text())==selector


def test_legacy_worker_does_not_receive_new_continuation_selector(gateway):
    _,control,_,_=gateway;admitted(gateway)
    assert 'continuation' not in json.loads((control/'adaptive-experiment.json').read_text())


@pytest.mark.parametrize('which',['monitor','controller'])
def test_other_laser_cannot_supply_grant_or_controller(gateway,which):
    runtime,control,grants,grant=gateway
    (runtime.directory(which)/'laser.yaml').write_text('id: another-laser\n')
    with pytest.raises(ValueError,match='another laser'):route(runtime,'task',dict(action='begin',request_id='begin'))
    assert not (control/'adaptive-experiment.json').exists()


def test_status_and_cancel_remain_available_after_stop_without_new_writes(gateway):
    runtime,control,grants,grant=gateway
    experiment=admitted(gateway)
    runtime.jobs['monitor']['status']='cancelled'
    revoke_monitor(runtime.root,'laser','monitor','generation')
    assert route(runtime,'task')['grant_active'] is False
    with pytest.raises(ValueError,match='stopped'):
        route(runtime,'task',dict(action='move_and_measure',request_id='late',value=.77,based_on='fresh'))
    result=route(runtime,'task',dict(action='cancel',request_id='stop',based_on='stale-is-ok-for-cancel'))
    assert result['status']=='submitted' and (experiment/'inbox/stop.json').exists()
    assert not (experiment/'inbox/late.json').exists()


@pytest.mark.parametrize('change',[dict(enabled=False),dict(last_seen=0.),dict(generation=None),dict(parameters_hash=None)])
def test_disabled_stale_or_incomplete_trusted_grant_cannot_begin(gateway,change):
    runtime,control,grants,grant=gateway
    atomic_json(grants/'monitor.json',{**grant,**change})
    with pytest.raises(ValueError,match='stopped'):
        route(runtime,'task',dict(action='begin',request_id='begin'))
    assert not (control/'adaptive-experiment.json').exists()


def test_task_revocation_cannot_be_undone_by_same_begin_id(gateway):
    runtime,control,grants,grant=gateway
    admitted(gateway)
    revoke_task(runtime.root,'laser','task')
    assert route(runtime,'task',dict(action='begin',request_id='begin-again'))['status']=='revoked'
    with pytest.raises(ValueError,match='revoked'):
        route(runtime,'task',dict(action='move_and_measure',request_id='late',value=.77,based_on='fresh'))


def test_revocation_is_scoped_to_monitor_generation_and_exact_task(gateway):
    runtime,control,grants,grant=gateway
    admitted(gateway)
    revoke_monitor(runtime.root,'laser','monitor','another-generation')
    revoke_task(runtime.root,'laser','unrelated-task')
    assert json.loads((control/'background-control.json').read_text())['enabled'] is True
    assert json.loads((grants/'monitor.json').read_text())['enabled'] is True
    revoke_task(runtime.root,'laser','task')
    assert json.loads((control/'background-control.json').read_text())['enabled'] is False
    assert json.loads((grants/'monitor.json').read_text())['enabled'] is True


@pytest.mark.parametrize('data',[
    dict(action='pi',request_id='unsupported'),dict(action='move_and_measure',request_id='missing',based_on='fresh'),
    dict(action='adopt',request_id='nan',value=float('nan'),based_on='fresh'),
    dict(action='measure_here',request_id='extra',value=.77,based_on='fresh'),
    dict(action='move_and_measure',request_id='bool',value=True,based_on='fresh')])
def test_unsupported_or_invalid_physical_requests_never_enqueue(gateway,data):
    runtime,control,grants,grant=gateway
    experiment=admitted(gateway)
    with pytest.raises(ValueError):route(runtime,'task',data)
    assert list((experiment/'inbox').iterdir())==[]


def test_no_running_lock_is_unavailable_and_never_started(gateway):
    runtime,control,grants,grant=gateway
    runtime.jobs['controller']['status']='cancelled'
    result=route(runtime,'task',dict(action='begin',request_id='begin'))
    assert result['status']=='unavailable'
    assert not (control/'adaptive-experiment.json').exists()


def test_terminal_session_and_command_replay_are_not_restarted(gateway):
    runtime,control,grants,grant=gateway
    experiment=admitted(gateway)
    data=dict(action='measure_here',request_id='point',based_on='fresh')
    route(runtime,'task',data)
    assert route(runtime,'task',data)['status']=='already_submitted'
    with pytest.raises(ValueError,match='conflicts'):
        route(runtime,'task',dict(action='move_and_measure',request_id='point',value=.77,based_on='fresh'))
    atomic_json(experiment/'status.json',dict(state='foreground_ready',active=False,decision_revision='revoked'))
    assert route(runtime,'task',dict(action='cancel',request_id='stop'))['status']=='already_terminal'
    with pytest.raises(ValueError,match='terminal'):
        route(runtime,'task',dict(action='adopt',request_id='late',value=.77,based_on='revoked'))


def test_pi_family_uses_exact_trusted_parameters_and_is_autonomous(gateway):
    runtime,control,grants,grant=gateway
    runtime.jobs['controller']['cards'][0]['id']='steady-lock'
    p=dict(controller_card_id='steady-lock',gain_factors=[.5],window_s=3.,min_samples=3,timeout_s=100.,save_settings=False)
    grant['bounds'].update(families=['current','pi'],pid_parameters=p)
    atomic_json(grants/'monitor.json',grant)
    response=route(runtime,'task',dict(action='begin',family='pi',request_id='pi-begin'))
    selector=json.loads((control/'adaptive-experiment.json').read_text())
    assert response['status']=='submitted' and selector['family']=='pi'
    assert selector['pid_parameters']['gain_factors']==[.5] and not selector['pid_parameters']['save_settings']
    assert 'half_span_v' not in selector
    experiment=control/'adaptive-experiments/task-task/adaptive-window'
    (experiment/'inbox').mkdir(parents=True)
    atomic_json(experiment/'status.json',dict(state='tuning',active=True,decision_revision='fresh'))
    with pytest.raises(ValueError,match='autonomously'):
        route(runtime,'task',dict(action='adopt',value=.7,request_id='not-a-pi-gain',based_on='fresh'))
    assert route(runtime,'task',dict(action='cancel',request_id='stop'))['status']=='submitted'


@pytest.mark.parametrize('failure',['family','persistent','controller'])
def test_pi_requires_explicit_family_runtime_only_and_matching_controller(gateway,failure):
    runtime,control,grants,grant=gateway
    runtime.jobs['controller']['cards'][0]['id']='steady-lock'
    grant['bounds'].update(families=['current','pi'],pid_parameters=dict(controller_card_id='steady-lock',save_settings=False))
    if failure=='family':grant['bounds']['families']=['current']
    elif failure=='persistent':grant['bounds']['pid_parameters']['save_settings']=True
    else:grant['bounds']['pid_parameters']['controller_card_id']='another-controller'
    atomic_json(grants/'monitor.json',grant)
    with pytest.raises(ValueError):route(runtime,'task',dict(action='begin',family='pi',request_id='begin'))
    assert not (control/'adaptive-experiment.json').exists()


def native_grant(gateway):
    runtime,control,grants,grant=gateway
    runtime.jobs['controller']['cards']=[dict(id='native-current-servo',operation='servo.native_current')]
    grant['bounds'].update(families=['pi'],pid_parameters=dict(controller_card_id='native-current-servo',
        strategy='validated_candidates',save_settings=False,window_s=30.,timeout_s=300.))
    atomic_json(grants/'monitor.json',grant)
    return runtime,control


def test_native_typed_candidate_is_frozen_but_not_a_policy_change(gateway):
    runtime,control=native_grant(gateway)
    assert route(runtime,'task')['supported_adjustments']==['pi']
    candidate=dict(kp_count_per_nm=-120.,ki_count_per_nm_s=-19000.,integral_limit_count=30.)
    result=route(runtime,'task',dict(action='begin',family='pi',request_id='candidate',pi_candidate=candidate))
    assert result['status']=='submitted'
    selected=json.loads((control/'adaptive-experiment.json').read_text())
    assert selected['pid_parameters']['candidate']==candidate
    assert selected['pid_parameters']['save_settings'] is False
    with pytest.raises(ValueError,match='changed'):
        route(runtime,'task',dict(action='begin',family='pi',request_id='candidate-2',
            pi_candidate={**candidate,'integral_limit_count':31.}))


@pytest.mark.parametrize('candidate',[None,{},dict(kp_count_per_nm=-100,ki_count_per_nm_s=-20000,integral_limit_count=True),
    dict(kp_count_per_nm=-100,ki_count_per_nm_s=-20000,integral_limit_count=float('inf'))])
def test_native_bad_candidate_cannot_admit(gateway,candidate):
    runtime,control=native_grant(gateway)
    request=dict(action='begin',family='pi',request_id='invalid')
    if candidate is not None:request['pi_candidate']=candidate
    with pytest.raises(ValueError):route(runtime,'task',request)
    assert not (control/'adaptive-experiment.json').exists()


def test_native_does_not_advertise_independent_current_axis(gateway):
    runtime,control=native_grant(gateway)
    with pytest.raises(ValueError,match='does not support'):
        route(runtime,'task',dict(action='begin',family='current',request_id='current'))
    assert not (control/'adaptive-experiment.json').exists()


def parameter_plan():
    return dict(objective='frequency_rms_hz',window_s=20.,min_samples=10,settle_s=5.,max_sample_gap_s=3.,
        timeout_s=300.,minimum_improvement_fraction=.1,maximum_cadence_fraction=.2,minimum_tec_excursion_v=0.)


def test_parameter_family_has_end_to_end_host_selector_and_explicit_capability(gateway,monkeypatch):
    import yaml
    from ldcon.core.adjustment_registry import capabilities
    runtime,control,grants,grant=gateway
    laser=dict(id='laser',devices=dict(rig=dict(type='ecdl.rig.owner',control_settings=dict(background_adjustments=dict(
        slow_loop=dict(enabled=True,bounds=dict(tec_gain=[.01,.2]),max_excursion=dict(current=.001,temperature=.0001)))))))
    (runtime.directory('controller')/'laser.yaml').write_text(yaml.safe_dump(laser))
    # The envelope comes from the rig owner's adopted device (2026-10-28), here the same as the frozen copy.
    monkeypatch.setattr('ldcon.agents.agent_background.owner_describe',lambda value:dict(laser_id='laser',revision='r',
        background_capabilities=capabilities('servo.ecdl_steady_lock',laser['devices']['rig'])))
    grant['bounds'].update(families=['slow_loop'],adjustment_parameters=dict(slow_loop=parameter_plan()))
    atomic_json(grants/'monitor.json',grant)
    assert 'slow_loop' in route(runtime,'task')['supported_adjustments']
    result=route(runtime,'task',dict(action='begin',family='slow_loop',parameter_candidate=dict(tec_gain=.1),request_id='start'))
    assert result['status']=='submitted'
    selector=json.loads((control/'adaptive-experiment.json').read_text())
    assert selector['adjustment_parameters']==dict(parameter_plan(),candidate=dict(tec_gain=.1))
    assert set(selector)=={'id','family','adjustment_parameters','background'}
    experiment=control/'adaptive-experiments/task-task/adaptive-window';(experiment/'inbox').mkdir(parents=True)
    atomic_json(experiment/'status.json',dict(state='measuring',active=True,decision_revision='fresh'))
    with pytest.raises(ValueError,match='autonomously'):
        route(runtime,'task',dict(action='adopt',request_id='bypass',value=.1,based_on='fresh'))
    assert route(runtime,'task',dict(action='cancel',request_id='cancel'))['status']=='submitted'


def test_native_does_not_advertise_nonexistent_tec_ff(gateway):
    runtime,control,grants,grant=gateway
    runtime.jobs['controller']['cards'][0]['operation']='servo.native_current'
    grant['bounds'].update(families=['feedforward'],adjustment_parameters=dict(feedforward=parameter_plan()))
    atomic_json(grants/'monitor.json',grant)
    assert route(runtime,'task')['supported_adjustments']==['pi']
    # Older native-only hosts reject the parameter_candidate schema itself;
    # broader hosts reject the unsupported family. Neither may enqueue work.
    with pytest.raises(ValueError,match='does not support|Invalid background step'):
        route(runtime,'task',dict(action='begin',family='feedforward',parameter_candidate=dict(current_per_temperature=1.),request_id='no'))
    assert not (control/'adaptive-experiment.json').exists()


def test_mode_revision_fences_stale_or_missing_plan_but_not_cleanup(gateway,monkeypatch):
    runtime,control,_,_=gateway
    for expected in (None,True,1):
        with pytest.raises(ValueError,match='Operating mode'):
            raw_route(runtime,'task',dict(action='begin',request_id='stale',expected_operating_mode_revision=expected))
    assert not (control/'adaptive-experiment.json').exists()
    admitted(gateway)
    monkeypatch.setattr('ldcon.agents.agent_background.mode_read',lambda laser:dict(mode='characterization',revision=1))
    with pytest.raises(ValueError,match='after admission'):
        raw_route(runtime,'task',dict(action='move_and_measure',request_id='stale-move',value=.77,based_on='fresh',expected_operating_mode_revision=1))
    def unavailable(*args):raise OSError('Owner mode read unavailable')
    monkeypatch.setattr('ldcon.agents.agent_background.mode_read',unavailable)
    assert raw_route(runtime,'task',dict(action='cancel',request_id='cleanup'))['status']=='submitted'


def test_current_advice_refreshed_without_changing_approved_span_or_move(gateway):
    from types import SimpleNamespace
    runtime,control,grants,grant=gateway
    advice=dict(schema_version=1,knobs=dict(current=dict(kind='absolute',step=.0002,unit='V',reason='Local probe')))
    laser=dict(id='laser')
    from ldcon.agents.adjustment_guidance import location
    path=location(runtime.root,'laser');path.parent.mkdir(parents=True);atomic_json(path,advice)
    runtime.interface_repository=SimpleNamespace(read=lambda *args:dict(document=laser))
    experiment=admitted(gateway)
    before=route(runtime,'task')
    advice['knobs']['current']['step']=.0001
    atomic_json(path,advice)
    after=route(runtime,'task')
    assert before['adjustment_guidance']['revision']!=after['adjustment_guidance']['revision']
    assert after['envelope']==before['envelope']==grant['bounds']
    # Larger than advice is not a gateway refusal. Physical owner validation
    # will still validate the candidate; this test submits no hardware write.
    assert route(runtime,'task',dict(action='move_and_measure',request_id='larger-probe',
                                    value=.775,based_on='fresh'))['status']=='submitted'


def test_ecdl_typed_independent_candidate_is_routed(gateway):
    runtime,control,grants,grant=gateway
    runtime.jobs['controller']['cards']=[dict(id='steady-lock',operation='servo.ecdl_steady_lock')]
    grant['bounds'].update(families=['pi'],pid_parameters=dict(controller_card_id='steady-lock',
        strategy='validated_candidates',save_settings=False,window_s=30.,timeout_s=300.))
    atomic_json(grants/'monitor.json',grant)
    candidate=dict(kp_count_per_nm=2e6,ki_count_per_nm_s=1.1e8,integral_limit_count=20000.)
    result=route(runtime,'task',dict(action='begin',family='pi',request_id='candidate',pi_candidate=candidate))
    assert result['status']=='submitted'
    selected=json.loads((control/'adaptive-experiment.json').read_text())
    assert selected['pid_parameters']['candidate']==candidate
    assert selected['pid_parameters']['save_settings'] is False
