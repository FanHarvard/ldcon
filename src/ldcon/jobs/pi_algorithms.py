"""Public PI operations with immutable algorithm identity.

An execution profile may select a task model, never a different controller
algorithm. Historical mixed-selector cards need an explicit migration choice.
"""
import copy
from ldcon.jobs.online_pid import parameters as internal_parameters

ALGORITHMS = {'tune.pid_coordinate_search':'adaptive_coordinate'}


def parameters(card, overrides):
    try:strategy=ALGORITHMS[card['operation']]
    except (KeyError,TypeError):raise ValueError('A fixed public PI algorithm is required') from None
    base=card.get('parameters',{})
    if not isinstance(base,dict) or not isinstance(overrides,dict):raise ValueError('PI parameters must be mappings')
    for source in (base,overrides):
        if source.get('strategy',strategy)!=strategy:
            raise ValueError('Algorithm is fixed by operation ID; choose a separate PI card')
    defaults={'window_s':10.} if strategy=='adaptive_coordinate' else {}
    return internal_parameters({'parameters':{**defaults,**copy.deepcopy(base),'strategy':strategy}},
                               {**copy.deepcopy(overrides),'strategy':strategy})


def operation_for_strategy(strategy):
    # Existing private comparators and frozen trial evidence keep their actual
    # identity. This does not admit a new public gain-reduction card.
    if strategy == 'factor_reduction':
        return 'tune.pid_factor_reduction'
    return next((operation for operation,value in ALGORITHMS.items() if value==strategy),'agent.task')
