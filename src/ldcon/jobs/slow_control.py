"""Universal sampled scalar PID; device units, limits and writes live in adapters."""
from dataclasses import dataclass
import copy
import math
from typing import Protocol
from ldcon.core.contracts import DeviceError,MeasurementUnavailable
from ldcon.core.device_conditions import finite
from ldcon.core.interface_cards import CardCancelled


@dataclass(frozen=True)
class ScalarSample:
    value:float
    unit:str
    acquired_s:float
    acquisition_known:bool=True
    valid:bool=True


@dataclass(frozen=True)
class ScalarReading:
    setpoint:float
    measured:float
    observed_s:float
    unit:str
    native:object=None


class ScalarSensor(Protocol):
    def read(self)->ScalarSample:...


class SlowActuator(Protocol):
    """One activated owner, one output, neutral PID units output/input."""
    config:dict
    def activate(self)->float:...
    def read(self)->ScalarReading:...
    def project(self,reading:ScalarReading,desired:float,elapsed_s:float)->tuple:...
    # MeasurementUnavailable means a proven pre-write rejection only.
    # Unknown write outcomes must raise another error and retain ownership.
    def apply(self,value:float,elapsed_s:float)->ScalarReading:...
    def save_pid(self,pid:dict)->dict:...
    def validate(self,reading:ScalarReading):...


def validate_pid(pid):
    keys={'kp','ki','kd','derivative_filter_s','integral_limit'}
    if not isinstance(pid,dict) or set(pid)!=keys:raise ValueError('Expected neutral scalar PID settings')
    for key,value in pid.items():finite(value,key)
    if pid['derivative_filter_s']<0 or pid['integral_limit']<0:raise ValueError('PID filter and integral limit must be nonnegative')
    if pid['kp']==pid['ki']==pid['kd']==0:raise ValueError('At least one PID gain must be nonzero')
    return copy.deepcopy(pid)


class ScalarPID:
    def __init__(self,pid,origin,*,filter_s=0.,deadband=0.,quantum=0.,hysteresis=.1):
        self.pid=validate_pid(pid);self.bias=finite(origin);self.integral=0.;self.derivative=0.
        self.filtered=None;self.previous_filtered=None;self.last_error=0.;self.last_output=origin
        self.filter_s=finite(filter_s);self.deadband=finite(deadband);self.quantum=finite(quantum);self.hysteresis=finite(hysteresis)
        if min(self.filter_s,self.deadband,self.quantum,self.hysteresis)<0:raise ValueError('Negative control setting')

    def step(self,value,target,dt,project,*,recovered=False):
        value=finite(value);target=finite(target);dt=finite(dt)
        if dt<=0:raise ValueError('PID dt must be positive')
        if self.filtered is None or recovered:self.filtered=value;self.derivative=0.
        else:self.filtered+=dt/(self.filter_s+dt)*(value-self.filtered)
        error=target-self.filtered
        if self.previous_filtered is not None and not recovered:
            raw=-(self.filtered-self.previous_filtered)/dt
            self.derivative+=dt/(self.pid['derivative_filter_s']+dt)*(raw-self.derivative)
        self.previous_filtered=self.filtered;self.last_error=error
        in_band=abs(error)<=self.deadband
        tentative=self.integral if in_band or recovered else self.integral+self.pid['ki']*error*dt
        tentative=max(-self.pid['integral_limit'],min(self.pid['integral_limit'],tentative))
        raw=self.bias+self.pid['kp']*error+tentative+self.pid['kd']*self.derivative
        desired=raw
        if in_band:desired=self.last_output
        elif self.quantum:
            if abs(raw-self.last_output)<=self.quantum*(.5+self.hysteresis):desired=self.last_output
            else:desired=round(raw/self.quantum)*self.quantum
        output,limited=project(desired)
        # Quantization alone is not saturation: retain integral authority to cross an LSB.
        if not limited or (desired-output)*(tentative-self.integral)<0:self.integral=tentative
        return output,{'error':target-value,'filtered_error':error,'integral':self.integral,
                       'derivative':self.derivative,'raw_output':raw,'candidate_output':output,
                       'limited':limited,'in_deadband':in_band}

    def applied(self,value):self.last_output=finite(value)

    def retune(self,pid):
        """Bumpless: preserve present output without changing activation reference."""
        new=validate_pid(pid);integral=max(-new['integral_limit'],min(new['integral_limit'],self.integral))
        self.bias=self.last_output-new['kp']*self.last_error-integral-new['kd']*self.derivative
        self.integral=integral;self.pid=new


