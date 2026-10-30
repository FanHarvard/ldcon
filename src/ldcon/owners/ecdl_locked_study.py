"""Study current versus passive dither and nearby-mode strength in a live lock.

Runs inside the existing stationary worker/owner lease, never a second hardware
process. Parameters use fractions of the approved current span, never volts.
"""
import math
from ldcon.core.contracts import DeviceError

DEFAULTS=dict(controller_card_id='steady-lock',span_fraction=.02,step_fraction=.0025,
              dwell_s=35.,timeout_s=1800.,stop_on_obvious_multimode=True)


def parameters(card,overrides):
    if not isinstance(card['parameters'],dict) or not isinstance(overrides,dict):raise ValueError('Expected study parameter mappings')
    if (set(card['parameters'])|set(overrides))-DEFAULTS.keys():raise ValueError('Unknown locked-study parameter')
    p={**DEFAULTS,**card['parameters'],**overrides}
    if not isinstance(p['controller_card_id'],str) or not p['controller_card_id']:raise ValueError('Stationary controller card ID is required')
    if type(p['stop_on_obvious_multimode']) is not bool:raise ValueError('Multimode stop flag must be boolean')
    for k in ('span_fraction','step_fraction','dwell_s','timeout_s'):
        if type(p[k]) not in (int,float) or not math.isfinite(p[k]) or p[k]<=0:raise ValueError('Invalid study '+k)
    if not 0<p['step_fraction']<=p['span_fraction']<=.25:raise ValueError('Study fractions must satisfy 0 < step <= span <= 0.25')
    if 4*math.ceil(p['span_fraction']/p['step_fraction'])>200:raise ValueError('Too many study points')
    if p['timeout_s']>86400:raise ValueError('Study timeout exceeds 24 hours')
    return p


def nearby_modes(metrics,profile):
    """Continuous device-defined competing-mode indicator plus raw components.

    This is a fringe proxy, not a calibrated longitudinal modal power fraction.
    """
    components={}
    for name,definition in profile['components'].items():
        raw=metrics.get(name);scale=definition['obvious_threshold']
        if type(raw) not in (int,float) or not math.isfinite(raw) or scale<=0:raise DeviceError('Nearby-mode indicator is unavailable')
        components[name]=dict(value=raw,normalized=raw/scale)
    if not components:raise DeviceError('Nearby-mode components are unset')
    strength=max(c['normalized'] for c in components.values())
    return dict(strength=strength,obvious_multimode=strength>=1.,components=components,
                definition_revision=profile['revision'],unit='relative fringe side-peak index')


class LockedCurrentStudy:
    def __init__(self,p,origin,policy,now,minimum_dwell):
        if p['dwell_s']<minimum_dwell:raise DeviceError('Study dwell is shorter than settling plus passive dither acquisition')
        span=(policy.high-policy.low)*p['span_fraction'];step=(policy.high-policy.low)*p['step_fraction']
        if policy.resolution and step<policy.resolution:raise DeviceError('Study current step is below device resolution')
        if not policy.low<=origin-span<origin+span<=policy.high:raise DeviceError('Study span exceeds owner-approved current bounds')
        # Full path is retained: origin -> lower -> upper -> lower -> origin.
        def ramp(a,b):
            count=max(1,math.ceil(abs(b-a)/step))
            return [a+(b-a)*i/count for i in range(1,count+1)]
        self.points=[origin]+ramp(origin,origin-span)+ramp(origin-span,origin+span)+ramp(origin+span,origin-span)+ramp(origin-span,origin)
        self.p=p;self.origin=origin;self.policy=policy;self.started=now;self.index=0;self.since=None
        self.returning=False;self.reason=None;self.status='running';self.direction='held';self.rows=[]
    def stop(self,reason):self.returning=True;self.reason=reason;self.since=None
    def tick(self,now,current):
        if now-self.started>self.p['timeout_s'] and not self.returning:self.stop('timeout')
        target=self.origin if self.returning else self.points[self.index]
        if self.policy.resolution:target=round(target/self.policy.resolution)*self.policy.resolution
        if abs(current-target)>max(self.policy.resolution/2,1e-10):
            self.since=None;self.direction='up' if target>current else 'down'
            return dict(action=dict(axis='current',value=target,reason='locked-current study return' if self.returning else 'locked-current study sweep'))
        if self.returning:
            self.status='cancelled' if self.reason=='cancelled' else 'failed' if self.reason=='timeout' else 'succeeded'
            return dict(done=True)
        if self.since is None:self.since=now
        if now-self.since<self.p['dwell_s']:return {}
        return dict(measure=True)
    def record(self,now,outputs,frequency,metrics,response,nearby):
        row=dict(point=self.index,current=outputs['current'],direction=self.direction,
                 temperature=outputs['temperature'],piezo=outputs['piezo'],frequency_hz=frequency,
                 wavelength_nm=299792458e9/frequency,fringe_metrics=metrics,dither_response=response,
                 nearby_modes=nearby,elapsed_s=now-self.started)
        self.rows.append(row);self.index+=1;self.since=None
        if nearby['obvious_multimode'] and self.p['stop_on_obvious_multimode']:self.stop('obvious multimode observed')
        elif self.index>=len(self.points):self.stop('completed')
        return row


