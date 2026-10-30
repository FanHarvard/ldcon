"""Metric acquisition card: depends on quantity IDs and MetricReader, not storage."""
import math
import time
from ldcon.core.metric_quantities import QUANTITIES
from ldcon.core.interface_cards import CardCancelled

DEFAULTS=dict(quantities=['optical.frequency'],window_s=60.,sample_interval_s=1.,max_sample_age_s=5.)


def parameters(card,overrides):
    p={**DEFAULTS,**card['parameters'],**overrides}
    if set(p)!=set(DEFAULTS):raise ValueError('Unknown metric acquisition parameter')
    if not isinstance(p['quantities'],list) or not 1<=len(p['quantities'])<=16 or any(not isinstance(q,str) or q not in QUANTITIES for q in p['quantities']):raise ValueError('Specify registered physical quantity IDs')
    if len(set(p['quantities']))!=len(p['quantities']):raise ValueError('Duplicate metric quantity')
    for key in ('window_s','sample_interval_s','max_sample_age_s'):
        if type(p[key]) not in (int,float) or not math.isfinite(p[key]) or not 0<p[key]<=86400:raise ValueError('Invalid '+key)
    return p


def acquire_metrics(metrics,p,*,clock,sleep,cancel,emit,wall_clock=time.time):
    end=wall_clock();start=end-p['window_s'];counts={}
    for quantity in p['quantities']:
        if cancel():raise CardCancelled('Metric acquisition cancelled')
        result=metrics.read(quantity,start,end)
        emit('metric_binding',result['provenance'])
        counts[quantity]=len(result['samples'])
        for sample in result['samples']:
            if cancel():raise CardCancelled('Metric acquisition cancelled')
            age=end-sample['source_unix_s']
            emit('metric_sample',{**sample,'source_age_s':age,'fresh':sample['acquired_unix_s'] is not None and 0<=age<=p['max_sample_age_s'],
                                  'time_basis':result['provenance']['binding']['time_basis']})
    return dict(status='succeeded',message='Historical metric retrieval completed; inspect sample quality and freshness',
                sample_counts=counts,empty_quantities=[q for q,n in counts.items() if not n],
                start_unix_s=start,end_unix_s=end,output_writes=0)
