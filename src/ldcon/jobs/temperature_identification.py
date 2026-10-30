"""Bounded, per-invocation temperature tuning estimate with a return check."""
import math
import statistics
from ldcon.core.contracts import DeviceError
from ldcon.core.device_conditions import ConditionError
from ldcon.core.interface_cards import sample_trace,_check_cancel,_require_frequency,ControlMeasurementGate


def identify_temperature_slope(wavelength,temperature,p,*,clock,sleep,cancel,emit,deadline):
    guard=temperature.conditions;initial=temperature.read_temperature();origin=initial.setpoint_c
    guard.check(initial.state(),origin,p['sample_interval_s'])
    span=p['max_excursion_c'] if p['max_excursion_c'] is not None else guard.high-guard.low
    low=max(guard.low,origin-span);high=min(guard.high,origin+span)
    step=p['probe_step_c']
    if high-origin>=step:destination=origin+step
    elif origin-low>=step:destination=origin-step
    else:raise ConditionError('No room for the requested bounded temperature probe')
    last=None;writes=0;observations=[]
    gate=ControlMeasurementGate(wavelength,temperature,p,clock=clock,sleep=sleep,cancel=cancel,emit=emit,deadline=deadline)
    def read():
        nonlocal last
        while clock()<deadline:
            _check_cancel(cancel)
            sample,t=gate.read('temperature identification');now=clock()
            row=sample_trace(sample,now,p['max_sample_age_s'])
            _require_frequency(row,emit,'temperature identification')
            if not 0<=now-t.observed_s<=p['max_sample_age_s']:raise DeviceError('Stale temperature during identification')
            guard.check(t.state(),t.setpoint_c,p['sample_interval_s'])
            at=sample.acquired_unix_s if sample.acquired_unix_s is not None else sample.acquired_s
            row.update(temperature_c=t.temperature_c,temperature_setpoint_c=t.setpoint_c,identification=True)
            emit('trace',row)
            if last is None or at>last:
                last=at;observations.append([t.temperature_c,sample.frequency_hz]);return sample,t
            sleep(p['sample_interval_s'])
        raise DeviceError('Temperature identification deadline exceeded')
    def sample_settled(target):
        end=min(deadline,clock()+p['probe_settle_timeout_s']);samples=[]
        while clock()<end:
            sample,t=read()
            if gate.recovered:samples=[]
            if abs(t.temperature_c-target)<=p['settle_tolerance_c']:samples.append((t.temperature_c,sample.frequency_hz))
            else:samples=[]
            if len(samples)>=p['probe_samples']:
                values=samples[-p['probe_samples']:]
                return {'temperature_c':statistics.mean(v[0] for v in values),
                        'frequency_hz':statistics.mean(v[1] for v in values),
                        'standard_error_hz':statistics.stdev(v[1] for v in values)/math.sqrt(len(values)) if len(values)>1 else 0.}
            sleep(p['sample_interval_s'])
        raise DeviceError('Temperature probe did not settle; no target acquisition is attempted')
    def move(target):
        nonlocal writes
        while clock()<deadline:
            sample,t=read();remaining=target-t.setpoint_c
            if abs(remaining)<1e-9:return
            dt=p['sample_interval_s'];delta=math.copysign(min(abs(remaining),p['requested_slew_c_per_s']*dt,guard.step),remaining)
            for _ in range(40):
                candidate=t.setpoint_c+delta
                try:guard.check(t.state(),candidate,dt);break
                except ConditionError:
                    delta*=.5
                    if abs(delta)<1e-12:raise
            else:raise ConditionError('No admissible temperature probe step')
            _check_cancel(cancel)
            emit('command_requested',{'interface':'temperature','temperature_c':candidate,'duration_s':dt,'identification':True})
            _check_cancel(cancel)
            applied=temperature.ramp_temperature(candidate,dt);writes+=1
            emit('command_applied',{'interface':'temperature','temperature_c':applied.setpoint_c,'identification':True})
            sleep(dt)
        raise DeviceError('Temperature probe move deadline exceeded')
    emit('state',{'state':'identifying','probe_temperature_c':destination})
    baseline=sample_settled(origin);move(destination);excited=sample_settled(destination)
    # Always attempt the bounded return before accepting the estimate. No blind retry.
    move(origin);returned=sample_settled(origin)
    delta_t=excited['temperature_c']-baseline['temperature_c'];delta_f=excited['frequency_hz']-baseline['frequency_hz']
    uncertainty=math.hypot(baseline['standard_error_hz'],excited['standard_error_hz'])
    if abs(delta_t)<p['probe_min_temperature_delta_c']:raise DeviceError('Probe temperature separation is unresolved')
    if abs(delta_f)<max(p['probe_min_frequency_delta_hz'],p['probe_snr']*uncertainty):raise DeviceError('Temperature tuning signal is unresolved above measurement noise')
    return_noise=math.hypot(baseline['standard_error_hz'],returned['standard_error_hz'])
    allowed=max(p['probe_min_frequency_delta_hz'],p['probe_snr']*return_noise,abs(delta_f)*.1)
    if abs(returned['frequency_hz']-baseline['frequency_hz'])>allowed:raise DeviceError('Temperature round-trip return check failed; possible drift or mode change')
    coefficient=delta_f/delta_t
    if not math.isfinite(coefficient) or coefficient==0:raise DeviceError('Invalid identified temperature coefficient')
    result={'frequency_hz_per_c':coefficient,'baseline':baseline,'excited':excited,'returned':returned,
            'frequency_standard_error_hz':uncertainty,'output_writes':writes,'observations':len(observations),
            'scope':'this invocation only','method':'bounded-temperature-round-trip'}
    emit('temperature_identification',result);return result
