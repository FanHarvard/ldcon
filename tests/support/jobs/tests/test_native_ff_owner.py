"""No sockets or hardware: atomic fake reproduces raw/held ADC semantics."""
import copy

import pytest

from ldcon.esp32_native_owner import NativeCurrentOwner, digest
from ldcon.esp32_native_client import NativeCurrentPort
from ldcon.esp32_servo import convert_sample
from ldcon.contracts import DeviceError, MeasurementUnavailable
from ldcon.instrument_client import BrokerError
from ldcon.native_ff_proof import FF_KEYS, prove
from test_esp32_native_current import Clock, Bias, Gate, Optical, Servo, policy


def ff_profile(p):
    return dict(enabled=True, revision='offline-ff-v1', firmware_version=p['firmware_version'],
        window_rule='S1', frequency_hz_per_v=1e9, awg_center_v=1., adc_center_count=20000,
        adc_noise_counts=3., adc_valid_bounds=[19970, 20030], max_half_span_hz=100e6,
        max_duration_s=30., heartbeat_timeout_s=2., missing_timeout_s=2.,
        unwind_interval_s=.2, unwind_steps=3, start_samples=3, start_span_s=.39,
        start_integral_fraction_max=.5, abort_integral_fraction_max=.9, abort_error_hz=10e6)


def plan():
    return dict(half_span_hz=1e6, step_hz=1e6, dwell_s=.2, edge_s=.1, cycles=1)


class FFServo(Servo):
    def __init__(self, p):
        super().__init__(p)
        self.cfg.update(ff_contract_version=1, ff_config_revision=0, ff_profile_digest='',
                        dac_quantization='truncate_nonnegative', dac_limit_enabled=False)
        self.raw = self.used = 20000
        self.invalid = self.cycle = 0
        self.integral = 0.
        self.fail_ff = False
        self.ff_writes = []
        self.corrupt_reply = False
        self.freeze_cycle = False
        self.snapshot_override = {}

    def read(self):
        sample = super().read()
        valid = self.cfg['mod_valid_min'] <= self.raw <= self.cfg['mod_valid_max']
        if valid:
            self.used = self.raw
        else:
            self.invalid += 1
        if not self.freeze_cycle:
            self.cycle += 1
        target = self.cfg['target']+self.cfg['multiplier_target']*(self.used-self.cfg['baseline_target'])
        offset = self.cfg['control_offset']+self.cfg['multiplier_control']*(self.used-self.cfg['baseline_control'])
        dac = max(0, min(65535, int(offset)))
        snap = dict(cycle_seq=self.cycle, cycle_time_us=self.cycle*100000,
            config_revision=self.cfg['ff_config_revision'], profile_digest=self.cfg['ff_profile_digest'],
            adc_raw=self.raw, adc_used=self.used, mod_valid=valid, mod_has_valid=True,
            mod_invalid=self.invalid, effective_target=target, target_for_pid=target,
            target_match_valid=True, target_match_overflow=0, effective_offset=offset,
            pid_output=0., pid_integral=self.integral, system_value=self.status['system_value'],
            adc_io_valid=True, software_modulation_enabled=False, analog_test=False,
            dac_limit_enabled=False, polling_interval_us=self.cfg['polling_interval']*1000,
            pid_cycle_seq=self.cycle, target_config_revision=self.cfg['ff_config_revision'],
            system_seq=self.status['last_udp_seq'], system_rx_us=self.cycle*100000,
            target_time_us=self.cycle*100000,
            dac_val=dac, dac_time_us=self.cycle*100000+10, dac_write_ok=True,
            dac_output_unknown=False, dac_write_failures=0,
            **{k:self.cfg[k] for k in FF_KEYS})
        snap.update(self.snapshot_override)
        if self.corrupt_reply:
            snap['config_revision'] -= 1
        sample['servo_status'].update(feedforward=snap, mod_raw=self.used, dac_val=dac)
        sample['dac'] = convert_sample(dac, self.metadata['dac'], calibrated=True)
        sample['adc'] = convert_sample(self.used, self.metadata['adc'], calibrated=True)
        return sample

    def set_feedforward(self, values, *, expected_revision, profile_digest):
        assert expected_revision == self.cfg['ff_config_revision']
        assert set(values) == set(FF_KEYS)
        self.ff_writes.append(copy.deepcopy(values))
        self.cfg.update(values)
        self.cfg.update(ff_config_revision=expected_revision+1, ff_profile_digest=profile_digest)
        if self.fail_ff:
            raise OSError('lost atomic commit reply')


