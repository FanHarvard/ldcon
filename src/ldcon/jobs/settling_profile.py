"""Versioned empirical completion profiles. No physical settings are changed."""
import hashlib
import json
import math
import time
from pathlib import Path

from ldcon.core.records import atomic_json, read_json
from ldcon.jobs.dashboard_api import state_dir_of
from ldcon.core.contracts import DeviceError
from ldcon.core.interface_cards import CardCancelled


def path_for(directory, laser):
    return Path(state_dir_of(directory))/'settling-profiles'/laser['id']/'active.json'


def context(laser):
    """Bind actual controller parameters, not the translated target/FF offset."""
    from ldcon.agents.agent_owner_status import owner_read, NATIVE
    from ldcon.jobs.target_response_feedforward import configuration
    devices=[(name,d) for name,d in laser.get('devices',{}).items() if d['type']==NATIVE]
    if len(devices)!=1:raise ValueError('Empirical PI response currently requires a native current owner')
    name,device=devices[0];state=owner_read(device,'status')
    if (state.get('device_id')!=device['connection']['device_id'] or not state.get('fresh')
            or not state.get('read_ok') or state.get('input_valid') is not True or state.get('fault') or not state.get('native_pi_active')):
        raise ValueError('Fresh healthy native PI owner evidence required')
    if (state.get('background') or {}).get('epoch'):
        raise ValueError('An owner adjustment is still active; completion context is not stable')
    cfg=state['sample']['servo_config']
    keys=('pid_kp','pid_ki','pid_kd','pid_multiplier','pid_integral_min','pid_integral_max',
          'multiplier_control','multiplier_target','polling_interval')
    if any(type(cfg.get(k)) not in (int,float) or not math.isfinite(cfg[k]) for k in keys):
        raise ValueError('Complete native PI/interval provenance required')
    ff=configuration(laser)
    policy=state.get('policy_digest')
    if not policy:raise ValueError('Owner policy identity is missing')
    bias=state.get('bias') or {};sample=bias.get('sample') or {}
    if not bias.get('fresh') or bias.get('fault') or not bias.get('bias_held') or any(k not in sample for k in ('current_a','temperature_c')):
        raise ValueError('Fresh held bias/TEC context required')
    source=device['control_settings']['native_current']['optical']
    return dict(laser_id=laser['id'],owner_type=device['type'],device_id=name,policy_digest=policy,
        owner_pid=state.get('owner_pid'),ff_profile_digest=state.get('ff_profile_digest'),
        held_bias={k:sample[k] for k in ('current_a','temperature_c')},
        controller={k:cfg[k] for k in keys},feedforward=ff,
        optical_source_sha256=hashlib.sha256(json.dumps(source,sort_keys=True).encode()).hexdigest(),
        scope='same native current lock, gains, policy, FF and measured target range; no TEC/bias changes')


def install(directory,laser,profile,current_context):
    from ldcon.jobs.learned_settling import matches_context
    if profile.get('status')!='validated' or not matches_context(profile,current_context):
        raise ValueError('Only independently validated matching profiles can be installed')
    path=path_for(directory,laser);path.parent.mkdir(parents=True,exist_ok=True)
    raw=json.dumps(profile,sort_keys=True,allow_nan=False).encode()
    revision=hashlib.sha256(raw).hexdigest()
    record=dict(schema='ldcon.active-settling/1',revision=revision,profile=profile,
        source_job=Path(directory).name,installed_unix_s=time.time(),
        consumers=['tune.lock_target','tune.operating_point'],physical_settings_changed=False)
    atomic_json(path.parent/(revision+'.json'),record)
    atomic_json(path,record)
    return dict(revision=revision,path=str(path),source_job=Path(directory).name)


