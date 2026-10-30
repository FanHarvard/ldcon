"""Device-level read-only voltage/physical-unit calibration, never a safety policy."""
import copy
import hashlib
import json
import math
from pathlib import Path
from ldcon.core.paths import checkout_root
import re
import tempfile
import os

DEFAULT_ROOT=checkout_root()/'data/web/device-metadata'

def number(v):return type(v) in (int,float) and math.isfinite(v)
def identifier(v):
    if not isinstance(v,str) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,100}',v):raise ValueError('Invalid device metadata identifier')
    return v

def defaults():
    return dict(current=dict(manufacturer='',model='',mode='CC',gain_a_per_v=None,reference_input_v=0.,source=''),temperature=dict(manufacturer='',model='',sensor_range_ohm=20000.,gain_ohm_per_v=None,reference_input_v=0.,resistance_at_reference_ohm=None,thermistor_model='',r_reference_ohm=None,t_reference_c=25.,beta_k=None,observed_resistance_ohm=None,source=''))

def validate(value):
    value=copy.deepcopy(value)
    if isinstance(value,dict) and isinstance(value.get('current'),dict):value['current'].pop('base_current_a',None)  # Legacy metadata, never used.
    if not isinstance(value,dict) or set(value)!=set(defaults()):raise ValueError('Expected current and temperature calibration')
    for axis,template in defaults().items():
        cfg=value[axis]
        if not isinstance(cfg,dict) or set(cfg)!=set(template):raise ValueError('Unexpected '+axis+' calibration fields')
        for key,default in template.items():
            v=cfg[key]
            if isinstance(default,str):
                if not isinstance(v,str) or len(v)>1000:raise ValueError('Expected short text: '+key)
            elif v is not None and not number(v):raise ValueError('Expected finite numeric value or null: '+key)
        if cfg['reference_input_v'] is None:raise ValueError('Reference voltage required')
    c=value['current'];t=value['temperature']
    if c['mode'] not in ('CC','CP','unknown'):raise ValueError('Unknown current controller mode')
    for cfg,keys in [(c,['gain_a_per_v']),(t,['sensor_range_ohm','gain_ohm_per_v','resistance_at_reference_ohm','r_reference_ohm','beta_k','observed_resistance_ohm'])]:
        for key in keys:
            if cfg[key] is not None and cfg[key]<=0:raise ValueError('Expected positive '+key)
    if t['t_reference_c'] is not None and t['t_reference_c']<=-273.15:raise ValueError('Invalid thermistor reference temperature')
    return copy.deepcopy(value)

def convert(outputs,calibration):
    cfg=validate(calibration);result={}
    v=outputs.get('current');c=cfg['current']
    if number(v):
        r=dict(model=c['model'],input_v=v,delta_a_valid=False)
        if c['mode']=='CC' and c['gain_a_per_v'] is not None:
            d=(v-c['reference_input_v'])*c['gain_a_per_v'];r.update(delta_a=d,delta_a_valid=True)
        result['current']=r
    v=outputs.get('temperature');t=cfg['temperature']
    if number(v):
        r=dict(model=t['model'],input_v=v,resistance_delta_ohm_valid=False,delta_c_valid=False)
        if t['gain_ohm_per_v'] is not None:
            dr=(v-t['reference_input_v'])*t['gain_ohm_per_v'];r.update(resistance_delta_ohm=dr,resistance_delta_ohm_valid=True)
            base=t['resistance_at_reference_ohm'];resistance=base+dr if base is not None else None
            if resistance is not None and resistance>0 and (t['sensor_range_ohm'] is None or resistance<=t['sensor_range_ohm']):
                if all(t[k] is not None for k in ('r_reference_ohm','t_reference_c','beta_k')):
                    def kelvin(ohm):
                        inverse=1/(t['t_reference_c']+273.15)+math.log(ohm/t['r_reference_ohm'])/t['beta_k']
                        return 1/inverse if inverse>0 else None
                    ref=kelvin(base);now=kelvin(resistance)
                    if ref is not None and now is not None:r.update(delta_c=now-ref,delta_c_valid=True)
            r['temperature_conversion_status']='calibrated_modulation_delta' if r['delta_c_valid'] else 'missing_or_out_of_range_thermistor_calibration'
        result['temperature']=r
    return result

class MetadataStore:
    def __init__(self,root=DEFAULT_ROOT):self.root=Path(root)
    def path(self,laser,device):return self.root/identifier(laser)/(identifier(device)+'.json')
    def get(self,laser,device):
        p=self.path(laser,device)
        if not p.exists():return dict(revision='',calibration=defaults())
        if p.stat().st_size>65536:raise ValueError('Calibration file too large')
        raw=p.read_bytes();return dict(revision=hashlib.sha256(raw).hexdigest(),calibration=validate(json.loads(raw)))
    def put(self,laser,device,value,expected_revision):
        import fcntl
        p=self.path(laser,device);p.parent.mkdir(parents=True,exist_ok=True)
        with (p.parent/(p.name+'.lock')).open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            if self.get(laser,device)['revision']!=expected_revision:raise ValueError('Calibration changed; reload before saving')
            value=validate(value);fd,name=tempfile.mkstemp(dir=p.parent,prefix='.calibration-')
            try:
                with os.fdopen(fd,'w') as f:json.dump(value,f,ensure_ascii=False,indent=2);f.write('\n');f.flush();os.fsync(f.fileno())
                raw=Path(name).read_bytes();revision=hashlib.sha256(raw).hexdigest()
                history=p.parent/'history'/identifier(device);history.mkdir(parents=True,exist_ok=True)
                (history/(revision+'.json')).write_bytes(raw)
                os.replace(name,p)
            finally:
                if os.path.exists(name):os.unlink(name)
        return self.get(laser,device)

def enrich(payload,laser,root=DEFAULT_ROOT):
    """Metadata failures affect reporting only, never control or output values."""
    if not laser:return payload
    outputs=payload.get('outputs')
    if not isinstance(outputs,dict):return payload
    result=dict(payload);physical={}
    try:
        folder=Path(root)/identifier(laser)
        for p in sorted(folder.glob('*.json'))[:32]:
            try:
                stored=MetadataStore(root).get(laser,p.stem);physical[p.stem]=dict(calibration_revision=stored['revision'],**convert(outputs,stored['calibration']))
            except (OSError,ValueError,TypeError,KeyError,OverflowError):physical[p.stem]=dict(calibration_valid=False)
    except (ValueError,OSError):return result
    if physical:result['physical']=physical
    return result
