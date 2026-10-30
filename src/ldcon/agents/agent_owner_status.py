"""Scoped read-only owner observations and conservative post-stop checks.

Never constructs a device client, claims a lease, clears a fault, or sends a
hardware command. Endpoints/actions come only from the assigned laser definition.
"""
from datetime import datetime
import json
import math
import os
import re
import time
from urllib.request import Request, build_opener, ProxyHandler

from ldcon.agents.agent_cards import _api, _scope, NoRedirect
from ldcon.agents.failure_policy import sanitize
from ldcon.core.instrument_client import validate_endpoint

NATIVE = 'esp32servocon.native_current.owner'
ECDL = 'ecdl.rig.owner'
CLD = 'thorlabs.cld1015.broker'
DLC = 'dlcpro.laser.owner'
TERMINAL = frozenset(('cancelled', 'failed', 'interrupted', 'succeeded', 'completed', 'completed_with_failures'))


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def owner_read(device, action):
    """Fixed allowlist of owner-only reads; redirect and arbitrary RPC forbidden."""
    kind = device['type']; connection = device['connection']
    endpoint = validate_endpoint(connection['endpoint'])
    payload = None
    if kind == NATIVE and action == 'status':
        path = '/v1/status'
    elif kind == CLD and action == 'status':
        path = '/status'
    elif kind == DLC and action == 'status':
        path='/rpc';payload=dict(action='pi_trial_state',laser_id=connection['laser_id'])
    elif kind == ECDL and action in ('status', 'inspect'):
        path = '/rpc'; payload = dict(action=action)
        if connection.get('instrument'):
            payload = dict(action='ecdl', instrument=connection['instrument'], request=payload)
    else:
        raise ValueError('Only typed owner status/inspect reads are allowed')
    headers = {'Content-Type': 'application/json'}
    token = os.environ.get('LDCON_INSTRUMENT_TOKEN')
    if token:headers['Authorization'] = 'Bearer ' + token
    request = Request(endpoint+path, data=None if payload is None else json.dumps(payload).encode(),
                      method='GET' if payload is None else 'POST', headers=headers)
    with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=5) as response:
        raw = response.read(2**20+1)
    if len(raw)>2**20:raise ValueError('Owner response exceeds limit')
    reply = json.loads(raw)
    if not isinstance(reply, dict):raise ValueError('Invalid owner response')
    if payload is not None:
        if reply.get('ok') is not True:raise ValueError('Owner read rejected')
        reply = reply['result']
    if not isinstance(reply, dict):raise ValueError('Invalid owner result')
    return reply


