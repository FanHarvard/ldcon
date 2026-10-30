"""Attached proxy ownership, durable no-replay and main-controller independence."""
from copy import deepcopy

import pytest

from .test_dlcpro_offline import rig
from .test_dlcpro_awg import make,config,request,FakePort
from ldcon.jobs.dlcpro_cards import Protocol
from ldcon.owners.dlcpro_awg_protocol import attach


def prepared(rig,monkeypatch,tmp_path):
    o,_,_=make(rig,monkeypatch)
    device=deepcopy(rig['device']);device['control_settings']['awg_sweep']=config()
    protocol=Protocol(o,device);protocol.client='existing-main-lease'
    attach(protocol,tmp_path,port_factory=FakePort,sleep=rig['clock'].sleep)
    def call(action,**kw):return protocol.dispatch(dict(action='awg_sweep_'+action,laser_id=o.laser_id,**kw))
    return o,protocol,call


def test_attached_lifecycle_does_not_claim_or_renew_main(rig,monkeypatch,tmp_path):
    o,p,call=prepared(rig,monkeypatch,tmp_path)
    last=o.last_client;rig['clock'].sleep(.1)
    row=call('begin',request=request(),token='b'*32,target_revision=o.target_revision)
    assert row['state']=='preparing' and not row['ready'] and row['token']=='b'*32
    assert o.awg_sweep.prepared.wait(2.)
    assert call('status',request_id='a'*32)['ready'] and o.last_client==last
    assert p.client=='existing-main-lease'
    rig['clock'].sleep(.1)
    call('heartbeat',token='b'*32)
    assert o.last_client==last and 'token' not in call('status',request_id='a'*32)
    row=call('cancel',request_id='a'*32,token='b'*32)
    assert row['verdict']=='cancelled' and row['restored']
    full=call('result',request_id='a'*32)
    assert 'token' not in full and full['request']['request_id']=='a'*32
    with pytest.raises(ValueError,match='already recorded'):
        call('begin',request=request(),token='b'*32,target_revision=o.target_revision)


def test_wrong_cancel_token_and_no_main_lease(rig,monkeypatch,tmp_path):
    o,p,call=prepared(rig,monkeypatch,tmp_path)
    p.client=None
    with pytest.raises(PermissionError):call('begin',request=request(),token='b'*32,target_revision=o.target_revision)
    p.client='live';call('begin',request=request(),token='b'*32,target_revision=o.target_revision)
    with pytest.raises(PermissionError):call('cancel',request_id='a'*32,token='c'*32)
    assert o.awg_sweep.active()
    p.dispatch(dict(action='stop',laser_id=o.laser_id,lease=p.client))
    assert o.mode=='idle' and call('status',request_id='a'*32)['pi_resumed'] is False


def test_offline_uncommissioned_configuration_does_not_break_owner(rig,tmp_path):
    d=deepcopy(rig['device']);d['control_settings']['awg_sweep']={'commissioned':False}
    p=Protocol(rig['owner'],d);attach(p,tmp_path)
    state=p.owner.awg_sweep.public()
    assert state['available'] is False and p.owner.fault is None
    with pytest.raises(ValueError):p.owner.awg_sweep.begin({})


def test_unknown_request_is_read_only_none(rig,monkeypatch,tmp_path):
    o,p,call=prepared(rig,monkeypatch,tmp_path)
    assert call('status',request_id='f'*32) is None
    with pytest.raises(ValueError,match='Unknown'):call('result',request_id='f'*32)


def test_failed_trial_preemption_is_not_reported_as_unchanged(rig,monkeypatch,tmp_path):
    o,p,call=prepared(rig,monkeypatch,tmp_path)
    o.pi_trial.active={'pending':True}
    def reject(*args):raise RuntimeError('preemption outcome requires review')
    monkeypatch.setattr(o.pi_trial,'preempt',reject)
    with pytest.raises(RuntimeError):
        call('begin',request=request(),token='b'*32,target_revision=o.target_revision)
    state=call('status',request_id='a'*32)
    assert state['outputs_uncertain'] and not state['restored']


def test_policy_change_blocks_stale_projection(rig,monkeypatch,tmp_path):
    o,p,call=prepared(rig,monkeypatch,tmp_path)
    o.policy['ff_factor_ma_per_v']+=.1
    with pytest.raises(ValueError,match='policy changed'):
        call('begin',request=request(),token='b'*32,target_revision=o.target_revision)
    assert not o.awg_sweep.active()


def test_terminal_result_keeps_cancellation_and_resume_refusal_separate(rig,monkeypatch,tmp_path):
    o,p,call=prepared(rig,monkeypatch,tmp_path)
    call('begin',request=request(),token='b'*32,target_revision=o.target_revision)
    assert o.awg_sweep.prepared.wait(2.)
    rig['source'].bias=1e9
    call('cancel',request_id='a'*32,token='b'*32)
    result=call('result',request_id='a'*32)
    assert result['stop_verdict']=='cancelled' and result['stop_reason']=='Caller cancelled this sweep'
    assert result['pi_resume_reason']=='Sweep ended outside the original PI capture context'
    assert result['restored'] and not result['pi_resumed'] and not result['outputs_uncertain']
    assert call('status',request_id='a'*32)['stop_reason']==result['stop_reason']
