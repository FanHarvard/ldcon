"""Rule K (2026-10-27): quality/classification labels are reported, never a veto.

Gate review §2.1 rows A (interface_cards, learned_settling), C (steady owner window/study dither), E2 (PI
commissioning), E3 (window-centre spectral degradation) and §2.2 ecdl_mode_map:450. Each test proves the action
proceeds and the finding is in the result/records; the (iii) data-validity refusals next to them are kept.
"""
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
import yaml

from ldcon.core.contracts import DeviceError, FrequencySample

ROOT = (Path(__file__).resolve().parent / 'support')
sys.path.insert(0, str(ROOT/'owner/tests'))
from test_locked_sweep_owner import rig  # noqa: E402,F401  (fixture: armed lock over the simulated sr422 rig)


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT/relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- A: generic wavelength/temperature cards (interface_cards.py) --------------------------------------------------

class Wave:
    def __init__(self, clock, quality='invalid', stale=False):
        self.clock, self.quality, self.stale, self.seq = clock, quality, stale, 0

    def read_frequency(self):
        now = self.clock(); self.seq += 1
        return FrequencySample(self.seq, 710e12, now-(5. if self.stale else 0.), now, self.quality, 'fixture')


class Temperature:
    conditions = SimpleNamespace(low=10., high=40., step=.5, check=lambda *a: None)
    frequency_hz_per_c = -1e10

    def __init__(self, clock): self.clock = clock

    def read_temperature(self):
        from ldcon.core.interface_cards import TemperatureReading
        return TemperatureReading(25., 25., self.clock(), True, True)

    def ramp_temperature(self, value, duration): raise AssertionError('no write expected')


P = dict(frequency_hz_per_c=None, target_frequency_hz=710e12, target_wavelength_nm=None, identify_slope=True, gain=.25,
         settle_tolerance_c=.001, stable_samples=1, sample_interval_s=.5, max_sample_age_s=1., measurement_recovery_timeout_s=15.,
         measurement_recovery_samples=3, max_initial_detuning_hz=1e12, timeout_s=600., max_excursion_c=None, tolerance_hz=1e6,
         requested_slew_c_per_s=.1, approach_time_s=10.)


def test_sample_trace_validity_is_finite_hz_and_quality_is_a_column():
    from ldcon.core.interface_cards import sample_trace
    row = sample_trace(FrequencySample(1, 710e12, 9., 9., 'invalid', 'f'), 9.5, 1.)
    assert row['sample_valid'] and row['control_valid'] and row['quality_flagged'] and row['sample_quality'] == 'invalid'
    assert not sample_trace(FrequencySample(2, None, 9., 9., 'invalid', 'f'), 9.5, 1.)['sample_valid']
    assert not sample_trace(FrequencySample(3, 710e12, 5., 5., 'valid', 'f'), 9.5, 1.)['control_valid']


def test_temperature_card_uses_a_quality_flagged_reading_and_reports_it():
    from ldcon.core.interface_cards import acquire_temperature
    now = [1000.]; clock = lambda: now[0]; events = []
    result = acquire_temperature(Wave(clock), Temperature(clock), dict(P), clock=clock, sleep=lambda s: now.__setitem__(0, now[0]+s),
                                 cancel=lambda: False, emit=lambda kind, value: events.append((kind, value)))
    assert result['status'] == 'succeeded' and result['output_writes'] == 0 and result['quality_flagged_samples'] == 1
    quality = [v for k, v in events if k == 'measurement_quality']
    assert quality and quality[0]['stage'] == 'target acquisition' and quality[0]['sample_quality'] == 'invalid'
    assert not [k for k, _ in events if k == 'measurement_rejected']


def test_temperature_card_still_refuses_a_stale_reading():
    from ldcon.core.interface_cards import acquire_temperature
    now = [1000.]; clock = lambda: now[0]
    with pytest.raises(DeviceError, match='Measurement recovery timed out'):
        acquire_temperature(Wave(clock, quality='valid', stale=True), Temperature(clock), dict(P), clock=clock,
                            sleep=lambda s: now.__setitem__(0, now[0]+s), cancel=lambda: False, emit=lambda *a: None)


# --- A: learned settling profiles (learned_settling.py) ------------------------------------------------------------