def observe(device_id, device):
    start = time.time();kind = device['type']
    result = dict(device_id=device_id, owner_type=kind, read_started_unix_s=start,
                  status='unavailable', fresh=False, pi_stopped=None, dac=None,
                  fixed_reference=None, lease_released=None, fault=None)
    try:
        value = owner_read(device, 'status');connection=device['connection']
        if kind == NATIVE:
            if value.get('device_id') != connection['device_id']:raise ValueError('Native owner identity mismatch')
            sample=value.get('sample') or {};cfg=sample.get('servo_config') or {}
            stamp=sample.get('read_finished_unix_s')
            fresh=(value.get('fresh') is True and value.get('read_ok') is True
                   and number(stamp) and stamp <= time.time())
            result.update(fresh=fresh, sample_unix_s=stamp,
                          pi_stopped=(value['native_pi_active'] is False and cfg['pid_multiplier'] == 0) if fresh and type(value.get('native_pi_active')) is bool and number(cfg.get('pid_multiplier')) else None,
                          dac=(sample.get('dac') or {}).get('native_code'),fixed_reference=cfg.get('control_offset'),
                          lease_released=not value['leased'] if type(value.get('leased')) is bool else None,
                          fault=sanitize(value.get('fault')), age_s=value.get('age_s'),
                          native_pi_active=value.get('native_pi_active'),polling_interval_ms=cfg.get('polling_interval'),firmware_version=sample.get('firmware_version'),sample_read_finished_unix_s=stamp)
            keys=('pid_kp','pid_ki','pid_kd','pid_integral_min','pid_integral_max','control_offset','target','system_min','system_max')
            result['native_config']={k:cfg[k] for k in keys if number(cfg.get(k))}
            if all(number(cfg.get(k)) for k in ('pid_ki','pid_integral_min','pid_integral_max')):
                result['integral_limit_count']=abs(cfg['pid_ki'])*max(abs(cfg['pid_integral_min']),abs(cfg['pid_integral_max']))
            background=value.get('background') or {}
            result['background_capability']={k:background[k] for k in ('system_id','supported_adjustments','pi_strategies','pi_candidate_fields','actual_pid','policy_revision','unsupported_adjustments','parameter_adjustments') if k in background}
        elif kind == DLC:
            sample=value.get('frequency') or {}
            age=value['observed_s']-sample['acquired_s']
            result.update(fresh=0<=age<=device['control_settings']['policy']['max_sample_age_s'],
                sample_unix_s=sample.get('acquired_unix_s'),age_s=age,
                pi_stopped=value['mode']=='idle',native_pi_active=value['mode']=='lock',
                lease_released=not value['controller_claimed'],fault=sanitize(value.get('fault')),
                output_pzt_v=value.get('applied_pzt_v'),control=value.get('control'),
                integral_v=value.get('integral_v'),pi_trial=value.get('trial'),
                evidence='Owner cached PI/verified-command state and fresh UDP acquisition; no independent chassis read')
        elif kind == CLD:
            if (value.get('instrument') != connection['instrument'] or value.get('resource') != connection['resource']):
                raise ValueError('CLD owner identity mismatch')
            leases=value.get('leases');bias=value.get('bias_held')
            result.update(fresh=value.get('fresh') is True, sample_unix_s=value.get('observed_at'),
                          lease_released=(not leases and bias is False) if isinstance(leases,list) and type(bias) is bool else None,
                          bias_held=bias, lease_count=len(leases) if isinstance(leases,list) else None,
                          fault=sanitize(value.get('fault')),age_s=value.get('age_s'))
        else:
            # Status is memory-only. Never reopen a disconnected/faulted rig just
            # to inspect it; unavailable hardware must stay explicitly unverified.
            if value.get('rig') != connection.get('instrument', device_id):raise ValueError('ECDL owner identity mismatch')
            result['fault']=sanitize(value.get('fault'))
            result['lease_released']=not value['busy'] if type(value.get('busy')) is bool else None
            if value.get('fault') or value.get('connected') is not True:
                result['reason']='Owner fault or disconnected transport; no hardware inspect attempted'
                return result
            value=owner_read(device,'inspect');snapshot=value.get('snapshot') or {}
            cfg=snapshot.get('servo_config') or {};servo=snapshot.get('servo_status') or {}
            background=value.get('background') or {}
            result['background_capability']={k:background[k] for k in ('supported_adjustments','unsupported_adjustments','parameter_adjustments','actual_parameters','epoch','foreground_ready') if k in background}
            result.update(fresh=True,sample_unix_s=time.time(),pi_stopped=(cfg['pid_multiplier'] == 0) if number(cfg.get('pid_multiplier')) else None,
                          dac=servo.get('dac_val'),fixed_reference=cfg.get('control_offset'),
                          open_loop=snapshot.get('open_loop'),outputs={k:v for k,v in (snapshot.get('outputs') or {}).items() if k in ('piezo','current','temperature') and number(v)},
                          lease_released=not value['lease_retained'] if type(value.get('lease_retained')) is bool else None,
                          fault=sanitize(value.get('fault')),evidence='Synchronous owner hardware read; optical acquisition time not implied')
        result['status']='observed' if result['fresh'] else 'stale'
    except (OSError, ValueError, KeyError, TypeError) as error:
        result.update(status='unavailable',fresh=False,reason=sanitize(str(error)))
    finally:
        result['read_finished_unix_s']=time.time()
    return result


def snapshot(devices):
    return [observe(key,device) for key,device in devices.items() if device.get('type') in (NATIVE,ECDL,CLD,DLC)]


def check_stop(first, second, terminal_s):
    """All checks are tri-state: True verified, False contradicted, None unknown."""
    checks=[]
    for old,new in zip(first,second):
        controller=new['owner_type'] in (NATIVE,ECDL)
        clean=not old.get('fault') and not new.get('fault')
        fresh=(old.get('fresh') is True and new.get('fresh') is True and clean
               and all(number(row.get('sample_unix_s')) and row['sample_unix_s'] >= terminal_s for row in (old,new)))
        independent=(fresh and new['sample_unix_s'] > old['sample_unix_s'])
        pi=(old.get('pi_stopped') is True and new.get('pi_stopped') is True) if fresh and controller else None
        dac=None
        if independent and controller and all(number(row.get(k)) for row in (old,new) for k in ('dac','fixed_reference')):
            dac=(old['dac']==new['dac']==old['fixed_reference']==new['fixed_reference'])
            if new['owner_type']==ECDL:dac=dac and old.get('open_loop') is True and new.get('open_loop') is True
        lease=(old['lease_released'] is True and new['lease_released'] is True) if fresh and all(type(row.get('lease_released')) is bool for row in (old,new)) else None
        checks.append(dict(device_id=new['device_id'],owner_type=new['owner_type'],pi_stopped=pi,
                           dac_held=dac,lease_released=lease,fresh_after_stop=fresh,
                           distinct_samples=independent,controller=controller))
    controllers=[row for row in checks if row['controller']]
    verified=bool(controllers) and all(row['lease_released'] is True and
                (not row['controller'] or (row['pi_stopped'] is True and row['dac_held'] is True)) for row in checks)
    return dict(status='verified' if verified else 'unverified',checks=checks)


