"""Idle-only policy reload from a protected maintainer approval artifact.

The RPC carries only identities/digests, never policy values or a file path.
Provision approval artifacts outside the laser-agent writable directories after
human approval of the exact diff. This module does not grant that approval.

TODO (rule M step 4, design Q3 open, 2026-10-27): the ``policy_reload`` action is a candidate for removal: layer-1
(physical) changes are a reviewed restart and layer-2 changes go through ``policy_update``. It is kept because the
human has not decided and because ``read``/``write``/``changes``/``check_journal``/``check_activation_range`` of this
module are shared by the owner, the live update and the startup checks. ``_reload`` refuses when the owner's
config.json carries no ``native_current`` copy any more (the trimmed layer-1 file).
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time

from ldcon.core.instrument_client import BrokerError,validate_endpoint
from urllib.request import Request,build_opener,ProxyHandler,HTTPRedirectHandler

ALLOWED=frozenset(('revision','calibration.current_a_per_v','calibration.evidence',
                  'native.kp_count_per_nm','native.ki_count_per_nm_s',
                  'native.integral_limit_count','native.evidence',
                  # 2026-10-21 human approval: activation-relative current window
                  # (laser-agent authority class "safety"); absolute current_bounds_a,
                  # DAC/ADC ranges and calibration remain maintainer-only.
                  'activation_range_a'))


def check_activation_range(policy):
    """Hard ceiling no approval can exceed: inside the absolute current window."""
    lo,hi=policy['activation_range_a'];b0,b1=policy['current_bounds_a']
    width=b1-b0
    if lo<-width or hi>width or not lo<=0<=hi:
        raise BrokerError('range','Activation range must contain zero and fit inside the absolute current bounds width')


def read(path):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size>2**20:
            raise ValueError('Bounded single-link policy file required')
        raw=stream.read(2**20+1)
    return json.loads(raw),hashlib.sha256(raw).hexdigest()


def write(path,value):
    path=Path(path);temp=path.with_name('.'+path.name+'.policy-reload.tmp')
    mode=stat.S_IMODE(path.stat(follow_symlinks=False).st_mode) if path.exists() else 0o600
    fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,mode)
    os.fchmod(fd,mode)
    with os.fdopen(fd,'w') as stream:
        json.dump(value,stream,allow_nan=False,indent=2);stream.flush();os.fsync(stream.fileno())
    os.replace(temp,path)
    fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)


def journal_path(state_file):return Path(str(state_file)+'.policy-reload.json')


def check_journal(state_file):
    path=journal_path(state_file)
    if os.path.lexists(path):
        value,_=read(path)
        if value.get('status')!='committed':raise ValueError('Incomplete policy reload; offline review required')


def changes(before,after,path=''):
    if isinstance(before,dict) and isinstance(after,dict):
        result=[]
        for key in set(before)|set(after):
            part=path+'.'+key if path else key
            if key not in before or key not in after:result.append(part)
            else:result.extend(changes(before[key],after[key],part))
        return result
    return [] if type(before) is type(after) and before==after else [path]


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):raise ValueError('Bias owner redirects are forbidden')


def bias_status(connection):
    endpoint=validate_endpoint(connection['endpoint'])
    with build_opener(ProxyHandler({}),NoRedirect()).open(Request(endpoint+'/status'),timeout=3) as response:
        raw=response.read(2**20+1)
    if len(raw)>2**20:raise ValueError('Bias response exceeds limit')
    value=json.loads(raw)
    if not isinstance(value,dict):raise ValueError('Invalid bias response')
    return value


def reload(owner,request):
    """Compatibility entry; physical writers do not read approval artifacts."""
    return owner.system_owner.policy_reload(request)


def _reload(owner,request):
    from ldcon.owners.esp32_native_owner import validate_policy,digest,envelope,current_for_code
    if set(request)!={'action','device_id','expected_definition','approved_definition','approval_id'}:
        raise BrokerError('request','Exact reload identities required; no policy or path accepted')
    config_file=getattr(owner,'policy_config_file',None)
    if config_file is None:raise BrokerError('action','Maintainer policy reload is not configured')
    # Caller holds the owner's io_lock, shared with poll and all writes.
    if (owner.fault or owner.active or owner.lease or owner.bias.lease or owner.pending
            or owner.prepared or owner.pid_tuning or owner.saved_pid is not None
            or getattr(owner,'ff_profile',None) is not None):
        raise BrokerError('busy','Clean, unleased, PI-off owner with no tuning/feedforward is required')
    if request['expected_definition']!=owner.definition:raise BrokerError('configuration','Current policy changed; do not replay')
    check_journal(owner.state_file)
    config_file=Path(config_file);approved_file=Path(str(config_file)+'.approved-policy.json')
    artifact,artifact_sha=read(approved_file)
    if (set(artifact)!={'schema_version','approval_id','expected_definition','approved_definition','policy','target_wavelength_nm','human_reference'}
            or artifact['schema_version']!=1 or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}',artifact['approval_id'])
            or not isinstance(artifact['human_reference'],str) or not artifact['human_reference'].strip()):
        raise BrokerError('configuration','Invalid protected approval artifact')
    if any(request[k]!=artifact[k] for k in ('approval_id','expected_definition','approved_definition')):
        raise BrokerError('configuration','Request differs from protected approval')
    policy=validate_policy(copy.deepcopy(artifact['policy']))
    if digest(policy)!=artifact['approved_definition']:raise BrokerError('configuration','Approved policy digest mismatch')
    diff=changes(owner.policy,policy)
    if not diff or set(diff)-ALLOWED:raise BrokerError('configuration','Reload only supports reviewed calibration/PI/activation-range fields and evidence')
    check_activation_range(policy)
    config,config_sha=read(config_file);state,state_sha=read(owner.state_file)
    if 'native_current' not in config:
        raise BrokerError('action','policy_reload is retired for a layer-1 owner configuration (no policy copy); use policy_update (design Q3)')
    if digest(config['native_current'])!=owner.definition or state['definition']!=owner.definition:
        raise BrokerError('configuration','Durable and running policy differ; offline review required')
    if state.get('fault') or state.get('lease') or state.get('bias_lease') or state.get('pending'):
        raise BrokerError('busy','Durable owner state is not clean and idle')
    # Independent owner cache: no bias lease is acquired for this check.
    bias=bias_status(policy['cld_interlock'])
    if (bias.get('instrument')!=policy['cld_interlock']['instrument'] or bias.get('resource')!=policy['cld_interlock']['resource']
            or bias.get('fresh') is not True or bias.get('fault') or bias.get('leases')!=[] or bias.get('bias_held') is not False):
        raise BrokerError('busy','Fresh matching bias owner with no leases required')
    before=owner._read_before()
    cfg=before['servo_config'];code=before['dac']['native_code']
    if cfg.get('pid_multiplier')!=0 or cfg.get('control_offset')!=code:
        raise BrokerError('busy','Actual PI-off and fixed DAC reference required')
    if any(cfg.get(k)!=v for k,v in policy['preserved'].items()):
        raise BrokerError('configuration','Actual preserved configuration differs, including polling interval')
    # Firmware, analog metadata, wiring and limits are unchanged by allowlist;
    # validate the entire new correction envelope at the approved target/code.
    proof=envelope(policy,artifact['target_wavelength_nm'],code)
    estimated=current_for_code(policy,code)
    if not policy['current_bounds_a'][0]<=estimated<=policy['current_bounds_a'][1]:
        raise BrokerError('range','Actual DAC is outside candidate absolute current bounds')
    after=owner._read_before()
    if after['servo_config']!=cfg or after['dac']['native_code']!=code:
        raise BrokerError('busy','Hardware changed during validation; no policy write')
    # Re-read all durable inputs before writing an intent. No output writes occur.
    if read(config_file)[1]!=config_sha or read(owner.state_file)[1]!=state_sha or read(approved_file)[1]!=artifact_sha:
        raise BrokerError('configuration','Approval or configuration changed during validation')
    intent=dict(status='intent',approval_id=artifact['approval_id'],expected_definition=owner.definition,
                approved_definition=digest(policy),config_before=config,state_before=state,
                approval_sha256=artifact_sha,started_unix_s=time.time(),hardware_writes=False)
    journal=journal_path(owner.state_file)
    try:
        write(journal,intent)
        new_config=copy.deepcopy(config);new_config['native_current']=policy
        new_state=copy.deepcopy(state);new_state['definition']=digest(policy)
        def commit(candidate, prepared):
            write(owner.state_file,new_state);write(config_file,new_config)
        receipt=dict(human_identity={'protected_artifact':artifact['approval_id'], 'human_reference':artifact['human_reference']},
                     receipt_id=artifact['approval_id'])
        owner.system_owner.adopt(policy, receipt=receipt, expected_revision=owner.definition,
                                 prepare=lambda candidate: {'reachable_proof':proof}, commit=commit)
        owner.expected_preserved=dict(policy['preserved'])
        owner.qualified=0;owner.last_seq=None;owner.last_seq_at=None;owner.last_optical=None
        owner.input_error='Await qualified input after approved policy reload'
        intent.update(status='committed',finished_unix_s=time.time(),proof=proof)
        write(journal,intent)
    except Exception as error:
        owner.pending={'kind':'policy_reload','approval_id':artifact['approval_id']}
        raise owner._latch('Policy reload persistence incomplete; offline review required') from error
    return dict(status='reloaded',approval_id=artifact['approval_id'],policy_digest=owner.definition,
                changed_paths=sorted(diff),hardware_writes=False,hardware_gains_applied=False,
                polling_interval_ms=cfg['polling_interval'],dac_code=code,proof=proof,
                note='Saved and running owner policy synchronized; hardware gains apply only on a later authorized prepare. No lock started.')
