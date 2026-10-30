"""Dither pause (human request 2026-10-27 "しばらくディザを切ってみてください"): steady_lock.dither.paused is a FREE,
owner_hot switch. Paused = the SAME CH1 ARB bank (name/frequency/carrier range unchanged) rewritten with all-zero
codes; resumed = exactly the reviewed sine payload again. Applied at the owner's safe boundary (settings adoption),
also while the native PI is armed; an uncertain write latches the existing dither fault (no retry).

Offline: the real ECDLOwner / NativeLockOwner / RigHardware / OffsetAWG code over the SCPI + firmware emulator of
owner/tests/fake_rig.py (no hardware, no network); fakes for the hot-control trim and the quality baseline.
"""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import yaml

from ldcon.core.agent_authority import classify, describe_changes, live_class, FREE, SAFETY, FORBIDDEN
from ldcon.core.contracts import DeviceError, MeasurementUnavailable
from ldcon.core.instrument_client import BrokerError
from ldcon.core.interface_schema import digest
from ldcon.drivers.ecdl import dither_payload, arb_name_matches
from ldcon.owners.ecdl_owner import ECDLOwner
from ldcon.owners.ecdl_steady_owner import NativeLockOwner, DITHER_PAUSED_REASON

_spec = importlib.util.spec_from_file_location('dither_pause_fake_rig', (Path(__file__).resolve().parent / 'support') / 'owner/tests/fake_rig.py')
fake_rig = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fake_rig)

PAUSED = ('devices', 'rig', 'control_settings', 'steady_lock', 'dither', 'paused')
PROGRAMMING = {'channel': 1, 'frequency_bounds_hz': [.25, .25], 'max_vpp': .0005, 'nominal_amplitude_vpp': .002}
CH1 = dict(waveform='ARB', enabled=True, amplitude_vpp=.002, value=.9185, arb_name='ldcd0.bin', frequency_hz=.25)


# ---- authority: FREE + owner_hot, the rest of the block unchanged -------------------------------------------------

def test_dither_paused_is_free_and_owner_hot_while_the_rest_of_the_block_keeps_its_class():
    assert classify('lasers', PAUSED) == FREE
    assert live_class('lasers', PAUSED) == 'owner_hot'
    assert classify('lasers', PAUSED[:-1] + ('frequency_hz',)) == SAFETY
    assert live_class('lasers', PAUSED[:-1] + ('frequency_hz',)) == 'snapshot'
    assert classify('lasers', PAUSED[:-1] + ('effective_amplitude_peak',)) == SAFETY
    for leaf in (('programming', 'name'), ('telemetry_endpoint',), ('selector',), ('verified',), ('evidence',)):
        assert classify('lasers', PAUSED[:-1] + leaf) == FORBIDDEN
        assert live_class('lasers', PAUSED[:-1] + leaf) == 'idle_only'
    before = fake_rig.laser_definition()
    after = copy.deepcopy(before)
    after['devices']['rig']['control_settings']['steady_lock']['dither']['paused'] = True
    rows = describe_changes('lasers', before, after)
    assert [(r['path'], r['class'], r['live'], r['old'], r['new']) for r in rows] == [
        ('devices.rig.control_settings.steady_lock.dither.paused', 'free', 'owner_hot', None, True)]


def test_profile_validator_accepts_only_a_boolean_pause():
    from ldcon.owners.ecdl_steady_policy import validate_profile
    profile = fake_rig.laser_definition()['devices']['rig']['control_settings']['steady_lock']
    validate_profile(profile, 'hold')
    profile['dither']['paused'] = True
    validate_profile(profile, 'hold')
    profile['dither']['paused'] = 'yes'
    with pytest.raises(ValueError, match='dither.paused must be boolean'):
        validate_profile(profile, 'hold')


# ---- driver payload ------------------------------------------------------------------------------------------------

