"""Owner-bounded, reversible TEC/target-FF comparisons with frozen PZT criteria."""
import copy
import hashlib
import json
import math
from pathlib import Path

from ldcon.core.contracts import DeviceError
from ldcon.core.records import atomic_json
from ldcon.jobs.target_response_experiment import OBJECTIVE, ACTIONS
from ldcon.jobs.target_response_search import _qualified, _hazard

OBJECTIVE += '''
Also optimize TEC gain and outer-loop supervision interval, within the supplied owner bounds.
Completion is JOINT frequency and PZT centering under the frozen center/noise/deadband/confirmation
and overshoot reference. Prefer the largest internal step meeting the goal in both directions, then
shorter joint settling. Inspect actual DAC receive-time waveforms for delayed oscillation and overshoot.
A short quiet interval before the first TEC action is not settling. Never move the center or alter PI,
calibration, slew, output bounds, dt cap or TEC-current FF. One pair must include at least two outer
periods and the frozen confirmation interval. Reduce the internal step only after qualified comparisons
at the larger step show need. Missing evidence permits one identical repeat, not a changed experiment.
Explain which observed transient motivates each candidate. An unmet 3-second goal may reflect thermal
physics; report it rather than weakening the criteria. Do not fill a parameter grid unnecessarily.'''


def identity(value):
    return json.dumps({k:value[k] for k in ('scale','step_hz','thermal')},sort_keys=True,allow_nan=False)