@pytest.fixture
def ff_rig(tmp_path):
    p = policy()
    p['dac'].update(min_voltage=-.25, max_voltage=.25)
    p['native'].update(kp_count_per_nm=-100., ki_count_per_nm_s=-100.)
    profile = ff_profile(p)
    clock, bias, gate = Clock(), Bias(), Gate()
    servo, optical = FFServo(p), Optical(clock)
    owner = NativeCurrentOwner(servo, .2, 1., None, p, tmp_path/'ff-state.json',
                               clock=clock, optical=optical, bias=bias, gate=gate, ff_profile=profile)
    def call(action, **values):
        if action == 'background_begin':
            values.setdefault('expected_operating_mode_revision', owner.system_owner.operating_mode.status()['revision'])
        return owner.dispatch(dict(device_id=servo.device_id, action=action, lease=owner.lease,
                                   profile_digest=owner.ff_definition, **values))
    def tick(n=1):
        for _ in range(n):
            clock.advance(.2)
            if not owner.fault:
                call('heartbeat')
            owner.poll()
    call('claim', policy_digest=digest(p))
    call('prepare', target_wavelength_nm=1091.7855453438308)
    tick(4)
    call('arm')
    tick(3)
    return owner, servo, clock, bias, optical, call, tick


def begin(rig):
    owner, servo, clock, bias, optical, call, tick = rig
    check = call('ff_validate', plan=plan())
    assert check['ok'], check['reasons']
    return call('ff_begin', plan=plan(), proof_digest=check['proof_digest'])


def test_normal_coherent_begin_unwind_preserves_pi_and_lease(ff_rig):
    owner, servo, clock, bias, optical, call, tick = ff_rig
    original = {k:servo.cfg[k] for k in FF_KEYS}
    s = begin(ff_rig)
    assert s['state'] == 'active' and owner.active
    assert owner.expected_preserved['multiplier_target'] != 0
    call('ff_end', token=s['token'], reason='normal completion')
    tick(4)
    assert call('ff_status', token=s['token'])['state'] == 'restored'
    assert {k:servo.cfg[k] for k in FF_KEYS} == original
    assert owner.active and owner.lease and bias.lease and owner.fault is None
    ratios = [v['multiplier_target']/v['multiplier_control'] for v in servo.ff_writes if v['multiplier_control']]
    assert ratios == pytest.approx([ratios[0]]*len(ratios))
    assert all('pid_multiplier' not in v for v in servo.ff_writes)


def test_held_adc_invalid_causes_ff_unwind_not_pi_stop(ff_rig):
    owner, servo, clock, bias, optical, call, tick = ff_rig
    begin(ff_rig)
    servo.raw = 0
    tick()
    assert owner.ff['state'] == 'unwinding'
    assert servo.used == 20000 and servo.invalid > 0
    tick(4)
    assert owner.ff['state'] == 'restored' and owner.active and owner.fault is None


def test_prewrite_missing_does_not_write_or_latch(ff_rig):
    owner, servo, clock, bias, optical, call, tick = ff_rig
    check = call('ff_validate', plan=plan())
    servo.read_error = True
    with pytest.raises(BrokerError) as exc:
        call('ff_begin', plan=plan(), proof_digest=check['proof_digest'])
    assert exc.value.code == 'measurement'
    assert not servo.ff_writes and owner.ff is None and owner.fault is None


@pytest.mark.parametrize('failure', ['lost_reply', 'wrong_revision'])
def test_postwrite_uncertainty_retains_intent_leases_and_never_replays(ff_rig, failure):
    owner, servo, clock, bias, optical, call, tick = ff_rig
    if failure == 'lost_reply':
        servo.fail_ff = True
    else:
        original = servo.set_feedforward
        def corrupt(*args, **kwargs):
            original(*args, **kwargs)
            servo.corrupt_reply = True
        servo.set_feedforward = corrupt
    with pytest.raises(BrokerError) as exc:
        begin(ff_rig)
    assert exc.value.code == 'fault'
    assert owner.fault and owner.pending and owner.lease and bias.lease
    assert len(servo.ff_writes) == 1
    owner.poll()
    assert len(servo.ff_writes) == 1
    assert call('ff_status')['state'] == 'fault'