def test_dither_payload_refuses_vpp_zero_without_an_explicit_pause_and_zeroes_the_same_bank_with_it():
    with pytest.raises(DeviceError, match='explicit paused=True'):
        dither_payload(1, .25, 0., CH1, PROGRAMMING)
    with pytest.raises(DeviceError, match='explicit paused=True'):
        dither_payload(1, .25, -.0001, CH1, PROGRAMMING)
    with pytest.raises(DeviceError, match='vpp 0'):
        dither_payload(1, .25, .0005, CH1, PROGRAMMING, paused=True)
    normal, sine = dither_payload(1, .25, .0005, CH1, PROGRAMMING)
    assert normal['name'] == 'ldcd1' and normal['paused'] is False and normal['programmed_vpp'] > 0
    assert np.frombuffer(sine, dtype='<i2').max() > 0
    paused, payload = dither_payload(1, .25, 0., CH1, PROGRAMMING, paused=True)
    codes = np.frombuffer(payload, dtype='<i2')
    assert codes.size == 8192 and not codes.any()
    assert paused['name'] == 'ldcd0' and paused['paused'] is True and paused['programmed_vpp'] == 0.
    assert paused['frequency_hz'] == .25 and paused['nominal_amplitude_vpp'] == .002 and paused['offset_v'] == .9185
    assert paused['payload_sha256'] == hashlib.sha256(bytes(8192 * 2)).hexdigest()
    # resume in place: the same bank, the reviewed sine
    resumed, payload = dither_payload(1, .25, .0005, CH1, PROGRAMMING, keep_bank=True)
    assert resumed['name'] == 'ldcd0' and resumed['paused'] is False and payload == sine
    with pytest.raises(DeviceError, match='one of'):
        dither_payload(1, .25, 0., dict(CH1, arb_name='ldcsw1'), PROGRAMMING, paused=True)


# ---- owner over the emulated rig ------------------------------------------------------------------------------------

class Clock:
    def __init__(self):
        self.now = 100.
    def __call__(self):
        return self.now
    def advance(self, seconds):
        self.now += seconds


def make_owner(tmp_path, clock, holder, laser=None):
    path = tmp_path / 'lasers' / 'sr422.yaml'
    if laser is None:
        laser = fake_rig.laser_definition()
    path.parent.mkdir(exist_ok=True)
    path.write_text(yaml.safe_dump(laser))

    def factory(device):
        holder['hw'] = fake_rig.FakeRig(device, clock)
        return holder['hw']
    return ECDLOwner(laser, audit_path=tmp_path / 'audit.jsonl', laser_file=path, factory=factory, clock=clock)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    import ldcon.drivers.siglent as siglent
    monkeypatch.setattr(siglent, 'VisaTransport', fake_rig.Visa)
    monkeypatch.setattr(NativeLockOwner, 'watch_enabled', False)


@pytest.fixture
def fits(monkeypatch):
    """The passive fit (VM telemetry) replaced by a counter: it must never run while paused."""
    calls = []

    def read_passive_dither(self):
        calls.append(self.o.clock())
        import time
        self.dither_report = dict(dither_response=.0011, dither_response_valid=True, dither_analysis_unix_s=time.time(),
                                  dither_source_unix_s=time.time() - 1., dither_analysis_status='valid',
                                  dither_source_start_unix_s=time.time() - 33.)
        return .0011
    monkeypatch.setattr(NativeLockOwner, 'read_passive_dither', read_passive_dither)
    return calls


def call(owner, action, **values):
    return owner.dispatch(dict(action=action, lease=owner.lease, **values))


def lock(owner, hw):
    assert call(owner, 'steady_ready', strategy='hold')['ready']
    call(owner, 'steady_ready', strategy='hold')
    f0 = hw.firmware.frequency()
    assert call(owner, 'steady_arm', strategy='hold', target_frequency_hz=f0, tolerance_hz=5e6)['locked']
    assert hw.firmware.cfg['pid_multiplier'] == 1 and owner.steady.armed and owner.steady.active
    return f0


def save(owner, paused):
    candidate = copy.deepcopy(owner.laser)
    candidate['devices']['rig']['control_settings']['steady_lock']['dither']['paused'] = paused
    owner.laser_file.write_text(yaml.safe_dump(candidate))
    return hashlib.sha256(owner.laser_file.read_bytes()).hexdigest(), candidate


