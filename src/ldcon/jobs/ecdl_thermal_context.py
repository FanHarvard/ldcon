"""Explicit rough thermal-context study, separate from clean-mode acquisition.

Only typed bounded axes and optical observations are used. Multimode spectra
may be studied, but never certify an operating point. No native PI operations.
"""
import math
import time
from ldcon.core.contracts import DeviceError, MeasurementUnavailable
from ldcon.core.interface_cards import CardCancelled

DEFAULTS=dict(follow_target_frequency=False,target_frequency_hz=None,tolerance_hz=20e6,desired_mode='desired',
    temperature_end_fraction=None,temperature_step_fraction=.0025,
    map_temperature_fraction=.025,current_start_fraction=None,
    current_end_fraction=None,current_step_fraction=.0025,
    current_preferred_fraction=None,current_conditioning_fraction=None,capture_hz=20e9,measurement_band_hz=5e11,
    replay_tolerance_hz=100e6,timeout_s=10800.,measurement_timeout_s=120.,
    stable_s=30.,sample_interval_s=1.,max_maps=40,max_replay_attempts=3)


def parameters(card,overrides):
    p={**DEFAULTS,**card['parameters'],**overrides}
    if set(p)-set(DEFAULTS):raise ValueError('Unknown thermal-context study parameter')
    nullable={'target_frequency_hz','temperature_end_fraction','current_start_fraction','current_end_fraction','current_preferred_fraction','current_conditioning_fraction'}
    for k,v in p.items():
        if v is None and k in nullable:continue
        if k=='follow_target_frequency':
            if type(v) is not bool:raise ValueError('follow_target_frequency must be boolean')
        elif k=='desired_mode':
            if not isinstance(v,str) or not v:raise ValueError('Mode identity required')
        elif k in ('temperature_end_fraction','current_start_fraction','current_end_fraction','current_preferred_fraction','current_conditioning_fraction'):
            if type(v) not in (int,float) or not math.isfinite(v) or not 0<=v<=1:raise ValueError('Invalid axis fraction: '+k)
        elif type(v) not in (int,float) or not math.isfinite(v) or v<=0:raise ValueError('Invalid thermal study parameter: '+k)
    if p['current_start_fraction'] is not None and p['current_end_fraction'] is not None and not p['current_start_fraction']<p['current_end_fraction']:raise ValueError('Current start must precede end')
    if all(p[k] is not None for k in ('current_start_fraction','current_end_fraction','current_preferred_fraction')) and not p['current_start_fraction']<=p['current_preferred_fraction']<=p['current_end_fraction']:raise ValueError('Preferred current must lie in scan')
    if min(p['temperature_step_fraction'],p['current_step_fraction'],p['map_temperature_fraction'])<=0 or max(p['temperature_step_fraction'],p['current_step_fraction'],p['map_temperature_fraction'])>1:raise ValueError('Invalid study step')
    if p['current_start_fraction'] is not None and p['current_end_fraction'] is not None and math.ceil((p['current_end_fraction']-p['current_start_fraction'])/p['current_step_fraction'])>100:raise ValueError('Too many current map intervals')
    if type(p['max_replay_attempts']) is not int or not 1<=p['max_replay_attempts']<=5:raise ValueError('Invalid replay attempt budget')
    if type(p['max_maps']) is not int or not 1<=p['max_maps']<=100:raise ValueError('Invalid finite map budget')
    if p['timeout_s']>86400 or p['sample_interval_s']<.1:raise ValueError('Invalid study duration')
    return p


class ContextNotReproduced(DeviceError):
    pass


class FrequencyContextLost(MeasurementUnavailable):
    pass


