"""Rule M step 1 (2026-10-27): sr422 operating conditions change without an instruments restart.

(i)   the authority's calibration dependency excludes the axis timing, so a changed update_interval_s / settle_s
      installs its projection and survives an owner restart without StaleContext (and a pre-step-1 journal migrates);
(ii)  axis timing, locked_sweep.**, sweep_api_defaults.** and the temperature_compensation switch are owner_hot;
(iii) settings_adopt: lease-less adoption now, deferred to the owner's own safe boundary under a lease, refused for a
      FORBIDDEN mix; configuration.save sends it to the rig owner the way the native owner is called.

Offline: the real ECDLOwner/RigHardware code over the SCPI/firmware emulator of owner/tests/fake_rig.py.
"""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest
import yaml

from experiment_core.errors import StaleContext
from experiment_core.records import Journal
from ldcon.core.agent_authority import approval_root, describe_changes, live_class
from ldcon.owners.ecdl_owner import ECDLOwner, calibration_dependency, policy_dependencies
from ldcon.core.instrument_client import BrokerError
from ldcon.core.interface_schema import digest
from ldcon.owners.owner_projection import SystemPolicyAuthority

_spec = importlib.util.spec_from_file_location('fake_rig', (Path(__file__).resolve().parent / 'support') / 'owner/tests/fake_rig.py')
fake_rig = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fake_rig)

TIMING = 'devices.rig.control_settings.axes.temperature.update_interval_s'
SPAN = 'devices.rig.control_settings.locked_sweep.limits.max_half_span_hz'


def make_owner(tmp_path, laser=None, clock=lambda: 100.):
    path = tmp_path / 'lasers' / 'sr422.yaml'
    if laser is None:
        laser = fake_rig.laser_definition()
        path.parent.mkdir()
        path.write_text(yaml.safe_dump(laser))
    return ECDLOwner(laser, audit_path=tmp_path / 'audit.jsonl', laser_file=path,
                     factory=lambda device: fake_rig.FakeRig(device, clock), clock=clock)


@pytest.fixture
def owner(tmp_path, monkeypatch):
    import ldcon.drivers.siglent as siglent
    monkeypatch.setattr(siglent, 'VisaTransport', fake_rig.Visa)
    owner = make_owner(tmp_path)
    owner.hw()
    yield owner
    owner.close(hold=False)


def save(owner, candidate):
    owner.laser_file.write_text(yaml.safe_dump(candidate))
    return hashlib.sha256(owner.laser_file.read_bytes()).hexdigest()


def decision(owner, before, candidate, identifier='a' * 32):
    root = approval_root(owner.laser_file.parent.parent, owner.laser['id']) / 'decisions'
    root.mkdir(parents=True, exist_ok=True)
    value = dict(id=identifier, laser_id=owner.laser['id'], scope=dict(kind='lasers', name=owner.laser['id']),
                 authority='human_approval', revision_after=hashlib.sha256(owner.laser_file.read_bytes()).hexdigest(),
                 changes=describe_changes('lasers', before, candidate))
    (root / (identifier + '.json')).write_text(json.dumps(value))
    return identifier


def audit_kinds(owner):
    return [json.loads(line)['kind'] for line in owner.audit_path.read_text().splitlines()]


def projection(owner, name):
    physical = owner.hardware.connections[name]._physical
    return physical.guard.inspect(physical.binding)


# ---- (ii) live classes -----------------------------------------------------------------------------------------

