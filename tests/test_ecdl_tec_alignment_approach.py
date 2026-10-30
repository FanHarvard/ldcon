"""tune.ecdl_operating_point (deck card tec-branch-alignment) after the human decision of 2026-10-27 (evening):
a plain TEC + LDC feed-forward approach loop with the tested recovery, three consecutive recovery failures fail the card.

Hardware-free plant in the style of tests/test_ecdl_recovery_card.py. Toy units: frequency 1000 + 100 dI + 200 dT + 50 dP;
the clean single-mode current window (half width .008) drifts with the TEC by -0.07 per TEC unit (the sr422
measurement of 2026-10-27, +0.007 V current per -0.1 V TEC); outside the window the laser hops (+300, other mode).
"""
import socket
import subprocess

import pytest

from ldcon.core.contracts import DeviceError, FrequencySample
from ldcon.jobs import ecdl_recovery
from ldcon.jobs.ecdl_mode_map import run_mode_map, parameters as map_parameters, selected_reference
from ldcon.jobs.ecdl_operating_point import DEFAULTS, PZT_TRIM_DEFAULTS, parameters, run_operating_point
from ldcon.jobs.ecdl_recovery import run_recovery_card
from ldcon.owners.ecdl_prelock import AxisPolicy, ModeObservation


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
        self.policy = AxisPolicy('test-unit', 0., 1., .02, .08, .25, .05, slope, name, 'calibration-1', resolution=.00001)
        self.uncertain_output = False

    def readback(self):
        return self.value

    def move(self, value, elapsed):
        assert self.plant.open_loop
        assert self.policy.low <= value <= self.policy.high
        assert abs(value - self.value) <= self.policy.max_step + 1e-9
        assert abs(value - self.value) <= self.policy.max_slew_per_s * elapsed + 1e-9
        self.plant.commands.append(dict(axis=self.name, before=self.value, value=value, at=self.plant.now))
        self.value = value

    def begin_recovery(self):
        self.plant.recovery_flags.append(True)

    def end_recovery(self):
        self.plant.recovery_flags.append(False)


class Plant:
    drift = -.07
    half_width = .008

    def __init__(self):
        self.now = 0.
        self.open_loop = True
        self.commands, self.events, self.recovery_flags = [], [], []
        self.current = Axis(self, 'current', .5, 100.)
        self.temperature = Axis(self, 'temperature', .48, 200.)
        self.piezo = Axis(self, 'piezo', .46, 50.)
        self.reference = None          # (current, temperature) anchoring the drifting window; set after the map
        self.sticky = False            # a hopped laser stays hopped inside the window (recovery cannot verify)
        self.hopped = False
        self.degraded = False
        self.cancelled = False
        self.on_emit = None

    @property
    def ports(self):
        return dict(current=self.current, temperature=self.temperature, piezo=self.piezo, observation=self, loop=self)

    @property
    def arguments(self):
        return dict(clock=lambda: self.now, sleep=self.sleep, cancel=lambda: self.cancelled, emit=self.emit)

    def sleep(self, duration):
        assert duration >= 0
        self.now += duration

    def emit(self, kind, value):
        self.events.append((kind, value))
        if self.on_emit:
            self.on_emit(kind, value)

    def is_open_loop(self):
        return self.open_loop

    def window_centre(self):
        return self.reference[0] + self.drift * (self.temperature.value - self.reference[1])

    def frequency(self):
        f = (1000. + 100. * (self.current.value - .5) + 200. * (self.temperature.value - .5)
             + 50. * (self.piezo.value - .5))
        if self.reference is not None:
            if abs(self.current.value - self.window_centre()) > self.half_width:
                self.hopped = True
            elif not self.sticky:
                self.hopped = False
            if self.hopped:
                f += 300.
        return f

    def read_mode(self):
        self.now += .001
        f = self.frequency()
        clean = not self.hopped and not self.degraded
        side = .05 if clean else .6
        metrics = dict(side_peak_prominence_max=side, side_peak_height_max=side, main_fwhm_fraction=.08,
                       near_main_peak_prominence_max=.02)
        analysis = dict(qualified=True, period_qualified=True, revision='full-period-free-position-test',
                        period_reference_px=16., classification_revision='classifier-1',
                        raw_patterns={'fizeau': [0., 1., 0., 0.]})
        sample = FrequencySample(int(self.now * 100000), f, self.now, self.now, 'valid', 'synthetic')
        return ModeObservation(sample, clean, 'other' if self.hopped else 'desired', True, .01,
                               metrics=metrics, fringe_analysis=analysis)

    def prepared(self, **overrides):
        """Qualify the v2 relative route with current observations; no branch map."""
        self.reference = (.5, .48)
        self.now = 12.
        from ldcon.jobs.recovery_procedure import parameters as recovery_parameters
        history = [dict(acquired_s=float(i), frequency_hz=self.frequency(), operation_epoch='a',
                        outputs=dict(current=.5,temperature=.48,piezo=.46)) for i in range(12)]
        settings = dict(reference=dict(lookback_s=30.,stable_s=.3,min_samples=3,max_gap_s=1.5,
                            guard_s=.1,max_stddev_hz=3.,max_slope_hz_per_s=2.),
                        fallback=[dict(offset=-.03,dwell_s=.1),dict(offset=0.,dwell_s=.1)],
                        history=history,event_time_s=12.,operation_epoch='a',execution='prepare',
                        execution_mode='deterministic',max_sample_age_s=2.,
                        tolerance_hz=5.,hop_threshold_hz=50.,stable_s=.2,confirm_samples=3,
                        measurement_timeout_s=5.,timeout_s=600.,sample_interval_s=.1,
                        perturbations=[.03,-.03])
        settings.update(overrides)
        result = run_recovery_card(self.ports, settings, **self.arguments)
        assert result['recovery_verified'] is True
        self.commands, self.events, self.recovery_flags = [], [], []
        return result['artifact']

    def align(self, artifact, **overrides):
        settings = dict(recovery_artifact=artifact, target_frequency_hz=960., tolerance_hz=.05, stable_s=.5,
                        ldc_ff=-.07, tec_settle_s=1., sample_interval_s=.1, measurement_timeout_s=5., timeout_s=7200.)
        settings.update(overrides)
        return run_operating_point(self.ports, parameters(dict(parameters={}), settings), **self.arguments)

    def kinds(self):
        return [kind for kind, _ in self.events]

    def of(self, kind):
        return [v for k, v in self.events if k == kind]


