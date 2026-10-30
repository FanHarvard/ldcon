from dataclasses import asdict, replace

import pytest

from tests.measurement_fixtures import measurement_parameters, measurement_batch

from tests.test_dlcpro_offline import rig, preserved_rig
from ldcon.core.contracts import DeviceError
from ldcon.jobs.dlcpro_cards import Protocol, Session
from ldcon.jobs.change_measure_dlc import DLCMovementAdapter,validate_owner_moves
from ldcon.jobs.change_measure_adapter import OperatingPointMeasurementAdapter
from ldcon.jobs.change_measure import run,run_batch


def binding(rig):
    protocol=Protocol(rig['owner'],rig['device'])
    class Local(Session):
        def rpc(self,action,_timeout=30,**values):
            return protocol.dispatch(dict(action=action,laser_id=rig['laser']['id'],**values))
    session=Local(rig['device']).connect()
    movement=DLCMovementAdapter(session)
    clock=rig['clock']
    adapter=OperatingPointMeasurementAdapter(movement,rig['laser'],clock=clock,sleep=clock.sleep,
                                            cancel=lambda:False,emit=lambda *_:None,wall_clock=clock)
    return adapter,session


def test_dlc_ordered_current_tec_pzt_uses_owner_and_does_not_touch_ff(rig):
    a,s=binding(rig);clock=rig['clock'];before=len(rig['driver'].writes)
    result=run(a,measurement_parameters(changes=[dict(parameter='current',value=101.),dict(parameter='temperature',value=25.01),
                             dict(parameter='piezo',value=70.1)],measurements={},duration_s=0.),
               clock=clock,wall_clock=clock,sleep=clock.sleep)
    assert result['status']=='succeeded'
    assert result['application']['readback']==pytest.approx(dict(current=101.,temperature=25.01,piezo=70.1))
    assert all('feedforward' not in path for path,value in rig['driver'].writes[before:])
    s.close()


def test_dlc_ff_joint_envelope_is_checked_before_any_write(rig):
    owner=rig['owner'];owner.policy['axes']['current']['bounds']=[90,101]
    before=len(rig['driver'].writes)
    with pytest.raises(ValueError,match='coupled FF'):
        validate_owner_moves(owner,[dict(parameter='current',value=100.9),dict(parameter='piezo',value=80.)])
    assert len(rig['driver'].writes)==before


def test_dlc_preserved_ff_uses_readable_delta_only(rig):
    rig=preserved_rig(rig)
    owner=rig['owner'];before=len(rig['driver'].writes)
    result=validate_owner_moves(owner,[dict(parameter='piezo',value=70.1)])
    assert result['valid'] and result['ff']['mode']=='preserve_existing'
    assert len(rig['driver'].writes)==before


def test_dlc_no_dither_capability_or_atomic_downgrade(rig):
    a,s=binding(rig)
    with pytest.raises(DeviceError):a.preflight([], 'ordered', {'dither':True})
    with pytest.raises(DeviceError):a.preflight([dict(parameter='piezo',value=70.1)], 'atomic', {})
    s.close()


def test_dlc_measured_quality_and_wavelength_have_source_timestamps(rig):
    source=rig['source'];original=source.read_mode
    def stamped():
        obs=original()
        return replace(obs,frequency=replace(obs.frequency,acquired_unix_s=rig['clock']()))
    source.read_mode=stamped
    a,s=binding(rig);clock=rig['clock']
    result=run(a,measurement_parameters(changes=[],measurements={'wavelength':True,'optical_quality':True},duration_s=.5,minimum_samples=3),
               clock=clock,wall_clock=clock,sleep=clock.sleep)
    assert result['status']=='succeeded'
    assert result['summary']['wavelength']['samples']>=3 and result['summary']['optical_quality']['samples']>=3
    s.close()


def test_dlc_snapshot_clock_never_replaces_unknown_source_clock(rig):
    a,s=binding(rig);clock=rig['clock']
    result=run(a,measurement_parameters(changes=[],duration_s=.3),clock=clock,wall_clock=clock,sleep=clock.sleep)
    assert result['status']=='measurement_incomplete' and result['samples']==[]
    s.close()


def test_dlc_batch_checks_coupled_ff_after_previous_step_before_writes(rig):
    # The first current change alone is safe. Applying the later PZT with this
    # new current (rather than the original current) would exceed the envelope.
    rig['owner'].policy['axes']['current']['bounds']=[90,101]
    a,s=binding(rig);clock=rig['clock'];before=len(rig['driver'].writes)
    steps=[measurement_parameters(changes=[dict(parameter='current',value=100.9)],measurements={},duration_s=0.),
           measurement_parameters(changes=[dict(parameter='piezo',value=80.)],measurements={},duration_s=0.)]
    with pytest.raises(ValueError,match='coupled FF'):
        run_batch(a,measurement_batch(steps),clock=clock,wall_clock=clock,sleep=clock.sleep)
    assert len(rig['driver'].writes)==before
    s.close()


def frequency_source(rig):
    """Independent source acquisitions, never polling timestamps substituted later."""
    def read():
        f=rig['source'].read_mode().frequency
        return replace(f,acquired_unix_s=f.acquired_s)
    rig['source'].read_frequency=read


