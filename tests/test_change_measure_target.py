import copy
import json
from types import SimpleNamespace

import pytest

from tests.measurement_fixtures import measurement_parameters, measurement_batch

from ldcon.core.contracts import DeviceError
from ldcon.jobs.change_measure import run
from ldcon.jobs.change_measure_target import TargetMeasurementAdapter


class Clock:
    def __init__(self):self.t=1000.
    def __call__(self):return self.t
    def sleep(self,t):self.t+=t


class History:
    def __init__(self,clock):self.clock=clock;self.error=False
    def read(self,start,end):
        # Synthetic 100 Hz source, deliberately much faster than host polling.
        samples=[dict(time_unix_s=i/100.,wavelength_nm=1000.) for i in range(int(start*100),int(end*100)+1)]
        return dict(samples=samples,integrity_errors=['bad'] if self.error else [],timestamp_basis='synthetic source')


class Context:
    def __init__(self):
        self.document=dict(id='laser',target=dict(frequency_hz=299792458e6,allowed_change_hz=[-1e9,1e9],max_change_hz=1e8))
        self.expected_revision='a';self.pending=False;self.superseded=False
    def check(self,**kwargs):
        if self.superseded:raise DeviceError('Superseded')


def setup(tmp_path):
    clock=Clock();history=History(clock);context=Context();calls=[]
    def target(directory,laser,card,p,**kwargs):
        calls.append(p)
        clock.sleep(.4)
        context.document['target']['frequency_hz']=p['target_frequency_hz']
        context.expected_revision='b'
        return dict(locked=True,status='succeeded',revision='b')
    adapter=TargetMeasurementAdapter(tmp_path,context.document,{'id':'response'},clock=clock,sleep=clock.sleep,
        cancel=lambda:False,emit=lambda *_:None,wall_clock=clock,api=object(),history=history,target_runner=target,context=context)
    return clock,history,context,calls,adapter


def test_full_source_history_covers_command_interval_not_just_polling(tmp_path):
    clock,history,context,calls,adapter=setup(tmp_path)
    result=run(adapter,measurement_parameters(changes=[dict(parameter='target_frequency_hz',offset=20e6)],baseline_s=.2,duration_s=.5,
        sample_interval_s=.1),clock=clock,sleep=clock.sleep,wall_clock=clock)
    assert result['status']=='succeeded' and len(calls)==1 and calls[0]['queue_steps'] is True
    assert len(result['samples'])>100 and sum(row['phase']=='transition' for row in result['samples'])>=39
    assert result['summary']['wavelength']['samples']>=49
    assert result['source_history']['timestamp_basis']=='synthetic source'


def test_no_source_history_rejects_before_target_write(tmp_path):
    clock,history,context,calls,adapter=setup(tmp_path);history.error=True
    with pytest.raises(DeviceError):run(adapter,measurement_parameters(changes=[dict(parameter='target_frequency_hz',offset=20e6)]),clock=clock,sleep=clock.sleep,wall_clock=clock)
    assert calls==[]


def test_foreground_supersession_never_restores_old_target(tmp_path):
    clock,history,context,calls,adapter=setup(tmp_path)
    plan=adapter.preflight([dict(parameter='target_frequency_hz',offset=20e6)],'ordered',{'wavelength':True})
    context.superseded=True
    with pytest.raises(DeviceError):adapter.apply(plan,lambda:None)
    assert calls==[]


def test_target_and_unrelated_axis_never_split_silently(tmp_path):
    clock,history,context,calls,adapter=setup(tmp_path)
    with pytest.raises(DeviceError):adapter.preflight([dict(parameter='target_frequency_hz',offset=20e6),dict(parameter='current',value=1.)],'ordered',{})
    assert not calls


def test_batch_observation_step_does_not_reuse_prior_target_interval(tmp_path):
    from ldcon.jobs.change_measure import run_batch
    clock,history,context,calls,adapter=setup(tmp_path)
    result=run_batch(adapter,measurement_batch([
        measurement_parameters(changes=[dict(parameter='target_frequency_hz',offset=20e6)],baseline_s=.2,duration_s=.5),
        measurement_parameters(changes=[],baseline_s=.2,duration_s=.5)]),clock=clock,sleep=clock.sleep,wall_clock=clock)
    first,second=result['steps']
    assert len(calls)==1 and second['application'] is None
    assert all(r['measurements']['wavelength']['acquired_unix_s']>=second['baseline_started_unix_s']
               for r in second['samples'])
    assert not any(r['phase']=='transition' for r in second['samples'])
    assert min(r['measurements']['wavelength']['acquired_unix_s'] for r in second['samples'])>=first['measurement_ended_unix_s']


def test_invalid_source_value_rejects_before_target_write(tmp_path):
    clock,history,context,calls,adapter=setup(tmp_path);original=history.read
    def invalid(start,end):
        result=original(start,end)
        for row in result['samples']:
            row.update(frequency_hz=299792458e6,wavelength_nm=float('nan'))
        return result
    history.read=invalid
    with pytest.raises(DeviceError,match='Recent source wavelength history'):
        run(adapter,measurement_parameters(changes=[dict(parameter='target_frequency_hz',offset=20e6)]),
            clock=clock,sleep=clock.sleep,wall_clock=clock)
    assert not calls


def test_corrupt_history_after_application_is_incomplete_and_strict_json(tmp_path):
    clock,history,context,calls,adapter=setup(tmp_path);original=history.read
    def invalid_after_write(start,end):
        result=original(start,end)
        if calls:
            result['samples'][-1].update(frequency_hz=299792458e6,wavelength_nm=float('nan'))
        return result
    history.read=invalid_after_write
    result=run(adapter,measurement_parameters(changes=[dict(parameter='target_frequency_hz',offset=20e6)],
        baseline_s=.2,duration_s=.5,history_wait_s=.1),clock=clock,sleep=clock.sleep,wall_clock=clock)
    assert len(calls)==1 and result['physical_outcome']=='applied'
    assert result['status']=='measurement_incomplete'
    assert result['source_history']['rejected_source_samples']==1
    assert 'nonfinite_source_value' in result['source_history']['integrity_errors']
    json.dumps(result,allow_nan=False)


def test_source_tail_requires_finite_increasing_timestamps():
    from ldcon.jobs.response_record import history_tail_complete
    for times in ([0.,.1,float('nan')],[0.,.1,.1,.2],[0.,.2,.1]):
        data=dict(samples=[dict(time_unix_s=stamp) for stamp in times])
        assert not history_tail_complete(data,.15,.2,.2)