class StudyInbox:
    """Same-worker online request bridge; no instrument client or extra lease."""
    def __init__(self,directory,deck,card_id,revision,emit):
        from pathlib import Path
        from ldcon.core.records import atomic_json
        self.directory=Path(directory);self.deck=deck;self.card_id=card_id;self.revision=revision;self.emit=emit;self.active=None
        atomic_json(self.directory/'controller-live.json',dict(controller_card_id=card_id,settings_revision=revision,config=dict(kind='ECDL stationary lock')))
    def point(self,row):
        if not self.active:raise DeviceError('Locked study observation has no matching request')
        self.emit('ecdl_locked_current_map_point',
            {**row,'request_id':self.active['request_id'],
             '_context':dict(card=self.active['card_id'],operation='study.ecdl_locked_current_map')},
            fields=dict(current=row['current'],temperature=row['temperature'],piezo=row['piezo'],
                frequency_hz=row['frequency_hz'],wavelength_nm=row['wavelength_nm'],
                dither_response=row['dither_response'],nearby_mode_strength=row['nearby_modes']['strength'],
                obvious_multimode=row['nearby_modes']['obvious_multimode']),
            tags=dict(direction=row['direction'],indicator_revision=row['nearby_modes']['definition_revision']))
    def poll(self,lock):
        from ldcon.core.records import atomic_json,read_json
        import time
        path=self.directory/'online-request.json'
        if path.exists():
            request=read_json(path);path.unlink()
            matching=next((c for c in self.deck['cards'] if c['id']==request.get('card_id') and c['operation']=='study.ecdl_locked_current_map'),None)
            try:
                if self.active or not matching or request.get('job_id')!=self.directory.name or request.get('settings_revision')!=self.revision or request.get('controller_card_id')!=self.card_id or time.time()>request.get('expires_unix_s',0):
                    raise ValueError('Study request is expired, busy, or does not match the active lock')
                p=parameters(matching,request['parameters'])
                if p['controller_card_id']!=self.card_id:raise ValueError('Wrong stationary controller')
                lock.study_start(p);self.active=request
                atomic_json(self.directory/'online-status.json',dict(request_id=request['request_id'],card_id=request['card_id'],status='running'))
                self.emit('ecdl_study_start',dict(request_id=request['request_id'],parameters=p))
            except Exception as error:
                atomic_json(self.directory/'online-status.json',dict(request_id=request.get('request_id'),status='rejected',reason=str(error)))
                if getattr(lock,'uncertain_output',False):raise
        if self.active:
            if read_json(self.directory/'online-cancel.json',{}).get('request_id')==self.active['request_id']:lock.study_stop()
            result=lock.study_status()
            if result['status'] in ('succeeded','failed','cancelled'):
                atomic_json(self.directory/'online-status.json',dict(request_id=self.active['request_id'],card_id=self.active['card_id'],**result))
                atomic_json(self.directory/('ecdl-current-study-'+self.active['request_id']+'.json'),result)
                self.emit('ecdl_study_end',dict(request_id=self.active['request_id'],**result));self.active=None