def commands(plant, axis):
    return [c for c in plant.commands if c['axis'] == axis]


# --- convergence with TEC + LDC feed-forward ------------------------------------------------------------------------

def test_converges_with_tec_and_ldc_feed_forward_and_trims_the_residual_with_the_pzt():
    plant = Plant()
    artifact = plant.prepared()
    result = plant.align(artifact)
    assert result['status'] == 'succeeded' and abs(result['final_error_hz']) <= .05 and result['lock_enabled'] is False
    assert result['final_frequency_hz'] == pytest.approx(960., abs=.05)
    assert result['hops'] == 0 and result['recoveries'] == 0 and result['recovery_failures'] == 0
    assert result['single_mode'] is True and result['spectral_quality_passed'] is None
    # the current followed the window: I = I0 + ldc_ff x dTEC, with the artifact's drift prior (-0.07) by default
    dtec = result['outputs']['temperature'] - result['start_outputs']['temperature']
    assert dtec < -.15 and result['outputs']['current'] == pytest.approx(.5 + (-.07) * dtec, abs=2e-4)
    assert abs(result['outputs']['current'] - plant.window_centre()) < plant.half_width
    k = result['coefficients']
    assert (k['tec_ff_hz_per_v'], k['tec_ff_source']) == (200., 'temperature.frequency_hz_per_unit')
    assert (k['ldc_ff_per_v'], k['ldc_ff_source']) == (-.07, 'parameter')
    assert (k['hop_threshold_hz'], k['hop_threshold_source']) == (50., 'recovery parameters')
    assert (k['counts_per_hz'], k['counts_per_hz_source'], k['piezo_hz_per_count']) == (pytest.approx(1 / 50.), 'piezo.frequency_hz_per_unit', 50.)
    # every iteration: one bounded TEC move (owner steps of .02 at most) and the LDC feed-forward in the same iteration
    steps = plant.of('ecdl_alignment_step')
    assert len(steps) == result['iterations'] >= 5
    assert all(s['tec_commands'] >= 1 and s['current_commands'] == 1 and not s['current_limited'] for s in steps)
    assert all(s['current_delta'] == pytest.approx(-.07 * s['delivered_delta_v'], abs=2e-5) for s in steps)
    assert all(abs(s['delivered_delta_v']) <= abs(s['requested_delta_v']) + .00001 for s in steps)   # axis resolution
    started = plant.of('ecdl_alignment_start')[0]
    assert started['frequency_hz'] == pytest.approx(994.) and started['error_hz'] == pytest.approx(-34.)   # f(.5, .48, .46)
    assert steps[0]['requested_delta_v'] == pytest.approx(.5 * -34. / 200.)                  # approach_gain x error / tec_ff
    assert max(abs(c['value'] - c['before']) for c in commands(plant, 'temperature')) == pytest.approx(.02)
    assert all(abs(s['error_hz']) > .05 for s in steps) and abs(steps[-1]['error_hz']) < abs(steps[0]['error_hz'])
    # the final bounded PZT trim of the residual, then the stability window again; headroom reported in counts and Hz
    trim = result['pzt_trim']
    assert trim['enabled'] and trim['performed'] and abs(trim['counts']) <= PZT_TRIM_DEFAULTS['max_counts']
    assert trim['counts'] == pytest.approx(trim['residual_hz'] / 50., abs=1e-5) and abs(trim['error_after_hz']) < abs(trim['residual_hz'])
    assert len(commands(plant, 'piezo')) >= 1 and plant.kinds().count('ecdl_alignment_pzt_trim') == 1
    assert result['pzt_headroom'] == dict(counts_low=pytest.approx(plant.piezo.value), counts_high=pytest.approx(1. - plant.piezo.value),
                                          hz_low=pytest.approx(50. * plant.piezo.value), hz_high=pytest.approx(50. * (1. - plant.piezo.value)))
    # the PZT trim counts toward the continuity check (piezo slope from the artifact)
    after = [r for r in result['trace'] if r['stage'] == 'after PZT trim'][0]
    assert after['continuity']['predicted_change_hz'] == pytest.approx(50. * trim['counts'], abs=1e-6) and not after['continuity']['hop']
    # evidence, quality and timing
    assert result['recovery_algorithm'] == artifact['algorithm'] and result['recovery_sha256'] == artifact['artifact_sha256']
    assert all({'continuity', 'optical_quality', 'error_hz'} <= set(r) for r in result['trace'])
    assert result['trace'][0]['stage'] == 'alignment start' and result['trace'][0]['continuity']['reference_stage'] == 'fresh alignment observation'
    assert result['optical_quality']['single_mode'] is True and result['physical_mode_identity_verified'] is False
    timing = result['timing']
    assert timing['iterations'] == result['iterations'] and timing['tec_commands'] == len(commands(plant, 'temperature'))
    assert timing['current_commands'] == len(commands(plant, 'current')) and timing['piezo_commands'] == len(commands(plant, 'piezo'))
    assert timing['command_count'] == len(plant.commands) and timing['settle_s'] == pytest.approx(result['iterations'] * 1.)
    assert timing['tec_distance_v'] == pytest.approx(abs(dtec)) and timing['total_s'] > timing['settle_s'] and timing['recoveries'] == 0
    assert plant.kinds()[0] == 'ecdl_map_observation' and 'ecdl_alignment_start' in plant.kinds() and plant.kinds()[-1] == 'ecdl_operating_point'
    assert started['required_tec_delta_v'] == pytest.approx(-.17)
    assert 'ecdl_alignment_hop' not in plant.kinds() and not plant.recovery_flags          # no recovery, no current-recovery state