def test_restart_retains_residual_session_without_device_write(ff_rig):
    owner, servo, clock, bias, optical, call, tick = ff_rig
    begin(ff_rig)
    before = len(servo.ff_writes)
    restored = NativeCurrentOwner(servo, .2, 1., None, owner.policy, owner.state_file,
        clock=clock, optical=optical, bias=bias, gate=Gate(), ff_profile=owner.ff_profile)
    assert restored.fault and restored.lease and restored.ff['state'] == 'active'
    restored.poll()
    assert len(servo.ff_writes) == before


def test_profile_mismatch_and_unsupported_request_cannot_weaken_proof(ff_rig):
    owner, servo, clock, bias, optical, call, tick = ff_rig
    with pytest.raises(BrokerError) as exc:
        owner.dispatch(dict(action='ff_validate', device_id=servo.device_id, lease=owner.lease,
                            profile_digest='a'*64, plan=plan()))
    assert exc.value.code == 'configuration'
    for field in ('adc_noise_counts', 'control_update_s', 'relative_gate_hz', 'pid_max_interval_ms'):
        with pytest.raises(BrokerError):
            call('ff_validate', plan=dict(plan(), **{field:0}))
    assert not servo.ff_writes


def test_native_read_absence_is_bounded_and_no_unwind_uses_stale_data(ff_rig):
    owner, servo, clock, bias, optical, call, tick = ff_rig
    begin(ff_rig)
    servo.read_error = True
    initial = len(servo.ff_writes)
    tick(5)
    assert owner.active and owner.ff['state'] == 'unwinding'
    assert len(servo.ff_writes) == initial
    tick(6)
    assert owner.fault and owner.lease
    assert 'disable could not be verified' in owner.fault
    assert owner.active  # last confirmed state; a failed read cannot prove PI stopped
    assert len(servo.ff_writes) == initial


def test_short_missing_recovers_and_fresh_unwind_completes(ff_rig):
    owner, servo, clock, bias, optical, call, tick = ff_rig
    begin(ff_rig)
    token = owner.ff['token']
    servo.read_error = True
    tick(2)
    servo.read_error = False
    tick(1)
    call('ff_end', token=token, reason='completed after fresh recovery')
    tick(7)
    assert owner.ff['state'] == 'restored' and owner.active and owner.fault is None


@pytest.mark.parametrize('change,reason', [
    (lambda s: setattr(s, 'integral', .014), 'integral'),
    (lambda s: s.status.update(system_value=s.status['system_value']+.00006), 'error')])
def test_coherent_error_and_integral_abort_only_ff(ff_rig, change, reason):
    owner, servo, clock, bias, optical, call, tick = ff_rig
    begin(ff_rig)
    change(servo)
    tick()
    assert owner.ff['state'] == 'restored' and reason in owner.ff['reason']
    assert owner.active and owner.fault is None


def test_mid_session_external_ff_change_is_fatal(ff_rig):
    owner, servo, clock, bias, optical, call, tick = ff_rig
    begin(ff_rig)
    servo.cfg['multiplier_target'] *= 2
    tick()
    assert owner.fault and owner.lease


def test_client_ff_reads_do_not_clear_unknown_mutation(ff_rig):
    owner, servo, clock, bias, optical, call, tick = ff_rig
    device = dict(control_settings=dict(native_current=owner.policy, native_ff_sweep=owner.ff_profile),
                  connection=dict(endpoint='http://127.0.0.1:12345', device_id=servo.device_id))
    port = NativeCurrentPort(device)
    port.lease = owner.lease
    port._rpc = lambda action, **values: owner.dispatch(dict(action=action, **values))
    port.ff_begin(plan())
    port.uncertain_output = True
    assert port.ff_status()['state'] == 'active' and port.uncertain_output
    with pytest.raises(DeviceError, match='uncertain'):
        port.ff_end()


