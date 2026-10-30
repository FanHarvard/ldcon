from copy import deepcopy
from types import SimpleNamespace

import pytest

from ldcon.core.contracts import DeviceError, MeasurementUnavailable
from ldcon.core.interface_schema import digest
from ldcon.drivers.dlcpro import UncertainDLCWrite
from ldcon.owners.dlcpro_parameter_trial import (
    ParameterTrialSettings, capabilities, settings, restore_ff_on_stop)
from .test_dlcpro_offline import rig
from .test_dlcpro_ff_adjustment import rig as ff_rig


def started(rig):
    owner = rig['owner']
    owner.start_lock(3e14, rig['control'])
    return owner


def test_current_uses_existing_lease_and_restores_with_missing_optical_data(rig):
    owner = started(rig)
    trial = ParameterTrialSettings(owner, 'current')
    lease, target = owner.lease, owner.target
    receipt = trial.switch({'current_ma':101.})
    assert receipt['confirmed'] and receipt['readback'] == {'current_ma':101.}
    assert owner.lease == lease and owner.mode == 'lock' and owner.target == target
    rig['source'].missing = True
    returned = trial.restore()
    assert returned['confirmed'] and returned['readback'] == {'current_ma':100.}
    assert owner.target == target and owner.mode == 'lock'


def test_current_invalid_input_and_physical_bound_refused_before_mutation(rig):
    owner = started(rig);trial = ParameterTrialSettings(owner, 'current')
    count = len(rig['driver'].writes)
    with pytest.raises(ValueError):trial.switch({'current_ma':1e6})
    rig['source'].missing = True
    with pytest.raises(MeasurementUnavailable):trial.switch({'current_ma':101.})
    assert len(rig['driver'].writes) == count


def test_current_unknown_write_quarantines_and_never_tries_checkpoint(rig):
    owner = started(rig);trial = ParameterTrialSettings(owner, 'current')
    rig['driver'].fail = True
    with pytest.raises(UncertainDLCWrite):trial.switch({'current_ma':101.})
    count = len(rig['driver'].writes)
    with pytest.raises(DeviceError):trial.restore()
    assert len(rig['driver'].writes) == count and owner.fault


def test_thermal_trial_restores_controls_and_temperature_without_optical_wait(rig):
    owner = started(rig);trial = ParameterTrialSettings(owner, 'slow_loop')
    initial = trial.checkpoint
    candidate = dict(tec_gain=.2, tec_period_s=.1, pzt_center_v=69.)
    writes = len(rig['driver'].writes)
    trial.switch(candidate)
    assert len(rig['driver'].writes) == writes  # Normal TEC loop makes the move.
    owner._tec(70., owner.clock()+1)
    assert rig['driver'].state['temperature'] != trial.checkpoint_temperature
    newer_target = owner.target+1e6
    owner.target = newer_target
    rig['source'].missing = True
    returned = trial.restore()
    assert returned['settings'] == returned['readback'] == initial
    assert returned['temperature_setpoint'] == rig['driver'].state['temperature'] == 25.
    assert returned['thermal_settled'] is False and owner.target == newer_target


@pytest.mark.parametrize('values', [dict(tec_gain=-1,tec_period_s=1,pzt_center_v=70),
    dict(tec_gain=.1,tec_period_s=0,pzt_center_v=70),
    dict(tec_gain=.1,tec_period_s=1,pzt_center_v=1000)])
def test_thermal_candidate_uses_control_and_absolute_bounds(rig, values):
    owner = started(rig);trial = ParameterTrialSettings(owner, 'slow_loop')
    before = deepcopy(owner.control)
    with pytest.raises(ValueError):trial.switch(values)
    assert owner.control == before


@pytest.fixture
def ff_owner(ff_rig):
    # Use the actual driver + Instrument and qualified formula console, with
    # an inaccessible reference. Only the source-read stub is synthetic.
    from .test_dlcpro_ff_delta_adjustment import QualifiedConsole
    from ldcon.drivers.dlcpro import DLCPro
    wire, inst, lease, events = ff_rig
    class CurrentConsole(QualifiedConsole):
        def command(self,expression,*,writing=False):
            prefix="(param-set! 'laser1:dl:cc:current-offset "
            if expression.startswith(prefix):
                assert writing
                self.bias=float(expression[len(prefix):-1])
                self.writes.append(('laser1:dl:cc:current-offset',self.bias))
                if len(self.writes)==self.fail_write:raise UncertainDLCWrite('current offset timeout')
                return '0'
            return super().command(expression,writing=writing)
    wire = CurrentConsole();inst.driver = DLCPro(wire)
    policy = deepcopy(inst.channels[1]['policy']);policy['ff_mode'] = 'preserve_existing'
    inst.channels[1].update(policy=deepcopy(policy),revision=digest(policy))
    inst.hardware['channels']['1']['ff_reference_v'] = None
    inst.snapshot(1,lease)
    owner = SimpleNamespace(instrument=inst,channel=1,lease=lease,policy=policy,
        fault=None,target_revision=1,read_control=lambda:None,audit=events.append)
    def healthy():
        if owner.fault:raise DeviceError('Fault retained')
    owner._healthy = healthy
    owner.ff_adjustment_evidence = dict(tolerance_ma=.01,evidence=dict(
        identity=deepcopy(wire.identity),pzt_hardware_bounds_v=wire.endpoints,
        pzt_delta_qualification=dict(verified=True,channel=1,source='synthetic-qualified-record')))
    return wire,owner