def test_hop_after_tec_step_restores_recent_context_then_uses_shared_v2_route(monkeypatch):
    plant=Plant();artifact=plant.prepared()
    # One spontaneous departure at a settled point; the route then restores it.
    fired=[False]
    def disturbance(kind,value):
        if kind=='ecdl_alignment_step' and not fired[0]:
            plant.hopped=True;plant.sticky=True;fired[0]=True
        if kind=='recovery_reference_fixed':plant.sticky=False
    plant.on_emit=disturbance
    result=plant.align(artifact)
    assert result['status']=='succeeded' and result['hops']==1 and result['recoveries']==1
    from ldcon.jobs import ecdl_operating_point, recovery_procedure
    assert ecdl_operating_point.execute is recovery_procedure.execute
    event=plant.of('ecdl_alignment_recovered')[0]['recovery']
    assert event['algorithm']=='relative_current_route.v1'
    assert event['reference']['operation_epoch']=='960.0'
    assert abs(event['measured_frequency_hz']-event['predicted_frequency_hz'])<=5.
    assert any(v.get('stage')=='restore recent alignment context' for k,v in plant.events if k=='command_applied')
    assert plant.recovery_flags[0] is True and plant.recovery_flags[-1] is False


def test_only_confirmed_frequency_failure_retries_and_unknown_writes_never_retry():
    plant=Plant();artifact=plant.prepared();fired=[False]
    def disturbance(kind,value):
        if kind=='ecdl_alignment_step' and not fired[0]:
            plant.sticky=True;plant.hopped=True;fired[0]=True
    plant.on_emit=disturbance
    with pytest.raises(DeviceError,match='3 consecutive recovery failures'):plant.align(artifact)
    assert [f['attempt'] for f in plant.of('ecdl_alignment_recovery_failed')]==[1,2,3]
    assert all('Frequency did not return' in f['reason'] for f in plant.of('ecdl_alignment_recovery_failed'))
    assert 'ecdl_operating_point' not in plant.kinds()
    # Every failed attempt returns to the recent stable TEC context, not the map.
    ref=plant.of('recovery_reference_fixed')[0]['reference']
    assert plant.temperature.value==pytest.approx(ref['outputs']['temperature'])
    plant=Plant();artifact=plant.prepared();original=plant.current.move
    def fault(value,elapsed):
        if plant.recovery_flags:raise RuntimeError('Unconfirmed hardware write')
        return original(value,elapsed)
    plant.current.move=fault
    def forced(kind,value):
        if kind=='ecdl_alignment_step':plant.sticky=True;plant.hopped=True
    plant.on_emit=forced
    with pytest.raises(RuntimeError,match='Unconfirmed'):plant.align(artifact)
    assert not plant.of('ecdl_alignment_recovery_failed')


