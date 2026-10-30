"""External-path exception remains explicit, approved and bounded by output proofs."""
import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ldcon.owners.esp32_native_owner import C_NM, digest, validate_policy
from ldcon.core.instrument_client import BrokerError
from ldcon.owners.native_modulation_limits import FIELD, budget_a, budget_codes, limited, report
from ldcon.owners.native_policy_update import journal_path

_spec = importlib.util.spec_from_file_location('modulation_fixture',
    (Path(__file__).resolve().parent / 'support') / 'jobs/tests/test_esp32_native_current.py')
_fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixture)


@pytest.fixture
def rig(tmp_path, monkeypatch, request):
    p = _fixture.policy()
    options = getattr(request, 'param', {})
    p.update(options)
    monkeypatch.setattr(_fixture, 'policy', lambda: copy.deepcopy(p))
    monkeypatch.delenv('LDCON_BACKGROUND_LASER_ID', raising=False)
    result = _fixture.rig.__wrapped__(tmp_path)
    result[0].system_owner.assemblies.factory = SimpleNamespace(laser_snapshot=dict(
        id='sr1092', interfaces=dict(native_current=dict(device='native')),
        devices=dict(native=dict(type='esp32servocon.native_current.owner',
                                connection=dict(device_id=result[0].driver.device_id)))))
    return result


def approve(rig, tmp_path, *, new=False, old=None, laser='sr1092', authority='human_approval'):
    owner = rig[0]
    owner.policy_config_file = tmp_path / 'owner.json'
    owner.policy_config_file.write_text(json.dumps(dict(native_current=owner.policy)))
    owner.approvals_root = tmp_path / 'approvals'
    decision = dict(schema_version=1, id='a'*32, laser_id=laser, authority=authority,
        scope=dict(kind='lasers', name=laser), human_name='offline exact approval',
        changes=[dict(path='devices.native.control_settings.native_current.'+FIELD, old=old, new=new)])
    path = owner.approvals_root / laser / 'decisions' / ('a'*32+'.json')
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(decision))
    owner._persist()
    return dict(action='policy_update', device_id=owner.driver.device_id, expected_definition=owner.definition,
                decision_id=decision['id'], laser_id=laser)


def test_absent_flag_preserves_policy_digest_and_legacy_budget():
    p = _fixture.policy()
    original = copy.deepcopy(p)
    assert validate_policy(p) == original and FIELD not in p
    assert digest(validate_policy(p)) == digest(original)
    assert limited(p) is True and budget_a(p) == 50e-6 and budget_codes(p) == 4
    explicit = dict(p, **{FIELD: True})
    assert budget_a(explicit) == budget_a(p) and budget_codes(explicit) == budget_codes(p)
    exempt = dict(p, **{FIELD: False})
    assert budget_a(exempt) is None and budget_codes(exempt) == 65535
    assert report(exempt)['host_max_step_a'] is None and report(exempt)['host_max_slew_a_per_s'] is None
    assert report(exempt)['configured_max_step_a'] == p['max_step_a']


@pytest.mark.parametrize('value', [None, 0, 1, 'false', [], {}])
def test_exception_is_a_strict_optional_boolean(value):
    with pytest.raises(ValueError, match='must be boolean'):
        validate_policy(dict(_fixture.policy(), **{FIELD: value}))


