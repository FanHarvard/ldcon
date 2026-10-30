"""Offline E3 regression; bounded plant adapted from reorganization/tests/test_ecdl_operating_point.py.

Run with PYTHONPATH=runtime/<role> pytest tests/test_ecdl_acquisition_gate_fixes.py.
No network, subprocess, services, or physical devices are permitted.
"""
from dataclasses import replace

import pytest
import socket
import subprocess

from ldcon.contracts import DeviceError, FrequencySample, MeasurementUnavailable
from ldcon.ecdl_mode_map import run_mode_map, parameters as map_parameters
from ldcon.ecdl_operating_point import parameters, run_operating_point
from ldcon.ecdl_prelock import AxisPolicy, ModeObservation
from ldcon.interface_cards import CardCancelled


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError('Live I/O is forbidden in this offline regression')
    monkeypatch.setattr(socket, 'socket', denied)
    monkeypatch.setattr(socket, 'create_connection', denied)
    monkeypatch.setattr(subprocess, 'Popen', denied)


class Axis:
    def __init__(self, plant, name, value, slope):
        self.plant, self.name, self.value = plant, name, value
        self.policy = AxisPolicy('test-unit', 0., 1., .02, .08, .25, .05,
                                 slope, name, 'calibration-1', resolution=.00001)
        self.uncertain_output = False

    def readback(self):
        return self.value

    def move(self, value, elapsed):
        assert self.plant.open_loop
        assert self.policy.low <= value <= self.policy.high
        assert abs(value - self.value) <= self.policy.max_step + 1e-9
        assert abs(value - self.value) <= self.policy.max_slew_per_s * elapsed + 1e-9
        if self.plant.prewrite_axis == self.name:
            self.plant.prewrite_axis = None
            raise MeasurementUnavailable('Explicitly no write; fresh sample required')
        self.plant.commands.append(dict(axis=self.name, before=self.value, value=value, at=self.plant.now))
        self.value = value
        if self.plant.fail_axis == self.name:
            self.uncertain_output = True
            raise OSError('Write reached hardware; reply lost')

    def begin_recovery(self):
        self.plant.recovery_flags.append(True)

    def end_recovery(self):
        self.plant.recovery_flags.append(False)


class Plant:
    def __init__(self):
        self.now = 0.
        self.open_loop = True
        self.commands, self.events, self.recovery_flags = [], [], []
        self.current = Axis(self, 'current', .5, 100.)
        self.temperature = Axis(self, 'temperature', .48, 200.)
        self.piezo = Axis(self, 'piezo', .46, 50.)
        self.classification_revision = 'classifier-1'
        self.mode = 'desired'
        self.frequency_offset = 0.
        self.read_failures = 0
        self.unavailable_until = None
        self.fail_axis = None
        self.prewrite_axis = None
        self.cancelled = False
        self.on_emit = None

    @property
    def ports(self):
        return dict(current=self.current, temperature=self.temperature, piezo=self.piezo,
                    observation=self, loop=self)

    @property
    def arguments(self):
        return dict(clock=lambda: self.now, sleep=self.sleep,
                    cancel=lambda: self.cancelled, emit=self.emit)

    def sleep(self, duration):
        self.now += duration

    def emit(self, kind, value):
        self.events.append((kind, value))
        if self.on_emit:
            self.on_emit(kind, value)

    def is_open_loop(self):
        return self.open_loop

    def read_mode(self):
        self.now += .001
        if self.read_failures:
            self.read_failures -= 1
            raise OSError('Temporary read transport failure')
        if self.unavailable_until is not None and self.now < self.unavailable_until:
            raise MeasurementUnavailable('Temporary missing optical stream')
        frequency = (1000. + 100. * (self.current.value - .5)
                     + 200. * (self.temperature.value - .5)
                     + 50. * (self.piezo.value - .5) + self.frequency_offset)
        sample = FrequencySample(int(self.now * 100000), frequency, self.now,
                                 self.now, 'valid', 'synthetic')
        metrics = dict(side_peak_prominence_max=.05, side_peak_height_max=.05,
                       main_fwhm_fraction=.08, near_main_peak_prominence_max=.02)
        analysis = dict(qualified=True, period_qualified=True,
                        revision='full-period-free-position-test', period_reference_px=16.,
                        classification_revision=self.classification_revision,
                        raw_patterns={'fizeau': [0., 1., 0., 0.]})
        return ModeObservation(sample, True, self.mode, True, .01,
                               metrics=metrics, fringe_analysis=analysis)

    def mapping(self):
        p = map_parameters(dict(parameters={}), dict(target_frequency_hz=1000.,
            current_start_fraction=.4, current_end_fraction=.6, current_step_fraction=.05,
            discontinuity_hz=100., capture_hz=50., dwell_s=.05,
            sample_interval_s=.1, timeout_s=300.))
        return run_mode_map(self.ports, p, **self.arguments)['artifact']

    def acquire(self, artifact, **overrides):
        settings = dict(map_artifact=artifact,
            target_frequency_hz=1002., tolerance_hz=.02,
            replay_tolerance_hz=.1, hop_threshold_hz=50.,
            temperature_center_fraction=.5, piezo_center_fraction=.5,
            stable_s=.5, replay_stable_s=.2, sample_interval_s=.1,
            measurement_timeout_s=4., timeout_s=600.)
        settings.update(overrides)
        p = parameters(dict(parameters={}), settings)
        return run_operating_point(self.ports, p, **self.arguments)