def audits(owner):
    return [json.loads(line) for line in owner.audit_path.read_text().splitlines()]


def kinds(owner):
    return [a['kind'] for a in audits(owner)]


def ch1_codes(hw):
    return hw.bk.uploads['ldcd0']['codes']


def ch1(hw):
    return hw.connections['current_awg'].read(1)


def reviewed_sha(owner):
    return owner.laser['devices']['rig']['control_settings']['steady_lock']['dither']['programming']['payload_sha256']


def wvdt_uploads(hw):
    return [line for line in hw.bk.log if line.startswith('C1:WVDT')]


def test_pause_and_resume_while_armed_rewrite_the_same_bank_at_the_owner_boundary(tmp_path, fits):
    clock, holder = Clock(), {}
    owner = make_owner(tmp_path, clock, holder)
    owner.hw()
    hw = holder['hw']
    call_claim = owner.dispatch(dict(action='claim', revision=digest(owner.device), owner='test'))
    assert call_claim['lease'] == owner.lease
    lock(owner, hw)
    sets_after_arm = len(hw.firmware.sets)
    before = ch1(hw)
    assert arb_name_matches(before['arb_name'], 'ldcd0') and before['frequency_hz'] == .25
    # the dashboard save: deferred under the lease, applied at the next read-only boundary (steady_read)
    revision, candidate = save(owner, True)
    result = owner.dispatch(dict(action='settings_adopt', saved_revision=revision))
    assert result['status'] == 'deferred' and owner.steady.dither_paused() is False
    clock.advance(11.)
    row = call(owner, 'steady_read')
    assert owner.adoption_request is None and owner.laser == candidate and owner.fault is None
    assert owner.steady.p['dither']['paused'] is True and owner.steady.dither_hardware_paused() is True
    # hardware: the SAME bank, zero codes, name/frequency/amplitude/offset/output unchanged, PI untouched
    assert not ch1_codes(hw).any()
    after = ch1(hw)
    assert after == before
    assert len(wvdt_uploads(hw)) == 1 and hw.firmware.cfg['pid_multiplier'] == 1 and len(hw.firmware.sets) == sets_after_arm
    # records
    k = kinds(owner)
    assert k.index('dither_paused_requested') < k.index('dither_readback') < k.index('dither_paused') < k.index('settings_reloaded') < k.index('settings_adopt_applied')
    reloaded = [a for a in audits(owner) if a['kind'] == 'settings_reloaded'][-1]
    assert reloaded['applied'] == ['control_settings.steady_lock.dither.paused'] and reloaded['deferred'] == [] and reloaded['snapshot'] == []
    paused_record = [a for a in audits(owner) if a['kind'] == 'dither_paused'][-1]
    assert paused_record['lease'] == owner.lease and paused_record['armed'] is True and paused_record['reason'] == 'settings adoption'
    assert paused_record['programmed']['paused'] is True and paused_record['programmed']['programmed_vpp'] == 0.
    assert paused_record['programmed']['name'] == 'ldcd0' and paused_record['before'] == paused_record['after']
    programmed = owner.steady.programmed_dither
    assert programmed['paused'] is True and programmed['programmed']['programmed_vpp'] == 0. and Path(programmed['evidence']).is_file()
    assert json.loads(Path(programmed['evidence']).read_text())['programmed']['payload_sha256'] == hashlib.sha256(bytes(16384)).hexdigest()
    # the lock cycle reports the pause and never fits; the durable state carries it
    assert fits == []
    assert row['state'] == 'locked' and row['dither_response'] is None and row['dither_response_valid'] is False
    assert row['dither_analysis_status'] == 'paused' and row['dither_paused'] is True and row['dither_analysis_reason'] == DITHER_PAUSED_REASON
    assert 'stationary_dither_unavailable' not in k
    clock.advance(11.)
    row = call(owner, 'steady_read')
    assert fits == [] and row['dither_analysis_status'] == 'paused'
    info = call(owner, 'response_dither_info')
    assert info['dither_analysis_status'] == 'paused' and info['dither_response_valid'] is False and fits == []
    with pytest.raises(BrokerError, match='paused'):
        call(owner, 'response_dither')
    assert owner.dispatch(dict(action='inspect'))['dither']['dither_analysis_status'] == 'paused'
    state = json.loads(owner.state_path.read_text()) if hasattr(owner, 'state_path') else None
    if state is not None:
        assert state['steady']['programmed_dither']['paused'] is True
    # resume: the reviewed sine payload again, same bank, fits run again
    revision, candidate = save(owner, False)
    assert owner.dispatch(dict(action='settings_adopt', saved_revision=revision))['status'] == 'deferred'
    clock.advance(11.)
    row = call(owner, 'steady_read')
    assert owner.laser == candidate and owner.steady.p['dither']['paused'] is False and owner.fault is None
    assert owner.steady.dither_hardware_paused() is False
    assert hashlib.sha256(ch1_codes(hw).tobytes()).hexdigest() == reviewed_sha(owner)
    assert ch1(hw) == before and len(wvdt_uploads(hw)) == 2
    resumed = [a for a in audits(owner) if a['kind'] == 'dither_resumed'][-1]
    assert resumed['programmed']['payload_sha256'] == reviewed_sha(owner) and resumed['programmed']['paused'] is False
    assert len(fits) == 1 and row['dither_analysis_status'] == 'valid' and row['dither_paused'] is False
    assert owner.steady.programmed_dither['paused'] is False
    assert owner.dispatch(dict(action='settings_adopt'))['status'] == 'unchanged'


