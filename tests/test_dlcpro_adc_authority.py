"""Input measurement gain is operational; output/guard changes remain reviewed."""
from copy import deepcopy

import pytest

from experiment_core.errors import AuthorizationRequired
from experiment_core.records import Journal, digest
from ldcon.core.agent_authority import FREE, classify
from ldcon.owners.dlcpro_awg_plan import classify_policy_change, configuration
from ldcon.owners.owner_projection import SystemPolicyAuthority
from .test_dlcpro_awg import config


def policy():
    return dict(awg_sweep=configuration(config()), axes={'piezo': {'bounds': [10., 90.]}},
                ff_enabled=True, ff_factor_ma_per_v=-.16)


def test_exact_input_gain_requires_host_authority_and_idle(tmp_path):
    old = policy()
    new = deepcopy(old)
    new['awg_sweep']['adc_v_per_awg_v'] = .9856819965
    journal = Journal(tmp_path / 'operations.sqlite')
    authority = SystemPolicyAuthority('test', old, dependencies={}, journal=journal,
                                     classify_change=classify_policy_change)
    calls = []
    args = dict(expected_revision=digest(old),
                prepare=lambda p: calls.append('prepare') or {'idle': True},
                verify=lambda p: dict(settled=True, source_revision=digest(p)))
    assert authority.adopt(new, boundary='cycle', operational_authorized=True,
                           **args)['state'] == 'deferred'
    assert calls == [] and authority.snapshot() == old
    with pytest.raises(AuthorizationRequired, match='operational'):
        authority.adopt(new, boundary='idle', **args)
    assert calls == [] and authority.snapshot() == old
    assert authority.adopt(new, boundary='idle', operational_authorized=True,
                           **args)['state'] == 'effective'
    assert calls == ['prepare'] and authority.snapshot() == new
    journal.close()
    journal = Journal(tmp_path / 'operations.sqlite')
    restarted = SystemPolicyAuthority('test', new, dependencies={}, journal=journal,
                                      classify_change=classify_policy_change)
    assert restarted.snapshot() == new
    journal.close()


@pytest.mark.parametrize('value', [0., -1., float('nan'), float('inf'), True, '1'])
def test_invalid_gain_rejected(value):
    old = policy()
    new = deepcopy(old)
    new['awg_sweep']['adc_v_per_awg_v'] = value
    with pytest.raises(ValueError):
        classify_policy_change(old, new)


@pytest.mark.parametrize('path,value', [
    (('awg_sweep', 'adc_tolerance_v'), .002),
    (('awg_sweep', 'pzt_v_per_input_v'), 2.),
    (('awg_sweep', 'target_input'), 'fine1'),
    (('awg_sweep', 'awg', 'min_v'), -2.),
    (('awg_sweep', 'awg', 'resource'), 'TCPIP::other.invalid::INSTR'),
    (('axes', 'piezo', 'bounds'), [5., 95.]),
    (('ff_factor_ma_per_v',), -.2),
])
def test_combined_change_never_gets_gain_only_authority(path, value):
    old = policy()
    new = deepcopy(old)
    new['awg_sweep']['adc_v_per_awg_v'] = .9856819965
    block = new
    for key in path[:-1]:
        block = block[key]
    block[path[-1]] = value
    try:
        result = classify_policy_change(old, new)
    except ValueError:
        return  # Invalid wiring/envelopes are refused before adoption.
    assert result.category == 'safety'


def test_api_exact_gain_leaf_is_free():
    path = ('devices', 'dlc', 'control_settings', 'awg_sweep', 'adc_v_per_awg_v')
    assert classify('lasers', path) == FREE