@pytest.mark.parametrize('path,expected', [
    (TIMING, 'owner_hot'),
    ('devices.rig.control_settings.axes.piezo.settle_s', 'owner_hot'),
    (SPAN, 'owner_hot'),
    ('devices.rig.control_settings.locked_sweep.awg.ch2_bounds_v', 'owner_hot'),
    ('devices.rig.control_settings.sweep_api_defaults.continuous.step_hz', 'owner_hot'),
    ('devices.rig.control_settings.sweep_api_defaults.ldc_ff_v_per_hz', 'owner_hot'),
    ('devices.rig.control_settings.temperature_compensation.enabled', 'owner_hot'),
    ('devices.rig.control_settings.temperature_compensation.minimum_interval_s', 'owner_hot'),
    ('devices.rig.control_settings.temperature_compensation.ohm_per_v', 'idle_only'),   # calibration stays forbidden
    ('devices.rig.control_settings.axes.temperature.frequency_hz_per_unit', 'idle_only'),
    ('devices.rig.control_settings.steady_lock.tec_gain', 'snapshot'),
])
def test_operating_conditions_are_classed_hot(path, expected):
    assert live_class('lasers', tuple(path.split('.'))) == expected


# ---- (i) timing + sweep limits under a lease, projection install, restart --------------------------------------

def test_timing_and_sweep_limit_adopt_under_lease_without_restart(owner, monkeypatch):
    import ldcon.drivers.siglent as siglent
    before = owner.laser
    lease = owner.dispatch(dict(action='claim', revision=digest(owner.device), owner='test'))['lease']
    dependencies = owner.policy_authority.dependencies
    old_piezo = projection(owner, 'piezo')['projection_revision']
    old_tec = projection(owner, 'tec_awg')['projection_revision']
    candidate = copy.deepcopy(before)
    rig = candidate['devices']['rig']
    rig['control_settings']['axes']['temperature']['update_interval_s'] = 2.
    rig['control_settings']['axes']['piezo']['update_interval_s'] = .2
    rig['control_settings']['locked_sweep']['limits']['max_half_span_hz'] = 5e8
    rig['control_settings']['temperature_compensation']['minimum_interval_s'] = .02
    save(owner, candidate)
    rows = describe_changes('lasers', before, candidate)
    assert {r['live'] for r in rows} == {'owner_hot'}
    identifier = decision(owner, before, candidate)
    result = owner.dispatch(dict(action='settings_reload', lease=lease, decision_id=identifier, device=rig,
                                 paths=[r['path'].split('.', 2)[2] for r in rows]))
    assert sorted(result['applied']) == ['control_settings.axes.piezo.update_interval_s',
                                         'control_settings.axes.temperature.update_interval_s',
                                         'control_settings.locked_sweep.limits.max_half_span_hz',
                                         'control_settings.temperature_compensation.minimum_interval_s']
    assert result['deferred'] == [] and result['snapshot'] == []
    assert owner.device == rig and owner.fault is None
    # derived policy (R1): the piezo slew (max_slew_per_s null) follows the new update interval
    assert owner.policies()['temperature'].update_interval_s == 2.
    assert owner.policies()['piezo'].max_slew_per_s == pytest.approx(65535. / .2)
    assert result['policies']['piezo']['max_slew_per_s'] == pytest.approx(65535. / .2)
    # projections moved to the new revision with the same dependencies; nothing was written to the firmware
    assert owner.policy_authority.dependencies == dependencies
    assert projection(owner, 'piezo')['projection_revision'] != old_piezo
    assert projection(owner, 'tec_awg')['projection_revision'] != old_tec
    assert projection(owner, 'piezo')['projection']['constraints']['sweep']['limits']['max_half_span_hz'] == 5e8
    assert projection(owner, 'piezo')['projection']['dependencies'] == dependencies
    assert owner.hardware.firmware.sets == []
    assert owner.dispatch(dict(action='describe'))['policy_adoption']['pending'] is False
    assert owner.dispatch(dict(action='release', lease=lease)) is True
    owner.close(hold=False)
    # (i) a restart on the same authority journal with the saved definition: no StaleContext, same dependencies
    restarted = make_owner(owner.laser_file.parent.parent, laser=candidate)
    try:
        assert restarted.policy_authority.dependencies == dependencies
        assert restarted.policies()['temperature'].update_interval_s == 2.
    finally:
        restarted.close(hold=False)