def test_pause_is_deferred_while_a_response_study_owns_the_dither_and_applied_at_the_next_boundary(tmp_path, fits):
    clock, holder = Clock(), {}
    owner = make_owner(tmp_path, clock, holder)
    owner.hw()
    hw = holder['hw']
    owner.dispatch(dict(action='claim', revision=digest(owner.device), owner='test'))
    lock(owner, hw)
    revision, candidate = save(owner, True)
    assert owner.dispatch(dict(action='settings_adopt', saved_revision=revision))['status'] == 'deferred'
    owner.steady.response_control = True
    call(owner, 'read')
    assert owner.adoption_request is not None and owner.steady.dither_hardware_paused() is False and wvdt_uploads(hw) == []
    owner.steady.response_control = False
    call(owner, 'read')
    assert owner.adoption_request is None and owner.steady.dither_hardware_paused() is True and not ch1_codes(hw).any()
    assert owner.laser == candidate


def test_lock_started_with_paused_true_programs_the_zero_payload_from_the_start_and_hold_restores_the_sine(tmp_path, fits):
    laser = fake_rig.laser_definition()
    laser['devices']['rig']['control_settings']['steady_lock']['dither']['paused'] = True
    clock, holder = Clock(), {}
    owner = make_owner(tmp_path, clock, holder, laser=laser)
    owner.hw()
    hw = holder['hw']
    owner.dispatch(dict(action='claim', revision=digest(owner.device), owner='test'))
    before = ch1(hw)
    lock(owner, hw)
    k = kinds(owner)
    assert k.index('dither_paused') < k.index('native_pi_requested') < k.index('native_lock_started')
    assert not ch1_codes(hw).any() and ch1(hw) == before
    assert owner.steady.dither_hardware_paused() is True and [a for a in audits(owner) if a['kind'] == 'dither_paused'][0]['reason'] == 'lock start'
    clock.advance(11.)
    row = call(owner, 'steady_read')
    assert row['dither_analysis_status'] == 'paused' and fits == []
    # an unarmed rig holds the reviewed sine again (locked-sweep prepare, claim cleanup and restore_reviewed_dither rely on it)
    assert call(owner, 'steady_hold', reason='test')['held'] is True
    assert hw.firmware.cfg['pid_multiplier'] == 0
    assert hashlib.sha256(ch1_codes(hw).tobytes()).hexdigest() == reviewed_sha(owner)
    assert owner.steady.dither_hardware_paused() is False and kinds(owner)[-1] == 'dither_resumed'
    assert owner.fault is None and ch1(hw) == before
    # the next arm under the still-paused profile pauses again
    lock(owner, hw)
    assert not ch1_codes(hw).any() and kinds(owner).count('dither_paused') == 2
    # commissioning the verified waveform is refused while the pause is set (it would record a zero payload)
    call(owner, 'steady_hold', reason='test')
    with pytest.raises(BrokerError, match='clear steady_lock.dither.paused'):
        call(owner, 'configure_dither', target_frequency_hz=hw.firmware.frequency(), tolerance_hz=5e6)


