"""Acquired WSU wavelength arrays with explicitly estimated DLC edge timing."""
import math
import statistics

from ldcon.core.records import atomic_json
from ldcon.jobs.shot_analysis import CONTRACT, block, write_status


def analyse(request, result, proxy_id):
    plan = request['plan']
    bounds = result.get('trigger_bounds_s')
    if not bounds or not result.get('samples'):
        raise ValueError('No measured trigger timing or wavelength samples')
    t0 = sum(bounds)/2
    uncertainty = (bounds[1]-bounds[0])/2
    points = {k: [] for k in ('t_unix_s', 't_rx_unix_s', 'wavelength_nm', 'frequency_hz',
        'time_relative_s', 'level_index', 'estimated_window_index', 'settled_level_index', 'in_transition', 'valid',
        'quality', 'sequence', 'error_hz', 'acquired_monotonic_s', 'received_monotonic_s')}
    groups = [[] for _ in plan['levels']]
    for row in result['samples']:
        f = row.get('frequency_hz')
        finite_frequency = type(f) in (float, int) and math.isfinite(f) and f > 0
        valid = finite_frequency and row.get('acquisition_known') is True
        t = row['acquired_s']-t0
        index = math.floor((t-plan['lead_s'])/plan['dwell_s'])
        index = min(len(groups), max(-1, index))
        local = t-plan['lead_s']-index*plan['dwell_s']
        in_window = (0 <= index < len(groups)
            and local >= plan['settle_s']+uncertainty
            and local <= plan['settle_s']+plan['acquire_s']-uncertainty)
        wavelength = 299792458e9/f if finite_frequency else None
        unix = row.get('acquired_unix_s')
        rx = unix+row['received_s']-row['acquired_s'] if unix is not None else None
        expected = result['origin_target_hz']+plan['levels'][index] if 0 <= index < len(groups) else None
        values = dict(t_unix_s=unix, t_rx_unix_s=rx, wavelength_nm=wavelength,
            frequency_hz=f, time_relative_s=t, level_index=index,
            estimated_window_index=index if valid and in_window else -1,
            settled_level_index=-1, in_transition=None, valid=valid, quality=row.get('quality'),
            sequence=row.get('sequence'), acquired_monotonic_s=row['acquired_s'],
            received_monotonic_s=row['received_s'], error_hz=f-expected if valid and expected else None)
        for key in points:
            points[key].append(values[key])
        if valid and in_window:
            groups[index].append(values)
    levels = []
    for i, group in enumerate(groups):
        wave = [r['wavelength_nm'] for r in group]
        errors = [r['error_hz'] for r in group]
        levels.append(dict(level_index=i, offset_hz=plan['levels'][i], n_settled=0, n_estimated_window=len(group),
            mean_wavelength_nm=statistics.mean(wave) if wave else None,
            std_wavelength_nm=statistics.pstdev(wave) if wave else None,
            mean_error_hz=statistics.mean(errors) if errors else None,
            start_unix_s=group[0]['t_unix_s'] if group else None,
            end_unix_s=group[-1]['t_unix_s'] if group else None))
    return dict(schema=CONTRACT, proxy_job=proxy_id, request_id=request['request_id'],
        plan_sha256=request['plan_sha256'], mode='levels', points=points, levels=levels,
        adc=result.get('adc', []), source_rows=result['samples'], source='owner WSU UDP acquisition ring',
        timing=dict(trigger_monotonic_estimate_s=t0, observation_half_interval_s=uncertainty,
                    adc_acquisition_timestamp_available=False,
                    receive_unix_time='Reconstructed from acquisition Unix time and host monotonic receive-minus-acquisition delta',
                    note='Host ADC read brackets; DLC internal ADC age is unknown. Not hardware edge timestamps.'),
        quality=dict(staircase_detected=result.get('playback_completed') is True,
                     optical_settling_verified=False, coverage=result.get('source_coverage')),
        classification='Estimated configured acquisition windows only. ADC internal delay unbounded; settled indices remain unknown (-1).' )


def save(directory, request, result):
    try:
        doc = analyse(request, result, directory.name)
        destination = directory/'shot-analysis'
        destination.mkdir(parents=True, exist_ok=True)
        atomic_json(destination/'result.json', doc)
        write_status(directory, schema=CONTRACT, state='ready', source=doc['source'], mode='levels')
        return block('ready', directory.name)
    except (KeyError, TypeError, ValueError, OSError) as error:
        write_status(directory, schema=CONTRACT, state='unavailable', reason=str(error))
        return block('unavailable', directory.name, str(error))
