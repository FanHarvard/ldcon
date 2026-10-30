import copy
import math
import pytest
from ldcon.jobs import target_response as study
from ldcon.jobs.target_response_feedforward import identify
from ldcon.jobs.target_response_decisions import FIELDS,validate_proposal
from ldcon.jobs.target_response_analysis import C_NM


def measured():
    p=study.parameters({},{});p['offsets_hz']=[100e6,0.];p['optimization_goal']='settling_and_feedforward'
    base=700e12;slope=-5e-8;delta=(C_NM/(base+100e6)-C_NM/base)/slope
    cfg=dict(pid_multiplier=1,pid_kp=1,pid_ki=1,pid_kd=0,pid_integral_min=-1,pid_integral_max=1,
        multiplier_control=0,multiplier_target=0,target=C_NM/base,control_offset=30000)
    control=dict(status='observed',configuration=cfg,outputs={})
    trials=[]
    for i,(old,new,a,b) in enumerate(((base,base+100e6,30000,30000+delta*1.1),(base+100e6,base,30000+delta*1.1,30000))):
        rows=[]
        for j in range(321):
            t=-2+j*.05;settled=t>=.2
            rows.append(dict(time_unix_s=100+t,received_unix_s=100+t,wavelength_nm=C_NM/(new if settled else old),
                             dac_count=(b if settled else a)+.1*math.sin(j)))
        trials.append(dict(index=i,previous_target_frequency_hz=old,target_frequency_hz=new,
            requested_unix_s=100.,confirmed_unix_s=100.1,record_end_unix_s=114.,
            analysis=dict(status='settled'),history=dict(samples=rows,integrity_errors=[]),
            control_before=copy.deepcopy(control),control_after=copy.deepcopy(control)))
    r=dict(parameters=p,trials=trials,feedforward_context=dict(status='configured',enabled=True,wavelength_nm_per_count=slope))
    r['feedforward_identification']=identify(r)
    return r


def test_default_jump_resolves_from_current_request_limit_without_mutation():
    p=study.parameters({},{});assert p['offsets_hz'] is None
    laser=dict(allow_output=True,target=dict(frequency_hz=700e12,max_change_hz=100e6,bounds_hz=[699e12,701e12]))
    r=study.resolved_parameters(laser,p)
    assert r['offsets_hz']==[100e6,0.] and p['offsets_hz'] is None
    assert study.validate_laser(laser,p)==700e12
    laser['target']['max_change_hz']=30e6
    assert study.resolved_parameters(laser,p)['offsets_hz']==[30e6,0.]
    p['offsets_hz']=[10e6,0.]
    assert study.resolved_parameters(laser,p)['offsets_hz']==[10e6,0.]


def test_static_fit_and_candidate_are_not_physical_optimization():
    r=measured();fit=r['feedforward_identification']
    assert fit['status']=='static_candidate_only'
    assert fit['target_feedforward_scale']==pytest.approx(1.1,abs=.001)
    proposal=dict(action='propose_response_and_feedforward',parameters=dict(profile={k:r['parameters'][k] for k in FIELDS},
        reason='Endpoint evidence',limitations='Static identification; untested dynamics',target_feedforward_scale=1.1,target_ramp_step_hz=100e6))
    value=validate_proposal(r,proposal)
    assert value['feedforward']['status']=='requires_physical_validation_and_approval'
    assert value['feedforward']['applied'] is False
    proposal['parameters']['target_feedforward_scale']=2
    with pytest.raises(ValueError,match='outside'):validate_proposal(r,proposal)


@pytest.mark.parametrize('damage',['dac','gap','controls','provenance','unsettled'])
def test_confounded_evidence_cannot_produce_ff_candidate(damage):
    r=measured()
    for leg in r['trials']:
        if damage=='dac':
            for row in leg['history']['samples']:row['dac_count']=None
        elif damage=='gap':leg['history']['integrity_errors']=['gap']
        elif damage=='controls':leg['control_after']['outputs']['temperature']=.1
        elif damage=='provenance':leg.pop('control_before')
        else:leg['analysis']['status']='not_settled'
    assert identify(r)['status']=='inconclusive'


def test_candidate_may_reduce_step_but_never_exceed_authorized_default():
    r=measured();proposal=dict(action='propose_response_and_feedforward',parameters=dict(profile={k:r['parameters'][k] for k in FIELDS},
        reason='Evidence',limitations='Needs physical validation',target_feedforward_scale=1.1,target_ramp_step_hz=200e6))
    with pytest.raises(ValueError,match='100 MHz'):validate_proposal(r,proposal)
    proposal['parameters']['target_ramp_step_hz']=20e6
    assert validate_proposal(r,proposal)['feedforward']['target_ramp_step_hz']==20e6
