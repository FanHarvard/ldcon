"""Target-ff design (2026-10-27, human decisions 1-5): the per-step target cap, the slew, the FF coefficients and the
post-step recentre fraction are FREE operating parameters adopted live; the physical limits and bounds stay SAFETY.
Pins agent_authority.classify / live_class, the native owner's classify_change / policy_update free paths and the
profile validators that accept the new keys."""
import copy
import importlib.util
from pathlib import Path

import pytest

from ldcon.core.agent_authority import classify, live_class, FREE, SAFETY, FORBIDDEN
from ldcon.owners.native_policy_update import FREE_PATHS, TARGET_FF_PATHS, LIVE_ALLOWED, allowed
from ldcon.owners.native_system_owner import NativeLaserSystemOwner as NativeSystemOwner

RIG = ('devices', 'rig', 'control_settings')
NATIVE = ('devices', 'native_esp', 'control_settings', 'native_current')

FREE_ROWS = {
    ('target', 'max_change_hz'): 'snapshot',                                        # decision 1
    RIG + ('steady_lock', 'target_ramp_step_hz'): 'owner_hot',                      # decision 1
    RIG + ('steady_lock', 'target_slew_hz_per_s'): 'owner_hot',                     # decision 2
    RIG + ('steady_lock', 'target_feedforward_scale'): 'owner_hot',
    RIG + ('steady_lock', 'tec_current_feedforward', 'current_per_temperature'): 'owner_hot',   # decision 3
    RIG + ('steady_lock', 'tec_current_feedforward', 'mode'): 'owner_hot',
    RIG + ('background_adjustments', 'feedforward', 'bounds', 'current_per_temperature', '0'): 'owner_hot',  # decision 3 bounds
    RIG + ('steady_lock', 'ldc_target_feedforward', 'v_per_hz'): 'owner_hot',       # decision 4
    RIG + ('steady_lock', 'ldc_target_feedforward', 'hz_per_v'): 'owner_hot',
    NATIVE + ('native', 'target_ramp_step_hz'): 'native_hot',                       # decision 1
    NATIVE + ('native', 'target_feedforward_scale'): 'native_hot',
    NATIVE + ('native', 'recentre_integral_fraction'): 'native_hot',                # decision 5
}
SAFETY_ROWS = {
    ('target', 'allowed_change_hz', '0'): 'snapshot',
    RIG + ('steady_lock', 'target_feedforward_scale_bounds'): 'owner_hot',
    RIG + ('steady_lock', 'ldc_target_feedforward', 'max_abs_v'): 'owner_hot',       # decision 4: safety
    RIG + ('steady_lock', 'ldc_target_feedforward', 'bounds_v_per_hz', '1'): 'owner_hot',
    RIG + ('background_adjustments', 'slow_loop', 'bounds', 'tec_gain', '0'): 'owner_hot',
    NATIVE + ('native', 'target_feedforward_scale_bounds'): 'native_hot',
    NATIVE + ('activation_range_a', '0'): 'native_hot',                            # physical window stays
    NATIVE + ('current_bounds_a', '1'): 'native_hot',
    ('devices', 'rig', 'absolute_conditions', 'axes', 'piezo', 'bounds', '1'): 'owner_hot',
}


@pytest.mark.parametrize('path,live', sorted(FREE_ROWS.items()))
def test_free_operating_parameters_and_their_live_paths(path, live):
    assert classify('lasers', path) == FREE
    assert live_class('lasers', path) == live


@pytest.mark.parametrize('path,live', sorted(SAFETY_ROWS.items()))
def test_bounds_and_physical_limits_stay_safety(path, live):
    assert classify('lasers', path) == SAFETY
    assert live_class('lasers', path) == live


def test_calibration_stays_forbidden():
    assert classify('lasers', RIG + ('steady_lock', 'wavelength_nm_per_count')) == FORBIDDEN
    assert classify('lasers', NATIVE + ('calibration', 'wavelength_nm_per_a')) == FORBIDDEN
    assert live_class('lasers', NATIVE + ('calibration', 'wavelength_nm_per_a')) == 'idle_only'


