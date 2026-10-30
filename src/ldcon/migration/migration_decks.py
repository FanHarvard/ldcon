"""Pure, audited conversion to canonical decks without touching live control.

Only reusable card definitions come from the canonical deck. All device values
come from the supplied live laser, and existing card values become per-laser
assignment overrides. Non-equivalent retired workflows are archived rather than
misrepresented as measured mode maps or verified recovery evidence.
"""
from __future__ import annotations

import copy
from pathlib import PurePosixPath

from ldcon.core.interface_schema import (OPERATION_ALIASES, RETIRED_OPERATIONS, digest,
                               parameters, readiness, validate_deck,
                               validate_laser)


REPLACEMENTS = {
    'tune.ecdl_initial_point': ['study.ecdl_mode_map', 'tune.ecdl_operating_point'],
    'study.ecdl_thermal_context': ['study.ecdl_mode_map'],
    'study.ecdl_current_response': ['study.ecdl_mode_map', 'tune.ecdl_window_center'],
    'study.ecdl_locked_current_map': ['tune.ecdl_window_center'],
}


def rewrite_paths(value, mapping=None):
    """Rewrite absolute path values at directory boundaries, with an audit.

    Embedded paths in prose and network URLs are not treated as executable path
    references. The caller must copy referenced evidence separately and verify
    its content; a rewritten path is not evidence of a completed copy.
    """
    pairs = []
    for source, destination in (mapping or {}).items():
        for path in (source, destination):
            if not isinstance(path, str) or not path.startswith('/') or '..' in PurePosixPath(path).parts:
                raise ValueError('Path rewrites require absolute paths without parent traversal')
        source, destination = source.rstrip('/'), destination.rstrip('/')
        if not source or not destination:
            raise ValueError('Root-directory rewrites are not permitted')
        pairs.append((source, destination))
    pairs.sort(key=lambda pair: len(pair[0]), reverse=True)
    changes = []

    def visit(item, pointer):
        if isinstance(item, dict):
            return {key: visit(child, pointer + '/' + str(key).replace('~', '~0').replace('/', '~1'))
                    for key, child in item.items()}
        if isinstance(item, list):
            return [visit(child, pointer + '/' + str(index)) for index, child in enumerate(item)]
        if isinstance(item, str):
            for source, destination in pairs:
                if item == source or item.startswith(source + '/'):
                    result = destination + item[len(source):]
                    if result != item:
                        changes.append(dict(pointer=pointer, source=item, target=result,
                                            content_copy_verified=False))
                    return result
        return copy.deepcopy(item)

    return visit(value, ''), changes


def _assignment_entry(assignment, deck_id, laser_id):
    if assignment.get('schema_version') != 1 or assignment.get('laser_id') != laser_id:
        raise ValueError('Assignment does not identify the supplied laser')
    if set(assignment) != {'schema_version', 'id', 'laser_id', 'decks'}:
        raise ValueError('Unknown assignment fields must be reviewed')
    entries = assignment.get('decks')
    if not isinstance(entries, list) or len(entries) != 1 or entries[0].get('deck_id') != deck_id:
        raise ValueError('Convert one explicitly selected assignment deck at a time')
    entry = entries[0]
    if set(entry) != {'deck_id', 'parameters'} or not isinstance(entry['parameters'], dict):
        raise ValueError('Invalid assignment deck parameters')
    return entry


def _copy_discovery_targets(archive, target, report):
    """Copy only units/meaning-preserving target fields into new draft cards."""
    initial = next((item for item in archive if item['operation'] == 'tune.ecdl_initial_point'), None)
    if initial is None:
        return
    values = initial['effective_parameters']
    for card_id, keys in (
            ('mode-map', ('target_frequency_hz', 'target_wavelength_nm', 'measurement_timeout_s')),
            ('operating-point', ('target_frequency_hz', 'target_wavelength_nm', 'tolerance_hz',
                                 'measurement_timeout_s', 'stable_s', 'approach_gain'))):
        if card_id not in report['target_card_ids']:
            continue
        selected = {key: copy.deepcopy(values[key]) for key in keys if key in values}
        if selected:
            target.setdefault(card_id, {}).update(selected)
            report['parameter_mappings'].append(dict(source_card_id=initial['card_id'],
                target_card_id=card_id, keys=sorted(selected), kind='same_semantic_fields_only',
                note='No scan path, branch identity, calibration, recovery proof or centering target was inferred.'))


