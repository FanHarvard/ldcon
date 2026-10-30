import pytest

from ldcon.core.contracts import MeasurementUnavailable
from ldcon.jobs.measurement_acquisition import collect, source_eligible


def test_absolute_deadline_cannot_spin_on_float_subtraction_roundoff():
    state={'clock':1000.4,'pauses':0,'samples':0}
    def pause(seconds):
        state['pauses']+=1
        assert state['pauses']<20, 'Zero-time acquisition loop'
        state['clock']+=seconds
    def sample():state['samples']+=1;return True
    result=collect(sample,clock=lambda:state['clock'],pause=pause,check=lambda:None,
                   sample_interval_s=.1,duration_s=.3)
    assert result['accepted_samples'] in (3,4)
    assert state['clock']==1000.4+.3


def test_count_acquisition_missing_data_has_finite_deadline():
    state={'clock':0.}
    with pytest.raises(MeasurementUnavailable):
        collect(lambda:False,clock=lambda:state['clock'],pause=lambda s:state.update(clock=state['clock']+s),
                check=lambda:None,sample_interval_s=.1,minimum_samples=3,timeout_s=.2)
    assert state['clock']==.2


def test_source_gate_never_replaces_timestamp_or_accepts_duplicate_window():
    assert source_eligible(10.,previous=9.,now=10.1,max_age_s=.2,after=9.5,window_start=9.6)
    for stamp in (None,float('nan'),9.,11.):
        assert not source_eligible(stamp,previous=9.,now=10.1,max_age_s=.2)
    assert not source_eligible(10.,previous=9.,now=10.1,after=9.5,window_start=9.4)
