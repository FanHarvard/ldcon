import copy
from types import SimpleNamespace

import pytest

from tests.measurement_fixtures import measurement_parameters, measurement_batch

from ldcon.core.contracts import DeviceError
from ldcon.core.interface_cards import CardCancelled
from ldcon.jobs.change_measure import ChangeMeasure, parameters, run_batch, batch_parameters


class Clock:
    def __init__(self):self.t=0.
    def __call__(self):return self.t
    def sleep(self,t):self.t+=t
    def wall(self):return 1000+self.t


class Owner:
    def __init__(self,clock):self.clock=clock;self.value=0.;self.writes=[];self.reads=0;self.failure=None;self.stale=False
    def check(self):pass
    def preflight(self,changes,mode,measurements,planned=None):
        if mode!='ordered':raise DeviceError('Unavailable transaction mode')
        state=dict(planned or {'current':self.value});resolved=[]
        for c in changes:
            if c['parameter']!='current':raise DeviceError('Unknown parameter')
            value=c.get('value',state['current']+c.get('offset',0.))
            if not 0<=value<=10:raise DeviceError('Range')
            resolved.append(dict(parameter='current',value=value));state['current']=value
        return dict(changes=resolved,planned=state)
    def apply(self,plan,sample):
        for c in plan['changes']:
            self.value=c['value'];self.writes.append(self.value)
            if self.failure:raise self.failure
            sample()
        return dict(status='applied',readback={'current':self.value})
    def read(self,measurements):
        self.reads+=1
        stamp=1000 if self.stale else self.clock.wall()
        return {name:dict(valid=True,acquired_unix_s=stamp,source_start_unix_s=stamp-.2,
                         values={'frequency_hz':100.+self.value} if name=='wavelength' else {'response':self.value})
                for name,on in measurements.items() if on}


def setup(**overrides):
    c=Clock();o=Owner(c)
    p=parameters({'parameters':measurement_parameters(**{'duration_s':1.,'sample_interval_s':.1,'minimum_samples':3,**overrides})}, {})
    r=ChangeMeasure(o,p,clock=c,sleep=c.sleep,wall_clock=c.wall)
    return c,o,r


def test_change_measure_captures_baseline_and_separates_noise_from_offset():
    c,o,r=setup(changes=[{'parameter':'current','offset':2.}],baseline_s=.3,wait_s=.2,target_frequency_hz=100.)
    result=r.run()
    assert result['status']=='succeeded' and o.writes==[2.]
    assert {r['phase'] for r in result['samples']}=={'baseline','transition','wait','measurement'}
    stats=result['summary']['wavelength']['statistics']['frequency_hz']
    assert stats['mean_offset_hz']==2. and stats['stddev']==0. and stats['rms_from_target_hz']==2.
    assert result['application']['readback']['current']==2.


def test_measure_only_makes_no_write_and_change_only_needs_no_measurements():
    c,o,r=setup();assert r.run()['status']=='succeeded' and not o.writes
    c,o,r=setup(changes=[{'parameter':'current','value':2.}],measurements={},duration_s=0.)
    assert r.run()['status']=='succeeded' and o.reads==0 and o.writes==[2.]


def test_duplicate_source_is_not_fresh_measurement():
    c,o,r=setup();o.stale=True
    assert r.run()['status']=='measurement_incomplete'


def test_dither_requires_fully_post_change_window():
    c,o,r=setup(measurements={'dither':True})
    result=r.run();rows=[v for v in result['samples'] if v['phase']=='measurement']
    assert rows and all(v['measurements']['dither']['source_start_unix_s']>1000 for v in rows)


def test_unsupported_transaction_rejected_before_outputs():
    c,o,r=setup(apply_mode='atomic',changes=[{'parameter':'current','value':1.}])
    with pytest.raises(DeviceError):r.run()
    assert not o.writes and r.result['physical_outcome']=='not_applied'


def test_unknown_write_never_replayed_or_restored():
    c,o,r=setup(changes=[{'parameter':'current','value':2.}]);o.failure=OSError('lost receipt')
    with pytest.raises(OSError):r.run()
    assert o.writes==[2.] and r.result['physical_outcome']=='unknown'


def test_cancel_before_change_does_not_apply_or_restore():
    c,o,r=setup(changes=[{'parameter':'current','value':2.}],baseline_s=.5)
    r.cancel=lambda:c.t>=.2
    with pytest.raises(CardCancelled):r.run()
    assert not o.writes and r.result['status']=='cancelled'


def test_batch_preflights_every_point_before_writing():
    c=Clock();o=Owner(c)
    steps=[measurement_parameters(changes=[{'parameter':'current','value':v}],measurements={},duration_s=0.) for v in (2.,12.)]
    with pytest.raises(DeviceError):run_batch(o,measurement_batch(steps),clock=c,sleep=c.sleep,wall_clock=c.wall)
    assert o.writes==[]


