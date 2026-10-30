"""Bounded open-loop re-arm trim of the steady lock (ecdl_rearm_trim, steady parameter ``rearm_trim``; hardware
finding 2026-10-27 19:53-20:37: the tested recovery verifies within tolerance_hz = 1 GHz, the owner arms within the
100 MHz acquisition tolerance). Deterministic plant of test_ecdl_recovery_card, no hardware, no sockets."""
import copy
import importlib.util
from pathlib import Path

import pytest

from ldcon.core.contracts import DeviceError
from ldcon.jobs import ecdl_steady
from .legacy_mapped_recovery_fixture import MappedStationaryRecovery
from .legacy_rearm_trim_fixture import DEFAULTS, parameters, trim_to_target, TrimBranchLost
from ldcon.jobs.ecdl_mode_map import BoundedExperiment
from ldcon.jobs.ecdl_steady import run_steady_lock

from .test_ecdl_recovery_card import Plant, prepared, steady as steady_parameters


def archived_steady_parameters(card, overrides):
    # Historical numeric trim contracts remain regression-tested, but active
    # steady admission explicitly rejects these old artifacts.
    merged={**card['parameters'],**overrides}
    old={k:merged.pop(k) for k in list(merged) if k in ecdl_steady.RETIRED_RECOVERY_PARAMETERS}
    old['rearm_trim']=parameters(old.get('rearm_trim'))
    old.setdefault('mapped_recovery_max_attempts',20)
    return {**ecdl_steady.parameters({'parameters':{}},merged),**old}

C = 299792458.


def laser_with_piezo_slope(hz_per_count, frequency_hz):
    """A laser profile whose steady_lock.wavelength_nm_per_count converts to ``hz_per_count`` at ``frequency_hz``."""
    nm_per_count = -hz_per_count * C * 1e9 / frequency_hz ** 2
    return dict(interfaces=dict(lock=dict(device='rig')), devices=dict(rig=dict(
        control_settings=dict(steady_lock=dict(wavelength_nm_per_count=nm_per_count)))))


class Lock:
    """The typed lock interface as the steady card sees it; arm/hold toggle the plant loop."""
    def __init__(self, plant):
        self.plant, self.calls, self.target = plant, [], None

    def recovery_ports(self):
        return self.plant.ports

    def response_info(self):
        return self.plant.response_info()

    def check_ready(self, strategy):
        self.calls.append(('check_ready', strategy))

    def arm(self, target, tolerance, strategy):
        assert self.plant.open_loop
        self.calls.append(('arm', target, tolerance, strategy))
        self.plant.open_loop = False
        self.target = target

    def hold(self, reason):
        self.calls.append(('hold', reason))
        self.plant.open_loop = True
        return dict(held=True, reason=reason)

    def heartbeat(self):
        return True

    def read_cycle(self):
        f = self.plant.frequency()
        self.plant.now += .001
        return dict(state='locked', frequency_hz=f, error_hz=f - self.target, outputs={}, single_mode=True, actions=[])

    def adjust(self, action):
        return dict(applied=True)


def events(plant, kind):
    return [v for k, v in plant.events if k == kind]


def offset_target(artifact, offset):
    return artifact['selected_reference']['frequency_hz'] + offset


# --- parameters --------------------------------------------------------------------------------------------------

def test_rearm_trim_parameters_default_complete_and_validate():
    p = archived_steady_parameters(dict(parameters={}), dict(target_frequency_hz=7e14, tolerance_hz=1e6))
    assert p['rearm_trim'] == DEFAULTS == dict(enabled=True, max_counts=15000., max_steps=4, tec_settle_s=45.)
    partial = archived_steady_parameters(dict(parameters=dict(rearm_trim=dict(max_counts=5000))), dict(target_frequency_hz=7e14, tolerance_hz=1e6))
    assert partial['rearm_trim'] == dict(enabled=True, max_counts=5000, max_steps=4, tec_settle_s=45.)
    assert parameters(None) == DEFAULTS and parameters(dict(enabled=False))['enabled'] is False
    for bad in (dict(enabled=1), dict(max_counts=0), dict(max_steps=0), dict(max_steps=2.), dict(max_steps=21),
                dict(tec_settle_s=-1), dict(tec_settle_s=601), dict(other=1), 'x'):
        with pytest.raises(ValueError):
            parameters(bad)
    assert 'rearm_trim' not in ecdl_steady.DEFAULTS  # archived fixture never restores the active control


