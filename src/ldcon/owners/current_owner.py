"""Current activation and tuning commits in the process that owns the hardware."""
import copy
import time
from ldcon.core.current_settings import CurrentConditions,current_state,validate_settings
from ldcon.core.device_conditions import ConditionError,policy_digest


def activate(controller,policy_hash,settings_hash):
    if controller.config.get('current_writes_enabled') is not True:raise ValueError('Current writes are not enabled')
    guard=CurrentConditions(controller.config['current_conditions'],controller.config['control_settings'])
    if guard.digest!=policy_hash or guard.settings_digest!=settings_hash:raise ValueError('Current policy/settings differ from instrument owner')
    if getattr(controller,'current_activation',None) is not None:raise ValueError('Current control already activated for this lease')
    observed=controller.read();now=time.monotonic()
    if not observed['ld_enabled'] or not observed['tec_enabled']:raise ConditionError('LD and TEC must already be enabled')
    origin={'current_a':observed['current_a']}
    guard.check(current_state(observed),observed['current_a'],guard.settings['current']['output_update_interval_s'],origin)
    # Rule M step 3: the activation remembers the projection revision it was admitted under; a re-projection of the
    # laser's operating conditions (system owner) invalidates tuning saves against this activation.
    controller.current_activation={'guard':guard,'activation':origin,'activated_s':now,'last_write_s':now,'last_setpoint_a':observed['current_a'],
                                   'source_revision':controller.config.get('source_revision')}
    return {'activation':origin,'settings':guard.settings,'policy_digest':guard.digest,'settings_digest':guard.settings_digest,
            'source_revision':controller.config.get('source_revision')}


def apply_current(controller,request,ramp_s):
    if set(request)!={'current_a'} or ramp_s:raise ValueError('Current policy requires one scheduled current-only update')
    context=getattr(controller,'current_activation',None)
    if context is None:raise ValueError('Current control must be activated by the instrument owner')
    if controller.config.get('current_writes_enabled') is not True:raise ValueError('Current writes are not enabled')
    state=controller.validate(request);value=float(request['current_a']);now=time.monotonic()
    if abs(state['current_a']-context['last_setpoint_a'])>1e-9:raise ConditionError('External current setpoint change detected')
    context['guard'].check(current_state(state),value,now-context['last_write_s'],context['activation'])
    controller.command(f'SOUR1:CURR {value:.12g}')
    # Once sent, never use a client timestamp or retry a lost acknowledgement.
    context['last_write_s']=time.monotonic();context['last_setpoint_a']=value
    result=controller.read()
    if abs(result['current_a']-value)>6e-5:raise RuntimeError('CLD1015 current setpoint verification failed')
    context['last_setpoint_a']=result['current_a']
    return result


def save_tuning(instrument,expected_digest,proposal):
    """Legacy in-process (Hub/Controller) tuning commit. The CLD owner refuses this path: the PID is layer 2 (laser yaml).

    Rule M step 3 replaced the former "laser file conditions changed; reload owner" comparison (a re-read of the laser
    yaml) with a projection-revision fence: the activation must still belong to the projection the conditions were
    compiled from.
    """
    if set(proposal)!={'pid','output_update_interval_s'}:raise ValueError('Tuning may only change PID and output interval')
    config=instrument.config
    if config.get('current_writes_enabled') is not True:raise ValueError('Current tuning save is not enabled')
    old=config['control_settings']
    if policy_digest(old)!=expected_digest:raise ValueError('Device settings changed; refuse stale tuning')
    context=getattr(instrument.io,'current_activation',None)
    if context is None or context['guard'].settings_digest!=expected_digest:raise ValueError('No matching current activation')
    if context.get('source_revision')!=config.get('source_revision'):
        raise ValueError('CLD operating conditions were re-projected since activation (revision %s -> %s); re-activate'
                         %(context.get('source_revision'),config.get('source_revision')))
    updated=copy.deepcopy(old);updated['current'].update(copy.deepcopy(proposal));validate_settings(updated['current'])
    new_guard=CurrentConditions(config['current_conditions'],updated)
    observed=instrument.io.read()
    new_guard.check(current_state(observed),observed['current_a'],updated['current']['output_update_interval_s'],context['activation'])
    config['control_settings']=updated
    if instrument.io:
        instrument.io.config['control_settings']=copy.deepcopy(updated)
        # Keep the same activation baseline and write clock; refresh only the shared-settings guard.
        context['guard']=new_guard
    return {'settings':updated,'settings_digest':policy_digest(updated),'saved':True}
