"""Finite paired staircase FF comparisons above the registered sweep card.

Every physical change and restoration belongs to that child and its owner.
This workflow recommends cadence-qualified pairs; it never saves coefficients.
"""
import copy
import fcntl
import math
import time
from pathlib import Path

from ldcon.core.interface_cards import CardCancelled
from ldcon.core.contracts import DeviceError
from ldcon.core.records import atomic_json
from ldcon.jobs.progress import CancelledRun
from ldcon.jobs.sweep_ff_analysis import evaluate_trial
from ldcon.jobs.sweep_ff_plan import parameters, validate_laser, child_parameters, condition_metadata


def recommendations(trials, p):
    """A Pareto envelope per cadence; never compose independently tested knobs."""
    rows = []
    for condition in p['conditions']:
        for candidate in p['candidates']:
            baseline = candidate['id'] == p['baseline_candidate_id']
            selected = [t for t in trials if t['condition']['id']==condition['id']
                        and t['candidate']['id']==candidate['id'] and t['role']==('baseline' if baseline else 'candidate')]
            if len(selected) < 1+p['confirmation_runs']:continue
            if not all(t['evaluation']['qualified'] and t.get('baseline_comparison', {}).get('comparable') is True for t in selected):
                continue
            rows.append(dict(candidate=copy.deepcopy(candidate), condition=condition_metadata(condition),
                trial_ids=[t['id'] for t in selected],
                worst_settling_s=max(t['evaluation']['worst_settling_s'] for t in selected),
                rms_error_hz=max(t['evaluation']['rms_error_hz'] for t in selected),
                evidence='repeated complete recorded span; not a measured mode-hop boundary',
                application='retain baseline' if baseline else 'recommended per-shot settings only', applied=False))
    # Cadence includes step sizes and direction: a large coarse step is not
    # equivalent to a small step merely because the dwell is the same.
    def cadence(row):
        c=row['condition']
        return (c['dwell_s'], c['edge_s'], tuple(c['step_sizes_hz']), c['direction'],
                c['cycle_period_s'], c['repetitions'], c['precondition_s'], c['lead_s'], c['tail_s'])
    for row in rows:
        row['pareto'] = not any(cadence(other)==cadence(row)
            and other['condition']['span_hz'] >= row['condition']['span_hz']
            and other['worst_settling_s'] <= row['worst_settling_s']
            and other['rms_error_hz'] <= row['rms_error_hz']
            and (other['condition']['span_hz'] > row['condition']['span_hz']
                 or other['worst_settling_s'] < row['worst_settling_s']
                 or other['rms_error_hz'] < row['rms_error_hz']) for other in rows)
    return rows


def baseline_comparison(before, after, maximum):
    def usable(value):
        return (value.get('frequency_continuity', {}).get('status')=='continuous'
                and value.get('data_valid') is True
                and type(value.get('mean_error_hz')) in (int,float)
                and math.isfinite(value['mean_error_hz']))
    delta = abs(after['mean_error_hz']-before['mean_error_hz']) if usable(before) and usable(after) else None
    return dict(comparable=delta is not None and delta<=maximum,
                mean_error_shift_hz=delta, max_baseline_shift_hz=maximum,
                reason='Matched baselines bracket this candidate; this does not establish thermal equilibrium')


def summary(result):
    return {key:copy.deepcopy(result[key]) for key in
            ('schema','status','reason','completed_trials','planned_trials','applied','recommendations','report') if key in result}


