"""Same-worker hot controls. Web only queues data; the existing lease executes."""
import math
import re
import time
from ldcon.core.contracts import MeasurementUnavailable
from ldcon.core.records import atomic_json,read_json

# Dither pause (2026-10-27): the trim's dither guard is skipped while steady_lock.dither.paused is true.
DITHER_GUARD_SKIPPED='dither paused (steady_lock.dither.paused): dither guard skipped, quality metrics guard only'

def trim_policy(profile):
    """Require device qualification; optional trim is unavailable without it.

    The fixed ceilings are this adapter's qualified hardware envelope, not
    defaults for another laser. Device settings may only narrow that envelope.
    """
    p=profile.get('hot_control')
    required=('max_trim_v','max_step_v','minimum_dwell_s','dither_max')
    if not isinstance(p,dict) or any(k not in p for k in required):
        raise ValueError('Explicit device hot_control trim policy is required')
    for key in required:
        if type(p[key]) not in (int,float) or not math.isfinite(p[key]) or p[key]<=0:
            raise ValueError('Invalid device trim '+key)
    if p['max_trim_v']>.003 or p['max_step_v']>.001 or p['minimum_dwell_s']<40.:
        raise ValueError('Trim exceeds qualified 3 mV total / 1 mV step / 40 s dwell envelope')
    return p

def command(value):
    if not isinstance(value,dict):raise ValueError('Hot control must be a mapping')
    action=value.get('action')
    allowed={'set_current_trim':{'action','delta_mv'},'pause_tec_ff':{'action'},
             'resume_tec_ff':{'action'},'set_strategy':{'action','strategy'}}
    if action not in allowed or set(value)!=allowed[action]:raise ValueError('Unknown hot control fields')
    if action=='set_current_trim':
        v=value['delta_mv']
        if type(v) not in (int,float) or not math.isfinite(v) or not 0<abs(v)<=3:raise ValueError('Trim must be nonzero within +/-3 mV')
    if action=='set_strategy' and value['strategy'] not in ('hold','center'):raise ValueError('Hidden current-window traversal is retired; use a separate window-center/adjustment card')
    return dict(value)

class HotControlInbox:
    def __init__(self,directory,controller,revision,emit,clock=time.monotonic,wall_clock=time.time):
        self.wall_clock=wall_clock
        self.directory=directory;self.controller=controller;self.revision=revision
        self.emit=emit;self.clock=clock;self.trim=None
    @property
    def busy(self):return self.trim is not None
    def publish(self,value):
        atomic_json(self.directory/'hot-control-status.json',value)
        if isinstance(value.get('request_id'),str) and re.fullmatch('[0-9a-f]{32}',value['request_id']):
            atomic_json(self.directory/('hot-control-result-'+value['request_id']+'.json'),value)
        self.emit('ecdl_hot_control',value)
    def poll(self,lock,busy=False):
        path=self.directory/'hot-control-request.json'
        if path.exists():
            r=read_json(path);path.unlink()  # No write/RPC retry after uncertain dispatch.
            try:
                if busy or self.busy:raise ValueError('Another controller operation is active')
                if (r.get('job_id')!=self.directory.name or r.get('controller_card_id')!=self.controller
                    or r.get('settings_revision')!=self.revision
                    or type(r.get('expires_unix_s')) not in (int,float) or not math.isfinite(r['expires_unix_s'])
                    or not 0<=r['expires_unix_s']-time.time()<=15):
                    raise ValueError('Expired or mismatched hot control')
                import re
                if not re.fullmatch('[0-9a-f]{32}',r.get('request_id','')):raise ValueError('Invalid hot control id')
                if (self.directory/('hot-control-result-'+r['request_id']+'.json')).exists():return
                c=command(r['command']);self.publish(dict(request_id=r['request_id'],status='dispatching'))
                if c['action']=='set_current_trim':
                    self.trim=LocalTrim(lock,c['delta_mv'],self.clock,self.wall_clock)
                    self.request=r
                else:
                    result=lock.hot_control(c)
                    self.publish(dict(request_id=r['request_id'],status='succeeded',result=result))
            except Exception as e:
                self.publish(dict(request_id=r.get('request_id'),status='rejected',reason=str(e)))
                if lock.uncertain_output:raise
        if self.trim:
            try:
                result=self.trim.poll()
                if result:
                    self.publish(dict(request_id=self.request['request_id'],**result));self.trim=None
            except MeasurementUnavailable as error:
                self.trim.evidence['last_hold_reason']=str(error)
                # No move on missing optical/dither data. Finite deadline still applies.
                if self.clock()>self.trim.deadline:
                    self.trim.close()
                    self.publish(dict(request_id=self.request['request_id'],status='failed',reason='Trim observation deadline; outputs held',evidence=self.trim.evidence))
                    self.trim=None
            except Exception as e:
                self.publish(dict(request_id=self.request['request_id'],status='failed',reason=str(e),evidence=self.trim.evidence))
                if not lock.uncertain_output:self.trim.close()
                self.trim=None
                if lock.uncertain_output:raise
    def interrupt(self):
        if self.trim:
            self.trim.close();self.trim=None
            self.publish(dict(request_id=self.request['request_id'],status='failed',reason='Trim interrupted by recovery/worker exit; no rollback write'))

