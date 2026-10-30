"""Background lifecycle and real stationary queue priority, with no transports."""
import json

import pytest

from experiment_core.background import BackgroundCoordinator
from ldcon.jobs.adaptive_window_session import AdaptiveExperimentManager, AdaptiveWindowSession
from ldcon.core.contracts import DeviceError
from ldcon.core.instrument_client import BrokerError
from .test_adaptive_window_session import Lock, command, result


BACKGROUND=dict(monitor_id='monitor',generation='generation',handoff_timeout_s=10.,grant_revision='approved')


class BackgroundLock(Lock):
    def __init__(self):
        super().__init__()
        self.bg=BackgroundCoordinator(lambda:self.t)
        self.holds=[]
        self.cleanups=[]

    def background_status(self):return self.bg.status()
    def background_reserve(self,request_ids):return self.bg.reserve(request_ids)
    def background_release(self,request_ids):return self.bg.release(request_ids)

    def background_begin(self,**kwargs):
        self.active=True
        epoch=self.bg.begin(kwargs['expected_generation'],self.owner_context,{'current':self.current},
                            kwargs['handoff_timeout_s'],kwargs['identity'])
        return dict(started=True,epoch=epoch,minimum_dwell_s=35.5)

    def background_current(self,token,value):
        self.bg.validate(token,self.owner_context)
        return self.response_current(value)

    def background_restore(self,token,reason):
        self.bg.returning(reason)
        epoch=self.bg.validate(token,self.owner_context,restoring=True)
        if self.current!=epoch['checkpoint']['current']:
            self.response_current(epoch['checkpoint']['current'])
            return self.bg.status()
        self.active=False
        self.cleanups.append(self.t)
        return self.bg.complete(token,self.owner_context,dict(confirmed=True,response_ended=True,current=self.current))

    def background_adopt(self,token):
        receipt=self.bg.complete(token,self.owner_context,dict(confirmed=True,response_ended=True,current=self.current),adopted=True)
        self.active=False
        return receipt

    def background_abandon(self,reason):
        self.bg.block(reason);self.active=False
        return self.bg.status()


def make(tmp_path):
    lock=BackgroundLock()
    session=AdaptiveWindowSession(tmp_path,lock,half_span_v=.0084,background=BACKGROUND,
        clock=lambda:lock.t,wall_clock=lambda:lock.t)
    return lock,session


def finish_point(lock,session):
    for _ in range(40):
        lock.t+=1
        session.poll()


def test_each_measurement_epoch_returns_before_model_wait_and_new_epoch_is_fresh(tmp_path):
    lock,session=make(tmp_path)
    command(session,'first','move_and_measure',value=.771)
    finish_point(lock,session)
    evidence=result(session,'first')
    assert evidence['outputs']['current']==.771
    assert evidence['disposition']=='checkpoint_restored'
    assert lock.current==.77 and not lock.active and not session.active
    assert lock.moves==[.771,.77]
    # Deterministic control is free to evolve between epochs. It becomes the
    # next checkpoint, not a stale frozen origin from a model conversation.
    lock.current=.7702
    command(session,'second','move_and_measure',value=.772)
    assert session.epoch['checkpoint']['current']==.7702
    finish_point(lock,session)
    assert lock.current==.7702 and lock.moves==[.771,.77,.772,.7702]


def test_preemption_abandons_settle_and_never_waits_for_dither(tmp_path):
    lock,session=make(tmp_path)
    command(session,'trial','move_and_measure',value=.771)
    lock.stale=True
    lock.background_reserve(['shot'])
    for _ in range(2):
        lock.t+=1
        session.check_deadlines()
    assert session.blocked and not session.active and lock.current==.77
    assert result(session,'trial')['state']=='foreground'
    assert result(session,'trial')['restoration']['last']['completed_at']<session.started+session.minimum_dwell
    assert not lock.holds


def test_adoption_preserves_new_baseline_without_return_replay(tmp_path):
    lock,session=make(tmp_path)
    command(session,'candidate','adopt',value=.771)
    finish_point(lock,session)
    assert result(session,'candidate')['state']=='adopted'
    assert lock.current==.771 and lock.moves==[.771]
    assert not lock.active and session.blocked