def test_timing_change_is_deferred_while_a_trajectory_is_active(owner):
    before = owner.laser
    lease = owner.dispatch(dict(action='claim', revision=digest(owner.device), owner='test'))['lease']
    candidate = copy.deepcopy(before)
    candidate['devices']['rig']['control_settings']['axes']['current']['settle_s'] = .7
    candidate['devices']['rig']['control_settings']['sweep_api_defaults'] = dict(continuous=dict(max_s=600., step_hz=2e6))
    save(owner, candidate)
    rows = describe_changes('lasers', before, candidate)
    identifier = decision(owner, before, candidate)
    owner.recovery = dict(before['devices']['rig']['absolute_conditions']['axes'])   # _busy: current recovery
    result = owner.dispatch(dict(action='settings_reload', lease=lease, device=candidate['devices']['rig'],
                                 decision_id=identifier, paths=[r['path'].split('.', 2)[2] for r in rows]))
    assert sorted(result['deferred']) == ['control_settings.axes.current.settle_s',
                                          'control_settings.sweep_api_defaults.continuous.max_s',
                                          'control_settings.sweep_api_defaults.continuous.step_hz']
    assert result['applied'] == [] and owner.device == before['devices']['rig']
    owner.recovery = None
    result = owner.dispatch(dict(action='settings_reload', lease=lease, device=candidate['devices']['rig'],
                                 decision_id=identifier, paths=[r['path'].split('.', 2)[2] for r in rows]))
    assert sorted(result['applied']) == ['control_settings.axes.current.settle_s',
                                         'control_settings.sweep_api_defaults.continuous.max_s',
                                         'control_settings.sweep_api_defaults.continuous.step_hz']
    assert owner.device == candidate['devices']['rig'] and owner.policies()['current'].settle_s == .7


def test_calibration_dependency_ignores_timing_but_not_calibration():
    axes = fake_rig.laser_definition()['devices']['rig']['control_settings']['axes']
    changed = copy.deepcopy(axes)
    changed['temperature']['update_interval_s'] = 3.
    changed['piezo']['settle_s'] = 1.
    assert calibration_dependency(changed) == calibration_dependency(axes)
    changed['temperature']['frequency_hz_per_unit'] = 1.
    assert calibration_dependency(changed) != calibration_dependency(axes)


def test_pre_step1_journal_migrates_once_and_a_real_calibration_change_still_refuses(tmp_path, monkeypatch):
    import ldcon.drivers.siglent as siglent
    monkeypatch.setattr(siglent, 'VisaTransport', fake_rig.Visa)
    laser = fake_rig.laser_definition()
    path = tmp_path / 'lasers' / 'sr422.yaml'
    path.parent.mkdir()
    path.write_text(yaml.safe_dump(laser))
    rig = laser['devices']['rig']
    legacy = dict(connection=digest(rig['connection']), calibration=digest(rig['control_settings']['axes']))
    journal = Journal(tmp_path / 'audit.authority.sqlite3')
    SystemPolicyAuthority(laser['id'], laser, dependencies=legacy, journal=journal, revision_function=digest)
    del journal
    owner = make_owner(tmp_path, laser=laser)
    try:
        assert owner.policy_authority.dependencies == policy_dependencies(laser) != legacy
        assert audit_kinds(owner) == ['policy_dependencies_migrated']
    finally:
        owner.close(hold=False)
    # a second start finds the migrated journal (no second migration); a changed calibration at an IDLE start is the
    # explicit idle configuration transition (rule M step 4 for the rig owner, 2026-10-27): adopted, audited,
    # dependencies updated -- no longer a StaleContext refusal (that refusal remains for a persisted lock context,
    # see test_ecdl_owner_startup_adoption.py).
    again = make_owner(tmp_path, laser=laser)
    again.close(hold=False)
    assert audit_kinds(again).count('policy_dependencies_migrated') == 1
    changed = copy.deepcopy(laser)
    changed['devices']['rig']['control_settings']['axes']['temperature']['frequency_hz_per_unit'] = 1.
    adopted = make_owner(tmp_path, laser=changed)
    adopted.close(hold=False)
    assert adopted.startup['state'] == 'adopted' and adopted.laser == changed
    assert adopted.policy_authority.dependencies == policy_dependencies(changed) != policy_dependencies(laser)
    assert audit_kinds(adopted)[-1] == 'policy_adopted_at_start'


