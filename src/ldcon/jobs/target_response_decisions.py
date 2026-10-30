"""Target-response evaluation policy over the common study decision port."""
import copy

from ldcon.jobs.target_response_analysis import analyse

FIELDS = ('expected_settle_s', 'evaluation_window_s', 'check_interval_s', 'confirmation_s',
          'mean_error_max_hz', 'rms_error_max_hz', 'peak_error_max_hz', 'minimum_samples', 'max_gap_s')
PROFILE = dict(type='object', additionalProperties=False, required=list(FIELDS), properties={
    key: dict(type=['number','null'] if key=='peak_error_max_hz' else 'integer' if key=='minimum_samples' else 'number',
              minimum=2 if key=='minimum_samples' else 0) for key in FIELDS})
ACTIONS = dict(propose_profile=dict(type='object', additionalProperties=False, required=['profile','reason','limitations'],
    properties=dict(profile=PROFILE, reason=dict(type='string',minLength=1,maxLength=4000),
                    limitations=dict(type='string',minLength=1,maxLength=4000))),
    inconclusive=dict(type='object', additionalProperties=False, required=['reason'],
                      properties=dict(reason=dict(type='string',minLength=1,maxLength=4000))))
OBJECTIVE = '''Optimize only expected settling time and settling evaluation thresholds/windows/confirmation.
Use the full recorded waveform, timestamps and source cadence, not only the original pass/fail labels.
Compare the transient against the late record, noise, bias, outliers, relapse and measurement gaps.
Do not loosen thresholds merely to make every trial pass. Explain precision versus latency and
false-settling tradeoffs. If data cannot establish a defensible profile, choose inconclusive.
Never tune PI/FF, hardware bounds, targets, optical quality classifiers or real-time lock settings.
Coverage criteria are evidence integrity, not optimization variables: preserve minimum_samples
and max_gap_s exactly. Return them unchanged in the complete candidate profile.
Candidates are for the next study only, not a demonstrated optimum or future stability guarantee.
The host will re-evaluate every original trial with the same evaluator and preserve both results.
Do not apply the candidate or perform additional measurements. All prompts and your result are English.'''

FF_OBJECTIVE=OBJECTIVE.replace('Never tune PI/FF, hardware bounds, targets, optical quality classifiers or real-time lock settings.',
    'Additionally assess the target-change feedforward coefficient, expressed as a dimensionless scale of '
    'the target-induced DAC offset. Also propose a desired internal target-ramp step at most 100 MHz; '
    'The default goal is settling within 3 seconds; prefer the largest passing step in both directions. '
    'Begin with 100 MHz and optimize FF first; reduce step only if qualified evidence warrants it. '
    'Do not test smaller steps unnecessarily. Owner output/step/slew checks may further reduce it. '
    'This is NOT TEC-to-current feedforward and NOT a calibration change. '
    'Use settled baseline/final DAC and wavelength evidence, controller/actuator provenance, both directions, '
    'noise, drift, bias and uncertainty. Never regress transient DAC and latency-corrected wavelength as '
    'simultaneous observations. A static scale estimate does not prove the fastest transient response. '
    'If the host physical comparison selected a candidate under the fixed goal, preserve that scale/step; '
    'do not rerank physical trials using your new criteria. Otherwise only propose a scale when the host '
    'static identification is supported; otherwise return a settling-only '
    'profile with an explicit FF limitation, or inconclusive. An untested coefficient needs a separate '
    'owner-validated physical comparison and exact authority before application. Never change PI, '
    'hardware bounds, targets, optical quality classifiers or real-time lock settings.')


def task_contract(goal):
    actions=copy.deepcopy(ACTIONS)
    if goal in ('settling_and_feedforward','settling_feedforward_and_tec'):
        combined=copy.deepcopy(actions['propose_profile'])
        combined['required'].append('target_feedforward_scale')
        combined['required'].append('target_ramp_step_hz')
        combined['properties']['target_feedforward_scale']=dict(type='number',exclusiveMinimum=0)
        combined['properties']['target_ramp_step_hz']=dict(type='number',exclusiveMinimum=0,maximum=100e6)
        actions['propose_response_and_feedforward']=combined
        objective=FF_OBJECTIVE
        if goal=='settling_feedforward_and_tec':
            objective+=' Evaluate frequency and actual PZT centering jointly. Preserve the host-selected TEC gain/interval and fixed PZT center, deadband, overshoot/noise reference and confirmation. Missing or unstable PZT evidence is not settled. Report distinct optical, PZT and joint settling times; do not re-rank using relaxed criteria.'
        return objective,actions
    return OBJECTIVE,actions


