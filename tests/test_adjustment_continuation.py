"""Real task-session lifecycle with a fake owner; no transports or hardware."""
import hashlib
import json

import pytest

from ldcon.jobs.adaptive_window_session import AdaptiveWindowSession
from ldcon.core.contracts import MeasurementUnavailable
from .test_background_window_session import BackgroundLock,BACKGROUND,finish_point
from .test_adaptive_window_session import command,result


class RecoverableLock(BackgroundLock):
    def context(self):
        return dict(self.owner_context,lease=hashlib.sha256(self.session.lease.encode()).hexdigest(),
                    target=self.target,mode_id='branch')

    def __init__(self):
        super().__init__();self.target=710e12;self.pi=True;self.fault=None

    def response_info(self,**kwargs):
        row=super().response_info(**kwargs)
        row.update(native_pi_active=self.pi,target_frequency_hz=self.target)
        return row

    def background_status(self):return dict(self.bg.status(),owner_fault=self.fault,native_pi_active=self.pi)

    def background_begin(self,**kwargs):
        self.active=True
        epoch=self.bg.begin(kwargs['expected_generation'],self.context(),{'current':self.current,'temperature':.5},
                            kwargs['handoff_timeout_s'],kwargs['identity'])
        return dict(started=True,epoch=epoch,minimum_dwell_s=35.5)

    def background_current(self,token,value):
        self.bg.validate(token,self.context());return self.response_current(value)

    def background_restore(self,token,reason):
        self.bg.returning(reason);epoch=self.bg.validate(token,self.context(),restoring=True)
        if self.current!=epoch['checkpoint']['current']:
            self.response_current(epoch['checkpoint']['current']);return self.bg.status()
        self.active=False
        return self.bg.complete(token,self.context(),dict(confirmed=True,response_ended=True,current=self.current))

    def background_adopt(self,token):
        self.active=False
        return self.bg.complete(token,self.context(),dict(confirmed=True,response_ended=True,current=self.current),adopted=True)

    def recovered(self,current=.77):
        self.current=current;self.bg.epoch=None;self.active=False;self.t+=1


def make(tmp_path,**kwargs):
    lock=RecoverableLock()
    session=AdaptiveWindowSession(tmp_path,lock,half_span_v=.0084,background=BACKGROUND,
        continuation=True,clock=lambda:lock.t,wall_clock=lambda:lock.t,**kwargs)
    return lock,session


def status(session):return json.loads((session.directory/'status.json').read_text())


def test_recovered_hop_returns_evidence_and_preserves_task_budget_then_adopts(tmp_path):
    lock,s=make(tmp_path)
    command(s,'old-move','move_and_measure',value=.771)
    deadline,origin,points=s.deadline,s.origin,s.points
    ticket=s.before_recovery()
    assert result(s,'old-move')['state']=='branch_lost'
    assert status(s)['state']=='waiting_recovery' and not s.blocked
    lock.recovered();s.after_recovery(ticket)
    assert status(s)['state']=='awaiting_replan'
    command(s,'replan','replan')
    assert s.segment==1 and (s.deadline,s.origin,s.points)==(deadline,origin,points)
    # Neither an old successful point nor a prior decision is adoption proof.
    command(s,'premature','adopt',value=.771)
    assert result(s,'premature')['state']=='rejected' and lock.current==.77
    command(s,'fresh-baseline','measure_here');finish_point(lock,s)
    command(s,'adopt','adopt',value=.771);finish_point(lock,s)
    assert result(s,'adopt')['state']=='adopted' and lock.current==.771
    assert s.blocked and s.journal.get('session')['state']!='running'
    assert (s.directory/'segment-000-outcome.json').exists()
    assert (s.directory/'segment-001-start.json').exists()
    assert s.before_recovery() is None  # unrelated later hop cannot revive it


def terminal_parent_arm(lock, session):
    import copy
    session.preempt('epoch_completed')
    session.blocked=True
    session._status('reconciliation_required',reason='Background checkpoint context changed')
    lock.bg.last=dict(copy.deepcopy(session.epoch),state='superseded_by_parent_arm')
    lock.bg.epoch=None
    lock.active=False