def test_archived_branch_does_not_override_current_branch_at_start():
    plant=Plant();artifact=plant.prepared()
    # Preparation's old return frequency and output are evidence only. Current
    # point is a different, stable branch; alignment must not replay that context.
    plant.current.value=.53
    current=plant.frequency()
    result=plant.align(artifact,target_frequency_hz=current,pzt_trim=dict(enabled=False))
    assert result['status']=='succeeded' and result['hops']==0
    assert not plant.commands and result['final_frequency_hz']==pytest.approx(current)


def test_quality_is_reported_every_iteration_and_never_stops_the_card():
    plant = Plant()
    artifact = plant.prepared()
    plant.degraded = True
    result = plant.align(artifact)
    assert result['status'] == 'succeeded' and abs(result['final_error_hz']) <= .05
    assert result['single_mode'] is False and result['spectral_quality_passed'] is None
    assert result['optical_quality']['single_mode'] is False and result['optical_quality']['metrics']['side_peak_height_max'] == .6
    assert all(r['optical_quality']['single_mode'] is False for r in result['trace'])
    assert all(s['optical_quality']['single_mode'] is False for s in plant.of('ecdl_alignment_step'))
    assert result['hops'] == 0 and 'ecdl_alignment_hop' not in plant.kinds()


# --- limits ---------------------------------------------------------------------------------------------------------

def test_excursion_budget_refuses_an_unreachable_target_before_any_write_and_bounds_the_tec():
    plant = Plant()
    artifact = plant.prepared()
    with pytest.raises(DeviceError, match='beyond max_tec_excursion_v'):
        plant.align(artifact, max_tec_excursion_v=.05)                 # 960 needs -0.2 V
    assert plant.commands == [] and 'ecdl_alignment_start' not in plant.kinds()
    plant = Plant()
    artifact = plant.prepared()
    result = plant.align(artifact, max_tec_excursion_v=.2)             # exactly the required move: never exceeded
    assert result['status'] == 'succeeded'
    assert all(abs(c['value'] - .48) <= .2 + 1e-9 for c in commands(plant, 'temperature'))
    assert plant.of('ecdl_alignment_start')[0]['tec_range'] == [pytest.approx(.28), pytest.approx(.68)]
    # the target out of reach by the owner interval is refused the same way
    plant = Plant()
    artifact = plant.prepared()
    with pytest.raises(DeviceError, match='beyond max_tec_excursion_v'):
        plant.align(artifact, target_frequency_hz=1200., max_tec_excursion_v=5.)


