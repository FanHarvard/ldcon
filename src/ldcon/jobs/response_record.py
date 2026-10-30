"""Shared source-history acquisition over an immutable response interval.

Used by generic change/measure target presets and specialized response
optimizers. It never sends a command or changes the requested time interval.
"""
import math
import statistics


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def validated_history(history):
    """Reject invalid source values without emitting non-standard JSON evidence.

    No missing or invalid sample is imputed. Nulls and explicit integrity errors
    preserve the reason the acquisition is incomplete, including invalid optional
    source metadata, while allowing the evidence itself to be stored reliably.
    """
    invalid_numbers = 0
    def clean(value):
        nonlocal invalid_numbers
        if isinstance(value, float) and not math.isfinite(value):
            invalid_numbers += 1
            return None
        if isinstance(value, dict):
            return {key: clean(item) for key, item in value.items()}
        if isinstance(value, list):
            return [clean(item) for item in value]
        return value
    if not isinstance(history, dict) or not isinstance(history.get('samples'), list):
        return dict(samples=[], integrity_errors=['invalid_history_document'])
    result = clean(history)
    faults = list(result.get('integrity_errors') or [])
    accepted, rejected = [], 0
    for row in result['samples']:
        quantities = ('frequency_hz', 'wavelength_nm')
        if (not isinstance(row, dict) or not finite(row.get('time_unix_s'))
                or not any(key in row for key in quantities)
                or any(key in row and (not finite(row[key]) or row[key] <= 0) for key in quantities)):
            rejected += 1
        else:
            accepted.append(row)
    if rejected:
        faults.append('invalid_source_measurement')
        result['rejected_source_samples'] = rejected
    if invalid_numbers:
        faults.append('nonfinite_source_value')
    result.update(samples=accepted, integrity_errors=sorted(set(faults)))
    return result


def history_tail_complete(history, request_unix_s, end_unix_s, max_gap_s):
    samples=history.get('samples',[])
    if history.get('integrity_errors') or len(samples)<2:return False
    stamps=[row.get('time_unix_s') for row in samples if isinstance(row,dict)]
    if (len(stamps)!=len(samples) or not all(finite(stamp) for stamp in stamps)
            or any(b<=a for a,b in zip(stamps,stamps[1:]))
            or not all(finite(value) for value in (request_unix_s,end_unix_s,max_gap_s))
            or max_gap_s<=0):return False
    baseline=[r['time_unix_s'] for r in samples if r['time_unix_s']<request_unix_s]
    dt=[b-a for a,b in zip(baseline,baseline[1:]) if b>a]
    if not dt:return False
    cadence=statistics.median(dt)
    single=[v for v in dt if v<=1.5*cadence]
    allowed=min(max_gap_s,2*max(single))
    return 0<=end_unix_s-samples[-1]['time_unix_s']<=allowed


def acquire(history, start_unix_s, request_unix_s, end_unix_s, *, data_wait_s,
            max_gap_s, clock, wait, check):
    """Bounded ingestion wait; retain every source sample and query evidence.

    Source errors are typed, not echoed (provider URLs may contain credentials).
    Foreground/cancellation checks always precede the next read or wait.
    """
    deadline,attempts,data=clock()+data_wait_s,[],None
    while True:
        check()
        try:
            data=validated_history(history.read(start_unix_s,end_unix_s))
            complete=history_tail_complete(data,request_unix_s,end_unix_s,max_gap_s)
            attempts.append(dict(query_s=data.get('query_s'),samples=len(data['samples']),tail_complete=complete))
            if complete or clock()>=deadline:break
        except (OSError,ValueError) as error:
            attempts.append(dict(error_type=type(error).__name__))
            if clock()>=deadline:break
        wait(min(.25,max(0.,deadline-clock())))
    return dict(history=data or dict(samples=[],integrity_errors=['history_unavailable']),attempts=attempts)
