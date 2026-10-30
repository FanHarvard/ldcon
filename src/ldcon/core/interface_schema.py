"""Separate laser/device policies, reusable interface decks, and assignments."""
from ldcon.core.state_layout import config_root, config_file

import copy
import hashlib
import json
import math
from pathlib import Path
import re
import yaml
from ldcon.core.deck_yaml import DeckLoader,MAX_YAML_BYTES,dump_document
from ldcon.core.device_conditions import TemperatureConditions,ConditionError,finite
from ldcon.core.contracts import DeviceError

from ldcon.core.operation_registry import (CARD_TYPES, LEGACY_CARD_TYPES, OPERATION_ALIASES,
                                 RETIRED_OPERATIONS, ECDL_OPEN_PORTS)

def card_interfaces(operation):
    operation=OPERATION_ALIASES.get(operation,operation)
    return CARD_TYPES.get(operation,LEGACY_CARD_TYPES.get(operation))
DEVICE_CAPABILITIES={'dlcpro.laser.owner':{'optical_mode_monitor','bounded_axis','open_loop_state','dlcpro_frequency_control'},'metrics.router':{'metric_reader'},'metrics.victoriametrics':{'metric_reader'},'metrics.sqlite':{'metric_reader'},'ecdl.rig.owner':{'optical_mode_monitor','bounded_axis','open_loop_state','ecdl_stationary_lock','servo_modulation'},
                     'esp32servocon.native_current.owner':{'native_current_lock'},
                     'fizeau.owner':{'frequency_monitor','scalar_sensor'},
                     'fizeau.http':{'frequency_monitor','scalar_sensor'},
                     'thorlabs.cld1015.broker':{'temperature_controller','current_controller','slow_actuator'},
                     'siglent.sdg2000x.broker':{'current_modulation'},
                     'awg.external_modulation.broker':{'current_modulation','servo_modulation'}}
MONITOR_DEFAULTS={'duration_s':60.,'sample_interval_s':.5,'max_sample_age_s':1.}
SEEK_DEFAULTS={'target_frequency_hz':None,'target_wavelength_nm':None,'tolerance_hz':None,
    'requested_slew_c_per_s':None,'approach_time_s':10.,
    'gain':.25,'settle_tolerance_c':.001,'stable_samples':3,'sample_interval_s':.5,
    'max_sample_age_s':1.,'measurement_recovery_timeout_s':15.,'measurement_recovery_samples':3,'max_initial_detuning_hz':1e12,'timeout_s':600.,'max_excursion_c':None,'identify_slope':True,
    'probe_step_c':.02,'probe_settle_timeout_s':60.,'probe_samples':5,
    'probe_min_temperature_delta_c':.005,'probe_min_frequency_delta_hz':1e6,'probe_snr':5.}


def identifier(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}',value):raise ValueError('Invalid configuration ID')
    return value


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,allow_nan=False).encode()).hexdigest()


def load_mapping(text):
    if not isinstance(text,str) or len(text.encode())>MAX_YAML_BYTES:raise ValueError('Configuration exceeds 256 KiB')
    try:
        data=yaml.load(text,Loader=DeckLoader);json.dumps(data,allow_nan=False)
        if not isinstance(data,dict):raise ValueError('Expected one configuration mapping')
        return data
    except (yaml.YAMLError,TypeError,OverflowError) as e:raise ValueError(str(e)) from e


def _fields(data,required,optional=()):
    if not isinstance(data,dict) or set(data)-set(required)-set(optional) or set(required)-set(data):raise ValueError('Invalid configuration fields')
    if data.get('schema_version',1)!=1 or type(data.get('schema_version',1)) is not int:raise ValueError('Expected schema_version 1')