def test_pzt_trim_limits_and_disabling():
    plant = Plant()
    artifact = plant.prepared()
    result = plant.align(artifact, pzt_trim=dict(max_counts=.0001))
    trim = result['pzt_trim']
    assert trim['performed'] and abs(trim['counts']) == pytest.approx(.0001, abs=2e-5) and abs(trim['requested_counts']) > .0001
    assert trim['max_counts'] == .0001 and result['status'] == 'succeeded'

    plant = Plant()
    artifact = plant.prepared()
    result = plant.align(artifact, pzt_trim=dict(enabled=False))
    assert result['pzt_trim'] == dict(enabled=False, performed=False) and not commands(plant, 'piezo')
    assert 'ecdl_alignment_pzt_trim' not in plant.kinds() and result['status'] == 'succeeded'
    assert result['pzt_headroom']['counts_low'] == pytest.approx(.46)

    plant = Plant()
    artifact = plant.prepared()
    result = plant.align(artifact, pzt_trim=dict(counts_per_hz=1 / 25.))   # an explicit (wrong by 2x) calibration
    assert result['coefficients']['counts_per_hz_source'] == 'parameter' and result['coefficients']['piezo_hz_per_count'] == 25.
    assert result['pzt_trim']['performed'] and result['status'] == 'succeeded'

    # an artifact without piezo_hz_per_count needs the explicit calibration when the trim is enabled
    plant = Plant()
    artifact = plant.prepared()
    from dataclasses import replace
    plant.piezo.policy=replace(plant.piezo.policy,frequency_hz_per_unit=None)
    with pytest.raises(ValueError, match='pzt_trim.counts_per_hz is required'):
        plant.align(artifact)
    assert plant.commands == []
    result = plant.align(artifact, pzt_trim=dict(enabled=False))
    assert result['status'] == 'succeeded' and result['pzt_headroom']['hz_low'] is None


# --- parameters -----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('bad', [
    dict(map_ref=dict(job_id='j', sha256='a' * 64)), dict(selected_branch_id='branch-1'), dict(replay_mode='auto'),
    dict(window_follow=dict(enabled=False)), dict(approach_settle_s=1.), dict(temperature_center_fraction=.5),
    dict(recovery_ref=dict(job_id='j')), dict(recovery_ref=dict(job_id='j', sha256='xyz')), dict(recovery_artifact=[]),
    dict(target_frequency_hz=0.), dict(target_frequency_hz=1., target_wavelength_nm=1.), dict(target_wavelength_nm=-1.),
    dict(tolerance_hz=0.), dict(stable_s=0.), dict(hop_threshold_hz=-1.),
    dict(tec_ff=0.), dict(tec_ff='x'), dict(ldc_ff=float('nan')),
    dict(approach_gain=0.), dict(approach_gain=1.5), dict(tec_settle_s=-1.), dict(tec_settle_s=3601.), dict(max_tec_excursion_v=0.),
    dict(pzt_trim=None), dict(pzt_trim=dict(unknown=1)), dict(pzt_trim=dict(enabled='yes')), dict(pzt_trim=dict(counts_per_hz=0.)),
    dict(pzt_trim=dict(max_counts=0.)),
    dict(max_consecutive_recovery_failures=0), dict(max_consecutive_recovery_failures=1.5), dict(max_consecutive_recovery_failures=101),
    dict(stable_s=3600., timeout_s=3600.), dict(tec_settle_s=3600., timeout_s=3600.), dict(timeout_s=86401.),
    dict(sample_interval_s=.01), dict(measurement_timeout_s=4000., timeout_s=3600.), dict(max_sample_age_s=200.),
])
def test_bad_parameters_are_refused(bad):
    with pytest.raises(ValueError):
        parameters(dict(parameters={}), bad)


