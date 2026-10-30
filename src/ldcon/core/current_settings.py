"""Shared device settings and owner-enforced current activation conditions."""
import copy
from ldcon.core.device_conditions import ConditionError,Formula,finite,policy_digest

PID_KEYS={'kp_a_per_hz','ki_a_per_hz_s','kd_a_s_per_hz','derivative_filter_s','integral_limit_a'}

def settings_draft():
    return {'enabled':False,'output_update_interval_s':None,'activation_range_a':None,
            'pid':{'kp_a_per_hz':None,'ki_a_per_hz_s':None,'kd_a_s_per_hz':0.,
                   'derivative_filter_s':1.,'integral_limit_a':None},
            'response':{'step_a':None,'hold_s':10.,'samples':8,'min_snr':5.}}


def validate_settings(value,require_pid=True):
    if not isinstance(value,dict) or set(value)!=set(settings_draft()):raise ValueError('Explicit current control settings are required')
    if type(value['enabled']) is not bool:raise ValueError('Current enabled must be boolean')
    interval=finite(value['output_update_interval_s'],'current update interval')
    if not .1<=interval<=60:raise ValueError('Current update interval must be in [0.1, 60] s')
    bounds=value['activation_range_a']
    if not isinstance(bounds,list) or len(bounds)!=2:raise ValueError('Current activation offsets are unset')
    lo,hi=[finite(v,'activation offset') for v in bounds]
    if not lo<=0<=hi or lo>=hi:raise ValueError('Activation offsets must increase and contain zero')
    pid=value['pid']
    if not isinstance(pid,dict) or set(pid)!=PID_KEYS:raise ValueError('Invalid device PID settings')
    for key,v in pid.items():
        if v is None and not require_pid:continue
        number=finite(v,key)
        if key in ('derivative_filter_s','integral_limit_a') and number<0:raise ValueError('PID filter and integral bound must be nonnegative')
    if require_pid and pid['kp_a_per_hz']==pid['ki_a_per_hz_s']==pid['kd_a_s_per_hz']==0:raise ValueError('At least one PID gain must be nonzero')
    response=value['response']
    if not isinstance(response,dict) or set(response)!={'step_a','hold_s','samples','min_snr'}:raise ValueError('Invalid device response settings')
    if type(response['samples']) is not int or not 3<=response['samples']<=1000:raise ValueError('Response samples must be in [3,1000]')
    if finite(response['hold_s'])<=0:raise ValueError('Response hold must be positive')
    if finite(response['min_snr'])<=0:raise ValueError('Response SNR must be positive')
    if response['step_a'] is not None and finite(response['step_a'])<=0:raise ValueError('Response step must be positive')
    return value


class CurrentConditions:
    def __init__(self,policy,settings):
        keys={'bounds_a','max_slew_a_per_s','max_step_a','min_update_interval_s','expressions'}
        if not isinstance(policy,dict) or set(policy)!=keys:raise ValueError('Explicit current absolute conditions are required')
        bounds=policy['bounds_a']
        if not isinstance(bounds,list) or len(bounds)!=2:raise ValueError('Absolute current bounds are unset')
        self.low,self.high=[finite(v,'current bound') for v in bounds]
        if not 0<=self.low<self.high:raise ValueError('Current bounds must increase and be nonnegative')
        rate=policy['max_slew_a_per_s'];self.rate=Formula(rate) if isinstance(rate,str) else finite(rate,'current maximum slew')
        if not isinstance(self.rate,Formula) and self.rate<=0:raise ValueError('Current maximum slew must be positive')
        self.step=finite(policy['max_step_a']);self.minimum_interval=finite(policy['min_update_interval_s'])
        if self.step<=0 or self.minimum_interval<=0:raise ValueError('Current step and minimum interval must be positive')
        if not isinstance(policy['expressions'],list):raise ValueError('Current expressions must be a list')
        self.expressions=[Formula(x) for x in policy['expressions']]
        self.policy=copy.deepcopy(policy);self.settings=copy.deepcopy(settings)
        validate_settings(self.settings['current'],require_pid=False)
        if self.settings['current']['output_update_interval_s']<self.minimum_interval:raise ValueError('Device update interval is below absolute minimum')
        self.digest=policy_digest(policy);self.settings_digest=policy_digest(settings)

    def bounds(self,activation):
        origin=finite(activation['current_a'],'activation current')
        lo,hi=self.settings['current']['activation_range_a']
        return max(self.low,origin+lo),min(self.high,origin+hi)

    def check(self,state,candidate,elapsed_s,activation):
        candidate=finite(candidate);dt=finite(elapsed_s)
        previous=finite(state['current_setpoint_a']);measured=finite(state['current_a'])
        lo,hi=self.bounds(activation)
        if not self.low<=measured<=self.high:raise ConditionError('Measured current exceeds absolute bounds')
        if not lo<=previous<=hi or not lo<=candidate<=hi:raise ConditionError('Current exceeds absolute or activation-relative range')
        if dt+1e-9<max(self.minimum_interval,self.settings['current']['output_update_interval_s']):raise ConditionError('Current output update interval is too short')
        rate=abs(candidate-previous)/dt
        activation_values={**activation,'low_a':lo,'high_a':hi,
                           'min_offset_a':self.settings['current']['activation_range_a'][0],
                           'max_offset_a':self.settings['current']['activation_range_a'][1]}
        values={'state':state,'candidate':{'current_a':candidate},'activation':activation_values,
                'settings':self.settings,'dt_s':dt,'rate_a_per_s':rate}
        maximum=finite(self.rate.value(values),'maximum slew') if isinstance(self.rate,Formula) else self.rate
        if maximum<=0 or rate>maximum+1e-12 or abs(candidate-previous)>self.step+1e-12:raise ConditionError('Current step or slew limit exceeded')
        for expr in self.expressions:
            if expr.value(values) is not True:raise ConditionError('Current device condition is false: '+expr.text)
        return candidate


def current_state(readback):
    return {'current_setpoint_a':readback['current_a'],'current_a':readback['current_read_a'],
            'temperature_c':readback['temperature_read_c'],'setpoint_c':readback['temperature_c'],
            'tec_current_a':readback['tec_current_a'],'ld_enabled':readback['ld_enabled'],'tec_enabled':readback['tec_enabled']}