def test_parent_arm_clears_terminal_active_flag_without_replay_or_revival(tmp_path):
    from ldcon.jobs.adaptive_window_session import AdaptiveExperimentManager
    lock,s=make(tmp_path);command(s,'probe','move_and_measure',value=.771)
    terminal_parent_arm(lock,s)
    assert s.before_recovery() is None
    manager=object.__new__(AdaptiveExperimentManager)
    manager.session=s;manager.owner_pending=False
    assert manager.active
    manager.after_recovery(None)
    assert not manager.active and s.blocked and s.epoch is None and s.returning is None
    assert lock.moves==[.771]  # No historical checkpoint move.
    assert status(s)['state']=='cancelled'
    assert s.journal.get('session')['state']=='cancelled'
    assert result(s,'probe')['disposition']=='superseded_by_parent_arm'
    assert s.before_recovery() is None and not s.reconcile_parent_arm()


@pytest.mark.parametrize('damage',['token','state','context','identity','generation','live_epoch','owner_fault','unknown','uncertain','pi','lease'])
def test_terminal_parent_cleanup_requires_exact_owner_receipt(tmp_path,damage):
    lock,s=make(tmp_path);command(s,'probe','move_and_measure',value=.771)
    terminal_parent_arm(lock,s)
    if damage=='token':lock.bg.last['token']='another-epoch'
    elif damage=='state':lock.bg.last['state']='restored'
    elif damage=='context':lock.bg.last['context']['target']+=20e6
    elif damage=='identity':lock.bg.last['identity']['generation']='another-task'
    elif damage=='generation':lock.bg.last['generation']+=1
    elif damage=='live_epoch':lock.bg.epoch=dict(s.epoch)
    elif damage=='owner_fault':lock.fault='unverified output'
    elif damage=='unknown':s.write_unknown=True
    elif damage=='pi':lock.pi=False
    elif damage=='lease':lock.session.lease='another-lease'
    else:lock.uncertain_output=True
    assert not s.reconcile_parent_arm()
    assert s.active and s.blocked and lock.moves==[.771]
    assert status(s)['state']=='reconciliation_required'


@pytest.mark.parametrize('error',[OSError('temporary read'),MeasurementUnavailable('temporary sample')])
def test_terminal_cleanup_retries_read_only_proof_from_foreground(tmp_path,error):
    from ldcon.jobs.adaptive_window_session import AdaptiveExperimentManager
    lock,s=make(tmp_path);command(s,'probe','move_and_measure',value=.771)
    terminal_parent_arm(lock,s)
    normal=lock.background_status
    lock.background_status=lambda:(_ for _ in ()).throw(error)
    assert not s.reconcile_parent_arm() and s.active and s.blocked
    lock.background_status=normal
    manager=object.__new__(AdaptiveExperimentManager)
    manager.lock=lock;manager.session=s;manager.owner_pending=False
    manager.reservations=set();manager.dispatched=set()
    manager.foreground_ids=lambda:set()
    manager._background_enabled=lambda _:False
    assert manager.foreground() and not manager.active and s.blocked
    assert lock.moves==[.771]


def test_terminal_cleanup_finalizes_correlated_cancel_and_preserves_measurement(tmp_path):
    lock,s=make(tmp_path);command(s,'probe','move_and_measure',value=.771)
    command(s,'stop','cancel')
    assert result(s,'stop')['state']=='stopping_restoring'
    s.pending_measurement={'partial_frames':['measured-before-hop']}
    terminal_parent_arm(lock,s)
    assert s.reconcile_parent_arm()
    assert result(s,'stop')['state']=='cancelled'
    assert result(s,'stop')['disposition']=='superseded_by_parent_arm'
    assert s.cancel_command is None and lock.moves==[.771]
    proof=json.loads((s.directory/'terminal-parent-arm-reconciliation.json').read_text())
    assert proof['interrupted_evidence']['partial_frames']==['measured-before-hop']