class Completion:
    """Fresh full-rate observation; no hardware commands and no target restoration."""
    def __init__(self,directory,laser,target_hz,applied_unix_s,*,origin_hz=None,history=None,record=None,context_reader=context):
        from ldcon.jobs.learned_settling import matches_context
        from ldcon.jobs.wavelength_history import configured_reader
        self.record=record if record is not None else read_json(path_for(directory,laser),{})
        self.directory,self.laser,self.target=Path(directory),laser,target_hz
        self.applied=applied_unix_s;self.last_poll=0.;self.last_context=0.
        self.reader=history;self.context_reader=context_reader
        self.result=dict(status='not_settled',reason='awaiting_fresh_profile_observations')
        if self.record:
            self.reader=history or configured_reader(state_dir_of(directory))
            validity=self.record['profile']['validity']
            if origin_hz is not None and (not validity['target_range_hz'][0]<=origin_hz<=validity['target_range_hz'][1]
                    or abs(target_hz-origin_hz)>validity['maximum_target_step_hz']*(1+1e-12)):
                raise DeviceError('Target move exceeds the measured profile scope; target applied, convergence unverified')

    @property
    def enabled(self):return bool(self.record)

    def poll(self,now):
        from ldcon.jobs.learned_settling import evaluate,matches_context
        if not self.enabled:return None
        if now-self.last_poll<.25:return self.result
        self.last_poll=now
        if now-self.last_context>=1.:
            try:current=self.context_reader(self.laser)
            except (OSError,ValueError):
                self.result=dict(status='inconclusive',reason='fresh_controller_context_unavailable')
                return self.result
            if not matches_context(self.record['profile'],current):
                raise DeviceError('Controller context changed during learned completion; target remains applied')
            self.last_context=now
        try:data=self.reader.read(self.applied,now)
        except (OSError,ValueError):
            self.result=dict(status='inconclusive',reason='full_rate_history_unavailable')
        else:
            if data.get('provenance')!=self.record['profile'].get('provenance'):
                self.result=dict(status='inconclusive',reason='optical_history_source_changed')
            else:self.result=evaluate(self.record['profile'],data,self.target,self.applied,now)
        if self.result['status']=='settled':
            # Recheck after the history query, immediately before publishing a
            # completion. The periodic check alone can miss a runtime PI edit.
            try:current=self.context_reader(self.laser)
            except (OSError,ValueError):
                self.result=dict(status='inconclusive',reason='fresh_controller_context_unavailable')
            else:
                if not matches_context(self.record['profile'],current):
                    raise DeviceError('Controller changed before completion confirmation; applied point retained')
        self.result.update(profile_revision=self.record['revision'],
            completion_basis='independently_validated_response_profile',optical_quality_used_for_completion=False)
        atomic_json(self.directory/'learned-completion.json',self.result)
        return self.result


def verify_move(directory,laser,target_hz,applied_unix_s,*,clock,sleep,cancel,timeout_s=90.,api=None):
    completion=Completion(directory,laser,target_hz,applied_unix_s)
    if not completion.enabled:return None
    from ldcon.jobs.dashboard_api import Dashboard
    from ldcon.jobs.lock_target import resolve_target
    api=api or Dashboard.from_job_directory(directory)
    snapshot=api.get_configuration('lasers',laser['id'])
    if resolve_target(snapshot['document'],{})!=target_hz:
        raise CardCancelled('New target superseded operating-point completion; applied point retained')
    deadline=clock()+timeout_s
    while clock()<deadline:
        if cancel():raise CardCancelled('Completion observation cancelled; the applied point is retained')
        if api.get_configuration('lasers',laser['id'])['revision']!=snapshot['revision']:
            raise CardCancelled('New configuration superseded operating-point completion; applied point retained')
        result=completion.poll(time.time())
        if result['status']=='settled':
            if api.get_configuration('lasers',laser['id'])['revision']!=snapshot['revision']:
                raise CardCancelled('New configuration superseded completion after measurement; applied point retained')
            return dict(result,ok=True)
        sleep(.25)
    raise DeviceError('Moved point has not met its learned completion profile; no automatic return')