def run_card(directory, laser, card, p, *, clock, sleep, cancel, emit,
             publish=lambda _:None, experiment=None, report_writer=None):
    from ldcon.core.interface_schema import load_mapping
    from ldcon.jobs.sweep_ff_transport import SweepExperiment
    directory=Path(directory)
    p=parameters({'parameters':p},{})
    deck=load_mapping((directory/'interface-deck.yaml').read_text())
    validate_laser(laser,deck,p)
    result=dict(schema='ldcon.sweep-ff-optimization/1', status='running', parameters=p,
                trials=[], completed_trials=0,
                planned_trials=len(p['conditions'])*(1+2*(len(p['candidates'])-1)*(1+p['confirmation_runs'])),
                applied=False, recommendations=[],
                limitations=['Finite recorded spans are not true hop boundaries or long-term qualification.',
                             'No target-change FF, saved coefficients, calibration, PI gains or limits are modified.',
                             'An active finite sweep may finish its record before owner cleanup completes.'])
    (directory/'sweep-ff'/'trials').mkdir(parents=True,exist_ok=True)
    guard=(directory.parent/'sweep-ff-optimization.lock').open('a')
    deadline=clock()+p['timeout_s']
    def save(*, final=False):
        result['completed_trials']=len(result['trials'])
        result['recommendations']=recommendations(result['trials'],p)
        atomic_json(directory/'sweep-ff-optimization.json',result)
        try:publish(summary(result))
        except CancelledRun:
            if not final:raise
    def check():
        if cancel():raise CardCancelled('Sweep FF optimization cancelled')
        if clock()>=deadline:raise CardCancelled('Sweep FF optimization deadline reached')
        if experiment is not None:experiment.check()
    def trial(condition,candidate,role):
        check()
        if len(result['trials'])>=p['max_trials']:raise TrialBudgetReached()
        experiment.wait(condition['precondition_s'])
        check()
        identity='trial-%04d'%len(result['trials'])
        metadata=dict(trial_id=identity,candidate=copy.deepcopy(candidate),condition=condition_metadata(condition))
        atomic_json(directory/'sweep-ff'/'trials'/(identity+'-intent.json'),metadata)
        evidence=experiment.run(child_parameters(p,condition,candidate))
        analysis=copy.deepcopy(evidence.get('analysis') or {})
        analysis['trial']=metadata
        analysis['owner_outcome']=copy.deepcopy(evidence['owner_outcome'])
        relative='sweep-ff/trials/'+identity+'.json'
        atomic_json(directory/relative,dict(analysis,child_id=evidence.get('child_id')))
        row=dict(id=identity,role=role,candidate=copy.deepcopy(candidate),
                 condition=condition_metadata(condition),child_id=evidence.get('child_id'),
                 owner_outcome=evidence['owner_outcome'],analysis_file=relative)
        outcome=evidence['owner_outcome']
        if outcome.get('restored') is not True or outcome.get('outputs_uncertain') is not False:
            # Persist physical uncertainty before analysis or any publication
            # callback can notice cancellation and mask it as a normal stop.
            row['evaluation']=dict(qualified=False,data_valid=False,
                reasons=['Child output or restoration is unconfirmed'],
                frequency_continuity={'status':'unknown'})
            result['trials'].append(row)
            result['physical_outcome']='child_output_or_restoration_unconfirmed'
            atomic_json(directory/'device-review.json',dict(
                lease_unknown=True,outputs_uncertain=True,child_id=evidence.get('child_id'),
                message='Sweep child output/restoration is unconfirmed; inspect the durable dispatch and child journal. No retry.'))
            raise DeviceError('Child restoration is unconfirmed; no further trial')
        evaluated=evaluate_trial(analysis,p['criteria'])
        row['evaluation']=evaluated
        result['trials'].append(row)
        save();emit('sweep_ff_trial',dict(trial_id=identity,role=role,
            candidate=candidate['id'],condition=condition['id'],qualified=evaluated['qualified'],
            reasons=evaluated['reasons']))
        if outcome.get('cancelled') is True:
            raise CardCancelled('Sweep FF child cancelled; owner cleanup evidence recorded')
        if outcome.get('cancelled') is not False or outcome.get('playback_completed') is not True:
            raise UnsafeToContinue('Child did not complete its record; no further trial')
        if outcome.get('censored') is not False or evaluated.get('data_valid') is not True:
            raise UnsafeToContinue('Sweep outcome or raw evidence is incomplete; no further trial')
        if evaluated.get('frequency_continuity',{}).get('status')!='continuous':
            raise UnsafeToContinue('Mode continuity is not established; no further sweep or automatic recovery')
        return row
    try:
        fcntl.flock(guard,fcntl.LOCK_EX|fcntl.LOCK_NB)
        experiment=experiment or SweepExperiment(directory,laser,deck,p['sweep_card_id'],
            timeout_s=p['timeout_s'],poll_s=p['poll_s'],cleanup_timeout_s=p['cleanup_timeout_s'],
            analysis_wait_s=p['analysis_wait_s'],clock=clock,sleep=sleep,cancel=cancel,
            emit=emit,publish=publish)
        save()
        baseline=next(c for c in p['candidates'] if c['id']==p['baseline_candidate_id'])
        # Frozen order and paired baselines make thermal history auditable;
        # the same coefficients are tested jointly in every repetition.
        for condition in p['conditions']:
            before=trial(condition,baseline,'baseline')
            for candidate in p['candidates']:
                if candidate['id']==baseline['id']:continue
                for _ in range(1+p['confirmation_runs']):
                    current=trial(condition,candidate,'candidate')
                    after=trial(condition,baseline,'baseline')
                    current['baseline_comparison']=dict(baseline_comparison(before['evaluation'],after['evaluation'],
                        p['max_baseline_shift_hz']),before_trial=before['id'],after_trial=after['id'])
                    for base in (before,after):
                        prior=base.get('baseline_comparison')
                        proof=copy.deepcopy(current['baseline_comparison'])
                        if prior and prior['comparable'] is not True:proof['comparable']=False
                        base['baseline_comparison']=proof
                    before=after
                    save()
        result.update(status='succeeded',reason='Finite comparison completed; recommendations are not applied')
        if not recommendations(result['trials'],p):
            result.update(status='inconclusive',reason='No repeatedly qualified candidate with stable matched baselines')
    except (TrialBudgetReached,UnsafeToContinue) as error:
        result.update(status='inconclusive',reason=str(error) or 'Explicit trial budget reached')
    except (CardCancelled,CancelledRun) as error:
        result.update(status='cancelled',reason=str(error));raise
    except Exception as error:
        result.update(status='failed',reason=type(error).__name__);raise
    finally:
        save(final=True);guard.close()
        # Results and owner effects survive a plotting/toolchain failure.
        try:
            if report_writer is None:
                from ldcon.jobs.sweep_ff_report import write_report
                report_writer=write_report
            pdf=report_writer(directory,result)
            import shutil
            if Path(pdf)!=directory/'report.pdf':shutil.copyfile(pdf,directory/'report.pdf')
            result['report']=dict(status='created',path='report.pdf')
        except Exception as error:
            result['report']=dict(status='unavailable',error_type=type(error).__name__)
        save(final=True)
    return summary(result)


class TrialBudgetReached(Exception):
    pass


class UnsafeToContinue(Exception):
    pass