@pytest.mark.parametrize('value,expected',[(.77,False),(.771,True)])
def test_baseline_event_requires_owner_confirmed_net_current_change(tmp_path,value,expected):
    lock,session=make(tmp_path)
    events=[];session.emit=lambda kind,payload:events.append((kind,payload))
    command(session,'candidate','adopt',value=value)
    finish_point(lock,session)
    assert result(session,'candidate')['state']=='adopted'
    baseline=[payload for kind,payload in events if kind=='quality_adjustment_accepted']
    assert bool(baseline) is expected
    if expected:
        assert baseline[0]['action_id']==lock.bg.last['token']
        assert lock.bg.last['receipt']['current']!=lock.bg.last['checkpoint']['current']


def test_cancel_waits_for_confirmed_return_and_preserves_lock(tmp_path):
    lock,session=make(tmp_path)
    command(session,'trial','move_and_measure',value=.771)
    command(session,'stop','cancel')
    assert result(session,'stop')['state']=='stopping_restoring'
    for _ in range(2):lock.t+=1;session.poll()
    assert result(session,'stop')['state']=='cancelled'
    assert result(session,'trial')['disposition']=='checkpoint_restored'
    assert lock.moves==[.771,.77] and not lock.holds


def test_unknown_return_is_never_replayed_and_restart_requires_reconciliation(tmp_path):
    lock,session=make(tmp_path)
    command(session,'trial','move_and_measure',value=.771)
    lock.fail=True
    session.preempt('foreground')
    with pytest.raises(DeviceError):session.check_deadlines()
    count=len(lock.moves)
    again=AdaptiveWindowSession(tmp_path,lock,half_span_v=.0084,background=BACKGROUND,
        clock=lambda:lock.t,wall_clock=lambda:lock.t)
    again.poll()
    assert again.blocked and len(lock.moves)==count


def test_lease_loss_is_fatal_and_never_becomes_a_measurement_retry(tmp_path):
    lock,session=make(tmp_path)
    command(session,'trial','move_and_measure',value=.771)
    session.preempt('foreground')
    attempts=[]
    def lost(*args):
        attempts.append(args)
        raise BrokerError('ownership','Lease lost')
    lock.background_restore=lost
    with pytest.raises(BrokerError,match='Lease lost'):session.check_deadlines()
    session.check_deadlines()
    assert session.write_unknown and len(attempts)==1


def test_rejected_cleanup_is_bounded_even_without_owner_progress(tmp_path):
    lock,session=make(tmp_path)
    command(session,'trial','move_and_measure',value=.771)
    session.preempt('background_disabled')
    def refused(*args):raise BrokerError('configuration','Owner cannot currently confirm cleanup')
    lock.background_restore=refused
    for _ in range(12):lock.t+=1;session.check_deadlines()
    status=json.loads((session.directory/'status.json').read_text())
    assert session.blocked and status['state']=='reconciliation_required'
    assert session.active and lock.current==.771 and not lock.holds


def manager_fixture(tmp_path,monkeypatch):
    lock=BackgroundLock()
    host=tmp_path/'host';host.mkdir()
    job=tmp_path/'deployment/jobs/laser/job';job.mkdir(parents=True)
    monkeypatch.setenv('LDCON_ADAPTIVE_ROOT',str(host))
    manager=AdaptiveExperimentManager(job,lock,laser_id='laser',clock=lambda:lock.t,wall_clock=lambda:lock.t)
    (manager.directory/'adaptive-experiment.json').write_text(json.dumps(dict(id='trial',half_span_v=.0084,background=BACKGROUND)))
    return lock,job,manager


def enable(manager):
    (manager.directory/'background-control.json').write_text(json.dumps(dict(enabled=True,monitor_id='monitor',generation='generation')))
    path=manager.directory.parent/'monitors/monitor.json';path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(dict(enabled=True,generation='generation',parameters_hash='approved',
                                   bounds=dict(enabled=True),last_seen=manager.kwargs['wall_clock'](),max_age_s=180.)))


