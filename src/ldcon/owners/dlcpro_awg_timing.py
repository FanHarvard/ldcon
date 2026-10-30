"""Conservative host-observed plateau checks; no invented ADC acquisition clock."""
import math


def observe_plateau(session, config, adc):
    plan=session['request']['plan']
    low,high=session['trigger_bounds_s']
    # The sample must fit strictly inside a plateau for both possible trigger
    # observations. The unknown internal ADC delay remains separately reported.
    for index,level in enumerate(plan['levels']):
        left=high+plan['lead_s']+index*plan['dwell_s']
        right=low+plan['lead_s']+(index+1)*plan['dwell_s']
        if adc['started_s'] > left+1e-9 and adc['completed_s'] < right-1e-9:
            expected=level/config['frequency_hz_per_input_v']*config.get('adc_v_per_awg_v',1.)+config['adc_zero_v']
            if abs(adc['value_v']-expected)>config['adc_tolerance_v']:
                return False
            session['level_observations'][index]+=1
            break
    return True


def source_coverage(session):
    rows=session['samples']
    stamps=[row['acquired_s'] for row in rows]
    trigger=session['trigger_s'];duration=session['request']['plan']['duration_s']
    if not stamps:
        return dict(sample_count=0,available=False,complete=False,reason='no acquired frequency samples')
    gaps=[b-a for a,b in zip(stamps,stamps[1:])]
    return dict(sample_count=len(rows),available=True,
        first_acquired_s=stamps[0],last_acquired_s=stamps[-1],
        maximum_gap_s=max(gaps,default=None),
        covers_estimated_playback=trigger is not None and stamps[0]<=trigger and stamps[-1]>=trigger+duration,
        acquisition_timestamps_preserved=True,
        complete=None,
        completeness_note='Source packets can be missing; timestamp coverage is not proof of zero packet loss')
