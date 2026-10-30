"""Frozen assembly wiring follows approved live native policy revisions.

Rule M step 4 (2026-10-27): the assembly dependency pins the wiring only (``native_system_owner.assembly_wiring``);
the operating policy and the FF profile are layer 2, adopted live (policy_update) and at start from the laser yaml
(``monitor_owner.startup_policy``); the owner's config.json keeps only the physical block.
"""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from experiment_core.records import Journal, digest
from ldcon.owners.native_assembly import make_assembly_factory
from ldcon.owners.monitor_owner import load_native_assembly, startup_policy
from ldcon.owners.native_system_owner import assembly_wiring, physical_block
from ldcon.owners.owner_projection import SystemPolicyAuthority
from ldcon.owners.esp32_native_owner import NativeCurrentOwner


@pytest.fixture
def assembly(monkeypatch, tmp_path):
    policy = dict(revision='initial', firmware_version='fixture',
        adc=dict(bits=16, min_voltage=0., max_voltage=3.3), dac=dict(bits=16, min_voltage=-.25, max_voltage=.25),
        calibration=dict(current_a_per_v=.15, evidence='original'), native=dict(evidence='original'),
        current_bounds_a=[.045, .055], heartbeat_timeout_s=5., missing_timeout_s=5., update_interval_s=.1,
        preserved=dict(multiplier_control=0., multiplier_target=0., software_modulation_enable=0, target_time_match=0))
    profile = dict(enabled=True, revision='ff-v1', firmware_version='fixture', window_rule='S1',
        frequency_hz_per_v=1e9, awg_center_v=1., adc_center_count=20000, adc_noise_counts=3.,
        adc_valid_bounds=[19970, 20030], max_half_span_hz=100e6, max_duration_s=30.,
        heartbeat_timeout_s=2., missing_timeout_s=2., unwind_interval_s=.2, unwind_steps=3,
        start_samples=3, start_span_s=.4, start_integral_fraction_max=.5,
        abort_integral_fraction_max=.9, abort_error_hz=10e6)
    laser = dict(devices={'native': dict(connection={'device_id': 'esp'},
        control_settings={'native_current': deepcopy(policy), 'native_ff_sweep': deepcopy(profile)})},
        interfaces={'native_current': {'device': 'native'}})
    journal = Journal(tmp_path / 'authority.sqlite')
    authority = SystemPolicyAuthority('laser', policy, dependencies={}, journal=journal)
    class System:
        runtime = NativeCurrentOwner.__new__(NativeCurrentOwner)
        runtime.driver = SimpleNamespace(device_id='esp')
        runtime.input_events = []
        runtime.clock = lambda: 123.
        ff_profile = deepcopy(profile)
        @property
        def policy(self):
            return authority.snapshot()
    system = System()
    system.authority = authority
    constructed = []
    monkeypatch.setattr('ldcon.jobs.native_ff_sweep.configuration', lambda laser: {
        'broker': {'projection': {'principal': 'laser', 'key_env': 'OFFLINE_UNUSED_KEY'}}})
    monkeypatch.setattr('ldcon.jobs.native_ff_sweep.NativeFFAwg',
        lambda config, **kwargs: constructed.append((config, kwargs)) or SimpleNamespace(config=config, **kwargs))
    factory = make_assembly_factory(laser)
    def adopt(change):
        candidate = authority.snapshot()
        change(candidate)
        old = authority.revision
        token = authority.approve(candidate, expected_revision=old,
            authorize_human=lambda p: {'human_identity': 'offline human', 'receipt_id': digest(candidate)})
        authority.adopt(candidate, expected_revision=old, approval=token, prepare=lambda p: {},
                        verify=lambda p: {'settled': True, 'source_revision': digest(p)})
    yield factory, system, laser, adopt, constructed
    journal.close()


@pytest.mark.parametrize('change', [
    lambda p: p['native'].update(evidence='reviewed metadata'),
    lambda p: p['calibration'].update(evidence='reviewed metadata'),
    lambda p: p.update(current_bounds_a=[.046, .054]),
])
def test_factory_accepts_adopted_live_policy_without_rebinding_assembly(assembly, change):
    factory, system, laser, adopt, constructed = assembly
    revision = factory.definition_revision
    adopt(change)
    awg = factory(system, {'request_id': 'new-session'})
    assert awg.authority is system.authority and len(constructed) == 1
    assert factory.definition_revision == revision == digest(assembly_wiring(laser))
    assert factory.laser_snapshot == laser


