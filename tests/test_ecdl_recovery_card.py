"""ONE ECDL recovery card (prepare.ecdl_recovery, 2026-10-27): plan + perturbation test -> one artifact that the
steady lock (recovery_ref) and the standalone recover card execute through the same procedure (rule F).
Deterministic plant, no hardware, no sockets."""
import copy
from unittest.mock import Mock

import pytest

from ldcon.core.contracts import DeviceError, FrequencySample
from ldcon.core.interface_schema import parameters as schema_parameters
from ldcon.core.operation_registry import spec, normalize_parameters, CARD_TYPES
from ldcon.jobs import ecdl_recovery, ecdl_mode_recovery, ecdl_steady
from . import legacy_recovery_executor_fixture as historical_executor
from . import legacy_mapped_recovery_fixture as historical_mapped
from ldcon.jobs.ecdl_mode_map import run_mode_map, parameters as map_parameters
from ldcon.jobs.ecdl_recovery import run_recovery_card
from .legacy_recovery_executor_fixture import run_tested_recovery, checked_recovery, SCHEMA
from .legacy_recovery_plan_fixture import build_plan, piezo_hz_per_count_from_profile
from .legacy_recovery_test_fixture import run_recovery_test, plan_translation
from .legacy_mapped_recovery_fixture import MappedStationaryRecovery
from ldcon.jobs.ecdl_mode_recovery import run_mode_recovery, DEFAULTS as HOP_DEFAULTS
from ldcon.jobs.workflow_artifacts import KINDS, freeze_inputs, hydrate_inputs
from ldcon.owners.ecdl_prelock import AxisPolicy, ModeObservation


class Axis:
    def __init__(self, plant, name, value, slope):
        self.plant, self.name, self.value = plant, name, value
        self.policy = AxisPolicy('test-unit', 0., 1., .02, .08, .25, .05, slope, name, 'calibration-1', resolution=.00001)
        self.uncertain_output = False

    def readback(self):
        return self.value

    def move(self, value, elapsed):
        assert self.plant.open_loop and self.policy.low <= value <= self.policy.high
        assert abs(value - self.value) <= self.policy.max_step + 1e-9
        self.plant.commands.append((self.name, round(value, 6)))
        self.value = value

    def begin_recovery(self):
        pass

    def end_recovery(self):
        pass


class Plant:
    """Linear tuning (100 Hz/current unit, 200 Hz/TEC unit, 50 Hz/PZT count) with a mode hop (+300 Hz) once the
    current leaves a window around the mapped reference (enabled after the map)."""
    def __init__(self):
        self.now = 0.
        self.open_loop = True
        self.commands, self.events = [], []
        self.current = Axis(self, 'current', .5, 100.)
        self.temperature = Axis(self, 'temperature', .48, 200.)
        self.piezo = Axis(self, 'piezo', .46, 50.)
        self.hop_center = None
        self.cancelled = False

    @property
    def ports(self):
        return dict(current=self.current, temperature=self.temperature, piezo=self.piezo, observation=self, loop=self)

    @property
    def arguments(self):
        return dict(clock=lambda: self.now, sleep=self.sleep, cancel=lambda: self.cancelled, emit=self.emit)

    def sleep(self, duration):
        self.now += duration

    def emit(self, kind, value):
        self.events.append((kind, copy.deepcopy(value)))

    def is_open_loop(self):
        return self.open_loop

    def frequency(self):
        f = (1000. + 100. * (self.current.value - .5) + 200. * (self.temperature.value - .5)
             + 50. * (self.piezo.value - .5))
        if self.hop_center is not None and abs(self.current.value - self.hop_center) > .015:
            f += 300.
        return f

    def read_mode(self):
        self.now += .001
        sample = FrequencySample(int(self.now * 100000), self.frequency(), self.now, self.now, 'valid', 'synthetic')
        analysis = dict(qualified=True, period_qualified=True, revision='full-period-free-position-test',
                        period_reference_px=16., classification_revision='classifier-1',
                        raw_patterns={'fizeau': [0., 1., 0., 0.]})
        metrics = dict(side_peak_prominence_max=.05, side_peak_height_max=.05, main_fwhm_fraction=.08,
                       near_main_peak_prominence_max=.02)
        return ModeObservation(sample, True, 'desired', True, .01, metrics=metrics, fringe_analysis=analysis)

    def response_info(self):
        self.now += .001
        return dict(observation=dict(frequency=dict(frequency_hz=self.frequency(), acquired_s=self.now, unit='Hz',
                                                    acquisition_known=False), mode_id='desired'),
                    outputs=dict(current=self.current.value, temperature=self.temperature.value, piezo=self.piezo.value))

    def mapping(self):
        p = map_parameters(dict(parameters={}), dict(target_frequency_hz=1000., current_start_fraction=.4,
            current_end_fraction=.6, current_step_fraction=.05, discontinuity_hz=100., capture_hz=50., dwell_s=.05,
            sample_interval_s=.1, timeout_s=300.))
        artifact = run_mode_map(self.ports, p, **self.arguments)['artifact']
        from ldcon.jobs.ecdl_mode_map import selected_reference
        self.hop_center = selected_reference(artifact)[2]['outputs']['current']
        return artifact


