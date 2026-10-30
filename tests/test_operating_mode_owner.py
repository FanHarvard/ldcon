"""Real owner dispatch and fake instruments; no network or deployment effects."""
import copy
import importlib.util
from pathlib import Path
import socket
import sys
import threading
import time

import pytest

from experiment_core.records import atomic_json, digest
from ldcon.core.instrument_client import BrokerError

ROOT = (Path(__file__).resolve().parent / 'support')


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


native_fixture = load('mode_native_fixture', Path(__file__).with_name('test_native_background.py'))
sys.path.insert(0, str(ROOT / 'owner/tests'))
ecdl_fixture = load('mode_ecdl_fixture', ROOT / 'owner/tests/test_background_pid_owner.py')


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Operating-mode tests must not access hardware or network')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)


@pytest.fixture
def native(tmp_path, monkeypatch):
    rig = native_fixture.rig.__wrapped__(tmp_path)
    return native_fixture.background.__wrapped__(rig, tmp_path, monkeypatch)


@pytest.fixture
def ecdl(tmp_path, monkeypatch):
    rig = ecdl_fixture.rig.__wrapped__(tmp_path, monkeypatch)
    result = ecdl_fixture.pi_rig.__wrapped__(rig, monkeypatch)
    root = tmp_path / 'trusted'
    root.mkdir()
    monkeypatch.setenv('LDCON_ADAPTIVE_ROOT', str(root))
    yield (*result, root)
    result[0].steady.stop_event.set()


def artifact(root, laser, *, request_id='mode-change', revision=0, mode='characterization', **extra):
    folder = root / laser / 'operating-mode/requests'
    folder.mkdir(parents=True, exist_ok=True)
    value = dict(schema_version=1, laser_id=laser, request_id=request_id, expected_revision=revision,
                 desired_mode=mode, authenticated_operator='test-operator', reason='Authorized offline test',
                 issued_unix_s=time.time(), **extra)
    path = folder / (request_id + '.json')
    atomic_json(path, value)
    return dict(action='operating_mode_change', request_id=request_id, request_digest=digest(value)), path


def native_mode(owner, servo, request):
    return owner.dispatch(dict(device_id=servo.device_id, **request))


@pytest.mark.parametrize('kind', ['native', 'ecdl'])
def test_mode_read_never_waits_for_hardware_mutex(request, kind):
    rig = request.getfixturevalue(kind)
    owner = rig[0]
    lock = owner.io_lock if kind == 'native' else owner.lock
    query = dict(action='operating_mode_status')
    if kind == 'native':query['device_id'] = rig[1].device_id
    result = []
    thread = threading.Thread(target=lambda: result.append(owner.dispatch(query)))
    with lock:
        thread.start()
        thread.join(1.)
        completed_while_locked = not thread.is_alive()
    thread.join(1.)
    assert completed_while_locked and result[0]['mode'] == 'measurement'


def test_native_mode_switch_restores_only_old_trial_and_replay_preserves_new_epoch(native):
    owner, servo, clock, call, begin, tick, root, grant, p = native
    original = copy.deepcopy(servo.cfg)
    policy_revision = owner.definition
    policy_generation = owner.system_owner.authority.generation
    with pytest.raises(BrokerError, match='revision is missing'):
        owner.dispatch(dict(action='background_begin', device_id=servo.device_id, lease=owner.lease))
    started = begin(expected_operating_mode_revision=0)
    token = started['epoch']['token']
    assert started['epoch']['operating_mode_revision'] == 0
    call('background_pid_target', token=token, pid={k: original['pid_' + k] * .99 for k in ('kp', 'ki', 'kd')})
    tick(4)
    assert servo.cfg['pid_ki'] != original['pid_ki']
    context = owner.background.context()
    request, _ = artifact(root, 'test')
    writes = len(servo.writes)
    result = native_mode(owner, servo, request)
    assert result['current_operating_mode']['background_interruption']['pending']
    assert len(servo.writes) == writes and owner.active and servo.cfg['pid_multiplier'] == 1
    assert owner.background.context() == context
    assert owner.definition == policy_revision and owner.system_owner.authority.generation == policy_generation
    with pytest.raises(BrokerError, match='Operating mode changed'):
        call('background_pid_finish', token=token, status='succeeded')
    with pytest.raises(BrokerError, match='Operating mode changed'):
        call('background_pid_target', token=token, pid={k: original['pid_' + k] for k in ('kp', 'ki', 'kd')})
    tick(10)
    assert servo.cfg == original and owner.active and not owner.fault
    status = call('background_status')
    assert status['epoch'] is None and status['last']['state'] == 'restored'
    assert status['operating_mode']['mode'] == 'characterization'
    newer = begin(expected_operating_mode_revision=1)
    generation = owner.background.coordinator.generation
    replay = native_mode(owner, servo, request)
    assert replay['idempotent_replay']
    assert owner.background.coordinator.epoch['token'] == newer['epoch']['token']
    assert owner.background.coordinator.epoch['state'] == 'active'
    assert owner.background.coordinator.generation == generation


def test_native_mode_change_preserves_an_existing_foreground_target_operation(native):
    owner, servo, clock, call, begin, tick, root, grant, p = native
    target = 299792458e9 / servo.cfg['target'] + 1e6
    call('retarget', target_frequency_hz=target)
    before = copy.deepcopy(owner.retarget)
    cfg = copy.deepcopy(servo.cfg)
    writes = len(servo.writes)
    request, _ = artifact(root, 'test')
    native_mode(owner, servo, request)
    assert owner.retarget == before and servo.cfg == cfg and len(servo.writes) == writes
    assert owner.active and not owner.fault