def validate_deck(data):
    _fields(data,{'schema_version','id','name','interfaces','cards'})
    identifier(data['id'])
    if not isinstance(data['name'],str) or not data['name']:raise ValueError('Deck name is required')
    if not isinstance(data['interfaces'],dict):raise ValueError('Deck interfaces are required')
    capabilities=set().union(*DEVICE_CAPABILITIES.values()) | {'optical_mode_monitor','bounded_axis','open_loop_state'}
    if any(cap not in capabilities for cap in data['interfaces'].values()):raise ValueError('Decks declare interface capabilities, never devices')
    if not isinstance(data['cards'],list) or not 1<=len(data['cards'])<=64:raise ValueError('Expected 1..64 cards')
    ids=set()
    for card in data['cards']:
        card['operation']=OPERATION_ALIASES.get(card['operation'],card['operation'])
        if card['operation'] in ('study.ecdl_locked_current_map','tune.ecdl_window_center'):
            target=parameters(card,{})['controller_card_id']
            if not any(c['id']==target and c['operation']=='servo.ecdl_steady_lock' for c in data['cards']):raise ValueError('Online ECDL study must reference an ECDL stationary card')
        _fields(card,{'id','operation','uses','parameters'},{'fallback','agent_execution'})
        identifier(card['id'])
        if 'agent_execution' in card:
            from experiment_core.agent_contracts import execution_policy
            execution_policy(card['agent_execution'])
            if card['operation'] not in ('agent.task','agent.history_review','prepare.ecdl_recovery','study.target_response','optimize.settling_criteria','optimize.target_feedforward','optimize.target_thermal'):
                raise ValueError('Agent execution policy belongs on a task composition card; deterministic cards retain their executor')
        if card['id'] in ids:raise ValueError('Duplicate interface card ID')
        ids.add(card['id']);required=card_interfaces(card['operation'])
        if required is None or card['uses']!=list(required):raise ValueError('Invalid card operation/interfaces')
        if any(data['interfaces'].get(name)!=cap for name,cap in required.items()):raise ValueError('Incompatible deck interface')
        parameters(card,{})
    for card in data['cards']:
        target=card.get('fallback',{}).get('agent_card_id')
        if 'fallback' in card:
            _fields(card['fallback'],set(),{'agent_card_id','teams_card_id'})
            if not card['fallback']:raise ValueError('Fallback reference is empty')
            if card['operation'] in ('agent.resident','notify.teams'):raise ValueError('Fallback target cards cannot have fallback references')
            teams=card['fallback'].get('teams_card_id')
            if teams is not None and not any(c['id']==teams and c['operation']=='notify.teams' for c in data['cards']):raise ValueError('Teams reference must name a notify.teams card in this deck')
        targets=[target] if target is not None else []
        if card['operation'] in ('agent.request','agent.log_watch'):targets.append(parameters(card,{})['agent_card_id'])
        if card['operation'] in ('agent.task','agent.history_review') and parameters(card,{})['agent_card_id']:
            targets.append(parameters(card,{})['agent_card_id'])
        for target in targets:
            if not any(c['id']==target and c['operation']=='agent.resident' for c in data['cards']):raise ValueError('Agent reference must name a resident card in this deck')
        if card['operation']=='agent.monitor':
            monitor=parameters(card,{})
            for field in ('task_card_id','history_task_card_id'):
                if monitor[field] and not any(c['id']==monitor[field] and c['operation'] in ('agent.task','agent.history_review') for c in data['cards']):
                    raise ValueError('Monitor task must reference a registered agent task in this deck')
            if monitor['history_task_card_id'] and not any(c['id']==monitor['history_task_card_id'] and c['operation']=='agent.history_review' for c in data['cards']):
                raise ValueError('Historical monitoring requires a read-only history review card')
            if monitor['background']['enabled']:
                task=next(c for c in data['cards'] if c['id']==monitor['task_card_id'])
                if task['operation']!='agent.task' or parameters(task,{})['instruction_profile']!='adjustment':
                    raise ValueError('Background monitoring requires an adjustment task card')
            if monitor['agent_card_id'] and not any(c['id']==monitor['agent_card_id'] and c['operation']=='agent.resident' for c in data['cards']):
                raise ValueError('Monitor resident reference is absent')
        if card['operation'] in ('tune.pid_factor_reduction','tune.pid_coordinate_search'):
            target=parameters(card,{})['controller_card_id']
            if not any(c['id']==target and c['operation'] in ('servo.current_pid','servo.ecdl_steady_lock','servo.native_current') for c in data['cards']):raise ValueError('Online tuner must reference a stationary PID card in the deck')
    # No device addresses, device IDs, driver names, laser IDs, or safety policies.
    return data


def validate_laser(data):
    _fields(data,{'schema_version','id','name','allow_output','devices','interfaces'}, {'telemetry','agent_authority','target','measurement_plan'})
    identifier(data['id'])
    if 'measurement_plan' in data:
        from ldcon.core.adjustment_plan import approved_adjustment_plan
        approved_adjustment_plan(data['measurement_plan'])
    if 'target' in data:
        # The ONE source of the lock/recovery/window-centre target (2026-10-22): lock_target.laser_target validates it.
        from ldcon.jobs.lock_target import laser_target
        laser_target(data)
    if 'agent_authority' in data:
        # Maintainer-only ceilings for laser-agent safety approvals (agent_authority.check_ceilings).
        _fields(data['agent_authority'],set(),{'ceilings'})
        if not isinstance(data['agent_authority'].get('ceilings',{}),dict):raise ValueError('agent_authority.ceilings must be a mapping')
    if 'telemetry' in data:
        _fields(data['telemetry'],{'telegraf_endpoint'})
        endpoint=data['telemetry']['telegraf_endpoint']
        if endpoint is not None:
            from urllib.parse import urlsplit
            parsed=urlsplit(endpoint)
            if parsed.scheme!='udp' or not parsed.hostname or not parsed.port or parsed.path or parsed.query or parsed.fragment:
                raise ValueError('Expected Telegraf udp://host:port or null')
    if type(data['allow_output']) is not bool:raise ValueError('allow_output must be boolean')
    if not isinstance(data['devices'],dict) or not isinstance(data['interfaces'],dict):raise ValueError('Invalid laser device assignments')
    for key,device in data['devices'].items():
        identifier(key);_fields(device,{'type','connection','absolute_conditions'},{'control_settings'})
        if device['type'] not in DEVICE_CAPABILITIES or not isinstance(device['connection'],dict) or not isinstance(device['absolute_conditions'],dict):raise ValueError('Unknown device type/configuration')
        if 'control_settings' in device:
            if not isinstance(device['control_settings'],dict):raise ValueError('Device settings must be a mapping')
            # JSON serialization above guarantees serializable settings; complete current validation is required before execution.
            if device['type']=='ecdl.rig.owner':
                from ldcon.owners.ecdl_steady_policy import validate_window_quality
                profile=device['control_settings'].get('steady_lock',{})
                if isinstance(profile,dict):validate_window_quality(profile.get('window',{}))
        if device['type']=='metrics.router':
            from ldcon.jobs.metric_interface import router_configuration
            router_configuration(device,data['devices'])
        elif device['type'].startswith('metrics.'):
            from ldcon.jobs.metric_interface import configuration
            configuration(device)
        if device['type']=='thorlabs.cld1015.broker':
            from ldcon.core.temperature_settings import coefficient
            coefficient(device.get('control_settings'))
            policy=device['absolute_conditions'].get('temperature_c')
            if policy is not None:
                _fields(policy,{'bounds_c','max_slew_c_per_s','max_step_c','expressions'})
                # null values are editable drafts; non-null malformed values must fail.
                from ldcon.core.device_conditions import Formula
                if isinstance(policy['max_slew_c_per_s'],str):Formula(policy['max_slew_c_per_s'])
                for text in policy['expressions']:Formula(text)
                if all(policy[k] is not None for k in ('bounds_c','max_slew_c_per_s','max_step_c')):TemperatureConditions(policy)
    for role,binding in data['interfaces'].items():
        identifier(role);_fields(binding,{'device','capability'},{'axis'})
        device=data['devices'].get(binding['device'])
        if binding['capability']=='slow_actuator' and binding.get('axis')!='current':raise ValueError('Only the current-axis physical slow adapter is implemented')
        if device is None or binding['capability'] not in DEVICE_CAPABILITIES[device['type']]:raise ValueError('Device type cannot provide interface '+role)
    return data


