"""DLC monitor authority and continuous PI cancellation, entirely offline."""
from copy import deepcopy
import threading
import time

import pytest

from experiment_core.records import atomic_json, digest
from experiment_core.errors import StaleContext
from ldcon.core.instrument_client import BrokerError
from ldcon.core.interface_schema import digest as device_digest
from ldcon.jobs.dlcpro_cards import Protocol
from ldcon.owners.dlcpro_operating_mode import attach
from .test_dlcpro_offline import rig


@pytest.fixture
def supervised(rig, tmp_path, monkeypatch):
    owner = rig['owner']
    owner.start_lock(3e14, rig['control'])
    protocol = Protocol(owner, rig['device'])
    protocol.client = 'controller'
    mode = attach(protocol, tmp_path/'authority.sqlite3')
    root = tmp_path/'trusted'
    root.mkdir()
    monkeypatch.setenv('LDCON_ADAPTIVE_ROOT', str(root))
    binding = dict(monitor_id='monitor-one', generation='generation-one', parameters_hash='frozen-plan')
    path = root/owner.laser_id/'monitors/monitor-one.json'
    path.parent.mkdir(parents=True)
    grant = dict(generation=binding['generation'], parameters_hash=binding['parameters_hash'], enabled=True,
        bounds=dict(enabled=True, controller_operation='servo.dlcpro_frequency', families=['pi'], timeout_s=60.),
        last_seen=time.time(), max_age_s=30.)
    atomic_json(path, grant)

    def call(action, **values):
        return protocol.dispatch(dict(action=action, laser_id=owner.laser_id, **values))

    def begin(**values):
        return call('pi_trial_begin', device_revision=device_digest(rig['device']), identity='task-one',
            target_revision=owner.target_revision, policy_revision=device_digest(owner.policy), watchdog_s=5.,
            **dict(expected_operating_mode_revision=0, monitor_grant=binding, **values))

    def change(revision=0, desired='characterization', identifier='mode-one'):
        value = dict(schema_version=1, laser_id=owner.laser_id, request_id=identifier,
            expected_revision=revision, desired_mode=desired, authenticated_operator='operator',
            reason='Offline authorized intent change', issued_unix_s=time.time())
        request_path = root/owner.laser_id/'operating-mode/requests'/(identifier+'.json')
        request_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(request_path, value)
        return call('operating_mode_change', request_id=identifier, request_digest=digest(value))

    yield dict(rig=rig, owner=owner, protocol=protocol, mode=mode, call=call, begin=begin,
        change=change, root=root, path=path, grant=grant, binding=binding, db=tmp_path/'authority.sqlite3')
    mode.authority.journal.close()


def test_persistent_owner_mode_read_without_control_lock_or_lease(supervised):
    s = supervised
    owner, call = s['owner'], s['call']
    s['protocol'].client = None
    before = len(s['rig']['driver'].writes)
    rows = []
    with owner.mutex:
        thread = threading.Thread(target=lambda: rows.append(call('operating_mode_status')))
        thread.start(); thread.join(1.)
        complete = not thread.is_alive()
    thread.join(1.)
    assert complete and rows[0]['mode'] == 'measurement'
    assert rows[0]['revision'] == 0 and len(s['rig']['driver'].writes) == before
    s['change']()
    other = Protocol(owner, s['rig']['device'])
    restored = attach(other, s['db'])
    assert restored.status()['mode'] == 'characterization'
    assert restored.status()['revision'] == 1
    restored.authority.journal.close()