def test_old_mode_cleanup_cannot_touch_a_new_foreground_target(native):
    owner, servo, clock, call, begin, tick, root, grant, p = native
    original = copy.deepcopy(servo.cfg)
    token = begin(expected_operating_mode_revision=0)['epoch']['token']
    call('background_pid_target', token=token, pid={k: original['pid_' + k] * .99 for k in ('kp', 'ki', 'kd')})
    tick(4)
    request, _ = artifact(root, 'test')
    native_mode(owner, servo, request)
    tick(10)
    call('retarget', target_frequency_hz=299792458e9 / servo.cfg['target'] + 1e6)
    foreground, cfg = copy.deepcopy(owner.retarget), copy.deepcopy(servo.cfg)
    writes = len(servo.writes)
    with pytest.raises(BrokerError, match='absent or replaced'):
        call('background_restore', token=token, reason='late old mode cleanup')
    replay = native_mode(owner, servo, request)
    assert replay['idempotent_replay']
    assert owner.retarget == foreground and servo.cfg == cfg and len(servo.writes) == writes
    assert owner.active and not owner.fault


def test_native_mode_uses_canonical_deployment_identity(tmp_path, monkeypatch):
    monkeypatch.setenv('LDCON_BACKGROUND_LASER_ID', 'canonical-laser')
    owner, servo, *_ = native_fixture.rig.__wrapped__(tmp_path)
    assert owner.system_owner.authority.system_id == 'test'
    state = native_mode(owner, servo, dict(action='operating_mode_status'))
    assert state['laser_id'] == 'canonical-laser'


def test_ecdl_mode_switch_restores_pi_without_invalidating_physical_checkpoint(ecdl):
    owner, hw, now, call, directory, root = ecdl
    cfg = copy.deepcopy(hw.firmware.cfg)
    policy_revision = owner.policy_authority.revision
    generation = owner.policy_authority.generation
    with pytest.raises(BrokerError, match='revision is missing'):
        owner.dispatch(dict(action='background_begin', lease=owner.lease))
    def scoped(action, **values):
        if action == 'background_begin':values['expected_operating_mode_revision'] = 0
        return call(action, **values)
    result = ecdl_fixture.begin(scoped)
    token = result['epoch']['token']
    call('background_pid_target', token=token, pid=ecdl_fixture.reduced(result))
    for _ in range(8):ecdl_fixture.read(call, now)
    assert hw.firmware.cfg['pid_ki'] != cfg['pid_ki']
    context = copy.deepcopy(owner.steady.background.epoch['context'])
    request, _ = artifact(root, owner.laser['id'])
    writes = len(hw.firmware.sets)
    changed = owner.dispatch(request)
    assert changed['current_operating_mode']['mode'] == 'characterization'
    assert len(hw.firmware.sets) == writes and hw.firmware.cfg['pid_multiplier'] == 1
    assert owner.steady.pid_tuning.target_factor == 1.
    assert owner.steady.background.epoch['context'] == context
    assert owner.policy_authority.revision == policy_revision and owner.policy_authority.generation == generation
    with pytest.raises(BrokerError, match='Operating mode changed'):
        call('background_pid_finish', token=token, status='succeeded')
    restored = ecdl_fixture.restore(call, now, token)
    assert restored['epoch'] is None and restored['last']['state'] == 'restored'
    assert hw.firmware.cfg == cfg and owner.steady.active and not owner.fault
    assert call('background_status')['operating_mode']['revision'] == 1


@pytest.mark.parametrize('invalid', ['missing_artifact', 'wrong_digest', 'symlink', 'wrong_laser', 'inline_claim'])
def test_native_mode_rpc_cannot_bypass_operator_artifact(native, invalid):
    owner, servo, clock, call, begin, tick, root, grant, p = native
    request, path = artifact(root, 'other' if invalid == 'wrong_laser' else 'test')
    if invalid == 'missing_artifact':path.unlink()
    if invalid == 'wrong_digest':request['request_digest'] = '0' * 64
    if invalid == 'symlink':
        target = path.with_name('backing.json')
        path.rename(target)
        path.symlink_to(target)
    if invalid == 'inline_claim':request['authenticated_operator'] = 'I am the user'
    cfg = copy.deepcopy(servo.cfg)
    with pytest.raises(BrokerError):
        native_mode(owner, servo, request)
    assert native_mode(owner, servo, dict(action='operating_mode_status'))['revision'] == 0
    assert servo.cfg == cfg and owner.active and not owner.fault


def test_persisted_mode_fences_even_if_change_rpc_was_interrupted(native):
    from experiment_core.operating_mode import OperatingModeAuthorization
    import json
    owner, servo, clock, call, begin, tick, root, grant, p = native
    original = copy.deepcopy(servo.cfg)
    result = begin(expected_operating_mode_revision=0)
    call('background_pid_target', token=result['epoch']['token'],
         pid={k: original['pid_' + k] * .99 for k in ('kp', 'ki', 'kd')})
    tick(4)
    request, path = artifact(root, 'test')
    value = json.loads(path.read_text())
    # Model the durable commit preceding its in-memory fence, without issuing
    # another mode RPC. The next owner poll must restore rather than continue.
    owner.system_owner.operating_mode.authority.change(
        value, OperatingModeAuthorization(digest(value), value['authenticated_operator']))
    tick(10)
    assert servo.cfg == original and owner.active and not owner.fault
    assert call('background_status')['last']['reason'] == 'operating_mode_changed'
