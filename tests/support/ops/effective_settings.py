"""Print every effective setting leaf of one laser with its source (rule M, read-only).

    python3 ops/effective_settings.py --laser sr422 [--root /opt/ldcon/var] [--json] [--owner-rpc]

One row per leaf: ``path | value | source | class | live``. Sources:

  yaml:<file>                 layer 2, the laser yaml after interface_schema.validate_laser (the owner's policy input).
                              sr1092 (rule M steps 3-4, 2026-10-27): the CLD conditions are projected to the CLD owner
                              and the native policy / FF profile are adopted by the native owner at start and by
                              policy_update, so a leaf carries ``[layer 2: projected to <owner>]`` or
                              ``[layer 2: adopted by <owner>]``; while an untrimmed config.json still duplicates it,
                              ``[owner-config copy ignored =]`` or ``[owner-config copy ignored DIVERGED: <value>]``
                              (ops/trim_owner_config.py removes the copies); the native physical leaves
                              (firmware_version, dac, adc, calibration) are layer 1 and carry ``[owner-config physical =]``
  owner-config:<file>         layer 1, every leaf of the device/instrument owner configs that mention the laser;
                              a layer-2 copy that the owner ignores is marked ``[layer1 copy ignored]`` (live: ignored)
  assignment / deck           card parameters resolved by interface_schema.parameters (request -> assignment -> deck)
  code:<module>.DEFAULTS      a card parameter that neither the assignment nor the deck sets (steps 7-8 move them)
  code:<module>.<DICT>        the non-DEFAULTS code dicts of design section 1 row 17, overlaid by yaml where the code does
  request-default             a value the code takes from a request .get(key, literal) (step 9); ``None`` = no cap
  derived:ecdl_owner.axis_policies   the policy values the rig owner derives (max_step / slew from update_interval_s)
  owner-rpc:<endpoint>        with --owner-rpc only: describe() of each owner (policy_adoption, policies, revisions)

Use: save ``--json`` before and after a step and diff; a difference outside the intended keys blocks the deployment.
Nothing here writes: no owner state, no files, no RPC unless --owner-rpc (read-only describe).
"""
import argparse
import importlib
import json
import os
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
for candidate in (REPO / 'packages' / 'ldcon' / 'src', REPO / 'packages' / 'experiment-core' / 'src'):
    if candidate.is_dir() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from ldcon.agent_authority import classify, live_class  # noqa: E402
from ldcon.interface_schema import load_mapping, validate_deck, validate_laser  # noqa: E402

DEFAULT_ROOT = '/opt/ldcon/var'
RIG = 'ecdl.rig.owner'
NATIVE = 'esp32servocon.native_current.owner'
CLD = 'thorlabs.cld1015.broker'

# operation -> (module, name of its defaults dict); from interface_schema._parameters
OPERATION_DEFAULTS = {
    'study.ecdl_mode_map': ('ecdl_mode_map', 'DEFAULTS'), 'plan.ecdl_recovery': ('ecdl_recovery_plan', 'DEFAULTS'),
    'test.ecdl_recovery': ('ecdl_recovery_test', 'DEFAULTS'), 'prepare.ecdl_recovery': ('ecdl_recovery', 'DEFAULTS'),
    'tune.ecdl_branch_hop': ('ecdl_branch_hop', 'DEFAULTS'),
    'recover.ecdl_mode_hop': ('ecdl_mode_recovery', 'DEFAULTS'), 'laser.emission': ('emission_card', 'DEFAULTS'),
    'tune.operating_point': ('operating_point', 'DEFAULTS'), 'tune.lock_target': ('lock_target', 'DEFAULTS'),
    'tune.pi_response': ('pi_response', 'DEFAULTS'), 'study.target_response': ('target_response', 'DEFAULTS'),
    'tune.device_parameters': ('device_parameters', 'DEFAULTS'), 'tune.ecdl_operating_point': ('ecdl_operating_point', 'DEFAULTS'),
    'notify.teams': ('teams_card', 'DEFAULTS'), 'acquire.metrics': ('metric_cards', 'DEFAULTS'),
    'agent.resident': ('resident_agent', 'parameters'), 'agent.request': ('resident_agent', 'parameters'),
    'agent.task': ('agent_task', 'DEFAULTS'), 'agent.history_review': ('agent_task', 'DEFAULTS'),
    'agent.monitor': ('agent_monitor', 'DEFAULTS'), 'agent.log_watch': ('agent_log_watch', 'DEFAULTS'),
    'tune.native_current': ('native_current_acquire', 'DEFAULTS'), 'servo.native_current': ('native_current_card', 'DEFAULTS'),
    'sweep.servo_modulation': ('modulation_sweep', 'DEFAULTS'), 'sweep.current_modulation': ('modulation_sweep', 'DEFAULTS'),
    'tune.sweep_tracking': ('sweep_tracking', 'DEFAULTS'), 'analyze.shot_sweep': ('shot_analysis', 'DEFAULTS'),
    'report.grafana_dashboard': ('grafana_dashboard', 'DEFAULTS'), 'study.ecdl_current_response': ('ecdl_response_study', 'DEFAULTS'),
    'commission.ecdl_pi': ('ecdl_pi_commission', 'DEFAULTS'), 'tune.ecdl_window_center': ('window_center', 'DEFAULTS'),
    'study.ecdl_locked_current_map': ('ecdl_locked_study', 'DEFAULTS'), 'servo.ecdl_steady_lock': ('ecdl_steady', 'DEFAULTS'),
    'study.ecdl_thermal_context': ('ecdl_thermal_context', 'DEFAULTS'), 'tune.ecdl_initial_point': ('ecdl_prelock', 'DEFAULTS'),
    'tune.pid_online': ('online_pid', 'DEFAULTS'), 'servo.current_pid': ('current_cards', 'DEFAULTS'),
    'identify.current_response': ('current_cards', 'DEFAULTS'), 'monitor.wavelength': ('interface_schema', 'MONITOR_DEFAULTS'),
    'tune.temperature_once': ('interface_schema', 'SEEK_DEFAULTS'),
}