def degrade_after_replay(plant, *, qualified=False):
    """Keep the map/replay qualification strict; degrade only corrective moves."""
    read = plant.read_mode
    plant.degraded = False

    def degraded_read():
        obs = read()
        if not plant.degraded:
            return obs
        analysis = dict(obs.fringe_analysis, qualified=qualified, period_qualified=qualified)
        return replace(obs, single_mode=False, maintenance_required=True,
                       fringe_analysis=analysis)

    def degrade(kind, row):
        if kind == 'command_applied' and row['stage'] == 'temperature centering':
            plant.degraded = True

    plant.read_mode = degraded_read
    plant.on_emit = degrade
    return degrade


@pytest.mark.parametrize('current_feedback', [False, True])
@pytest.mark.parametrize('qualified', [False, True])
def test_degraded_same_branch_reaches_frequency_without_claiming_spectral_quality(current_feedback, qualified):
    plant = Plant()
    artifact = plant.mapping()
    degrade_after_replay(plant, qualified=qualified)
    result = plant.acquire(artifact, current_feedback=current_feedback)
    assert plant.degraded
    assert result['status'] == 'succeeded'
    assert abs(result['final_frequency_hz'] - 1002.) <= .02
    assert result['spectral_quality_passed'] is False
    assert result['single_mode'] is False
    assert result['lock_enabled'] is False
    assert result['original_window_valid'] is False
    assert result['physical_mode_identity_verified'] is False
    degraded = [row for kind, row in plant.events if kind == 'ecdl_acquisition_quality_degraded']
    assert degraded and all(row['branch_continuous'] for row in degraded)
    assert all(row['observation']['mode_id'] == 'desired' for row in degraded)
    assert not any(kind in ('ecdl_acquisition_branch_lost', 'ecdl_acquisition_reference_restored')
                   for kind, _ in plant.events)
    # Synthetic Axis.move enforces absolute range, per-step and slew bounds.
    assert abs(result['outputs']['temperature'] - .48) <= .1
    assert abs(result['outputs']['current'] - .5) <= .001


@pytest.mark.parametrize('new_mode,frequency_offset', [('other', 0.), ('desired', 500.)])
def test_real_branch_change_or_frequency_discontinuity_still_restores_and_rejects(new_mode, frequency_offset):
    plant = Plant()
    artifact = plant.mapping()
    degrade = degrade_after_replay(plant)
    move = plant.current.move

    def restore_on_preparation(value, elapsed):
        move(value, elapsed)
        if value <= .41:
            plant.mode = 'desired'
            plant.frequency_offset = 0.
            plant.degraded = False

    def hop_after_centering(kind, row):
        degrade(kind, row)
        if kind == 'ecdl_acquisition_centered':
            plant.mode = new_mode
            plant.frequency_offset = frequency_offset

    plant.current.move = restore_on_preparation
    plant.on_emit = hop_after_centering
    with pytest.raises(DeviceError, match='reference restored, remap required'):
        plant.acquire(artifact)
    lost = [row for kind, row in plant.events if kind == 'ecdl_acquisition_branch_lost']
    assert len(lost) == 1
    assert lost[0]['observation']['mode_id'] == new_mode
    assert lost[0]['residual_hz'] == pytest.approx(frequency_offset)
    assert plant.current.value == pytest.approx(.5)
    assert plant.temperature.value == pytest.approx(.48)
    assert plant.piezo.value == pytest.approx(.46)
    assert plant.mode == 'desired'
    assert any(kind == 'ecdl_acquisition_reference_restored' for kind, _ in plant.events)
    assert not any(kind == 'ecdl_operating_point' for kind, _ in plant.events)


def test_degraded_spectrum_does_not_hide_classification_identity_change():
    plant = Plant()
    artifact = plant.mapping()
    degrade = degrade_after_replay(plant)
    commands_at_change = []

    def change(kind, row):
        degrade(kind, row)
        if kind == 'ecdl_acquisition_centered':
            plant.classification_revision = 'different-classifier'
            commands_at_change.append(len(plant.commands))

    plant.on_emit = change
    with pytest.raises(DeviceError, match='classification'):
        plant.acquire(artifact)
    assert len(plant.commands) == commands_at_change[0]
    assert not any(kind == 'ecdl_acquisition_reference_restored' for kind, _ in plant.events)


def test_uncertain_corrective_write_never_replays_even_with_degraded_spectrum():
    plant = Plant()
    artifact = plant.mapping()
    degrade = degrade_after_replay(plant)

    def fail_next_approach(kind, row):
        degrade(kind, row)
        if kind == 'ecdl_acquisition_centered':
            plant.fail_axis = 'temperature'

    plant.on_emit = fail_next_approach
    with pytest.raises(OSError, match='reply lost'):
        plant.acquire(artifact)
    assert plant.degraded and plant.temperature.uncertain_output
    assert plant.commands[-1]['axis'] == 'temperature'
    assert plant.temperature.value == plant.commands[-1]['value']
    assert not any(kind in ('ecdl_acquisition_branch_lost', 'ecdl_acquisition_reference_restored')
                   for kind, _ in plant.events)


def test_initial_scientific_reference_still_requires_qualified_spectrum():
    plant = Plant()
    artifact = plant.mapping()
    degrade_after_replay(plant)
    plant.degraded = True
    with pytest.raises(DeviceError, match='ECDL experiment deadline; outputs held'):
        plant.acquire(artifact, timeout_s=8.)
    verification = [row for kind, row in plant.events if kind == 'ecdl_map_observation'
                    and row['stage'] == 'recovery optical verification']
    assert len(verification) >= 3
    assert all(row['single_mode'] is False for row in verification)
    assert not any(kind == 'ecdl_acquisition_centered' for kind, _ in plant.events)

