import copy
import json

import pytest

from ldcon.contracts import FrequencySample, DeviceError, MeasurementUnavailable
from ldcon.esp32_native_owner import (NativeCurrentOwner, C_NM, validate_policy,
                                     current_for_code, envelope, digest)
from ldcon.esp32_servo import analog_metadata, convert_sample
from ldcon.instrument_client import BrokerError
from ldcon.native_current_card import parameters, run_native_current
from ldcon.esp32_native_client import NativeCurrentPort


class Clock:
    now = 100.
    def __call__(self):
        return self.now
    def advance(self, seconds=.2):
        self.now += seconds


def policy():
    return dict(revision='offline-fixture', firmware_version='fake-time-pi', writes_enabled=True,
        calibration=dict(bias_current_a=.05, zero_voltage_v=0., current_a_per_v=.15,
                         gain_relative_uncertainty=.05, wavelength_nm_per_a=4.,
                         evidence='offline external modulation fixture', verified=True, response_verified=True),
        dac=dict(bits=16, min_voltage=-2.5, max_voltage=2.5),
        adc=dict(bits=16, min_voltage=0., max_voltage=3.3),
        current_bounds_a=[.045, .055], activation_range_a=[-.001, .001],
        max_step_a=.00005, max_slew_a_per_s=.0005, update_interval_s=.1,
        max_sample_age_s=1., missing_timeout_s=5., heartbeat_timeout_s=5.,
        preserved=dict(multiplier_control=0., baseline_control=0., multiplier_target=0.,
                       baseline_target=0., mod_valid_min=0, mod_valid_max=65535,
                       software_modulation_enable=0, polling_interval=100.,
                       target_time_match=0, target_delay_us=0),
        native=dict(input=dict(valid_data_index=3, channel=4, cycle=0, src='192.0.2.5', udp_port=5557),
                    kp_count_per_nm=-1000., ki_count_per_nm_s=-1000., integral_limit_count=1.5,
                    capture_hz=20e6, interval_bounds_s=[.05, .3], evidence='offline response fixture', arm_enabled=True),
        optical=dict(endpoint='http://192.0.2.22:8765', channel=4, cycle=0,
                     expected_laser='test', trust_source_clock=True),
        cld_interlock=dict(endpoint='http://127.0.0.1:47143', instrument='test', resource='USB::test'),
        udp_gate=dict(listen_host='192.0.2.5', listen_port=5558, source_ip='192.0.2.22',
                      max_age_s=.5, future_tolerance_s=.01))


class Gate:
    valid = True
    def contains(self, *_):
        return self.valid
    def snapshot(self):
        return dict(fresh=self.valid)


class Bias:
    lease = None
    fresh = True
    fault = None
    current = .05
    measured = .05
    def claim(self, owner):
        self.lease = 'bias-fixture'
    def status(self):
        return dict(fault=self.fault, bias_held=bool(self.lease), fresh=self.fresh,
                    sample=dict(current_a=self.current, current_read_a=self.measured,
                                ld_enabled=True, tec_enabled=True, modulation_enabled=True))
    def release(self, *, native_stopped):
        assert native_stopped
        self.lease = None


class Optical:
    def __init__(self, clock):
        self.clock = clock
        self.valid = True
        self.frequency = C_NM / 1091.7855453438308
    def read_frequency(self):
        return FrequencySample(1, self.frequency, self.clock(), self.clock(),
                               'valid' if self.valid else 'invalid', 'fixture',
                               acquired_unix_s=self.clock())


