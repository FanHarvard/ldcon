"""Study policy queue scope, canonical target limits and no replay."""
import copy
import threading
from types import SimpleNamespace

import pytest
import yaml

from ldcon.jobs import target_response_control as control
from ldcon.core.contracts import DeviceError
from ldcon.core.records import atomic_json, read_json

STUDY='a'*32
PARENT='b'*32
OTHER='c'*32


class Port:
    def __init__(self):
        self.calls=[]
        self.value=dict(revision='owner:0',active=False,session=None)
    def target_ff_study_status(self):
        self.calls.append('status')
        return copy.deepcopy(self.value)
    def target_ff_study_set(self,revision,**values):
        self.calls.append('set')
        assert revision==self.value['revision']
        self.value=dict(revision='owner:1',active=True,session=dict(study_token='d'*32,scale=values['scale']))
        return copy.deepcopy(self.value)
    def target_ff_study_restore(self,revision,token):
        self.calls.append('restore')
        assert revision==self.value['revision'] and token==self.value['session']['study_token']
        self.value=dict(revision='owner:2',active=False,session=None)
        return copy.deepcopy(self.value)


@pytest.fixture
def rig(tmp_path,monkeypatch):
    monkeypatch.setattr(control.time,'time',lambda:100.)
    root=tmp_path/'jobs'/'laser';root.mkdir(parents=True)
    documents={}
    for ident,operation in ((STUDY,'optimize.target_feedforward'),(PARENT,'servo.native_current')):
        directory=root/ident;directory.mkdir()
        meta=dict(id=ident,status='running',laser=dict(id='laser'),cards=[dict(operation=operation)])
        atomic_json(directory/'job.json',meta)
        atomic_json(directory/'progress.json',dict(status='running'))
        documents[ident]=meta
    def get_job(ident):
        result=copy.deepcopy(documents[ident])
        if (root/ident/'cancel.request').exists():result['status']='stopping'
        return result
    laser=dict(id='laser',allow_output=True,target=dict(frequency_hz=300e12,
                bounds_hz=[299e12,301e12],max_change_hz=100e6))
    runtime=SimpleNamespace(mutex=threading.RLock(),directory=lambda ident:root/ident,
        get_job=get_job,active_jobs=lambda:[get_job(ident) for ident in documents],
        interface_repository=SimpleNamespace(read=lambda *_:dict(revision='laser:0',document=copy.deepcopy(laser))))
    port=Port()
    live=SimpleNamespace(directory=root/PARENT,laser=dict(id='laser'),revision='laser:0',lock=port,port=None)
    return runtime,live,port,documents,laser


def policy(rig,value):
    runtime,live,port,_,_=rig
    control.request_policy(runtime,STUDY,value)
    control.poll_policy(live,False)
    return read_json(runtime.directory(STUDY)/'target-ff-policy.json',{})


def set_policy(rig):
    receipt=policy(rig,dict(action='status'))
    return policy(rig,dict(action='set',expected_revision=receipt['result']['revision'],
                           scale=1.,step_hz=100e6,duration_s=120.))


def request_binding(receipt):
    return dict(job_id=PARENT,laser_id='laser',revision_before='laser:0',revision_after='laser:1',
        changes=[dict(path='target.frequency_hz')],authority=dict(target_response_binding=dict(
            study_id=STUDY,parent_id=PARENT,study_token=receipt['result']['session']['study_token'])))


def test_policy_queue_is_memory_only_and_target_binding_is_exact(rig):
    receipt=set_policy(rig)
    runtime,live,port,_,_=rig
    assert receipt['status']=='completed' and receipt['hardware_write'] is False
    assert port.calls==['status','set']
    assert control.bound_token(live,request_binding(receipt))=='d'*32
    assert control.bound_token(live,dict(authority={})) is None
    for change in (dict(job_id=OTHER),dict(laser_id='other'),dict(revision_before='stale'),
                   dict(changes=[dict(path='devices.native.calibration')])):
        with pytest.raises(ValueError,match='superseded'):
            control.bound_token(live,{**request_binding(receipt),**change})


@pytest.mark.parametrize('payload',[
    {},dict(action='other'),dict(action='status',scale=1.),
    dict(action='set',expected_revision='x',scale=True,step_hz=100e6,duration_s=1.),
    dict(action='set',expected_revision='x',scale=1.,step_hz=float('nan'),duration_s=1.),
    dict(action='set',expected_revision='x',scale=1.,step_hz=100e6,duration_s=901.),
])
def test_invalid_request_is_rejected_before_queue(rig,payload):
    runtime,live,port,_,_=rig
    with pytest.raises(ValueError):control.request_policy(runtime,STUDY,payload)
    assert not list(live.directory.glob('target-ff-policy-request-*.json')) and not port.calls


