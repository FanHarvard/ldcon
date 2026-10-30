"""One explicit operation catalog; existing algorithms remain ordinary adapters.

Historical names are readable, but retired or unknown operations cannot execute.
The catalog describes configured capability, never a physical write grant.
"""
from dataclasses import dataclass
from typing import Callable

CARD_TYPES={'notify.teams':{},'acquire.metrics':{'metrics':'metric_reader'},'agent.resident':{},'agent.request':{},'agent.log_watch':{},'sweep.servo_modulation':{'servo_modulation':'servo_modulation'},
            'servo.native_current':{'native_current':'native_current_lock'},
            'tune.native_current':{'native_current':'native_current_lock'},
            'sweep.current_modulation':{'current_modulation':'current_modulation'},'report.grafana_dashboard':{},'commission.ecdl_pi':{'lock':'ecdl_stationary_lock'},
            'study.ecdl_current_response':{'lock':'ecdl_stationary_lock','current':'bounded_axis',
                'piezo':'bounded_axis','temperature':'bounded_axis','observation':'optical_mode_monitor','loop':'open_loop_state'},
            'study.ecdl_locked_current_map':{'lock':'ecdl_stationary_lock'},
            'tune.ecdl_window_center':{'lock':'ecdl_stationary_lock'},
            'servo.ecdl_steady_lock':{'lock':'ecdl_stationary_lock'},
            'tune.ecdl_initial_point':{'observation':'optical_mode_monitor','piezo':'bounded_axis',
                                    'temperature':'bounded_axis','current':'bounded_axis','loop':'open_loop_state'},
            'tune.pid_online':{},'monitor.wavelength':{'wavelength':'frequency_monitor'},
            'tune.temperature_once':{'wavelength':'frequency_monitor','temperature':'temperature_controller'},
            'servo.current_pid':{'wavelength':'frequency_monitor','current':'current_controller'},
            'identify.current_response':{'wavelength':'frequency_monitor','current':'current_controller'}}
CARD_TYPES.update({'agent.task':{},'agent.monitor':{},'agent.history_review':{}})
CARD_TYPES['study.ecdl_thermal_context']=dict(CARD_TYPES['tune.ecdl_initial_point'])
OPERATION_ALIASES={'acquisition.native_current':'tune.native_current',
                   'study.ecdl_window_center':'tune.ecdl_window_center'}
ECDL_OPEN_PORTS={'observation':'optical_mode_monitor','piezo':'bounded_axis',
                'temperature':'bounded_axis','current':'bounded_axis','loop':'open_loop_state'}
# prepare.ecdl_recovery (2026-10-27, rule F): plan from a mode map + perturbation test in ONE card -> ONE artifact
# that the steady lock (recovery_ref) and the standalone recover.ecdl_mode_hop card execute.
for operation in ('study.ecdl_mode_map','test.ecdl_recovery','tune.ecdl_operating_point','recover.ecdl_mode_hop','tune.ecdl_branch_hop','prepare.ecdl_recovery'):
    CARD_TYPES[operation]=dict(ECDL_OPEN_PORTS)
CARD_TYPES['plan.ecdl_recovery']={}
RETIRED_OPERATIONS={
    'tune.pid_online':'agent.task with adjustment_family=pi; tune.pid_coordinate_search where the controller supports it',
    'agent.log_watch':'agent.monitor',
    'tune.ecdl_initial_point':'tune.ecdl_operating_point',
    'study.ecdl_thermal_context':'study.ecdl_mode_map',
    'study.ecdl_current_response':'study.ecdl_mode_map / tune.ecdl_window_center',
    'study.ecdl_locked_current_map':'tune.ecdl_window_center',
    # 2026-10-27 (human decision): plan and test merged into ONE recovery card; old artifacts stay readable.
    'plan.ecdl_recovery':'prepare.ecdl_recovery (plan + test merged into one recovery card, 2026-10-27)',
    'test.ecdl_recovery':'prepare.ecdl_recovery (plan + test merged into one recovery card, 2026-10-27)'}