# Owner-config duplicates of laser yaml blocks (design section 1 rows 12-14): (config prefix, device type, yaml suffix).
# Since steps 3-4 the owners ignore these copies (layer 1 keeps only the physical facts); the rows stay listed so the
# before/after diff shows the copies until ops/trim_owner_config.py removes them.
DUPLICATES = (
    ('native_current', NATIVE, 'control_settings.native_current'),
    ('native_ff_sweep', NATIVE, 'control_settings.native_ff_sweep'),
    ('controller.current_conditions', CLD, 'absolute_conditions.current_a'),
    ('controller.temperature_conditions', CLD, 'absolute_conditions.temperature_c'),
    ('controller.control_settings', CLD, 'control_settings'),
    ('controller.temperature_limits_c', CLD, 'absolute_conditions.temperature_c.bounds_c'),
)
# Layer 1 of the native owner (physical block) that the yaml also states: compared, never ignored.
NATIVE_PHYSICAL = ('firmware_version', 'dac', 'adc', 'calibration')
# Layer-2 copies an owner config.json may still carry (ignored by the owner since steps 3-4).
IGNORED_COPIES = {NATIVE: ('native_current', 'native_ff_sweep'),
                  CLD: ('controller.temperature_limits_c', 'controller.temperature_conditions', 'controller.current_conditions',
                        'controller.control_settings', 'controller.current_writes_enabled', 'controller.temperature_writes_enabled',
                        'controller.temperature_conditions_file', 'controller.policy_device')}
LAYER2_OWNER = {NATIVE: 'adopted by', CLD: 'projected to'}

# Non-DEFAULTS code dicts (design section 1 row 17): (module, dict name, needs rig?, needs native?)
CODE_DICTS = (
    ('native_ff_proxy', 'READY_DEFAULTS', False, True),
    ('window_actions', 'GUARD_DEFAULTS', True, False),
    ('operating_point', 'AXIS_DEFAULTS', False, False),
)


def leaves(value, path=()):
    """Flatten to the leaves agent_authority.changes reports: scalars, scalar lists, and indexed lists of mappings."""
    if isinstance(value, dict):
        if not value:
            yield path, value
        for key in sorted(value, key=str):
            yield from leaves(value[key], path + (str(key),))
    elif isinstance(value, list) and value and any(isinstance(item, (dict, list)) for item in value):
        for index, item in enumerate(value):
            yield from leaves(item, path + (str(index),))
    else:
        yield path, value


def row(path, value, source, klass='', live=''):
    return dict(path=path, value=value, source=source, **{'class': klass}, live=live)


def _lookup(document, path):
    for key in path:
        if isinstance(document, dict) and key in document:
            document = document[key]
        elif isinstance(document, list) and key.isdigit() and int(key) < len(document):
            document = document[int(key)]
        else:
            return None, False
    return document, True


