from copy import deepcopy
import pytest
from ldcon.jobs.response_comparison import errors,metrics
from ldcon.core.contracts import DeviceError


def step(times,*,changes=None,phase='measurement',error=1.):
    return dict(status='succeeded',parameters=dict(target_frequency_hz=1e9,changes=changes or [],max_sample_gap_s=1.),
        samples=[dict(phase=phase,measurements=dict(wavelength=dict(acquired_unix_s=t,values=dict(frequency_hz=1e9+error)))) for t in times],
        application=dict(owner_result=dict(delta_frequency_hz=100e6,confirmed_unix_s=1.,started_unix_s=0.)),
        measurement_ended_unix_s=11.)


def steady(_):return dict(mse=1.,mse_se=.1,peak_hz=1.,rms_hz=1.,integral_saturation_fraction=0.)


def test_missing_transient_cannot_look_instantly_settled():
    baseline=step(range(12));leg=step([1,10,11],changes=[dict(parameter='target_frequency_hz',offset=100e6)])
    with pytest.raises(DeviceError,match='coverage'):
        metrics(dict(steps=[baseline,leg,baseline]),steady)


def test_band_uses_only_first_steady_measurement_and_is_frozen():
    baseline=step(range(12));extra=step([-.5],phase='baseline',error=1e7)['samples']
    baseline['samples']=extra+baseline['samples']
    leg=step(range(1,12),changes=[dict(parameter='target_frequency_hz',offset=100e6)])
    result=metrics(dict(steps=[baseline,leg,baseline]),steady)
    assert result['response_band_hz']==1.2
    assert result['response'][0]['settling_s_after_verified']==0
    noisy=deepcopy(leg)
    for row in noisy['samples']:row['measurements']['wavelength']['values']['frequency_hz']+=10
    result=metrics(dict(steps=[baseline,noisy,baseline]),steady,band=1.2)
    assert result['response_band_hz']==1.2 and result['unsettled']==1
    assert result['response'][0]['settling_s_after_verified'] is None
    assert result['response'][0]['absolute_error_integral_hz_s']>0