def test_an_uncertain_pause_write_latches_the_dither_fault_and_is_never_retried(tmp_path, fits, monkeypatch):
    clock, holder = Clock(), {}
    owner = make_owner(tmp_path, clock, holder)
    owner.hw()
    hw = holder['hw']
    owner.dispatch(dict(action='claim', revision=digest(owner.device), owner='test'))
    lock(owner, hw)
    revision, candidate = save(owner, True)
    assert owner.dispatch(dict(action='settings_adopt', saved_revision=revision))['status'] == 'deferred'
    original = hw.bk.write

    def skewed(line):
        original(line)
        if line.startswith('C1:BSWV FRQ'):
            hw.bk.ch[1].wave['FRQ'] = '0.5HZ'          # the instrument reads back another frequency after the write
    monkeypatch.setattr(hw.bk, 'write', skewed)
    clock.advance(11.)
    with pytest.raises(BrokerError, match='Uncertain dither configuration'):
        call(owner, 'steady_read')
    assert owner.fault.startswith('Uncertain dither configuration: Selected dither readback mismatch')
    assert len(wvdt_uploads(hw)) == 1 and hw.firmware.cfg['pid_multiplier'] == 1   # the PI is not touched
    assert owner.steady.p['dither'].get('paused') is not True and owner.laser != candidate   # profile not adopted
    assert 'settings_adopt_refused' in kinds(owner) and 'dither_paused' not in kinds(owner)
    assert json.loads(owner.quarantine_path.read_text())['fault'] == owner.fault and owner.lease is not None
    with pytest.raises(BrokerError, match='Owner fault requires review'):
        call(owner, 'steady_heartbeat')
    clock.advance(11.)
    with pytest.raises(BrokerError):
        call(owner, 'steady_read')
    assert len(wvdt_uploads(hw)) == 1                                              # no second attempt


# ---- hot-control trim: dither guard skipped, quality guard kept ----------------------------------------------------

class TrimLock:
    def __init__(self, dither):
        profile = dict(supervision_interval_s=10., capture_hz=5e6,
                       hot_control=dict(max_trim_v=.003, max_step_v=.001, minimum_dwell_s=40., dither_max=.002),
                       dither=dict(window_s=32., frequency_hz=.25, delivery_delay_s=3.),
                       current_safety=dict(metrics_max=dict(side_peak_height_max=.35, side_peak_prominence_max=.18, main_fwhm_fraction=.27)))
        self.session = SimpleNamespace(device=dict(control_settings=dict(steady_lock=profile, max_sample_age_s=5.)))
        self.dither = dither
        self.t = 0.
        self.current = .9
        self.uncertain_output = False
        self.commands = []
        self.moves = []
        self.policy = SimpleNamespace(unit='V', max_step=.002, settle_s=.5, low=.8, high=1., resolution=1e-4)
    def recovery_ports(self):
        return {'current': SimpleNamespace(policy=self.policy)}
    def hot_control(self, command):
        self.commands.append(command)
        return {}
    def response_info(self):
        return dict(observation=dict(frequency=dict(frequency_hz=700e12, acquired_s=self.t, acquired_unix_s=self.t),
                                     metrics=dict(side_peak_height_max=.1, side_peak_prominence_max=.05, main_fwhm_fraction=.1), mode_id='m'),
                    outputs=dict(current=self.current, temperature=.14, piezo=3e4), native_pi_active=True, target_frequency_hz=700e12)
    def response_dither_info(self):
        return self.dither(self.t)
    def response_begin(self):
        return dict(minimum_dwell_s=40.)
    def response_current(self, value):
        self.moves.append(value)
        self.current = value
        return dict(applied=True, value=value)
    def response_end(self):
        pass


