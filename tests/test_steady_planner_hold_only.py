"""The steady owner maintains PI/TEC/FF; admitted cards own exploration."""
from types import SimpleNamespace
from pathlib import Path

import pytest
import yaml

from ldcon.owners.ecdl_steady_policy import SteadyPlanner, validate_dither_profile


def planner(*, direction='decrement', ff=None):
    profile = dict(
        current_span=.02, tec_gain=.2, counts_per_temperature_unit=1000.,
        tec_center_fraction=.5, tec_dt_cap_s=1.,
        window=dict(initial_safe_center=.79, edge_response=.003, step=.001),
        current_safety=dict(metrics_max={'side_peak_height_max': .1},
                            direction=direction, decrement=.001, step=.001,
                            max_correction=.01))
    if ff is not None:
        profile['tec_current_feedforward'] = dict(current_per_temperature=ff)
    def axis(low, high, resolution):
        return SimpleNamespace(low=low, high=high, resolution=resolution,
                               max_step=.001, max_slew_per_s=.01,
                               update_interval_s=.1)
    policies = dict(current=axis(0., 1., .0001), temperature=axis(0., 1., .0001),
                    piezo=axis(0., 60000., 1.))
    return SteadyPlanner(profile, policies,
                         dict(current=.8, temperature=.5, piezo=30000.), 'hold')


def state(**outputs):
    return dict(outputs=dict(dict(current=.8, temperature=.5, piezo=30000.), **outputs),
                tec_control_ready=True, optical_maintenance_required=True)


@pytest.mark.parametrize('direction', ['decrement', 'toward_interior'])
@pytest.mark.parametrize('metrics', [{}, {'side_peak_height_max': .9}])
def test_quality_metadata_and_large_dither_cannot_trigger_hidden_current_moves(direction, metrics):
    p = planner(direction=direction)
    for now in (1., 20., 1000.):
        result = p.plan(now, 1., state(), metrics, response=10.)
        assert result['actions'] == []
        assert result['blocked'] == []
    assert not hasattr(p, 'window')
    assert not hasattr(p, 'safety_triggered')


@pytest.mark.parametrize('metrics', [{}, {'side_peak_height_max': .9}])
def test_quality_warning_does_not_block_bounded_continuous_tec_centering(metrics):
    p = planner()
    snapshot = state(); snapshot['outputs']['piezo'] += 1000.
    result = p.plan(1., 1., snapshot, metrics, response=10.)
    assert result['actions'] == [dict(axis='temperature', value=.501,
                                      reason='PZT slow centering')]


def test_current_feedforward_remains_active_quantized_and_span_checked():
    p = planner(ff=1.25)
    snapshot = state(); snapshot['outputs']['temperature'] += .001
    result = p.plan(1., 1., snapshot, {}, response=10.)
    assert result['actions'][0]['axis'] == 'current'
    assert result['actions'][0]['reason'] == 'TEC current feedforward'
    assert result['actions'][0]['value'] == pytest.approx(.801)
    p = planner(ff=100.)
    result = p.plan(1., 1., snapshot, {}, response=10.)
    assert not result['actions']
    assert result['blocked'] == ['TEC current feedforward outside approved current span']


def test_invalid_control_input_or_explicit_pause_still_prevents_continuous_moves():
    for flag in ('tec_control_ready', 'tec_ff_paused'):
        p = planner(ff=1.25)
        snapshot = state(); snapshot['outputs'].update(temperature=.501, piezo=31000.)
        snapshot[flag] = flag == 'tec_ff_paused'
        assert p.plan(1., 1., snapshot, {}, response=10.)['actions'] == []


def test_passive_dither_validation_needs_no_old_window_motion_policy():
    fixture = (Path(__file__).resolve().parent / 'support') / 'jobs/tests/fixtures/live-20261022/lasers/sr422.yaml'
    profile = yaml.safe_load(fixture.read_text())['devices']['rig']['control_settings']['steady_lock']
    profile.pop('window', None)
    assert validate_dither_profile(profile) is profile['dither']
    profile['dither']['verified'] = False
    with pytest.raises(ValueError, match='not been verified'):
        validate_dither_profile(profile)
