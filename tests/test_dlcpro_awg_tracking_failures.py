"""No replay, independent source epochs, finite missing-data and cancel behavior."""
from dataclasses import replace

import pytest

from ldcon.core.contracts import DeviceError, MeasurementUnavailable
from ldcon.drivers.dlcpro import UncertainDLCWrite
from .test_dlcpro_awg_tracking import tracked, play, staircase
from .test_dlcpro_offline import rig


def pzt_writes(t):
    return [(k,v) for k,v in t['driver'].writes if k.endswith('pc:voltage-set')]


def test_unknown_tracking_write_retains_port_lease_and_never_replays(tracked):
    def fail(t, elapsed, _):
        if elapsed > 2.: t['driver'].fail = True
    with pytest.raises(UncertainDLCWrite): play(tracked, step_hook=fail)
    s, o = tracked['sweep'], tracked['owner']; rows = s.last['tracking_samples']
    assert s.last['outputs_uncertain'] and not s.last['restored']
    assert not rows[-1]['verified'] and not s.last['tracking']['active']
    assert s.port and not s.port.restored and not s.port.closed
    assert o.instrument.channels[o.channel]['lease'] == o.lease
    count = len(tracked['driver'].writes)
    s.tick(); o.tick()
    assert len(tracked['driver'].writes) == count
    with pytest.raises(DeviceError): o.instrument.release(o.channel,o.lease)


@pytest.mark.parametrize('kind', ['stale', 'missing'])
def test_missing_wavelength_holds_then_uses_existing_fatal_deadline(tracked, kind):
    count = []
    original = tracked['owner'].read_control
    def unavailable(*args, **kwargs):
        raise OSError('read only source unavailable')
    def fail(t, elapsed, _):
        if elapsed > 2.:
            if not count: count.append(len(pzt_writes(t)))
            if kind == 'missing': t['owner'].read_control = unavailable
            else: t['rig']['source'].fixed = t['rig']['clock']()-1.
    result = play(tracked, step_hook=fail)
    tracked['owner'].read_control = original
    assert len(pzt_writes(tracked)) == count[0]
    assert 'deadline' in result['stop_reason']
    assert result['restored'] and not result['outputs_uncertain'] and not result['pi_resumed']
    assert tracked['owner'].fault and not result['tracking']['active']


def test_fresh_but_previous_epoch_sample_is_not_integrated(tracked):
    original = tracked['owner'].read_control; observed = []
    def lagging(*args, **kwargs):
        sample = original(*args, **kwargs)
        epoch = tracked['sweep'].tracking.epoch
        return replace(sample, acquired_s=epoch['confirmed_after_s']-.001)
    def lag(t, elapsed, _):
        if 1.64 < elapsed < 1.8:
            t['owner'].read_control = lagging
            observed.append((len(pzt_writes(t)),t['sweep'].tracking.integral))
        else: t['owner'].read_control = original
    result = play(tracked, step_hook=lag)
    assert observed and len(set(observed)) == 1
    assert result['tracking']['writes'] and result['restored']
    first = result['tracking_samples'][0]
    assert first['integral_v'] == 0.  # Held interval was never accumulated.


def test_duplicate_acquisition_cannot_be_replayed_after_hold(tracked):
    original = tracked['owner'].read_control; saved = []; counts = []
    def duplicate(*args, **kwargs):
        if not saved: saved.append(original(*args, **kwargs))
        return saved[0]
    def hold(t, elapsed, _):
        if 2. < elapsed < 2.15:
            t['owner'].read_control = duplicate
            counts.append(len(pzt_writes(t)))
        else: t['owner'].read_control = original
    result = play(tracked, step_hook=hold)
    assert max(counts)-min(counts) <= 1
    acquisitions = [r['acquired_s'] for r in result['tracking_samples']]
    assert len(acquisitions) == len(set(acquisitions))


def test_cancel_arriving_during_read_is_checked_before_mutation(tracked):
    original = tracked['owner'].read_control; count = []
    def cancel(*args, **kwargs):
        sample = original(*args, **kwargs)
        tracked['sweep'].request_cancel(resume=False)
        return sample
    def inject(t, elapsed, _):
        if elapsed > 2. and not count:
            count.append(len(pzt_writes(t))); t['owner'].read_control = cancel
    result = play(tracked, step_hook=inject)
    assert len(pzt_writes(tracked)) == count[0]
    assert result['restored'] and not result['pi_resumed'] and not result['outputs_uncertain']


def test_acknowledged_slow_write_stops_known_output_without_false_uncertainty(tracked, monkeypatch):
    wire = tracked['driver']; original = wire.write
    def slow(path, value, **kwargs):
        result = original(path, value, **kwargs)
        if path.endswith('pc:voltage-set'): tracked['rig']['clock'].sleep(.12)
        return result
    monkeypatch.setattr(wire,'write',slow)
    result = play(tracked)
    assert 'timing budget' in result['stop_reason']
    assert result['tracking_samples'][-1]['verified']
    assert result['restored'] and not result['outputs_uncertain'] and not result['pi_resumed']


