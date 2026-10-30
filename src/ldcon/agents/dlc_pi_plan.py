"""Freeze the existing measure.change preset for DLC agent-task PI comparisons."""
from copy import deepcopy
from ldcon.jobs.change_measure import parameters


def is_dlc(laser):
    binding=laser.get('interfaces',{}).get('dlc_lock',{})
    return laser.get('devices',{}).get(binding.get('device'),{}).get('type')=='dlcpro.laser.owner'


def freeze(laser, deck, p, invocations):
    if not is_dlc(laser):return None
    if p['adjustment_family']!='pi' or p['monitor_job_id'] or not p['request_delegation']:
        raise ValueError('DLC adjustment currently supports a finite delegated PI task')
    card=next((c for c in deck['cards'] if c['id']==p['adjustment_plan_card_id']),None)
    if card and card['operation']=='measure.batch':
        from ldcon.agents.dlc_pi_response_plan import freeze as response_plan
        return response_plan(laser,card,invocations.get(card['id'],{}))
    if card and card['operation']!='measure.change':card=None
    if card is None:raise ValueError('Select a measure.change preset for the PI measurement window')
    plan=parameters(card,invocations.get(card['id'],{}))
    if plan['changes'] or plan['apply_mode']!='atomic' or plan['measurements']!={'wavelength':True,'optical_quality':False,'dither':False}:
        raise ValueError('PI plan must be an unchanged, atomic wavelength measurement preset')
    return dict(kind='dlc_pi_change_measure',measurement=deepcopy(plan))
