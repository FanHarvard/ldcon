"""Resident MCP tool ``device_parameters`` (rule I): catalog / change through this laser's dashboard route.

The dashboard (host process, outside the sandbox) runs ``device_parameters.change`` with the host approval ledger;
the agent never holds a lease, an owner endpoint or a registry token."""
import json
import re

from ldcon.agents.agent_cards import _api


def call(box, name, args):
    if name != 'device_parameters':
        raise ValueError('Unknown device-parameter tool')
    action = args.get('action')
    base = 'lasers/' + box._laser_id + '/device-parameters'
    if action == 'catalog':
        refresh = 'true' if args.get('refresh', '').lower() in ('1', 'true', 'yes') else 'false'
        return _api(box, base + '?refresh=' + refresh)
    if action != 'change':
        raise ValueError('action must be catalog or change')
    if not args.get('request_id') or not args.get('changes_json'):
        raise ValueError('change needs changes_json and a stable request_id')
    changes = json.loads(args['changes_json'], parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Finite values required')))
    if not isinstance(changes, list) or not 1 <= len(changes) <= 16:
        raise ValueError('changes_json must list 1..16 changes')
    payload = dict(changes=changes)
    if args.get('approval_id'):
        if not re.fullmatch('[0-9a-f]{32}', args['approval_id']):
            raise ValueError('approval_id must be the 32-hex id from safety_change_approve')
        payload['approval_id'] = args['approval_id']
    dry_run = args.get('dry_run', '').lower() in ('1', 'true', 'yes')
    if dry_run:
        payload['dry_run'] = True
        return _api(box, base, 'POST', payload)

    def apply():
        value = _api(box, base, 'POST', payload)
        if value.get('status') == 'rejected':
            return value
        return dict(value, note='Applied and verified by the owners (readback). Not rolled back automatically.')
    return box.once(name, args, apply)
