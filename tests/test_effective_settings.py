"""ops/effective_settings.py: every effective leaf of a laser with its source (rule M baseline tool, step 2).

Fixtures are copies of the live sr422 / sr1092 yaml, assignments, decks and owner configs of 2026-10-27 (no tokens
or webhook URLs: only env-var names). The golden files are the tool's own output over those fixtures; a change in
the tool or in a resolver it calls must be reviewed through the golden diff.
"""
import copy
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import yaml

from ldcon.core.interface_schema import load_mapping, validate_laser

REPO = (Path(__file__).resolve().parent / 'support')
TOOL = REPO / 'ops' / 'effective_settings.py'
FIXTURES = Path(__file__).resolve().parent / 'fixtures' / 'effective-settings'
ROOT = FIXTURES / 'var'

_spec = importlib.util.spec_from_file_location('effective_settings', TOOL)
effective_settings = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(effective_settings)


def golden(laser):
    return json.loads((FIXTURES / ('golden-' + laser + '.json')).read_text(encoding='utf-8'))


def yaml_leaves(laser):
    document = load_mapping((ROOT / 'lasers' / laser / 'config' / 'lasers' / (laser + '.yaml')).read_text())
    validate_laser(document)
    return {'.'.join(path) for path, _ in effective_settings.leaves(document)}


@pytest.mark.parametrize('laser', ['sr422', 'sr1092'])
def test_output_matches_golden_and_covers_every_yaml_leaf(laser):
    result = effective_settings.effective(ROOT, laser)
    assert result == golden(laser)
    rows = result['rows']
    assert [r['path'] for r in rows] == sorted(r['path'] for r in rows)
    assert {r['path'] for r in rows if r['source'].startswith('yaml:lasers/')} == yaml_leaves(laser)
    sources = {r['source'].split(':')[0].split(' ')[0] for r in rows}
    assert {'yaml', 'owner-config', 'assignment', 'deck', 'code'} <= sources
    for r in rows:
        assert set(r) == {'path', 'value', 'source', 'class', 'live'}
        if r['source'].startswith('error'):
            assert laser == 'sr422' and r['path'] in {
                'cards.ecdl-pzt-ldc-tec.steady-lock', 'cards.ecdl-pzt-ldc-tec.mode-recovery', 'cards.ecdl-pzt-ldc-tec.window-center'}
            assert any(text in r['source'] for text in ('Legacy recovery policy retired', 'Retired search selector', 'Legacy recovery artifacts retired', 'retired'))


@pytest.mark.parametrize('laser', ['sr422', 'sr1092'])
def test_cli_json_equals_the_library_result(laser):
    completed = subprocess.run([sys.executable, '-I', str(TOOL), '--laser', laser, '--root', str(ROOT), '--json'],
                               capture_output=True, text=True, check=True)
    assert json.loads(completed.stdout) == golden(laser)
    text = subprocess.run([sys.executable, '-I', str(TOOL), '--laser', laser, '--root', str(ROOT)],
                          capture_output=True, text=True, check=True).stdout
    assert text.splitlines()[0].startswith('path') and text.strip().endswith('(' + laser + ')')


def test_sr422_rows_show_classes_live_and_derived_policies():
    rows = {r['path']: r for r in golden('sr422')['rows']}
    timing = rows['devices.rig.control_settings.axes.piezo.update_interval_s']
    assert timing['value'] == .1 and timing['class'] == 'safety' and timing['live'] == 'owner_hot'
    assert rows['devices.rig.control_settings.locked_sweep.limits.max_half_span_hz']['live'] == 'owner_hot'
    assert rows['devices.rig.control_settings.axes.temperature.frequency_hz_per_unit']['live'] == 'idle_only'
    assert rows['derived.rig.policy.piezo.max_slew_per_s']['value'] == pytest.approx(65535. / .1)
    assert rows['derived.rig.policy.piezo.max_slew_per_s']['source'] == 'derived:ecdl_owner.axis_policies'
    # sr422 has no continuous slope cap in its yaml today (design R8): the tool says so explicitly
    assert rows['sweep_api.rig.continuous.max_slope_hz_per_s'] == dict(
        path='sweep_api.rig.continuous.max_slope_hz_per_s', value=None, source='request-default (no cap)', **{'class': ''}, live='')
    assert rows['sweep_api.rig.ldc_ff_v_per_hz']['value'] == -5e-12 and rows['sweep_api.rig.ldc_ff_v_per_hz']['source'].startswith('yaml:')
    assert rows['sweep_api.rig.pzt_ff_gain_scale']['source'] == 'code:sweep_api.CODE_DEFAULTS[rig]'
    steady = 'cards.ecdl-pzt-ldc-tec.steady-lock.'
    assert rows[steady + 'tolerance_hz']['source'].startswith('assignment:')
    assert rows[steady + 'tec_gain']['source'] == 'code:ecdl_steady.DEFAULTS'
    assert rows['cards.ecdl-pzt-ldc-tec.online-pid.window_s']['source'].startswith('assignment:')
    assert rows['owner-config.sdg2042x-SDG2XFBCA00839.capabilities.max_vpp']['source'] == 'owner-config:instrument-owners/sdg2042x-SDG2XFBCA00839/config.json'
    assert rows['owner-config.sdg2042x-SDG2XFBCA00839.capabilities.max_vpp']['live'] == 'restart'