class Servo:
    device_id = 'fixture-esp'
    host = '192.0.2.6'
    capabilities = ('adc_monitor', 'dac_monitor')
    def __init__(self, p):
        self.metadata = analog_metadata(dict(dac=p['dac'], adc=p['adc'], mode='normal', saved=True, storage_error='ESP_OK'))
        self.cfg = dict(p['preserved'], valid_data_index=-1, target=0., system_min=0., system_max=1e6,
                        control_offset=32768., pid_kp=0., pid_ki=0., pid_kd=0.,
                        pid_integral_min=-1., pid_integral_max=1., pid_multiplier=0,
                        pid_max_interval=500., udp_enable=1, udp_port=5557)
        self.cfg['polling_interval'] = 1.
        self.status = dict(firmware_version=p['firmware_version'], pid_time_based=True,
                           system_src=1, system_value=1091.7855453438308,
                           last_udp_channel=4, last_udp_cycle=0, last_udp_src='192.0.2.5',
                           last_udp_quality=0, last_udp_flags=5, last_udp_seq=10,
                           last_system_interval_us=100000, step_test_state='idle', mod_raw=35)
        self.writes = []
        self.read_error = False
        self.fail_enable = False
    def read(self):
        if self.read_error:
            raise OSError('read fixture outage')
        self.status['last_udp_seq'] += 1
        dac = int(self.cfg['control_offset'])
        self.status['dac_val'] = dac
        return dict(firmware_version=self.status['firmware_version'], metadata=copy.deepcopy(self.metadata),
                    servo_config=copy.deepcopy(self.cfg), servo_status=copy.deepcopy(self.status),
                    dac=convert_sample(dac, self.metadata['dac'], calibrated=True),
                    adc=convert_sample(35, self.metadata['adc'], calibrated=True))
    def set_control(self, values):
        self.writes.append(copy.deepcopy(values))
        self.cfg.update({k:v for k,v in values.items() if k!='pid_reset_state'})
        if values.get('pid_multiplier') == 1 and self.fail_enable:
            raise OSError('lost enable reply')
    def close(self):
        pass


@pytest.fixture
def rig(tmp_path):
    p = policy()
    clock, bias, gate = Clock(), Bias(), Gate()
    servo = Servo(p)
    optical = Optical(clock)
    owner = NativeCurrentOwner(servo, .2, 1., None, p, tmp_path/'state.json',
                               clock=clock, optical=optical, bias=bias, gate=gate)
    def call(action, **values):
        if action == 'background_begin':
            values.setdefault('expected_operating_mode_revision', owner.system_owner.operating_mode.status()['revision'])
        return owner.dispatch(dict(device_id=servo.device_id, action=action, lease=owner.lease, **values))
    def ready():
        call('claim', policy_digest=digest(p))
        call('prepare', target_wavelength_nm=1091.7855453438308)
        for _ in range(4):
            clock.advance()
            owner.poll()
    return owner, servo, clock, bias, optical, gate, call, ready


def test_native_envelope_includes_quantization_and_transfer_uncertainty():
    p = policy()
    proof = envelope(p, 1091.7855453438308, 32768)
    assert proof['code_bounds'] == [32766, 32770]
    assert proof['current_bounds_a'][1]-proof['current_bounds_a'][0] < 50e-6
    p['preserved']['polling_interval'] = 1
    # Human decision 2026-10-21: faster polling no longer shrinks the admitted PI range;
    # the reachable jump is reported, rails and absolute bounds still raise.
    fast = envelope(p, 1091.7855453438308, 32768)
    assert fast['code_bounds'] == proof['code_bounds'] and 'none' in fast['firmware_step_cap']
    p['native']['kp_count_per_nm'] = -1e9
    with pytest.raises(ValueError, match='DAC rail|absolute current'):
        envelope(p, 1091.7855453438308, 32768)


@pytest.mark.parametrize('change', [
    lambda p: p['native'].update(kp_count_per_nm=1000),
    lambda p: p['calibration'].update(response_verified=False),
    lambda p: p['dac'].update(bits=True),
    lambda p: p['native']['input'].update(src='192.0.2.22'),
    lambda p: p.update(missing_timeout_s=999),
])
def test_incomplete_or_unsafe_profiles_rejected(change):
    p = policy()
    change(p)
    with pytest.raises(ValueError):
        validate_policy(p)


def test_claim_prepare_arm_stop_release_and_cache_reads(rig):
    owner, servo, clock, bias, optical, gate, call, ready = rig
    ready()
    assert servo.cfg['polling_interval'] == 100
    assert bias.lease and owner.lease
    before = servo.status['last_udp_seq']
    for _ in range(5):
        call('status')
    assert servo.status['last_udp_seq'] == before
    call('arm')
    assert servo.cfg['pid_multiplier'] == 1 and owner.active
    assert servo.writes[-2:] == [{'pid_reset_state': 1}, {'pid_multiplier': 1}]
    stop = call('stop')
    assert stop['hold_kind'] == 'fixed_reference' and servo.cfg['pid_multiplier'] == 0
    call('release')
    assert owner.lease is None and bias.lease is None


def test_small_prewrite_read_gap_does_not_write_or_fault(rig):
    owner, servo, clock, bias, optical, gate, call, ready = rig
    ready()
    count = len(servo.writes)
    servo.read_error = True
    with pytest.raises(BrokerError) as exc:
        call('arm')
    assert exc.value.code == 'measurement'
    assert len(servo.writes) == count and owner.fault is None and owner.lease