class LocalTrim:
    """One selected offset, <=1mV per step, >=40s and fresh3 after each step."""
    def __init__(self,lock,delta_mv,clock,wall_clock=time.time):
        self.lock=lock;self.clock=clock;self.wall_clock=wall_clock;self.started=clock()
        profile=lock.session.device['control_settings']['steady_lock']
        self.trim_policy=trim_policy(profile)
        command(dict(action='set_current_trim',delta_mv=delta_mv))
        if abs(delta_mv)/1000.>self.trim_policy['max_trim_v']:
            raise ValueError('Requested trim exceeds the configured device envelope')
        policy=lock.recovery_ports()['current'].policy
        if policy.unit!='V':raise ValueError('mV trim requires a voltage current interface')
        self.fit_interval=profile['supervision_interval_s']
        d=profile['dither']
        self.step_limit=min(self.trim_policy['max_step_v'],policy.max_step)
        self.dwell=max(self.trim_policy['minimum_dwell_s'],policy.settle_s+max(d['window_s'],8.,3/d['frequency_hz']+1.)+d.get('delivery_delay_s',3.))
        steps=math.ceil(abs(delta_mv)/1000/self.step_limit)
        self.deadline=self.started+max(240.,(steps+1)*(self.dwell+2*self.fit_interval)+2*self.fit_interval+30)
        self.last_fit_source=None;self.step_completed_unix_s=None
        self.rebaseline=False;self.prewrite_samples=[];self.expected_current=None
        # Pause survives completion: caller explicitly resumes TEC/FF later.
        self.lock.hot_control(dict(action='pause_tec_ff'))
        self.origin=None;self.target=None;self.next_at=0.;self.samples=[];self.step_origin=None
        self.baseline=[];self.evidence=dict(baseline=None,steps=[]);self.guard_reason=None
        self.last_stamp=None
        self.delta=delta_mv/1000.;self.returning=False;self.control=False
    def close(self):
        if self.control:self.lock.response_end();self.control=False
    def _dither_guard(self,samples):
        """Mean/peak of the fitted responses against dither_max. With the dither paused (any sample without a fit)
        the guard is SKIPPED with the recorded reason; the quality-metric guard decides alone."""
        import statistics
        values=[x.get('dither_response') for x in samples]
        if any(not isinstance(v,(int,float)) or not math.isfinite(v) for v in values):
            return dict(trip=False,mean=None,peak=None,skipped=DITHER_GUARD_SKIPPED)
        mean=statistics.mean(values)
        return dict(trip=mean>=self.trim_policy['dither_max'],mean=mean,peak=max(values),skipped=None)
    def poll(self):
        if self.clock()>self.deadline:
            self.close();return dict(status='failed',reason='Finite trim deadline; outputs held',evidence=self.evidence)
        if self.clock()<self.next_at:return None
        info=self.lock.response_info();o=info['observation'];f=o['frequency'];m=o.get('metrics') or {}
        # v5.1-hostcheck (human decision 2026-10-22): wavemeter quality/status/aligned labels are informational.
        fv=f.get('frequency_hz')
        if type(fv) not in (int,float) or not math.isfinite(fv) or fv<=0:raise MeasurementUnavailable('Finite optical sample required')
        # response_info uses the owner freshness/native/identity guards too.
        stamp=f.get('acquired_unix_s',f['acquired_s'])
        if not isinstance(stamp,(int,float)) or not 0<=self.wall_clock()-stamp<=self.lock.session.device['control_settings'].get('max_sample_age_s',1.):raise MeasurementUnavailable('Trim observation is stale')
        if self.last_stamp is not None and stamp<=self.last_stamp:raise MeasurementUnavailable('Trim requires a new acquisition')
        self.last_stamp=stamp
        dither=self.lock.response_dither_info()
        ad=dither.get('dither_response');source=dither.get('dither_source_unix_s')
        source_start=dither.get('dither_source_start_unix_s')
        paused=dither.get('dither_analysis_status')=='paused' or dither.get('dither_paused') is True
        if paused:
            # Dither pause (2026-10-27): there is no modulation to fit, so the dither guard is skipped with a recorded
            # reason; the quality-metric guard still applies and the samples keep the supervision cadence, timed by
            # the optical acquisition instead of the fit window.
            self.evidence['dither_guard']=dict(skipped=True,reason=DITHER_GUARD_SKIPPED,
                                               dither_analysis_status=dither.get('dither_analysis_status'))
            ad=None;source=source_start=stamp
        elif (not dither.get('dither_response_valid') or type(source) not in (int,float)
                or not math.isfinite(source) or type(source_start) not in (int,float) or not math.isfinite(source_start)
                or source_start>source):
            raise MeasurementUnavailable('Qualified timestamped fit required')
        if self.last_fit_source is not None and source-self.last_fit_source<self.fit_interval:
            raise MeasurementUnavailable('Three new fits at supervision cadence required' if not paused
                                         else 'Three new samples at supervision cadence required (dither paused)')

        p=self.lock.session.device['control_settings']['steady_lock']
        target=info.get('target_frequency_hz');hz=f.get('frequency_hz')
        if (not info.get('native_pi_active') or type(target) not in (int,float) or type(hz) not in (int,float)
                or not math.isfinite(hz) or abs(hz-target)>p['capture_hz']):
            self.close();return dict(status='failed',reason='Native capture lost; no rollback write',evidence=self.evidence)
        limits=p['current_safety']['metrics_max']
        if not all(type(m.get(k)) in (int,float) and math.isfinite(m[k]) for k in limits):raise MeasurementUnavailable('Qualified phase-free metrics required')
        if not paused and (not isinstance(ad,(int,float)) or not math.isfinite(ad)):raise MeasurementUnavailable('Valid dither required')
        if self.step_completed_unix_s is not None and source_start<self.step_completed_unix_s+self.policy.settle_s:
            raise MeasurementUnavailable('Fit still contains data from before current settled' if not paused
                                         else 'Sample precedes the settled current (dither paused)')
        self.last_fit_source=source
        sample=dict(stamp=stamp,metrics=dict(m),dither_response=ad,dither=dict(dither),dither_paused=paused)
        if self.origin is None:
            context=(info['outputs']['current'],info['outputs']['temperature'],o['mode_id'])
            if self.baseline and self.baseline[-1]['context']!=context:self.baseline=[]
            self.baseline.append(dict(sample,context=context))
            self.baseline=self.baseline[-3:]
            self.evidence['baseline']=list(self.baseline)
            if len(self.baseline)<3:self.next_at=self.clock()+.5;return None
            import statistics
            initial={k:statistics.median(x['metrics'][k] for x in self.baseline) for k in limits}
            if sum(initial[k]>=v for k,v in limits.items())>1:raise MeasurementUnavailable('Several phase-free warning limits exceeded')
            if self._dither_guard(self.baseline)['trip']:raise MeasurementUnavailable('Trim dither guard')
            self.origin=dict(info['outputs']);self.mode=o['mode_id'];self.initial=initial
            self.evidence['baseline']=list(self.baseline)
            self.policy=self.lock.recovery_ports()['current'].policy
            if self.policy.unit!='V':raise ValueError('mV trim requires a voltage current interface')
            self.target=self.origin['current']+self.delta
            if not self.policy.low<=self.target<=self.policy.high:raise ValueError('Trim outside owner bounds')
            requirements=self.lock.response_begin();self.control=True
            minimum=requirements.get('minimum_dwell_s') if isinstance(requirements,dict) else None
            if type(minimum) not in (int,float) or not math.isfinite(minimum) or minimum<=0:
                raise ValueError('Owner minimum dwell is unavailable')
            self.dwell=max(self.dwell,minimum)
            self.expected_current=self.origin['current']
        if o['mode_id']!=self.mode or abs(info['outputs']['temperature']-self.origin['temperature'])>1e-9:
            self.close();return dict(status='failed',reason='Trim context changed; no rollback write',evidence=self.evidence)
        current=info['outputs']['current']
        if abs(current-self.expected_current)>max(self.policy.resolution/2,1e-10):
            self.close();return dict(status='failed',reason='Current context changed; no rollback write',evidence=self.evidence)
        if self.rebaseline:
            self.prewrite_samples.append(sample)
            self.prewrite_samples=self.prewrite_samples[-3:]
            if len(self.prewrite_samples)<3:self.next_at=self.clock()+self.fit_interval;return None
            import statistics
            self.initial={k:statistics.median(x['metrics'][k] for x in self.prewrite_samples) for k in limits}
            if (sum(self.initial[k]>=v for k,v in limits.items())>1
                    or self._dither_guard(self.prewrite_samples)['trip']):
                raise MeasurementUnavailable('Deferred move needs a new qualified three-fit baseline')
            self.evidence.setdefault('prewrite_requalification',[]).append(list(self.prewrite_samples))
            self.rebaseline=False
        if self.step_origin is not None:
            stamp=f.get('acquired_unix_s',f['acquired_s'])
            if self.samples and stamp<=self.samples[-1]['stamp']:return None
            self.samples.append(sample)
            if len(self.samples)<3:self.next_at=self.clock()+self.fit_interval;return None
            import statistics
            med={k:statistics.median(x['metrics'][k] for x in self.samples) for k in limits}
            guard=self._dither_guard(self.samples)
            peak_ad,mean_ad,dither_trip=guard['peak'],guard['mean'],guard['trip']
            worse_metrics=[k for k,v in limits.items() if med[k]>=v and med[k]>self.initial[k]]
            worsened=bool(worse_metrics)
            self.evidence['steps'].append(dict(current=current,step_origin=self.step_origin,
                phase='rollback' if self.returning else 'forward',before_metrics=dict(self.initial),
                after_metrics=med,samples=list(self.samples),dither_max=peak_ad,dither_mean=mean_ad,aggregation='mean_of_three_new_fits',
                quality_limits=dict(limits),dither_limit=self.trim_policy['dither_max'],
                quality_trip_metrics=worse_metrics,dither_trip=dither_trip,dither_guard_skipped=guard['skipped']))
            if not self.returning and (dither_trip or worsened):
                self.guard_reason=dict(quality_metrics=worse_metrics,dither=dither_trip)
                # Return only the last <=1mV step, never reverse across a detected hop.
                self.target=self.step_origin;self.returning=True
            elif abs(current-self.target)<=max(self.policy.resolution/2,1e-10):
                self.close()
                return dict(status='succeeded' if not self.returning else 'failed',outputs=info['outputs'],
                            reason='Local trim verified; TEC/FF remains paused' if not self.returning else 'Guarded step returned',
                            samples=self.samples,evidence=self.evidence,guard_reason=self.guard_reason)
            self.initial=med
            self.step_origin=None
        if abs(current-self.target)>max(self.policy.resolution/2,1e-10):
            amount=min(self.step_limit,self.policy.max_step)
            value=current+max(-amount,min(amount,self.target-current))
            try:result=self.lock.response_current(value)
            except MeasurementUnavailable:
                self.rebaseline=True;self.prewrite_samples=[];self.next_at=self.clock()+self.fit_interval
                raise
            if result.get('applied'):
                self.step_origin=current;self.samples=[];self.step_completed_unix_s=self.wall_clock();self.expected_current=result['value'];self.next_at=self.clock()+self.dwell
            else:self.rebaseline=True;self.prewrite_samples=[];self.next_at=self.clock()+self.fit_interval
        return None