# ---- (iii) settings_adopt -----------------------------------------------------------------------------------------

def test_settings_adopt_without_lease_adopts_the_saved_definition_now(owner):
    before = owner.laser
    candidate = copy.deepcopy(before)
    candidate['devices']['rig']['control_settings']['optical']['single_mode_max']['side_peak_height_max'] *= .9
    revision = save(owner, candidate)
    with pytest.raises(BrokerError, match='revision differs'):
        owner.dispatch(dict(action='settings_adopt', saved_revision='0' * 64))
    assert owner.laser == before
    result = owner.dispatch(dict(action='settings_adopt', saved_revision=revision))
    assert result['status'] == 'adopted' and result['pending'] is False
    assert owner.laser == candidate and owner.lease is None and owner.fault is None
    assert owner.dispatch(dict(action='describe'))['policy_adoption']['pending'] is False
    assert owner.dispatch(dict(action='settings_adopt'))['status'] == 'unchanged'
    assert 'settings_adopted' in audit_kinds(owner)
    assert owner.hardware.firmware.sets == []


def test_settings_adopt_without_lease_keeps_the_exact_decision_gate(owner):
    before = owner.laser
    candidate = copy.deepcopy(before)
    candidate['devices']['rig']['absolute_conditions']['axes']['current']['max_step'] *= .9
    revision = save(owner, candidate)
    with pytest.raises(BrokerError, match='exact trusted decision'):
        owner.dispatch(dict(action='settings_adopt', saved_revision=revision))
    assert owner.laser == before
    identifier = decision(owner, before, candidate)
    result = owner.dispatch(dict(action='settings_adopt', saved_revision=revision, decision_id=identifier))
    assert result['status'] == 'adopted' and owner.laser == candidate
    assert owner.policies()['current'].max_step == pytest.approx(before['devices']['rig']['absolute_conditions']['axes']['current']['max_step'] * .9)


def test_settings_adopt_refuses_a_forbidden_mix(owner):
    before = owner.laser
    candidate = copy.deepcopy(before)
    candidate['devices']['rig']['control_settings']['optical']['single_mode_max']['side_peak_height_max'] *= .9
    candidate['devices']['rig']['control_settings']['axes']['temperature']['identity'] = 'Other/temperature'
    save(owner, candidate)
    with pytest.raises(BrokerError, match='identity or calibration'):
        owner.dispatch(dict(action='settings_adopt'))
    assert owner.laser == before and owner.fault is None


def test_settings_adopt_under_a_lease_is_deferred_to_the_owner_boundary(owner):
    before = owner.laser
    lease = owner.dispatch(dict(action='claim', revision=digest(owner.device), owner='test'))['lease']
    candidate = copy.deepcopy(before)
    candidate['devices']['rig']['control_settings']['optical']['single_mode_max']['side_peak_height_max'] *= .9
    candidate['devices']['rig']['control_settings']['locked_sweep']['limits']['max_duration_s'] = 30.
    revision = save(owner, candidate)
    identifier = decision(owner, before, candidate)
    result = owner.dispatch(dict(action='settings_adopt', saved_revision=revision, decision_id=identifier))
    assert result['status'] == 'deferred' and result['pending'] is True
    assert owner.laser == before
    state = owner.dispatch(dict(action='describe'))['policy_adoption']
    assert state['pending'] is True and state['adoption_request']['decision_id'] == identifier
    # a busy owner (current recovery in progress) keeps the request at its boundary
    owner.recovery = dict(before['devices']['rig']['absolute_conditions']['axes'])
    owner.dispatch(dict(action='read', lease=lease))
    assert owner.adoption_request is not None and owner.laser == before
    owner.recovery = None
    # the next read-only lease-holder request is the safe boundary: adopted, projection installed, no writes
    owner.dispatch(dict(action='read', lease=lease))
    assert owner.adoption_request is None
    assert owner.laser == candidate and owner.fault is None
    assert projection(owner, 'piezo')['projection']['constraints']['sweep']['limits']['max_duration_s'] == 30.
    assert owner.hardware.firmware.sets == []
    assert audit_kinds(owner)[-2:] == ['settings_reloaded', 'settings_adopt_applied']
    assert owner.dispatch(dict(action='describe'))['policy_adoption']['pending'] is False
    # a lease-holder request by another lease is still refused before the boundary runs
    with pytest.raises(BrokerError, match='lease'):
        owner.dispatch(dict(action='read', lease='other'))