def test_background_requires_explicit_enable_and_disable_restores_without_lock_stop(tmp_path,monkeypatch):
    lock,job,manager=manager_fixture(tmp_path,monkeypatch)
    manager.poll()
    assert manager.session is None
    enable(manager);manager.poll()
    command(manager.session,'trial','move_and_measure',value=.771)
    (manager.directory/'background-control.json').write_text(json.dumps(dict(enabled=False,monitor_id='monitor',generation='generation')))
    for _ in range(2):lock.t+=1;manager.check_deadlines()
    assert manager.session.blocked and not manager.active and lock.current==.77
    assert result(manager.session,'trial')['state']=='background_disabled' and not lock.holds


@pytest.mark.parametrize('change',['expired','revised','missing'])
def test_worker_revokes_stale_or_changed_monitor_grant_independently_of_model(tmp_path,monkeypatch,change):
    lock,job,manager=manager_fixture(tmp_path,monkeypatch)
    enable(manager);manager.poll()
    command(manager.session,'trial','move_and_measure',value=.771)
    path=manager.directory.parent/'monitors/monitor.json'
    grant=json.loads(path.read_text())
    if change=='missing':path.unlink()
    else:
        if change=='expired':grant['last_seen']=lock.t-181
        else:grant['parameters_hash']='narrower-approved-configuration'
        path.write_text(json.dumps(grant))
    for _ in range(2):lock.t+=1;manager.check_deadlines()
    assert not manager.active and manager.session.blocked and lock.current==.77
    assert not lock.holds


def test_reenabled_old_monitor_generation_cannot_revive_closed_intervention(tmp_path,monkeypatch):
    lock,job,manager=manager_fixture(tmp_path,monkeypatch)
    enable(manager);manager.poll()
    command(manager.session,'trial','move_and_measure',value=.771)
    control=manager.directory/'background-control.json'
    control.write_text(json.dumps(dict(enabled=False,monitor_id='monitor',generation='generation')))
    for _ in range(2):lock.t+=1;manager.check_deadlines()
    before=list(lock.moves)
    enable(manager)
    command(manager.session,'late','move_and_measure',value=.772)
    again=AdaptiveExperimentManager(job,lock,laser_id='laser',clock=lambda:lock.t,wall_clock=lambda:lock.t)
    again.poll()
    assert again.session.blocked and lock.moves==before and not again.active


def test_restarted_journal_cannot_claim_foreground_ready_with_old_owner_epoch(tmp_path,monkeypatch):
    lock,job,manager=manager_fixture(tmp_path,monkeypatch)
    enable(manager);manager.poll()
    command(manager.session,'trial','move_and_measure',value=.771)
    # Simulate worker journal reopening without claiming that an old effect
    # completed. The owner still owns its interrupted epoch.
    again=AdaptiveExperimentManager(job,lock,laser_id='laser',clock=lambda:lock.t,wall_clock=lambda:lock.t)
    again.poll()
    pending=job/'ff-requests/pending/shot.json';pending.parent.mkdir(parents=True);pending.write_text('{}')
    assert again.foreground() is False and again.active
    assert pending.exists() and lock.moves==[.771]


@pytest.mark.parametrize('path',[
    'operating-point-requests/pending/move.json','ff-requests/pending/shot.json',
    'runtime-parameter-request-abc.json','laser-reload-request-abc.json',
    'online-request.json','hot-control-request.json'])
def test_all_foreground_queues_reserve_without_consuming_request(tmp_path,monkeypatch,path):
    lock,job,manager=manager_fixture(tmp_path,monkeypatch)
    enable(manager);manager.poll()
    command(manager.session,'trial','move_and_measure',value=.771)
    pending=job/path;pending.parent.mkdir(parents=True,exist_ok=True);pending.write_text('{}')
    assert manager.foreground() is False
    assert path in lock.bg.reservations and pending.exists()
    for _ in range(2):lock.t+=1;manager.check_deadlines()
    assert manager.foreground() is True and pending.exists()
    assert lock.current==.77
    pending.unlink();manager.foreground()
    assert not lock.bg.reservations