LEGACY_CARD_TYPES={name:CARD_TYPES.pop(name) for name in RETIRED_OPERATIONS}
CARD_TYPES['analyze.shot_sweep']={}   # rule F one-shot, read-only post-shot analysis (2026-10-23)
CARD_TYPES['tune.sweep_tracking']={}   # rule F one-shot: sweep tracking assessment + mechanical recommendation (2026-10-23)
CARD_TYPES['optimize.sweep_feedforward']={}
# Rule F generic cards (2026-10-22): one card for every laser; they open what the laser needs at run time
# (an open-loop owner session when unlocked, the running lock job's request queue when locked).
from ldcon.jobs.pi_algorithms import ALGORITHMS as PI_ALGORITHMS
CARD_TYPES.update({name:{} for name in PI_ALGORITHMS})
# Retire public admission only. Frozen history and private owner-backed
# comparisons remain readable; the reduction helper is not a tuning policy.
RETIRED_OPERATIONS['tune.pid_factor_reduction']='agent.task with adjustment_family=pi; measure.change for explicit PI comparisons'
LEGACY_CARD_TYPES['tune.pid_factor_reduction']={}
CARD_TYPES['tune.operating_point']={}
CARD_TYPES['measure.change']={}
CARD_TYPES['measure.batch']={}
CARD_TYPES['tune.lock_target']={}
RETIRED_OPERATIONS['study.target_response']='measure.target_response / optimize.settling_criteria / optimize.target_feedforward / optimize.target_thermal / optimize.target_step'
LEGACY_CARD_TYPES['study.target_response']={}
from ldcon.jobs.response_algorithms import ALGORITHMS as RESPONSE_ALGORITHMS
CARD_TYPES.update({name:{} for name in RESPONSE_ALGORITHMS})
CARD_TYPES['tune.pi_response']={}
# Rule I (2026-10-22): device settings through the instrument registry (owners do every instrument check).
CARD_TYPES['tune.device_parameters']={}
CARD_TYPES['laser.emission']={}
CARD_TYPES['identify.axis_frequency']={'observation':'optical_mode_monitor','axis':'bounded_axis','loop':'open_loop_state'}
CARD_TYPES['servo.dlcpro_frequency']={'dlc_lock':'dlcpro_frequency_control'}
CARD_TYPES['sweep.dlcpro_frequency']={'dlc_lock':'dlcpro_frequency_control'}
CARD_TYPES['sweep.dlcpro_awg']={}


_OBSERVERS = frozenset({
    'monitor.wavelength', 'report.grafana_dashboard',
    'agent.resident', 'agent.request', 'agent.log_watch', 'agent.task', 'agent.monitor', 'agent.history_review', 'acquire.metrics',
    'notify.teams', 'analyze.shot_sweep', 'tune.sweep_tracking',
})


def _domain_parameters(card, overrides):
    from ldcon.core.interface_schema import _parameters
    return _parameters(card, overrides)


def _domain_readiness(laser, deck, card, overrides):
    from ldcon.core.interface_schema import _readiness
    return _readiness(laser, deck, card, overrides)


@dataclass(frozen=True)
class OperationSpec:
    operation_id: str
    ports: tuple
    execution: str
    cancellation: str
    version: str = '1'
    parameter_validator: Callable = _domain_parameters
    readiness_validator: Callable = _domain_readiness

    def describe(self):
        return dict(operation_id=self.operation_id, version=self.version,
                    uses=dict(self.ports), execution=self.execution,
                    cancellation=self.cancellation,
                    parameter_validator=self.parameter_validator.__name__,
                    physical_authority='owner revalidation required')


