"""Measured armed-idle preparation; all correction is before DLC ARC enable."""
from copy import deepcopy
import math
import statistics


def observe(read, config, checkpoint, pause, rows, *, phase):
    """Observe a stationary window within the existing arm/heartbeat deadlines.

    Fine inputs expose host request brackets, not independent acquisition clocks.
    The window spans adc_max_interval_s with at least three separate reads. A
    changing input restarts only the read window, never a waveform or a deadline.
    """
    window = []
    while True:
        checkpoint()
        try:
            raw = read()
        except Exception as error:
            rows.append(dict(phase=phase, read_error=type(error).__name__, reason=str(error)))
            raise
        value = raw['value_v']
        finite = type(value) in (int, float) and math.isfinite(value)
        row = dict(raw, phase=phase, finite=finite,
                   value_v=value if finite else None,
                   error_v=value-config['adc_zero_v'] if finite else None)
        if not finite:
            row['nonfinite_value'] = str(value)
        rows.append(row)
        if not finite:
            raise ValueError('Nonfinite armed idle input; no ARC enable')
        gain = config['adc_v_per_awg_v']
        limits = config['awg']
        if not (limits['min_v']*gain-config['adc_tolerance_v'] <= value-config['adc_zero_v']
                <= limits['max_v']*gain+config['adc_tolerance_v']):
            raise ValueError('Armed idle input exceeds the approved electrical envelope')
        if (row['completed_s']-row['started_s'] > config['adc_max_interval_s']):
            raise ValueError('Armed idle read exceeds the ADC observation interval')
        if window and row['completed_s']-window[-1]['started_s'] > config['adc_max_interval_s']:
            window = []
        window.append(row)
        # Never turn an observed transition into a stationary zero estimate.
        if max(r['value_v'] for r in window)-min(r['value_v'] for r in window)>config['adc_tolerance_v']:
            window = [row]
        if (len(window) >= 3
                and row['completed_s']-window[0]['started_s'] >= config['adc_max_interval_s']):
            return dict(samples=deepcopy(window), value_v=statistics.median(r['value_v'] for r in window),
                        started_s=window[0]['started_s'], completed_s=row['completed_s'],
                        acquisition_timestamp_available=False)
        pause(config['adc_max_interval_s']/2)


def at_zero(window, config):
    return all(abs(r['value_v']-config['adc_zero_v']) <= config['adc_tolerance_v']
               for r in window['samples'])


def corrected_proof(config, proof, policy, correction_v):
    """Validate both original and corrected envelopes before a single mutation."""
    if not math.isfinite(correction_v):
        raise ValueError('Nonfinite armed idle correction')
    old = proof['voltage_envelope_v']
    shifted = [v+correction_v for v in old]
    limits = config['awg']
    if not all(limits['min_v'] <= v <= limits['max_v'] for v in shifted):
        raise ValueError('Armed idle correction exceeds the approved AWG envelope')
    factor = proof['pzt_factor_v_per_v']
    pzt = [v+factor*correction_v for v in proof['pzt_envelope_v']]
    ff = policy['ff_factor_ma_per_v'] if policy['ff_enabled'] else 0.
    current = [v+ff*factor*correction_v for v in proof['total_current_envelope_ma']]
    for axis, values in (('piezo',pzt),('current',current)):
        lo, hi = policy['axes'][axis]['bounds']
        if not all(lo <= v <= hi for v in values):
            raise ValueError('Armed idle correction exceeds the approved '+axis+' envelope')
    result = deepcopy(proof)
    result.update(nominal_voltage_envelope_v=list(old), voltage_envelope_v=shifted,
        zero_offset_correction_v=correction_v,
        admitted_voltage_envelope_v=[min(old+shifted),max(old+shifted)],
        pzt_envelope_v=[min(proof['pzt_envelope_v']+pzt),max(proof['pzt_envelope_v']+pzt)],
        total_current_envelope_ma=[min(proof['total_current_envelope_ma']+current),
                                   max(proof['total_current_envelope_ma']+current)])
    return result
