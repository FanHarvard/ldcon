"""Read host-owned monitor grants at the owner effect boundary."""
import os
import hashlib
from pathlib import Path
import time
from experiment_core.records import safe_id
from ldcon.jobs.adaptive_window_session import bounded_json,finite


def read_grant(system_id,identity,*,lease=None):
    required={'monitor_id','generation','controller_job_id','grant_revision'}
    measurement=isinstance(identity,dict) and 'measurement_job_id' in identity
    if measurement:required.add('measurement_job_id')
    if not isinstance(identity,dict) or set(identity)!=required or not all(isinstance(v,str) and v for v in identity.values()):
        raise ValueError('Exact monitor, controller and grant revision identity required')
    root=Path(os.environ.get('LDCON_ADAPTIVE_ROOT',''))
    if not root.is_absolute() or not root.is_dir() or root.resolve()!=root:
        raise ValueError('Trusted adaptive root is required in the owner')
    directory=root/safe_id(system_id);monitor=safe_id(identity['monitor_id']);job=safe_id(identity['controller_job_id'])
    paths=[directory,directory/'monitors',directory/job,directory/'monitors'/(monitor+'.json'),directory/job/'background-control.json']
    if measurement:
        measured=safe_id(identity['measurement_job_id'])
        own=directory/'measurements'/measured
        paths=[directory,directory/'monitors',directory/'measurements',own,paths[-2],own/'control.json']
    if any(p.is_symlink() for p in paths):raise ValueError('Background authority must not use symlinks')
    try:grant=bounded_json(paths[-2]);control=bounded_json(paths[-1])
    except (OSError,ValueError) as error:raise ValueError('Background authority is unreadable') from error
    if (not isinstance(grant,dict) or not isinstance(control,dict) or grant.get('enabled') is not True
        or not isinstance(grant.get('bounds'),dict) or grant['bounds'].get('enabled') is not True or control.get('enabled') is not True
        or any(control.get(k)!=identity[k] for k in ('monitor_id','generation'))
        or grant.get('generation')!=identity['generation'] or grant.get('parameters_hash')!=identity['grant_revision']
        or not finite(grant.get('last_seen')) or not finite(grant.get('max_age_s'))
        or not 0<grant['max_age_s']<=180 or not 0<=time.time()-grant['last_seen']<=grant['max_age_s']):
        raise ValueError('Background monitor/task grant is revoked or stale')
    if measurement and any(value.get('source')!='measurement' or value.get('schema_version')!=2
            or any(value.get(k)!=identity[k] for k in required) for value in (grant,control)):
        raise ValueError('Measurement grant/controller identity differs')
    if measurement:
        if not isinstance(lease,str) or not lease:
            raise ValueError('Active controller lease required for measurement')
        lease_digest=hashlib.sha256(lease.encode()).hexdigest()
        if any(value.get('controller_lease_sha256')!=lease_digest for value in (grant,control)):
            raise ValueError('Measurement controller lease was replaced')
    return grant['bounds']