def test_independent_optical_quality_does_not_disable_native_pi(rig):
    owner, servo, clock, bias, optical, gate, call, ready = rig
    ready()
    call('arm')
    initial = len(servo.writes)
    optical.valid = False
    for _ in range(10):
        clock.advance(.4)
        call('heartbeat')
        owner.poll()
    assert owner.active and len(servo.writes) == initial
    assert owner.fault is None
    clock.advance(1.1)
    call('heartbeat')
    owner.poll()
    # Rule B: independent optical quality is informational, not a second PI gate.
    assert owner.active and servo.cfg['pid_multiplier'] == 1
    assert owner.fault is None and owner.lease and bias.lease
    assert len(servo.writes) == initial


def test_fresh_recovery_resumes_without_native_rewrite(rig):
    owner, servo, clock, bias, optical, gate, call, ready = rig
    ready()
    call('arm')
    count = len(servo.writes)
    gate.valid = False
    clock.advance()
    owner.poll()
    assert owner.active and owner.input_error
    gate.valid = True
    for _ in range(3):
        clock.advance()
        owner.poll()
    assert owner.input_error is None and owner.qualified >= 3
    assert len(servo.writes) == count


def test_lost_enable_result_is_not_retried_and_retains_both_leases(rig):
    owner, servo, clock, bias, optical, gate, call, ready = rig
    ready()
    servo.fail_enable = True
    with pytest.raises(BrokerError) as exc:
        call('arm')
    assert exc.value.code == 'fault'
    assert owner.pending == dict(reason='enable native current PI', values={'pid_multiplier': 1})
    assert servo.writes[-2:] == [{'pid_multiplier': 1}, {'pid_multiplier': 0}]
    assert owner.lease and bias.lease and owner.fault
    with pytest.raises(BrokerError):
        call('release')


def test_bias_failure_is_fatal_and_leases_retained(rig):
    owner, servo, clock, bias, optical, gate, call, ready = rig
    ready()
    call('arm')
    bias.fault = 'physical readback invalid'
    owner.poll()
    assert owner.fault and owner.lease and bias.lease
    assert servo.cfg['pid_multiplier'] == 0


def test_owner_restart_quarantines_without_touching_hardware(rig):
    owner, servo, clock, bias, optical, gate, call, ready = rig
    ready()
    count = len(servo.writes)
    restarted = NativeCurrentOwner(servo, .2, 1., None, owner.policy, owner.state_file,
                                  clock=clock, optical=optical, bias=Bias(), gate=gate)
    restarted.poll()
    assert restarted.fault and restarted.lease and restarted.bias.lease
    assert len(servo.writes) == count


def test_probe_permitted_before_response_commission_but_arm_is_blocked(tmp_path):
    p = policy()
    p['calibration'].update(response_verified=False, wavelength_nm_per_a=None)
    p['native'].update(arm_enabled=False, kp_count_per_nm=0., ki_count_per_nm_s=0.)
    clock, servo, bias = Clock(), Servo(p), Bias()
    owner = NativeCurrentOwner(servo, .2, 1., None, p, tmp_path/'probe.json',
                               clock=clock, optical=Optical(clock), bias=bias, gate=Gate())
    call = lambda action, **kw: owner.dispatch(dict(device_id=servo.device_id, lease=owner.lease, action=action, **kw))
    call('claim', policy_digest=digest(p))
    clock.advance(.2)
    result = call('probe_offset', expected_code=32768., code=32769)
    assert result['response_commissioned'] is False and servo.cfg['pid_multiplier'] == 0
    assert servo.cfg['polling_interval'] == 1.
    with pytest.raises(BrokerError, match='disabled'):
        call('prepare', target_wavelength_nm=1091.7855453438308)
    clock.advance(.2)
    call('probe_offset', expected_code=32769, code=32768)
    call('release')


def test_probe_rejects_large_or_stale_move_before_mutation(rig):
    owner, servo, clock, bias, optical, gate, call, ready = rig
    call('claim', policy_digest=digest(owner.policy))
    clock.advance()
    with pytest.raises(BrokerError) as exc:
        call('probe_offset', expected_code=32768, code=32773)
    assert exc.value.code == 'range' and not servo.writes
    with pytest.raises(BrokerError) as exc:
        call('probe_offset', expected_code=32767, code=32769)
    assert exc.value.code == 'measurement' and not servo.writes


