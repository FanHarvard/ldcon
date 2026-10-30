"""Finite-record PZT centering evaluation, independent of frequency settling.

DAC timestamps are ESP32 receive times, not latency-corrected optical times.
The reference and criteria are frozen before comparisons; candidates cannot pass
by moving the center, inflating their FF jump, or loosening the output envelope.
"""
from bisect import bisect_left, bisect_right
import copy
import math
import statistics


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def criteria(context, baseline, *, reference_ff_step_count, confirmation_s,
             noise_multiplier=3., overshoot_ratio=1.):
    required=('center_dac','resolution_count','tolerance_count','low_dac','high_dac')
    if any(not finite(context.get(k)) for k in required) or context.get('reference_confirmed') is not True:
        raise ValueError('A confirmed owner PZT center and physical envelope are required')
    if (not context['low_dac'] < context['center_dac'] < context['high_dac']
            or context['resolution_count']<=0 or context['tolerance_count']<=0
            or any(not finite(v) or v<=0 for v in (confirmation_s,noise_multiplier,overshoot_ratio))
            or not finite(reference_ff_step_count) or reference_ff_step_count==0):
        raise ValueError('Invalid frozen PZT settling criteria')
    values=[r['dac_count'] for r in baseline if finite(r.get('dac_count'))]
    if len(values)<3:raise ValueError('PZT baseline noise is unavailable')
    sigma=statistics.stdev(values)
    floor=max(context['resolution_count'],noise_multiplier*sigma)
    tolerance=max(context['tolerance_count'],floor)
    return dict(schema='ldcon.pzt-response-criteria/1',center_dac=context['center_dac'],
        reference_ff_step_count=abs(reference_ff_step_count),noise_floor_count=floor,
        baseline_stddev_count=sigma,tolerance_count=tolerance,confirmation_s=confirmation_s,
        overshoot_limit_count=max(overshoot_ratio*abs(reference_ff_step_count),floor),
        low_dac=context['low_dac'],high_dac=context['high_dac'],
        reference_revision=context.get('reference_revision'),
        basis='Frozen reference FF step, baseline DAC noise, and owner centering resolution')


def analyse(history, requested_s, end_s, p, frozen, *, direction, period_s):
    result=dict(status='inconclusive',reason='PZT evidence unavailable',settling_from_request_s=None,
                windows=[],coverage_valid=False,criteria=copy.deepcopy(frozen),
                timestamp_basis='ESP32 record receive time; no optical latency subtraction')
    if (not frozen or direction not in (-1,1) or not finite(period_s) or period_s<=0
            or history.get('integrity_errors')):return result
    raw=history.get('samples',[])
    rows=[r for r in raw if finite(r.get('received_unix_s')) and requested_s<=r['received_unix_s']<=end_s]
    if len(rows)<p['minimum_samples'] or any(not finite(r.get('dac_count')) for r in rows):
        return dict(result,reason='Missing actual PZT DAC samples')
    times=[r['received_unix_s'] for r in rows];values=[r['dac_count'] for r in rows]
    gaps=[b-a for a,b in zip(times,times[1:])]
    if (any(g<=0 or g>p['max_gap_s'] for g in gaps) or times[0]-requested_s>p['max_gap_s']
            or end_s-times[-1]>p['max_gap_s']):
        return dict(result,reason='PZT coverage or receive-time ordering invalid')
    center=frozen['center_dac'];errors=[v-center for v in values]
    result.update(coverage_valid=True,sample_count=len(rows),maximum_gap_s=max(gaps),
        overshoot_count=max(0.,max(-direction*e for e in errors)),
        minimum_dac=min(values),maximum_dac=max(values))
    result['overshoot_passes']=result['overshoot_count']<=frozen['overshoot_limit_count']
    result['headroom_passes']=all(frozen['low_dac']<=v<=frozen['high_dac'] for v in values)
    # Observe at least two complete outer periods; a short quiet interval before
    # the first slow-loop action is not evidence that the thermal loop settled.
    if end_s-requested_s<2*period_s+frozen['confirmation_s']:
        return dict(result,reason='Record does not cover two outer-loop periods and confirmation')
    width=p['evaluation_window_s'];index=0;good_start=None
    while requested_s+index*p['check_interval_s']+width<=end_s+1e-7:
        start=requested_s+index*p['check_interval_s'];stop=start+width
        window=errors[bisect_left(times,start-1e-7):bisect_right(times,stop+1e-7)]
        good=len(window)>=p['minimum_samples'] and max(map(abs,window),default=math.inf)<=frozen['tolerance_count']
        result['windows'].append(dict(start_unix_s=start,end_unix_s=stop,passes=good,
                                      peak_abs_error_count=max(map(abs,window),default=None)))
        if not good:good_start=None
        elif good_start is None:good_start=start
        index+=1
    if good_start is None or end_s-good_start<frozen['confirmation_s']:
        return dict(result,status='not_settled',reason='PZT center band not sustained to record end')
    if not result['overshoot_passes'] or not result['headroom_passes']:
        return dict(result,status='constraint_failed',reason='PZT overshoot or physical headroom criterion failed',
                    stable_start_unix_s=good_start)
    return dict(result,status='settled',reason='PZT center sustained; overshoot and headroom pass',
                stable_start_unix_s=good_start,settling_from_request_s=good_start-requested_s)


def joint(frequency, thermal, requested_s, confirmed_s, expected_settle_s):
    """Never let a frequency-only pass hide missing/unstable PZT evidence."""
    result=copy.deepcopy(frequency)
    result['frequency_settling_from_request_s']=frequency.get('settling_from_request_s')
    result['pzt_settling_from_request_s']=thermal.get('settling_from_request_s')
    result['thermal']=thermal
    if frequency.get('status')=='settled' and thermal.get('status')=='settled':
        duration=max(frequency['settling_from_request_s'],thermal['settling_from_request_s'])
        result.update(status='settled',reason='Frequency and PZT jointly settled',settling_from_request_s=duration,
                      stable_start_unix_s=requested_s+duration,
                      settling_after_apply_interval_s=[max(0.,requested_s+duration-confirmed_s),duration],
                      within_expected_settle_time=duration<=expected_settle_s)
    else:
        result.update(status='inconclusive' if 'inconclusive' in (frequency.get('status'),thermal.get('status')) else 'not_settled',
                      reason='Frequency and PZT joint criteria not met',settling_from_request_s=None,
                      stable_start_unix_s=None,settling_after_apply_interval_s=None,within_expected_settle_time=False)
    return result


def evaluate_leg(leg, p):
    """One evaluator shared by measurement, model review and adoption validation."""
    from ldcon.jobs.target_response_analysis import analyse as frequency_analysis
    a=frequency_analysis(leg['history'],leg['target_frequency_hz'],leg['requested_unix_s'],
        leg['confirmed_unix_s'],leg['record_end_unix_s'],p)
    if 'pzt_evaluation' not in leg:return a
    ctx=leg['pzt_evaluation']
    t=analyse(leg['history'],leg['requested_unix_s'],leg['record_end_unix_s'],p,
        ctx['criteria'],direction=ctx['direction'],period_s=ctx['period_s'])
    return joint(a,t,leg['requested_unix_s'],leg['confirmed_unix_s'],p['expected_settle_s'])
