"""Acquisition-window evidence, independent of waveform playback completion."""
import math
import statistics


def summarize(session, policy, tolerance_hz):
    plan = session['request']['plan']; bounds = session['trigger_bounds_s']
    if bounds is None:
        return dict(qualified=False, plateaus=[], reason='No observed trigger')
    rows = []
    first_nonzero = next((i for i,v in enumerate(plan['levels']) if v != 0), len(plan['levels']))
    for index, level in enumerate(plan['levels']):
        left = bounds[1]+plan['lead_s']+index*plan['dwell_s']+plan['settle_s']
        right = bounds[0]+plan['lead_s']+index*plan['dwell_s']+plan['settle_s']+plan['acquire_s']
        target = session['origin_target_hz']+level
        points = {r['acquired_s']:r['frequency_hz'] for r in session['samples']
                  if left < r['acquired_s'] < right and r.get('acquisition_known') is True
                  and r.get('unit') == 'Hz'
                  and type(r.get('frequency_hz')) in (int,float)
                  and math.isfinite(r['frequency_hz']) and r['frequency_hz'] > 0}
        times = sorted(points); errors = [points[t]-target for t in times]
        gaps = [b-a for a, b in zip([left, *times], [*times, right])]
        covered = (len(times) >= 2 and right > left
                   and max(gaps) <= policy['max_sample_age_s'])
        within = bool(errors) and max(map(abs, errors)) <= tolerance_hz
        control_points = [r for r in session.get('tracking_samples', [])
                          if r['index'] == index and r.get('verified')
                          and left < r['acquired_s'] < right]
        passive = index < first_nonzero
        controlled = bool(control_points)
        adc_continuity = not any(g['left_s'] < right and g['right_s'] > left
                                 for g in session.get('tracking_observation_gaps', []))
        slope = None
        if len(times) >= 2:
            center = statistics.mean(times); mean = statistics.mean(errors)
            slope = sum((t-center)*(e-mean) for t,e in zip(times,errors))/sum((t-center)**2 for t in times)
        rows.append(dict(index=index, target_hz=target, acquisition_window_s=[left,right],
            samples=len(times), covered=covered, maximum_gap_s=max(gaps),
            mean_error_hz=statistics.mean(errors) if errors else None,
            maximum_absolute_error_hz=max(map(abs,errors)) if errors else None,
            standard_deviation_hz=statistics.pstdev(errors) if errors else None,
            slope_hz_per_s=slope, within_tolerance=within,
            observation_mode='passive_pretrigger_zero' if passive else 'plateau_tracking',
            control_evidence_samples=len(control_points), tracking_observed=controlled,
            adc_continuity_confirmed=adc_continuity,
            qualified=covered and within and adc_continuity and (passive or controlled)))
    return dict(qualified=bool(session.get('playback_completed')) and all(r['qualified'] for r in rows), plateaus=rows,
        tolerance_hz=tolerance_hz, schedule_extended=False,
        timing_basis='host trigger brackets; ADC internal delay remains unqualified')
