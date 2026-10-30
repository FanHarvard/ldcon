"""Device-independent continuous PID and one-shot bounded response tuning."""
import copy
import math
import statistics
from ldcon.core.contracts import DeviceError
from ldcon.core.device_conditions import ConditionError,finite
from ldcon.core.interface_cards import ControlMeasurementGate,_check_cancel,_check_initial_detuning
from ldcon.core.current_settings import validate_settings

CURRENT_OPERATIONS={'servo.current_pid','identify.current_response'}
COMMON={'max_sample_age_s':1.,'measurement_recovery_timeout_s':15.,'measurement_recovery_samples':3,
        'max_initial_detuning_hz':1e12}
SERVO_DEFAULTS={**COMMON,'measurement_recovery_timeout_s':120.,'target_frequency_hz':None,'target_wavelength_nm':None,'tolerance_hz':1e7,'duration_s':None}
RESPONSE_DEFAULTS={**COMMON,'timeout_s':600.,'save_settings':True,'optimize_update_interval':True}


def card_parameters(card,overrides):
    defaults=SERVO_DEFAULTS if card['operation']=='servo.current_pid' else RESPONSE_DEFAULTS
    if not isinstance(overrides,dict) or not isinstance(card['parameters'],dict):raise ValueError('Parameters must be mappings')
    if (set(card['parameters'])|set(overrides))-set(defaults):raise ValueError('Current cards use device-side PID/interval, not card overrides')
    p={**defaults,**card['parameters'],**overrides}
    for key,value in p.items():
        if key in ('save_settings','optimize_update_interval'):
            if type(value) is not bool:raise ValueError(key+' must be boolean')
        elif value is None and key in ('target_frequency_hz','target_wavelength_nm','duration_s'):continue
        else:
            if finite(value,key)<=0:raise ValueError(key+' must be positive')
    if type(p['measurement_recovery_samples']) is not int or p['measurement_recovery_samples']<1:raise ValueError('Recovery samples must be a positive integer')
    if p.get('target_frequency_hz') is not None and p.get('target_wavelength_nm') is not None:raise ValueError('Specify frequency or wavelength, not both')
    if p.get('timeout_s',0)>86400:raise ValueError('Response timeout exceeds 24 hours')
    # Output timing belongs to the device; this placeholder is resolved before I/O.
    p['sample_interval_s']=.5
    return p


class CurrentSession:
    def __init__(self,wavelength,current,p,clock,sleep,cancel,emit,deadline):
        self.current=current;self.p=p;self.clock=clock;self.sleep=sleep;self.cancel=cancel;self.emit=emit
        self.settings=current.settings['current'];self.interval=self.settings['output_update_interval_s']
        self.gate=ControlMeasurementGate(wavelength,current,{**p,'sample_interval_s':self.interval},clock=clock,sleep=sleep,cancel=cancel,emit=emit,deadline=deadline)
        self.last_write=clock();self.last_command_started=self.last_write;self.next_read=clock()+self.interval;self.writes=0;self.last_acquisition=None
        self.activation=current.activate()['activation'];self.origin=self.activation['current_a'];self.deadline=deadline
        self.last_write=clock();self.next_read=self.last_write+self.interval
        emit('current_activation',{'activation':self.activation,'device_settings':current.settings,'conditions':current.current_conditions.policy})

    def wait(self):
        while self.clock()<min(self.next_read,self.deadline):
            _check_cancel(self.cancel)
            remaining=min(.2,self.next_read-self.clock(),self.deadline-self.clock())
            if remaining>0:self.sleep(remaining)
        _check_cancel(self.cancel)
        if self.clock()>=self.deadline:raise DeviceError('Current operation deadline exceeded; last output is held')

    def read(self,stage):
        while True:
            self.wait();sample,reading=self.gate.read(stage)
            self.next_read=self.clock()+self.interval
            if self.gate.recovered:self.emit('state',{'state':'current_servo' if stage=='current servo' else 'current_identification','recovered':True})
            acquired=sample.acquired_unix_s if sample.acquired_unix_s is not None else sample.acquired_s
            self.emit('trace',{'frequency_hz':sample.frequency_hz,'wavelength_nm':299792458e9/sample.frequency_hz,
                'sample_age_s':self.clock()-sample.acquired_s,'control_valid':True,'current_a':reading.current_a,
                'current_setpoint_a':reading.setpoint_a,'activation_current_a':self.origin,
                'measurement_recovered':self.gate.recovered})
            if self.last_acquisition is None or acquired>self.last_acquisition:
                self.last_acquisition=acquired;return sample,reading

    def candidate(self,reading,desired):
        guard=self.current.current_conditions;lo,hi=guard.bounds(self.activation)
        clipped=max(lo,min(hi,desired));delta=math.copysign(min(abs(clipped-reading.setpoint_a),guard.step),clipped-reading.setpoint_a)
        for _ in range(40):
            value=reading.setpoint_a+delta
            try:guard.check(reading.state(),value,self.clock()-self.last_write,self.activation);return value
            except ConditionError:
                delta*=.5
                if abs(delta)<1e-14:raise
        raise ConditionError('No admissible current candidate')

    def write(self,value,sample):
        if self.clock()-sample.acquired_s>self.p['max_sample_age_s']:return False
        _check_cancel(self.cancel)
        if self.clock()>=self.deadline:raise DeviceError('Current operation deadline exceeded')
        self.emit('command_requested',{'interface':'current','current_a':value})
        _check_cancel(self.cancel)
        self.last_command_started=self.clock()
        self.current.apply_current(value,self.clock()-self.last_write)
        self.last_write=self.clock();self.next_read=self.last_write+self.interval;self.writes+=1
        self.emit('command_applied',{'interface':'current','current_a':value});return True

    def move(self,target):
        lo,hi=self.current.current_conditions.bounds(self.activation)
        if not lo<=target<=hi:raise ConditionError('Response step exceeds absolute or activation-relative range')
        while True:
            sample,reading=self.read('current response identification')
            if abs(reading.setpoint_a-target)<1e-10:return
            candidate=self.candidate(reading,target)
            if abs(candidate-reading.setpoint_a)<1e-14:raise ConditionError('Response move cannot progress')
            self.write(candidate,sample)

    def collect(self):
        spec=self.settings['response'];end=self.clock()+spec['hold_s'];rows=[]
        while self.clock()<end or len(rows)<spec['samples']:
            sample,reading=self.read('current response identification')
            if self.gate.recovered:rows=[];end=self.clock()+spec['hold_s']
            rows.append((self.clock(),reading.setpoint_a,sample.frequency_hz))
        n=spec['samples'];values=[r[2] for r in rows[-n:]]
        return rows,statistics.mean(values),statistics.stdev(values)/math.sqrt(n)