def run(directory,result,ctx,selected,*,measure_round,clock,sleep,cancel,publish,provider=None):
    from ldcon.jobs.target_response_decisions import evidence,FIELDS
    from ldcon.jobs.study_execution import evaluate
    p=result['parameters'];started=clock();rounds=[];directory=Path(directory)
    ctx.ff_deadline=started+p['ff_budget_s']
    if len(p['offsets_hz'])!=2 or p['offsets_hz'][1]!=0 or not 0<p['offsets_hz'][0]<=100e6:
        raise ValueError('Thermal comparison requires a positive jump <=100 MHz followed by zero')
    initial=ctx.policy_request('status',sleep=sleep)
    if initial.get('active'):raise DeviceError('Another target study owns the temporary policy')
    thermal=initial.get('thermal') or {}
    bounds=thermal.get('authorized_bounds') or result['thermal_context'].get('authorized_bounds')
    if not bounds or not {'tec_gain','supervision_interval_s'}<=set(bounds):
        raise DeviceError('Owner-approved thermal comparison bounds are unavailable')
    if initial['maximum_step_hz']<100e6 or initial['authorized_scale_bounds'][0]>.5 or initial['authorized_scale_bounds'][1]<2:
        raise DeviceError('Owner target FF comparison envelope is unavailable')
    criteria=dict(frequency={k:p[k] for k in FIELDS},pzt=copy.deepcopy(result['pzt_criteria']))
    revision=hashlib.sha256(json.dumps(criteria,sort_keys=True).encode()).hexdigest()
    actions=copy.deepcopy(ACTIONS)
    actions['next_trial']['required'].append('thermal')
    actions['next_trial']['properties']['thermal']=dict(type='object',additionalProperties=False,
        required=['tec_gain','supervision_interval_s'],properties={
            k:dict(type='number',minimum=bounds[k][0],maximum=bounds[k][1]) for k in ('tec_gain','supervision_interval_s')})
    candidate=dict(scale=initial['defaults']['scale'],step_hz=100e6,
                   thermal=copy.deepcopy(thermal['parameters']))
    def finish(reason,best=None):
        return dict(action='select' if best else 'inconclusive',reason=reason,
            parameters={k:copy.deepcopy(best[k]) for k in ('scale','step_hz','thermal')} if best else {},
            rounds=rounds,criteria=criteria,criteria_revision=revision,goal_s=p['settling_goal_s'],
            elapsed_s=clock()-started,applied=False,
            adoption='Candidate only; temporary TEC and FF policies restored, no physical output rewind')
    while True:
        ctx.check(force=True)
        if any(_hazard(row) for row in rounds):return finish('Terminal outcome; no replay')
        assessed=[(row,_qualified(row,p['settling_goal_s'])) for row in rounds]
        passing=[(r,a) for r,a in assessed if a.get('passes')]
        best=min(passing,key=lambda x:(-x[0]['step_hz'],x[1]['worst_settle_s']))[0] if passing else None
        if best and best['step_hz']==100e6:return finish('Largest step meets frozen joint criteria in both directions',best)
        remaining=ctx.ff_deadline-clock()
        if len(rounds)>=p['ff_max_roundtrips'] or remaining<=0:return finish('Comparison budget exhausted',best)
        if rounds:
            latest,quality=assessed[-1]
            if not quality['qualified']:
                repeats=sum(identity(r)==identity(latest) and not a['qualified'] for r,a in assessed)
                if repeats>=2:return finish('Repeated incomplete PZT/frequency evidence',best)
                candidate={k:copy.deepcopy(latest[k]) for k in ('scale','step_hz','thermal')}
            else:
                # Leave enough time for an unchanged short pair before asking.
                reserve=2*(p['baseline_s']+p['command_timeout_s']+p['observation_s']+p['data_wait_s'])+160
                if remaining<=reserve:return finish('Insufficient budget for a bounded decision and return',best)
                decision_dir=directory/(directory.name+'-thermal-'+str(len(rounds)))
                decision_dir.mkdir(exist_ok=False)
                view=copy.deepcopy(result)
                view['ff_search']=dict(rounds=rounds,criteria=criteria,authorized_thermal_bounds=bounds,
                    remaining_s=remaining,goal_s=p['settling_goal_s'])
                response=evaluate(decision_dir,selected,laser_id=result['laser_id'],documents=evidence(view),
                    objective=OBJECTIVE,actions=actions,timeout_s=min(p['agent_timeout_s'],remaining-reserve),
                    clock=clock,sleep=sleep,cancel=cancel,publish=publish,provider=provider)
                if response['action']=='inconclusive':return finish(response['parameters']['reason'],best)
                candidate=response['parameters']
                if any(identity(r)==identity(candidate) for r in rounds):
                    raise ValueError('Duplicate thermal experiment; no automatic replay')
                if candidate['step_hz']<latest['step_hz']:
                    qualified_at_step=[r for r,a in assessed if a['qualified'] and r['step_hz']==latest['step_hz']]
                    if len(qualified_at_step)<2:raise ValueError('Compare FF/TEC at the larger step before reducing it')
                if best and candidate['step_hz']<=best['step_hz']:
                    return finish('Do not test smaller steps than an already passing candidate',best)
        if (any(type(candidate.get(k)) not in (int,float) or not math.isfinite(candidate[k]) for k in ('scale','step_hz'))
                or not .5<=candidate['scale']<=2 or not 10e6<=candidate['step_hz']<=100e6):
            raise ValueError('Target FF/step exceeds owner comparison envelope')
        t=candidate['thermal']
        if (not isinstance(t,dict) or set(t)!={'tec_gain','supervision_interval_s'}
                or any(type(t[k]) not in (int,float) or not math.isfinite(t[k]) or not bounds[k][0]<=t[k]<=bounds[k][1] for k in t)):
            raise ValueError('TEC candidate exceeds owner comparison envelope')
        observation=max(p['observation_s'],2*t['supervision_interval_s']+criteria['pzt']['confirmation_s'])
        pair_bound=2*(p['baseline_s']+p['command_timeout_s']+observation+p['data_wait_s'])+160
        if pair_bound>min(900.,ctx.ff_deadline-clock()):
            return finish('Candidate period cannot fit a complete pair before token expiry',best)
        receipt=ctx.policy_request('set',sleep=sleep,scale=candidate['scale'],step_hz=candidate['step_hz'],
            thermal=t,duration_s=min(900.,ctx.ff_deadline-clock()))
        if (receipt.get('thermal') or {}).get('parameters')!=t:
            raise DeviceError('Owner did not confirm the requested temporary TEC parameters')
        result['feedforward_context'].update(target_feedforward_scale=candidate['scale'],
            firmware_chunk_ceiling_hz=candidate['step_hz'])
        publish(dict(state='comparing_target_feedforward_and_tec',round_index=len(rounds),**candidate))
        legs=measure_round(len(rounds))
        row={k:copy.deepcopy(candidate[k]) for k in ('scale','step_hz','thermal')}
        row.update(status='completed',trials=copy.deepcopy(legs),criteria_revision=revision,
            owner_policy={k:v for k,v in receipt.items() if k!='session'})
        rounds.append(row)
        result['ff_comparison']=dict(action='running',rounds=rounds,criteria=criteria,goal_s=p['settling_goal_s'])
        atomic_json(directory/'target-response.json',result)
