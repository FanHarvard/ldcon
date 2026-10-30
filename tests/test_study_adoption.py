import copy
import threading
from types import SimpleNamespace

import pytest
import yaml
from experiment_core.records import atomic_json, digest
from ldcon.jobs import study_adoption as m
from ldcon.jobs.target_response_decisions import FIELDS


@pytest.fixture
def adoption(tmp_path,monkeypatch):
    root=tmp_path/'job';root.mkdir()
    profile=dict(expected_settle_s=3.,evaluation_window_s=1.,check_interval_s=.1,confirmation_s=2.,
        mean_error_max_hz=1e7,rms_error_max_hz=1e7,peak_error_max_hz=2.5e7,minimum_samples=10,max_gap_s=.2)
    before={'id':'laser','laser_id':'laser','decks':[{'deck_id':'deck','parameters':{'response':{'adoption_mode':'auto_within_approved'}}}]}
    frozen=copy.deepcopy(before);(root/'set.yaml').write_text(yaml.safe_dump(frozen))
    invocation={'card_id':'response','parameters':{'adoption_mode':'auto_within_approved'}}
    atomic_json(root/'invocation.json',invocation)
    result={'parameters':{**profile,'peak_error_max_hz':None,'adoption_mode':'auto_within_approved'},
        'measurement_status':'succeeded','restoration':'starting_target_confirmed','laser_id':'laser',
        'final_configuration_revision':'laser-rev'}
    atomic_json(root/'target-response.json',result);atomic_json(root/'study-decision.json',{'proposal':{'test':'original'}})
    candidate={'status':'candidate_only','all_trials_settled':True,'parameters':profile,
        'feedforward':{'target_feedforward_scale':1.2}}
    monkeypatch.setattr('ldcon.jobs.target_response_decisions.validate_proposal',lambda *a:copy.deepcopy(candidate))
    job={'id':'job','set_id':'laser','deck_id':'deck','status':'running','cards':[{'operation':'optimize.settling_criteria'}]}
    records={'lasers':{'revision':'laser-rev','document':{}},'assignments':{'revision':'assignment-rev','document':before}}
    calls=[]
    runtime=SimpleNamespace(mutex=threading.RLock(),get_job=lambda _:job,directory=lambda _:root,
        interface_repository=SimpleNamespace(read=lambda k,n:copy.deepcopy(records[k])))
    def save(rt,kind,name,text,rev,**kw):
        after=yaml.safe_load(text)
        rows=m.authorize_criteria(rt,kind,name,records[kind]['document'],after,
            [{'path':'decks.0.parameters.response.peak_error_max_hz','class':'safety'}],kw['study_criteria_binding'])
        assert rows[0]['class']=='free'
        calls.append(after);records[kind]={'document':after,'revision':'new'}
        return {'revision':'new'}
    monkeypatch.setattr('ldcon.core.configuration.save',save)
    return SimpleNamespace(root=root,runtime=runtime,result=result,candidate=candidate,records=records,
        job=job,calls=calls,value={'result_revision':digest(result)})


def test_auto_saves_only_next_study_criteria_and_does_not_adopt_ff(adoption):
    a=adoption;r=m.apply(a.runtime,'job',a.value)
    assert r['applied'] and r['scope']=='next_response_study_defaults_only'
    assert r['feedforward']=='requires_separate_persistent_adoption_authority'
    assert 'target_feedforward_scale' not in str(a.calls)
    assert m.apply(a.runtime,'job',a.value)==r and len(a.calls)==1


@pytest.mark.parametrize('change',['new_target','cancel','precision','assignment'])
def test_stale_or_weakened_criteria_never_save(adoption,change):
    a=adoption
    if change=='new_target':a.records['lasers']['revision']='new'
    if change=='cancel':(a.root/'cancel.request').write_text('stop')
    if change=='precision':a.candidate['parameters']['rms_error_max_hz']=2e7
    if change=='assignment':a.records['assignments']['document']['name']='Concurrent edit'
    if change=='precision':assert m.apply(a.runtime,'job',a.value)['status']=='requires_human_approval'
    else:
        with pytest.raises(ValueError):m.apply(a.runtime,'job',a.value)
    assert not a.calls


def test_lost_save_reply_is_not_retried(adoption,monkeypatch):
    a=adoption
    def save(*args,**kwargs):a.calls.append('sent');raise OSError('lost reply')
    monkeypatch.setattr('ldcon.core.configuration.save',save)
    with pytest.raises(OSError):m.apply(a.runtime,'job',a.value)
    assert not m.apply(a.runtime,'job',a.value)['applied'] and a.calls==['sent']


def test_receipt_reports_the_card_side_shift_promotion(adoption):
    """target-ff design (2026-10-27): the per-step cap is saved by the card (configuration API, FREE keys); the host
    receipt only reports that outcome next to the criteria adoption."""
    a=adoption
    a.result['shift']=dict(adoption=dict(status='applied'),adopted_hz=250e6,best_passing_hz=250e6,audit=[dict(basis='trial'),dict(basis='adopted')])
    atomic_json(a.root/'target-response.json',a.result)
    r=m.apply(a.runtime,'job',{'result_revision':digest(a.result)})
    assert r['applied'] and r['shift']==dict(status='applied',adopted_hz=250e6,best_passing_hz=250e6,
        audit=[dict(basis='trial'),dict(basis='adopted')],basis='card_configuration_api')
    assert 'target_ramp_step_hz' not in str(a.calls)      # the assignment save carries criteria only