LASER = dict(interfaces=dict(lock=dict(device='rig')), devices=dict(rig=dict(
    control_settings=dict(steady_lock=dict(wavelength_nm_per_count=-5.4e-8)))))


def card_parameters(plant, mapping, **overrides):
    p = dict(map_artifact=mapping, tolerance_hz=1., hop_threshold_hz=50., stable_s=.2, confirm_samples=3,
             verification_timeout_s=120., timeout_s=600., sample_interval_s=.1, dwell_s=.1, measurement_timeout_s=5.,
             max_perturbation_fraction=.1, perturbations=[dict(axis='current', offset_fraction=.03),
                                                         dict(axis='current', offset_fraction=-.03)],
             piezo_hz_per_count=50., context_mode='translate')
    p.update(overrides)
    return p


def prepared(plant=None, **overrides):
    plant = plant or Plant()
    mapping = plant.mapping()
    p = card_parameters(plant, mapping, **overrides)
    from .recovery_legacy_fixtures import archived_preparation
    result = archived_preparation(plant.ports, p, **plant.arguments)
    return plant, result


# --- the merged card -------------------------------------------------------------------------------------------







def test_retired_plan_and_test_names_point_to_the_merged_card():
    for name in ('plan.ecdl_recovery', 'test.ecdl_recovery'):
        with pytest.raises(ValueError, match='Retired card: prepare.ecdl_recovery'):
            spec(name)
        assert spec(name, historical=True).execution == 'retired'
    # old job parameters stay readable
    old = dict(id='recovery-plan', operation='plan.ecdl_recovery', uses=[], parameters=dict(tolerance_hz=1e9, hop_threshold_hz=5e9))
    assert normalize_parameters(old, {}, historical=True)['tolerance_hz'] == 1e9


def test_old_recovery_artifact_cannot_be_frozen_for_new_execution(tmp_path):
    _, result=prepared()
    with pytest.raises(ValueError,match='artifact'):
        freeze_inputs(tmp_path,dict(recovery_artifact=result['artifact']))


def test_retired_map_executor_cannot_be_instantiated():
    from ldcon.jobs.ecdl_mapped_recovery import MappedStationaryRecovery
    with pytest.raises(ValueError,match='retired'):MappedStationaryRecovery(None,None)


def steady(artifact, **overrides):
    p = dict(target_frequency_hz=artifact['selected_reference']['frequency_hz'], tolerance_hz=.5, strategy='hold',
             measurement_timeout_s=5., sample_interval_s=.1, recovery_artifact=artifact)
    p.update(overrides)
    return p


def lock_for(plant):
    lock = Mock()
    lock.recovery_ports.return_value = plant.ports
    lock.response_info.side_effect = plant.response_info
    lock.hold.return_value = dict(held=True)
    return lock