def test_learned_settling_counts_quality_labels_and_still_validates():
    from ldcon.jobs.learned_settling import fit, evaluate
    fixture = load('empirical_fixture', 'tests/test_learned_settling.py')
    records = fixture.trials(); history = records[3]['history']
    history['samples'][200]['quality_valid'] = False
    history['samples'][201]['quality'] = 512
    history['quality_valid'] = False
    profile = fit(records, fixture.CONTEXT)
    assert profile['status'] == 'validated'
    assert profile['quality_flags']['flagged_samples'] == 2
    assert profile['quality_flags']['trials'][3] == dict(trial_index=3, flagged_samples=2, history_quality_valid=False)
    leg = records[3]
    result = evaluate(profile, history, leg['target_frequency_hz'], leg['confirmed_unix_s'], leg['record_end_unix_s'])
    assert result['quality_flags'] == dict(flagged_samples=2, history_quality_valid=False)
    history['integrity_errors'] = ['conflicting_sequence']
    assert fit(records, fixture.CONTEXT)['reason'] == 'history_integrity_error'


# --- C: steady owner window traversal without a verified dither (ecdl_steady_owner.py) ------------------------------

def test_hidden_window_strategy_is_rejected_and_missing_dither_keeps_hold_lock(rig, monkeypatch):
    owner, hw, now, call, tmp_path = rig
    from ldcon.owners.ecdl_steady_owner import NativeLockOwner
    monkeypatch.setattr(NativeLockOwner, 'passive_dither', lambda self: pytest.fail('no telemetry fit without a verified dither'))
    from ldcon.core.instrument_client import BrokerError
    with pytest.raises(BrokerError,match='window traversal is retired'):
        call('steady_hot_control',command=dict(action='set_strategy',strategy='window'))
    assert owner.steady.strategy=='hold'
    owner.steady.p['dither']['verified'] = False      # the runtime profile no longer carries a verified dither
    now[0] += 10.5
    row = call('steady_read')                          # before rule K: DeviceError -> BrokerError -> lock hold
    assert row['state']=='locked'
    assert row['dither_analysis_reason']=='Verified passive dither is not configured'
    assert row['dither_analysis_status'] == 'not_configured'
    assert hw.firmware.cfg['pid_multiplier'] == 1 and owner.fault is None and owner.steady.active
    audit = [json.loads(line) for line in (tmp_path/'audit.jsonl').read_text().splitlines()]
    assert not [e for e in audit if e['kind']=='stationary_dither_unavailable']
    assert audit[-1]['kind']=='stationary_supervision'


# --- E2: PI commissioning single-mode loss (ecdl_pi_commission.py) ---------------------------------------------------

def test_pi_commissioning_records_single_mode_loss_and_continues():
    from ldcon.jobs.ecdl_pi_commission import Trial
    profile = yaml.safe_load((ROOT/'jobs/tests/fixtures/live-20261022/lasers/sr422.yaml').read_text())['devices']['rig']['control_settings']['steady_lock']
    target = 710962838507871.5
    t = Trial(profile, target, dict(piezo=31753, current=.6949, temperature=.4725), lambda: 0.)
    def row(i, **labels):
        return dict(dict(outputs=dict(piezo=31758, current=.6949, temperature=.4725), frequency_hz=target, single_mode=True, acquired_s=float(i)), **labels)
    t.add(row(0, single_mode=False), 5e6)              # before rule K: DeviceError('Single mode lost during PI commissioning')
    t.add(row(1, maintenance_required=True), 5e6)
    for i in range(2, 34): t.add(row(i), 5e6)
    status = t.status()
    assert status['ready'] and status['valid_samples'] == 34
    assert status['quality']['single_mode_verified'] is False and status['quality']['degraded_samples'] == 2
    assert status['quality']['first'] == dict(stage=0, acquired_s=0., single_mode=False, maintenance_required=False)
    assert status['quality']['last']['maintenance_required'] is True
    assert len(t.schedule) > 1 and t.raise_bound() == t.schedule[1]


# --- E3: window-centre search under spectral degradation (window_center.py, ecdl_window_center.py) ------------------

class DegradedLock:
    """SimulatedLock whose spectrum degrades (side peak high, single mode lost, no dither fit) while ``degraded``."""
    def __init__(self):
        from ldcon.jobs.simulation import SimulatedLock
        self.inner = SimulatedLock(); self.degraded = False
    def __getattr__(self, name): return getattr(self.inner, name)
    def response_info(self):
        row = self.inner.response_info()
        row['observation'].update(single_mode=not self.degraded, metrics={'side_peak_height_max': .9 if self.degraded else .1})
        return row
    def response_dither_info(self):
        info = self.inner.response_dither_info()
        if self.degraded: info.update(dither_response=None, dither_response_valid=False)
        return info