def test_rearm_trim_is_retired_before_runtime_owner_access(tmp_path):
    from unittest.mock import Mock
    from ldcon.jobs.card_runtime import CardRuntime
    card=dict(id='steady-lock',operation='servo.ecdl_steady_lock',uses=['lock'],parameters={})
    p=ecdl_steady.parameters(card,dict(target_frequency_hz=7e14,tolerance_hz=1e6))
    lock=Mock(runtime_owner_parameters=False,legacy_runtime_values={})
    with pytest.raises(ValueError,match='Legacy recovery parameters retired'):
        CardRuntime(tmp_path,card,{**p,'rearm_trim':None},lambda *a:None,lock=lock)
    assert not lock.mock_calls
    runtime=CardRuntime(tmp_path,card,p,lambda *a:None,lock=lock)
    assert 'rearm_trim' not in runtime.specs and 'mapped_recovery_max_attempts' not in runtime.specs
    with pytest.raises(ValueError,match='Legacy recovery parameters retired'):
        runtime.validate(dict(rearm_trim=dict(max_steps=2)))
    runtime.close()


# --- (a) lock start: prime -> trim -> arm --------------------------------------------------------------------------

def test_lock_start_trims_the_pzt_into_the_acquisition_tolerance_before_the_arm():
    plant, result = prepared()
    artifact = result['artifact']
    target = offset_target(artifact, .8)          # within the artifact tolerance (1 Hz), outside the acquisition tolerance
    lock = Lock(plant)
    p = steady_parameters(artifact, target_frequency_hz=target, acquisition_tolerance_hz=.1)
    p = archived_steady_parameters(dict(parameters={}), p)
    piezo_before = plant.piezo.value
    plant.events.clear()
    recovery = MappedStationaryRecovery(lock, p, **plant.arguments)
    recovery.prime()
    assert lock.calls == []                                           # the trim never arms by itself
    trims = events(plant, 'ecdl_rearm_trim')
    assert len(trims) == 1 and trims[0]['stage'] == 'lock start' and trims[0]['within_tolerance']
    assert abs(trims[0]['before_hz'] - (target - .8)) < 1e-6 and abs(trims[0]['after_hz'] - target) <= .1
    assert trims[0]['steps'] and all(s['axis'] == 'piezo' for s in trims[0]['steps'])
    assert abs(trims[0]['counts_moved'] - .8 / 50.) < 1e-6 and trims[0]['tec_moved'] == 0.
    assert abs(plant.piezo.value - piezo_before - .8 / 50.) < 1e-6
    assert abs(plant.frequency() - target) <= .1
    # The current card no longer silently invokes the historical trim algorithm.
    with pytest.raises(ValueError,match='Legacy recovery parameters retired'):
        ecdl_steady.parameters({'parameters':{}},p)


def test_a_disabled_trim_and_an_already_centred_start_do_not_move_anything():
    plant, result = prepared()
    artifact = result['artifact']
    lock = Lock(plant)
    commands = list(plant.commands)
    p = archived_steady_parameters(dict(parameters={}), steady_parameters(artifact, target_frequency_hz=offset_target(artifact, .8),
                                                                      acquisition_tolerance_hz=.1, rearm_trim=dict(enabled=False)))
    plant.events.clear()
    MappedStationaryRecovery(lock, p, **plant.arguments).prime()
    trim = events(plant, 'ecdl_rearm_trim')[0]
    assert trim['reason'] == 'disabled' and trim['steps'] == [] and plant.commands == commands
    p = archived_steady_parameters(dict(parameters={}), steady_parameters(artifact, acquisition_tolerance_hz=.1))
    plant.events.clear()
    MappedStationaryRecovery(lock, p, **plant.arguments).prime()
    trim = events(plant, 'ecdl_rearm_trim')[0]
    assert trim['reason'] == 'already within tolerance' and plant.commands == commands


# --- (b) in-lock recovery -> trim -> re-arm -------------------------------------------------------------------------