def test_mapped_recovery_consumes_the_merged_artifact_and_recovers_in_lock_with_the_same_procedure():
    plant, result = prepared()
    artifact = result['artifact']
    lock = lock_for(plant)
    recovery = MappedStationaryRecovery(lock, steady(artifact), **plant.arguments)
    assert recovery.proof is recovery.plan and recovery.context_mode == 'translate'
    recovery.prime()
    recovery.armed = True
    # a hop: the laser sits off the branch (current displaced by the owner / PI); 3 fresh samples confirm it
    plant.current.value = plant.hop_center + .03
    assert not recovery.probe() and not recovery.probe() and recovery.probe()
    recovery.recover('confirmed original-mode frequency/identity loss')
    lock.hold.assert_called_once()
    lock.check_ready.assert_called_once_with('hold')
    lock.arm.assert_called_once_with(artifact['selected_reference']['frequency_hz'], 1., 'hold')
    verified = [v for k, v in plant.events if k == 'ecdl_mapped_recovery_verified'][-1]
    assert verified['recovery']['context_mode'] == 'translate' and abs(verified['recovery']['measured_frequency_hz'] - verified['recovery']['predicted_frequency_hz']) <= 1.
    assert abs(plant.current.value - artifact['selected_reference']['outputs']['current']) < 1e-6
    # rule F: the lock and the standalone card call the same function
    assert historical_mapped.execute is historical_executor.execute
    # evidence checks: unverified / tampered artifacts and a different target are refused before any write
    from ldcon.jobs.ecdl_mode_map import seal
    with pytest.raises(ValueError, match='test evidence'):
        MappedStationaryRecovery(lock, steady(seal(dict(artifact, verified=False))), **plant.arguments)
    with pytest.raises(ValueError, match='hash'):
        MappedStationaryRecovery(lock, steady(dict(artifact, branch_id='other')), **plant.arguments)
    with pytest.raises(ValueError, match='Steady target differs'):
        MappedStationaryRecovery(lock, steady(artifact, target_frequency_hz=artifact['selected_reference']['frequency_hz'] + 10.), **plant.arguments)


def test_deprecated_plan_test_pair_is_still_accepted_by_the_lock():
    plant = Plant()
    mapping = plant.mapping()
    plan = build_plan(dict(map_artifact=mapping, tolerance_hz=1., hop_threshold_hz=50., stable_s=.2, timeout_s=120.))
    assert plan['context_mode'] == 'restore'   # plan-only callers keep the original behaviour
    plant.current.value = plan['selected_reference']['outputs']['current']   # the retired test expected the laser at the reference
    test = run_recovery_test(plant.ports, dict(plan_artifact=plan, perturbations=[dict(axis='current', offset_fraction=.03)],
        max_perturbation_fraction=.1, sample_interval_s=.1, dwell_s=.1, measurement_timeout_s=5., timeout_s=600.), **plant.arguments)
    assert test['status'] == 'succeeded'
    recovery = MappedStationaryRecovery(lock_for(plant), dict(steady(plan), recovery_artifact=None,
        recovery_plan_artifact=plan, recovery_test_artifact=test['artifact']), **plant.arguments)
    assert recovery.context_mode == 'restore' and recovery.proof['schema'] == 'ldcon.ecdl-recovery-test.v1'


# --- the standalone recover card -----------------------------------------------------------------------------------

def hop_parameters(artifact, **overrides):
    p = dict(HOP_DEFAULTS, recovery_artifact=artifact, sample_interval_s=.1, measurement_timeout_s=5.,
             target_frequency_hz=artifact['selected_reference']['frequency_hz'] if artifact else None)
    p.update(overrides)
    return p




# --- context_mode translate ------------------------------------------------------------------------------------------

def test_translate_mode_recovers_at_the_predicted_frequency_with_a_drifted_tec_and_piezo():
    plant, result = prepared()
    artifact = result['artifact']
    reference = artifact['selected_reference']
    plant.temperature.value = reference['outputs']['temperature'] + .1      # within 0.2
    plant.piezo.value = reference['outputs']['piezo'] + .02                  # within 10000 counts (plant units)
    plant.current.value = plant.hop_center + .03
    plant.commands.clear()
    outcome = run_tested_recovery(plant.ports, hop_parameters(artifact), **plant.arguments)
    recovery = outcome['recovery']
    predicted = reference['frequency_hz'] + 200. * .1 + 50. * .02
    assert recovery['context_mode'] == 'translate'
    assert abs(recovery['delta_temperature'] - .1) < 1e-9 and abs(recovery['delta_piezo'] - .02) < 1e-9
    assert abs(recovery['predicted_frequency_hz'] - predicted) < 1e-6
    assert abs(recovery['measured_frequency_hz'] - predicted) <= 1.
    # TEC and PZT were kept (no restore_context writes); the current route was shifted by current_per_tec_v x dTEC
    assert not any(axis in ('temperature', 'piezo') for axis, _ in plant.commands)
    assert abs(plant.current.value - (reference['outputs']['current'] - .07 * .1)) < 1e-6   # I = I0 + (-0.07) x dTEC
    assert recovery['optical_quality']['single_mode'] is True