def test_native_owner_classifies_the_free_leaves_as_operational():
    from experiment_core.configuration import Parameter
    before = dict(native=dict(target_ramp_step_hz=100e6, target_feedforward_scale=1., integral_limit_count=100.),
                  activation_range_a=[-.003, .003])
    for key, value in (('target_ramp_step_hz', 500e6), ('target_feedforward_scale', 1.1), ('recentre_integral_fraction', .4)):
        after = copy.deepcopy(before)
        after['native'][key] = value
        assert NativeSystemOwner.classify_change(before, after) == Parameter('operational', '', '', 'cycle')
    after = copy.deepcopy(before)
    after['activation_range_a'] = [-.01, .01]
    assert NativeSystemOwner.classify_change(before, after) == Parameter('safety', '', '', 'cycle')


def test_native_policy_update_free_paths_match_the_authority_table():
    assert {'native.target_ramp_step_hz', 'native.target_feedforward_scale', 'native.recentre_integral_fraction'} <= FREE_PATHS
    assert 'native.target_feedforward_scale_bounds' not in FREE_PATHS
    assert 'native.recentre_integral_fraction' in TARGET_FF_PATHS and TARGET_FF_PATHS <= LIVE_ALLOWED
    assert allowed('native.recentre_integral_fraction') and not allowed('calibration.wavelength_nm_per_a')


def test_native_policy_accepts_the_recentre_fraction_in_range():
    from ldcon.owners.esp32_native_owner import validate_policy, recentre_integral_fraction
    spec = importlib.util.spec_from_file_location('native_fixture_authority',
        (Path(__file__).resolve().parent / 'support') / 'jobs/tests/test_esp32_native_current.py')
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    policy = fixture.policy()
    assert recentre_integral_fraction(policy['native']) == .5           # the default when the yaml names none
    policy['native']['recentre_integral_fraction'] = .3
    assert validate_policy(policy)['native']['recentre_integral_fraction'] == .3
    for bad in (-.1, 1.5, 'x', float('nan')):
        policy['native']['recentre_integral_fraction'] = bad
        with pytest.raises(ValueError, match='recentre_integral_fraction'):
            validate_policy(policy)


def test_ecdl_profile_validator_accepts_the_ldc_term_and_a_negative_tec_coefficient():
    from ldcon.owners.ecdl_target_ff import ldc_policy, LDC_DEFAULTS
    assert ldc_policy({}) == dict(LDC_DEFAULTS, enabled=False)
    block = dict(v_per_hz=1e-11, max_abs_v=.008, bounds_v_per_hz=[-2e-11, 2e-11], hz_per_v=5.5e9)
    assert ldc_policy(dict(ldc_target_feedforward=block))['enabled'] is True
    assert ldc_policy(dict(ldc_target_feedforward=dict(v_per_hz=0.)))['enabled'] is False
    for bad in (dict(block, v_per_hz=3e-11), dict(block, max_abs_v=0.), dict(block, hz_per_v=None), dict(block, extra=1),
                dict(block, bounds_v_per_hz=[1e-11])):
        with pytest.raises(ValueError):
            ldc_policy(dict(ldc_target_feedforward=bad))
    from ldcon.owners.ecdl_steady_policy import validate_profile
    import yaml
    laser = yaml.safe_load(((Path(__file__).resolve().parent / 'support') / 'jobs/tests/fixtures/live-20261022/lasers/sr422.yaml').read_text())
    profile = laser['devices']['rig']['control_settings']['steady_lock']
    profile['ldc_target_feedforward'] = block
    profile['tec_current_feedforward']['current_per_temperature'] = -.07      # the measured sr422 window drift
    validate_profile(profile, 'hold')
    profile['tec_current_feedforward']['current_per_temperature'] = 0.
    with pytest.raises(ValueError, match='Zero TEC current feedforward'):
        validate_profile(profile, 'hold')
