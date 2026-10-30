"""Read-only observations of explicitly configured laser owner endpoints.

No transport discovery, claim, reconnect, inspect, policy adoption or device I/O
is requested here. Observed owner revisions remain separate from configured files.
"""
import time


def describe_owners(laser, *, rpc=None):
    if rpc is None:
        from ldcon.core.configuration import owner_rpc
        rpc = owner_rpc
    owners = []
    for device_id, device in laser.get('devices', {}).items():
        kind = device.get('type')
        connection = device.get('connection') or {}
        if kind not in ('esp32servocon.native_current.owner', 'ecdl.rig.owner'):
            continue
        row = dict(device_id=device_id, kind=kind, status='unavailable',
                   effective_policy_revision=None, observed=None)
        endpoint = connection.get('endpoint')
        try:
            if not endpoint:
                raise ValueError('Configured owner endpoint is absent')
            if kind == 'esp32servocon.native_current.owner':
                identity = connection['device_id']
                def request(action):
                    return rpc(endpoint, dict(action=action, device_id=identity), timeout_s=1.)
                description = request('describe')
                if description.get('device_id') != identity:
                    raise ValueError('Native owner identity differs from configured binding')
                observed = request('status')
                effective = (observed.get('policy_authority') or {}).get('effective_revision') or description.get('policy_digest')
                fields = ('fault','leased','native_pi_active','read_only','input_valid','policy_authority',
                          'read_ok','state','last_success_unix_s')
            else:
                def request(action):
                    return rpc(endpoint, dict(action='ecdl', instrument=laser['id'], request=dict(action=action)), timeout_s=1.)
                description = request('describe')
                if description.get('laser_id') != laser['id']:
                    raise ValueError('ECDL owner identity differs from configured binding')
                observed = request('status')
                if observed.get('rig') != laser['id']:
                    raise ValueError('ECDL owner status differs from configured binding')
                effective = description.get('policy_adoption', {}).get('effective_revision')
                fields = ('fault','busy','connected','writes_enabled','locked_sweep','native_input')
            row.update(status='observed', effective_policy_revision=effective,
                capabilities=description.get('capabilities'),
                policy_adoption=description.get('policy_adoption'),
                observed={key:observed.get(key) for key in fields}, received_unix_s=time.time(), freshness='Owner report receipt only; source timestamps must be checked separately')
        except Exception as error:
            row['reason'] = str(error)
        owners.append(row)
    return dict(status=('not_connected' if not owners else 'observed' if all(
                    row['status']=='observed' for row in owners) else 'unavailable'),
                effective_policy_revision={row['device_id']:row['effective_policy_revision'] for row in owners} or None,
                observed=owners or None, evidence='Independent read-only owner report; no write grant or physical stop assertion')