def parameters(card,overrides):
    from ldcon.core.operation_registry import normalize_parameters
    return normalize_parameters(card, overrides, historical=True)


def _parameters(card,overrides):
    from ldcon.jobs.response_algorithms import ALGORITHMS, parameters as response_algorithm_parameters
    if card['operation'] in ALGORITHMS:return response_algorithm_parameters(card,overrides)
    if card['operation'] in ('measure.change', 'measure.batch'):
        from ldcon.jobs.change_measure import parameters as change_parameters, batch_parameters
        return (batch_parameters if card['operation']=='measure.batch' else change_parameters)(card,overrides)
    if card['operation']=='identify.axis_frequency':
        from ldcon.jobs.axis_frequency import parameters as axis_parameters
        return axis_parameters(card,overrides)
    if card['operation'] in ('servo.dlcpro_frequency','sweep.dlcpro_frequency'):
        from ldcon.jobs.dlcpro_cards import parameters as dlc_parameters
        return dlc_parameters(card,overrides)
    if card['operation'] in OPERATION_ALIASES:
        card={**card,'operation':OPERATION_ALIASES[card['operation']]}
    if card['operation']=='study.ecdl_mode_map':
        from ldcon.jobs.ecdl_mode_map import parameters as mode_map_parameters
        return mode_map_parameters(card,overrides)
    if card['operation']=='plan.ecdl_recovery':
        from ldcon.jobs.ecdl_recovery_plan import parameters as plan_parameters
        return plan_parameters(card,overrides)
    if card['operation']=='test.ecdl_recovery':
        from ldcon.jobs.ecdl_recovery_test import parameters as test_parameters
        return test_parameters(card,overrides)
    if card['operation']=='prepare.ecdl_recovery':
        from ldcon.jobs.ecdl_recovery import parameters as recovery_parameters
        return recovery_parameters(card,overrides)
    if card['operation']=='tune.ecdl_branch_hop':
        from ldcon.jobs.ecdl_branch_hop import parameters as hop_parameters
        return hop_parameters(card,overrides)
    if card['operation']=='recover.ecdl_mode_hop':
        from ldcon.jobs.ecdl_mode_recovery import parameters as hop_parameters
        return hop_parameters(card,overrides)
    if card['operation']=='laser.emission':
        from ldcon.jobs.emission_card import parameters as emission_parameters
        return emission_parameters(card,overrides)
    if card['operation']=='tune.operating_point':
        from ldcon.jobs.operating_point import parameters as point_parameters
        return point_parameters(card,overrides)
    if card['operation']=='tune.lock_target':
        from ldcon.jobs.lock_target import parameters as target_parameters
        return target_parameters(card,overrides)
    if card['operation']=='tune.pi_response':
        from ldcon.jobs.pi_response import parameters as pi_response_parameters
        return pi_response_parameters(card,overrides)
    if card['operation']=='study.target_response':
        from ldcon.jobs.target_response import parameters as response_parameters
        return response_parameters(card,overrides)
    if card['operation']=='tune.device_parameters':
        from ldcon.jobs.device_parameters import parameters as device_parameters
        return device_parameters(card,overrides)
    if card['operation']=='tune.ecdl_operating_point':
        from ldcon.jobs.ecdl_operating_point import parameters as point_parameters
        return point_parameters(card,overrides)
    if card['operation']=='notify.teams':
        from ldcon.agents.teams_card import parameters as teams_parameters
        return teams_parameters(card,overrides)
    if card['operation']=='acquire.metrics':
        from ldcon.jobs.metric_cards import parameters as metric_parameters
        return metric_parameters(card,overrides)
    if card['operation'] in ('agent.resident','agent.request'):
        from ldcon.agents.resident_agent import parameters as agent_parameters
        return agent_parameters(card,overrides)
    if card['operation'] in ('agent.task','agent.history_review'):
        from ldcon.agents.agent_task import parameters as task_parameters
        return task_parameters(card,overrides)
    if card['operation']=='agent.monitor':
        from ldcon.agents.agent_monitor import parameters as monitor_parameters
        return monitor_parameters(card,overrides)
    if card['operation']=='agent.log_watch':
        from ldcon.agents.agent_log_watch import parameters as watch_parameters
        return watch_parameters(card,overrides)
    if card['operation']=='tune.native_current':
        from ldcon.jobs.native_current_acquire import parameters as native_acquire_parameters
        return native_acquire_parameters(card,overrides)
    if card['operation']=='servo.native_current':
        from ldcon.jobs.native_current_card import parameters as native_parameters
        return native_parameters(card,overrides)
    if card['operation'].startswith('sweep.') and card['operation']!='sweep.dlcpro_frequency':
        from ldcon.jobs.modulation_sweep import parameters as sweep_parameters
        return sweep_parameters(card,overrides)
    if card['operation']=='tune.sweep_tracking':
        from ldcon.jobs.sweep_tracking import parameters as tracking_parameters
        return tracking_parameters(card,overrides)
    if card['operation']=='analyze.shot_sweep':
        from ldcon.jobs.shot_analysis import parameters as analysis_parameters
        return analysis_parameters(card,overrides)
    if card['operation']=='report.grafana_dashboard':
        from ldcon.web.grafana_dashboard import parameters as grafana_parameters
        return grafana_parameters(card,overrides)
    if card['operation']=='study.ecdl_current_response':
        from ldcon.jobs.ecdl_response_study import parameters as response_parameters
        return response_parameters(card,overrides)
    if card['operation']=='commission.ecdl_pi':
        from ldcon.jobs.ecdl_pi_commission import parameters as commission_parameters
        return commission_parameters(card,overrides)
    if card['operation']=='tune.ecdl_window_center':
        from ldcon.jobs.window_center import parameters as window_parameters
        return window_parameters(card,overrides)
    if card['operation']=='study.ecdl_locked_current_map':
        from ldcon.owners.ecdl_locked_study import parameters as study_parameters
        return study_parameters(card,overrides)
    if card['operation']=='servo.ecdl_steady_lock':
        from ldcon.jobs.ecdl_steady import parameters as steady_parameters
        return steady_parameters(card,overrides)
    if card['operation']=='study.ecdl_thermal_context':
        from ldcon.jobs.ecdl_thermal_context import parameters as study_parameters
        return study_parameters(card,overrides)
    if card['operation']=='tune.ecdl_initial_point':
        from ldcon.owners.ecdl_prelock import parameters as ecdl_parameters
        return ecdl_parameters(card,overrides)
    if card['operation']=='tune.pid_online':
        from ldcon.jobs.online_pid import parameters as online_parameters
        return online_parameters(card,overrides)
    if card['operation'] in ('servo.current_pid','identify.current_response'):
        from ldcon.jobs.current_cards import card_parameters
        return card_parameters(card,overrides)
    if not isinstance(card['parameters'],dict) or not isinstance(overrides,dict):raise ValueError('Parameters must be mappings')
    if card['operation']=='tune.temperature_once':
        if any(source.get('frequency_hz_per_c') is not None for source in (card['parameters'],overrides)):
            raise ValueError('Move frequency_hz_per_c from card/assignment parameters to device.control_settings.temperature')
        # Old empty calibration placeholders are harmless; do not project them
        # back into the card editor as an alternate source of calibration.
        card={**card,'parameters':{k:v for k,v in card['parameters'].items() if k!='frequency_hz_per_c'}}
        overrides={k:v for k,v in overrides.items() if k!='frequency_hz_per_c'}
    defaults=MONITOR_DEFAULTS if card['operation']=='monitor.wavelength' else SEEK_DEFAULTS
    if set(card['parameters'])-defaults.keys() or set(overrides)-defaults.keys():raise ValueError('Unknown card parameter')
    p={**defaults,**card['parameters'],**overrides}
    nullable={'target_frequency_hz','target_wavelength_nm','tolerance_hz','requested_slew_c_per_s','max_excursion_c'}
    for key,value in p.items():
        if key=='identify_slope':
            if type(value) is not bool:raise ValueError('identify_slope must be boolean')
            continue
        if value is None and key not in nullable:raise ValueError(key+' cannot be unset')
        if value is not None:
            try:number=finite(value,key)
            except ConditionError as e:raise ValueError(str(e)) from e
            if number<=0:raise ValueError(key+' must be positive')
    if 'gain' in p and p['gain']>1:raise ValueError('gain must be <=1')
    if 'probe_samples' in p and (type(p['probe_samples']) is not int or p['probe_samples']<3):raise ValueError('probe_samples must be an integer >=3')
    if 'measurement_recovery_samples' in p and (type(p['measurement_recovery_samples']) is not int or p['measurement_recovery_samples']<1):raise ValueError('measurement_recovery_samples must be a positive integer')
    if 'stable_samples' in p and (type(p['stable_samples']) is not int or p['stable_samples']<1):raise ValueError('stable_samples must be a positive integer')
    if p.get('target_frequency_hz') is not None and p.get('target_wavelength_nm') is not None:raise ValueError('Specify frequency or wavelength, not both')
    if p.get('timeout_s',p.get('duration_s',0))>86400:raise ValueError('Card runtime must be <=24 hours')
    if p['sample_interval_s'] is None or not .1<=p['sample_interval_s']<=2:raise ValueError('sample_interval_s must be in [0.1, 2] seconds')
    if p['sample_interval_s']<.1:raise ValueError('sample_interval_s must be >=0.1 s')
    return p