def test_translate_mode_refuses_outside_the_context_tolerance_and_a_shifted_recipe_outside_the_interval():
    plant, result = prepared()
    artifact = result['artifact']
    reference = artifact['selected_reference']
    plant.temperature.value = reference['outputs']['temperature'] + .3      # beyond 0.2
    plant.commands.clear()
    with pytest.raises(DeviceError, match='Present temperature differs from the map context'):
        run_tested_recovery(plant.ports, hop_parameters(artifact), **plant.arguments)
    assert plant.commands == []
    plant.temperature.value = reference['outputs']['temperature']
    plant.piezo.value = reference['outputs']['piezo'] + .5
    tolerant = dict(artifact, context_tolerances=dict(temperature=.2, piezo=.1))
    from ldcon.jobs.ecdl_mode_map import seal
    with pytest.raises(DeviceError, match='Present piezo differs'):
        run_tested_recovery(plant.ports, hop_parameters(seal(tolerant), target_frequency_hz=None), **plant.arguments)
    plant.piezo.value = reference['outputs']['piezo']
    # the shifted recipe must stay inside the owner's interval
    plant.temperature.value = reference['outputs']['temperature'] + .1
    from ldcon.jobs.ecdl_mode_map import BoundedExperiment
    experiment = BoundedExperiment(plant.ports, dict(timeout_s=60., measurement_timeout_s=5., sample_interval_s=.1, max_sample_age_s=1.), **plant.arguments)
    translation = plan_translation(experiment, artifact)
    assert all(abs(t['value'] - s['value'] + .007) < 1e-9 for t, s in zip(translation['recipe'],
               [s for s in artifact['recipe'] if s['action'] != 'restore_context']))
    with pytest.raises(DeviceError, match='Translated recovery recipe .* leaves the owner interval'):
        plan_translation(experiment, dict(artifact, current_per_tec_v=10.))
    assert plant.commands == []




def test_restore_mode_keeps_the_original_behaviour():
    plant, result = prepared(context_mode='restore')
    artifact = result['artifact']
    assert artifact['context_mode'] == 'restore' and artifact['verified'] is True
    reference = artifact['selected_reference']
    plant.temperature.value = reference['outputs']['temperature'] + .1
    plant.current.value = plant.hop_center + .03
    plant.commands.clear()
    outcome = run_tested_recovery(plant.ports, hop_parameters(artifact), **plant.arguments)
    assert outcome['recovery']['context_mode'] == 'restore' and outcome['recovery']['delta_temperature'] == 0.
    assert any(axis == 'temperature' for axis, _ in plant.commands)      # restore_context wrote the TEC back
    assert abs(plant.temperature.value - reference['outputs']['temperature']) < 1e-9




@pytest.mark.parametrize('legacy', [dict(classes=[]),dict(map_artifact={}),dict(branch_match=None),dict(recovery_ref={'job_id':'old','sha256':'a'*64})])
def test_active_recovery_rejects_old_algorithm_before_ports(legacy):
    ports=Mock()
    with pytest.raises(ValueError,match='Legacy'):
        run_mode_recovery(ports,legacy,clock=Mock(),sleep=Mock(),cancel=Mock(),emit=Mock())
    assert not ports.mock_calls
    with pytest.raises(ValueError,match='Legacy'):
        run_recovery_card(ports,legacy,clock=Mock(),sleep=Mock(),cancel=Mock(),emit=Mock())
    assert not ports.mock_calls


@pytest.mark.parametrize('key',['recovery_ref','recovery_artifact','recovery_plan_ref','recovery_test_ref'])
def test_active_steady_rejects_old_artifact_dispatch(key):
    with pytest.raises(ValueError,match='Legacy recovery parameters retired'):
        ecdl_steady.parameters({'parameters':{}},{key:{'job_id':'old','sha256':'a'*64}})