_SPECS = {}
for _name, _ports in CARD_TYPES.items():
    _execution = ('observer' if _name in _OBSERVERS else 'attached' if _name in PI_ALGORITHMS
                  else 'job_or_attached' if _name == 'tune.ecdl_window_center' else 'job')
    _cancel = ('cancel_subordinate_preserve_parent' if _execution in ('attached', 'job_or_attached')
               or _name.startswith('sweep.') or _name in ('tune.operating_point', 'measure.change', 'measure.batch', 'tune.lock_target', 'tune.pi_response')
               else 'cooperative_owner_cleanup')
    if _name in PI_ALGORITHMS:
        from ldcon.jobs.pi_algorithms import parameters as pi_parameters
        _SPECS[_name] = OperationSpec(_name, (), 'attached', 'cancel_subordinate_preserve_parent', parameter_validator=pi_parameters)
    elif _name == 'sweep.dlcpro_awg':
        from ldcon.jobs.dlcpro_awg_card import parameters as awg_parameters, readiness as awg_readiness
        _SPECS[_name] = OperationSpec(_name, (), 'attached', 'cancel_subordinate_preserve_parent',
                                    parameter_validator=awg_parameters, readiness_validator=awg_readiness)
    elif _name == 'optimize.sweep_feedforward':
        from ldcon.jobs.sweep_ff_plan import parameters as sweep_ff_parameters, readiness as sweep_ff_readiness
        _SPECS[_name] = OperationSpec(_name, (), 'job', 'cancel_subordinate_preserve_parent',
                                    parameter_validator=sweep_ff_parameters, readiness_validator=sweep_ff_readiness)
    elif _name in RESPONSE_ALGORITHMS:
        from ldcon.jobs.response_algorithms import parameters as response_parameters, readiness as response_readiness
        _SPECS[_name] = OperationSpec(_name, (), 'job', 'cancel_subordinate_preserve_parent', parameter_validator=response_parameters, readiness_validator=response_readiness)
    else:
        _SPECS[_name] = OperationSpec(_name, tuple(_ports.items()), _execution, _cancel,
            version='2' if _name in ('recover.ecdl_mode_hop','prepare.ecdl_recovery','tune.ecdl_operating_point','tune.ecdl_window_center') else '1')
OBSERVERS = frozenset(name for name, value in _SPECS.items() if value.execution == 'observer')


def spec(operation_id, *, historical=False):
    operation_id = OPERATION_ALIASES.get(operation_id, operation_id)
    if operation_id in RETIRED_OPERATIONS:
        if historical:
            return OperationSpec(operation_id, tuple(LEGACY_CARD_TYPES[operation_id].items()),
                                 'retired', 'historical_read_only')
        raise ValueError('Retired card: ' + RETIRED_OPERATIONS[operation_id])
    try:
        return _SPECS[operation_id]
    except (KeyError, TypeError):
        raise ValueError('Unknown operation: ' + str(operation_id)) from None


def definitions():
    return [value.describe() for value in _SPECS.values()]


def normalize_parameters(card, overrides, *, historical=False):
    registration = spec(card['operation'], historical=historical)
    return registration.parameter_validator({**card, 'operation': registration.operation_id}, overrides)


def validate_card(laser, deck, card, overrides=None, *, historical=False):
    registration = spec(card['operation'], historical=historical)
    return registration.readiness_validator(laser, deck, {**card, 'operation': registration.operation_id}, overrides)


def admission_class(laser, card, active_jobs=()):
    operation_id = card.get('operation', card.get('card'))
    registration = spec(operation_id)
    op = registration.operation_id
    if registration.execution == 'observer':
        return 'observer'
    if op in RESPONSE_ALGORITHMS or op == 'optimize.sweep_feedforward':return 'proxy'
    if op in ('tune.lock_target', 'tune.device_parameters', 'laser.emission', 'study.target_response', 'tune.pi_response'):
        return 'proxy'
    if op in ('measure.change', 'measure.batch') and 'dlc_lock' in laser.get('interfaces', {}):
        return 'exclusive'
    if op in ('tune.operating_point', 'measure.change', 'measure.batch'):
        from ldcon.jobs.operating_point import lock_operation
        from ldcon.web.web_runtime import TERMINAL
        try:
            lock_op = lock_operation(laser)
        except (ValueError, KeyError, TypeError):
            return 'exclusive'
        if any(job.get('status') not in TERMINAL and any(
                c.get('operation', c.get('card')) == lock_op for c in job.get('cards', ()))
                for job in active_jobs):
            return 'proxy'
    if op == 'sweep.dlcpro_awg':return 'proxy'
    if op == 'sweep.dlcpro_frequency':
        return 'exclusive'
    if op.startswith('sweep.'):
        from ldcon.jobs.native_ff_proxy import feedforward_route
        from ldcon.jobs.ecdl_locked_sweep_proxy import rig_locked_route
        role = card['uses'][0]
        if feedforward_route(laser, role) or rig_locked_route(laser, role):
            return 'proxy'
    return 'exclusive'