def _read_yaml(path):
    return load_mapping(Path(path).read_text(encoding='utf-8'))


def _laser_root(root, laser):
    return Path(root) / 'lasers' / laser


def _configs(root, laser):
    """Owner configs (layer 1) that mention this laser, as (relative path, document)."""
    result = []
    for kind in ('device-owners', 'instrument-owners'):
        for path in sorted((Path(root) / kind).glob('*/config.json')):
            try:
                text = path.read_text(encoding='utf-8')
                document = json.loads(text)
            except (OSError, ValueError):
                continue
            if laser.lower() in text.lower():
                result.append((str(path.relative_to(root)), document))
    return result


def _config_kind(config):
    if config.get('driver') == 'esp32servocon' and (config.get('native_current') is not None or config.get('native_assembly') is not None):
        return NATIVE
    if isinstance(config.get('controller'), dict):
        return CLD
    return None


def _duplicates(laser_document, configs):
    """yaml path -> (config path, config value, kind) for every leaf an owner config duplicates or states (physical)."""
    result = {}
    devices = laser_document.get('devices', {})
    for relative, config in configs:
        physical = config.get('physical')
        if physical is None and isinstance(config.get('native_current'), dict):
            physical = {key: config['native_current'][key] for key in NATIVE_PHYSICAL if key in config['native_current']}
        for prefix, device_type, suffix in DUPLICATES:
            block, present = _lookup(config, tuple(prefix.split('.')))
            if not present:
                continue
            for key, device in devices.items():
                if device.get('type') != device_type:
                    continue
                for path, value in leaves(block):
                    kind = 'physical' if prefix == 'native_current' and path and path[0] in NATIVE_PHYSICAL else 'copy'
                    result['.'.join(('devices', key) + tuple(suffix.split('.')) + path)] = (relative, prefix, value, kind)
        if physical and 'native_current' not in config:
            for key, device in devices.items():
                if device.get('type') != NATIVE:
                    continue
                for path, value in leaves(physical):
                    result['.'.join(('devices', key, 'control_settings', 'native_current') + path)] = (relative, 'physical', value, 'physical')
    return result


def _layer2_owners(laser_document, configs):
    """yaml path prefix -> annotation of the owner that adopts/projects that block (steps 3-4)."""
    result = {}
    owners = {_config_kind(config): Path(relative).parent.name for relative, config in configs if _config_kind(config)}
    for key, device in laser_document.get('devices', {}).items():
        kind = device.get('type')
        if kind == NATIVE and NATIVE in owners:
            for block in ('native_current', 'native_ff_sweep'):
                result['devices.%s.control_settings.%s.' % (key, block)] = '[layer 2: adopted by %s]' % owners[NATIVE]
        elif kind == CLD and CLD in owners:
            for block in ('absolute_conditions', 'control_settings'):
                result['devices.%s.%s.' % (key, block)] = '[layer 2: projected to %s]' % owners[CLD]
    return result


def yaml_rows(laser_document, laser_file, duplicates, layer2=None):
    rows = []
    layer2 = layer2 or {}
    for path, value in leaves(laser_document):
        dotted = '.'.join(path)
        source = 'yaml:' + laser_file
        for prefix, note in layer2.items():
            if dotted.startswith(prefix):
                source += ' ' + note
        if dotted in duplicates:
            relative, prefix, other, kind = duplicates[dotted]
            label = 'owner-config physical' if kind == 'physical' else 'owner-config copy ignored'
            if kind == 'physical' and path[-1] == 'evidence':
                other = value               # calibration.evidence is free text, not part of the physical comparison
            source += ' [%s =]' % label if other == value else ' [%s DIVERGED: %s in %s]' % (label, json.dumps(other, sort_keys=True), relative)
        rows.append(row(dotted, value, source, classify('lasers', path), live_class('lasers', path)))
    return rows


def owner_config_rows(configs):
    rows = []
    for relative, document in configs:
        name = Path(relative).parent.name
        ignored = IGNORED_COPIES.get(_config_kind(document), ())
        for path, value in leaves(document):
            dotted = '.'.join(path)
            copy = any(dotted == prefix or dotted.startswith(prefix + '.') for prefix in ignored)
            if copy and path[0] == 'native_current' and len(path) > 1 and path[1] in NATIVE_PHYSICAL:
                copy = False            # the physical facts inside an untrimmed copy are layer 1
            source = 'owner-config:' + relative + (' [layer1 copy ignored; ops/trim_owner_config.py]' if copy else '')
            rows.append(row('owner-config.' + name + '.' + dotted, value, source, 'layer1', 'ignored' if copy else 'restart'))
    return rows


