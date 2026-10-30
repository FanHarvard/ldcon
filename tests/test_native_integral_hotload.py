"""Integral output-limit policy changes use fake hardware and protected decisions."""
import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ldcon.owners.esp32_native_owner import NativeCurrentOwner
from ldcon.core.instrument_client import BrokerError
from ldcon.owners.native_current_tuning import effective_policy
from ldcon.owners.native_policy_update import journal_path, check_journal

_spec = importlib.util.spec_from_file_location('integral_hotload_fixture',
    (Path(__file__).resolve().parent / 'support') / 'jobs/tests/test_esp32_native_current.py')
_fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixture)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    p = _fixture.policy()
    p['optical']['expected_laser'] = 'Sr1092'
    p['activation_range_a'] = [-.003, .003]
    p['native'].update(kp_count_per_nm=-100., ki_count_per_nm_s=-21069.129507383714,
                       integral_limit_count=45.)
    monkeypatch.setattr(_fixture, 'policy', lambda: copy.deepcopy(p))
    monkeypatch.delenv('LDCON_BACKGROUND_LASER_ID', raising=False)
    result = _fixture.rig.__wrapped__(tmp_path)
    result[0].system_owner.assemblies.factory = SimpleNamespace(laser_snapshot=dict(
        id='sr1092', interfaces=dict(native_current=dict(device='native')),
        devices=dict(native=dict(type='esp32servocon.native_current.owner',
                                connection=dict(device_id=result[0].driver.device_id)))))
    return result