@pytest.mark.parametrize('rig', [{}, {FIELD: True}, {FIELD: False}], indirect=True)
def test_100mhz_uses_one_actual_step_only_with_explicit_exemption(rig):
    owner, servo, clock, bias, optical, gate, call, ready = rig
    ready(); call('arm')
    original = copy.deepcopy(servo.cfg)
    target = C_NM / original['target'] + 100e6
    start = len(servo.writes)
    call('retarget', target_frequency_hz=target)
    for _ in range(12):
        clock.advance(.3); call('heartbeat')
        optical.frequency = C_NM / servo.cfg['target']
        servo.status['system_value'] = servo.cfg['target']
        owner.poll()
        if not owner.retarget['active']:
            break
    result = owner.retarget
    assert not result['active'] and result['error'] is None
    assert result['current_hz'] == target
    writes = [w for w in servo.writes[start:] if 'target' in w]
    assert len(writes) == result['steps']
    if limited(owner.policy):
        assert result['steps'] > 1 and result['maximum_actual_step_hz'] < 100e6
    else:
        assert result['steps'] == 1 and result['maximum_actual_step_hz'] == 100e6
        assert result['minimum_actual_step_hz'] == 100e6
        proof = result['last_transition_proof']['code_bounds']
        assert proof[0] <= min(original['control_offset'], servo.cfg['control_offset'])
        assert proof[1] >= max(original['control_offset'], servo.cfg['control_offset'])
    for key in ('pid_kp', 'pid_ki', 'pid_kd', 'pid_integral_min', 'pid_integral_max', 'pid_multiplier'):
        assert servo.cfg[key] == original[key]
    assert owner.policy['calibration'] == _fixture.policy()['calibration']
    assert not owner.fault


@pytest.mark.parametrize('action', ['offset', 'probe_offset', 'acquire_offset', 'recenter'])
@pytest.mark.parametrize('rig', [{}, {FIELD: False}], indirect=True)
def test_offset_paths_use_optional_budget_and_keep_minimum_write_interval(rig, action):
    owner, servo, clock, bias, optical, gate, call, ready = rig
    ready()
    if action == 'recenter':
        call('arm'); clock.advance(.3); owner.poll()
    if action == 'acquire_offset':
        call('acquire_prepare', target_wavelength_nm=servo.cfg['target'])
    old = int(servo.cfg['control_offset'])
    kwargs = dict(code=old+20, expected_code=old)
    if action == 'recenter':
        kwargs['max_step_codes'] = 20
    writes = copy.deepcopy(servo.writes)
    if limited(owner.policy):
        with pytest.raises(BrokerError, match='step/slew|max_step_codes'):
            call(action, **kwargs)
        assert servo.writes == writes
    else:
        assert call(action, **kwargs)['code'] == old+20
        with pytest.raises(BrokerError, match='interval'):
            call(action, **dict(kwargs, code=old+21, expected_code=old+20))
        assert len(servo.writes) == len(writes)+1


@pytest.mark.parametrize('rig', [{FIELD: False}], indirect=True)
@pytest.mark.parametrize('boundary', ['absolute', 'activation', 'dac'])
def test_exemption_cannot_bypass_output_bounds(rig, boundary, monkeypatch):
    owner, servo, clock, bias, optical, gate, call, ready = rig
    ready(); call('arm'); clock.advance(.3); owner.poll()
    old = int(servo.cfg['control_offset'])
    step = 500 if boundary == 'absolute' else 100 if boundary == 'activation' else 20
    if boundary == 'dac':
        owner.dac_limits = dict(min_code=old-5, max_code=old+5)
        read = servo.read
        monkeypatch.setattr(servo, 'read', lambda: dict(read(), dac_limits=copy.deepcopy(owner.dac_limits)))
    writes = copy.deepcopy(servo.writes)
    with pytest.raises((BrokerError, ValueError), match='absolute|activation|relative|DAC limits'):
        call('recenter', code=old+step, expected_code=old, max_step_codes=step)
    assert servo.writes == writes and not owner.fault


