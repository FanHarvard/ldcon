"""Offline optimizer contracts and finite orchestration, with no device access."""
import copy
import json
from unittest.mock import patch

import pytest
import yaml

from ldcon.jobs import sweep_ff_optimization as workflow
from ldcon.jobs.sweep_ff_plan import parameters, readiness, condition_metadata, child_parameters


def plan():
    return dict(sweep_card_id='frequency-staircase',
        candidates=[dict(id='baseline',ldc_ff_v_per_hz=0.,pzt_ff_gain_scale=1.),
                    dict(id='paired',ldc_ff_v_per_hz=-1e-12,pzt_ff_gain_scale=1.1)],
        baseline_candidate_id='baseline',
        conditions=[dict(id='fast',levels_hz=[-20e6,0.,20e6,0.],dwell_s=1.,
                         edge_s=0.,lead_s=1.,tail_s=1.,repetitions=2,precondition_s=2.)],
        criteria=dict(error_tolerance_hz=1e6,confirmation_s=.2,min_samples=3,
                      max_sample_gap_s=.1,max_late_drift_hz_per_s=1e6,
                      hop_jump_hz=200e6,hop_confirmation_s=.2,
                      require_optical_quality=False,max_quality_age_s=.5),
        max_baseline_shift_hz=1e6,confirmation_runs=1,max_trials=5,
        timeout_s=1200.,poll_s=.1,cleanup_timeout_s=120.,analysis_wait_s=60.,
        sweep_parameters=dict(sample_rate_hz=20000.,sample_interval_s=.1,
            max_sample_age_s=1.,duration_s=120.,ready={},recentre_before_ff=False,full_data_log_ms=0))


def laser_deck():
    laser=dict(id='example',allow_output=True,interfaces={'servo_modulation':{'device':'rig'}},
        devices={'rig':dict(type='ecdl.rig.owner',control_settings={'locked_sweep':{
            'revision':'example','awg':{'channel':2},'ext_mod':{},'firmware':{},
            'ldc_ff':{'max_abs_v_per_hz':1e-10},'limits':{}}})})
    deck=dict(id='example-deck',cards=[dict(id='frequency-staircase',operation='sweep.servo_modulation',
        uses=['servo_modulation'],parameters={})])
    return laser,deck


def test_explicit_parameters_and_joint_pairs():
    p=plan()
    assert parameters({'parameters':p},{})==p
    for key in p:
        bad=copy.deepcopy(p);del bad[key]
        with pytest.raises(ValueError):parameters({'parameters':bad},{})
    bad=copy.deepcopy(p);bad['candidates'][1].pop('pzt_ff_gain_scale')
    with pytest.raises(ValueError):parameters({'parameters':bad},{})
    for key,value in [('dwell_s',0),('repetitions',True),('edge_s',2.)]:
        bad=copy.deepcopy(p);bad['conditions'][0][key]=value
        with pytest.raises(ValueError):parameters({'parameters':bad},{})


def test_dwell_period_and_full_repeated_waveform_are_distinct():
    p=plan();c=p['conditions'][0]
    m=condition_metadata(c)
    assert m['cycle_period_s']==4 and m['cycle_frequency_hz']==.25 and m['step_rate_hz']==1
    assert m['record_s']==10 and len(m['levels_hz'])==8
    child=child_parameters(p,c,p['candidates'][1])
    assert child['levels_hz']==m['levels_hz'] and child['trigger']=='auto'
    assert child['ldc_ff_v_per_hz']==-1e-12 and child['pzt_ff_gain_scale']==1.1


def test_unplayed_wrap_does_not_make_one_direction_bidirectional():
    p=plan();p['conditions'][0].update(levels_hz=[-20e6,0.,20e6],repetitions=1)
    with pytest.raises(ValueError,match='both travel directions'):
        parameters({'parameters':p},{})


