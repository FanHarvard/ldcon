"""Specialization of neutral slow control for frequency Hz and LD current A."""
import copy
import math
from ldcon.jobs.slow_control import ScalarSample,ScalarReading
from ldcon.core.current_settings import CurrentConditions
from ldcon.core.device_conditions import ConditionError,finite


def neutral_pid(pid):
    return {'kp':pid['kp_a_per_hz'],'ki':pid['ki_a_per_hz_s'],'kd':pid['kd_a_s_per_hz'],
            'derivative_filter_s':pid['derivative_filter_s'],'integral_limit':pid['integral_limit_a']}


def current_pid(pid):
    return {'kp_a_per_hz':pid['kp'],'ki_a_per_hz_s':pid['ki'],'kd_a_s_per_hz':pid['kd'],
            'derivative_filter_s':pid['derivative_filter_s'],'integral_limit_a':pid['integral_limit']}


class FrequencyScalarSensor:
    def __init__(self,port):self.port=port
    def read(self):
        s=self.port.read_frequency()
        return ScalarSample(s.frequency_hz,s.unit,s.acquired_s,s.acquisition_known,s.quality=='valid')


class CurrentSlowActuator:
    def __init__(self,port):
        self.port=port;settings=port.settings;control=settings['current'];options=settings.get('slow_control',{})
        resolution=finite(options.get('output_resolution',0.))
        sensitivity=finite(options.get('sensitivity',0.))
        self.config={'interval_s':control['output_update_interval_s'],'pid':neutral_pid(control['pid']),
            'output_unit':'A','measurement_unit':'Hz','resolution':resolution,
            'sensitivity':sensitivity,'measurement_filter_s':options.get('measurement_filter_s',0.),
            'deadband':max(options.get('error_deadband',0.),.6*abs(sensitivity*resolution)),
            'hysteresis':options.get('quantization_hysteresis',.1)}
        if resolution<0:raise ValueError('Output resolution must be nonnegative')

    @property
    def controller(self):return self.port.controller
    @property
    def uncertain_output(self):return self.port.uncertain_output
    def close(self):self.port.close()

    def activate(self):return self.port.activate()['activation']['current_a']
    def read(self):
        r=self.port.read_control_state();return ScalarReading(r.setpoint_a,r.current_a,r.observed_s,'A',r)
    def validate(self,reading):self.port.validate_readback(reading.native)

    def project(self,reading,desired,elapsed_s):
        g=self.port.current_conditions;lo,hi=g.bounds(self.port.activation);previous=reading.setpoint
        target=max(lo,min(hi,desired));delta=math.copysign(min(abs(target-previous),g.step),target-previous)
        quantum=self.config['resolution']
        if quantum:
            # Choose a lattice point inside all limits. Never round a clipped bound outward.
            lower=max(lo,previous-g.step);upper=min(hi,previous+g.step)
            first=math.ceil((lower-1e-12)/quantum);last=math.floor((upper+1e-12)/quantum)
            indices=range(first,last+1)
            if last-first>10000:raise ConditionError('Current quantum search exceeds bounded candidate count')
            candidates=sorted((k*quantum for k in indices),key=lambda x:abs(x-target))
            candidates.append(previous)
        else:candidates=[previous+delta/(2**n) for n in range(40)]+[previous]
        for value in candidates:
            if abs(value-target)>abs(previous-target)+1e-12:continue
            try:g.check(reading.native.state(),value,elapsed_s,self.port.activation)
            except ConditionError:continue
            limited=abs(value-desired)>max(quantum*.50001,1e-12) if quantum else abs(value-desired)>1e-12
            return value,limited
        # A no-op still validates interlocks and absolute measured-state conditions.
        g.check(reading.native.state(),previous,elapsed_s,self.port.activation)
        return previous,True

    def apply(self,value,elapsed_s):
        r=self.port.apply_current(value,elapsed_s);return ScalarReading(r.setpoint_a,r.current_a,r.observed_s,'A',r)

    def save_pid(self,pid):
        proposal={'pid':current_pid(pid),'output_update_interval_s':self.config['interval_s']}
        self.port.uncertain_output=True
        result=self.port.save_tuning(proposal)
        self.port.uncertain_output=False
        self.port.settings=copy.deepcopy(result['settings'])
        self.port.current_conditions=CurrentConditions(self.port.current_conditions.policy,self.port.settings)
        self.config['pid']=copy.deepcopy(pid)
        return result

    def trace(self,sample,reading):
        return {'frequency_hz':sample.value,'wavelength_nm':299792458e9/sample.value,
                'current_a':reading.measured,'current_setpoint_a':reading.setpoint,
                'activation_current_a':self.port.activation['current_a']}