@pytest.mark.parametrize('active', [False, True])
def test_exact_approval_adopts_exception_without_changing_hardware_or_direct_current_policy(rig, tmp_path, active):
    from ldcon.core.agent_authority import SAFETY, classify, live_class
    from ldcon.owners.native_policy_safety import policy_layers
    owner, servo, *_ = rig
    if active:
        rig[-1](); rig[-2]('arm')
    path = ('devices', 'native', 'control_settings', 'native_current', FIELD)
    assert classify('lasers', path) == SAFETY and live_class('lasers', path) == 'native_hot'
    request = approve(rig, tmp_path)
    original = copy.deepcopy(owner.policy); writes = copy.deepcopy(servo.writes)
    assert owner.dispatch(dict(request, action='policy_update_prepare'))['status'] == 'prepared'
    assert owner.policy == original and servo.writes == writes
    result = owner.dispatch(request)
    assert result['status'] == 'updated' and servo.writes == writes
    assert owner.policy == dict(original, **{FIELD: False})
    # rule M step 4: the state file (not a config.json copy) is the durable record of the adopted policy
    assert json.loads(owner.state_file.read_text())['definition'] == owner.definition
    assert FIELD not in json.loads(owner.policy_config_file.read_text())['native_current']
    assert json.loads(journal_path(owner.state_file).read_text())['status'] == 'committed'
    assert owner.dispatch(request)['idempotent'] is True
    for view in (policy_layers(owner), owner.system_owner.constraints()):
        assert view[FIELD] is False and view['host_max_step_a'] is None and view['host_max_slew_a_per_s'] is None


@pytest.mark.parametrize('rig', [{FIELD: False}], indirect=True)
def test_exact_approval_can_reinstate_the_original_caps(rig, tmp_path):
    owner, servo, *_ = rig
    request = approve(rig, tmp_path, old=False, new=True)
    writes = copy.deepcopy(servo.writes)
    assert owner.dispatch(request)['status'] == 'updated'
    assert limited(owner.policy) is True and budget_codes(owner.policy) == 4
    assert servo.writes == writes


@pytest.mark.parametrize('damage', ['wrong_laser', 'unapproved', 'wrong_old', 'nonboolean'])
def test_exception_requires_exact_canonical_approval(rig, tmp_path, damage):
    options = dict(laser='another') if damage == 'wrong_laser' else dict(authority='free') if damage == 'unapproved' else dict(old=True) if damage == 'wrong_old' else dict(new=0)
    request = approve(rig, tmp_path, **options)
    owner, servo, *_ = rig
    original = copy.deepcopy(owner.policy); writes = copy.deepcopy(servo.writes)
    with pytest.raises((BrokerError, ValueError)):
        owner.dispatch(request)
    assert owner.policy == original and servo.writes == writes
    assert not journal_path(owner.state_file).exists()


@pytest.mark.parametrize('busy', ['target', 'target_study', 'feedforward', 'pid'])
def test_modulation_adoption_refuses_active_trajectory(rig, tmp_path, busy):
    owner, servo, clock, bias, optical, gate, call, ready = rig
    ready(); call('arm')
    if busy == 'target':
        call('retarget', target_frequency_hz=C_NM/servo.cfg['target']+100e6)
    elif busy == 'target_study':
        status = call('target_ff_study_status')
        call('target_ff_study_set', expected_revision=status['revision'], scale=1., step_hz=100e6, duration_s=60.)
    elif busy == 'feedforward':
        owner.ff = {'state': 'active'}
    else:
        owner.pid_tuning = SimpleNamespace(state=lambda: {})
    request = approve(rig, tmp_path)
    original = copy.deepcopy(owner.policy); writes = copy.deepcopy(servo.writes)
    with pytest.raises(BrokerError, match='Finish the active'):
        owner.dispatch(request)
    assert owner.policy == original and servo.writes == writes


def test_operating_point_planner_uses_external_path_budget():
    from ldcon.jobs.operating_point import _NativePolicy
    p = _fixture.policy()
    assert _NativePolicy(p).max_step == budget_codes(p)
    exempt = _NativePolicy(dict(p, **{FIELD: False}))
    assert exempt.max_step == 65535 and exempt.update_interval_s == p['update_interval_s']


