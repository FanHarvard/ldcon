"""One current traversal; selecting a measurement never selects a movement policy."""
from types import SimpleNamespace

import pytest

from ldcon.jobs.window_center import WindowCenterPolicy, parameters
from ldcon.jobs.ecdl_window_center import WindowCenterSession


METRIC = 'metrics.side_peak_height_max'
AXIS = SimpleNamespace(low=0., high=1., max_step=.1, resolution=.0001, update_interval_s=.05, settle_s=.1)


def test_measurement_selection_has_no_hidden_budget_or_wait_changes():
    p = parameters({'parameters': dict(observables=[METRIC, 'dither_response'], measure_dither=False,
        boundary_limits={METRIC: .3, 'dither_response': 1.}, slow_observables=['dither_response'],
        fast_settle_s=3., settle_s=33., max_steps=12)}, {})
    assert p['observables'] == [METRIC]
    assert p['boundary_limits'] == {METRIC: .3}
    assert p['slow_observables'] == [] and p['fast_settle_s'] is None
    assert p['max_steps'] == 12 and p['settle_s'] == 33.
    assert parameters({'parameters': p}, {}) == p


def test_dither_can_be_enabled_without_a_separate_algorithm():
    p = parameters({'parameters': dict(observables=[METRIC], measure_dither=True)}, {})
    assert p['observables'] == [METRIC, 'dither_response']
    assert parameters({'parameters': dict(observables=[METRIC])}, {})['measure_dither'] is False
    with pytest.raises(ValueError, match='boolean'):
        parameters({'parameters': dict(measure_dither=1)}, {})


@pytest.mark.parametrize('search', ['standard', 'fast_side_peak', None])
def test_retired_search_selector_requires_explicit_migration(search):
    with pytest.raises(ValueError, match='variants are retired'):
        parameters({'parameters': dict(search=search)}, {})


def test_disabled_dither_does_not_require_or_wait_for_dither_configuration():
    lock = SimpleNamespace(session=SimpleNamespace(device={'control_settings': {}}),
        recovery_ports=lambda: {'current': SimpleNamespace(policy=AXIS)})
    session = WindowCenterSession({'request_id': 'w', 'card_id': 'window-center'},
        dict(measure_dither=False, observables=[METRIC], boundary_limits={METRIC: .3}, settle_s=.2),
        lock, {}, lambda *a: None, lambda *a: None, {}, clock=lambda: 0., wall_clock=lambda: 100.)
    assert session.interval == .2
    assert session.slow_fit_delay == 0.
    assert session.point_dither is False


def test_hop_holds_for_recovery_without_an_implicit_return_write():
    p = parameters({'parameters': dict(measure_dither=False, observables=[METRIC],
        boundary_limits={METRIC: .3}, target_frequency_hz=1e14, hop_threshold_hz=1e9)}, {})
    policy = WindowCenterPolicy(p, {'current': .5, 'temperature': .1, 'piezo': 100.}, AXIS, 0.)
    row = dict(outputs=dict(current=.5, temperature=.1, piezo=100.), acquired_s=1., acquired_unix_s=101.,
        observation=dict(mode_id='home', single_mode=True, metrics={'side_peak_height_max': .1},
                         frequency=dict(frequency_hz=1e14, acquired_s=1., acquired_unix_s=101.)))
    result = policy.tick(1., row)
    value = result['action']['value']
    policy.acknowledge(value, now=1.)
    row['outputs']['current'] = value
    row.update(acquired_s=2., acquired_unix_s=102.)
    row['observation']['frequency']['frequency_hz'] -= 2e9
    result = policy.tick(2., row)
    assert result['recovery_required'] is True
    assert result.get('action') is None
    assert result['boundaries']['low']['kind'] == 'hop'
    assert result['center_definition'] == 'branch_limited_window'
    assert result['center'] is None