def test_wiring_dependency_ignores_operating_policy_profile_and_target(assembly):
    """A policy / FF profile / target edit in the yaml never changes the saved assembly dependency (no restart)."""
    _, _, laser, _, _ = assembly
    edited = deepcopy(laser)
    edited['devices']['native']['control_settings']['native_current']['current_bounds_a'] = [.040, .060]
    edited['devices']['native']['control_settings']['native_ff_sweep']['unwind_steps'] = 5
    edited['target'] = {'frequency_hz': 274589143700000.}
    assert digest(assembly_wiring(edited)) == digest(assembly_wiring(laser))
    wired = deepcopy(laser)
    wired['devices']['native']['connection']['device_id'] = 'other'
    assert digest(assembly_wiring(wired)) != digest(assembly_wiring(laser))


@pytest.mark.parametrize('change', [
    lambda p: p['calibration'].update(current_a_per_v=.16),
    lambda p: p['adc'].update(max_voltage=3.4),
    lambda p: p.update(firmware_version='other'),
])
def test_factory_refuses_changes_outside_live_operating_policy(assembly, change):
    factory, system, _, adopt, constructed = assembly
    adopt(change)
    with pytest.raises(ValueError, match='configuration differs'):
        factory(system, {})
    assert not constructed


@pytest.mark.parametrize('field,value', [('heartbeat_timeout_s', 1.), ('missing_timeout_s', 1.), ('update_interval_s', .3)])
def test_current_policy_must_still_admit_the_effective_ff_profile(assembly, field, value):
    factory, system, _, adopt, constructed = assembly
    adopt(lambda p: p.update({field: value}))
    with pytest.raises(ValueError, match='FF supervision'):
        factory(system, {})
    assert not constructed


def test_factory_follows_the_adopted_ff_profile_and_still_requires_the_device(assembly):
    """Rule M step 4: the FF profile is layer 2; a session uses the system owner's effective (hot-adopted) profile."""
    factory, system, _, _, constructed = assembly
    system.ff_profile['revision'] = 'ff-v2'
    system.ff_profile['unwind_steps'] = 5
    awg = factory(system, {})
    assert awg.authority is system.authority and len(constructed) == 1
    system.ff_profile['firmware_version'] = 'other'          # the profile proof (validate_profile) still applies
    with pytest.raises(ValueError, match='firmware'):
        factory(system, {})
    system.ff_profile['firmware_version'] = 'fixture'
    system.runtime.driver.device_id = 'other'
    with pytest.raises(ValueError, match='configuration differs'):
        factory(system, {})
    assert len(constructed) == 1


def test_assembly_journal_preserves_step_kind_without_event_collision(assembly):
    factory, system, _, _, _ = assembly
    awg = factory(system, {})
    value = dict(kind='native_current_centre', time=12., value=32829)
    awg.journal('operating_point_step', value)
    value['value'] = 0
    assert system.runtime.input_events == [dict(kind='operating_point_step', time=123.,
        details=dict(kind='native_current_centre', time=12., value=32829))]


def _monkeypatch_validation(monkeypatch):
    monkeypatch.setattr('ldcon.core.interface_schema.validate_laser', lambda value: value)
    monkeypatch.setattr('ldcon.owners.esp32_native_owner.validate_policy', lambda value: deepcopy(value))


def test_monitor_startup_takes_the_policy_from_the_laser_yaml_and_the_physical_block_from_config(assembly, tmp_path, monkeypatch):
    _, _, laser, _, _ = assembly
    _monkeypatch_validation(monkeypatch)
    path = tmp_path / 'laser.json'
    path.write_text(json.dumps(laser))
    native = laser['devices']['native']['control_settings']
    config = dict(device_id='esp', physical=physical_block(native['native_current']), native_assembly={'laser_file': path.name})
    factory, document, source = load_native_assembly(config, tmp_path / 'monitor.json')
    assert callable(factory) and document == laser
    assert factory.definition_revision == digest(assembly_wiring(laser))
    policy, profile, ignored = startup_policy(config, document)
    assert policy == native['native_current'] and profile == native['native_ff_sweep'] and ignored == []
    # the yaml's operating policy may differ from any earlier owner state: it is simply what the owner starts from
    laser['devices']['native']['control_settings']['native_current']['current_bounds_a'] = [.046, .054]
    path.write_text(json.dumps(laser))
    policy, _, _ = startup_policy(config, load_native_assembly(config, tmp_path / 'monitor.json')[1])
    assert policy['current_bounds_a'] == [.046, .054]