def frozen_devices(box,job_id):
    # The owner definition used by the stopped job, not a newly reassigned device.
    from ldcon.agents.agent_log_read import _job, _regular_fd
    from ldcon.core.interface_schema import load_mapping
    with _job(box.root,box._laser_id,job_id) as directory:
        fd=_regular_fd(directory,'laser.yaml')
        with os.fdopen(fd,'r') as stream:
            raw=stream.read(2**20+1)
        if len(raw)>2**20:raise ValueError('Frozen laser definition exceeds limit')
        document=load_mapping(raw)
    if document.get('id')!=box._laser_id:raise ValueError('Frozen laser identity mismatch')
    return document.get('devices',{})


def call(box,name,args):
    bundle,deck=_scope(box)
    if deck is None:return bundle
    devices=bundle['laser_definition']['document'].get('devices',{})
    if name=='laser_owner_status':
        return dict(laser_id=box._laser_id,owners=snapshot(devices),read_only=True,
                    note='Owner observations only. Stale samples/faults do not prove physical PI or DAC state. Lease tokens and connection credentials are not returned.')
    if not re.fullmatch('[0-9a-f]{32}',args['job_id']):raise ValueError('Expected a job ID')
    job=_api(box,'jobs/'+args['job_id'])
    if job.get('status')=='rejected':return job
    if job.get('set_id')!=box.meta['set_id'] or job.get('laser',{}).get('id')!=box._laser_id:
        raise ValueError('Job belongs to another laser')
    if job.get('status') not in TERMINAL:
        return dict(status='pending',job_status=job.get('status'),note='Wait for cooperative terminal cleanup; no stop sent by this read tool.')
    if not any(c.get('operation',c.get('card')) in ('servo.native_current','servo.ecdl_steady_lock') for c in job.get('cards',[])):
        return dict(status='unsupported',note='No supported native/ECDL steady-lock card in this job; do not infer cleanup.')
    try:terminal_s=datetime.fromisoformat(job['updated_utc']).timestamp()
    except (KeyError,TypeError,ValueError):return dict(status='unverified',reason='Terminal timestamp unavailable')
    frozen=frozen_devices(box,job['id'])
    relevant={key:value for key,value in frozen.items() if value.get('type') in (NATIVE,ECDL,CLD)}
    if not relevant or any(key not in devices or value.get('type')!=devices[key].get('type')
            or value.get('connection')!=devices[key].get('connection') for key,value in relevant.items()):
        return dict(status='unverified',reason='Stopped-job owner definition differs from current assignment; no owner reads attempted')
    for device in relevant.values():
        if device['type']==NATIVE:
            interlock=device.get('control_settings',{}).get('native_current',{}).get('cld_interlock')
            if not interlock or not any(d['type']==CLD and d.get('connection')==interlock for d in relevant.values()):
                return dict(status='unverified',reason='Native bias-owner binding is missing; cannot verify all related leases')
    devices=relevant
    first=snapshot(devices)
    time.sleep(1.1)
    second=snapshot(devices)
    # A later independent card may have claimed control; never clear/stop it.
    active=_api(box,'jobs?active_only=true')
    blockers=[row['id'] for row in active.get('jobs',[]) if row.get('set_id')==box.meta['set_id']
              and row.get('laser',{}).get('id')==box._laser_id and not row.get('read_only',False)
              and row.get('status') not in TERMINAL]
    result=check_stop(first,second,terminal_s)
    if blockers or active.get('status')=='rejected':result['status']='unverified'
    result.update(laser_id=box._laser_id,job_id=job['id'],job_status=job['status'],terminal_unix_s=terminal_s,
                  observations=[first,second],control_slot_blockers=blockers,read_only=True,
                  note='Verified means fresh post-terminal PI-off, fixed DAC across distinct reads and owner lease release at observation time. Not proof of light output off or future stability. Unknown results never authorize retry, fault clearing, takeover or restart.')
    return result