def convert_configuration(laser, deck, assignment, canonical_deck, *,
                          active_parameters=None, path_rewrites=None):
    """Return laser/deck/assignment/report/archive, performing no I/O.

    ``active_parameters`` maps card IDs to their latest complete effective
    parameters, including acknowledged runtime injection. They take precedence
    over stored settings and are recorded in the report. Unknown/incompatible
    active values remain visible in the target and produce blocking errors;
    callers must not write a deployable plan if ``report['blockers']`` is nonempty.
    New optional cards may be incomplete drafts; those issues are listed per
    card separately from cutover blockers. No card is started by conversion.
    """
    laser, deck, assignment, canonical_deck = copy.deepcopy((laser, deck, assignment, canonical_deck))
    validate_laser(laser)
    validate_deck(canonical_deck)
    if deck.get('id') != canonical_deck['id']:
        raise ValueError('Canonical deck ID must match the source assignment')
    entry = _assignment_entry(assignment, deck['id'], laser['id'])
    active = copy.deepcopy(active_parameters or {})
    if not isinstance(active, dict) or any(not isinstance(v, dict) for v in active.values()):
        raise ValueError('Active parameters must map card IDs to full parameter mappings')
    source_cards = {card['id']: card for card in deck['cards']}
    if len(source_cards) != len(deck['cards']):
        raise ValueError('Duplicate source card IDs')
    target_cards = {card['id']: card for card in canonical_deck['cards']}
    report = dict(schema='ldcon.migration-decks.v1', laser_id=laser['id'],
                  source_hashes=dict(laser=digest(laser), deck=digest(deck), assignment=digest(assignment)),
                  canonical_deck_hash=digest(canonical_deck), target_card_ids=list(target_cards),
                  card_mappings=[], parameter_mappings=[], active_parameter_overrides=[],
                  path_rewrites=[], archived_cards=[], warnings=[], blockers=[], card_readiness=[])
    archive = []
    overrides = {}
    for card_id in sorted((set(entry['parameters']) | set(active)) - source_cards.keys()):
        report['blockers'].append(dict(card_id=card_id, reason='Parameters refer to an absent source card'))
    for card_id, card in source_cards.items():
        operation = OPERATION_ALIASES.get(card['operation'], card['operation'])
        effective = {**copy.deepcopy(card['parameters']), **copy.deepcopy(entry['parameters'].get(card_id, {}))}
        if card_id in active:
            stored_effective = copy.deepcopy(effective)
            effective.update(active[card_id])
            report['active_parameter_overrides'].append(dict(card_id=card_id,
                parameters=copy.deepcopy(active[card_id]),
                changed_from_stored=sorted(key for key, value in active[card_id].items()
                                          if key not in stored_effective or value != stored_effective[key])))
        target = target_cards.get(card_id)
        if target is None or target['operation'] != operation:
            archive.append(dict(card_id=card_id, operation=card['operation'], card=copy.deepcopy(card),
                                assignment_parameters=copy.deepcopy(entry['parameters'].get(card_id, {})),
                                effective_parameters=effective))
            report['archived_cards'].append(card_id)
            report['card_mappings'].append(dict(source=card_id, operation=card['operation'],
                target=None, replacement_operations=REPLACEMENTS.get(card['operation'], []),
                disposition='archived_non_equivalent_workflow' if card['operation'] in RETIRED_OPERATIONS else 'archived_unmatched_card'))
            if card_id in active:
                report['blockers'].append(dict(card_id=card_id,
                    reason='Active card has no equivalent canonical card; automatic restart conversion is prohibited'))
            continue
        if operation == 'servo.ecdl_steady_lock' and card_id in active and type(active[card_id].get('tec_ff_paused')) is bool:
            device_id = laser['interfaces']['lock']['device']
            profile = laser['devices'][device_id]['control_settings'].get('steady_lock')
            if not isinstance(profile, dict):
                report['warnings'].append(dict(card_id=card_id, reason='No existing owner steady profile; runtime pause cannot be promoted'))
            hot = profile.setdefault('hot_control', {}) if isinstance(profile, dict) else {}
            old_pause = hot.get('initial_tec_ff_paused', False)
            current_pause = active[card_id]['tec_ff_paused']
            if isinstance(profile, dict) and old_pause != current_pause:
                hot['initial_tec_ff_paused'] = current_pause
                report['parameter_mappings'].append(dict(source_card_id=card_id, target_card_id=card_id,
                    kind='promote_acknowledged_runtime_pause_to_new_owner_initial_context',
                    pointer='/devices/'+device_id+'/control_settings/steady_lock/hot_control/initial_tec_ff_paused',
                    old_value=old_pause, value=current_pause,
                    note='Prepared owner and worker use the same laser definition; live configuration is not modified.'))
        overrides[card_id] = effective
        if card['operation'] == 'tune.temperature_once' and 'frequency_hz_per_c' in effective:
            coefficient = effective.pop('frequency_hz_per_c')
            if coefficient is not None:
                binding = laser['interfaces'].get('temperature', {})
                device = laser['devices'].get(binding.get('device'))
                if not device:
                    report['blockers'].append(dict(card_id=card_id, reason='Temperature calibration has no bound device'))
                else:
                    settings = device.setdefault('control_settings', {}).setdefault('temperature', {})
                    existing = settings.get('frequency_hz_per_c')
                    if existing is not None and existing != coefficient:
                        report['blockers'].append(dict(card_id=card_id, reason='Card/device temperature calibration conflict'))
                    else:
                        settings['frequency_hz_per_c'] = coefficient
            report['parameter_mappings'].append(dict(source_card_id=card_id, target_card_id=card_id,
                keys=['frequency_hz_per_c'], value=coefficient,
                kind='remove_empty_placeholder' if coefficient is None else 'move_to_bound_temperature_device'))
        report['card_mappings'].append(dict(source=card_id, target=card_id,
            operation=operation, disposition='operation_alias' if operation != card['operation'] else 'preserved'))
        report['parameter_mappings'].append(dict(source_card_id=card_id, target_card_id=card_id,
            keys=sorted(effective), kind='pin_source_parameters_in_laser_assignment'))
        # Fallback references are deployment behavior, not laser-independent
        # template defaults. Preserve and validate explicitly if configured.
        if 'fallback' in card:
            target['fallback'] = copy.deepcopy(card['fallback'])
    _copy_discovery_targets(archive, overrides, report)
    target_assignment = copy.deepcopy(assignment)
    target_assignment['decks'] = [dict(deck_id=canonical_deck['id'], parameters=overrides)]
    rewritten, changes = rewrite_paths(dict(laser=laser, deck=canonical_deck, assignment=target_assignment), path_rewrites)
    report['path_rewrites'] = changes
    laser, canonical_deck, target_assignment = (rewritten[k] for k in ('laser', 'deck', 'assignment'))
    try:
        validate_laser(laser)
        validate_deck(canonical_deck)
    except (ValueError, TypeError, KeyError) as error:
        report['blockers'].append(dict(reason='Target schema validation: ' + str(error)))
    for card in canonical_deck['cards']:
        card_id = card['id']
        values = target_assignment['decks'][0]['parameters'].get(card_id, {})
        try:
            normalized = parameters(card, values)
            result = readiness(laser, canonical_deck, card, values)
            issues = result['blocked_reasons']
        except (ValueError, TypeError, KeyError) as error:
            report['blockers'].append(dict(card_id=card_id, reason='Target parameter validation: ' + str(error)))
            issues = [str(error)]
            normalized = None
        if card['operation'] == 'tune.ecdl_window_center' and normalized:
            old_metrics = sorted(set(normalized['observables']) & {'metrics.A', 'metrics.B', 'metrics.C', 'metrics.F2'})
            phase_free = any(d.get('control_settings', {}).get('optical', {}).get('metric_method') == 'phase_free_v1'
                             for d in laser['devices'].values())
            if old_metrics and phase_free:
                issues = list(issues) + ['Legacy positional fringe metrics require review against the current phase-free classifier: ' + ', '.join(old_metrics)]
        report['card_readiness'].append(dict(card_id=card_id, operation=card['operation'],
            active=card_id in active, issues=issues,
            configuration_ready=not bool(issues), hardware_verification_performed=False))
        if card_id in active and issues:
            report['blockers'].append(dict(card_id=card_id, reason='Active card is not ready in converted configuration', issues=issues))
    added = [card_id for card_id in target_cards if card_id not in source_cards]
    report['added_cards'] = added
    report['warnings'].append('New cards are not automatically started; missing configuration and measured artifacts remain explicit drafts.')
    if archive:
        report['warnings'].append('Archived legacy settings are retained as evidence; they are not measured mode maps or tested recovery plans.')
    report['target_hashes'] = {key: digest(value) for key, value in
                              (('laser', laser), ('deck', canonical_deck), ('assignment', target_assignment))}
    report['conversion_valid'] = not bool(report['blockers'])
    return dict(laser=laser, deck=canonical_deck, assignment=target_assignment,
                report=report, archive=dict(schema='ldcon.migration-deck-archive.v1',
                laser_id=laser['id'], source_deck=deck, source_assignment=assignment, retired_cards=archive))
