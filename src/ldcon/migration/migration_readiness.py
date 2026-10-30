"""Offline owner handoff checks and an inert, byte-preserving final-state capsule.

Nothing in this module sends RPCs, opens a device, changes services, installs
state, clears a lease/fault, or marks a cutover complete. See the handoff HTML.
"""
from datetime import datetime, timezone
import hashlib
import json
import math
import os
import re
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

import yaml

MODE = 'owner-handoff-preparation-only'
TERMINAL = {'succeeded', 'completed', 'completed_with_failures', 'failed',
            'cancelled', 'interrupted', 'inconclusive'}


def _validate_plan(plan):
    if plan.get('schema_version') != 1 or plan.get('mode') != MODE:
        raise ValueError('Explicit preparation-only owner handoff plan required')
    if type(plan.get('source_user_uid')) is not int or plan['source_user_uid'] < 0:
        raise ValueError('Original source user UID must be explicit')
    owners = plan.get('owners', [])
    ids = [item['id'] for item in owners]
    kinds = [item['kind'] for item in owners]
    if (len(set(ids)) != len(ids) or any(not re.fullmatch(r'[A-Za-z0-9_-]+', name) for name in ids)
            or any(kind not in ('ecdl', 'native_current', 'cld', 'monitor') for kind in kinds)
            or any(kinds.count(kind) != 1 for kind in ('ecdl', 'native_current', 'cld'))):
        raise ValueError('Distinct ECDL, native-current and CLD owners are required')
    units = plan.get('final_sync_source_units', [])
    if not units or len(set(units)) != len(units) or any(not re.fullmatch(r'ldcon-[A-Za-z0-9_.@-]+\.service', u) for u in units):
        raise ValueError('Explicit distinct ldcon source-service inventory is required')
    if set(plan.get('baselines', {})) != {'sr422', 'sr1092'}:
        raise ValueError('Both laser baselines are required')


def _load(path):
    path = Path(path)
    if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('An absolute, non-symlink file is required')
    raw = path.read_bytes()
    value = json.loads(raw) if path.suffix == '.json' else yaml.safe_load(raw)
    return value, hashlib.sha256(raw).hexdigest()


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def policy_definition(kind, config):
    """Mirror pinned owner identity calculations; never construct an owner."""
    if kind == 'native_current':
        return _digest(config['native_current'])
    if kind == 'cld':
        if config['controller'].get('temperature_conditions_file'):
            raise ValueError('Imported CLD policy needs an explicit expanded-policy audit')
        controller = dict(config['controller'], profile='cld1015', channels=[0],
                          resource=config['resource'], timeout_s=config['io_timeout_s'])
        return _digest(dict(resource=config['resource'], identity=config['identity'],
                            instrument=config['instrument'], controller=controller,
                            writes_enabled=config['writes_enabled']))
    if kind == 'ecdl':
        return _digest(config['devices']['rig'])
    raise ValueError('Unsupported durable owner kind')


def ecdl_entry_preview(observation, profile, target, tolerance):
    """Preview spectral quality and the retained PI capture gate; this is not an arm call."""
    from ldcon.owners.ecdl_steady_policy import entry_quality
    gate = profile.get('entry_gate')
    frequency = observation.get('frequency', {}).get('frequency_hz')
    finite_frequency = type(frequency) in (int, float) and math.isfinite(frequency) and frequency > 0
    spectrum = entry_quality(observation, profile, target)
    return dict(spectrum_gate_passed=spectrum,
                handover_frequency_passed=finite_frequency and abs(frequency-target) <= min(tolerance, profile['capture_hz']),
                single_mode=observation.get('single_mode'), explicit_entry_gate=gate is not None,
                effective_handover_tolerance_hz=min(tolerance, profile['capture_hz']),
                arm_executed=False, repeat_after_hold_required=True)


def _difference(first, second, prefix=''):
    if first == second:
        return []
    if isinstance(first, dict) and isinstance(second, dict):
        result = []
        for key in sorted(first.keys() | second.keys()):
            if key not in first or key not in second:
                result.append(prefix + '/' + key)
            else:
                result += _difference(first[key], second[key], prefix + '/' + key)
        return result
    return [prefix]


