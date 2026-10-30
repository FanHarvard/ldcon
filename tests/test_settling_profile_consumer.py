import copy
import importlib.util
from pathlib import Path

import pytest

from ldcon.jobs.settling_profile import Completion
from ldcon.core.contracts import DeviceError
from ldcon.jobs.learned_settling import fit

spec=importlib.util.spec_from_file_location('empirical_fixture',(Path(__file__).resolve().parent / 'support')/'tests/test_learned_settling.py')
fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)


@pytest.fixture
def consumer(tmp_path):
    trials=fixture.trials();profile=fit(trials,fixture.CONTEXT);leg=trials[-1]
    class History:
        data=leg['history']
        def read(self,*_):return self.data
    history=History()
    return tmp_path,profile,leg,history


def test_final_context_must_still_match_after_database_query(consumer):
    path,profile,leg,history=consumer
    contexts=iter([fixture.CONTEXT,{**fixture.CONTEXT,'gains':[1.,3.,100.]}])
    c=Completion(path,{'id':'test'},leg['target_frequency_hz'],leg['confirmed_unix_s'],
        history=history,record={'revision':'p','profile':profile},context_reader=lambda _:next(contexts))
    with pytest.raises(DeviceError,match='before completion'):
        c.poll(leg['record_end_unix_s'])


def test_temporary_context_loss_is_inconclusive_then_recovers(consumer):
    path,profile,leg,history=consumer;missing=[True]
    def context(_):
        if missing[0]:raise ValueError('temporary missing owner input')
        return fixture.CONTEXT
    c=Completion(path,{'id':'test'},leg['target_frequency_hz'],leg['confirmed_unix_s'],
        history=history,record={'revision':'p','profile':profile},context_reader=context)
    assert c.poll(leg['record_end_unix_s']-1)['status']=='inconclusive'
    missing[0]=False
    assert c.poll(leg['record_end_unix_s'])['status']=='settled'


def test_wrong_source_and_out_of_scope_origin_never_complete(consumer):
    path,profile,leg,history=consumer
    with pytest.raises(DeviceError,match='scope'):
        Completion(path,{'id':'test'},leg['target_frequency_hz'],leg['confirmed_unix_s'],origin_hz=300e12+1e9,
            history=history,record={'revision':'p','profile':profile},context_reader=lambda _:fixture.CONTEXT)
    history.data=copy.deepcopy(history.data);history.data['provenance']['host']='other-laser'
    c=Completion(path,{'id':'test'},leg['target_frequency_hz'],leg['confirmed_unix_s'],
        history=history,record={'revision':'p','profile':profile},context_reader=lambda _:fixture.CONTEXT)
    assert c.poll(leg['record_end_unix_s'])['reason']=='optical_history_source_changed'


def test_operating_point_completion_does_not_claim_old_target_after_foreground(monkeypatch,tmp_path):
    from ldcon.jobs import settling_profile as module
    from ldcon.core.interface_cards import CardCancelled
    from types import SimpleNamespace
    class Observer:
        enabled=True
        def __init__(self,*_):pass
        def poll(self,*_):return {'status':'settled'}
    monkeypatch.setattr(module,'Completion',Observer)
    responses=iter([{'revision':'one','document':{'target':{'frequency_hz':300e12}}},
                    {'revision':'one'}, {'revision':'new-human-request'}])
    api=SimpleNamespace(get_configuration=lambda *_:next(responses))
    with pytest.raises(CardCancelled,match='after measurement'):
        module.verify_move(tmp_path,{'id':'test'},300e12,0.,clock=lambda:0.,sleep=lambda _:None,cancel=lambda:False,api=api)