def readiness(laser,deck,card,overrides=None):
    from ldcon.core.operation_registry import validate_card
    return validate_card(laser, deck, card, overrides, historical=True)


def _readiness(laser,deck,card,overrides=None):
    from ldcon.jobs.response_algorithms import ALGORITHMS, readiness as response_readiness
    if card['operation'] in ALGORITHMS:return response_readiness(laser,deck,card,overrides)
    card={**card,'operation':OPERATION_ALIASES.get(card['operation'],card['operation'])}
    p=parameters(card,overrides or {});issues=[]
    if card['operation'] in ('identify.axis_frequency','servo.dlcpro_frequency','sweep.dlcpro_frequency'):
        from ldcon.jobs.dlcpro_cards import readiness as dlc_readiness
        issues.extend(dlc_readiness(laser,card,p))
    if card['operation'] in RETIRED_OPERATIONS:
        issues.append('Retired card; review and migrate settings to '+RETIRED_OPERATIONS[card['operation']])
    if card['operation']=='tune.ecdl_branch_hop':
        try:
            from ldcon.jobs.ecdl_branch_hop import require_configured
            require_configured(p)
        except ValueError as error:issues.append(str(error))
        if not laser['allow_output']:issues.append('Laser output writes are not enabled')
    if card['operation']=='recover.ecdl_mode_hop':
        try:
            from ldcon.jobs.ecdl_mode_recovery import validate as hop_validate
            hop_validate(p)
        except (ValueError,TypeError) as error:issues.append(str(error))
    if card['operation']=='laser.emission':
        try:
            from ldcon.jobs.emission_card import configuration
            configuration(laser)
        except (ValueError,TypeError,KeyError,DeviceError) as error:issues.append(str(error))
    if card['operation'] in ('measure.change','measure.batch'):
        from ldcon.jobs.change_measure_adapter import readiness as measurement_readiness
        issues.extend(measurement_readiness(laser,card,p))
    if card['operation']=='tune.operating_point':
        try:
            from ldcon.jobs.operating_point import axis_map,validate as point_validate
            axis_map(laser)
            if p.get('moves') is not None or p.get('schedule') is not None:point_validate(p)
        except (ValueError,TypeError,KeyError,DeviceError) as error:issues.append(str(error))
        if not laser['allow_output']:issues.append('Laser output writes are not enabled')
    if card['operation']=='tune.device_parameters':
        try:
            from ldcon.jobs.device_parameters import validate as device_validate
            if p.get('changes') is not None:device_validate(p)
        except (ValueError,TypeError) as error:issues.append(str(error))
        if not laser['allow_output']:issues.append('Laser output writes are not enabled')
    if card['operation']=='tune.lock_target':
        try:
            from ldcon.jobs.lock_target import laser_target
            if laser_target(laser) is None:issues.append('The laser definition has no target block (target.frequency_hz, allowed_change_hz, max_change_hz)')
        except (ValueError,TypeError) as error:issues.append(str(error))
    if card['operation']=='tune.pi_response':
        try:
            from ldcon.jobs.pi_response import validate_laser
            validate_laser(laser,deck,p)
        except (ValueError,TypeError,KeyError,DeviceError) as error:issues.append(str(error))
    if card['operation']=='study.target_response':
        try:
            from ldcon.jobs.target_response import validate_laser
            validate_laser(laser,p)
            if p['execution_mode']=='agent' and not any(c['id']==p['agent_card_id'] and c['operation']=='agent.resident' for c in deck['cards']):
                issues.append('Study agent reference must name a resident card in this deck')
        except (ValueError,TypeError,KeyError,DeviceError) as error:issues.append(str(error))
    if card['operation'] in ('study.ecdl_mode_map','test.ecdl_recovery','prepare.ecdl_recovery','tune.ecdl_operating_point','recover.ecdl_mode_hop'):
        if not laser['allow_output']:issues.append('Laser output writes are not enabled')
        binding=laser['interfaces'].get('observation')
        device=laser['devices'].get(binding['device']) if binding else None
        if not device or device['type']!='ecdl.rig.owner':issues.append('ECDL owner adapters have not been assigned')
        else:
            try:
                from ldcon.owners.ecdl_owner import axis_policies
                axis_policies(device)
                if not device['connection'].get('endpoint'):issues.append('ECDL owner endpoint is unset')
                if len({laser['interfaces'].get(n,{}).get('device') for n in ECDL_OPEN_PORTS})!=1:
                    issues.append('All ECDL interfaces must share one owner')
            except (ValueError,TypeError,KeyError,ConditionError) as error:issues.append(str(error))
    required={
        'study.ecdl_mode_map':('target_frequency_hz','current_start_fraction','current_end_fraction','capture_hz','discontinuity_hz'),
        'plan.ecdl_recovery':('tolerance_hz','hop_threshold_hz'),
        'prepare.ecdl_recovery':('tolerance_hz','hop_threshold_hz')}
    for name in required.get(card['operation'],()):
        if p[name] is None:issues.append(name+' is required')
    # 2026-10-27: tune.ecdl_operating_point takes the tested recovery artifact (recovery_ref) instead of a map
    for operation,kind in (('plan.ecdl_recovery','map'),('test.ecdl_recovery','plan'),('tune.ecdl_operating_point','recovery')):
        if card['operation']==operation and p[kind+'_artifact'] is None and p[kind+'_ref'] is None:
            issues.append('A recorded '+kind+' artifact or same-laser job reference is required')
    if card['operation']=='test.ecdl_recovery' and not p['perturbations']:issues.append('Explicit bounded hop perturbations are required')
    if card['operation']=='prepare.ecdl_recovery':
        try:
            from ldcon.jobs.recovery_procedure import require_configured
            require_configured(p)
        except ValueError as error:issues.append(str(error))
    if card['operation']=='prepare.ecdl_recovery' and p['execution_mode']=='agent':
        if not any(c['id']==p['agent_card_id'] and c['operation']=='agent.resident' for c in deck['cards']):
            issues.append('Recovery preparation agent must name a gateway card in this deck')
    if card['operation']=='tune.ecdl_operating_point':
        from ldcon.jobs.lock_target import resolve_target
        if resolve_target(laser,p) is None:issues.append('Target frequency/wavelength is required (card parameter or laser.target)')
        if p.get('recovery_artifact') is not None:
            try:
                from ldcon.jobs.recovery_procedure import checked_recovery
                checked_recovery(p['recovery_artifact'])
            except (ValueError,TypeError,KeyError) as error:issues.append(str(error))
        elif p.get('recovery_ref'):issues.append('Resolve the same-laser recovery reference before starting')
        if p.get('tec_ff') is None and device and device['type']=='ecdl.rig.owner':
            try:
                from ldcon.owners.ecdl_owner import axis_policies
                if axis_policies(device)['temperature'].frequency_hz_per_unit is None:
                    issues.append('tec_ff is required (temperature.frequency_hz_per_unit of the TEC axis is null)')
            except (ValueError,TypeError,KeyError,ConditionError):pass
    if card['operation'] in ('servo.native_current','tune.native_current'):
        binding=laser['interfaces'].get('native_current')
        if binding:
            try:
                from ldcon.owners.esp32_native_owner import validate_policy
                device=laser['devices'][binding['device']]
                policy=validate_policy(device['control_settings']['native_current'])
                if not policy['writes_enabled']:
                    issues.append('Native current writes are not enabled')
                if card['operation'] == 'servo.native_current' and not policy['native']['arm_enabled']:
                    issues.append('Native PI arm is not enabled')
                if not policy['calibration']['verified'] or not policy['calibration']['response_verified']:
                    issues.append('External modulation calibration/response is not verified')
                if not device['connection'].get('endpoint') or not device['connection'].get('device_id'):
                    issues.append('Independent native owner endpoint/device_id are required')
            except (ValueError,KeyError,TypeError) as error:issues.append(str(error))
        if not laser['allow_output']:issues.append('Laser output writes are not enabled')
        from ldcon.jobs.lock_target import resolve_target
        if resolve_target(laser,p) is None:
            issues.append('Target frequency/wavelength is unset (card parameter or laser.target)')
    if card['operation'].startswith('sweep.') and card['operation']!='sweep.dlcpro_frequency':
        try:
            from ldcon.jobs.native_ff_proxy import feedforward_route
            from ldcon.jobs.locked_triangle import card_parameters
            sweep_parameters = card_parameters(laser, p)
            if feedforward_route(laser,card['uses'][0]):
                from ldcon.jobs.native_ff_sweep import configuration as ff_configuration
                from ldcon.owners.native_ff_proof import validate_profile
                from ldcon.jobs.native_ff_proxy import ff_profile
                ff_configuration(laser)
                native=laser['devices'][laser['interfaces']['native_current']['device']]
                validate_profile(ff_profile(laser),native['control_settings']['native_current'])
                from ldcon.jobs.native_ff_proxy import plan_from_card
                plan_from_card(sweep_parameters, p.get('shot_sweep',{}).get('plan') if p.get('shot_sweep') else None)
            elif __import__('ldcon.jobs.ecdl_locked_sweep_proxy',fromlist=['rig_locked_route']).rig_locked_route(laser,card['uses'][0]):
                from ldcon.jobs.ecdl_locked_sweep_proxy import validate_settings,locked_settings,plan_from_card
                validate_settings(locked_settings(laser,card['uses'][0]))
                plan_from_card(sweep_parameters,p.get('shot_sweep',{}).get('plan') if p.get('shot_sweep') else None)
            else:
                from ldcon.jobs.modulation_sweep import configuration
                configuration(laser,card['uses'][0])
                issues.append('Laser system binding required: use a native FF or ECDL owner-backed sweep')
        except (ValueError,KeyError,TypeError) as error:issues.append('External modulation configuration: '+str(error))
    if card['operation']=='study.ecdl_locked_current_map':issues.append('A matching running ECDL stationary lock is required')
    if card['operation']=='tune.ecdl_window_center':
        # The same traversal runs unlocked when no lock-owned dither is requested.
        if p['measure_dither'] or p['mode']!='center_only':issues.append('A matching running ECDL stationary lock is required')
    if card['operation'] in ('servo.ecdl_steady_lock','commission.ecdl_pi','study.ecdl_current_response'):
        binding=laser['interfaces'].get('lock')
        device=laser['devices'].get(binding['device']) if binding else None
        if device:
            try:
                from ldcon.owners.ecdl_steady_policy import validate_profile
                from ldcon.owners.ecdl_owner import axis_policies
                axis_policies(device)
                validate_profile(device.get('control_settings',{}).get('steady_lock'),p.get('strategy','window'),
                                 require_handover=card['operation']!='commission.ecdl_pi')
                if not device['connection'].get('endpoint'):issues.append('ECDL owner endpoint is unset')
            except (ValueError,TypeError,KeyError,ConditionError) as error:issues.append(str(error))
        if not laser['allow_output']:issues.append('Laser output writes are not enabled')
        target=p['target_frequency_hz']
        if target is None and card['operation']=='servo.ecdl_steady_lock':
            # ONE target source (2026-10-22 fix of the live 422): a null card target follows laser.target; the worker
            # resolves the same value (interface_worker, resolve_target) before run_steady_lock.
            from ldcon.jobs.lock_target import resolve_target
            target=resolve_target(laser,p)
        if target is None or p['tolerance_hz'] is None:
            issues.append('Target frequency and tolerance are required'+(' (card parameter or laser.target)' if card['operation']=='servo.ecdl_steady_lock' else ''))
        if card['operation']=='servo.ecdl_steady_lock':
            for kind in ('recovery','recovery_plan','recovery_test'):
                if p.get(kind+'_ref') and p.get(kind+'_artifact') is None:
                    issues.append('Resolve the same-laser '+kind+' reference before starting')
    if card['operation'] in ('tune.ecdl_initial_point','study.ecdl_thermal_context'):
        binding=laser['interfaces'].get('observation')
        device=laser['devices'].get(binding['device']) if binding else None
        if not device or device['type']!='ecdl.rig.owner':issues.append('ECDL owner adapters have not been assigned')
        else:
            if len({b['device'] for n,b in laser['interfaces'].items() if n in card_interfaces(card['operation'])})!=1:issues.append('All ECDL interfaces must share one owner')
            try:
                from ldcon.owners.ecdl_owner import axis_policies
                axis_policies(device)
                if not device['connection'].get('endpoint'):issues.append('ECDL owner endpoint is unset')
            except (ValueError,TypeError,KeyError,ConditionError) as error:issues.append('ECDL device settings incomplete: '+str(error))
        if not laser['allow_output']:issues.append('Laser output writes are not enabled')
        if p['target_frequency_hz'] is None or p['tolerance_hz'] is None:issues.append('Target frequency and tolerance are required')
    if card['operation']=='study.ecdl_thermal_context' and any(p[k] is None for k in ('temperature_end_fraction','current_start_fraction','current_end_fraction','current_preferred_fraction')):issues.append('Thermal study axis fractions are required')
    for name,cap in card_interfaces(card['operation']).items():
        binding=laser['interfaces'].get(name)
        if not binding or binding['capability']!=cap:issues.append('Missing interface: '+name);continue
        device=laser['devices'][binding['device']];connection=device['connection']
        if cap in ('frequency_monitor','scalar_sensor'):
            if not connection.get('endpoint'):issues.append('Wavemeter endpoint is unset')
            source=connection.get('source',{}) if device['type']=='fizeau.owner' else connection
            if card['operation']!='monitor.wavelength' and source.get('trust_source_clock') is not True:issues.append('Wavemeter acquisition clock is not trusted')
        elif cap=='temperature_controller':
            interval=device.get('control_settings',{}).get('temperature',{}).get('output_update_interval_s')
            if interval is not None:
                if type(interval) not in (int,float) or not math.isfinite(interval) or not .1<=interval<=2:issues.append('Temperature device interval must be in [0.1,2] s')
                else:p['sample_interval_s']=interval
            for key in ('endpoint','instrument','resource'):
                if not connection.get(key):issues.append('Temperature controller '+key+' is unset')
            try:TemperatureConditions(device['absolute_conditions'].get('temperature_c'))
            except (ValueError,ConditionError,TypeError) as e:issues.append(str(e))
    if card['operation'] in ('servo.current_pid','identify.current_response'):
        from ldcon.core.current_settings import CurrentConditions,validate_settings
        binding=laser['interfaces'].get('current')
        if binding:
            device=laser['devices'][binding['device']];settings=device.get('control_settings',{})
            try:
                CurrentConditions(device['absolute_conditions'].get('current_a'),settings)
                p['sample_interval_s']=settings['current']['output_update_interval_s']
                validate_settings(settings['current'],require_pid=card['operation'] in ('servo.current_pid'))
                if card['operation']=='identify.current_response' and settings['current']['response']['step_a'] is None:issues.append('Current response step is unset')
            except (ValueError,KeyError,TypeError,ConditionError) as error:issues.append(str(error))
            if settings.get('current',{}).get('enabled') is not True:issues.append('Current output writes are not enabled')
            for key in ('endpoint','instrument','resource'):
                if not device['connection'].get(key):issues.append('Current controller '+key+' is unset')
        if not laser['allow_output']:issues.append('Laser output writes are not enabled')
        if card['operation']=='servo.current_pid' and p['target_frequency_hz'] is None and p['target_wavelength_nm'] is None:issues.append('Target frequency/wavelength is unset')
    if card['operation'] in ('tune.pid_factor_reduction','tune.pid_coordinate_search'):issues.append('A running stationary PID controller is required')
    if card['operation']=='tune.temperature_once':
        if not laser['allow_output']:issues.append('Laser output writes are not enabled')
        if p['target_frequency_hz'] is None and p['target_wavelength_nm'] is None:issues.append('Target frequency/wavelength is unset')
        from ldcon.core.temperature_settings import coefficient
        binding=laser['interfaces'].get('temperature')
        settings=laser['devices'][binding['device']].get('control_settings') if binding else None
        if coefficient(settings) is None and not p['identify_slope']:issues.append('Temperature device tuning coefficient or identification is required')
        for key in ('tolerance_hz','requested_slew_c_per_s'):
            if p[key] is None:issues.append(key+' is unset')
    if card['operation']=='acquire.metrics' and 'metrics' in laser['interfaces']:
        from ldcon.jobs.metric_interface import open_metric_interface
        try:
            port=open_metric_interface(laser)
            for quantity in p['quantities']:port.describe(quantity)
        except (ValueError,KeyError,TypeError) as error:issues.append(str(error))
    if card['operation']=='notify.teams' and (not p['webhook_env'] or not p['message']):issues.append('Teams secret reference and manual message are required')
    if card['operation']=='agent.request' and (not p['agent_card_id'] or not p['prompt']):issues.append('Agent card and prompt are required')
    if card['operation'] in ('agent.task','agent.history_review'):
        from experiment_core.agent_contracts import execution_policy,choose_profile
        try:
            policy=execution_policy(card.get('agent_execution',dict(mode='required',task_profile=p['execution_profile'])))
            if policy.get('difficulty') or policy['provider_profiles'] and not p['execution_profile']:
                if not p['execution_profile'] and not any(c['id']==p['agent_card_id'] and c['operation']=='agent.resident' for c in deck['cards']):
                    raise ValueError('Select a gateway card for inherited task execution')
                # The registry, connected gateway and difficulty table belong to
                # admission. Static validation has neither their context nor the
                # authority to pick a fallback profile.
                chosen='resolved_at_admission'
            else:chosen=choose_profile(policy,requested=p['request_delegation'],override=p['execution_profile'])
            if chosen is None and not p['agent_card_id']:issues.append('Resident execution requires an agent card')
        except ValueError as error:issues.append(str(error))
        if not p['prompt']:issues.append('A task objective is required at invocation')
    if card['operation']=='agent.monitor' and not (p['agent_card_id'] or p['task_card_id'] or p['history_task_card_id']):
        issues.append('A resident or task card is required for monitoring')
    if card['operation']=='agent.log_watch' and not any(c['id']==p['agent_card_id'] and c['operation']=='agent.resident' for c in deck['cards']):
        issues.append('Agent reference must name a resident card in this deck')
    return {'runnable':not issues,'blocked_reasons':issues,'parameters':p}