def test_wavelength_only_uses_udp_with_stale_optical_cache(rig):
    from tests.test_dlcpro_udp import receiver,feed
    from ldcon.owners.dlcpro_observation import DatagramOpticalSource
    from ldcon.core.contracts import MeasurementUnavailable
    clock=rig['clock'];r=receiver(rig);original=rig['source'].read_mode
    rig['source'].read_mode=lambda:replace(original(),frequency=replace(original().frequency,acquired_unix_s=clock()))
    source=DatagramOpticalSource(r,rig['source'],quality_interval_s=1.,quality_max_age_s=2.,clock=clock)
    source.refresh_quality();clock.sleep(.4)
    assert feed(r,rig,12,value=300.000001)
    rig['owner'].observation=source
    a,s=binding(rig)
    row=a.read({'wavelength':True})['wavelength']
    acquired=r.read_frequency()
    assert row['acquired_unix_s']==acquired.acquired_unix_s
    assert row['values']['frequency_hz']==acquired.frequency_hz
    assert row['acquired_unix_s']!=clock()  # The UDP source has its own UTC clock.
    with pytest.raises(MeasurementUnavailable,match='fresh control'):
        a.read({'wavelength':True,'optical_quality':True})
    source.refresh_quality()
    both=a.read({'wavelength':True,'optical_quality':True})
    assert both['wavelength']['acquired_unix_s']==both['optical_quality']['acquired_unix_s']==clock()
    assert both['wavelength']['values']['frequency_hz']==3e14!=acquired.frequency_hz
    clock.sleep(.21)
    with pytest.raises(MeasurementUnavailable):a.read({'wavelength':True})
    assert rig['owner'].policy['max_sample_age_s']==.2
    s.close()


@pytest.mark.parametrize('optical', [False,True])
def test_read_only_measurement_refusal_is_a_skippable_sample(rig,monkeypatch,optical):
    from ldcon.core.contracts import MeasurementUnavailable
    from ldcon.core.instrument_client import BrokerError
    a,s=binding(rig)
    def reject(action,**kw):raise BrokerError('measurement','source unavailable')
    monkeypatch.setattr(s,'call',reject)
    with pytest.raises(MeasurementUnavailable):
        a.read({'wavelength':True,'optical_quality':optical})
    assert not s.uncertain_output


@pytest.mark.parametrize('code,uncertain', [('io',False),('fault',False),('measurement',True)])
def test_other_read_errors_and_existing_uncertainty_are_not_hidden(rig,monkeypatch,code,uncertain):
    from ldcon.core.instrument_client import BrokerError
    a,s=binding(rig);s.uncertain_output=uncertain
    def reject(action,**kw):raise BrokerError(code,'inspect required')
    monkeypatch.setattr(s,'call',reject)
    with pytest.raises(BrokerError):a.read({'wavelength':True})
    assert s.uncertain_output is uncertain


@pytest.mark.parametrize('permanent', [False,True])
def test_missing_postwrite_sample_keeps_verified_application_without_replay(rig,monkeypatch,permanent):
    from ldcon.core.instrument_client import BrokerError
    frequency_source(rig);a,s=binding(rig);clock=rig['clock']
    original=s.call;misses=[]
    def call(action,**kw):
        if action=='observe_frequency' and rig['driver'].writes and (permanent or not misses):
            misses.append(action)
            raise BrokerError('measurement','transient after acknowledged move')
        return original(action,**kw)
    monkeypatch.setattr(s,'call',call)
    steps=[measurement_parameters(changes=[dict(parameter='piezo',value=v)],duration_s=.5,minimum_samples=3)
           for v in (70.1,70.2)]
    result=run_batch(a,measurement_batch(steps),clock=clock,wall_clock=clock,sleep=clock.sleep)
    assert result['physical_outcome']=='applied'
    assert result['steps'][0]['application']['readback']=={'piezo':70.1}
    writes=[value for path,value in rig['driver'].writes if path.endswith('pc:voltage-set')]
    if permanent:
        assert result['status']=='measurement_incomplete' and len(result['steps'])==1
        assert writes==[70.1]
    else:
        assert result['status']=='succeeded' and len(misses)==1
        assert writes==[70.1,70.2]
    assert not s.uncertain_output and not rig['owner'].fault
    s.close()


def test_unknown_move_remains_unknown_and_is_not_replayed(rig):
    frequency_source(rig);a,s=binding(rig);clock=rig['clock'];rig['driver'].fail=True
    steps=[measurement_parameters(changes=[dict(parameter='piezo',value=v)],duration_s=.5) for v in (70.1,70.2)]
    with pytest.raises(DeviceError) as failed:
        run_batch(a,measurement_batch(steps),clock=clock,wall_clock=clock,sleep=clock.sleep)
    result=failed.value.measurement_result
    assert result['physical_outcome']=='unknown' and len(result['steps'])==1
    assert s.uncertain_output and rig['instrument'].channels[1]['fault']
    assert len(rig['driver'].writes)==1