@pytest.mark.parametrize('stop',['cancelled','foreground','background_disabled','parent_stopped'])
def test_stop_during_recovery_wins_over_late_success(tmp_path,stop):
    lock,s=make(tmp_path);command(s,'probe','move_and_measure',value=.771)
    ticket=s.before_recovery();s.close(stop);lock.recovered();s.after_recovery(ticket)
    assert s.blocked and status(s)['state']==stop
    assert s.before_recovery() is None
    assert lock.moves==[.771]


@pytest.mark.parametrize('damage',['target','lease','policy','generation','reservation','fault','outside','uncertain'])
def test_recovery_cannot_create_authority_after_context_or_owner_change(tmp_path,damage):
    lock,s=make(tmp_path);command(s,'probe','move_and_measure',value=.771)
    ticket=s.before_recovery();lock.recovered()
    if damage=='target':lock.target+=20e6
    elif damage=='lease':lock.session.lease='new-lease'
    elif damage=='policy':lock.owner_context['policy_revision']='new'
    elif damage=='generation':lock.bg.generation+=1
    elif damage=='reservation':lock.bg.reserve(['foreground'])
    elif damage=='fault':lock.fault='unverified hardware'
    elif damage=='outside':lock.current=.79
    else:lock.uncertain_output=True
    s.after_recovery(ticket)
    assert s.blocked and status(s)['state']=='context_changed'
    assert s.segment==0 and lock.moves==[.771]


def test_recovery_time_counts_and_old_ticket_does_not_complete_second_recovery(tmp_path):
    lock,s=make(tmp_path,timeout_s=100)
    command(s,'first','measure_here');first=s.before_recovery();lock.recovered();s.after_recovery(first)
    command(s,'plan-1','replan');command(s,'second','measure_here')
    second=s.before_recovery();lock.recovered();s.after_recovery(first)
    assert status(s)['state']=='waiting_recovery'
    lock.t=201;s.after_recovery(second)
    assert s.blocked and status(s)['state']=='experiment_timeout'
    assert s.points==2 and s.segment==1 and s.deadline==200


def test_point_timeout_uses_same_protocol_without_hop_or_recovery(tmp_path):
    lock,s=make(tmp_path,point_timeout_s=3)
    command(s,'point','move_and_measure',value=.771)
    for _ in range(7):lock.t+=1;s.poll()
    assert status(s)['state']=='awaiting_replan' and lock.current==.77
    assert result(s,'point')['disposition']=='checkpoint_restored'
    command(s,'replan','replan')
    assert s.segment==1 and s.points==1


def test_recovered_context_waits_for_pi_and_freshness_without_moving(tmp_path):
    lock,s=make(tmp_path);command(s,'point','measure_here')
    ticket=s.before_recovery();lock.recovered();lock.pi=False;s.after_recovery(ticket)
    assert status(s)['state']=='waiting_recovery' and not s.blocked
    lock.pi=True;s.poll()
    assert status(s)['state']=='awaiting_replan' and not lock.moves


def test_stale_replan_does_not_act_or_cancel_ready_task(tmp_path):
    lock,s=make(tmp_path);command(s,'point','measure_here');old=s.decision_revision
    ticket=s.before_recovery();lock.recovered();s.after_recovery(ticket)
    path=s.directory/'inbox'/'stale.json'
    path.write_text(json.dumps(dict(id='stale',action='replan',based_on=old)))
    s.poll()
    assert status(s)['state']=='awaiting_replan' and s.segment==0 and not s.blocked
    command(s,'current','replan');assert s.segment==1


def test_idle_completed_measurement_can_replan_but_terminal_cancel_cannot(tmp_path):
    lock,s=make(tmp_path);command(s,'baseline','measure_here');finish_point(lock,s)
    assert not s.active and not s.blocked
    ticket=s.before_recovery();lock.recovered();s.after_recovery(ticket)
    command(s,'stop','cancel')
    assert status(s)['state']=='cancelled' and s.blocked
    assert s.before_recovery() is None