def test_sr1092_layer2_leaves_name_their_owner_and_the_ignored_copies_are_marked():
    """Rule M steps 3-4: the sr1092 yaml blocks are layer 2 (adopted by the native owner / projected to the CLD owner);
    the untrimmed config.json copies are listed as ignored (live: ignored) until ops/trim_owner_config.py removes them."""
    rows = {r['path']: r for r in golden('sr1092')['rows']}
    native = 'devices.native_esp.control_settings.native_current.'
    assert rows[native + 'max_step_a']['source'].endswith('[layer 2: adopted by esp32servocon-sr1092] [owner-config copy ignored =]')
    assert rows[native + 'native.integral_limit_count']['source'].endswith('[owner-config copy ignored =]')
    assert rows[native + 'calibration.current_a_per_v']['source'].endswith('[layer 2: adopted by esp32servocon-sr1092] [owner-config physical =]')
    assert rows[native + 'dac.max_voltage']['source'].endswith('[owner-config physical =]')
    ff = rows['devices.native_esp.control_settings.native_ff_sweep.unwind_steps']
    assert ff['source'].endswith('[owner-config copy ignored =]') and ff['live'] == 'native_hot'
    bounds = rows['devices.ldc.absolute_conditions.current_a.bounds_a']
    assert bounds['source'].endswith('[layer 2: projected to cld1015-M01343851] [owner-config copy ignored =]') and bounds['live'] == 'idle_only'
    assert rows['devices.ldc.control_settings.current.pid.kp_a_per_hz']['live'] == 'native_hot'
    # the live yaml holds the YAML string '5e-05' where the CLD config holds the float: a real divergence
    diverged = rows['devices.ldc.control_settings.slow_control.output_resolution']
    assert diverged['value'] == '5e-05' and 'copy ignored DIVERGED: 5e-05 in device-owners/cld1015-M01343851/config.json' in diverged['source']
    assert sum('DIVERGED' in r['source'] for r in rows.values()) == 1
    copies = {p: r for p, r in rows.items() if r['live'] == 'ignored'}
    assert all(r['source'].endswith('[layer1 copy ignored; ops/trim_owner_config.py]') for r in copies.values())
    assert 'owner-config.esp32servocon-sr1092.native_current.max_step_a' in copies
    assert 'owner-config.cld1015-M01343851.controller.current_conditions.bounds_a' in copies
    assert rows['owner-config.esp32servocon-sr1092.native_current.dac.max_voltage']['live'] == 'restart'     # layer 1
    assert rows['owner-config.cld1015-M01343851.controller.current_limits_a']['live'] == 'restart'             # device rating (Q1)
    assert rows['sweep_api.current_awg.continuous.max_slope_hz_per_s']['value'] == 1e9
    assert rows['code.native_ff_proxy.READY_DEFAULTS.tolerance_hz']['source'] == 'code:native_ff_proxy.READY_DEFAULTS'


def test_sr1092_trimmed_owner_configs_keep_every_yaml_value(tmp_path):
    """After ops/trim_owner_config.py the owner-config copy rows disappear and no yaml value changes."""
    _spec = importlib.util.spec_from_file_location('trim_owner_config', REPO / 'ops' / 'trim_owner_config.py')
    trim_tool = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(trim_tool)
    root = tmp_path / 'var'
    shutil.copytree(ROOT, root)
    for name, extra in (('esp32servocon-sr1092', {}), ('cld1015-M01343851', dict(principal='laser-sr1092', key_env='LDCON_PROJECTION_SR1092'))):
        path = root / 'device-owners' / name / 'config.json'
        trimmed, _ = trim_tool.trim(json.loads(path.read_text()), **extra)
        path.write_text(json.dumps(trimmed))
    before = {r['path']: r for r in golden('sr1092')['rows']}
    after = {r['path']: r for r in effective_settings.effective(root, 'sr1092')['rows']}
    assert not [p for p in after if p in before and after[p]['value'] != before[p]['value']]
    gone = {p for p in before if p not in after}
    assert gone == {p for p, r in before.items() if r['live'] == 'ignored'} | {
        p for p in before if p.startswith('owner-config.esp32servocon-sr1092.native_current.')}   # physical leaves move to physical.*
    assert set(after) - set(before) == {'owner-config.esp32servocon-sr1092.physical.' + p for p in
                                        ('firmware_version', 'dac.bits', 'dac.min_voltage', 'dac.max_voltage', 'adc.bits', 'adc.min_voltage', 'adc.max_voltage',
                                         'calibration.bias_current_a', 'calibration.current_a_per_v', 'calibration.evidence', 'calibration.gain_relative_uncertainty',
                                         'calibration.response_verified', 'calibration.verified', 'calibration.wavelength_nm_per_a', 'calibration.zero_voltage_v')} | {
        'owner-config.cld1015-M01343851.projection_sources.laser-sr1092.channels', 'owner-config.cld1015-M01343851.projection_sources.laser-sr1092.key_env',
        'owner-config.cld1015-M01343851.projection_sources.laser-sr1092.system_id'}
    native = 'devices.native_esp.control_settings.native_current.'
    assert after[native + 'max_step_a']['source'].endswith('[layer 2: adopted by esp32servocon-sr1092]')
    assert after[native + 'dac.max_voltage']['source'].endswith('[owner-config physical =]')
    assert after['devices.ldc.absolute_conditions.current_a.bounds_a']['source'].endswith('[layer 2: projected to cld1015-M01343851]')
    assert not any('DIVERGED' in r['source'] for r in after.values())