def decision(rig, tmp_path, *, limit=100., authority='free', laser='sr1092', extra=None):
    owner = rig[0]
    owner.policy_config_file = tmp_path / 'owner.json'
    owner.policy_config_file.write_text(json.dumps(dict(native_current=owner.policy)))
    owner.approvals_root = tmp_path / 'approvals'
    rows = [dict(path='devices.native.control_settings.native_current.native.integral_limit_count',
                 old=owner.policy['native']['integral_limit_count'], new=limit)]
    rows.extend(extra or [])
    value = dict(schema_version=1, id='a'*32, laser_id=laser, authority=authority,
                 scope=dict(kind='lasers', name=laser), changes=rows)
    path = owner.approvals_root / laser / 'decisions' / (value['id'] + '.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    owner._persist()
    return dict(action='policy_update', device_id=owner.driver.device_id, expected_definition=owner.definition,
                decision_id=value['id'], laser_id=laser)


def arm(rig, *, adopted=False):
    owner, servo, clock, bias, optical, gate, call, ready = rig
    ready(); call('arm'); clock.advance()
    # Model a previously verified runtime-only optimization, not desired policy.
    if adopted:
        servo.cfg['pid_kp'] *= .8
        servo.cfg['pid_ki'] *= .5
    servo.cfg.update(pid_integral_min=-54./abs(servo.cfg['pid_ki']),
                     pid_integral_max=54./abs(servo.cfg['pid_ki']))
    owner.expected_config.update({key: servo.cfg[key] for key in owner.expected_config})
    return owner, servo


@pytest.mark.parametrize('adopted', [False, True])
def test_exact_limit_hot_applies_without_reset_and_preserves_next_prepare(rig, tmp_path, adopted):
    owner, servo = arm(rig, adopted=adopted)
    original = copy.deepcopy(servo.cfg)
    request = decision(rig, tmp_path)
    writes = copy.deepcopy(servo.writes)
    before_state = owner.state_file.read_bytes()
    prepared = owner.dispatch(dict(request, action='policy_update_prepare'))
    assert prepared['status'] == 'prepared' and not prepared['hardware_writes']
    assert owner.state_file.read_bytes() == before_state
    assert prepared['integral_limit_transition']['before_limit_count'] == pytest.approx(54.)
    assert prepared['integral_limit_transition']['maximum_clamp_count'] == 0
    assert servo.writes == writes and not journal_path(owner.state_file).exists()
    result = owner.dispatch(request)
    assert result['integral_limit_applied'] and result['hardware_gains_applied']
    assert not result['gains_pending'] and owner.active and owner.lease and owner.fault is None
    assert len(servo.writes) == len(writes) + 1
    assert set(servo.writes[-1]) == {'pid_integral_min', 'pid_integral_max'}
    for key in original:
        if key not in ('pid_integral_min', 'pid_integral_max'):
            assert servo.cfg[key] == original[key]
    assert servo.cfg['pid_integral_max'] * abs(servo.cfg['pid_ki']) == pytest.approx(100.)
    assert servo.cfg['pid_integral_min'] == -servo.cfg['pid_integral_max']
    assert result['gain_state']['actual']['integral_limit_count'] == pytest.approx(100.)
    n = effective_policy(owner.policy, owner.saved_pid)['native']
    assert n['integral_limit_count'] == pytest.approx(100.)
    assert n['ki_count_per_nm_s'] == original['pid_ki']
    assert n['kp_count_per_nm'] == original['pid_kp']
    durable = json.loads(owner.state_file.read_text())
    assert durable['saved_pid'] == owner.saved_pid
    journal = json.loads(journal_path(owner.state_file).read_text())
    assert journal['hardware_before'] == original
    assert journal['hardware_after'] == servo.cfg and journal['status'] == 'committed'
    count = len(servo.writes)
    assert owner.dispatch(request)['idempotent'] and len(servo.writes) == count
    # A later ordinary tuning save must retain the explicit raw-I reference.
    rig[-2]('pid_begin')
    rig[-2]('pid_save', pid={key: servo.cfg['pid_'+key] for key in ('kp', 'ki', 'kd')})
    rig[-2]('pid_finish', status='unchanged')
    assert effective_policy(owner.policy, owner.saved_pid)['native']['integral_limit_count'] == pytest.approx(100.)
    # A deliberate stop/release and a new owner retain the same next-prepare PI.
    rig[-2]('stop'); rig[-2]('release')
    restarted = NativeCurrentOwner(servo, .2, 1., None, owner.policy, owner.state_file,
        clock=rig[2], optical=rig[4], bias=rig[3], gate=rig[5])
    def call(action, **values):
        return restarted.dispatch(dict(action=action, device_id=servo.device_id, lease=restarted.lease, **values))
    call('claim', policy_digest=restarted.definition)
    call('prepare', target_wavelength_nm=original['target'])
    assert servo.cfg['pid_ki'] == original['pid_ki'] and servo.cfg['pid_kp'] == original['pid_kp']
    assert servo.cfg['pid_integral_max'] * abs(servo.cfg['pid_ki']) == pytest.approx(100.)


@pytest.mark.parametrize('damage', ['range', 'wrong_laser', 'old', 'mixed_free', 'target', 'pid', 'stale', 'interval'])
def test_refusals_do_not_write_hardware_or_policy(rig, tmp_path, damage):
    owner, servo = arm(rig)
    options = {'limit': 1000.} if damage == 'range' else {'laser': 'Sr1092'} if damage == 'wrong_laser' else {}
    if damage == 'mixed_free':
        options['extra'] = [dict(path='devices.native.control_settings.native_current.current_bounds_a',
                                old=owner.policy['current_bounds_a'], new=[.04, .06])]
    request = decision(rig, tmp_path, **options)
    if damage == 'old':
        path = owner.approvals_root / 'sr1092' / 'decisions' / ('a'*32+'.json')
        value = json.loads(path.read_text()); value['changes'][0]['old'] = 54.; path.write_text(json.dumps(value))
    elif damage == 'target':
        owner.retarget = dict(active=True)
    elif damage == 'pid':
        owner.pid_tuning = SimpleNamespace(state=lambda: {})
    elif damage == 'stale':
        rig[5].valid = False
    elif damage == 'interval':
        owner.last_write = owner.clock()
    original = copy.deepcopy(owner.policy); writes = copy.deepcopy(servo.writes)
    durable = owner.policy_config_file.read_bytes()
    with pytest.raises((BrokerError, ValueError)):
        owner.dispatch(request)
    assert servo.writes == writes and owner.policy == original
    assert owner.policy_config_file.read_bytes() == durable
    assert not journal_path(owner.state_file).exists() and owner.active and not owner.fault


def test_large_contraction_cannot_hide_raw_integral_clamp_behind_current_dac(rig, tmp_path):
    owner, servo = arm(rig)
    request = decision(rig, tmp_path, limit=10.)
    # The observed DAC is at the offset. It cannot prove rawI is small under FF/P.
    writes = copy.deepcopy(servo.writes)
    with pytest.raises(BrokerError, match='contraction exceeds'):
        owner.dispatch(request)
    assert servo.writes == writes and owner.active and not owner.fault


def test_expansion_cannot_widen_saved_physical_dac_caps(rig, tmp_path, monkeypatch):
    owner, servo = arm(rig)
    limits = dict(min_code=32700, max_code=32836)
    owner.dac_limits = copy.deepcopy(limits)
    read = servo.read
    monkeypatch.setattr(servo, 'read', lambda: dict(read(), dac_limits=copy.deepcopy(limits)))
    request = decision(rig, tmp_path)
    writes = copy.deepcopy(servo.writes)
    with pytest.raises(BrokerError, match='saved firmware DAC limits'):
        owner.dispatch(request)
    assert owner.dac_limits == limits and servo.writes == writes
    assert owner.active and not owner.fault


def test_preparation_does_not_interrupt_or_reserve_an_active_background(rig, tmp_path):
    owner, servo = arm(rig)
    request = decision(rig, tmp_path)
    owner.background.coordinator.epoch = {'synthetic': 'active trajectory'}
    state = owner.state_file.read_bytes()
    generation = owner.background.coordinator.generation
    writes = copy.deepcopy(servo.writes)
    with pytest.raises(BrokerError, match='active background'):
        owner.dispatch(dict(request, action='policy_update_prepare'))
    assert not owner.background.coordinator.reservations
    assert owner.background.coordinator.generation == generation
    assert owner.state_file.read_bytes() == state and servo.writes == writes


def test_small_contraction_proves_old_accumulator_and_destination(rig, tmp_path):
    owner, servo = arm(rig)
    request = decision(rig, tmp_path, limit=53.)
    result = owner.dispatch(request)
    transition = result['integral_limit_transition']
    assert transition['maximum_clamp_count'] == pytest.approx(1.)
    assert transition['quantized_clamp_bound_count'] == pytest.approx(3.)
    assert {row['source'] for row in transition['proofs']} == {
        'integral_limit_retained_accumulator', 'integral_limit_destination'}
    assert owner.active and servo.cfg['pid_integral_max'] * abs(servo.cfg['pid_ki']) == pytest.approx(53.)


def test_unknown_write_stays_quarantined_and_is_not_replayed(rig, tmp_path, monkeypatch):
    owner, servo = arm(rig)
    request = decision(rig, tmp_path)
    original = servo.set_control
    def lose_reply(values):
        original(values)
        if 'pid_integral_max' in values:
            raise OSError('lost physical write reply')
    monkeypatch.setattr(servo, 'set_control', lose_reply)
    with pytest.raises(BrokerError, match='offline review'):
        owner.dispatch(request)
    assert owner.fault and owner.lease and owner.pending
    count = len(servo.writes)
    with pytest.raises(BrokerError):
        owner.dispatch(request)
    assert len(servo.writes) == count
    with pytest.raises(ValueError, match='Incomplete'):
        check_journal(owner.state_file)
    assert json.loads(journal_path(owner.state_file).read_text())['hardware_stage'] == 'intent'


def test_postwrite_persistence_failure_keeps_rollback_evidence_and_no_replay(rig, tmp_path, monkeypatch):
    import ldcon.owners.native_policy_reload as persistence
    owner, servo = arm(rig)
    request = decision(rig, tmp_path)
    original = copy.deepcopy(servo.cfg)
    writer = persistence.write
    def fail(path, value):
        # Rule M step 4: the state file is the durable commit (config.json carries no policy copy any more).
        if Path(path) == owner.state_file and value.get('definition') != owner.definition:
            raise OSError('durable state unavailable')
        return writer(path, value)
    monkeypatch.setattr(persistence, 'write', fail)
    with pytest.raises(BrokerError, match='offline review'):
        owner.dispatch(request)
    journal = json.loads(journal_path(owner.state_file).read_text())
    assert journal['hardware_before'] == original
    assert journal['hardware_after'] == servo.cfg and journal['hardware_stage'] == 'verified'
    assert 'config_before' not in journal
    assert json.loads(owner.policy_config_file.read_text())['native_current']['native']['integral_limit_count'] == 45.
    assert owner.fault and owner.lease and owner.pending
    count = len(servo.writes)
    with pytest.raises(BrokerError):
        owner.dispatch(request)
    assert len(servo.writes) == count


def test_legacy_saved_gain_reference_stays_strict_and_new_marker_is_bound(rig):
    policy = rig[0].policy
    pid = dict(kp=-80., ki=policy['native']['ki_count_per_nm_s']/2, kd=0.)
    raw = policy['native']['integral_limit_count']/abs(policy['native']['ki_count_per_nm_s'])
    legacy = dict(pid=pid, raw_integral_limit=raw)
    assert effective_policy(policy, legacy)['native']['integral_limit_count'] == pytest.approx(22.5)
    with pytest.raises(ValueError, match='raw integral limit changed'):
        effective_policy(policy, dict(legacy, raw_integral_limit=2*raw))
    from ldcon.owners.native_current_tuning import saved_integral_limit
    saved = saved_integral_limit(policy, pid)
    assert effective_policy(policy, saved)['native']['integral_limit_count'] == pytest.approx(45.)
    saved['policy_integral_limit_count'] = 46.
    with pytest.raises(ValueError, match='reference changed'):
        effective_policy(policy, saved)
