"""Agent-selected PI candidates measured by the shared change/measure executor.

The provider has evidence tools only. This host validates each proposal, measures
paired baseline/candidate windows, and adopts only a repeated improvement.
"""
from copy import deepcopy
import math
from pathlib import Path
import statistics

from ldcon.core.contracts import DeviceError
from ldcon.core.interface_cards import CardCancelled
from ldcon.core.interface_schema import load_mapping
from ldcon.core.records import read_json, atomic_json
from ldcon.jobs.change_measure import ChangeMeasure
from ldcon.jobs.dlc_pi_measurement import PIAdapter
from ldcon.jobs.study_execution import export_evidence, parse_proposal
from ldcon.owners.dlcpro_pi_trial import candidate, KEYS


def metrics(result):
    if result['status']!='succeeded':raise DeviceError('Incomplete PI measurement window')
    target=result['parameters']['target_frequency_hz']
    rows=[(r['measurements']['wavelength']['acquired_unix_s'],r['measurements']['wavelength']['values']['frequency_hz']-target)
          for r in result['samples'] if r['phase']=='measurement' and 'wavelength' in r['measurements']]
    start,end=rows[0][0],rows[-1][0]
    blocks=[[] for _ in range(6)]
    for t,e in rows:blocks[min(5,int(6*(t-start)/(end-start)))].append(e)
    if any(len(b)<2 for b in blocks):raise DeviceError('Six populated temporal blocks required')
    loss=[statistics.mean(e*e for e in b) for b in blocks]
    error=[e for _,e in rows]
    controls=result.get('control_observations',[])
    outputs=[r['applied_pzt_v'] for r in controls]
    return dict(samples=len(error),duration_s=end-start,mean_hz=statistics.mean(error),
        rms_hz=math.sqrt(statistics.mean(e*e for e in error)),peak_hz=max(map(abs,error)),
        mse=statistics.mean(e*e for e in error),mse_se=statistics.stdev(loss)/math.sqrt(6),
        block_mean_hz=[statistics.mean(b) for b in blocks],
        pzt_activity_v_per_s=sum(abs(b-a) for a,b in zip(outputs,outputs[1:]))/(end-start),
        integral_saturation_fraction=sum(abs(r['integral_v'])>=.999*r['control']['integral_limit_v'] for r in controls)/max(1,len(controls)))


def improved(before, trial, after):
    # Matched baselines account for ordinary drift; block scatter is empirical,
    # not an independent-sample confidence guarantee or a lock-tolerance gate.
    if 'response_loss' in before:
        from ldcon.jobs.response_comparison import improved as joint_improvement
        return joint_improvement(before,trial,after)
    baseline=min(before['mse'],after['mse'])
    uncertainty=2*math.sqrt(before['mse_se']**2+trial['mse_se']**2+after['mse_se']**2)
    return (baseline-trial['mse']>uncertainty
        and trial['peak_hz']<=max(before['peak_hz'],after['peak_hz'])
        and trial['integral_saturation_fraction']<=max(before['integral_saturation_fraction'],after['integral_saturation_fraction']))


def evidence_documents(directory, result, state, turn):
    prefix='decision-%03d-'%turn
    docs=[(prefix+'summary',deepcopy(result)),(prefix+'control',deepcopy(state))]
    for row in result['measurements'][-3:]:
        raw=read_json(Path(directory)/row['path'])
        steps=raw.get('steps',[raw])
        values=[(i,r['phase'],r['measurements']['wavelength']['acquired_unix_s'],r['measurements']['wavelength']['values']['frequency_hz'],step['parameters']['target_frequency_hz'])
                for i,step in enumerate(steps) for r in step['samples'] if 'wavelength' in r['measurements']]
        for index in range(0,len(values),600):
            docs.append((prefix+row['id']+'-samples-%04d'%index,
                dict(columns=['step','phase','acquired_unix_s','frequency_hz','target_frequency_hz'],rows=values[index:index+600])))
        control=[dict(observed_s=r['observed_s'],pzt_v=r['applied_pzt_v'],integral_v=r['integral_v'],
                      last_tec_s=r['last_tec_s'],settings=r['control']) for step in steps for r in step.get('control_observations',[])]
        for index in range(0,len(control),40):
            docs.append((prefix+row['id']+'-control-%04d'%index,control[index:index+40]))
    return docs