def paused_dither(t):
    return dict(dither_response=None, dither_response_valid=False, dither_analysis_status='paused', dither_paused=True,
                dither_source_unix_s=None, dither_source_start_unix_s=None)


def elevated_dither(t):
    return dict(dither_response=.003, dither_response_valid=True, dither_analysis_status='valid', dither_paused=False,
                dither_source_unix_s=t, dither_source_start_unix_s=t - 32.)


def run_trim(lock, delta_mv=1., steps=40):
    from ldcon.owners.ecdl_hot_control import LocalTrim
    trim = LocalTrim(lock, delta_mv, lambda: lock.t, lambda: lock.t)
    last = None
    for _ in range(steps):
        lock.t += 10.
        try:
            result = trim.poll()
        except MeasurementUnavailable as error:
            last = dict(status='unavailable', reason=str(error))
            if lock.t > trim.deadline:
                return trim, last
            continue
        if result:
            return trim, result
    return trim, last


def test_hot_control_trim_skips_the_dither_guard_with_a_recorded_reason_while_paused():
    from ldcon.owners.ecdl_hot_control import DITHER_GUARD_SKIPPED
    lock = TrimLock(paused_dither)
    trim, result = run_trim(lock)
    assert result['status'] == 'succeeded' and lock.moves == [pytest.approx(.901)]
    assert trim.evidence['dither_guard'] == dict(skipped=True, reason=DITHER_GUARD_SKIPPED, dither_analysis_status='paused')
    step = trim.evidence['steps'][0]
    assert step['dither_trip'] is False and step['dither_guard_skipped'] == DITHER_GUARD_SKIPPED
    assert step['dither_max'] is None and step['dither_mean'] is None and step['quality_trip_metrics'] == []
    assert all(sample['dither_paused'] is True and sample['dither_response'] is None for sample in step['samples'])
    assert all(x['dither_response'] is None for x in trim.evidence['baseline'])
    assert lock.commands == [dict(action='pause_tec_ff')]


def test_hot_control_trim_dither_guard_still_trips_when_the_dither_runs():
    lock = TrimLock(elevated_dither)
    trim, result = run_trim(lock, steps=6)
    assert result == dict(status='unavailable', reason='Trim dither guard') and lock.moves == []
    assert 'dither_guard' not in trim.evidence


# ---- quality baseline: paused = no fit required, recorded as such ------------------------------------------------

def baseline_cycle(source, *, paused):
    row = dict(state='locked', source_acquired_unix_s=source, acquired_s=source, native_input_seq=int(source * 10),
               native_input_stale=False, control_ready_passed=True, target_ramp_active=False, mode_id='desired',
               target_frequency_hz=700e12, owner_context=dict(system_id='laser', generation='owner-1', policy_revision='policy-1'),
               outputs=dict(current=.9, temperature=.14, piezo=25000 + source),
               metrics=dict(side_peak_height_max=.2, side_peak_prominence_max=.1, main_fwhm_fraction=.12, near_main_peak_prominence_max=.01))
    if paused:
        row.update(dither_response=None, dither_response_valid=False, dither_analysis_status='paused', dither_paused=True,
                   dither_response_unit='nm / V', dither_source_unix_s=None, dither_source_start_unix_s=None)
    else:
        row.update(dither_response=.0024, dither_response_valid=True, dither_analysis_status='valid', dither_paused=False,
                   dither_response_unit='nm / V', dither_source_start_unix_s=100.1, dither_source_unix_s=source)
    return row