def test_deferred_adoption_without_decision_is_refused_once_and_recorded(owner):
    before = owner.laser
    lease = owner.dispatch(dict(action='claim', revision=digest(owner.device), owner='test'))['lease']
    candidate = copy.deepcopy(before)
    candidate['devices']['rig']['control_settings']['max_sample_age_s'] = 2.
    save(owner, candidate)
    assert owner.dispatch(dict(action='settings_adopt'))['status'] == 'deferred'
    assert owner.dispatch(dict(action='read', lease=lease))['open_loop']
    assert owner.adoption_request is None and owner.laser == before
    assert 'settings_adopt_refused' in audit_kinds(owner)


# ---- configuration.save -> rig owner ---------------------------------------------------------------------------------

def test_configuration_save_sends_settings_adopt_to_the_rig_owner(tmp_path, monkeypatch):
    from ldcon.core import state_layout as paths
    from ldcon.core.configuration import save as save_configuration
    from ldcon.core.interface_schema import InterfaceRepository
    monkeypatch.delenv('LDCON_MAINTAINER_TOKEN', raising=False)
    laser = fake_rig.laser_definition()
    path = paths.config_file(tmp_path, 'lasers', 'sr422')
    path.parent.mkdir(parents=True)
    path.write_text(yaml.safe_dump(laser))
    for kind in ('assignments', 'interface-decks'):
        (tmp_path / kind).mkdir()
    repo = InterfaceRepository(tmp_path)
    runtime = SimpleNamespace(root=tmp_path, mutex=threading.RLock(), interface_repository=repo,
                              active_jobs=lambda: [], assert_admission_open=lambda: None)
    calls = []

    def rpc(endpoint, request):
        calls.append((endpoint, request))
        if request['action'] == 'policy_update_status':
            return dict(status='absent')
        return dict(status='adopted', pending=False)

    after = copy.deepcopy(laser)
    after['devices']['rig']['control_settings']['optical']['single_mode_max']['side_peak_height_max'] *= .9
    result = save_configuration(runtime, 'lasers', 'sr422', yaml.safe_dump(after), repo.read('lasers', 'sr422')['revision'],
                                native_rpc=rpc)
    assert result['live']['owner'] == dict(rig=dict(status='adopted', pending=False))
    endpoint, request = [c for c in calls if c[1]['action'] == 'ecdl'][0]
    assert endpoint == laser['devices']['rig']['connection']['endpoint']
    assert request['instrument'] == 'sr422'
    assert request['request'] == dict(action='settings_adopt', saved_revision=result['revision'], decision_id=None)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == result['revision']
    # an unreachable owner never fails the save: the file is published, the refusal is reported
    def down(endpoint, request):
        if request['action'] == 'ecdl':
            raise OSError('connection refused')
        return dict(status='absent')
    later = copy.deepcopy(after)
    later['name'] = 'Sr422 renamed'
    result = save_configuration(runtime, 'lasers', 'sr422', yaml.safe_dump(later), result['revision'], native_rpc=down)
    assert result['live']['owner']['rig']['status'] == 'unavailable'
    assert yaml.safe_load(path.read_text())['name'] == 'Sr422 renamed'