def test_monitor_startup_with_an_untrimmed_config_ignores_the_copies(assembly, tmp_path, monkeypatch):
    """Backward compatibility: the live config.json still carries native_current / native_ff_sweep; the physical
    block is taken from the copy, the copies are ignored (audited by the owner), the yaml decides the policy."""
    _, _, laser, _, _ = assembly
    _monkeypatch_validation(monkeypatch)
    path = tmp_path / 'laser.json'
    path.write_text(json.dumps(laser))
    native = laser['devices']['native']['control_settings']
    config = dict(device_id='esp', native_current=deepcopy(native['native_current']),
                  native_ff_sweep=deepcopy(native['native_ff_sweep']), native_assembly={'laser_file': path.name})
    config['native_current']['current_bounds_a'] = [.030, .070]            # a stale copy: ignored
    config['native_ff_sweep']['unwind_steps'] = 9
    _, document, source = load_native_assembly(config, tmp_path / 'monitor.json')
    policy, profile, ignored = startup_policy(config, document)
    assert ignored == ['native_current', 'native_ff_sweep']
    assert policy['current_bounds_a'] == [.045, .055] and profile['unwind_steps'] == 3


@pytest.mark.parametrize('damage', ['device', 'calibration', 'firmware', 'missing', 'adc'])
def test_startup_refuses_physical_disagreement_between_config_and_yaml(assembly, tmp_path, monkeypatch, damage):
    _, _, laser, _, constructed = assembly
    _monkeypatch_validation(monkeypatch)
    path = tmp_path / 'assembly.json'; path.write_text(json.dumps(laser))
    native = laser['devices']['native']['control_settings']
    cfg = dict(device_id='esp', physical=physical_block(native['native_current']), native_assembly={'laser_file': str(path)})
    if damage == 'device': cfg['device_id'] = 'other'
    elif damage == 'calibration': cfg['physical']['calibration']['current_a_per_v'] = .16
    elif damage == 'firmware': cfg['physical']['firmware_version'] = 'other'
    elif damage == 'missing': cfg.pop('physical')
    else: cfg['physical']['adc']['max_voltage'] = 3.4
    with pytest.raises(ValueError, match='another physical device|physical facts differ|physical block'):
        factory, document, _ = load_native_assembly(cfg, tmp_path / 'monitor.json')
        startup_policy(cfg, document)
    assert not constructed and json.loads(path.read_text()) == laser


def test_calibration_evidence_is_free_text_for_the_physical_comparison(assembly, tmp_path, monkeypatch):
    _, _, laser, _, _ = assembly
    _monkeypatch_validation(monkeypatch)
    path = tmp_path / 'assembly.json'; path.write_text(json.dumps(laser))
    native = laser['devices']['native']['control_settings']
    cfg = dict(device_id='esp', physical=physical_block(native['native_current']), native_assembly={'laser_file': str(path)})
    cfg['physical']['calibration']['evidence'] = 'older wording'
    policy, _, _ = startup_policy(cfg, load_native_assembly(cfg, tmp_path / 'monitor.json')[1])
    assert policy['calibration']['evidence'] == 'original'


def test_frozen_startup_definition_is_independent_of_mutable_live_target(assembly, tmp_path, monkeypatch):
    _, _, laser, _, _ = assembly
    _monkeypatch_validation(monkeypatch)
    laser['target'] = {'frequency_hz': 274589143700000.}
    frozen = tmp_path/'assembly-definition.json'
    frozen.write_text(json.dumps(laser))
    native = laser['devices']['native']['control_settings']
    cfg = dict(device_id='esp', physical=physical_block(native['native_current']), native_assembly={'laser_file': str(frozen)})
    before, _, _ = load_native_assembly(cfg, tmp_path/'monitor.json')
    changed = deepcopy(laser); changed['target']['frequency_hz'] += 20e6; frozen.write_text(json.dumps(changed))
    after, _, _ = load_native_assembly(cfg, tmp_path/'monitor.json')
    assert after.definition_revision == before.definition_revision