def test_changed_target_during_tracking_is_not_overwritten(tracked):
    changed = []
    def change(t, elapsed, _):
        if elapsed > 2. and not changed:
            t['owner'].target += 1e6; t['owner'].target_revision += 1
            changed.append(True)
    result = play(tracked, step_hook=change)
    assert 'context changed' in result['stop_reason']
    assert tracked['owner'].target == 3e14+1e6
    assert not result['pi_resumed'] and result['restored']


def test_partial_source_coverage_never_claims_qualified_dwells(tracked):
    original = tracked['owner'].observation.samples_after
    tracked['owner'].observation.samples_after = lambda after:[r for r in original(after) if int(r.acquired_s*10)%8 == 0]
    result = play(tracked, gain=1., drift=0.)
    assert result['playback_completed'] and not result['tracking']['qualified']
    assert any(not r['covered'] for r in result['tracking']['plateaus'])


def test_invalid_read_is_known_data_failure_not_unknown_output(tracked):
    original = tracked['owner'].read_control
    def invalid(*args, **kwargs):
        tracked['owner'].read_control = original
        raise DeviceError('malformed scalar source')
    def fail(t, elapsed, _):
        if elapsed > 2.: t['owner'].read_control = invalid
    result = play(tracked, step_hook=fail)
    assert result['stop_reason'].startswith('Invalid tracking wavelength read')
    assert result['restored'] and not result['outputs_uncertain'] and not result['pi_resumed']


@pytest.mark.parametrize('value', [float('nan'), float('inf'), None, 'broken'])
def test_malformed_frequency_sample_never_reaches_a_write(tracked, value):
    owner = tracked['owner']; original = owner.read_control; injected = []
    count = []
    def malformed(*args, **kwargs):
        owner.read_control = original
        sample = replace(original(*args,**kwargs),frequency_hz=value)
        return owner._validate_frequency(sample,target=kwargs.get('target'))
    def inject(t, elapsed, _):
        if elapsed > 2. and not injected:
            injected.append(True); count.append(len(pzt_writes(t)))
            owner.read_control = malformed
    result = play(tracked, step_hook=inject)
    # None is an unavailable sample under the existing input contract; finite
    # timeout/recovery can continue. Other malformed values stop before a write.
    if value is not None:
        assert result['stop_reason'].startswith('Invalid tracking wavelength read')
        assert len(pzt_writes(tracked)) == count[0]
    assert result['restored'] and not result['outputs_uncertain']


@pytest.mark.parametrize('budget', [.1, .2])
def test_100ms_poll_plus_verified_write_obeys_original_adc_budget(tracked, monkeypatch, budget):
    tracked['sweep'].config['adc_max_interval_s'] = budget
    wire = tracked['driver']; original = wire.write
    def delayed(path, value, **kwargs):
        result = original(path,value,**kwargs)
        if path.endswith('pc:voltage-set'): tracked['rig']['clock'].sleep(.04)
        return result
    monkeypatch.setattr(wire,'write',delayed)
    result = play(tracked,poll_s=.1)
    assert tracked['sweep'].config['adc_max_interval_s'] == budget
    assert result['restored'] and not result['outputs_uncertain']
    if budget == .1:
        assert not result['playback_completed']
        assert 'ADC observation gap' in result['stop_reason']
    else:
        assert result['playback_completed'] and result['tracking']['qualified']


def test_frozen_controller_cannot_claim_tracking_from_passive_wsu_data(tracked, monkeypatch):
    from copy import deepcopy
    from ldcon.core.interface_schema import digest
    from ldcon.owners.dlcpro_awg_tracking import Tracking
    o = tracked['owner']; o.policy['measurement_timeout_s'] = 20.
    o.instrument.channels[o.channel].update(policy=deepcopy(o.policy), revision=digest(o.policy))
    def frozen(*args): raise MeasurementUnavailable('unmatched ADC/WSU epoch')
    monkeypatch.setattr(Tracking,'_correct',frozen)
    result = play(tracked,gain=1.,drift=0.)
    assert result['playback_completed'] and not result['tracking_samples']
    assert all(r['within_tolerance'] for r in result['tracking']['plateaus'])
    assert not result['tracking']['qualified']
    assert all(not r['qualified'] for r in result['tracking']['plateaus'][1:])


def test_ownership_loss_never_attempts_a_correction_or_cleanup(tracked):
    counts = []
    def lose(t, elapsed, _):
        if elapsed > 2. and not counts:
            counts.append(len(t['driver'].writes))
            o = t['owner']; o.instrument.channels[o.channel]['lease'] = 'another-owner'
    with pytest.raises(DeviceError,match='ownership'): play(tracked,step_hook=lose)
    assert len(tracked['driver'].writes) == counts[0]
    result = tracked['sweep'].last
    assert result['outputs_uncertain'] and not result['tracking']['active']
    assert not tracked['sweep'].port.restored


def test_persistent_bad_read_after_verified_cleanup_does_not_invent_unknown_write(tracked):
    def malformed(*args,**kwargs): raise ValueError('nonfinite source frequency')
    def inject(t,elapsed,_):
        if elapsed > 2.: t['owner'].read_control = malformed
    result = play(tracked,step_hook=inject)
    assert result['restored'] and not result['outputs_uncertain'] and not result['pi_resumed']
    assert 'Post-cleanup frequency unavailable' in result['pi_resume_reason']
    assert result['stop_reason'].startswith('Invalid tracking wavelength read')