def _defaults(operation):
    module_name, attribute = OPERATION_DEFAULTS.get(operation, (None, None))
    if module_name is None:
        return None, None
    try:
        module = importlib.import_module('ldcon.' + module_name)
    except Exception:
        return module_name + '.' + attribute, None
    value = getattr(module, attribute, None)
    return module_name + '.' + attribute, value if isinstance(value, dict) else None


def card_rows(root, laser, laser_document):
    from ldcon.interface_schema import parameters
    rows = []
    base = _laser_root(root, laser) / 'config'
    assignment_file = base / 'assignments' / (laser + '.yaml')
    if not assignment_file.is_file():
        return rows
    assignment = _read_yaml(assignment_file)
    for index, entry in enumerate(assignment.get('decks', [])):
        deck_id = entry.get('deck_id')
        deck_file = base / 'interface-decks' / (str(deck_id) + '.yaml')
        try:
            deck = _read_yaml(deck_file)
            if not isinstance(deck,dict) or not isinstance(deck.get('cards'),list):
                raise ValueError('Deck card list required for read-only inventory')
        except (OSError, ValueError, TypeError, KeyError) as error:
            rows.append(row('cards.' + str(deck_id), None, 'error: ' + str(error)))
            continue
        for card in deck.get('cards', []):
            card_id = card['id']
            overrides = (entry.get('parameters') or {}).get(card_id, {})
            label, defaults = _defaults(card['operation'])
            valid = True
            try:
                resolved = parameters(card, overrides)
            except Exception as error:
                rows.append(row('cards.' + deck_id + '.' + card_id, None, 'error: ' + str(error)))
                # Read-only inventory: preserve obsolete saved fields for migration,
                # but never present their unvalidated values as executable settings.
                valid = False
                resolved = {**(defaults or {}), **(card.get('parameters') or {}), **overrides}
            for key in sorted(resolved, key=str):
                if key in overrides:
                    source = 'assignment:' + str(assignment_file.relative_to(root))
                elif key in (card.get('parameters') or {}):
                    source = 'deck:' + str(deck_file.relative_to(root))
                elif defaults is not None and key in defaults:
                    source = 'code:' + label
                else:
                    source = 'code:' + (label or card['operation']) + ' (resolver)'
                for path, value in leaves(resolved[key], (key,)):
                    authority_path = ('decks', str(index), 'parameters', card_id) + path
                    rows.append(row('cards.' + deck_id + '.' + card_id + '.' + '.'.join(path), value, source,
                                    classify('assignments', authority_path),
                                    live_class('assignments', authority_path) if valid else 'unvalidated'))
    return rows


def code_rows(laser_document):
    rows = []
    devices = laser_document.get('devices', {})
    has_rig = any(d.get('type') == RIG for d in devices.values())
    has_native = any(d.get('type') == NATIVE for d in devices.values())
    try:
        from ldcon.sweep_api import CODE_DEFAULTS
    except Exception:
        CODE_DEFAULTS = {}
    for key, device in devices.items():
        block = (device.get('control_settings') or {}).get('sweep_api_defaults')
        if not isinstance(block, dict) and device.get('type') not in (RIG,):
            continue
        kind = 'rig' if device.get('type') == RIG else 'native'
        block = block if isinstance(block, dict) else {}
        for name, value in sorted(CODE_DEFAULTS.get(kind, {}).items()):
            present = name in block
            rows.append(row('sweep_api.' + key + '.' + name, block[name] if present else value,
                            'yaml:control_settings.sweep_api_defaults' if present else 'code:sweep_api.CODE_DEFAULTS[%s]' % kind,
                            classify('lasers', ('devices', key, 'control_settings', 'sweep_api_defaults', name)) if present else '',
                            live_class('lasers', ('devices', key, 'control_settings', 'sweep_api_defaults', name)) if present else ''))
        continuous = block.get('continuous') if isinstance(block.get('continuous'), dict) else {}
        cap = continuous.get('max_slope_hz_per_s')
        rows.append(row('sweep_api.' + key + '.continuous.max_slope_hz_per_s', cap,
                        'yaml:control_settings.sweep_api_defaults' if 'max_slope_hz_per_s' in continuous else 'request-default (no cap)'))
    for module_name, name, rig_only, native_only in CODE_DICTS:
        if (rig_only and not has_rig) or (native_only and not has_native):
            continue
        try:
            value = getattr(importlib.import_module('ldcon.' + module_name), name)
        except Exception as error:
            rows.append(row('code.' + module_name + '.' + name, None, 'error: ' + str(error)))
            continue
        for path, leaf in leaves(value):
            rows.append(row('code.' + module_name + '.' + name + '.' + '.'.join(path), leaf, 'code:' + module_name + '.' + name))
    return rows