def test_card_has_no_pid_or_safety_overrides():
    card = dict(parameters={})
    with pytest.raises(ValueError):
        parameters(card, dict(pid_kp=123))


def test_card_runs_native_lifecycle_and_cleans_up_both_leases(rig, tmp_path):
    owner, servo, clock, bias, optical, gate, call, ready = rig
    device = dict(control_settings=dict(native_current=owner.policy),
                  connection=dict(endpoint='http://127.0.0.1:1', device_id=servo.device_id))
    port = NativeCurrentPort(device)
    port._call = lambda action, **kw: owner.dispatch(dict(action=action, device_id=servo.device_id, **kw))
    port.connect()
    events = []
    def sleep(seconds):
        clock.advance(seconds)
        owner.poll()
    params = parameters(dict(parameters={}), dict(target_wavelength_nm=1091.7855453438308, duration_s=3.))
    from ldcon.jobs.operation_history import OperationHistory
    history = OperationHistory(tmp_path, dict(laser=dict(id='native',devices={},interfaces={}),
        deck=dict(id='fixture'),card=dict(id='lock',operation='servo.native_current'),
        invocation=dict(parameters=params)), wall_clock=clock)
    def record(kind, row):
        events.append((kind,row))
        history.event(kind,row,elapsed_s=clock(),run_id='fixture',monotonic_s=clock())
    result = run_native_current(port, params, clock=clock, sleep=sleep, cancel=lambda:False,
                                emit=record)
    assert result['status'] == 'succeeded' and result['locked_cycles'] > 0
    assert port.active and owner.active
    port.close()
    assert owner.lease is None and bias.lease is None and servo.cfg['pid_multiplier'] == 0
    assert any(row.get('state') == 'locked' for _,row in events)
    from ldcon.core.records import read_json
    milestone = read_json(tmp_path/'operation-record.json')['milestones'][0]
    assert milestone['evidence']['native_pi_active']
    assert milestone['evidence']['target_ramp_active'] is False


def test_schema_readiness_blocks_unassigned_native_card_without_breaking_other_cards():
    from ldcon.interface_schema import InterfaceRepository
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]/'examples'/'interface-config'
    bundle = InterfaceRepository(root).assignment('sr1092')
    status = bundle['deck_definitions'][0]['card_status']['native-current-servo']
    assert not status['runnable']
    assert 'Missing interface: native_current' in status['blocked_reasons']


def test_measured_anchor_bounds_protect_an_intercept_different_from_setpoint(rig):
    owner, servo, clock, bias, optical, gate, call, ready = rig
    # The nominal .05 A model would permit this DAC move, but measured total
    # current is already close to the independent absolute lower boundary.
    bias.measured = .045005
    call('claim', policy_digest=digest(owner.policy))
    clock.advance()
    with pytest.raises(BrokerError, match='measured-anchor absolute'):
        call('probe_offset', expected_code=32768, code=32767)
    assert not servo.writes and owner.fault is None


@pytest.mark.parametrize('source', ['native', 'bias'])
def test_pure_read_outages_keep_pi_and_recover_without_gain_replay(rig, source):
    owner, servo, clock, bias, optical, gate, call, ready = rig
    ready()
    call('arm')
    count = len(servo.writes)
    previous = bias.status
    if source == 'native':
        servo.read_error = True
    else:
        def failed():
            raise TimeoutError('CLD lock held by a bounded USB read')
        bias.status = failed
    for _ in range(8):
        clock.advance(.4)
        call('heartbeat')
        owner.poll()
    assert owner.active and owner.fault is None and len(servo.writes) == count
    servo.read_error = False
    bias.status = previous
    for _ in range(3):
        clock.advance()
        owner.poll()
    assert owner.input_error is None and owner.qualified >= 3
    assert len(servo.writes) == count


def test_explicit_bias_ownership_loss_is_fatal_not_a_read_gap(rig):
    owner, servo, clock, bias, optical, gate, call, ready = rig
    ready()
    call('arm')
    def lost():
        raise BrokerError('ownership', 'bias token is no longer owned')
    bias.status = lost
    owner.poll()
    assert owner.fault and not owner.active and owner.lease
    assert servo.cfg['pid_multiplier'] == 0


def test_user_stop_works_during_optical_dropout(rig):
    owner, servo, clock, bias, optical, gate, call, ready = rig
    ready()
    call('arm')
    optical.valid = False
    clock.advance()
    owner.poll()
    assert owner.active
    call('stop')
    call('release')
    assert owner.lease is None and bias.lease is None and servo.cfg['pid_multiplier'] == 0