def test_foreground_arrival_at_last_prewrite_check_prevents_outward_effect(tmp_path,monkeypatch):
    lock,job,manager=manager_fixture(tmp_path,monkeypatch)
    enable(manager);manager.poll()
    original=lock.background_begin
    def arrival(**kwargs):
        receipt=original(**kwargs)
        pending=job/'ff-requests/pending/shot.json';pending.parent.mkdir(parents=True);pending.write_text('{}')
        return receipt
    lock.background_begin=arrival
    command(manager.session,'trial','move_and_measure',value=.771)
    assert lock.moves==[]
    lock.t+=1;manager.check_deadlines()
    assert not manager.active and manager.session.blocked


@pytest.mark.parametrize('operation',['point','target','sweep'])
@pytest.mark.parametrize('adjusting',[False,True])
def test_real_stationary_loop_defers_original_queue_until_confirmed_restoration(tmp_path,monkeypatch,operation,adjusting):
    from ldcon.jobs.ecdl_steady import parameters,run_steady_lock
    lock,job,manager=manager_fixture(tmp_path,monkeypatch)
    enable(manager)
    monkeypatch.setattr('ldcon.jobs.adaptive_window_session.AdaptiveExperimentManager',lambda *a,**k:manager)
    lock.check_ready=lambda *a:None
    lock.arm=lambda *a:None
    heartbeat=[]
    lock.heartbeat=lambda:heartbeat.append(lock.t)
    lock.hold=lambda reason:lock.holds.append(reason) or {'held':True}
    adjusted=[]
    lock.adjust=lambda action:adjusted.append(lock.t) or {'applied':True}
    lock.read_cycle=lambda:dict(state='locked',frequency_hz=710e12,error_hz=0.,single_mode=True,
        outputs={'current':lock.current},actions=[dict(axis='temperature',reason='slow control')])
    original=[];submitted=set()
    relative={'point':'operating-point-requests/pending/original.json',
              'target':'laser-reload-request-original.json',
              'sweep':'ff-requests/pending/original.json'}[operation]
    pending=job/relative
    pending.parent.mkdir(parents=True,exist_ok=True)
    def accepted(request):
        assert lock.current==.77 and not lock.active
        if adjusting:assert lock.cleanups and lock.cleanups[-1]<=lock.t
        else:assert not lock.cleanups
        original.append(request['request_id'])
        return dict(request_id=request['request_id'],status='succeeded')
    def foreground(queue,*args,**kwargs):
        request=queue.receive()
        if request is None:return None
        assert kwargs['busy'] is False
        return accepted(request)
    monkeypatch.setattr('ldcon.jobs.operating_point.lock_action',foreground)
    def attached(*args,**kwargs):
        if not pending.exists():return None
        assert not kwargs.get('online_busy',kwargs.get('busy',False))
        request=json.loads(pending.read_text());pending.unlink()
        return accepted(request)
    class Runtime:
        live=None
        def poll(self,**kwargs):return attached(**kwargs)
    def sleep(seconds):
        lock.t+=seconds
        if adjusting and manager.session and 'trial' not in submitted:
            submitted.add('trial');command(manager.session,'trial','move_and_measure',value=.771)
        elif (manager.active or not adjusting) and 'foreground' not in submitted:
            submitted.add('foreground')
            pending.write_text(json.dumps(dict(request_id='original')))
    p=parameters({'parameters':{}},dict(target_frequency_hz=710e12,tolerance_hz=1e6,duration_s=8.))
    run_steady_lock(lock,p,clock=lambda:lock.t,sleep=sleep,cancel=lambda:False,emit=lambda *a:None,
                    directory=job,laser={'id':'laser'},
                    sweep=attached if operation=='sweep' else None,
                    runtime_factory=(lambda *a:Runtime()) if operation=='target' else None)
    assert original==['original'] and len(heartbeat)>=8
    assert lock.moves==([.771,.77] if adjusting else []) and lock.holds==['card exit']
    assert not pending.exists()