def run_scalar_lock(sensor,actuator,p,*,clock,sleep,cancel,emit,online=None):
    config=actuator.config;interval=finite(config['interval_s']);started=clock()
    if interval<=0:raise ValueError('Scalar output interval must be positive')
    deadline=math.inf if p.get('duration_s') is None else started+p['duration_s']
    origin=actuator.activate();pid=ScalarPID(config['pid'],origin,filter_s=config.get('measurement_filter_s',0.),
        deadband=max(p.get('tolerance',0.),config.get('deadband',0.)),quantum=config.get('resolution',0.),hysteresis=config.get('hysteresis',.1))
    target=finite(p['target']);last_write=clock();previous_time=None;last_acquisition=None;next_sample=clock()+interval
    waiting=None;recovery_count=0;recovery_acquired=None;samples=0;writes=0
    emit('slow_activation',{'activation_output':origin,'output_unit':config['output_unit'],'measurement_unit':config['measurement_unit'],
                            'target':target,'device_config':config})
    emit('state',{'state':'slow_servo'})
    def reject(reason,sample,before):
        nonlocal waiting,recovery_count,recovery_acquired
        if waiting is None:waiting=before;emit('state',{'state':'waiting_measurement'})
        recovery_count=0;recovery_acquired=None
        emit('measurement_rejected',{'reason':reason,
            'sample_age_s':clock()-sample.acquired_s if sample and math.isfinite(sample.acquired_s) else None,
            'acquisition_known':sample.acquisition_known if sample else False,
            'sample_valid':sample.valid if sample else False})
        if online:online.invalidate('Measurement dropout',pid)
        if clock()-waiting>=p['measurement_recovery_timeout_s']:
            raise DeviceError('Scalar measurement recovery timed out; output held')
    try:
        while clock()<deadline:
            while clock()<min(next_sample,deadline):
                if cancel():raise CardCancelled('Slow PID stopped; last accepted output is held')
                remaining=min(.2,next_sample-clock(),deadline-clock())
                if remaining>0:sleep(remaining)
            if cancel():raise CardCancelled('Slow PID stopped; last accepted output is held')
            if clock()>=deadline:break
            before=clock();sample=None;reason=None
            try:sample=sensor.read()
            except (DeviceError,OSError) as error:reason=str(error)
            if sample is not None:
                age=clock()-sample.acquired_s
                if (not sample.valid or not sample.acquisition_known or type(sample.value) not in (int,float) or not math.isfinite(sample.value)
                    or sample.unit!=config['measurement_unit'] or not 0<=age<=p['max_sample_age_s']):reason='Invalid, stale or mismatched scalar measurement'
            if reason is None:
                try:reading=actuator.read()
                except (MeasurementUnavailable,OSError) as error:
                    if getattr(actuator,'uncertain_output',False):raise
                    reason=str(error)
                else:
                    actuator.validate(reading)
                    if reading.unit!=config['output_unit']:raise DeviceError('Scalar actuator unit mismatch')
                    if not 0<=clock()-reading.observed_s<=p['max_sample_age_s']:reason='Stale scalar actuator readback'
                    if clock()-sample.acquired_s>p['max_sample_age_s']:reason='Scalar measurement expired during actuator read'
            if reason is not None:
                reject(reason,sample,before)
            elif waiting is not None:
                if recovery_acquired is None or sample.acquired_s>recovery_acquired:recovery_count+=1
                else:recovery_count=0
                recovery_acquired=sample.acquired_s
                emit('measurement_recovery_sample',{'consecutive':recovery_count})
            if waiting is not None:
                if clock()-waiting>=p['measurement_recovery_timeout_s']:raise DeviceError('Scalar measurement recovery timed out; output held')
                if reason is not None or recovery_count<p['measurement_recovery_samples']:
                    next_sample=clock()+interval;continue
                waiting=None;previous_time=None;last_acquisition=None
                emit('state',{'state':'slow_servo','recovered':True})
            if sample.acquired_s==last_acquisition:
                next_sample=clock()+interval
                if online:online.invalidate('Repeated measurement',pid)
                continue
            if last_acquisition is not None and sample.acquired_s<last_acquisition:raise DeviceError('Scalar acquisition time moved backwards')
            last_acquisition=sample.acquired_s
            if abs(target-sample.value)>(p.get('max_initial_detuning') or math.inf):raise DeviceError('Scalar target is too far from the measured value; check units')
            if clock()>=deadline:break
            now=clock();dt=interval if previous_time is None else now-previous_time
            checkpoint=copy.deepcopy(pid.__dict__)
            output,details=pid.step(sample.value,target,dt,lambda desired:actuator.project(reading,desired,clock()-last_write),recovered=previous_time is None)
            previous_time=now;samples+=1
            # A deferred command consumes no integral or elapsed-time authority.
            write_accepted=False
            try:
                if abs(output-reading.setpoint)>1e-12:
                    if clock()-sample.acquired_s>p['max_sample_age_s']:
                        raise MeasurementUnavailable('Scalar measurement expired before output')
                    if cancel():raise CardCancelled('Slow PID stopped before output')
                    emit('command_requested',{'interface':'slow_actuator','output':output,'unit':reading.unit})
                    if cancel():raise CardCancelled('Slow PID stopped before output')
                    if clock()>=deadline:break
                    if clock()-sample.acquired_s>p['max_sample_age_s']:
                        raise MeasurementUnavailable('Scalar measurement expired before output')
                    applied=actuator.apply(output,clock()-last_write)
                    write_accepted=True
                    if applied.unit!=config['output_unit']:raise DeviceError('Acknowledged scalar output unit mismatch')
                    actuator.validate(applied);pid.applied(applied.setpoint);last_write=clock();writes+=1
                    emit('command_applied',{'interface':'slow_actuator','output':applied.setpoint,'unit':reading.unit})
                else:pid.applied(reading.setpoint)
            except MeasurementUnavailable as error:
                if write_accepted or getattr(actuator,'uncertain_output',False):raise
                pid.__dict__.clear();pid.__dict__.update(checkpoint)
                previous_time=None
                emit('command_deferred',{'interface':'slow_actuator','output':output,'reason':str(error)})
                reject(str(error),sample,before)
                next_sample=clock()+interval;continue
            requested=p.get('tolerance',0.)
            quantization_band=.5*abs(config.get('sensitivity',0.)*config.get('resolution',0.))
            row={'requested_tolerance':requested,'control_deadband':pid.deadband,
                 'within_requested_tolerance':abs(target-sample.value)<=requested,
                 'quantization_limited':quantization_band>requested and abs(target-sample.value)>requested,
                 'measurement':sample.value,'target':target,'output':reading.setpoint,'measured_output':reading.measured,
                 'measurement_unit':sample.unit,'output_unit':reading.unit,'sample_age_s':clock()-sample.acquired_s,
                 'control_valid':True,'activation_output':origin,**details}
            if hasattr(actuator,'trace'):row.update(actuator.trace(sample,reading))
            emit('trace',row);emit('pid',details)
            if online:online.tick(now,row,pid,actuator)
            next_sample=max(clock(),before+interval,last_write+interval)
    finally:
        if online:online.close(pid)
    return {'status':'succeeded','output_writes':writes,'samples':samples,'activation_output':origin,'target':target,'pid':pid.pid}