def run_context_study(ports,p,*,clock,sleep,cancel,emit):
    if any(p[k] is None for k in ('target_frequency_hz','temperature_end_fraction','current_start_fraction','current_end_fraction','current_preferred_fraction')):raise ValueError('Thermal study target and axis fractions required')
    axes={k:ports[k] for k in ('piezo','temperature','current')}
    started=clock();last_write={k:started for k in axes};last_stamp=None;recovering=False;maps=0;writes=0
    expected={k:a.readback() for k,a in axes.items()}
    tp=axes['temperature'].policy;cp=axes['current'].policy
    end=tp.low+p['temperature_end_fraction']*(tp.high-tp.low)
    thermal_start=expected['temperature']
    if p['follow_target_frequency'] and (tp.frequency_hz_per_unit is None or not math.isfinite(tp.frequency_hz_per_unit) or tp.frequency_hz_per_unit==0):raise ValueError('Calibrated thermal frequency slope required')
    preferred=cp.low+p['current_preferred_fraction']*(cp.high-cp.low)
    def check():
        if cancel():raise CardCancelled('Thermal-context study cancelled; outputs held')
        if clock()-started>=p['timeout_s']:raise DeviceError('Thermal-context study deadline; outputs held')
        if not ports['loop'].is_open_loop():raise DeviceError('Thermal study requires held native feedback')
    def pause(duration):
        until=clock()+duration
        while clock()<until:
            check()
            remaining=until-clock()
            if remaining>0:sleep(min(.5,remaining))
    def read(stage,frequency=True,full=False,recover_after=None):
        nonlocal last_stamp
        begin=clock()
        while True:
            check()
            try:o=ports['observation'].read_mode()
            except (MeasurementUnavailable,OSError) as error:
                reason=str(error)
            else:
                # v5.1-hostcheck (human decision 2026-10-22): wavemeter quality/status/aligned labels are informational; the source clock is used for age only when trusted+aligned.
                sample=o.frequency;source_clock=sample.acquisition_known and sample.acquired_unix_s is not None
                age=time.time()-sample.acquired_unix_s if source_clock else clock()-sample.acquired_s
                stamp=sample.acquired_unix_s if sample.acquired_unix_s is not None else sample.acquired_s
                valid=math.isfinite(age) and 0<=age<=1.
                if last_stamp is not None and stamp<=last_stamp:valid=False
                f=sample.frequency_hz
                if frequency:valid=valid and type(f) in (int,float) and math.isfinite(f) and abs(f-p['target_frequency_hz'])<=p['measurement_band_hz']
                reason='Fresh optical observation required'
                if valid:
                    values={k:a.readback() for k,a in axes.items()}
                    for k,a in axes.items():
                        if abs(values[k]-expected[k])>max(a.policy.resolution,1e-8):raise DeviceError('Axis changed outside study: '+k)
                    check()
                    age=time.time()-sample.acquired_unix_s if sample.acquired_unix_s is not None else clock()-sample.acquired_s
                    if not math.isfinite(age) or not 0<=age<=1.:reason='Observation expired during readback'
                    else:
                        last_stamp=stamp
                        analysis=dict(o.fringe_analysis)
                        if not full:analysis={k:v for k,v in analysis.items() if k not in ('profile','phase','per_period')}
                        row=dict(stage=stage,outputs=values,frequency_hz=f,acquired_unix_s=sample.acquired_unix_s,quality=sample.quality,single_mode=o.single_mode,mode_id=o.mode_id,metrics=dict(o.metrics),fringe_analysis=analysis)
                        emit('ecdl_thermal_observation',row)
                        return o,row
            if recover_after is not None and clock()-begin>=recover_after:
                raise FrequencyContextLost('Fresh normal wavelength unavailable; hold TEC and remap current')
            if clock()-begin>=p['measurement_timeout_s']:raise DeviceError('Thermal study measurement deadline; outputs held')
            emit('measurement_rejected',dict(stage=stage,reason=reason));pause(p['sample_interval_s'])
    def move(name,target):
        nonlocal writes
        axis=axes[name];policy=axis.policy
        if not policy.low<=target<=policy.high:raise DeviceError('Study target outside owner envelope')
        if recovering and name!='current':raise DeviceError('TEC/PZT frozen during explicit current recovery')
        deferred_since=None
        for _ in range(10000):
            check();current=expected[name]
            if abs(current-target)<=max(policy.resolution/2,1e-10):return
            pause(max(0,last_write[name]+policy.update_interval_s-clock()))
            o,_=read('before '+name+' move',frequency=not recovering,recover_after=min(5.,p['measurement_timeout_s']) if name=='temperature' else None)
            if name=='temperature' and abs(o.frequency.frequency_hz-p['target_frequency_hz'])>p['capture_hz']:
                raise FrequencyContextLost('Target-near branch lost before TEC write; hold TEC and remap current')
            amount=min(policy.max_step,policy.max_slew_per_s*policy.update_interval_s)
            delta=max(-amount,min(amount,target-current))
            if policy.resolution:
                delta=math.copysign(math.floor((abs(delta)+1e-12)/policy.resolution)*policy.resolution,delta)
            value=current+delta
            if policy.resolution:value=round(value/policy.resolution)*policy.resolution
            if abs(value-current)<1e-12:raise DeviceError('Study step below axis resolution')
            check()
            try:axis.move(value,policy.update_interval_s)
            except MeasurementUnavailable as error:
                if getattr(axis,'uncertain_output',False):raise
                if deferred_since is None:deferred_since=clock()
                if clock()-deferred_since>=p['measurement_timeout_s']:raise DeviceError('Command deferral deadline; outputs held') from error
                emit('command_deferred',dict(interface=name,reason=str(error)));pause(p['sample_interval_s']);continue
            deferred_since=None
            expected[name]=value;last_write[name]=clock();writes+=1
            emit('command_applied',dict(interface=name,value=value,phase='thermal_context_diagnostic'))
        raise DeviceError('Study actuator iteration budget exhausted')
    def map_current_once():
        nonlocal recovering,maps
        maps+=1
        if maps>p['max_maps']:raise DeviceError('Thermal context map budget exhausted')
        emit('state',dict(state='rough_current_context',context=maps,held=dict(expected)))
        begin=getattr(axes['current'],'begin_recovery',None)
        if begin:begin();recovering=True
        lo=cp.low+p['current_start_fraction']*(cp.high-cp.low);hi=cp.low+p['current_end_fraction']*(cp.high-cp.low)
        count=max(1,math.ceil((p['current_end_fraction']-p['current_start_fraction'])/p['current_step_fraction']))
        path=[lo+(hi-lo)*i/count for i in range(count+1)];rows=[];map_points=[]
        def condition():
            if p.get('current_conditioning_fraction') is not None:
                conditioning=cp.low+p['current_conditioning_fraction']*(cp.high-cp.low)
                emit('ecdl_thermal_conditioning',dict(context=maps,current=conditioning,scan_start=lo,preparation_only=True))
                move('current',conditioning);pause(cp.settle_s)
        condition()
        for value in path:
            move('current',value);pause(cp.settle_s);o,row=read('rough current context',frequency=False,full=True)
            row.update(context=maps,current=expected['current'],direction='up',diagnostic_only=True);emit('ecdl_thermal_map_point',row)
            map_points.append(row)
            f=o.frequency.frequency_hz
            if type(f) in (int,float) and math.isfinite(f) and abs(f-p['target_frequency_hz'])<=p['capture_hz'] and o.fringe_analysis.get('period_qualified'):
                m=o.metrics
                if not all(type(m.get(k)) in (int,float) and math.isfinite(m[k]) for k in ('side_peak_prominence_max','side_peak_height_max','main_fwhm_fraction','near_main_peak_prominence_max')):continue
                score=m.get('side_peak_prominence_max',1.)+m.get('side_peak_height_max',1.)+5*max(0,m.get('main_fwhm_fraction',1.)-.2)+m.get('near_main_peak_prominence_max',1.)
                rows.append((score,abs(expected['current']-preferred),expected['current'],f))
        from ldcon.jobs.fringe_sweep_reference import analyze_sweep
        emit('ecdl_sweep_reference',analyze_sweep(map_points,discontinuity_hz=p['replay_tolerance_hz']))
        if not rows:raise DeviceError('No target-near branch found; TEC held, current map saved')
        chosen=min(rows);target=chosen[2]
        # Replay conditioning and discovery; direction alone is not reset.
        condition();move('current',path[0])
        for value in path:
            if value>target+1e-10:break
            move('current',value);pause(cp.settle_s)
        move('current',target);pause(cp.settle_s);o,row=read('context route replay verification',frequency=True,full=True,recover_after=min(5.,p['measurement_timeout_s']))
        if chosen and abs(o.frequency.frequency_hz-chosen[3])>p['replay_tolerance_hz']:
            shifted=dict(context=maps,expected_frequency_hz=chosen[3],actual=row)
            if p['follow_target_frequency'] and abs(o.frequency.frequency_hz-p['target_frequency_hz'])<=p['capture_hz']:
                # Rough TEC transport is intentionally changing frequency and
                # its thermal response continues during a current-map replay.
                # A stale fixed-frequency reference is not a hop certificate.
                # Keep the current valid near-target candidate, but never claim
                # exact longitudinal-mode identity or clean-mode qualification.
                emit('ecdl_thermal_transport_frequency_shift',dict(**shifted,original_mode_identity_verified=False,diagnostic_only=True))
            else:
                emit('ecdl_thermal_context_unreproduced',shifted)
                raise ContextNotReproduced('Selected thermal context branch did not reproduce; outputs held')
        if recovering:
            axes['current'].end_recovery();recovering=False
        emit('ecdl_thermal_context_selected',dict(context=maps,diagnostic_only=True,has_near_target_branch=bool(chosen),actual=row))
        return o
    def map_current():
        for attempt in range(1,p['max_replay_attempts']+1):
            try:return map_current_once()
            except (ContextNotReproduced,FrequencyContextLost) as error:
                emit('ecdl_thermal_replay_deferred',dict(attempt=attempt,budget=p['max_replay_attempts'],reason=str(error),held=dict(expected)))
                if attempt==p['max_replay_attempts']:raise
                # This is a new bounded search, never a repeated hardware write.
                # TEC/PZT remain frozen and the full discovery path is rescanned.
                pause(p['sample_interval_s']);read('fresh observation before route rediscovery',frequency=False)
    check();read('initial thermal context',frequency=False)
    map_current();last_map_temperature=expected['temperature'];stable=None;previous_stamp=None
    emit('state',dict(state='diagnostic_tec_walk',target_temperature=end,held=dict(expected),qualification='not an adopted single-mode point'))
    while True:
        try:o,row=read('diagnostic TEC study',frequency=True,recover_after=min(5.,p['measurement_timeout_s']))
        except FrequencyContextLost as error:
            emit('ecdl_thermal_wavelength_context_lost',dict(reason=str(error),held=dict(expected)))
            map_current();last_map_temperature=expected['temperature'];stable=None;previous_stamp=None
            continue
        f=o.frequency.frequency_hz;stamp=last_stamp
        if abs(f-p['target_frequency_hz'])>p['capture_hz']:
            emit('ecdl_thermal_branch_lost',dict(held=dict(expected),frequency_hz=f))
            map_current();last_map_temperature=expected['temperature'];stable=None;previous_stamp=None
            continue
        if p['follow_target_frequency'] and abs(f-p['target_frequency_hz'])<=p['tolerance_hz']:
            return dict(status='succeeded',target_qualified=False,qualification='Target frequency approached diagnostically; independent fresh mode/PI qualification required',outputs=dict(expected),maps=maps,output_writes=writes,final=row)
        clean=o.single_mode and not o.maintenance_required and abs(f-p['target_frequency_hz'])<=p['tolerance_hz']
        if clean:
            if previous_stamp is not None and stamp-previous_stamp>max(2*p['sample_interval_s'],2.):stable=None
            if stable is None:stable=stamp
            if stamp-stable>=p['stable_s']:
                return dict(status='succeeded',target_qualified=True,outputs=dict(expected),maps=maps,output_writes=writes,final=row)
            # Do not move any axis during final stability qualification.
            previous_stamp=stamp;pause(p['sample_interval_s']);continue
        else:stable=None
        previous_stamp=stamp
        if abs(expected['temperature']-end)<=max(tp.resolution/2,1e-10):
            map_current();o,row=read('final thermal context',frequency=True,full=True)
            return dict(status='succeeded',target_qualified=False,qualification='Diagnostic thermal study completed; operating point requires independent fresh acquisition',outputs=dict(expected),maps=maps,output_writes=writes,final=row)
        if abs(expected['temperature']-last_map_temperature)>=(tp.high-tp.low)*p['map_temperature_fraction']:
            map_current();last_map_temperature=expected['temperature'];stable=None
        step=min(tp.max_step,tp.max_slew_per_s*tp.update_interval_s,(tp.high-tp.low)*p['temperature_step_fraction'])
        desired=end
        if p['follow_target_frequency']:
            desired=expected['temperature']-(f-p['target_frequency_hz'])/tp.frequency_hz_per_unit
            desired=max(min(thermal_start,end),min(max(thermal_start,end),desired))
        target=expected['temperature']+max(-step,min(step,desired-expected['temperature']))
        if tp.resolution:target=round(target/tp.resolution)*tp.resolution
        try:move('temperature',target)
        except FrequencyContextLost as error:
            emit('ecdl_thermal_wavelength_context_lost',dict(reason=str(error),held=dict(expected)))
            map_current();last_map_temperature=expected['temperature'];stable=None;previous_stamp=None
            continue
        pause(tp.settle_s)