def evidence(result):
    from ldcon.jobs.target_response import summary
    metadata = summary(result)
    metadata['trials'] = []
    documents = []
    for i, leg in enumerate(result['trials']):
        summary = {k:v for k,v in leg.items() if k not in ('history','analysis')}
        summary['analysis'] = {k:copy.deepcopy(v) for k,v in leg['analysis'].items() if k!='windows'}
        if 'thermal' in summary['analysis']:summary['analysis']['thermal'].pop('windows',None)
        summary['source'] = {k:v for k,v in leg['history'].items() if k!='samples'}
        summary['sample_files'] = []
        # Full rows, bounded below the existing scoped 64 KiB text-read limit.
        samples = leg['history']['samples']
        for offset in range(0,len(samples),80):
            name = 'trial-%d-samples-%d' % (i,offset)
            documents.append((name, dict(trial=i, first_index=offset, samples=samples[offset:offset+80])))
            summary['sample_files'].append(name+'.json')
        documents.append(('trial-'+str(i)+'-metadata',summary))
        metadata['trials'].append(dict(index=i,metadata_file='trial-'+str(i)+'-metadata.json',
                                      sample_files=summary['sample_files']))
    return [('manifest',metadata), *documents]


def optimize(directory, result, selected, *, clock, sleep, cancel, publish, provider=None):
    from ldcon.jobs.study_execution import evaluate
    from ldcon.jobs.target_response import parameters
    objective,actions=task_contract(result['parameters'].get('optimization_goal','settling_criteria'))
    proposal = evaluate(directory, selected, laser_id=result['laser_id'], documents=evidence(result),
        objective=objective, actions=actions, timeout_s=result['parameters']['agent_timeout_s'],
        clock=clock, sleep=sleep, cancel=cancel, publish=publish, provider=provider)
    return validate_proposal(result,proposal)


def validate_proposal(result,proposal):
    """Same postconditions for resident execution and independent benchmarking."""
    from ldcon.jobs.target_response import parameters
    import jsonschema
    _, actions=task_contract(result['parameters'].get('optimization_goal','settling_criteria'))
    if not isinstance(proposal,dict) or set(proposal)!={'action','parameters'} or proposal['action'] not in actions:
        raise ValueError('Unknown target response proposal')
    try: jsonschema.validate(proposal['parameters'],actions[proposal['action']])
    except jsonschema.ValidationError as error:
        raise ValueError('Invalid proposal schema (internal step maximum 100 MHz): '+str(error.validator)) from None
    if proposal['action']=='inconclusive':
        return dict(status='inconclusive', applied=False, reason=proposal['parameters']['reason'])
    value = proposal['parameters']
    p = parameters({'parameters':result['parameters']}, value['profile'])
    for key in ('minimum_samples','max_gap_s'):
        if p[key] != result['parameters'][key]:
            raise ValueError('Study proposal cannot weaken evidence coverage')
    from ldcon.jobs.target_response_thermal import evaluate_leg
    analyses = [evaluate_leg(leg,p) for leg in result['trials']]
    result_value=dict(status='candidate_only', applied=False, parameters=copy.deepcopy(value['profile']),
                reason=value['reason'], limitations=value['limitations'],
                validation='re-evaluated against every original full-rate trial; no new physical test',
                trials=analyses, all_trials_settled=all(a['status']=='settled' for a in analyses))
    if proposal['action']=='propose_response_and_feedforward':
        if result['parameters'].get('optimization_goal') not in ('settling_and_feedforward','settling_feedforward_and_tec'):
            raise ValueError('Feedforward proposal was not requested')
        identification=result.get('feedforward_identification',{})
        comparison=result.get('ff_comparison',{})
        physical=comparison.get('action')=='select'
        if not physical and identification.get('status')!='static_candidate_only':
            raise ValueError('Feedforward evidence does not support a coefficient candidate')
        scale=value['target_feedforward_scale']
        step=value['target_ramp_step_hz']
        import math
        if type(scale) not in (int,float) or not math.isfinite(scale) or scale<=0:
            raise ValueError('Invalid target feedforward scale')
        if type(step) not in (int,float) or not math.isfinite(step) or not 0<step<=100e6:
            raise ValueError('Internal step candidate must be positive and at most 100 MHz')
        if physical:
            if {k:comparison['parameters'][k] for k in ('scale','step_hz')}!={'scale':scale,'step_hz':step}:
                raise ValueError('Candidate differs from fixed-criterion physical selection')
            result_value['feedforward']=dict(target_feedforward_scale=scale,target_ramp_step_hz=step,applied=False,
                status='physically_compared_candidate',goal_s=comparison['goal_s'],
                basis='both directions under original frozen criteria',calibration_changed=False,
                thermal=copy.deepcopy(comparison['parameters'].get('thermal')))
            return result_value
        rows=[r for r in identification['trials'] if r['status']=='static_estimate']
        low=min(r['scale']-r['uncertainty_scale'] for r in rows)
        high=max(r['scale']+r['uncertainty_scale'] for r in rows)
        if not low-1e-9<=scale<=high+1e-9:
            raise ValueError('Feedforward candidate is outside the measured static estimate range')
        result_value['feedforward']=dict(target_feedforward_scale=scale,target_ramp_step_hz=step,applied=False,
            status='requires_physical_validation_and_approval',basis='static_endpoint_estimates',
            safety_range_approved=False,calibration_changed=False)
    elif result['parameters'].get('optimization_goal') in ('settling_and_feedforward','settling_feedforward_and_tec'):
        result_value['feedforward']=dict(status='inconclusive',applied=False,
            reason='No supported feedforward candidate was proposed')
    return result_value
