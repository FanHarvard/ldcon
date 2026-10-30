"""Pure migration of configured cards; no service or instrument operations."""
import copy


def migrate(deck, assignment, *, manual_recovery=False):
    deck, assignment = copy.deepcopy(deck), copy.deepcopy(assignment)
    removed = {c['id'] for c in deck['cards'] if c['operation'] == 'agent.log_watch'}
    deck['cards'] = [c for c in deck['cards'] if c['id'] not in removed]
    for entry in assignment['decks']:
        if entry['deck_id'] != deck['id']:
            continue
        for ident in removed:
            entry['parameters'].pop(ident, None)
        if manual_recovery:
            lock = next(c for c in deck['cards'] if c['operation'] == 'servo.ecdl_steady_lock')
            recovery = next(c for c in deck['cards'] if c['operation'] == 'recover.ecdl_mode_hop')
            effective = {**lock['parameters'], **entry['parameters'].get(lock['id'], {})}
            procedure = copy.deepcopy(effective['recovery']['procedure'])
            # Start-time laser.target is authoritative. Do not freeze an old target.
            assert procedure.get('target_frequency_hz') is None
            existing = entry['parameters'].get(recovery['id'], {})
            if existing and existing != procedure:
                raise ValueError('Existing manual recovery requires a reviewed merge')
            entry['parameters'][recovery['id']] = procedure
    return deck, assignment