def _state_reasons(kind, state):
    if not isinstance(state, dict):
        return ['invalid_state_mapping']
    reasons = []
    if state.get('schema_version') != 1:
        reasons.append('invalid_state_schema')
    keys = {'native_current': ('lease', 'bias_lease', 'pending'),
            'cld': ('leases', 'pending'),
            'ecdl': ('lease', 'native_armed', 'recovery')}[kind]
    for key in (*keys, 'fault'):
        if key not in state:
            reasons.append(key + '_missing')
            continue
        value = state[key]
        if key == 'leases':
            valid = isinstance(value, dict)
        elif key in ('lease', 'bias_lease', 'fault'):
            valid = value is None or (isinstance(value, str) and bool(value))
        elif key == 'native_armed':
            valid = type(value) is bool
        else:
            valid = value is None or (isinstance(value, dict) and bool(value))
        if not valid:
            reasons.append(key + '_invalid')
        elif value:
            reasons.append(key + '_retained')
    return reasons


def _check(issues, code, passed, detail, category='actionable_defect'):
    issues.append(dict(code=code, status='passed' if passed else category, detail=detail))


def _time(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


def inspect_readiness(plan, inventory, *, now=None):
    """Inspect files and already-captured read-only evidence. No remote reads."""
    _validate_plan(plan)
    issues, definitions, entry_previews = [], [], {}
    _check(issues, 'inspection_fresh',
           0 <= (time.time() if now is None else now) - _time(inventory['created_utc']) <= 300,
           'Read-only inventory must be at most 300 seconds old.')
    _check(issues, 'inspection_read_only', inventory.get('read_only') is True and
           inventory.get('hardware_writes') is False and not inventory.get('errors'),
           'Inspection must be read-only with all owner probes successful.')
    for name, service in inventory.get('services', {}).items():
        _check(issues, 'source_service_' + name,
               service.get('ActiveState') == 'active' and bool(service.get('identity')),
               'Expected source service and process identity remain active.')
    for laser, expected in plan['baselines'].items():
        data = inventory['deployments'].get(laser, {})
        jobs = [j for j in data.get('active_jobs', []) if j.get('status') not in TERMINAL]
        good = len(jobs) == 1 and jobs[0].get('id') == expected['job_id']
        good = good and jobs[0].get('status') == 'running'
        good = good and jobs[0].get('controller_state') in expected['accepted_states']
        _check(issues, laser + '_operator_baseline', good,
               'Accepted baseline is scoped to this job and preparation only; control quality is unchanged.')
    owners = inventory.get('owners', {})
    broker = owners.get('broker', {})
    obs = broker.get('observation', {})
    _check(issues, 'sr422_pi_ownership_fault',
           'fault' in broker and broker['fault'] is None and broker.get('lease_retained') is True and
           broker.get('snapshot', {}).get('servo_config', {}).get('pid_multiplier') == 1,
           'PI must remain enabled under its existing lease without owner fault.')
    _check(issues, 'sr422_observation_quality', obs.get('quality_valid') is True and
           obs.get('frequency', {}).get('quality') == 'valid' and
           obs.get('frequency', {}).get('acquisition_known') is True,
           'mode_degraded acceptance does not waive input quality or acquisition provenance.')
    freshness = broker.get('observation', {}).get('frequency', {})
    timestamp = freshness.get('acquired_unix_s')
    # The inspection timestamp precedes several bounded HTTP reads. Per-owner
    # sample freshness is checked by owner and by a bounded capture span here.
    _check(issues, 'sr422_observation_fresh', isinstance(timestamp, (int, float)) and
           math.isfinite(timestamp) and -60 <= _time(inventory['created_utc']) - timestamp <= 5,
           'Fresh optical source timestamp must be within the bounded inspection interval.')
    esp = owners.get('esp', {})
    _check(issues, 'sr1092_pi_ownership_fault', esp.get('native_pi_active') is True and
           esp.get('leased') is True and 'fault' in esp and esp['fault'] is None and
           esp.get('input_valid') is True and esp.get('fresh') is True,
           'Native current PI, lease, valid input, freshness, and no fault are required.')
    cld = owners.get('cld', {})
    _check(issues, 'sr1092_bias_interlock', 'fault' in cld and cld['fault'] is None and
           cld.get('fresh') is True and cld.get('bias_held') is True and
           cld.get('sample', {}).get('ld_enabled') is True and
           cld.get('sample', {}).get('tec_enabled') is True,
           'The existing independent CLD bias lease and LD/TEC state must remain healthy.')
    for item in plan['owners']:
        kind = item['kind']
        source, source_hash = _load(item['source_definition_file'])
        target, target_hash = _load(item['prepared_definition_file'])
        actual_differences = _difference(source, target)
        _check(issues, item['id'] + '_definition_changes',
               set(actual_differences) <= set(item.get('allowed_difference_pointers', [])),
               'Only explicitly reviewed configuration fields may differ: ' + ', '.join(actual_differences))
        if kind == 'monitor':
            continue
        old_digest, new_digest = policy_definition(kind, source), policy_definition(kind, target)
        durable_path = Path(item['source_state'])
        state = _load(durable_path)[0] if durable_path.exists() else None
        record = dict(owner=item['id'], source_file_sha256=source_hash,
                      prepared_file_sha256=target_hash, source_definition=old_digest,
                      prepared_definition=new_digest, durable_state_present=state is not None,
                      durable_state_unchanged=True)
        if kind == 'ecdl':
            # ECDL has only a quarantine file, not a restartable active-state
            # file. Its revision must never be rewritten to match a new path.
            _check(issues, item['id'] + '_quarantine_absent', state is None,
                   'Any ECDL quarantine requires separate review; never delete it or translate its revision.')
            paused = target['devices']['rig']['control_settings']['steady_lock'].get('hot_control', {}).get('initial_tec_ff_paused', False)
            _check(issues, item['id'] + '_initial_tec_state',
                   paused is broker.get('tec_context', {}).get('tec_ff_paused'),
                   'Prepared initial TEC pause must equal the observed hot-control state.')
            record['restart_semantics'] = 'No quarantine migration; new revision negotiated after clean held release.'
            baseline = plan['baselines'][item['laser_id']]
            entry_previews[item['laser_id']] = ecdl_entry_preview(obs,
                target['devices']['rig']['control_settings']['steady_lock'],
                baseline['target_frequency_hz'], baseline['acquisition_tolerance_hz'])
            preview = entry_previews[item['laser_id']]
            _check(issues, item['id'] + '_unchanged_entry_gate_preview',
                   preview['spectrum_gate_passed'] and preview['handover_frequency_passed'],
                   'Read-only preview; repeat full owner steady_ready/arm checks only during authorized cutover.',
                   'conditional_restart_guard')
        else:
            _check(issues, item['id'] + '_saved_definition',
                   state is not None and old_digest == new_digest == state.get('definition'),
                   'Relocated path fields must not alter the owner policy digest.')
            reasons = _state_reasons(kind, state or {})
            _check(issues, item['id'] + '_clean_durable_state', not reasons,
                   'Pending final held release: ' + ', '.join(reasons),
                   'planned_stop_prerequisite' if set(reasons) <=
                   {'lease_retained', 'bias_lease_retained', 'leases_retained'} else 'actionable_defect')
            record['state_blockers'] = reasons
        definitions.append(record)
    broker_config, _ = _load(plan['broker_config'])
    legacy = broker_config.get('instruments', {}).get('repump_1092_controller')
    retained = broker_config.get('retained_instrument_faults', {}).get('repump_1092_controller')
    preserved = (legacy is None or (isinstance(retained, dict) and
                 retained.get('resource') == legacy.get('resource') and bool(retained.get('fault'))))
    _check(issues, 'legacy_cld_quarantine_retained', preserved,
           'Old broker CLD entry must remain faulted or be removed only in a reviewed no-transport handoff.')
    runtime = Path(plan['broker_runtime']) / 'ldcon/instrument_service.py'
    code = runtime.read_text()
    _check(issues, 'pinned_broker_retains_faults',
           'retained_instrument_faults=config.get' in code and 'instrument.fault' in code,
           'Pinned broker must implement retained fault startup; common older runtime must not replace it.')
    for name in ('final_held_snapshot', 'source_owner_stop', 'post_stop_final_sync', 'target_first_read_comparison'):
        _check(issues, name, False, 'Reserved for the separately authorized future cutover.',
               'planned_stop_prerequisite')
    for note in plan.get('optional_configuration', []):
        _check(issues, note['id'], False, note['detail'], 'optional_configuration')
    defects = [i['code'] for i in issues if i['status'] == 'actionable_defect']
    return dict(schema_version=1, mode=MODE, checked_utc=datetime.now(timezone.utc).isoformat(),
                preparation_checks_passed=not defects, actionable_defects=defects,
                cutover_authorized=False, handoff_executed=False, services_changed=False,
                hardware_writes=False, checks=issues, definitions=definitions, entry_previews=entry_previews)


def _inactive_services(units, source_user_uid):
    if os.getuid() != source_user_uid:
        raise ValueError('Run final state capture as the original source user, not the target service user')
    environment = dict(os.environ, XDG_RUNTIME_DIR=f'/run/user/{source_user_uid}',
                       DBUS_SESSION_BUS_ADDRESS=f'unix:path=/run/user/{source_user_uid}/bus')
    for unit in units:
        result = subprocess.run(['/usr/bin/systemctl', '--user', 'show', unit,
                                 '--property=ActiveState,SubState,MainPID'],
                                check=True, capture_output=True, text=True, env=environment)
        state = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
        if state.get('ActiveState') != 'inactive' or state.get('MainPID') != '0':
            raise ValueError('Source service is not inactive: ' + unit)


def stage_final_state(plan, held_inventory, output, *, now=None, service_check=None):
    """After a separately authorized stop, capture clean state to a NEW capsule.

    This never stops a service or activates the capsule. Existing live snapshots
    are deliberately rejected. A capsule alone is not cutover authorization.
    """
    _validate_plan(plan)
    now = time.time() if now is None else now
    if not 0 <= now - _time(held_inventory['created_utc']) <= 120:
        raise ValueError('Fresh held snapshot within 120 seconds is required')
    if (held_inventory.get('errors') != [] or held_inventory.get('read_only') is not True
            or held_inventory.get('hardware_writes') is not False):
        raise ValueError('Successful explicit read-only held evidence required')
    services = held_inventory.get('services')
    if (not isinstance(services, dict) or len(services) != len(plan['final_sync_source_units'])
            or any(not isinstance(v, dict) for v in services.values())
            or {v.get('unit') for v in services.values()} != set(plan['final_sync_source_units'])):
        raise ValueError('Held inventory must cover every original source service exactly once')
    if any(not isinstance(v.get('identity'), dict) or not v['identity'].get('boot_id') for v in services.values()):
        raise ValueError('All source services need explicit held-snapshot identities')
    boot_ids = {v['identity']['boot_id'] for v in services.values()}
    if len(boot_ids) != 1:
        raise ValueError('All source services need one explicit held-snapshot boot identity')
    deployments = held_inventory.get('deployments')
    if not isinstance(deployments, dict) or set(deployments) != {'sr422', 'sr1092'}:
        raise ValueError('Held inventory must contain both laser deployments')
    for deployment in deployments.values():
        if not isinstance(deployment, dict) or not isinstance(deployment.get('active_jobs'), list):
            raise ValueError('Explicit active_jobs list is required for both lasers')
        if any(not isinstance(j, dict) or j.get('status') not in TERMINAL for j in deployment['active_jobs']):
            raise ValueError('Active or unknown job prevents final state capture')
    owners = held_inventory.get('owners')
    if not isinstance(owners, dict) or not {'broker', 'esp', 'cld', 'scope'} <= set(owners):
        raise ValueError('All four successful owner probes are required')
    if any(not isinstance(owners[k], dict) for k in ('broker', 'esp', 'cld', 'scope')):
        raise ValueError('Owner probes must be mappings')
    broker, esp, cld, scope = [owners[k] for k in ('broker', 'esp', 'cld', 'scope')]
    for name, owner in (('Sr422', broker), ('Sr1092', esp), ('CLD', cld)):
        if 'fault' not in owner or owner['fault'] is not None:
            raise ValueError(name + ' requires an explicit fault-free result')
    broker_config = broker.get('snapshot', {}).get('servo_config', {})
    esp_config = esp.get('sample', {}).get('servo_config', {})
    if (broker.get('lease_retained') is not False or
            type(broker_config.get('pid_multiplier')) not in (int, float) or
            broker_config['pid_multiplier'] != 0):
        raise ValueError('Sr422 must be fault-free, unleased, and PI-held')
    if (esp.get('native_pi_active') is not False or esp.get('leased') is not False or
            esp.get('fresh') is not True or type(esp_config.get('pid_multiplier')) not in (int, float) or
            esp_config['pid_multiplier'] != 0):
        raise ValueError('Sr1092 must be fault-free, fresh, unleased, and PI-held')
    if (type(cld.get('leases')) is not list or cld['leases'] != [] or
            cld.get('bias_held') is not False or cld.get('fresh') is not True or
            cld.get('sample', {}).get('ld_enabled') is not True or
            cld.get('sample', {}).get('tec_enabled') is not True):
        raise ValueError('CLD bias lease must be released with LD and TEC still enabled')
    if scope.get('fresh') is not True or scope.get('read_ok') is not True or scope.get('read_only') is not True:
        raise ValueError('Read-only scope owner requires a fresh successful probe')
    check_services = service_check or (lambda units: _inactive_services(units, plan['source_user_uid']))
    check_services(plan['final_sync_source_units'])
    output = Path(output)
    if not output.is_absolute() or output.exists() or any(p.is_symlink() for p in output.parents):
        raise ValueError('A new absolute capsule directory is required')
    records, definitions = [], []
    for item in plan['owners']:
        source_config, source_sha = _load(item['source_definition_file'])
        target, prepared_sha = _load(item['prepared_definition_file'])
        definitions.append(dict(owner=item['id'],
                                source_definition_file=item['source_definition_file'],
                                prepared_definition_file=item['prepared_definition_file'],
                                source_file_sha256=source_sha, prepared_file_sha256=prepared_sha))
        if not set(_difference(source_config, target)) <= set(item.get('allowed_difference_pointers', [])):
            raise ValueError('Configuration changed after preparation; regenerate reviewed conversion')
        if item['kind'] == 'monitor':
            continue
        src = Path(item['source_state'])
        if item['kind'] == 'ecdl':
            if src.exists():
                raise ValueError('ECDL quarantine must not be cleared or translated')
            continue
        state, sha = _load(src)
        if not (state.get('definition') == policy_definition(item['kind'], target)
                == policy_definition(item['kind'], source_config)):
            raise ValueError('Saved owner definition differs from prepared configuration')
        if _state_reasons(item['kind'], state):
            raise ValueError('Retained lease, pending write, or fault prevents final copy')
        records.append(dict(owner=item['id'], source=str(src), sha256=sha,
                            target=item['target_state'], file=item['id'] + '/' + src.name))
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix='.final-owner-state-', dir=output.parent))
    try:
        for record in records:
            target = temporary / record['file']
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(record['source'], target)
            if _load(target)[1] != record['sha256'] or _load(record['source'])[1] != record['sha256']:
                raise ValueError('Source state changed during final copy')
            target.chmod(0o660)
        check_services(plan['final_sync_source_units'])
        for definition in definitions:
            if (_load(definition['source_definition_file'])[1] != definition['source_file_sha256']
                    or _load(definition['prepared_definition_file'])[1] != definition['prepared_file_sha256']):
                raise ValueError('Owner definition changed during final capture')
        manifest = dict(schema_version=1, mode='inert-clean-owner-state-capsule',
                        plan_sha256=_digest(plan), held_inventory_sha256=_digest(held_inventory),
                        source_units=plan['final_sync_source_units'],
                        boot_id=next(iter(boot_ids)),
                        definitions=definitions, source_user_uid=plan['source_user_uid'],
                        files=records, created_utc=datetime.now(timezone.utc).isoformat(),
                        hardware_writes=False, services_changed=False, activated=False,
                        handoff_executed=False, cutover_authorized=False,
                        held_outputs=dict(sr422=broker['snapshot']['outputs'],
                                          sr1092_dac=esp['sample']['dac'], cld=cld['sample']),
                        still_required=['final configuration and history delta',
                                        'physical lock ownership handoff',
                                        'target owner first-read output comparison'])
        (temporary/'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
        for directory in (temporary, *(p for p in temporary.rglob('*') if p.is_dir())):
            directory.chmod(0o2770)
        temporary.rename(output)
        return manifest
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
