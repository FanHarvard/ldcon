"""Admit a finite operating-point move within the current approved window envelope."""
import math
import time
from ldcon.jobs.window_actions import WindowQueue
from ldcon.core.records import read_json


def validate(directory,parameters,*,wall_clock=time.time):
    capability=read_json(directory/'window-controller.json',{})
    stamp=capability.get('updated_unix_s')
    if capability.get('status')!='running' or type(stamp) not in (int,float) or not math.isfinite(stamp) or not 0<=wall_clock()-stamp<=30:
        raise ValueError('Current approved window capability is unavailable or stale')
    selected=capability.get('selected_parameters')
    if not isinstance(selected,dict) or selected.get('controller_card_id')!=parameters.get('controller_card_id'):
        raise ValueError('Operating-point controller differs from approved window capability')
    if parameters.get('mode')!='position':raise ValueError('Expected finite operating-point mode')
    # Only the operational mode and a within-envelope offset may differ. No
    # widening of span/step/deadline/metrics/quality or owner guards is possible.
    ignored={'mode','position_offset_fraction'}
    if {k:v for k,v in parameters.items() if k not in ignored}!={k:v for k,v in selected.items() if k not in ignored}:
        raise ValueError('Operating-point move must retain the complete approved window parameters')
    if abs(parameters['position_offset_fraction'])>selected['max_span_fraction']:
        raise ValueError('Operating-point offset exceeds approved span')
    if any(row['status'] in ('queued','running','unknown') for row in WindowQueue(directory).list()):
        raise ValueError('Finish or cancel existing window work before an operating-point move')
    return dict(capability_revision=capability['capability_revision'],guard_revision=capability['guard_revision'])
