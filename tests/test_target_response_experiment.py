"""No-I/O experiment integration: goals, admission and bounded resident choices."""
import copy
from types import SimpleNamespace
import pytest
from ldcon.jobs import target_response as study
from ldcon.jobs import target_response_experiment as experiment


def fixture(tmp_path,monkeypatch,times,answers=()):
    clock=SimpleNamespace(now=0.)
    calls=[];decisions=[];cursor=iter(times);answer_iter=iter(answers)
    p=study.parameters({},dict(offsets_hz=[100e6,0.],optimization_goal='settling_and_feedforward',execution_mode='agent'))
    result=dict(parameters=p,trials=[],laser_id='test',feedforward_context={})
    class Context:
        ff_deadline=None
        def check(self,**_):
            if self.ff_deadline is not None and clock.now>=self.ff_deadline:raise study.CardCancelled('deadline')
        def policy_request(self,action,**values):
            calls.append((action,values))
            return dict(active=False,maximum_step_hz=100e6,authorized_scale_bounds=[.5,2.],
                        defaults=dict(scale=1.,step_hz=100e6),session={},revision='r')
    def measure(index):
        clock.now+=20
        value=next(cursor)
        legs=[dict(index=len(result['trials'])+i,direction=d,history={'integrity_errors':[]},
            analysis=dict(status='inconclusive' if value is None else 'settled',settling_from_request_s=value))
              for i,d in enumerate(('out','back'))]
        result['trials'].extend(legs)
        return legs
    def decision(*a,**kw):
        decisions.append(kw)
        return next(answer_iter)
    monkeypatch.setattr('ldcon.jobs.study_execution.evaluate',decision)
    monkeypatch.setattr('ldcon.jobs.target_response_decisions.evidence',lambda result:[])
    monkeypatch.setattr(experiment.ff,'identify',lambda _:dict(status='static_candidate_only',target_feedforward_scale=1.2,
        trials=[dict(status='static_estimate',scale=1.2,uncertainty_scale=.01)]*2))
    kwargs=dict(measure_round=measure,clock=lambda:clock.now,sleep=lambda s:None,cancel=lambda:False,publish=lambda _:None)
    return result,Context(),kwargs,calls,decisions,clock


def choice(scale=1.2,step=100e6):
    return dict(action='next_trial',parameters=dict(scale=scale,step_hz=step,reason='Measured response'))


def test_successful_largest_step_stops_without_smaller_or_model_trials(tmp_path,monkeypatch):
    r,c,k,calls,decisions,_=fixture(tmp_path,monkeypatch,[2.])
    out=experiment.run(tmp_path,r,c,(),**k)
    assert out['action']=='select' and out['parameters']['step_hz']==100e6
    assert len([x for x in calls if x[0]=='set'])==1 and not decisions
    assert out['applied'] is False
    assert out['criteria']['mean_error_max_hz']==r['parameters']['mean_error_max_hz']


def test_resident_compares_ff_at_100_before_decreasing_step(tmp_path,monkeypatch):
    r,c,k,calls,decisions,_=fixture(tmp_path,monkeypatch,[4.,2.5],[choice()])
    out=experiment.run(tmp_path,r,c,(),**k)
    assert out['action']=='select' and out['parameters']==dict(scale=1.2,step_hz=100e6)
    assert len(decisions)==1
    assert [v['scale'] for a,v in calls if a=='set']==[1.,1.2]


def test_model_cannot_shrink_before_supported_ff_comparison(tmp_path,monkeypatch):
    r,c,k,calls,_,_=fixture(tmp_path,monkeypatch,[4.],[choice(step=50e6)])
    with pytest.raises(ValueError,match='Premature'):experiment.run(tmp_path,r,c,(),**k)
    assert len([x for x in calls if x[0]=='set'])==1


def test_gap_gets_exact_repeat_but_no_changed_coefficient(tmp_path,monkeypatch):
    r,c,k,calls,_,_=fixture(tmp_path,monkeypatch,[None,2.],[choice(scale=1.)])
    out=experiment.run(tmp_path,r,c,(),**k)
    assert out['action']=='select' and len([x for x in calls if x[0]=='set'])==2
    second=tmp_path/'other';second.mkdir()
    r,c,k,calls,_,_=fixture(second,monkeypatch,[None],[choice(scale=1.2)])
    with pytest.raises(ValueError,match='unchanged'):experiment.run(second,r,c,(),**k)


def test_declared_budget_reserves_return_before_admission(tmp_path,monkeypatch):
    r,c,k,calls,_,_=fixture(tmp_path,monkeypatch,[])
    r['parameters']['ff_budget_s']=300
    out=experiment.run(tmp_path,r,c,(),**k)
    assert out['action']=='inconclusive' and not [x for x in calls if x[0]=='set']
