from copy import deepcopy
import json
import math
from pathlib import Path
from types import SimpleNamespace

import pytest
from ldcon.agents.dlc_pi_adjustment import run, metrics, improved, evidence_documents
from ldcon.agents.dlc_pi_plan import freeze
from ldcon.core.records import atomic_json


def plan():
    return dict(changes=[],apply_mode='atomic',measurements={'wavelength':True,'optical_quality':False,'dither':False},
        baseline_s=0.,wait_s=0.,duration_s=12.,history_wait_s=0.,sample_interval_s=.1,minimum_samples=12,
        measurement_max_age_s=2.,max_sample_gap_s=.5,timeout_s=60.,target_frequency_hz=None)


class Clock:
    now=1000.
    def __call__(self):return self.now
    def sleep(self,dt):self.now+=dt


class Adapter:
    def __init__(self,c):
        self.clock=self.wall_clock=c;self.target=3e14;self.origin=dict(kp=.3,ki_per_s=.5,integral_limit_v=1.)
        self.current=deepcopy(self.origin);self.state={};self.receipts=[];self.closed=None;self.identity='test-task'
    def begin(self):pass
    def check(self,**kw):self.state={'control':self.current}
    def preflight(self,changes,mode,measurements):return {'candidate':{x['parameter']:x['value'] for x in changes}}
    def apply(self,plan,sample):
        self.current=deepcopy(plan['candidate']);r=dict(status='applied',readback=self.current);self.receipts.append(r);return r
    def read(self,m):
        self.check();f=self.target+1e6*(self.current['kp']/.3)*math.sin(self.clock()*math.pi)
        return {'wavelength':dict(valid=True,acquired_unix_s=self.clock(),values={'frequency_hz':f})}
    def close(self,adopt=False):
        if not adopt:self.current=deepcopy(self.origin)
        self.closed=adopt;return dict(confirmed=True,state='adopted' if adopt else 'restored',
            settings=deepcopy(self.current),identity=self.identity,target_restored=True)


class Model:
    def __init__(self,c,answers):self.deadline=c()+400;self.profile={'max_turns':10};self.turns=1;self.answers=iter(answers)
    def request(self,payload,tick):
        tick();self.turns+=1
        return json.dumps(next(self.answers))


def setup(tmp_path):
    atomic_json(tmp_path/'invocation.json',{'adjustment_plan':dict(kind='dlc_pi_change_measure',measurement=plan())})
    (tmp_path/'laser.yaml').write_text('id: synthetic\n')


def test_real_host_loop_measures_repeats_then_adopts_only_confirmed_candidate(tmp_path,monkeypatch):
    setup(tmp_path);clock=Clock();adapter=Adapter(clock)
    monkeypatch.setattr('ldcon.agents.dlc_pi_adjustment.export_evidence',lambda *a:(None,[]))
    model=Model(clock,[dict(action='trial',parameters=dict(candidate=dict(kp=.15,ki_per_s=.5,integral_limit_v=1.),reason='Compare P alone')),
                       dict(action='adopt',parameters=dict(trial_id=0,reason='Repeat the lower-error candidate'))])
    pdf=tmp_path/'mock.pdf';pdf.write_bytes(b'offline report fixture')
    out=run(tmp_path,dict(prompt='Reduce oscillation',adjustment_objective='',adoption_mode='auto_within_approved'),
        clock=clock,sleep=clock.sleep,cancel=lambda:False,emit=lambda *a:None,
        selected=(SimpleNamespace(root=tmp_path),{},None,None),decision_session=model,adapter=adapter,report_writer=lambda *a:pdf)
    assert out['adopted'] and adapter.closed and adapter.current['kp']==.15
    assert [r['role'] for r in out['measurements']]==['baseline','candidate','matched_baseline','confirmation','matched_baseline']
    assert len(out['trials'])==2 and all(t['eligible'] for t in out['trials'])
    assert all((tmp_path/r['path']).is_file() for r in out['measurements'])
    docs=evidence_documents(tmp_path,out,adapter.state,model.turns)
    assert all(len(json.dumps(value).encode())<65536 for _,value in docs)


def test_rejected_candidate_cannot_be_adopted_and_cleans_checkpoint(tmp_path,monkeypatch):
    setup(tmp_path);clock=Clock();adapter=Adapter(clock)
    monkeypatch.setattr('ldcon.agents.dlc_pi_adjustment.export_evidence',lambda *a:(None,[]))
    invalid=dict(action='trial',parameters=dict(candidate=dict(kp=-2,ki_per_s=.5,integral_limit_v=1.),reason='invalid'))
    model=Model(clock,[invalid]*3)
    with pytest.raises(ValueError,match='Three invalid'):
        run(tmp_path,dict(prompt='',adjustment_objective='',adoption_mode='auto_within_approved'),clock=clock,sleep=clock.sleep,
            cancel=lambda:False,emit=lambda *a:None,selected=(SimpleNamespace(root=tmp_path),{},None,None),
            decision_session=model,adapter=adapter,report_writer=lambda *a: (_ for _ in ()).throw(RuntimeError('no report fixture')))
    assert adapter.closed is False and adapter.current==adapter.origin
    assert len(adapter.receipts)==1  # baseline only; no invalid writes


