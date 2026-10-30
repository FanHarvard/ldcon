"""Finite target FF comparisons; the resident proposes, the parent owner executes.

Selection is based on the same frozen settling criteria in both directions.
Every coefficient is temporary; returning targets never overrides foreground CAS.
"""
import copy
import hashlib
import json
from pathlib import Path

from ldcon.core.contracts import DeviceError
from ldcon.core.records import atomic_json
from ldcon.jobs import target_response_feedforward as ff

OBJECTIVE = '''Choose the next bounded target-response experiment from the recorded full-rate waveforms.
The goal is settling within the supplied goal_s (default 3 seconds), measured from target request,
in BOTH directions with the SAME frozen precision/coverage/confirmation criteria. Prefer the largest
internal target step that meets this goal. Start at 100 MHz; optimize target FF at that size first.
Only reduce step size if qualified evidence shows the larger step cannot meet the goal. Never run
smaller steps just to fill a grid or if the largest tested step already passes. A missing record is
not evidence that a step is too large. Do not relax criteria to make a coefficient pass.
Target FF scale multiplies the target-induced DAC offset derived from existing calibration; it is
not TEC-to-current FF or a calibration change. Consider both directions, drift, static bias, overshoot,
noise, relapse, endpoint actuator provenance and latency. Endpoint inference is not proof of dynamic
optimality. If there is no defensible next experiment, choose inconclusive.
Propose parameters only; do not operate devices or tools that change configuration. The host checks
bounds, budget, foreground priority and owner proof before a trial. All evidence and outputs are English.'''
ACTIONS = dict(next_trial=dict(type='object',additionalProperties=False,
    required=['scale','step_hz','reason'],properties=dict(
        scale=dict(type='number',minimum=.5,maximum=2.),
        step_hz=dict(type='number',minimum=10e6,maximum=100e6),
        reason=dict(type='string',minLength=1,maxLength=4000))),
    inconclusive=dict(type='object',additionalProperties=False,required=['reason'],
        properties=dict(reason=dict(type='string',minLength=1,maxLength=4000))))


def run(directory, result, ctx, selected, *, measure_round, clock, sleep, cancel, publish, provider=None):
    from ldcon.jobs.target_response_search import plan
    from ldcon.jobs.target_response_decisions import evidence, FIELDS
    from ldcon.jobs.study_execution import evaluate
    p=result['parameters'];started=clock();rounds=[];directory=Path(directory)
    ctx.ff_deadline=started+p['ff_budget_s']
    if len(p['offsets_hz'])!=2 or p['offsets_hz'][1]!=0 or not 0<p['offsets_hz'][0]<=100e6:
        raise ValueError('FF comparison requires one positive jump <=100 MHz followed by zero')
    criteria={k:p[k] for k in FIELDS}
    revision=hashlib.sha256(json.dumps(criteria,sort_keys=True).encode()).hexdigest()
    initial=ctx.policy_request('status',sleep=sleep)
    if initial.get('active'): raise DeviceError('Another FF study owns the temporary policy')
    if initial['maximum_step_hz']<100e6: raise DeviceError('Owner does not authorize a 100 MHz starting step')
    bounds=initial['authorized_scale_bounds']
    if bounds[0]>.5 or bounds[1]<2.: raise DeviceError('Owner FF comparison bounds .5..2 are not approved')
    initial_scale=initial['defaults']['scale']
    next_parameters=None
    while True:
        ctx.check(force=True)
        choice=plan(rounds,goal_s=p['settling_goal_s'],initial_scale=initial_scale,
            elapsed_s=clock()-started,max_roundtrips=p['ff_max_roundtrips'],budget_s=p['ff_budget_s'],
            criteria_revision=revision)
        if choice['action']!='propose_trial':
            return dict(choice,rounds=rounds,criteria=criteria,goal_s=p['settling_goal_s'],
                elapsed_s=clock()-started,applied=False,
                adoption='validated candidate only; temporary coefficient restored after comparison')
        remaining=p['ff_budget_s']-(clock()-started)
        pair_bound=2*(p['baseline_s']+p['command_timeout_s']+p['observation_s']+p['data_wait_s'])+160
        if remaining<pair_bound:
            return dict(action='inconclusive',reason='insufficient_budget_for_bounded_round_trip',
                        rounds=rounds,elapsed_s=clock()-started,applied=False)
        if rounds:
            # Each decision has its own immutable evidence path and request id.
            decision_dir=directory/(directory.name+'-ff-'+str(len(rounds)))
            decision_dir.mkdir(exist_ok=False)
            candidate=copy.deepcopy(result)
            candidate['ff_search']=dict(rounds=rounds,goal_s=p['settling_goal_s'],criteria_revision=revision,
                host_suggested_next=choice,remaining_s=remaining,maximum_roundtrips=p['ff_max_roundtrips'])
            response=evaluate(decision_dir,selected,laser_id=result['laser_id'],documents=evidence(candidate),
                objective=OBJECTIVE,actions=ACTIONS,timeout_s=min(p['agent_timeout_s'],remaining-pair_bound),
                clock=clock,sleep=sleep,cancel=cancel,publish=publish,provider=provider)
            if response['action']=='inconclusive':
                return dict(action='inconclusive',reason=response['parameters']['reason'],rounds=rounds,applied=False)
            next_parameters=response['parameters']
            # A model may choose a coefficient from evidence. Narrowing is only
            # allowed when the host has exhausted justified FF at the larger size.
            if next_parameters['step_hz']<choice['parameters']['step_hz']:
                raise ValueError('Premature step reduction; compare FF at the larger step first')
            repeat=choice['reason'].startswith('Repeat identical scale and step')
            duplicate=any(abs(r['scale']-next_parameters['scale'])<1e-9 and r['step_hz']==next_parameters['step_hz'] for r in rounds)
            if repeat and any(next_parameters[k]!=choice['parameters'][k] for k in ('scale','step_hz')):
                raise ValueError('Missing evidence permits only one unchanged measurement repeat')
            if duplicate and not repeat: raise ValueError('Duplicate FF experiment; no automatic replay')
        else:
            next_parameters=choice['parameters']
        scale=next_parameters['scale'];step=next_parameters['step_hz']
        if not .5<=scale<=2. or not 10e6<=step<=100e6: raise ValueError('FF experiment exceeds approved bounds')
        ctx.check(force=True)
        if ctx.ff_deadline-clock()<pair_bound:
            return dict(action='inconclusive',reason='decision_consumed_round_trip_budget',rounds=rounds,applied=False)
        receipt=ctx.policy_request('set',sleep=sleep,scale=scale,step_hz=step,
            duration_s=min(900.,p['ff_budget_s']-(clock()-started)))
        public={k:v for k,v in receipt.items() if k!='session'}
        public['temporary_parameters']=dict(scale=scale,step_hz=step)
        result['feedforward_context'].update(target_feedforward_scale=scale,firmware_chunk_ceiling_hz=step)
        publish(dict(state='comparing_target_feedforward',round_index=len(rounds),scale=scale,step_hz=step,
                     settling_goal_s=p['settling_goal_s']))
        legs=measure_round(len(rounds))
        local=dict(result,trials=legs)
        row=dict(scale=scale,step_hz=step,status='completed',trials=copy.deepcopy(legs),
            criteria_revision=revision,feedforward_identification=ff.identify(local),owner_policy=public)
        rounds.append(row)
        result['ff_comparison']=dict(action='running',rounds=rounds,goal_s=p['settling_goal_s'],criteria=criteria)
        atomic_json(directory/'target-response.json',result)