def test_capture_can_arm_before_fine_tolerance_but_never_claims_unachieved_lock(rig):
    owner, servo, clock, bias, optical, gate, call, ready = rig
    optical.frequency += 15e6
    servo.status['system_value'] = C_NM/optical.frequency
    device = dict(control_settings=dict(native_current=owner.policy),
                  connection=dict(endpoint='http://127.0.0.1:1', device_id=servo.device_id))
    port = NativeCurrentPort(device)
    port._call = lambda action, **kw: owner.dispatch(dict(action=action, device_id=servo.device_id, **kw))
    port.connect()
    events = []
    def sleep(seconds):
        clock.advance(seconds)
        owner.poll()
    params = parameters(dict(parameters={}), dict(target_wavelength_nm=1091.7855453438308, duration_s=3.))
    with pytest.raises(DeviceError, match='tolerance was not reached'):
        run_native_current(port, params, clock=clock, sleep=sleep, cancel=lambda:False,
                           emit=lambda kind,row:events.append((kind,row)))
    assert port.active and owner.active
    assert all('control_offset' not in values for values in servo.writes)
    assert not any(row.get('state') == 'locked' or row.get('locked') for _,row in events)
    port.close()


def direct_rig(tmp_path, *, commissioned=False):
    p = policy()
    p['udp_gate'] = None
    p['native']['input']['src'] = '192.0.2.22'
    p['native']['acceptance_hz'] = 10e9
    if not commissioned:
        p['calibration'].update(response_verified=False, wavelength_nm_per_a=None)
        p['native'].update(arm_enabled=False, kp_count_per_nm=0., ki_count_per_nm_s=0.)
    clock, servo, bias = Clock(), Servo(p), Bias()
    servo.status['last_udp_src'] = '192.0.2.22'
    optical = Optical(clock)
    # Explicit direct mode must never call the injected gate either.
    class ForbiddenGate:
        def __getattr__(self, name):
            raise AssertionError('Direct mode used a gate: ' + name)
    owner = NativeCurrentOwner(servo, .2, 1., None, p, tmp_path/'direct.json',
                               clock=clock, optical=optical, bias=bias, gate=ForbiddenGate())
    def call(action, **values):
        return owner.dispatch(dict(device_id=servo.device_id, action=action, lease=owner.lease, **values))
    return p, owner, servo, clock, bias, optical, call


def test_direct_input_setup_changes_only_receiver_fields_without_response_proof(tmp_path):
    p, owner, servo, clock, bias, optical, call = direct_rig(tmp_path)
    optical.valid = False  # Route-only setup does not need a PI-ready wavelength.
    call('claim', policy_digest=digest(p))
    before = copy.deepcopy(servo.cfg)
    target = 274589143700000.
    result = call('configure_input', target_frequency_hz=target)
    expected = dict(valid_data_index=3, system_min=C_NM/(target+10e9), system_max=C_NM/(target-10e9))
    assert servo.writes == [expected]
    assert servo.cfg == {**before, **expected}
    assert servo.cfg['pid_multiplier'] == 0 and servo.cfg['control_offset'] == 32768
    assert servo.cfg['target'] == 0. and servo.cfg['polling_interval'] == 1.
    assert result['acceptance_hz'] == 10e9 and result['input_mode'] == 'direct'
    assert result['preserved_dac_code'] == 32768
    assert owner.prepared is None and p['native']['arm_enabled'] is False
    call('release')
    saved = json.loads(owner.state_file.read_text())
    assert saved['input_configuration'] == result
    assert saved['lease'] is None and saved['pending'] is None and saved['fault'] is None
    restarted = NativeCurrentOwner(servo, .2, 1., None, p, owner.state_file,
                                  clock=clock, optical=optical, bias=Bias())
    assert restarted.input_configuration == result and restarted.fault is None


def test_direct_mode_has_no_gate_lifecycle_or_gate_membership_predicate(tmp_path):
    p, owner, servo, clock, bias, optical, call = direct_rig(tmp_path)
    call('claim', policy_digest=digest(p))
    call('configure_input', target_frequency_hz=274589143700000.)
    for _ in range(4):
        clock.advance()
        owner.poll()
    state = owner.snapshot()
    assert owner.gate is None and state['udp_gate'] is None
    assert state['native_input_mode'] == 'direct' and state['preintegration_quality_gate'] is False
    assert state['input_valid'] and state['qualified_samples'] >= 3
    # Rule B: firmware-consumed quality flags are not revalidated by the host.
    servo.status['last_udp_quality'] = 512
    clock.advance()
    owner.poll()
    assert owner.snapshot()['input_valid']
    call('release')
    owner.stop.set()
    owner.run()  # No UDP bind/start/close even through the actual run lifecycle.