def servo_current(wavelength,current,p,*,clock,sleep,cancel,emit,online=None):
    """Compatibility entry point specialized through the generic scalar loop."""
    from ldcon.jobs.slow_control import run_scalar_lock
    from ldcon.jobs.slow_current import FrequencyScalarSensor,CurrentSlowActuator
    validate_settings(current.settings['current'])
    target=p['target_frequency_hz'] or 299792458e9/p['target_wavelength_nm']
    actuator=CurrentSlowActuator(current)
    if callable(online):online=online(actuator)
    return run_scalar_lock(FrequencyScalarSensor(wavelength),actuator,
        {**p,'target':target,'tolerance':p['tolerance_hz'],'max_initial_detuning':p['max_initial_detuning_hz']},
        clock=clock,sleep=sleep,cancel=cancel,emit=emit,online=online)


def identify_current(wavelength,current,p,*,clock,sleep,cancel,emit):
    settings=validate_settings(current.settings['current'],require_pid=False)
    step=finite(settings['response']['step_a'],'response step')
    if step<=0:raise ValueError('Response step must be positive')
    session=CurrentSession(wavelength,current,p,clock,sleep,cancel,emit,clock()+p['timeout_s'])
    lo,hi=current.current_conditions.bounds(session.activation)
    destination=session.origin+step if session.origin+step<=hi else session.origin-step
    if not lo<=destination<=hi:raise ConditionError('No room for configured current stimulus')
    emit('state',{'state':'current_identification','step_current_a':destination})
    baseline,bf,bnoise=session.collect();session.move(destination)
    command_started=session.last_command_started
    excited,ef,enoise=session.collect();session.move(session.origin)
    returned,rf,rnoise=session.collect()
    delta=destination-session.origin;signal=ef-bf;noise=math.hypot(bnoise,enoise)
    if abs(signal)<max(1.,settings['response']['min_snr']*noise):raise DeviceError('Current response is unresolved above noise; settings not saved')
    if abs(rf-bf)>max(abs(signal)*.1,settings['response']['min_snr']*math.hypot(bnoise,rnoise),1.):raise DeviceError('Current return check failed; possible drift or mode change; settings not saved')
    gain=signal/delta
    # Low-bandwidth PI from the observed, sampled step response, not an optimum claim.
    spacing=statistics.median(b[0]-a[0] for a,b in zip(excited,excited[1:]))
    crossings=[r[0]-command_started for r in excited if (r[2]-bf)/signal>=.632]
    resolution=max(session.interval,spacing,excited[0][0]-command_started)
    tau=max(resolution,min(crossings) if crossings else settings['response']['hold_s'])
    resolved=bool(crossings) and (excited[0][2]-bf)/signal<.632
    interval=max(current.current_conditions.minimum_interval,min(settings['response']['hold_s']/settings['response']['samples'],60.,max(spacing,tau/5)))
    if not p['optimize_update_interval']:interval=session.interval
    kp=.2/gain
    width=max(session.origin-lo,hi-session.origin)
    proposal={'output_update_interval_s':interval,'pid':{'kp_a_per_hz':kp,'ki_a_per_hz_s':kp/max(5*tau,5*interval),
              'kd_a_s_per_hz':0.,'derivative_filter_s':interval,'integral_limit_a':max(0.,width/2)}}
    result={'status':'succeeded','output_writes':session.writes,'frequency_hz_per_a':gain,'time_constant_s':tau,'time_constant_resolved':resolved,'sampling_resolution_s':resolution,
            'baseline_hz':bf,'excited_hz':ef,'returned_hz':rf,'proposal':proposal,'settings_saved':False,
            'limitations':'Conservative local sampled PI estimate; requires servo validation, no optimality guarantee.'}
    emit('current_response_estimate',result)
    _check_cancel(cancel)
    if p['save_settings']:
        result['saved_settings']=current.save_tuning(copy.deepcopy(proposal));result['settings_saved']=True
        emit('device_settings_saved',result['saved_settings'])
    return result