def test_second_recovery_invalidates_ready_ticket_and_queued_replan(tmp_path):
    lock,s=make(tmp_path);command(s,'point','measure_here')
    first=s.before_recovery();lock.recovered();s.after_recovery(first)
    old=s.decision_revision
    (s.directory/'inbox'/'old.json').write_text(json.dumps(dict(id='old',action='replan',based_on=old)))
    second=s.before_recovery()
    assert second!=first and status(s)['state']=='waiting_recovery'
    lock.recovered();s.after_recovery(first)
    assert status(s)['state']=='waiting_recovery'
    s.after_recovery(second);s.poll()
    assert s.segment==0 and not s.blocked
    command(s,'new','replan');assert s.segment==1


@pytest.mark.parametrize('reason',['epoch_completed','cancelled','foreground','background_disabled'])
def test_hop_during_restoration_never_reuses_abandoned_epoch(tmp_path,reason):
    lock,s=make(tmp_path);command(s,'point','move_and_measure',value=.771)
    s.preempt(reason);ticket=s.before_recovery()
    assert not s.active and s.epoch is None and s.returning is None
    lock.recovered();s.after_recovery(ticket);s.check_deadlines()
    assert lock.moves==[.771]
    if reason=='epoch_completed':assert status(s)['state']=='awaiting_replan'
    else:assert s.blocked and ticket is None


@pytest.mark.parametrize('text',['{','[]','{"id":"another","action":"replan"}'])
def test_malformed_continuation_only_stops_optional_task(tmp_path,text):
    lock,s=make(tmp_path);command(s,'point','measure_here')
    ticket=s.before_recovery();lock.recovered();s.after_recovery(ticket)
    (s.directory/'inbox'/'bad.json').write_text(text)
    s.poll()
    assert s.blocked and status(s)['state']=='invalid_command' and lock.pi and not lock.moves


def test_freshness_loss_at_replan_admission_retains_metadata_without_replay(tmp_path):
    lock,s=make(tmp_path);command(s,'point','measure_here')
    ticket=s.before_recovery();lock.recovered();s.after_recovery(ticket)
    original=s._fresh_continuation_context;calls=[]
    def proof():
        calls.append(1)
        if len(calls)==2:raise MeasurementUnavailable('Transient new observation unavailable')
        return original()
    s._fresh_continuation_context=proof
    command(s,'plan','replan')
    assert (s.directory/'inbox'/'plan.json').exists() and s.segment==0
    s.poll()
    assert s.segment==1 and result(s,'plan')['state']=='replanned' and not lock.moves


def test_generation_change_between_reads_cannot_replan(tmp_path):
    lock,s=make(tmp_path);command(s,'point','measure_here')
    ticket=s.before_recovery();lock.recovered()
    original=lock.response_info
    def raced(**kwargs):
        row=original(**kwargs);lock.bg.generation+=1;return row
    lock.response_info=raced;s.after_recovery(ticket)
    assert s.blocked and status(s)['state']=='context_changed' and not lock.moves


def test_generation_changed_after_replan_prevents_next_epoch(tmp_path):
    lock,s=make(tmp_path);command(s,'point','measure_here')
    ticket=s.before_recovery();lock.recovered();s.after_recovery(ticket)
    command(s,'plan','replan');lock.bg.generation+=1
    command(s,'next-baseline','measure_here')
    assert not s.active and s.blocked and not lock.moves


def test_revocation_side_effect_in_probe_cannot_revive_task(tmp_path):
    lock,s=make(tmp_path);command(s,'point','measure_here')
    ticket=s.before_recovery();lock.recovered()
    def revoke():s.close('background_disabled');return False
    s.foreground_probe=revoke;s.after_recovery(ticket)
    assert s.blocked and status(s)['state']=='background_disabled' and not lock.moves


def test_interrupted_partial_samples_are_preserved_before_replanning(tmp_path):
    lock,s=make(tmp_path);command(s,'point','measure_here')
    s.frames=[dict(raw_pattern=[1,2,3],sample='incomplete')]
    ticket=s.before_recovery();lock.recovered();s.after_recovery(ticket)
    command(s,'plan','replan')
    old=json.loads((s.directory/'segment-000-outcome.json').read_text())
    assert old['partial_evidence']['frames']==[dict(raw_pattern=[1,2,3],sample='incomplete')]
    assert not s.frames