def test_parameters_normalize_and_resolve_their_defaults():
    p = parameters(dict(parameters={}), {})
    assert p == dict(DEFAULTS, pzt_trim=PZT_TRIM_DEFAULTS)
    assert (p['tolerance_hz'], p['stable_s'], p['approach_gain'], p['tec_settle_s'], p['max_tec_excursion_v'],
            p['max_consecutive_recovery_failures']) == (100e6, 10., .5, 45., .3, 3)
    assert (p['tec_ff'], p['ldc_ff'], p['hop_threshold_hz'], p['recovery_ref'], p['recovery_artifact']) == (None, None, None, None, None)
    assert PZT_TRIM_DEFAULTS == dict(enabled=True, counts_per_hz=None, max_counts=15000.)
    assert parameters(dict(parameters={}), dict(pzt_trim=dict(max_counts=10.)))['pzt_trim'] == dict(PZT_TRIM_DEFAULTS, max_counts=10.)
    assert parameters(dict(parameters={}), p) == p                     # idempotent for the runtime validator
    nm = parameters(dict(parameters={}), dict(target_wavelength_nm=421.671066))
    assert nm['target_frequency_hz'] == pytest.approx(710962838507871.5, abs=1.)
    assert parameters(dict(parameters={}), dict(target_wavelength_nm=421.671066, target_frequency_hz=710962838507871.5))['target_frequency_hz'] == pytest.approx(710962838507871.5, abs=1.)
    ref = dict(job_id='j1', sha256='b' * 64)
    assert parameters(dict(parameters={}), dict(recovery_ref=ref, tec_ff=-9.4e10, ldc_ff=-.07))['recovery_ref'] == ref
    # card-level refusals before any device access
    plant = Plant()
    artifact = plant.prepared()
    with pytest.raises(ValueError, match='recovery_ref resolved into recovery_artifact'):
        plant.align(artifact, recovery_artifact=None)
    with pytest.raises(ValueError, match='was not resolved'):
        plant.align(artifact, recovery_artifact=None, recovery_ref=ref)
    with pytest.raises(ValueError, match='Target frequency/wavelength is required'):
        plant.align(artifact, target_frequency_hz=None)
    from ldcon.jobs.ecdl_mode_map import seal
    # An explicitly configured proposal route is usable; it never claims verified recovery.
    with pytest.raises(ValueError, match='hash'):
        plant.align(artifact, recovery_artifact=dict(artifact, branch_id='other'))
    assert plant.commands == []
    # explicit coefficients override the axis policy and the artifact
    result = plant.align(artifact, tec_ff=200., ldc_ff=-.07, hop_threshold_hz=40.)
    assert result['coefficients']['tec_ff_source'] == 'parameter' and result['coefficients']['ldc_ff_source'] == 'parameter'
    assert result['coefficients']['hop_threshold_source'] == 'parameter' and result['coefficients']['hop_threshold_hz'] == 40.
    assert result['status'] == 'succeeded'


def test_runtime_specs_expose_gain_settle_and_stability(tmp_path):
    from ldcon.jobs.card_runtime import CardRuntime, SUPPORTED
    assert 'tune.ecdl_operating_point' in SUPPORTED
    plant = Plant()
    artifact = plant.prepared()
    p = parameters(dict(parameters={}), dict(recovery_artifact=artifact, target_frequency_hz=960.))
    card = dict(id='tec-branch-alignment', operation='tune.ecdl_operating_point', uses=[], parameters={})
    runtime = CardRuntime(tmp_path, card, p, lambda kind, value: None)
    assert {'approach_gain', 'stable_s', 'tec_settle_s', 'target_frequency_hz', 'target_wavelength_nm', 'tolerance_hz'} < set(runtime.specs)
    assert not {'window_follow', 'approach_settle_s', 'approach_step_fraction', 'current_feedback_gain'} & set(runtime.specs)
    assert all(runtime.specs[name].boundaries == ('between_points',) for name in runtime.specs)
    hot = runtime.validate({**runtime.hot, 'approach_gain': .8, 'tec_settle_s': 30., 'stable_s': 5.})
    assert (hot['approach_gain'], hot['tec_settle_s'], hot['stable_s']) == (.8, 30., 5.)
    with pytest.raises(ValueError):
        runtime.validate({**runtime.hot, 'tec_settle_s': 5000.})
    with pytest.raises(ValueError, match='cannot be cleared'):
        runtime.validate({**runtime.hot, 'target_frequency_hz': None})
    runtime.close()