def test_native_compiler_honors_explicit_centre_holds():
    from ldcon.jobs.native_ff_proxy import plan_from_card
    from ldcon.jobs.native_ff_sweep import compile_wave
    p=plan();condition=p['conditions'][0];condition['edge_s']=.01
    candidate=dict(id='native',ff_gain_scale=1.1)
    child=child_parameters(p,condition,candidate)
    compiled_plan=plan_from_card(child,None)
    assert compiled_plan['lead_s']==condition['lead_s'] and compiled_plan['tail_s']==condition['tail_s']
    wave=compile_wave(compiled_plan,1e9,sample_rate_hz=20000.)
    assert wave['duration_s']==condition_metadata(condition)['record_s']
    assert wave['lead_s']==1. and wave['tail_s']==1.
    assert not any(wave['codes'][:20000])


def test_route_scope_and_existing_owner_ff_envelope():
    p=plan();laser,deck=laser_deck()
    card=dict(operation='optimize.sweep_feedforward',parameters=p,uses=[])
    assert readiness(laser,deck,card,{})['runnable']
    laser['devices']['rig']['control_settings']['locked_sweep']['ldc_ff']['max_abs_v_per_hz']=1e-13
    assert not readiness(laser,deck,card,{})['runnable']
    laser['devices']['rig']['type']='toptica.dlcpro.owner'
    assert not readiness(laser,deck,card,{})['runnable']


class FakeExperiment:
    def __init__(self,*,bad_restoration=False):self.sent=[];self.waits=[];self.bad=bad_restoration
    def check(self):pass
    def wait(self,duration):self.waits.append(duration)
    def run(self,p):
        self.sent.append(copy.deepcopy(p))
        return dict(child_id=str(len(self.sent)),analysis={'points':{}},owner_outcome=dict(
            restored=not self.bad,outputs_uncertain=self.bad,playback_completed=True,
            cancelled=False,censored=False,mode_hop=False))


def evaluation(doc,p):
    baseline=doc['trial']['candidate']['id']=='baseline'
    return dict(qualified=True,data_valid=True,reasons=[],visits=[],
                frequency_continuity={'status':'continuous'},worst_settling_s=.6 if baseline else .2,
                rms_error_hz=100.,mean_error_hz=0.,max_late_drift_hz_per_s=0.)


def run(tmp_path,p=None,experiment=None,evaluate=evaluation,
        emit=lambda *_:None,publish=lambda *_:None):
    laser,deck=laser_deck()
    (tmp_path/'interface-deck.yaml').write_text(yaml.safe_dump(deck))
    def pdf(*_):
        path=tmp_path/'synthetic.pdf';path.write_bytes(b'offline test');return path
    with patch.object(workflow,'evaluate_trial',evaluate):
        result=workflow.run_card(tmp_path,laser,{'id':'sweep-ff'},p or plan(),clock=lambda:0.,
            sleep=lambda _:None,cancel=lambda:False,emit=emit,publish=publish,
            experiment=experiment or FakeExperiment(),report_writer=pdf)
    return result,json.loads((tmp_path/'sweep-ff-optimization.json').read_text())


def test_baselines_bracket_repeated_candidates_and_only_recommend(tmp_path):
    experiment=FakeExperiment();short,full=run(tmp_path,experiment=experiment)
    assert short['status']=='succeeded' and short['applied'] is False
    assert [x['role'] for x in full['trials']]==['baseline','candidate','baseline','candidate','baseline']
    assert len(experiment.sent)==5 and experiment.waits==[2.]*5
    best=[r for r in full['recommendations'] if r['pareto']]
    assert [r['candidate']['id'] for r in best]==['paired']
    assert all((tmp_path/t['analysis_file']).exists() for t in full['trials'])


