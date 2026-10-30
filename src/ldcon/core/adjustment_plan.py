"""Typed approved adjustment envelopes shared by monitoring and finite measurements."""
import copy
import math


def approved_adjustment_plan(value):
    """Validate an envelope without coupling its authority to a monitor card.

    The host freezes this saved plan; run arguments only select candidates within
    it. Calling this validator never creates a grant or authorizes physical I/O.
    """
    b = copy.deepcopy(value)
    if isinstance(b, dict) and 'controller_operation' in b:
        from ldcon.agents.dlc_monitor_grant import validate_bounds
        return validate_bounds(b)
    if not isinstance(b, dict) or type(b.get('enabled')) is not bool:
        raise ValueError('Invalid adjustment plan')
    if set(b) - {'enabled', 'half_span_v', 'handoff_timeout_s', 'max_points', 'timeout_s','families','pid_parameters','adjustment_parameters'}:
        raise ValueError('Unknown adjustment plan field')
    from ldcon.core.adjustment_registry import FAMILIES,PARAMETER_FAMILIES,parameters as trial_parameters
    families=b.get('families',['current'])
    if not isinstance(families,list) or not families or any(not isinstance(f,str) for f in families) or len(set(families))!=len(families) or set(families)-FAMILIES:
        raise ValueError('Unknown adjustment family')
    if 'pi' in families:
        from ldcon.jobs.background_pid_session import background_parameters as pid_parameters
        if 'pid_parameters' not in b:raise ValueError('PI adjustment requires an explicit approved trial plan')
        b['pid_parameters']=pid_parameters({'parameters':b['pid_parameters']},{})
        if 'candidate' in b['pid_parameters']:
            raise ValueError('An approved PI plan cannot preselect a runtime candidate')
        if b['pid_parameters']['save_settings']:
            raise ValueError('PI adjustment adoption is runtime-only; save_settings must be false')
    plans=b.get('adjustment_parameters',{})
    expected=set(families)&set(PARAMETER_FAMILIES)
    if not isinstance(plans,dict) or set(plans)!=expected:
        raise ValueError('Each selected parameter family requires its explicit comparison plan')
    if plans:
        b['adjustment_parameters']={f:trial_parameters(v,f) for f,v in plans.items()}
        if any('candidate' in v for v in plans.values()):
            raise ValueError('An approved plan grants comparison bounds; a run chooses the candidate')
    if b['enabled']:
        required={'handoff_timeout_s','timeout_s'} | ({'half_span_v','max_points'} if 'current' in families else set())
        if not required <= b.keys():
            raise ValueError('Adjustment plan requires explicit approved bounds')
        for key, maximum in (('half_span_v', 1000), ('handoff_timeout_s', 600), ('timeout_s', 86400)):
            if key not in b:continue
            if type(b[key]) not in (int, float) or not math.isfinite(b[key]) or not 0 < b[key] <= maximum:
                raise ValueError('Invalid adjustment ' + key)
        if 'max_points' in b and (type(b['max_points']) is not int or not 1 <= b['max_points'] <= 1000):
            raise ValueError('Invalid adjustment point budget')
    return b
