"""Public algorithm identities for response measurement and optimization.

The shared response recorder is an implementation helper. No public operation
can change its experiment-selection algorithm by overriding optimization_goal.
"""
import copy

ALGORITHMS = {
    'measure.target_response': ('settling_criteria', 'deterministic', False),
    'optimize.settling_criteria': ('settling_criteria', 'agent', False),
    'optimize.target_feedforward': ('settling_and_feedforward', 'agent', False),
    'optimize.target_thermal': ('settling_feedforward_and_tec', 'agent', False),
    'optimize.target_step': ('settling_criteria', 'deterministic', True),
}


def parameters(card, overrides):
    from ldcon.jobs.target_response import parameters as validate
    operation=card['operation']
    goal, execution, search=ALGORITHMS[operation]
    given={**card.get('parameters',{}),**overrides}
    for key, expected in (('optimization_goal',goal),('execution_mode',execution)):
        if key in given and given[key]!=expected:
            raise ValueError('Select a different operation to change '+key)
    if not search and (given.get('shift_hz') is not None or given.get('shift_search') is not None):
        raise ValueError('Step-width search requires optimize.target_step')
    if search and given.get('shift_hz') is None:
        raise ValueError('Step-width optimization needs an explicit shift_hz')
    return validate({'parameters':{**given,'optimization_goal':goal,'execution_mode':execution}}, {})


# Policy mutation is a narrower capability than response observation.
POLICY_OPERATIONS = frozenset(('optimize.target_feedforward', 'optimize.target_thermal'))


def readiness(laser, deck, card, overrides):
    from ldcon.core.contracts import DeviceError
    from ldcon.jobs.target_response import validate_laser
    p = parameters(card, overrides or {})
    issues = []
    try:
        validate_laser(laser, p)
        if p['execution_mode'] == 'agent' and not any(c['id'] == p['agent_card_id'] and c['operation'] == 'agent.resident' for c in deck['cards']):
            issues.append('Study agent reference must name a gateway card in this deck')
    except (ValueError, TypeError, KeyError, DeviceError) as error:
        issues.append(str(error))
    return dict(runnable=not issues, blocked_reasons=issues, parameters=p)