class InterfaceRepository:
    def __init__(self,root):
        self.root=config_root(root); self.state_root=Path(root)
        for name in ('lasers','interface-decks','assignments'):(self.root/name).mkdir(parents=True,exist_ok=True)

    def read(self,kind,name):
        path=config_file(self.state_root,kind,identifier(name))
        if path.is_symlink() or not path.is_file():raise FileNotFoundError(name)
        if path.stat().st_size>MAX_YAML_BYTES:raise ValueError('Configuration exceeds 256 KiB')
        text=path.read_text();data=load_mapping(text)
        if data.get('id')!=name:raise ValueError('Configuration id must match filename')
        try:
            if kind=='lasers':validate_laser(data)
            elif kind=='interface-decks':validate_deck(data)
        except (ConditionError,TypeError,KeyError) as e:raise ValueError(str(e)) from e
        return {'id':name,'document':data,'yaml':text,'revision':hashlib.sha256(text.encode()).hexdigest(),'source_path':str(path)}

    def assignment(self,name):
        value=self.read('assignments',name);data=value['document']
        _fields(data,{'schema_version','id','laser_id','decks'})
        if not isinstance(data['decks'],list) or not data['decks']:raise ValueError('Assignment decks are required')
        laser=self.read('lasers',data['laser_id']);decks=[];seen=set()
        for entry in data['decks']:
            _fields(entry,{'deck_id','parameters'})
            if entry['deck_id'] in seen:raise ValueError('Duplicate assigned deck')
            seen.add(entry['deck_id']);deck=self.read('interface-decks',entry['deck_id'])
            if not isinstance(entry['parameters'],dict) or set(entry['parameters'])-{c['id'] for c in deck['document']['cards']}:raise ValueError('Unknown invocation card')
            statuses={c['id']:readiness(laser['document'],deck['document'],c,entry['parameters'].get(c['id'],{})) for c in deck['document']['cards']}
            decks.append({**deck,'card_status':statuses,'invocation_parameters':entry['parameters']})
        fingerprint=digest({'assignment':data,'laser':laser['document'],'decks':[d['document'] for d in decks]})
        return {**value,'laser_definition':laser,'deck_definitions':decks,'definition_revision':fingerprint}

    def list(self,kind):
        result=[]
        for path in sorted((self.root/kind).glob('*.yaml')):
            try:
                value=self.read(kind,path.stem);result.append({'id':value['id'],'name':value['document'].get('name',value['id']),'revision':value['revision']})
            except (ValueError,OSError,ConditionError) as e:result.append({'id':path.stem,'error':str(e)})
        return result