def test_recovery_then_trim_then_rearm_with_the_laser_profile_slope():
    # restore-mode artifact: no piezo_hz_per_count in the artifact, the slope comes from the laser profile; the
    # recovery puts the PZT back to the map context, so every re-arm needs the trim again.
    plant, result = prepared(context_mode='restore', piezo_hz_per_count=None)
    artifact = result['artifact']
    assert artifact['piezo_hz_per_count'] is None
    target = offset_target(artifact, .8)
    lock = Lock(plant)
    p = archived_steady_parameters(dict(parameters={}), steady_parameters(artifact, target_frequency_hz=target, acquisition_tolerance_hz=.1))
    recovery = MappedStationaryRecovery(lock, p, laser=laser_with_piezo_slope(50., target), **plant.arguments)
    assert abs(recovery.trim_slope - 50.) < 1e-6
    recovery.prime()
    lock.arm(target, .1, 'hold')
    recovery.armed = True
    plant.events.clear()
    plant.current.value = plant.hop_center + .03                     # a hop, confirmed by 3 fresh samples
    assert not recovery.probe() and not recovery.probe() and recovery.probe()
    recovery.recover('confirmed original-mode frequency/identity loss')
    kinds = [k for k, _ in plant.events]
    assert kinds.index('ecdl_lock_held') < kinds.index('ecdl_rearm_trim') < kinds.index('ecdl_mapped_recovery_verified')
    trim = events(plant, 'ecdl_rearm_trim')[-1]
    assert trim['stage'] == 're-arm after recovery' and trim['within_tolerance'] and trim['steps'][0]['axis'] == 'piezo'
    assert abs(trim['before_hz'] - artifact['selected_reference']['frequency_hz']) <= 1. and abs(trim['after_hz'] - target) <= .1
    assert [c[0] for c in lock.calls] == ['arm', 'hold', 'check_ready', 'arm'] and abs(plant.frequency() - target) <= .1
    verified = events(plant, 'ecdl_mapped_recovery_verified')[-1]
    assert verified['rearm_trim']['within_tolerance'] and verified['recoveries'] == 1
    assert recovery.trim_summary()['count'] == 2 and recovery.trim_summary()['branch_losses'] == 0


def test_translate_mode_recovery_keeps_the_trimmed_pzt_and_needs_no_second_trim():
    plant, result = prepared()
    artifact = result['artifact']
    target = offset_target(artifact, .8)
    lock = Lock(plant)
    p = archived_steady_parameters(dict(parameters={}), steady_parameters(artifact, target_frequency_hz=target, acquisition_tolerance_hz=.1))
    recovery = MappedStationaryRecovery(lock, p, **plant.arguments)
    recovery.prime()
    recovery.armed = True
    plant.current.value = plant.hop_center + .03
    assert not recovery.probe() and not recovery.probe() and recovery.probe()
    recovery.recover('confirmed original-mode frequency/identity loss')
    trim = events(plant, 'ecdl_rearm_trim')[-1]
    assert trim['stage'] == 're-arm after recovery' and trim['reason'] == 'already within tolerance' and trim['steps'] == []
    assert abs(plant.frequency() - target) <= .1


# --- PZT limit -> TEC fallback ---------------------------------------------------------------------------------------

def test_pzt_at_the_owner_interval_falls_back_to_a_tec_step_and_waits_for_the_thermal_settle():
    from dataclasses import replace
    plant = Plant()
    piezo_high = plant.piezo.value + .005                           # 0.005 counts of room, the trim needs 0.016
    plant.piezo.policy = replace(plant.piezo.policy, high=piezo_high)
    _, result = prepared(plant)
    artifact = result['artifact']
    target = offset_target(artifact, .8)
    lock = Lock(plant)
    p = archived_steady_parameters(dict(parameters={}), steady_parameters(artifact, target_frequency_hz=target, acquisition_tolerance_hz=.1,
                                                                      rearm_trim=dict(tec_settle_s=45.)))
    recovery = MappedStationaryRecovery(lock, p, **plant.arguments)
    tec_before, piezo_before, t0 = plant.temperature.value, plant.piezo.value, plant.now
    plant.events.clear()
    recovery.prime()
    trim = events(plant, 'ecdl_rearm_trim')[0]
    assert trim['within_tolerance'] and trim['steps'][0]['axis'] == 'temperature'
    assert trim['steps'][0]['pzt_destination_refused'] > piezo_high and trim['steps'][0]['settle_s'] == 45.
    assert abs(trim['tec_moved'] - .8 / 200.) < 1e-6 and trim['counts_moved'] == 0.
    assert abs(plant.temperature.value - tec_before - .8 / 200.) < 1e-6 and plant.piezo.value == piezo_before
    assert plant.now - t0 >= 45.                                   # the thermal settle before the re-read
    assert abs(plant.frequency() - target) <= .1
    # neither axis can reach the target: refused before any write
    plant2 = Plant()
    plant2.piezo.policy = replace(plant2.piezo.policy, high=plant2.piezo.value + .005)
    plant2.temperature.policy = replace(plant2.temperature.policy, high=plant2.temperature.value + .001)
    _, result2 = prepared(plant2)
    artifact2 = result2['artifact']
    p2 = archived_steady_parameters(dict(parameters={}), steady_parameters(artifact2, target_frequency_hz=offset_target(artifact2, .8), acquisition_tolerance_hz=.1))
    commands = list(plant2.commands)
    with pytest.raises(DeviceError, match='neither the PZT .* nor the TEC'):
        MappedStationaryRecovery(Lock(plant2), p2, **plant2.arguments).prime()
    assert plant2.commands == commands