def test_quality_baseline_treats_a_paused_dither_as_not_required_and_records_it(tmp_path):
    from ldcon.jobs.quality_baseline import QualityBaseline, export_metrics, safe_read, DITHER_PAUSED
    obj = QualityBaseline(tmp_path / 'quality-baseline.json', laser_id='laser', controller_job_id='job-1', require_dither=True, clock=lambda: 100.)
    obj.observe(baseline_cycle(99.9, paused=True), now=100.)
    assert obj.accepted('current', 'adopt-1', 100.)['state'] == 'pending'
    result = None
    for offset in (.5, 1., 1.5, 2., 2.5):
        result = obj.observe(baseline_cycle(100. + offset, paused=True), now=100. + offset)
    assert result == {'state': 'available', 'persisted': True}
    record = safe_read(obj.path)
    assert record is not None and record['dither'] == DITHER_PAUSED == {'status': 'paused'}
    assert record['require_dither'] is True and record['source_start_unix_s'] == 100.5 and record['source_end_unix_s'] == 102.5
    exported = export_metrics(record, now=1000.)
    assert exported['quality_baseline_available'] == 1 and 'quality_baseline_dither_response' not in exported
    # a record claiming a paused dither with extra fields is not a valid record
    corrupt = dict(record, dither={'status': 'paused', 'value': .1})
    obj.path.write_text(json.dumps(corrupt))
    assert safe_read(obj.path) is None
    # the unpaused reference still needs (and records) the fit
    fresh = QualityBaseline(tmp_path / 'quality-baseline-2.json', laser_id='laser', controller_job_id='job-2', require_dither=True, clock=lambda: 200.)
    fresh.observe(baseline_cycle(199.9, paused=False), now=200.)
    assert fresh.accepted('current', 'adopt-2', 200.)['state'] == 'pending'
    assert fresh.observe(baseline_cycle(200.5, paused=True), now=200.5)['state'] == 'pending'
    for offset in (1., 1.5, 2., 2.5, 3.):
        result = fresh.observe(dict(baseline_cycle(200. + offset, paused=False), dither_source_start_unix_s=200.1), now=200. + offset)
    assert result['state'] == 'available' and safe_read(fresh.path)['dither']['value'] == .0024


# ---- window centre: the dither_response observable is unavailable, fast_side_peak unaffected ------------------------

class WindowLock:
    def __init__(self, dither):
        self.t = 100.
        self.dither = dither
        self.session = SimpleNamespace(device={'control_settings': {'steady_lock': {
            'capture_hz': 1e8, 'dither': {'window_s': .2, 'frequency_hz': 10., 'delivery_delay_s': 0.}},
            'optical': {'single_mode_max': {'side_peak_height_max': .35}}}})
        self.axis = SimpleNamespace(low=0., high=2., max_step=.01, resolution=1e-4, update_interval_s=.1, max_slew_per_s=1.)
    def recovery_ports(self):
        return {'current': SimpleNamespace(policy=self.axis)}
    def response_info(self):
        self.t += .01
        return {'outputs': {'current': 1.2, 'temperature': .09, 'piezo': 29000.}, 'native_pi_active': True,
                'observation': {'frequency': {'frequency_hz': 710e12, 'acquired_s': self.t, 'acquired_unix_s': 1e5 + self.t},
                                'mode_id': 'm', 'single_mode': True, 'metrics': {'side_peak_height_max': .2}}}
    def response_dither_info(self):
        return self.dither(self.t)


def window_session(lock, p):
    from ldcon.jobs.ecdl_window_center import WindowCenterSession
    return WindowCenterSession({'request_id': 'w', 'card_id': 'window-center'}, p, lock, {}, lambda *a: None, lambda v: None,
                               {'target_frequency_hz': 710e12}, clock=lambda: lock.t, wall_clock=lambda: 1e5 + lock.t)


def test_window_centre_dither_observable_is_unavailable_while_paused_and_fast_side_peak_is_unaffected():
    lock = WindowLock(paused_dither)
    standard = window_session(lock, {})
    with pytest.raises(MeasurementUnavailable, match='Dither paused'):
        standard._read(lock)

    def never(t):
        raise AssertionError('fast_side_peak must not read the dither')
    lock = WindowLock(never)
    fast = window_session(lock, dict(measure_dither=False, require_single_mode=False, observables=['metrics.side_peak_height_max']))
    row = fast._read(lock)
    assert row['observation']['metrics']['side_peak_height_max'] == .2