def test_batch_relative_changes_keep_direction_and_no_hidden_restore():
    c=Clock();o=Owner(c)
    steps=[measurement_parameters(changes=[{'parameter':'current','offset':1.}],measurements={},duration_s=0.)]*3
    result=run_batch(o,measurement_batch(steps),clock=c,sleep=c.sleep,wall_clock=c.wall)
    assert result['status']=='succeeded' and o.writes==[1.,2.,3.]


def test_batch_stops_on_measurement_shortfall_and_keeps_partial_result():
    c=Clock();o=Owner(c);o.stale=True
    steps=[measurement_parameters(changes=[{'parameter':'current','value':v}],duration_s=.2) for v in (1.,2.)]
    result=run_batch(o,measurement_batch(steps),clock=c,sleep=c.sleep,wall_clock=c.wall)
    assert result['status']=='measurement_incomplete' and len(result['steps'])==1 and o.writes==[1.]


@pytest.mark.parametrize('changes',[[{'parameter':'p','value':1.,'offset':2.}],
    [{'parameter':'p','value':float('nan')}],[{'parameter':'p','value':1.},{'parameter':'p','value':2.}]])
def test_invalid_change_shape(changes):
    with pytest.raises(ValueError):parameters({'parameters':measurement_parameters(changes=changes)}, {})


def test_acquisition_requires_coverage_to_end_not_only_early_samples():
    c,o,r=setup(duration_s=5.,max_sample_gap_s=.5)
    original=o.read
    def read(flags):
        if c.t>.4:o.stale=True
        return original(flags)
    o.read=read
    assert r.run()['status']=='measurement_incomplete'


def test_empty_batch_is_draft_only():
    assert batch_parameters({'parameters':measurement_batch([])},{})['steps']==[]
    c=Clock();o=Owner(c)
    with pytest.raises(ValueError):run_batch(o,measurement_batch([]),clock=c,sleep=c.sleep)
    assert not o.writes


def test_finish_called_once_with_unknown_write_and_no_implicit_retry():
    c,o,r=setup(changes=[{'parameter':'current','value':2.}]);o.failure=OSError('lost acknowledgement')
    finished=[];o.finish=lambda result:finished.append(copy.deepcopy(result))
    with pytest.raises(OSError):r.run()
    assert len(finished)==1 and finished[0]['physical_outcome']=='unknown' and o.writes==[2.]


def test_numeric_samples_without_any_finite_measurement_are_incomplete():
    c,o,r=setup()
    original=o.read
    def read(flags):
        data=original(flags)
        for block in data.values():block['values']={'bad':float('nan')}
        return data
    o.read=read
    result=r.run()
    assert result['status']=='measurement_incomplete'
    import json
    json.dumps(result,allow_nan=False)
    assert result['rejected_samples']['wavelength']['nonfinite_or_missing_values']>0


def test_source_history_completion_must_obey_same_deadline():
    c,o,r=setup(timeout_s=1.2)
    def complete(result,*,check,remaining_s):
        assert remaining_s<=.2+1e-9
        c.sleep(.3);check()
    o.complete_measurement=complete
    with pytest.raises(DeviceError,match='deadline'):r.run()
    assert r.result['elapsed_s']>=1.2 and r.result['status']=='failed'


def test_batch_preflight_failure_keeps_durable_partial_contract():
    c=Clock();o=Owner(c);events=[]
    with pytest.raises(DeviceError) as failure:
        run_batch(o,measurement_batch([measurement_parameters(changes=[dict(parameter='current',value=20)])]),clock=c,
                  sleep=c.sleep,wall_clock=c.wall,emit=lambda k,v:events.append((k,v)))
    assert failure.value.measurement_result['physical_outcome']=='not_applied'
    assert events[-1][0]=='change_measure_batch_result' and not o.writes


def test_normalization_only_fills_disabled_switches_and_absent_statistic_target():
    p = measurement_parameters(measurements={'wavelength': True})
    p.pop('target_frequency_hz')
    normalized = parameters({'parameters': p}, {})
    assert normalized == dict(p, target_frequency_hz=None,
        measurements=dict(wavelength=True, optical_quality=False, dither=False))


def test_resolved_card_values_are_preserved_and_request_override_is_explicit():
    p = measurement_parameters(duration_s=7., history_wait_s=.4, timeout_s=99.)
    normalized = parameters({'parameters': p}, {'duration_s': 8.})
    assert normalized == dict(p, duration_s=8.)
    assert p['duration_s'] == 7.
    # No code default fills the omitted history budget, but an explicit request may.
    del p['history_wait_s']
    with pytest.raises(ValueError, match='history_wait_s'):
        parameters({'parameters': p}, {})
    assert parameters({'parameters': p}, {'history_wait_s': .6})['history_wait_s'] == .6