@pytest.mark.parametrize('automatic', [True,False])
def test_live_ff_preserves_total_without_any_reference(ff_owner, automatic):
    wire, owner = ff_owner
    wire.auto_preserve=automatic
    wire.reference=71.;wire.residual=.03
    total = wire.total
    trial = ParameterTrialSettings(owner,'feedforward')
    receipt = trial.switch({'ff_factor_ma_per_v':-.14})
    assert receipt['confirmed'] and wire.total == pytest.approx(total)
    assert owner.policy['ff_factor_ma_per_v'] == -.14
    assert owner.policy['ff_mode'] == 'preserve_existing'
    transaction = receipt['ff_transaction']
    assert transaction['diagnostic_reference_v'] is None
    assert all(x is None for x in transaction['model_residual_ma'].values())
    trial.restore()
    assert wire.factor == -.16 and wire.total == pytest.approx(total)


def test_live_ff_adoption_restores_first_saved_factor_at_lock_stop(ff_owner):
    wire, owner = ff_owner;wire.auto_preserve=True
    first = ParameterTrialSettings(owner,'feedforward')
    first.switch({'ff_factor_ma_per_v':-.14});first.commit()
    second = ParameterTrialSettings(owner,'feedforward')
    second.switch({'ff_factor_ma_per_v':-.12});second.commit()
    assert owner.runtime_ff_restore == {'ff_factor_ma_per_v':-.16}
    third = ParameterTrialSettings(owner,'feedforward')
    third.switch({'ff_factor_ma_per_v':-.14});third.restore()
    assert wire.factor == -.12  # Own checkpoint, not the earlier adoption.
    receipt = restore_ff_on_stop(owner)
    assert receipt['confirmed'] and wire.factor == -.16
    assert not hasattr(owner,'runtime_ff_restore')


def test_live_ff_explicit_prewrite_rejection_does_not_quarantine(ff_owner):
    wire, owner = ff_owner;trial=ParameterTrialSettings(owner,'feedforward')
    calls=[]
    def read():
        calls.append(1)
        if len(calls)>1:raise MeasurementUnavailable('stale before write')
    owner.read_control=read
    with pytest.raises(MeasurementUnavailable):trial.switch({'ff_factor_ma_per_v':-.14})
    assert not wire.writes and not owner.fault
    assert not owner.instrument.channels[1]['fault']


@pytest.mark.parametrize('stage', ['factor', 'compensation'])
def test_live_ff_unknown_effect_never_attempts_another_write(ff_owner,stage):
    wire,owner=ff_owner;trial=ParameterTrialSettings(owner,'feedforward')
    wire.fail_write=1 if stage=='factor' else 2
    with pytest.raises(UncertainDLCWrite):trial.switch({'ff_factor_ma_per_v':-.14})
    count=len(wire.writes)
    with pytest.raises(DeviceError):trial.restore()
    assert len(wire.writes)==count and owner.fault


def test_live_ff_full_delta_envelope_and_missing_proof_are_prewrite_rejections(ff_owner):
    wire,owner=ff_owner;trial=ParameterTrialSettings(owner,'feedforward')
    with pytest.raises(ValueError):trial.switch({'ff_factor_ma_per_v':-.5})
    assert not wire.writes and not owner.fault
    del owner.ff_adjustment_evidence
    assert capabilities(owner)['feedforward']['supported'] is False
    with pytest.raises(ValueError):ParameterTrialSettings(owner,'feedforward')
    assert not wire.writes


def test_ff_restoration_includes_offset_after_pzt_changes_and_multiple_increments(ff_owner):
    wire,owner=ff_owner;wire.auto_preserve=True
    origin=wire.bias
    trial=ParameterTrialSettings(owner,'feedforward')
    trial.switch({'ff_factor_ma_per_v':-.14})
    trial.switch({'ff_factor_ma_per_v':-.12})
    wire.pzt+=.25  # Normal PI changed the live anchor during observation.
    result=trial.restore()
    assert wire.factor==-.16 and wire.bias==pytest.approx(origin)
    assert result['current_offset_ma']==pytest.approx(origin)
    assert len(result['receipts'])==2


def test_stop_reverses_intra_trial_adopted_factor_path(ff_owner):
    wire,owner=ff_owner;wire.auto_preserve=True
    trial=ParameterTrialSettings(owner,'feedforward')
    trial.switch({'ff_factor_ma_per_v':-.14})
    trial.switch({'ff_factor_ma_per_v':-.12})
    trial.commit()
    result=restore_ff_on_stop(owner)
    assert wire.factor==-.16 and len(result['receipts'])==2