def test_save_target_checks_maximum_step_and_only_changes_target(rig,monkeypatch):
    runtime,live,port,_,laser=rig
    set_policy(rig)
    writes=[]
    monkeypatch.setattr('ldcon.core.configuration.save',lambda *a,**kw:writes.append((a,kw)) or {'revision':'laser:1'})
    candidate=copy.deepcopy(laser);candidate['target']['frequency_hz']+=100e6+1
    with pytest.raises(DeviceError,match='exceeds the cap'):
        control.save_target(runtime,STUDY,dict(yaml=yaml.safe_dump(candidate),expected_revision='laser:0'))
    candidate['target']['frequency_hz']=laser['target']['frequency_hz']+100e6
    candidate['allow_output']=False
    with pytest.raises(ValueError,match='one canonical'):
        control.save_target(runtime,STUDY,dict(yaml=yaml.safe_dump(candidate),expected_revision='laser:0'))
    candidate['allow_output']=True
    control.save_target(runtime,STUDY,dict(yaml=yaml.safe_dump(candidate),expected_revision='laser:0'))
    assert len(writes)==1 and writes[0][1]['target_response_binding']['study_id']==STUDY


def test_policy_set_is_claimed_before_rpc_and_never_replayed_after_crash(rig,monkeypatch):
    runtime,live,port,_,_=rig
    status=policy(rig,dict(action='status'))
    calls=[]
    def crash(*args,**kwargs):
        calls.append('set')
        assert not list(live.directory.glob('target-ff-policy-request-*.json'))
        assert list(live.directory.glob('target-ff-policy-claimed-*.json'))
        raise KeyboardInterrupt('simulated worker loss')
    monkeypatch.setattr(port,'target_ff_study_set',crash)
    request=dict(action='set',expected_revision=status['result']['revision'],scale=1.,step_hz=100e6,duration_s=120.)
    control.request_policy(runtime,STUDY,request)
    with pytest.raises(KeyboardInterrupt):control.poll_policy(live,False)
    control.poll_policy(live,False)
    assert calls==['set']
    with pytest.raises(ValueError,match='unconfirmed'):control.request_policy(runtime,STUDY,request)


def test_unknown_memory_rpc_remains_unconfirmed_without_replay(rig,monkeypatch):
    runtime,live,port,_,_=rig
    status=policy(rig,dict(action='status'))
    def lost(*args,**kwargs):raise OSError('lost response')
    monkeypatch.setattr(port,'target_ff_study_set',lost)
    result=policy(rig,dict(action='set',expected_revision=status['result']['revision'],scale=1.,step_hz=100e6,duration_s=120.))
    assert result['status']=='unknown' and list(live.directory.glob('target-ff-policy-claimed-*.json'))
    control.poll_policy(live,False)
    assert read_json(runtime.directory(STUDY)/'target-ff-policy.json',{})==result


def test_cancellation_allows_memory_cleanup_but_not_target_return(rig):
    runtime,live,port,_,laser=rig
    receipt=set_policy(rig)
    (runtime.directory(STUDY)/'cancel.request').touch()
    with pytest.raises(ValueError,match='cancellation'):
        control.bound_token(live,request_binding(receipt))
    candidate=copy.deepcopy(laser);candidate['target']['frequency_hz']+=1e6
    with pytest.raises(ValueError,match='running'):
        control.save_target(runtime,STUDY,dict(yaml=yaml.safe_dump(candidate),expected_revision='laser:0'))
    result=policy(rig,dict(action='restore',expected_revision=receipt['result']['revision']))
    assert result['status']=='completed' and port.calls[-1]=='restore'


def test_cross_laser_study_cannot_control_parent_or_overwrite_its_evidence(rig):
    runtime,live,port,documents,_=rig
    control.request_policy(runtime,STUDY,dict(action='status'))
    meta=copy.deepcopy(documents[STUDY]);meta['laser']['id']='other'
    atomic_json(runtime.directory(STUDY)/'job.json',meta)
    control.poll_policy(live,False)
    assert not port.calls and not (runtime.directory(STUDY)/'target-ff-policy.json').exists()
    assert list(live.directory.glob('target-ff-policy-rejected-*.json'))


def test_bound_token_rejects_path_traversal_and_symlink(rig,tmp_path):
    runtime,live,port,_,_=rig
    receipt=set_policy(rig);request=request_binding(receipt)
    request['authority']['target_response_binding']['study_id']='../../elsewhere'
    with pytest.raises(ValueError,match='identity'):control.bound_token(live,request)
    request=request_binding(receipt)
    policy_file=runtime.directory(STUDY)/'target-ff-policy.json'
    copy_file=tmp_path/'copy.json';copy_file.write_text(policy_file.read_text())
    policy_file.unlink();policy_file.symlink_to(copy_file)
    with pytest.raises(ValueError,match='symlinks'):control.bound_token(live,request)


def test_expired_or_cancelled_queued_set_has_no_owner_call(rig):
    runtime,live,port,_,_=rig
    status=policy(rig,dict(action='status'))
    control.request_policy(runtime,STUDY,dict(action='set',expected_revision=status['result']['revision'],
        scale=1.,step_hz=100e6,duration_s=120.))
    path=next(live.directory.glob('target-ff-policy-request-*.json'))
    request=read_json(path,{})
    request['expires_unix_s']=99.
    atomic_json(path,request)
    control.poll_policy(live,False)
    assert port.calls==['status']
    assert read_json(runtime.directory(STUDY)/'target-ff-policy.json',{})['status']=='rejected'


def test_another_study_cannot_borrow_active_owner_policy(rig):
    runtime,live,port,documents,_=rig
    documents[OTHER]={**copy.deepcopy(documents[STUDY]),'id':OTHER}
    with pytest.raises(ValueError,match='Unambiguous active'):
        control.request_policy(runtime,STUDY,dict(action='status'))
    assert not port.calls
