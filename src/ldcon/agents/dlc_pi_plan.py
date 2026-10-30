"""Freeze measure.change presets for DLC agent-task parameter comparisons."""
from copy import deepcopy
from ldcon.jobs.change_measure import parameters


def is_dlc(laser):
    binding=laser.get('interfaces',{}).get('dlc_lock',{})
    return laser.get('devices',{}).get(binding.get('device'),{}).get('type')=='dlcpro.laser.owner'


def freeze(laser, deck, p, invocations):
    if not is_dlc(laser):return None
    if p.get('adjustment_measurement_cards'):
        return freeze_families(deck,p,invocations)
    if p['adjustment_family'] not in ('auto','pi') or not p['request_delegation']:
        raise ValueError('DLC adjustment currently supports a finite delegated PI task')
    card=next((c for c in deck['cards'] if c['id']==p['adjustment_plan_card_id']),None)
    if card and card['operation']=='measure.batch':
        if p['monitor_job_id']:
            raise ValueError('Background PI adjustment cannot move the optical target')
        from ldcon.agents.dlc_pi_response_plan import freeze as response_plan
        return response_plan(laser,card,invocations.get(card['id'],{}))
    if card and card['operation']!='measure.change':card=None
    if card is None:raise ValueError('Select a measure.change preset for the PI measurement window')
    plan=parameters(card,invocations.get(card['id'],{}))
    if plan['changes'] or plan['apply_mode']!='atomic' or plan['measurements']!={'wavelength':True,'optical_quality':False,'dither':False}:
        raise ValueError('PI plan must be an unchanged, atomic wavelength measurement preset')
    return dict(kind='dlc_pi_change_measure',measurement=deepcopy(plan))


def freeze_families(deck,p,invocations):
    if not p['request_delegation']:raise ValueError('DLC adjustments require a delegated decision session')
    names=p['adjustment_measurement_cards']
    if p['adjustment_family']!='auto' and p['adjustment_family'] not in names:
        raise ValueError('Selected family has no saved measurement preset')
    measurements={}
    for family, name in names.items():
        card=next((c for c in deck['cards'] if c['id']==name and c['operation']=='measure.change'),None)
        if not card:raise ValueError('Each adjustment family requires a saved measure.change preset')
        plan=parameters(card,invocations.get(name,{}))
        flags=plan['measurements']
        if (plan['changes'] or not flags['wavelength'] or flags['dither']
                or plan['apply_mode']!=('atomic' if family=='pi' else 'coordinated')
                or flags['optical_quality']!=(family!='pi')):
            raise ValueError('DLC family preset must measure wavelength and supported optical quality without predefined changes')
        measurements[family]=plan
    return dict(kind='dlc_adjustment',family=p['adjustment_family'],measurements=deepcopy(measurements))


def freeze_task(laser, deck, p, invocations):
    """One admission hook for both manual and monitor adjustment tasks."""
    dlc = freeze(laser, deck, p, invocations)
    if dlc is not None or p['monitor_job_id']:return dlc
    from ldcon.agents.agent_monitor import parameters as monitor_parameters
    card = next((c for c in deck['cards'] if c['id']==p['adjustment_plan_card_id'] and c['operation']=='agent.monitor'), None)
    if not card:raise ValueError('A saved approved adjustment plan is required')
    return monitor_parameters(card, invocations.get(card['id'], {}))
