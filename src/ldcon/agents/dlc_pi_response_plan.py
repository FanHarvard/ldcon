"""A frozen change/measurement program for joint PI response optimization."""
from copy import deepcopy
from ldcon.jobs.change_measure import batch_parameters
from ldcon.jobs.lock_target import check_change, resolve_target


def freeze(laser,card,overrides):
    batch=batch_parameters(card,overrides)
    steps=batch['steps']
    if len(steps)<3 or steps[0]['changes'] or steps[-1]['changes']:
        raise ValueError('Response plan needs steady windows before and after target steps')
    origin=resolve_target(laser,{})
    current=origin;offsets=[]
    for p in steps:
        if p['measurements']!={'wavelength':True,'optical_quality':False,'dither':False} or p['apply_mode']!='atomic':
            raise ValueError('PI response plan requires atomic wavelength measurement steps')
        if p['changes']:
            if len(p['changes'])!=1 or set(p['changes'][0])!={'parameter','offset'} or p['changes'][0]['parameter']!='target_frequency_hz':
                raise ValueError('Response program admits only relative target increments')
            previous=current;current+=p['changes'][0]['offset']
            document=deepcopy(laser);document['target']['frequency_hz']=previous
            check_change(document,current)
            offsets.append(current-origin)
    if not offsets or offsets[-1]!=0 or not any(offsets):raise ValueError('Response program must move and return to its starting target')
    return dict(kind='dlc_pi_target_response',steps=steps,target_program=dict(offsets_hz=offsets,max_cycles=12),
        target_frequency_hz=origin,timeout_s=batch['timeout_s'])