@pytest.mark.parametrize('rig', [{FIELD: False}], indirect=True)
def test_unknown_target_write_still_latches_and_is_never_replayed(rig, monkeypatch):
    owner, servo, clock, bias, optical, gate, call, ready = rig
    ready(); call('arm')
    call('retarget', target_frequency_hz=C_NM/servo.cfg['target']+100e6)
    write = servo.set_control

    def lost(values):
        write(values)
        if 'target' in values:
            raise OSError('lost write reply')

    monkeypatch.setattr(servo, 'set_control', lost)
    clock.advance(.3)
    with pytest.raises(BrokerError, match='Uncertain'):
        owner.poll()
    count = len(servo.writes)
    clock.advance(.3); owner.poll()
    assert len(servo.writes) == count and owner.fault and owner.lease


@pytest.mark.parametrize('flag', [True, False])
def test_finite_acquisition_keeps_its_resolution_without_inheriting_exempt_current_cap(flag):
    from ldcon.jobs.native_current_acquire import parameters, run_native_acquire
    clock = _fixture.Clock()
    p = dict(_fixture.policy(), max_step_a=1e-12, **{FIELD: flag})
    target = C_NM/1091.7855453438308
    calls = []
    port = SimpleNamespace(policy=p, heartbeat=lambda: None)

    class FirstMove(Exception):
        pass

    def mutate(action, **kw):
        calls.append((action, kw))
        if action == 'acquire_offset':
            raise FirstMove()
        return {}

    port.mutate = mutate
    port.read = lambda: dict(native_pi_active=False, optical=dict(frequency_hz=target-100e6,
        acquired_s=clock(), acquired_unix_s=clock()), sample={'servo_config': {'control_offset': 32768}})
    params = parameters({'parameters': {}}, dict(target_frequency_hz=target, settle_s=1., stable_s=1.))
    expected = _fixture.DeviceError if flag else FirstMove
    with pytest.raises(expected):
        run_native_acquire(port, params, clock=clock, sleep=clock.advance, cancel=lambda: False, emit=lambda *_: None)
    if flag:
        assert not calls
    else:
        assert calls[-1][0] == 'acquire_offset'
        assert abs(calls[-1][1]['code']-32768) == params['max_step_codes']


@pytest.mark.parametrize('rig', [{}, {FIELD: False}], indirect=True)
def test_flexible_gain_transition_uses_same_optional_budget_and_keeps_envelope_proofs(rig):
    from ldcon.owners.flexible_native_trial import FlexibleNativeTrial
    owner, servo, clock, bias, optical, gate, call, ready = rig
    ready(); call('arm')
    before = owner._read_before(require_input=True)
    trial = FlexibleNativeTrial(owner, before,
        dict(kp_count_per_nm=-500., ki_count_per_nm_s=-500., integral_limit_count=1.5), 60.)
    assert trial.current_step_a == budget_a(owner.policy)
    for row in trial._plan(trial.original, trial.candidate_cfg):
        assert row['proof']['code_bounds'][0] >= 0 and row['proof']['code_bounds'][1] <= 65535


@pytest.mark.parametrize('flag', [True, False])
def test_stationary_card_acquisition_uses_the_same_route_budget(flag):
    from ldcon.jobs.native_current_card import parameters, run_native_current
    clock = _fixture.Clock()
    p = dict(_fixture.policy(), **{FIELD: flag})
    target = C_NM/1091.7855453438308
    calls = []
    port = SimpleNamespace(policy=p, active=False, heartbeat=lambda: None)

    class FirstMove(Exception):
        pass

    def mutate(action, **kw):
        calls.append((action, kw))
        if action == 'offset':
            raise FirstMove()
        return {}

    port.mutate = mutate
    port.read = lambda: dict(native_pi_active=False, optical=dict(frequency_hz=target-100e6,
        acquired_s=clock(), acquired_unix_s=clock()), sample={'servo_config': {'control_offset': 32768}})
    params = parameters({'parameters': {}}, dict(target_frequency_hz=target))
    with pytest.raises(FirstMove):
        run_native_current(port, params, clock=clock, sleep=clock.advance, cancel=lambda: False, emit=lambda *_: None)
    step = abs(calls[-1][1]['code']-32768)
    assert step == budget_codes(p) if flag else step > budget_codes(_fixture.policy())