def test_schema_and_readiness_take_the_recovery_reference_instead_of_a_map():
    from pathlib import Path
    from ldcon.core.interface_schema import load_mapping, parameters as schema_parameters, readiness, validate_laser
    from ldcon.core.operation_registry import CARD_TYPES, normalize_parameters
    card = dict(id='tec-branch-alignment', operation='tune.ecdl_operating_point', uses=list(CARD_TYPES['tune.ecdl_operating_point']), parameters={})
    ref = dict(job_id='j1', sha256='b' * 64)
    p = schema_parameters(card, dict(recovery_ref=ref))
    assert p == normalize_parameters(card, dict(recovery_ref=ref)) and p['recovery_ref'] == ref and p['tolerance_hz'] == 100e6
    fixtures = Path(__file__).resolve().parent / 'fixtures' / 'effective-settings' / 'var' / 'lasers' / 'sr422' / 'config'
    laser = load_mapping((fixtures / 'lasers' / 'sr422.yaml').read_text())
    validate_laser(laser)
    deck = load_mapping((fixtures / 'interface-decks' / 'ecdl-pzt-ldc-tec.yaml').read_text())
    deck_card = next(c for c in deck['cards'] if c['id'] == 'tec-branch-alignment')
    blocked = readiness(laser, deck, deck_card, {})['blocked_reasons']
    assert any('recovery artifact or same-laser job reference is required' in r for r in blocked)
    assert not any('replay_tolerance_hz' in r or 'center_fraction' in r or 'map' in r for r in blocked)
    blocked = readiness(laser, deck, deck_card, dict(recovery_ref=ref))['blocked_reasons']
    assert any('Resolve the same-laser recovery reference' in r for r in blocked)
    plant = Plant()
    artifact = plant.prepared()
    status = readiness(laser, deck, deck_card, dict(recovery_artifact=artifact))
    assert not any('recovery' in r.lower() or 'tec_ff' in r for r in status['blocked_reasons'])
    from ldcon.jobs.ecdl_mode_map import seal
    blocked = readiness(laser, deck, deck_card, dict(recovery_artifact=seal(dict(artifact, schema='ldcon.ecdl-recovery.v1'))))['blocked_reasons']
    assert any('artifact' in r.lower() for r in blocked)


# --- pacing ---------------------------------------------------------------------------------------------------------

def test_pacing_thermal_settle_per_iteration_owner_interval_per_step_and_gain():
    def run(**overrides):
        plant = Plant()
        artifact = plant.prepared()
        result = plant.align(artifact, **overrides)
        assert result['status'] == 'succeeded'
        return plant, result

    plant, baseline = run()
    steps = plant.of('ecdl_alignment_step')
    reads = [r for r in baseline['trace'] if r['stage'] == 'after TEC settle']
    assert len(reads) == len(steps)
    # after the TEC / LDC writes of an iteration the next read waits tec_settle_s (thermal settling), not the policy settle
    for step, read in zip(steps, reads):
        last_write = max(c['at'] for c in plant.commands if c['at'] <= read['acquired_s'])
        assert read['acquired_s'] - last_write >= 1. - 1e-9 and step['settle_s'] == 1.
    # the owner's update interval still paces every TEC sub-step of a bounded move
    tec = commands(plant, 'temperature')
    gaps = [b['at'] - a['at'] for a, b in zip(tec, tec[1:])]
    assert gaps and min(gaps) >= .25 - 1e-9
    assert all(abs(c['value'] - c['before']) <= .02 + 1e-9 for c in tec)

    plant, slow = run(tec_settle_s=45.)
    assert slow['iterations'] == baseline['iterations'] and slow['timing']['settle_s'] == pytest.approx(45. * slow['iterations'])
    assert slow['timing']['total_s'] > baseline['timing']['total_s'] + 40. * slow['iterations']
    reads = [r for r in slow['trace'] if r['stage'] == 'after TEC settle']
    for read in reads:
        last_write = max(c['at'] for c in plant.commands if c['at'] <= read['acquired_s'])
        assert read['acquired_s'] - last_write >= 45. - 1e-9

    plant, fast = run(approach_gain=1.)
    assert fast['iterations'] < baseline['iterations']
    plant, gentle = run(approach_gain=.25)
    assert gentle['iterations'] > baseline['iterations']
    # the stability window: at least three fresh samples over stable_s, sample_interval_s apart
    plant, result = run(stable_s=2.)
    stability = [r for r in result['trace'] if r['stage'] == 'stability']
    assert len(stability) >= 3 and result['timing']['total_s'] > baseline['timing']['total_s']