def test_before_after_diff_isolates_a_timing_change_and_a_divergence(tmp_path):
    root = tmp_path / 'var'
    shutil.copytree(ROOT, root)
    laser_file = root / 'lasers' / 'sr422' / 'config' / 'lasers' / 'sr422.yaml'
    document = load_mapping(laser_file.read_text())
    document['devices']['rig']['control_settings']['axes']['piezo']['update_interval_s'] = .2
    laser_file.write_text(yaml.safe_dump(document))
    before = {r['path']: r for r in golden('sr422')['rows']}
    after = {r['path']: r for r in effective_settings.effective(root, 'sr422')['rows']}
    changed = {p for p in set(before) | set(after) if before.get(p) != after.get(p)}
    assert changed == {'devices.rig.control_settings.axes.piezo.update_interval_s',
                       'derived.rig.policy.piezo.update_interval_s', 'derived.rig.policy.piezo.max_slew_per_s'}
    config = root / 'device-owners' / 'esp32servocon-sr1092' / 'config.json'
    value = json.loads(config.read_text())
    value['native_current']['max_step_a'] = .002
    config.write_text(json.dumps(value))
    rows = {r['path']: r for r in effective_settings.effective(root, 'sr1092')['rows']}
    assert 'DIVERGED: 0.002 in device-owners/esp32servocon-sr1092/config.json' in rows['devices.native_esp.control_settings.native_current.max_step_a']['source']
    assert rows['owner-config.esp32servocon-sr1092.native_current.max_step_a']['value'] == .002


def test_owner_rpc_is_read_only_describe_through_the_rig_wrapper():
    calls = []

    def rpc(endpoint, request):
        calls.append((endpoint, request))
        return dict(policy_adoption=dict(pending=False, effective_revision='e', saved_revision='e'),
                    policies=dict(piezo=dict(max_step=65535.)))
    rows = {r['path']: r for r in effective_settings.effective(ROOT, 'sr422', rpc=rpc)['rows']}
    assert calls == [('http://192.168.1.5:47130', dict(action='ecdl', instrument='sr422', request=dict(action='describe')))]
    assert rows['owner.rig.policy_adoption.pending']['value'] is False
    assert rows['owner.rig.policies.piezo.max_step']['source'] == 'owner-rpc:http://192.168.1.5:47130'
    calls.clear()
    rows = {r['path']: r for r in effective_settings.effective(ROOT, 'sr1092', rpc=rpc)['rows']}
    assert calls == [('http://127.0.0.1:47141', dict(action='describe', device_id='esp32servocon-sr1092'))]
    assert 'owner.native_esp.policy_adoption.pending' in rows
    assert all(r['source'].startswith('owner-rpc:') for p, r in rows.items() if p.startswith('owner.'))
    # a failing owner never hides the file-based rows
    failing = {r['path']: r for r in effective_settings.effective(ROOT, 'sr422', rpc=lambda e, q: (_ for _ in ()).throw(OSError('down')))['rows']}
    assert failing['owner.rig']['source'].startswith('owner-rpc:http://192.168.1.5:47130 error: down')
    assert {p for p in failing if not p.startswith('owner.')} == {r['path'] for r in golden('sr422')['rows']}
    refused = {r['path']: r for r in effective_settings.effective(
        ROOT, 'sr1092', rpc=lambda e, q: (_ for _ in ()).throw(RuntimeError('AWG broker configuration: Native device identity mismatch')))['rows']}
    assert refused['owner.native_esp']['source'] == 'owner-rpc:http://127.0.0.1:47141 error: AWG broker configuration: Native device identity mismatch'


def test_fixtures_carry_no_secrets():
    for path in ROOT.rglob('*'):
        if path.is_file():
            text = path.read_text(encoding='utf-8').lower()
            for needle in ('webhook.office', 'https://hooks', 'bearer ', 'token:', '"token"', 'secret', 'password'):
                assert needle not in text, (path, needle)


def test_retired_recovery_settings_are_visible_but_explicitly_unvalidated():
    rows = {r['path']: r for r in effective_settings.effective(ROOT, 'sr422')['rows']}
    prefix = 'cards.ecdl-pzt-ldc-tec.steady-lock'
    assert 'retired' in rows[prefix]['source'].lower()
    value = rows[prefix + '.recovery.procedure.branch_match.max_age_s']
    assert value['value'] == 86400.0 and value['live'] == 'unvalidated'