def test_wide_acceptance_expands_native_output_proof_not_arm_capture(tmp_path):
    p, owner, servo, clock, bias, optical, call = direct_rig(tmp_path, commissioned=True)
    assert p['native']['capture_hz'] == 20e6
    call('claim', policy_digest=digest(p))
    # These gains pass the original 20 MHz proof; with a 10 GHz receiver the
    # reachable PI jump exceeds one host step. Since the human decision of
    # 2026-10-21 that is reported, not refused; prepare writes nothing.
    prepared = call('prepare', target_wavelength_nm=1091.7855453438308)
    proof = prepared.get('envelope') or owner.prepared['envelope']
    assert proof['maximum_jump_a'] > p['max_step_a'] and 'none' in proof['firmware_step_cap']
    assert not any(w.get('pid_multiplier') == 1 for w in servo.writes) and not owner.active and owner.fault is None
    assert p['native']['capture_hz'] == 20e6


def test_direct_wide_receiver_does_not_widen_pi_arm_entry(tmp_path):
    p = policy()
    p['udp_gate'] = None
    p['native'].update(acceptance_hz=10e9, kp_count_per_nm=-1.)
    p['native']['input']['src'] = '192.0.2.22'
    clock, servo, bias = Clock(), Servo(p), Bias()
    servo.status['last_udp_src'] = '192.0.2.22'
    optical = Optical(clock)
    optical.frequency += 100e6
    servo.status['system_value'] = C_NM/optical.frequency
    owner = NativeCurrentOwner(servo, .2, 1., None, p, tmp_path/'safe-direct.json',
                               clock=clock, optical=optical, bias=bias)
    call = lambda action, **kw: owner.dispatch(dict(action=action, device_id=servo.device_id, lease=owner.lease, **kw))
    call('claim', policy_digest=digest(p))
    call('prepare', target_wavelength_nm=1091.7855453438308)
    assert servo.cfg['system_min'] == pytest.approx(C_NM/(274589143700000.+10e9))
    for _ in range(4):
        clock.advance()
        owner.poll()
    count = len(servo.writes)
    with pytest.raises(BrokerError, match='outside the qualified native capture'):
        call('arm')
    assert len(servo.writes) == count and servo.cfg['pid_multiplier'] == 0


def test_input_setup_requires_clean_lease_existing_listener_and_pi_off(tmp_path):
    p, owner, servo, clock, bias, optical, call = direct_rig(tmp_path)
    request = dict(target_frequency_hz=274589143700000.)
    with pytest.raises(BrokerError) as missing:
        call('configure_input', **request)
    assert missing.value.code == 'ownership'
    call('claim', policy_digest=digest(p))
    owner.pending = {'other': 'intent'}
    with pytest.raises(BrokerError, match='clean lease'):
        call('configure_input', **request)
    owner.pending = None
    servo.cfg['udp_enable'] = 0
    with pytest.raises(BrokerError, match='already be enabled'):
        call('configure_input', **request)
    servo.cfg['udp_enable'] = 1
    servo.cfg['pid_multiplier'] = 1
    with pytest.raises(BrokerError, match='PI off'):
        call('configure_input', **request)
    assert not servo.writes


def test_uncertain_route_only_write_retains_fault_and_lease_without_retry(tmp_path):
    p, owner, servo, clock, bias, optical, call = direct_rig(tmp_path)
    call('claim', policy_digest=digest(p))
    setter = servo.set_control
    def lost(values):
        setter(values)
        raise OSError('Receiver write reply lost')
    servo.set_control = lost
    with pytest.raises(BrokerError) as error:
        call('configure_input', target_frequency_hz=274589143700000.)
    assert error.value.code == 'fault'
    assert len(servo.writes) == 1 and owner.lease and bias.lease and owner.pending
    assert owner.input_configuration is None
    assert servo.cfg['pid_multiplier'] == 0 and servo.cfg['control_offset'] == 32768


@pytest.mark.parametrize('width', [True, 0, -1, float('nan'), 1e6])
def test_acceptance_must_be_finite_positive_and_cover_capture(width):
    p = policy()
    p['native']['acceptance_hz'] = width
    with pytest.raises(ValueError):
        validate_policy(p)