def run(directory,p,*,clock,sleep,cancel,emit,selected,decision_session,adapter=None,report_writer=None):
    directory=Path(directory)
    invocation=read_json(directory/'invocation.json')
    plan=invocation.get('adjustment_plan') or {}
    if plan.get('kind') not in ('dlc_pi_change_measure','dlc_pi_target_response'):raise ValueError('Frozen PI measurement plan required')
    laser=load_mapping((directory/'laser.yaml').read_text())
    adapter=adapter or PIAdapter(laser,directory.name,clock=clock)
    joint=plan['kind']=='dlc_pi_target_response'
    if joint:
        if adapter.target!=plan['target_frequency_hz']:raise ValueError('Live target differs from frozen response reference')
        adapter.target_program=deepcopy(plan['target_program'])
    program=deepcopy(plan['steps'] if joint else [plan['measurement']])
    frame_s=sum(q['baseline_s']+q['wait_s']+q['duration_s'] for q in program)
    frame_budget=plan.get('timeout_s',program[0]['timeout_s'])
    measurement=program[0]
    result=dict(status='running',trials=[],measurements=[],checkpoint=adapter.origin,adopted=False,
        physical_outcome='no_owner_command_in_flight',target_frequency_hz=adapter.target,
        interpretation='Finite paired windows; not a global optimum or a certified confidence interval')
    adopted=False
    def save():atomic_json(directory/'pi-adjustment.json',result)
    def check():
        if cancel() or clock()>=decision_session.deadline:raise CardCancelled('PI adjustment cancelled/deadline')
        adapter.check()
    def measure(values,role):
        for attempt in range(2):
            check()
            if joint and adapter.program_cycle>=plan['target_program']['max_cycles']:
                raise CardCancelled('Approved response round-trip budget exhausted')
            serial=len(result['measurements'])+len(result.get('incomplete_measurements',[]))
            name='pi-window-%03d'%serial
            frame_deadline=min(decision_session.deadline,clock()+plan.get('timeout_s',measurement['timeout_s']))
            raw_steps=[]
            for index,template in enumerate(program):
                check()
                while getattr(adapter,'measurement_missing',False):
                    sleep(.1);check()
                    if clock()>=frame_deadline:raise DeviceError('PI frame deadline during input hold')
                q=deepcopy(template)
                if index==0:q['changes']=[dict(parameter=k,value=v) for k,v in values.items()]
                target=adapter.target
                if q['changes'] and q['changes'][0]['parameter']=='target_frequency_hz':target+=q['changes'][0]['offset']
                q['target_frequency_hz']=target
                remaining=frame_deadline-clock()
                if remaining<q['baseline_s']+q['wait_s']+q['duration_s']+10:
                    raise CardCancelled('Insufficient remaining task time for a complete PI window')
                q['timeout_s']=min(q['timeout_s'],remaining)
                runner=ChangeMeasure(adapter,q,clock=clock,wall_clock=adapter.wall_clock,sleep=sleep,cancel=cancel,emit=emit)
                try:step=runner.run()
                finally:atomic_json(directory/(name+'-step-%02d.json'%index),runner.result)
                raw_steps.append(step)
            raw=dict(status='succeeded',steps=raw_steps) if joint else raw_steps[0]
            atomic_json(directory/(name+'.json'),raw)
            if any(step['status']!='succeeded' for step in raw_steps):
                result.setdefault('incomplete_measurements',[]).append(dict(id=name,role=role,
                    path=name+'.json',settings=deepcopy(values),reason='Measurement coverage incomplete; not compared'))
                save()
                if attempt or decision_session.deadline-clock()<frame_s+30:
                    raise DeviceError('Incomplete PI evidence after bounded recollection')
                # All typed steps finished and returned before recollection.
                # Never replay an unknown or rejected application from here.
                continue
            if joint:
                from ldcon.jobs.response_comparison import metrics as response_metrics
                measured=response_metrics(raw,metrics,result.get('response_band_hz'))
                result.setdefault('response_band_hz',measured['response_band_hz'])
            else:measured=metrics(raw)
            row=dict(id=name,role=role,settings=deepcopy(values),metrics=measured,path=name+'.json')
            result['measurements'].append(row);save()
            emit('pi_measurement_completed',row)
            return row

    def commit(values,reason):
        check()
        proposal=adapter.preflight([dict(parameter=k,value=v) for k,v in values.items()],'atomic',measurement['measurements'])
        result['adoption_receipt']=adapter.apply(proposal,lambda:None)
        receipt=adapter.close(adopt=True)
        result['cleanup']=receipt
        if (not receipt or receipt.get('state')!='adopted' or receipt.get('confirmed') is not True
                or receipt.get('settings')!=values or receipt.get('identity')!=adapter.identity
                or receipt.get('target_restored') is not True):
            raise CardCancelled('Adoption was preempted or not confirmed by the owner')
        result.update(adopted=True,adopted_settings=values,reason=reason,
            adoption_basis='Task-model decision after repeated matched measurements')
    try:
        adapter.begin();save()
        baseline=measure(adapter.origin,'baseline')
        invalid=0
        while True:
            check()
            if decision_session.turns>=decision_session.profile['max_turns']-2:break
            if decision_session.deadline-clock()<30:break
            store,meta,_,_=selected
            docs=evidence_documents(directory,result,adapter.state,decision_session.turns)
            _,manifest=export_evidence(directory,store,laser['id'],docs)
            prompt=('Optimize independent normalized P and I, and integral-output contribution if justified. '
                'Assess oscillation, sustained bias/drift, saturation, sample cadence and TEC/PZT coupling. '
                'Each candidate gets the same recorded measurement window, then checkpoint coefficients are restored '
                'for a matched baseline. A promising candidate must pass a second comparison before adoption. '
                'Do not use the lock display tolerance as an optimization stopping target. Do not change the optical '
                'target program, TEC settings, FF, calibration, physical bounds or firmware. PZT output is continuous at '
                'coefficient changes; both gains can increase or decrease. Model kp and ki_per_s act on '
                '(target-frequency)/signed_PZT_slope; the integral is stored directly in volts, limited by '
                'integral_limit_v, and an output offset compensates transitions. Consider several periods; '
                'report if the fixed measurement duration cannot establish a conclusion. Read every evidence file. '
                'Return one JSON object {"action":"trial","parameters":{"candidate":{"kp":number,'
                '"ki_per_s":number,"integral_limit_v":number},"reason":"..."}}, '
                'or {"action":"adopt","parameters":{"trial_id":number,"reason":"..."}}, '
                'or {"action":"finish","parameters":{"reason":"..."}}. No other commands. '
                'Explain why the evidence warrants a trial, adoption, or no further changes. '+p['prompt']+' '+p['adjustment_objective'])
            if joint:
                prompt+=' This is JOINT target-step and steady-noise optimization. The host executes the frozen target-step and return sequence; you only choose P/I/integral coefficients. Judge the overall trade-off among response speed, steady RMS, mean bias, overshoot, saturation and practical robustness. A modest RMS increase can be worthwhile for a substantial speed gain: the human explicitly gave +10 percent RMS with 2x faster response as an acceptable kind of trade-off, not a fixed numeric acceptance rule. Do not require RMS non-degradation or invent fixed weights. Quantify gains and costs and justify your preference. The response band is empirical from the first checkpoint and stays fixed. Null settling times are censored, not zero or the deadline. Compare request/verified-write timestamps, signed mean error, response integral and the full raw waveforms. No candidate-specific relaxation of the band. Requesting adopt for an initial trial triggers a repeat and matched baseline. Review that new confirmation row before issuing adopt again for its trial_id; only the second decision can commit. You may decline after seeing confirmation.'
            answer=decision_session.request(dict(kind='pi_adjustment_decision',prompt=prompt,evidence_files=manifest),tick=check)
            try:
                decision=parse_proposal(answer,allow_progress_prefix=True)
                if not isinstance(decision,dict) or set(decision)!={'action','parameters'}:raise ValueError('Expected action and parameters')
                action=decision['action'];args=decision['parameters']
                if not isinstance(args,dict) or not isinstance(args.get('reason'),str) or not args['reason']:raise ValueError('Reason required')
                expected={'trial':{'candidate','reason'},'adopt':{'trial_id','reason'},'finish':{'reason'}}
                if action not in expected or set(args)!=expected[action]:raise ValueError('Unknown action or fields')
                if action=='trial':candidate(args['candidate'])
                if action=='adopt':
                    if type(args['trial_id']) is not int or not 0<=args['trial_id']<len(result['trials']):raise ValueError('Unknown trial')
                    if not result['trials'][args['trial_id']]['eligible']:raise ValueError('Trial lacks qualified comparison evidence')
                    if p['adoption_mode']!='auto_within_approved':raise ValueError('This run permits report only')
            except (ValueError,TypeError) as error:
                invalid+=1;result['last_rejection']=str(error);save()
                if invalid>=3:raise ValueError('Three invalid PI decisions; no command replay')
                continue
            invalid=0;result.pop('last_rejection',None)
            atomic_json(directory/('pi-decision-%03d.json'%decision_session.turns),decision)
            if action=='finish':result['reason']=args['reason'];break
            values=args['candidate'] if action=='trial' else result['trials'][args['trial_id']]['candidate']
            if joint and action=='adopt' and 'confirmation_of' in result['trials'][args['trial_id']]:
                commit(values,args['reason']);adopted=True
                break
            if decision_session.deadline-clock()<2*frame_budget+30:
                result['last_rejection']='Insufficient time for another paired experiment; finish or adopt an already confirmed candidate';save()
                continue
            if joint and adapter.program_cycle+2>plan['target_program']['max_cycles']:
                result['last_rejection']='Insufficient approved round trips for a paired experiment';save()
                continue
            missing_before=len(result.get('incomplete_measurements',[]))
            trial=measure(values,'candidate' if action=='trial' else 'confirmation')
            after=measure(adapter.origin,'matched_baseline')
            recollected=len(result.get('incomplete_measurements',[]))!=missing_before
            eligible=not recollected and improved(baseline['metrics'],trial['metrics'],after['metrics'])
            row=dict(id=len(result['trials']),candidate=values,before=baseline['id'],trial=trial['id'],after=after['id'],eligible=eligible,reason=args['reason'])
            if recollected:row['ineligible_reason']='Input gap interrupted the pair; the new matched baseline is available for replanning'
            if joint and action=='adopt':row['confirmation_of']=args['trial_id']
            result['trials'].append(row);baseline=after;save()
            if action=='adopt' and eligible and not joint:
                commit(values,args['reason']);adopted=True
                break
        result['status']='succeeded'
        result.setdefault('reason','Finite task budget reached; checkpoint retained')
    finally:
        # Never restore an old optical target; the owner preempts this epoch
        # before foreground changes and keeps a correlated terminal receipt.
        try:
            if not adopted and not result.get('cleanup'):result['cleanup']=adapter.close(adopt=False)
        except Exception as error:
            result.update(status='unknown',physical_outcome='requires_inspection',cleanup_error=type(error).__name__)
            atomic_json(directory/'device-review.json',dict(source='dlc_pi',message='PI cleanup unconfirmed; inspect owner, do not replay'))
            save();raise
        if result['status']=='running':result['status']='interrupted'
        save()
        try:
            from ldcon.jobs.measurement_report import write_report
            frames=[read_json(directory/row['path']) for row in result['measurements']]
            steps=[step for raw in frames for step in raw.get('steps',[raw])]
            report=(report_writer or write_report)(directory,dict(schema='ldcon.pi-comparison/1',status=result['status'],steps=steps,pi_result=result))
            import shutil
            shutil.copyfile(report,directory/'report.pdf')
            result['report']='report.pdf'
        except Exception as error:result['report_error']=type(error).__name__
        save()
    result['message']='Measured PI improvement adopted for this running lock' if adopted else 'PI checkpoint retained after measured comparisons'
    return result