def test_ff_config_and_pid_changes_are_exclusive(ff_rig):
    owner, servo, clock, bias, optical, call, tick = ff_rig
    begin(ff_rig)
    for action in ('pid_begin', 'prepare', 'release', 'offset'):
        with pytest.raises(BrokerError) as exc:
            call(action)
        assert exc.value.code == 'busy'


def test_postcommit_prior_revision_waits_for_next_pid_sample(ff_rig):
    owner, servo, clock, bias, optical, call, tick = ff_rig
    begin(ff_rig)
    servo.snapshot_override['target_config_revision'] = servo.cfg['ff_config_revision']-1
    tick(2)
    assert owner.ff['state'] == 'active' and owner.fault is None
    servo.snapshot_override.clear()
    tick()
    assert owner.ff['state'] == 'active' and owner.fault is None


def test_adc_cycles_do_not_freshen_an_unconsumed_pid_sample(ff_rig):
    owner, servo, clock, bias, optical, call, tick = ff_rig
    begin(ff_rig)
    snapshot = servo.read()['servo_status']['feedforward']
    servo.snapshot_override.update({k:snapshot[k] for k in
        ('system_seq', 'system_rx_us', 'target_time_us', 'pid_cycle_seq')})
    tick(9)
    assert owner.active and owner.fault is None
    tick(3)
    assert owner.fault and 'freshness deadline' in owner.fault and owner.lease


def test_prewrite_snapshot_transition_does_not_escape_poll_or_write(ff_rig):
    owner, servo, clock, bias, optical, call, tick = ff_rig
    session = begin(ff_rig)
    call('ff_end', token=session['token'])
    original = owner._read_before
    def transient(**kwargs):
        sample = original(**kwargs)
        sample['servo_status']['feedforward']['target_match_valid'] = False
        sample['servo_status']['feedforward']['target_time_us'] = -1
        return sample
    owner._read_before = transient
    writes = len(servo.ff_writes)
    tick(2)
    assert len(servo.ff_writes) == writes and owner.active and owner.fault is None
    owner._read_before = original
    tick(4)
    assert owner.ff['state'] == 'restored' and owner.active


@pytest.mark.parametrize('change', [
    lambda s: s.cfg.update(ff_config_revision=s.cfg['ff_config_revision']+1),
    lambda s: s.cfg.update(ff_profile_digest='f'*64),
    lambda s: s.snapshot_override.update(dac_write_ok=False),
    lambda s: s.snapshot_override.update(dac_output_unknown=True),
    lambda s: s.snapshot_override.update(dac_write_failures=1)])
def test_revision_profile_or_unconfirmed_dac_faults_with_lease(ff_rig, change):
    owner, servo, clock, bias, optical, call, tick = ff_rig
    begin(ff_rig)
    change(servo)
    tick()
    assert owner.fault and owner.lease and owner.ff['state'] == 'fault'


def test_client_close_unwinds_before_native_stop_and_release(ff_rig, monkeypatch):
    owner, servo, clock, bias, optical, call, tick = ff_rig
    device = dict(control_settings=dict(native_current=owner.policy, native_ff_sweep=owner.ff_profile),
                  connection=dict(endpoint='http://127.0.0.1:12345', device_id=servo.device_id))
    port = NativeCurrentPort(device)
    port.lease = owner.lease
    port._rpc = lambda action, **values: owner.dispatch(dict(action=action, **values))
    port.ff_begin(plan())
    monkeypatch.setattr('ldcon.esp32_native_client.time.sleep', lambda _:tick())
    port.close()
    assert owner.ff['state'] == 'restored' and owner.lease is None and bias.lease is None
    assert not owner.active and not port.uncertain_output


def test_initial_invalid_held_adc_cannot_enable_ff(ff_rig):
    owner, servo, clock, bias, optical, call, tick = ff_rig
    # Current broad stationary ADC window can accept zero. It still must not
    # be relabelled as the approved AWG center when the held/used value differs.
    servo.raw = 0
    tick(3)
    with pytest.raises(BrokerError) as exc:
        begin(ff_rig)
    assert exc.value.code == 'measurement' and not servo.ff_writes and owner.active


def test_heartbeat_expiry_autonomously_unwinds(ff_rig):
    owner, servo, clock, bias, optical, call, tick = ff_rig
    begin(ff_rig)
    tick(14)
    assert owner.ff['state'] == 'restored' and owner.active and owner.fault is None