def test_mode_change_restores_pi_immediately_and_never_replays(supervised):
    s = supervised; owner, call = s['owner'], s['call']
    initial = owner.pi_trial.settings()
    token = s['begin']()['token']
    call('pi_trial_apply', token=token, expected_revision=owner.pi_trial.revision,
         candidate=dict(kp=.7, ki_per_s=.4, integral_limit_v=.3))
    target = owner.target; output = owner.applied_pzt; writes = len(s['rig']['driver'].writes)
    result = s['change']()
    assert result['current_operating_mode']['revision'] == 1
    assert owner.pi_trial.active is None and owner.pi_trial.settings() == initial
    assert owner.pi_trial.last['reason'] == 'operating_mode_changed'
    assert owner.applied_pzt == output and owner.target == target and owner.mode == 'lock'
    assert len(s['rig']['driver'].writes) == writes
    with pytest.raises(ValueError, match='no longer active'):
        call('pi_trial_finish', token=token, adopt=True)
    newer = call('pi_trial_begin', device_revision=device_digest(s['rig']['device']), identity='task-two',
        target_revision=owner.target_revision, policy_revision=device_digest(owner.policy), watchdog_s=5.,
        expected_operating_mode_revision=1, monitor_grant=s['binding'])
    # Reuse the exact persisted request, not a newly issued authorization.
    from ldcon.core.records import read_json
    value = read_json(s['root']/owner.laser_id/'operating-mode/requests/mode-one.json')
    replay = call('operating_mode_change', request_id='mode-one', request_digest=digest(value))
    assert replay['idempotent_replay'] and owner.pi_trial.active['token'] == newer['token']


@pytest.mark.parametrize('kind', ['disabled', 'expired', 'generation', 'missing'])
def test_revoked_monitor_fences_adoption_and_restores_without_optical_wait(supervised, kind):
    s = supervised; owner, call = s['owner'], s['call']
    original = owner.pi_trial.settings()
    token = s['begin']()['token']
    call('pi_trial_apply', token=token, expected_revision=owner.pi_trial.revision,
         candidate=dict(kp=.7, ki_per_s=.4, integral_limit_v=.3))
    grant = deepcopy(s['grant'])
    if kind == 'disabled':grant['enabled'] = False
    if kind == 'expired':grant['last_seen'] -= 31
    if kind == 'generation':grant['generation'] = 'another'
    if kind == 'missing':s['path'].unlink()
    else:atomic_json(s['path'], grant)
    writes = len(s['rig']['driver'].writes)
    s['rig']['source'].missing = True
    with pytest.raises(ValueError, match='no longer active'):
        call('pi_trial_finish', token=token, adopt=True)
    assert owner.pi_trial.settings() == original
    assert owner.pi_trial.last['reason'] == 'monitor_grant_revoked'
    assert owner.mode == 'lock' and len(s['rig']['driver'].writes) == writes


def test_owner_poll_revokes_while_model_is_busy_and_bounds_total_duration(supervised):
    s = supervised; owner = s['owner']
    s['begin']()
    atomic_json(s['path'], dict(s['grant'], enabled=False))
    owner.pi_trial.poll()
    assert owner.pi_trial.active is None
    atomic_json(s['path'], s['grant'])
    s['begin']()
    # The owner deadline is independent of model heartbeat renewals.
    s['rig']['clock'].sleep(60.)
    owner.pi_trial.active['renewed_s'] = owner.clock()
    owner.pi_trial.poll()
    assert owner.pi_trial.last['reason'] == 'monitor_trial_deadline'


def test_monitor_cannot_skip_mode_fence_or_add_target_excursions(supervised):
    s = supervised; owner = s['owner']
    initial = owner.pi_trial.settings(); writes = len(s['rig']['driver'].writes)
    for revision, program in [(None, None), (-1, None), (0, dict(offsets_hz=[1e6, 0], max_cycles=1))]:
        with pytest.raises((ValueError, StaleContext)):
            s['call']('pi_trial_begin', device_revision=device_digest(s['rig']['device']), identity='task-one',
                target_revision=owner.target_revision, policy_revision=device_digest(owner.policy), watchdog_s=5.,
                expected_operating_mode_revision=revision, monitor_grant=s['binding'], target_program=program)
        assert owner.pi_trial.active is None
    assert owner.pi_trial.settings() == initial and len(s['rig']['driver'].writes) == writes


def test_untrusted_mode_change_is_rejected_without_effect(supervised):
    s = supervised; writes = len(s['rig']['driver'].writes)
    with pytest.raises(BrokerError):
        s['call']('operating_mode_change', desired_mode='characterization', approved=True)
    with pytest.raises(PermissionError):
        s['protocol'].dispatch(dict(action='operating_mode_status', laser_id='another-laser'))
    assert s['call']('operating_mode_status')['revision'] == 0
    assert len(s['rig']['driver'].writes) == writes


