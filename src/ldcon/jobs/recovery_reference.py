"""Branch-independent recovery evidence. No clocks, hardware or map lookup."""
import copy
import math
import statistics


# Draft placeholders only. A reference must carry its resolved observation
# policy; executable history windows never acquire numerical defaults here.
DEFAULTS = dict(lookback_s=None, stable_s=None, min_samples=None, max_gap_s=None,
                guard_s=None, max_stddev_hz=None, max_slope_hz_per_s=None)


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def parameters(value):
    if not isinstance(value, dict) or set(value)-set(DEFAULTS):
        raise ValueError('Invalid recovery reference settings')
    missing = sorted(key for key in DEFAULTS if value.get(key) is None)
    if missing:
        raise ValueError('Explicit recovery reference settings required: '+', '.join(missing))
    p = {**DEFAULTS, **value}
    for key in set(DEFAULTS)-{'min_samples'}:
        if not finite(p[key]) or p[key] < 0 or key != 'guard_s' and p[key] == 0:
            raise ValueError('Explicit positive recovery reference setting required: '+key)
    if type(p['min_samples']) is not int or not 3 <= p['min_samples'] <= 100000:
        raise ValueError('Recovery reference needs at least three samples')
    if p['lookback_s'] > 86400 or p['stable_s']+p['guard_s'] > p['lookback_s']:
        raise ValueError('Recovery reference window exceeds lookback')
    return p


def infer(history, event_time_s, epoch, settings):
    """Latest contiguous stable pre-event window, frozen before any recovery write.

    Samples use one declared clock and operation epoch. An intentional target or
    sweep change ends the old epoch. No stable window means no recovery authority.
    Frequency is an observed return reference, never a physical mode identity.
    """
    p = parameters(settings)
    if not finite(event_time_s) or not isinstance(epoch, str) or not epoch:
        raise ValueError('Recovery event time and operation epoch required')
    rows = []
    previous = None
    for row in history:
        if not isinstance(row, dict):
            raise ValueError('Recovery history must contain observation mappings')
        stamp = row.get('acquired_s')
        if not finite(stamp) or previous is not None and stamp <= previous:
            raise ValueError('Recovery history must use ordered distinct timestamps')
        previous = stamp
        if event_time_s-p['lookback_s'] <= stamp <= event_time_s-p['guard_s']:
            rows.append(row)
    # Newest qualifying interval wins, but invalid/foreign-epoch rows split it.
    for end in range(len(rows)-1, -1, -1):
        window = []
        for start in range(end, -1, -1):
            row = rows[start]
            if (row.get('operation_epoch') != epoch or row.get('intentional_operation', False)
                    or not finite(row.get('frequency_hz')) or row['frequency_hz'] <= 0
                    or row.get('valid', True) is not True):
                break
            if window and window[-1]['acquired_s']-row['acquired_s'] > p['max_gap_s']:
                break
            window.append(row)
            duration = window[0]['acquired_s']-row['acquired_s']
            if duration < p['stable_s'] or len(window) < p['min_samples']:
                continue
            values = [r['frequency_hz'] for r in window]
            times = [r['acquired_s']-row['acquired_s'] for r in window]
            mean_t = statistics.mean(times)
            mean_f = statistics.mean(values)
            xx = sum((t-mean_t)**2 for t in times)
            slope = sum((t-mean_t)*(f-mean_f) for t, f in zip(times, values))/xx if xx else math.inf
            deviation = statistics.pstdev(values)
            if deviation > p['max_stddev_hz'] or abs(slope) > p['max_slope_hz_per_s']:
                break
            outputs = window[0].get('outputs', {})
            if not all(finite(outputs.get(k)) for k in ('current', 'temperature', 'piezo')):
                break
            return dict(frequency_hz=statistics.median(values), operation_epoch=epoch,
                        start_s=row['acquired_s'], end_s=window[0]['acquired_s'],
                        samples=len(window), stddev_hz=deviation, slope_hz_per_s=slope,
                        outputs=copy.deepcopy(outputs), inferred_before_event_s=event_time_s,
                        physical_mode_identity_verified=False)
    raise ValueError('No stable pre-hop reference in this operation epoch')


def select(reference, fallback, specializations):
    """Select at most one applicable parameter specialization; ambiguity uses fallback.

    Entries specialize one relative-current-route algorithm, never another recovery
    algorithm. Selection is descriptive; owner limits are checked separately.
    """
    matches = []
    for item in specializations:
        band = item['frequency_range_hz']
        context = item.get('context', {})
        if (band[0] <= reference['frequency_hz'] <= band[1]
                and all(bounds[0] <= reference['outputs'][axis] <= bounds[1]
                        for axis, bounds in context.items())):
            matches.append(item)
    if len(matches) == 1:
        return copy.deepcopy(matches[0]['route']), dict(kind='specialization', id=matches[0]['id'],
                                                      evidence=matches[0].get('evidence'))
    return copy.deepcopy(fallback), dict(kind='fallback', reason='ambiguous' if matches else 'no applicable specialization',
                                         candidates=[x['id'] for x in matches])


def observation(row, now_s):
    """Normalize one adapter observation to monotonic time, preserving source provenance."""
    row = copy.deepcopy(row)
    if row.get('acquisition_clock') == 'unix':
        acquired = row.get('acquired_unix_s', row.get('acquired_s'))
        recorded = row.get('recorded_unix_s')
        if not finite(acquired) or not finite(recorded) or recorded < acquired:
            raise ValueError('Cannot align recovery observation source clock')
        row['source_acquired_s'] = acquired
        row['source_acquisition_clock'] = 'unix'
        row['acquired_s'] = now_s-(recorded-acquired)
        row['acquisition_clock'] = 'monotonic'
    return row