@pytest.mark.parametrize('require_single_mode', [False, True])
def test_window_search_reports_spectral_degradation_and_finishes_without_recovery(require_single_mode):
    from ldcon.jobs.ecdl_window_center import WindowCenterSession
    lock = DegradedLock(); events = []
    session = WindowCenterSession({'request_id': 'midpoint', 'card_id': 'window'}, {
        'step_fraction': .02, 'max_span_fraction': .4, 'max_steps': 100, 'timeout_s': 180., 'settle_s': .1,
        'observables': ['dither_response', 'metrics.side_peak_height_max'], 'boundary_limits': {'dither_response': 1.},
        'urgent_limits': {'metrics.side_peak_height_max': .5}, 'require_single_mode': require_single_mode},
        lock, {}, lambda kind, payload: events.append((kind, payload)), lambda value: None, {},
        clock=lock.clock, wall_clock=lock.wall_clock)
    for _ in range(250):
        lock.inner.t += 1.5
        lock.degraded = len(lock.moves) >= 3 and not session.result.get('degraded')   # transient, clean again for the return
        session.poll(lock)
        if session.status != 'running' or session.needs_recovery: break
    assert not session.needs_recovery and session.recovery_reason is None      # before rule K: needs_recovery -> parent mode-hop recovery
    assert session.status == 'failed' and lock.pi_active and len(lock.moves) >= 3
    assert session.result['state'] == 'unresolved' and session.result['degraded'] is True
    assert session.result['warning'] == 'spectral degradation with unavailable auxiliary feature'
    assert session.result['return_outcome']['status'] == 'verified'
    kinds = [kind for kind, _ in events]
    assert ('window_clean_point_degraded' in kinds) == require_single_mode and 'window_clean_point_recovery_requested' not in kinds
    assert kinds[-1] == 'window_center_end' and events[-1][1]['degraded'] is True


# --- §2.2: mode map with an unqualified explicit branch selection (ecdl_mode_map.py:450) ----------------------------

def test_unqualified_branch_selection_is_recorded_and_the_map_card_succeeds():
    from ldcon.jobs.ecdl_mode_map import run_mode_map, parameters, selected_reference
    fixture = load('ecdl_gate_fixes_fixture', 'tests/test_ecdl_acquisition_gate_fixes.py')
    settings = dict(target_frequency_hz=1000., current_start_fraction=.4, current_end_fraction=.6, current_step_fraction=.05,
                    discontinuity_hz=100., capture_hz=50., dwell_s=.05, sample_interval_s=.1, timeout_s=300., selection_author='human')
    plant = fixture.Plant()
    result = run_mode_map(plant.ports, parameters(dict(parameters={}), dict(settings, selected_branch_id='branch-9')), **plant.arguments)
    assert result['status'] == 'succeeded' and result['selection_required'] and result['selection_qualified'] is False
    assert result['unqualified'] == dict(branch_id='branch-9', reason='absent from the map')
    artifact = result['artifact']
    assert artifact['status'] == 'complete' and artifact['ranked_branch_ids'] == ['branch-1']
    assert artifact['selection'] == dict(branch_id='branch-9', author='human', qualified=False, unqualified=result['unqualified'],
                                         qualification='source-qualified phase-free pattern; not physical mode proof')
    with pytest.raises(ValueError, match='qualified mapped branch'): selected_reference(artifact)   # the plan side still refuses
    emitted = [v for k, v in plant.events if k == 'ecdl_mode_map_artifact']
    assert emitted[-1]['selection_qualified'] is False and emitted[-1]['unqualified'] == result['unqualified']
    plant = fixture.Plant()
    result = run_mode_map(plant.ports, parameters(dict(parameters={}), dict(settings, target_frequency_hz=5000., selected_branch_id='branch-1')), **plant.arguments)
    assert result['status'] == 'succeeded' and result['selected_branch_id'] == 'branch-1'
    assert result['unqualified'] == dict(branch_id='branch-1', reason='not optically qualified') and result['artifact']['ranked_branch_ids'] == []
    with pytest.raises(ValueError, match='qualified mapped branch'): selected_reference(result['artifact'])
    plant = fixture.Plant()
    result = run_mode_map(plant.ports, parameters(dict(parameters={}), dict(settings, selected_branch_id='branch-1')), **plant.arguments)
    assert result['selection_qualified'] and result['unqualified'] is None and not result['selection_required']
    assert selected_reference(result['artifact'])[1]['id'] == 'branch-1'