def test_monitor_family_bound_is_checked_before_any_output(supervised):
    s = supervised; before = len(s['rig']['driver'].writes)
    with pytest.raises(ValueError, match='outside this monitor grant'):
        s['begin'](family='current')
    assert s['owner'].pi_trial.active is None
    assert len(s['rig']['driver'].writes) == before


@pytest.mark.parametrize('family', ['current', 'slow_loop'])
def test_parameter_family_shares_single_slot_and_reset_restores_checkpoint(supervised, family):
    s = supervised; owner, call = s['owner'], s['call']
    grant = deepcopy(s['grant']); grant['bounds']['families'] = ['pi', family]
    atomic_json(s['path'], grant)
    before = call('pi_trial_state')['parameter_settings'][family]
    started = s['begin'](family=family); token = started['token']
    assert started['active']['family'] == family and started['active']['checkpoint'] == before
    with pytest.raises(ValueError, match='empty adjustment slot'):
        s['begin']()
    values = dict(current_ma=100.1) if family=='current' else dict(tec_gain=.02, tec_period_s=2., pzt_center_v=69.)
    changed = call('pi_trial_apply', token=token, expected_revision=owner.pi_trial.revision, candidate=values)
    assert changed['confirmed'] and changed['readback'] == values
    if family=='slow_loop':
        s['rig']['clock'].sleep(.1)
        owner.instrument.move(owner.channel, owner.lease, 'temperature', 25.01)
    reset = call('pi_trial_reset', token=token, expected_revision=owner.pi_trial.revision)
    assert reset['confirmed'] and reset['readback'] == before
    assert owner.pi_trial.active is not None
    if family=='slow_loop':
        assert owner.instrument.confirmed_outputs[owner.channel]['temperature'] == 25.
        assert reset['thermal_settled'] is False
    call('pi_trial_finish', token=token, adopt=False)
    assert owner.pi_trial.last['settings'] == before and owner.pi_trial.active is None
    assert owner.mode == 'lock' and owner.target == 3e14


def test_current_trial_mode_interruption_restores_without_optical_wait(supervised):
    s = supervised; owner, call = s['owner'], s['call']
    grant = deepcopy(s['grant']); grant['bounds']['families'] = ['current']
    atomic_json(s['path'], grant)
    token = s['begin'](family='current')['token']
    call('pi_trial_apply', token=token, expected_revision=owner.pi_trial.revision, candidate=dict(current_ma=100.1))
    s['rig']['source'].missing = True
    s['change']()
    assert owner.instrument.confirmed_outputs[owner.channel]['current'] == 100.
    assert owner.pi_trial.active is None and owner.mode == 'lock'
    assert owner.pi_trial.last['family'] == 'current'


def test_readonly_state_is_available_before_any_lock_has_started(rig):
    protocol = Protocol(rig['owner'], rig['device'])
    before = len(rig['driver'].writes)
    state = protocol.dispatch(dict(action='pi_trial_state', laser_id=rig['owner'].laser_id))
    assert state['mode'] == 'idle' and state['control'] == {}
    assert state['parameter_settings']['slow_loop'] is None
    assert state['parameter_capabilities']['slow_loop']['supported'] is False
    assert len(rig['driver'].writes) == before


def test_unknown_parameter_write_keeps_inspection_slot_during_grant_revocation(supervised):
    from ldcon.drivers.dlcpro import UncertainDLCWrite
    s = supervised; owner, call = s['owner'], s['call']
    grant = deepcopy(s['grant']); grant['bounds']['families'] = ['current']
    atomic_json(s['path'], grant)
    token = s['begin'](family='current')['token']
    s['rig']['driver'].fail = True
    with pytest.raises(UncertainDLCWrite):
        call('pi_trial_apply', token=token, expected_revision=owner.pi_trial.revision, candidate=dict(current_ma=100.1))
    before = len(s['rig']['driver'].writes)
    atomic_json(s['path'], dict(grant,enabled=False))
    s['rig']['clock'].sleep(6.)
    owner.tick()
    assert owner.fault and owner.pi_trial.active['inspection_required'] is True
    assert len(s['rig']['driver'].writes) == before
    assert owner.pi_trial.last is None
