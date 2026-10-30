"""Public PI cards identify an algorithm, not an overridable selector."""
import pytest
from ldcon.core.operation_registry import spec,normalize_parameters
from ldcon.core.interface_schema import parameters
from ldcon.jobs.pi_algorithms import ALGORITHMS,operation_for_strategy
from ldcon.jobs.online_pid import OnlinePIDTuner


@pytest.mark.parametrize('operation,strategy',ALGORITHMS.items())
def test_fixed_pi_algorithm_cannot_be_overridden(operation,strategy):
    card=dict(id='pi',operation=operation,uses=[],parameters={})
    result=parameters(card,{})
    assert result['strategy']==strategy and spec(operation).execution=='attached'
    assert parameters(card,result)==result
    other='factor_reduction'
    with pytest.raises(ValueError,match='fixed by operation ID'):parameters(card,{'strategy':other})
    with pytest.raises(ValueError,match='fixed by operation ID'):
        parameters(dict(card,parameters={'strategy':other}),{})


def test_old_ambiguous_operation_is_historical_only():
    with pytest.raises(ValueError,match='Retired card'):spec('tune.pid_online')
    with pytest.raises(ValueError,match='Retired card'):
        normalize_parameters(dict(operation='tune.pid_online',parameters={}),{})
    assert spec('tune.pid_online',historical=True).execution=='retired'
    assert parameters(dict(operation='tune.pid_online',parameters={'strategy':'adaptive_coordinate','window_s':10.}),{})['strategy']=='adaptive_coordinate'


@pytest.mark.parametrize('strategy,operation',[(v,k) for k,v in ALGORITHMS.items()]+[
    ('validated_candidates','agent.task'),('factor_reduction','tune.pid_factor_reduction')])
def test_events_report_actual_algorithm_identity(strategy,operation):
    events=[]
    tuner=OnlinePIDTuner(lambda:None,lambda _:None,lambda k,v:events.append(v),lambda:0.)
    tuner.active=dict(card_id='pi',parameters={'strategy':strategy})
    tuner._emit('online_tuning',{'state':'running'})
    assert events[0]['_context']['operation']==operation


def test_hidden_window_algorithm_cannot_hide_inside_the_steady_lock():
    from ldcon.jobs.ecdl_steady import parameters as steady_parameters
    from ldcon.owners.ecdl_hot_control import command
    with pytest.raises(ValueError,match='window traversal is retired'):
        steady_parameters({'parameters':{}},{'strategy':'window'})
    assert steady_parameters({'parameters':{}},{})['strategy']=='hold'
    with pytest.raises(ValueError,match='window traversal is retired'):
        command({'action':'set_strategy','strategy':'window'})
    assert command({'action':'set_strategy','strategy':'hold'})['strategy']=='hold'