def test_trim_stops_at_max_steps_and_max_counts_and_reports_it():
    plant, result = prepared()
    artifact = result['artifact']
    target = offset_target(artifact, .8)
    experiment = BoundedExperiment(plant.ports, dict(timeout_s=60., measurement_timeout_s=5., sample_interval_s=.1, max_sample_age_s=1.), **plant.arguments)
    record = trim_to_target(experiment, target_hz=target, tolerance_hz=.1, hop_threshold_hz=50., piezo_hz_per_count=50.,
                            settings=dict(max_counts=.004, max_steps=4, tec_settle_s=0.), emit=plant.emit, stage='test')
    assert not record['within_tolerance'] and record['reason'] == 'max_counts reached'
    assert abs(record['counts_moved'] - .004) < 1e-9 and len(record['steps']) == 1
    record = trim_to_target(experiment, target_hz=target, tolerance_hz=.1, hop_threshold_hz=50., piezo_hz_per_count=100.,
                            settings=dict(max_counts=10., max_steps=1, tec_settle_s=0.), emit=plant.emit, stage='test')
    assert record['reason'] == 'max_steps reached' and len(record['steps']) == 1   # a wrong slope needs a second step
    record = trim_to_target(experiment, target_hz=target, tolerance_hz=.1, hop_threshold_hz=50., piezo_hz_per_count=None,
                            settings=None, emit=plant.emit, stage='test')
    assert record['reason'].startswith('piezo_hz_per_count unavailable') and record['steps'] == []


# --- hop during the trim -> recovery again (bounded) -------------------------------------------------------------------

class HoppingPlant(Plant):
    """The branch is lost when the PZT rises above ``piezo_hop_above`` (once, or every time)."""
    def __init__(self, *, once):
        super().__init__()
        self.piezo_hop_above, self.once, self.hops = None, once, 0

    def frequency(self):
        f = super().frequency()
        if self.piezo_hop_above is not None and self.piezo.value > self.piezo_hop_above:
            if not self.once or self.hops == 0:
                self.hops += 1
                return f + 300.
        return f


def test_a_hop_during_the_trim_runs_the_tested_recovery_again_and_counts_it():
    plant = HoppingPlant(once=True)
    _, result = prepared(plant, context_mode='restore')            # restore puts the PZT back below the hop edge
    artifact = result['artifact']
    target = offset_target(artifact, .8)
    plant.piezo_hop_above = plant.piezo.value + .01
    lock = Lock(plant)
    p = archived_steady_parameters(dict(parameters={}), steady_parameters(artifact, target_frequency_hz=target, acquisition_tolerance_hz=.1))
    recovery = MappedStationaryRecovery(lock, p, laser=laser_with_piezo_slope(50., target), **plant.arguments)
    plant.events.clear()
    recovery.prime()
    hops = events(plant, 'ecdl_rearm_trim_hop')
    assert len(hops) == 1 and hops[0]['stage'] == 'lock start' and 'did not follow the piezo step' in hops[0]['reason']
    assert recovery.recoveries == 1 and recovery.trim_hops == 1
    trims = events(plant, 'ecdl_rearm_trim')
    assert trims[0]['reason'] == 'branch lost' and trims[-1]['within_tolerance']
    assert abs(plant.frequency() - target) <= .1 and lock.calls == []
    assert recovery.trim_summary() == dict(recovery.trim_summary(), count=2, branch_losses=1)
    # bounded: a branch that is lost at every trim exhausts mapped_recovery_max_attempts
    plant = HoppingPlant(once=False)
    _, result = prepared(plant, context_mode='restore')
    artifact = result['artifact']
    target = offset_target(artifact, .8)
    plant.piezo_hop_above = plant.piezo.value + .01
    p = archived_steady_parameters(dict(parameters={}), steady_parameters(artifact, target_frequency_hz=target, acquisition_tolerance_hz=.1,
                                                                      mapped_recovery_max_attempts=2))
    recovery = MappedStationaryRecovery(Lock(plant), p, laser=laser_with_piezo_slope(50., target), **plant.arguments)
    plant.events.clear()
    with pytest.raises(DeviceError, match='attempt budget exhausted'):
        recovery.prime()
    assert recovery.recoveries == 3 and len(events(plant, 'ecdl_rearm_trim_hop')) == 3