def test_baseline_can_win_without_forcing_a_worse_change(tmp_path):
    def worse(doc,p):
        e=evaluation(doc,p)
        if doc['trial']['candidate']['id']=='paired':e['worst_settling_s']=.8
        return e
    _,full=run(tmp_path,evaluate=worse)
    best=[r for r in full['recommendations'] if r['pareto']]
    assert [r['candidate']['id'] for r in best]==['baseline']
    assert best[0]['application']=='retain baseline'


def test_drift_invalidates_apparent_improvement(tmp_path):
    count=0
    def drifting(doc,p):
        nonlocal count
        count+=1;e=evaluation(doc,p);e['mean_error_hz']=count*2e6;return e
    short,full=run(tmp_path,evaluate=drifting)
    assert short['status']=='inconclusive' and not full['recommendations']


def test_uncertain_restore_and_mode_hop_never_run_another_shot(tmp_path):
    from ldcon.core.contracts import DeviceError
    exp=FakeExperiment(bad_restoration=True)
    with pytest.raises(DeviceError,match='restoration'):run(tmp_path,experiment=exp)
    full=json.loads((tmp_path/'sweep-ff-optimization.json').read_text())
    assert full['status']=='failed' and len(exp.sent)==1
    assert json.loads((tmp_path/'device-review.json').read_text())['outputs_uncertain']


def test_uncertain_restoration_precedes_analysis_and_cancelled_publication(tmp_path):
    from ldcon.core.contracts import DeviceError
    from ldcon.jobs.progress import CancelledRun
    def evaluate(*_):
        raise AssertionError('Uncertain output must be recorded before evaluation')
    def emit(*_):
        raise CancelledRun()
    def publish(*_):
        if (tmp_path/'device-review.json').exists():raise CancelledRun()
    with pytest.raises(DeviceError,match='restoration'):
        run(tmp_path,experiment=FakeExperiment(bad_restoration=True),
            evaluate=evaluate,emit=emit,publish=publish)
    full=json.loads((tmp_path/'sweep-ff-optimization.json').read_text())
    assert full['status']=='failed'
    assert full['physical_outcome']=='child_output_or_restoration_unconfirmed'
    assert full['trials'][0]['evaluation']['qualified'] is False


def test_budget_retains_evidence_but_no_incomplete_confirmation(tmp_path):
    p=plan();p['max_trials']=2
    short,full=run(tmp_path,p)
    assert short['status']=='inconclusive' and len(full['trials'])==2
    assert not full['recommendations']


def test_cancelled_child_is_not_a_successful_or_inconclusive_optimization(tmp_path):
    from ldcon.core.interface_cards import CardCancelled
    class Cancelled(FakeExperiment):
        def run(self,p):
            value=super().run(p);value['owner_outcome']['cancelled']=True;return value
    exp=Cancelled()
    with pytest.raises(CardCancelled):run(tmp_path,experiment=exp)
    full=json.loads((tmp_path/'sweep-ff-optimization.json').read_text())
    assert full['status']=='cancelled' and len(exp.sent)==1
    assert full['trials'][0]['owner_outcome']['cancelled']


def test_censored_result_stops_even_with_good_mocked_metrics(tmp_path):
    class Censored(FakeExperiment):
        def run(self,p):
            value=super().run(p);value['owner_outcome']['censored']=True;return value
    exp=Censored();short,_=run(tmp_path,experiment=exp)
    assert short['status']=='inconclusive' and len(exp.sent)==1


def test_registration_and_no_direct_instrument_opening():
    from ldcon.core.operation_registry import spec,admission_class,normalize_parameters
    from ldcon.jobs.interface_devices import open_interfaces
    from ldcon.core.interface_cards import INTERFACE_CATALOG
    card=dict(id='sweep-ff',operation='optimize.sweep_feedforward',parameters=plan(),uses=[])
    laser,_=laser_deck()
    assert spec(card['operation']).execution=='job'
    assert admission_class(laser,card)=='proxy'
    assert normalize_parameters(card,{})==plan()
    assert open_interfaces(laser,card)=={}
    assert card['operation'] in INTERFACE_CATALOG
