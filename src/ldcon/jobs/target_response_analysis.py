"""Pure finite-record settling evaluation. No control or threshold adoption."""
import math
import statistics
from bisect import bisect_left, bisect_right

C_NM = 299792458e9


def analyse(history, target_hz, requested_s, confirmed_s, end_s, p):
    rows = [s for s in history['samples'] if requested_s <= s['time_unix_s'] <= end_s]
    result = dict(status='inconclusive', reason=None, sample_count=len(rows), target_frequency_hz=target_hz,
                  request_to_confirmation_s=confirmed_s-requested_s, windows=[],
                  settling_from_request_s=None, settling_after_apply_interval_s=None,
                  qualification='finite observed record only; not a guarantee of future stability')
    if history.get('integrity_errors'):
        result['reason'] = 'history_integrity_error'
        return result
    if len(rows) < p['minimum_samples']:
        result['reason'] = 'insufficient_samples'
        return result
    times = [r['time_unix_s'] for r in rows]
    gaps = [b-a for a, b in zip(times, times[1:])]
    result.update(median_interval_s=statistics.median(gaps), maximum_gap_s=max(gaps))
    if (times[0]-requested_s > p['max_gap_s'] or end_s-times[-1] > p['max_gap_s']
            or max(gaps) > p['max_gap_s']):
        result['reason'] = 'missing_coverage_or_sample_gap'
        return result
    errors = [C_NM/r['wavelength_nm']-target_hz for r in rows]
    sums, squares = [0.], [0.]
    for value in errors:
        sums.append(sums[-1]+value)
        squares.append(squares[-1]+value*value)
    result.update(peak_abs_error_hz=max(map(abs, errors)), final_error_hz=errors[-1])
    index = 0
    t = requested_s
    while t + p['evaluation_window_s'] <= end_s + 1e-6:
        left, right = bisect_left(times,t-1e-7), bisect_right(times,t+p['evaluation_window_s']+1e-7)
        count = right-left
        covered = (count >= p['minimum_samples'] and times[left]-t <= p['max_gap_s'] and
                   t+p['evaluation_window_s']-times[right-1] <= p['max_gap_s'])
        mean = (sums[right]-sums[left])/count if count else None
        rms = math.sqrt(max(0.,(squares[right]-squares[left])/count)) if count else None
        std = math.sqrt(max(0., rms*rms-mean*mean)) if count else None
        good = (covered and abs(mean) <= p['mean_error_max_hz'] and rms <= p['rms_error_max_hz']
                and (p['peak_error_max_hz'] is None or max(map(abs, errors[left:right])) <= p['peak_error_max_hz']))
        result['windows'].append(dict(start_unix_s=t, end_unix_s=t+p['evaluation_window_s'],
            samples=count, mean_error_hz=mean, rms_error_hz=rms, stddev_hz=std, passes=bool(good)))
        index += 1
        t = requested_s+index*p['check_interval_s']
    # A lucky early crossing followed by relapse is not settling. The accepted
    # suffix must remain qualified for the requested confirmation duration.
    windows = result['windows']
    last_bad = max((i for i, w in enumerate(windows) if not w['passes']), default=-1)
    good = windows[last_bad+1:]
    if not good or end_s-good[0]['start_unix_s'] < p['confirmation_s']:
        result.update(status='not_settled', reason='criteria_not_sustained_in_record')
        return result
    settled = good[0]['start_unix_s']
    result.update(status='settled', reason='criteria_sustained_to_record_end',
        settling_from_request_s=max(0., settled-requested_s),
        within_expected_settle_time=settled-requested_s <= p['expected_settle_s'],
        settling_after_apply_interval_s=[max(0., settled-confirmed_s), max(0., settled-requested_s)],
        stable_start_unix_s=settled,
        final_window={k:v for k,v in good[-1].items() if k!='passes'})
    return result