def derived_rows(laser_document):
    rows = []
    for key, device in laser_document.get('devices', {}).items():
        if device.get('type') != RIG:
            continue
        try:
            from dataclasses import asdict
            from ldcon.ecdl_owner import axis_policies
            policies = {axis: asdict(policy) for axis, policy in axis_policies(device).items()}
        except Exception as error:
            rows.append(row('derived.' + key + '.policy', None, 'error: ' + str(error)))
            continue
        for path, value in leaves(policies):
            rows.append(row('derived.' + key + '.policy.' + '.'.join(path), value, 'derived:ecdl_owner.axis_policies'))
    return rows


def owner_rows(laser, laser_document, rpc):
    rows = []
    for key, device in laser_document.get('devices', {}).items():
        endpoint = (device.get('connection') or {}).get('endpoint')
        if not endpoint or device.get('type') not in (RIG, NATIVE):
            continue
        request = dict(action='describe')
        if device.get('type') == RIG:
            request = dict(action='ecdl', instrument=device['connection'].get('instrument', laser), request=request)
        else:
            # the native owner refuses every request whose device_id is not its own ("Native device identity mismatch")
            request['device_id'] = device['connection'].get('device_id')
        try:
            description = rpc(endpoint, request)
        except Exception as error:
            rows.append(row('owner.' + key, None, 'owner-rpc:' + endpoint + ' error: ' + str(error)))
            continue
        for path, value in leaves(description):
            rows.append(row('owner.' + key + '.' + '.'.join(path), value, 'owner-rpc:' + endpoint))
    return rows


def effective(root, laser, *, rpc=None):
    root = Path(root)
    laser_file = _laser_root(root, laser) / 'config' / 'lasers' / (laser + '.yaml')
    laser_document = _read_yaml(laser_file)
    validate_laser(laser_document)
    configs = _configs(root, laser)
    rows = []
    rows += yaml_rows(laser_document, str(laser_file.relative_to(root)), _duplicates(laser_document, configs),
                      _layer2_owners(laser_document, configs))
    rows += owner_config_rows(configs)
    rows += card_rows(root, laser, laser_document)
    rows += code_rows(laser_document)
    rows += derived_rows(laser_document)
    if rpc is not None:
        rows += owner_rows(laser, laser_document, rpc)
    rows.sort(key=lambda r: r['path'])
    return dict(laser=laser, rows=rows)


def table(result):
    rows = result['rows']
    rendered = [(r['path'], json.dumps(r['value'], sort_keys=True, ensure_ascii=False), r['source'], r['class'], r['live']) for r in rows]
    header = ('path', 'value', 'source', 'class', 'live')
    widths = [min(max(len(str(line[i])) for line in rendered + [header]), 80) for i in range(5)]
    lines = [' | '.join(str(header[i]).ljust(widths[i]) for i in range(5)), '-+-'.join('-' * w for w in widths)]
    for line in rendered:
        lines.append(' | '.join(str(line[i])[:widths[i]].ljust(widths[i]) for i in range(5)))
    lines.append('%d leaves (%s)' % (len(rows), result['laser']))
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--laser', required=True)
    parser.add_argument('--root', default=os.environ.get('LDCON_VAR', DEFAULT_ROOT))
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--owner-rpc', action='store_true', help='also read describe() from each owner (read-only RPC)')
    args = parser.parse_args(argv)
    rpc = None
    if args.owner_rpc:
        from ldcon.configuration import owner_rpc
        rpc = owner_rpc
    result = effective(args.root, args.laser, rpc=rpc)
    if args.json:
        print(json.dumps(result, indent=1, sort_keys=True, ensure_ascii=False, allow_nan=False))
    else:
        print(table(result))
    return 0


if __name__ == '__main__':
    sys.exit(main())