def test_startup_dependency_migration_drops_the_profile_and_keeps_the_policy(assembly, tmp_path, monkeypatch):
    """A journal written before step 4 (native_ff_profile + whole-snapshot assembly digest) is migrated once at start."""
    from experiment_core.errors import StaleContext
    from ldcon.owners.native_system_owner import migrate_dependencies, saved_document
    _, _, laser, _, _ = assembly
    policy = laser['devices']['native']['control_settings']['native_current']
    legacy = {'physical_definition': 'phys', 'native_ff_profile': 'ff', 'assembly_definition': digest(laser)}
    current = {'physical_definition': 'phys', 'assembly_definition': digest(assembly_wiring(laser))}
    journal = Journal(tmp_path/'restart.sqlite')
    try:
        SystemPolicyAuthority('restart', policy, dependencies=legacy, journal=journal)
        with pytest.raises(StaleContext, match='dependencies differ'):
            SystemPolicyAuthority('restart', policy, dependencies=current, journal=journal)
        state = saved_document(journal, 'restart.policy', policy, current)
        migrated = migrate_dependencies(journal, 'restart.policy', state, current)
        assert migrated == dict(before=legacy, after=current)
        restarted = SystemPolicyAuthority('restart', policy, dependencies=current, journal=journal)
        assert restarted.snapshot() == policy and restarted.dependencies == current
        # a physical change is never migrated silently
        other = dict(current, physical_definition='changed')
        assert migrate_dependencies(journal, 'restart.policy', saved_document(journal, 'restart.policy', policy, other), other) is None
    finally:
        journal.close()


def test_live_laser_yaml_is_the_policy_source_and_the_snapshot_only_a_fallback(assembly, tmp_path, monkeypatch):
    """Coordinator 2026-10-27: adopting a stale assembly snapshot at an idle start would silently change the policy."""
    import yaml
    from ldcon.owners.monitor_owner import live_laser_file
    _, _, laser, _, _ = assembly
    _monkeypatch_validation(monkeypatch)
    laser = dict(laser, id='sr1092')
    snapshot = tmp_path / 'device-owners' / 'esp' / 'assembly-definition.yaml'
    snapshot.parent.mkdir(parents=True)
    stale = deepcopy(laser)
    stale['devices']['native']['control_settings']['native_current']['current_bounds_a'] = [.030, .070]
    snapshot.write_text(yaml.safe_dump(stale))
    native = laser['devices']['native']['control_settings']
    cfg = dict(device_id='esp', physical=physical_block(native['native_current']), native_assembly={'laser_file': str(snapshot)},
               approvals_root=str(tmp_path / 'agent-approvals'))
    monkeypatch.setenv('LDCON_BACKGROUND_LASER_ID', 'sr1092')
    # no live yaml yet: the snapshot is used and the source says so
    _, document, source = load_native_assembly(cfg, tmp_path / 'device-owners' / 'esp' / 'config.json')
    assert source['kind'] == 'snapshot' and document == stale
    assert source['live_missing'].startswith(str(tmp_path / 'lasers' / 'sr1092')) and source['live_missing'].endswith('sr1092.yaml')
    # the live yaml (single-laser layout, the file configuration.save writes) wins over the snapshot
    live_root = tmp_path / 'lasers' / 'sr1092'
    (live_root / 'config' / 'lasers').mkdir(parents=True)
    (live_root / 'laser-layout.json').write_text(json.dumps({'schema': 'ldcon.single-laser-layout.v1', 'laser_id': 'sr1092'}))
    live = live_root / 'config' / 'lasers' / 'sr1092.yaml'
    live.write_text(yaml.safe_dump(laser))
    assert live_laser_file(cfg, stale) == live
    factory, document, source = load_native_assembly(cfg, tmp_path / 'device-owners' / 'esp' / 'config.json')
    assert source == dict(kind='live', path=str(live), snapshot=str(snapshot)) and document == laser
    assert startup_policy(cfg, document)[0]['current_bounds_a'] == [.045, .055]
    assert factory.definition_revision == digest(assembly_wiring(laser)) == digest(assembly_wiring(stale))
    # an explicit laser_file in the owner config takes precedence over the derivation
    explicit = tmp_path / 'explicit.yaml'
    explicit.write_text(yaml.safe_dump(laser))
    assert load_native_assembly(dict(cfg, laser_file=str(explicit)), tmp_path / 'config.json')[2]['path'] == str(explicit)
    # a live file naming another laser is refused
    other = deepcopy(laser); other['id'] = 'sr422'
    live.write_text(yaml.safe_dump(other))
    with pytest.raises(ValueError, match='names another laser'):
        load_native_assembly(cfg, tmp_path / 'device-owners' / 'esp' / 'config.json')