def test_plan_and_physical_field_rejection():
    laser={'interfaces':{'dlc_lock':{'device':'dlc'}},'devices':{'dlc':{'type':'dlcpro.laser.owner'}}}
    deck={'cards':[dict(id='pi-observation',operation='measure.change',parameters=plan())]}
    p=dict(adjustment_family='pi',monitor_job_id='',request_delegation=True,adjustment_plan_card_id='pi-observation')
    assert freeze(laser,deck,p,{})['measurement']['duration_s']==12
    deck['cards'][0]['parameters']['changes']=[dict(parameter='temperature',value=20)]
    with pytest.raises(ValueError):freeze(laser,deck,p,{})


def test_preemption_at_adoption_is_never_reported_as_adopted(tmp_path,monkeypatch):
    from ldcon.core.interface_cards import CardCancelled
    setup(tmp_path);clock=Clock();adapter=Adapter(clock)
    original_close=adapter.close
    def preempted(adopt=False):return original_close(False)
    adapter.close=preempted
    monkeypatch.setattr('ldcon.agents.dlc_pi_adjustment.export_evidence',lambda *a:(None,[]))
    model=Model(clock,[dict(action='trial',parameters=dict(candidate=dict(kp=.15,ki_per_s=.5,integral_limit_v=1.),reason='compare')),
                       dict(action='adopt',parameters=dict(trial_id=0,reason='confirm'))])
    with pytest.raises(CardCancelled,match='preempted'):
        run(tmp_path,dict(prompt='',adjustment_objective='',adoption_mode='auto_within_approved'),
            clock=clock,sleep=clock.sleep,cancel=lambda:False,emit=lambda *a:None,
            selected=(SimpleNamespace(root=tmp_path),{},None,None),decision_session=model,adapter=adapter,
            report_writer=lambda *a: (_ for _ in ()).throw(RuntimeError('no report fixture')))
    result=json.loads((tmp_path/'pi-adjustment.json').read_text())
    assert not result['adopted'] and result['cleanup']['state']=='restored'
    assert adapter.current==adapter.origin


class JointAdapter(Adapter):
    def __init__(self,clock):
        super().__init__(clock);self.program_cycle=0;self.program_index=0;self.target_program=None
    def preflight(self,changes,mode,measurements):
        if changes and changes[0]['parameter']=='target_frequency_hz':
            return dict(target=self.target+changes[0]['offset'])
        return super().preflight(changes,mode,measurements)
    def apply(self,plan,sample):
        if 'target' not in plan:return super().apply(plan,sample)
        delta=plan['target']-self.target;self.target=plan['target']
        self.program_index+=1
        if self.program_index==2:self.program_index=0;self.program_cycle+=1
        return dict(status='applied',readback={'target_frequency_hz':self.target},
            owner_result=dict(delta_frequency_hz=delta,started_unix_s=self.clock(),confirmed_unix_s=self.clock()))


def test_joint_model_sees_repeat_and_can_choose_higher_rms(tmp_path,monkeypatch):
    clock=Clock();adapter=JointAdapter(clock)
    steps=[plan()]
    for offset in (100e6,-100e6):
        q=plan();q.update(changes=[dict(parameter='target_frequency_hz',offset=offset)],duration_s=6.)
        steps.append(q)
    steps.append(plan())
    atomic_json(tmp_path/'invocation.json',dict(adjustment_plan=dict(kind='dlc_pi_target_response',steps=steps,
        timeout_s=60.,target_frequency_hz=adapter.target,target_program=dict(offsets_hz=[100e6,0],max_cycles=12))))
    (tmp_path/'laser.yaml').write_text('id: synthetic\n')
    monkeypatch.setattr('ldcon.agents.dlc_pi_adjustment.export_evidence',lambda *a:(None,[]))
    model=Model(clock,[dict(action='trial',parameters=dict(candidate=dict(kp=.33,ki_per_s=.5,integral_limit_v=1.),reason='Compare tradeoff')),
        dict(action='adopt',parameters=dict(trial_id=0,reason='Request repeat')),
        dict(action='adopt',parameters=dict(trial_id=1,reason='Reviewed confirmation; accept tradeoff'))])
    pdf=tmp_path/'mock.pdf';pdf.write_bytes(b'offline fixture')
    result=run(tmp_path,dict(prompt='',adjustment_objective='',adoption_mode='auto_within_approved'),
        clock=clock,sleep=clock.sleep,cancel=lambda:False,emit=lambda *a:None,
        selected=(SimpleNamespace(root=tmp_path),{},None,None),decision_session=model,
        adapter=adapter,report_writer=lambda *a:pdf)
    assert result['adopted'] and model.turns==4
    assert len(result['trials'])==2 and result['trials'][1]['confirmation_of']==0
    assert result['measurements'][1]['metrics']['rms_hz']>result['measurements'][0]['metrics']['rms_hz']
    assert adapter.current['kp']==.33 and adapter.target==3e14 and adapter.program_cycle==5
