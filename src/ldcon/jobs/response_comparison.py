"""Shared measured comparison metrics for steady noise and target transients.

The response band is frozen from the first checkpoint window. An unobserved
settling time is censored, never reported as the observation deadline.
"""
from copy import deepcopy
import math
import statistics
from ldcon.core.contracts import DeviceError


def errors(step, phase=None):
    target=step['parameters']['target_frequency_hz']
    return [(r['measurements']['wavelength']['acquired_unix_s'],r['measurements']['wavelength']['values']['frequency_hz']-target)
            for r in step['samples'] if 'wavelength' in r['measurements'] and (phase is None or r['phase']==phase)]


def metrics(raw, steady_metrics, band=None):
    steady=[steady_metrics(s) for s in raw['steps'] if not any(c['parameter']=='target_frequency_hz' for c in s['parameters']['changes'])]
    legs=[s for s in raw['steps'] if any(c['parameter']=='target_frequency_hz' for c in s['parameters']['changes'])]
    if not steady or not legs:raise DeviceError('Both steady and step observations required')
    if band is None:
        first=errors(raw['steps'][0], 'measurement');values=sorted(abs(e) for _,e in first)
        if not values:raise DeviceError('Missing steady baseline for response criterion')
        # Empirical baseline envelope with headroom, frozen across candidates.
        band=1.2*values[min(len(values)-1,math.ceil(.99*len(values))-1)]
        amplitude=min(abs(s['application']['owner_result']['delta_frequency_hz']) for s in legs)
        if not 0<band<amplitude/4:raise DeviceError('Baseline cannot resolve this step; tune steady noise before response comparison')
    response=[]
    for step in legs:
        receipt=step['application']['owner_result'];start=receipt['confirmed_unix_s'];requested=receipt['started_unix_s']
        rows=[(t,e) for t,e in errors(step) if t>=start]
        end=step['measurement_ended_unix_s'];settling=None
        gaps=[b-a for (a,_),(b,_) in zip(rows,rows[1:])]
        if (not rows or any(g<=0 for g in gaps)
                or max([rows[0][0]-start,end-rows[-1][0],*gaps])>step['parameters']['max_sample_gap_s']):
            raise DeviceError('Incomplete response coverage from verified write to observation end')
        for i,(t,e) in enumerate(rows):
            if end-t>=2 and all(abs(v)<=band for _,v in rows[i:]):settling=t-start;break
        area=sum((b-a)*(abs(ea)+abs(eb))/2 for (a,ea),(b,eb) in zip(rows,rows[1:]))
        response.append(dict(settling_s_after_verified=settling,
            settling_s_after_request=None if settling is None else start-requested+settling,
            censored=settling is None,observed_s=end-start,absolute_error_integral_hz_s=area,
            peak_error_hz=max(abs(e) for _,e in rows),target_hz=step['parameters']['target_frequency_hz']))
    out=deepcopy(steady[0])
    out.update(mse=max(s['mse'] for s in steady),mse_se=max(s['mse_se'] for s in steady),
        peak_hz=max(s['peak_hz'] for s in steady),rms_hz=max(s['rms_hz'] for s in steady),
        integral_saturation_fraction=max(s['integral_saturation_fraction'] for s in steady),
        steady_windows=steady,response_band_hz=band,response=response,
        response_loss=statistics.mean(s['absolute_error_integral_hz_s'] for s in response),
        unsettled=sum(s['censored'] for s in response),
        interpretation='Baseline-derived absolute error envelope; finite response observation, no asymptotic guarantee')
    return out


def improved(before,trial,after):
    # Eligibility means complete evidence, not a hard RMS non-degradation rule.
    # The human explicitly wants trade-offs (e.g. +10% RMS for 2x faster response)
    # evaluated by the task model, including the repeated confirmation evidence.
    # Physical envelopes, freshness and the capture guard are enforced upstream.
    return all(math.isfinite(x[key]) for x in (before,trial,after)
               for key in ('mse','mse_se','response_loss','peak_hz'))
