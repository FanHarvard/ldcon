"""Rule M steps 3-4 (2026-10-27): sr1092 CLD conditions by projection, native owner on the laser yaml. Fakes only.

Design: preparations/refactor-20261026/phase2/config-two-layer.md section 5 rows 3-4; card-system-design.md rule M.
The native owner is the real NativeCurrentOwner over the fake servo / bias / optical of jobs/tests; the CLD owner is
the real CLDOwner over MockIO, linked to the native owner's system authority through its RPC dispatch (no HTTP).
"""
import copy
import importlib.util
import json
from pathlib import Path
import socket
from types import SimpleNamespace

import pytest

from experiment_core.records import Journal
from ldcon.owners.esp32_native_owner import digest
from ldcon.owners.cld_projection import CLD_TYPE, CLDProjectionClient, cld_constraints, cld_document
from ldcon.owners.cld_system_owner import compile_policy
from ldcon.owners.esp32_native_owner import NativeCurrentOwner
from ldcon.core.instrument_client import BrokerError
from ldcon.owners.native_policy_update import journal_path, LIVE_ALLOWED
from ldcon.owners.native_system_owner import physical_block
from ldcon.owners.owner_projection import SystemPolicyAuthority

TESTS = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location('native_step34_fixture', (TESTS / 'support') / 'jobs/tests/test_esp32_native_current.py')
native = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(native)
_cld_spec = importlib.util.spec_from_file_location('cld_step34_fixture', TESTS / 'test_cld_system_boundary.py')
cld = importlib.util.module_from_spec(_cld_spec)
_cld_spec.loader.exec_module(cld)
_trim_spec = importlib.util.spec_from_file_location('trim_owner_config', (TESTS / 'support') / 'ops' / 'trim_owner_config.py')
trim_tool = importlib.util.module_from_spec(_trim_spec)
_trim_spec.loader.exec_module(trim_tool)

FIXTURES = TESTS / 'fixtures' / 'effective-settings' / 'var' / 'device-owners'


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError('No hardware/network access in offline test')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.delenv('LDCON_BACKGROUND_LASER_ID', raising=False)


class Gate(native.Gate):
    started = closed = False
    def start(self):
        self.started = True
    def close(self):
        self.closed = True


def profile_for(policy):
    # enabled=False: the fake servo publishes no FF status block (the FF supervision is not under test here)
    return dict(enabled=False, revision='ff-fixture', firmware_version=policy['firmware_version'], window_rule='S1',
                frequency_hz_per_v=1e9, awg_center_v=1., adc_center_count=20000, adc_noise_counts=3.,
                adc_valid_bounds=[19970, 20030], max_half_span_hz=100e6, max_duration_s=30.,
                heartbeat_timeout_s=2., missing_timeout_s=2., unwind_interval_s=.2, unwind_steps=3,
                start_samples=3, start_span_s=.4, start_integral_fraction_max=.5,
                abort_integral_fraction_max=.9, abort_error_hz=10e6)


def laser_for(policy, profile, cld_owner=None):
    """A laser document: the native device, and (optionally) the CLD device bound to a fixture CLD owner."""
    devices = {'native': dict(type='esp32servocon.native_current.owner', connection=dict(endpoint='http://127.0.0.1:1', device_id='fixture-esp'),
                              absolute_conditions={}, control_settings=dict(native_current=copy.deepcopy(policy), native_ff_sweep=copy.deepcopy(profile)))}
    interfaces = {'native_current': dict(device='native', capability='native_current_lock')}
    if cld_owner is not None:
        document = cld.cld_document()
        devices['ldc'] = dict(type=CLD_TYPE, connection=dict(endpoint='http://127.0.0.1:2', instrument='controller', resource=cld_owner.config['resource'],
                                                         projection=dict(principal=cld.PRINCIPAL, key_env=cld.KEY_ENV)),
                              absolute_conditions=document['absolute_conditions'], control_settings=document['control_settings'])
    return dict(schema_version=1, id='test', name='test', allow_output=True, devices=devices, interfaces=interfaces)


class Rig:
    """The native owner with its fakes; ``restart`` builds the next owner on the same state file."""
    def __init__(self, tmp_path, policy=None, profile=None, laser=None, cld_link=None):
        self.tmp_path = tmp_path
        self.clock, self.bias, self.gate = native.Clock(), native.Bias(), Gate()
        self.policy = policy or native.policy()
        self.servo = native.Servo(self.policy)
        self.optical = native.Optical(self.clock)
        self.profile = profile
        self.laser = laser
        self.cld_link = cld_link
        self.owner = self.start(self.policy, self.profile)

    def start(self, policy, profile=None, laser=None):
        self.owner = NativeCurrentOwner(self.servo, .2, 1., None, policy, self.tmp_path / 'state.json',
                                        clock=self.clock, optical=self.optical, bias=self.bias, gate=self.gate,
                                        ff_profile=profile, laser=laser if laser is not None else self.laser, cld_link=self.cld_link)
        self.owner.approvals_root = self.tmp_path / 'approvals'
        self.owner.policy_config_file = self.tmp_path / 'owner-config.json'
        self.owner.policy_config_file.write_text(json.dumps(dict(physical=physical_block(policy))))
        return self.owner

    def call(self, action, **values):
        return self.owner.dispatch(dict(device_id=self.servo.device_id, action=action, lease=self.owner.lease, **values))

    def lock(self):
        self.call('claim', policy_digest=self.owner.definition)
        self.call('prepare', target_wavelength_nm=1091.7855453438308)
        for _ in range(4):
            self.clock.advance()
            self.owner.poll()
        self.call('arm')
        assert self.owner.active and self.owner.fault is None

    def shutdown(self):
        self.owner.stop.set()
        self.owner.run()

    decisions = 0

    def decision(self, rows, *, authority='human_approval', laser_id='test', cls='safety'):
        self.decisions += 1
        identifier = ('%032x' % self.decisions)
        path = self.owner.approvals_root / laser_id / 'decisions' / (identifier + '.json')
        path.parent.mkdir(parents=True, exist_ok=True)
        value = dict(schema_version=1, id=identifier, laser_id=laser_id, authority=authority,
                     scope=dict(kind='lasers', name=laser_id), human_name='offline human',
                     changes=[dict(path=p, old=old, new=new, **{'class': cls}) for p, old, new in rows])
        path.write_text(json.dumps(value))
        return dict(action='policy_update', device_id=self.servo.device_id, expected_definition=self.owner.definition,
                    decision_id=value['id'], laser_id=laser_id)

    def events(self, kind):
        return [e for e in self.owner.input_events if e['kind'] == kind]


def snapshot_binding(owner):
    owner.system_owner.assemblies.factory = SimpleNamespace(laser_snapshot=dict(
        id='test', interfaces=dict(native_current=dict(device='native')),
        devices=dict(native=dict(type='esp32servocon.native_current.owner', connection=dict(device_id=owner.driver.device_id)))))


# ---- step 3: CLD compiled from the projection --------------------------------------------------------------------

def test_cld_policy_is_compiled_from_the_projection_never_from_a_file(tmp_path):
    layer1 = dict(current_limits_a=[.045, .055], modulation_role='repump', temperature_conditions_file=str(tmp_path / 'nope.yaml'),
                  temperature_limits_c=[1., 2.], current_writes_enabled=True)     # legacy keys are not read
    unprojected = compile_policy(layer1)
    assert unprojected['projected'] is False and unprojected['source_revision'] is None
    assert unprojected['current_limits_a'] == [.045, .055] and unprojected['temperature_limits_c'] is None
    assert unprojected['current_writes_enabled'] is False and unprojected['temperature_writes_enabled'] is False
    document = cld.cld_document(current=True, conditions=True)
    document['absolute_conditions']['current_a']['bounds_a'] = [.040, .052]
    projection = dict(schema='ldcon.policy-projection/v1', policy_revision=digest(document), constraints=cld_constraints(document))
    compiled = compile_policy(layer1, projection)
    assert compiled['projected'] and compiled['source_revision'] == digest(document)
    assert compiled['current_limits_a'] == [.045, .052]           # rating (layer 1) intersected with the projection
    assert compiled['temperature_limits_c'] == [20., 30.] and compiled['current_writes_enabled'] is True
    assert compiled['current_conditions'] == document['absolute_conditions']['current_a']
    assert compiled['control_settings'] == document['control_settings'] and compiled['modulation_role'] == 'repump'
    document['absolute_conditions']['current_a']['bounds_a'] = [.056, .060]
    with pytest.raises(ValueError, match='Conflicting CLD current envelopes'):
        compile_policy(layer1, dict(projection, constraints=cld_constraints(document)))


def test_projection_install_refuses_a_stale_expected_revision_and_a_foreign_principal(tmp_path):
    owner, raw, call = cld.owner_fixture(tmp_path, current=True)
    try:
        s = owner._test_system
        from ldcon.owners.projection_transport import sign, key_from_environment
        status = s.client.status()
        document = s.client.document(status)
        receipt = sign(s.journal, cld.PRINCIPAL, key_from_environment(cld.KEY_ENV), 'projection', document, lease=None, expected_revision='stale')
        with pytest.raises(BrokerError, match='Physical projection changed|superseded|invalid'):
            owner.dispatch(dict(action='projection_install', instrument='controller', principal=cld.PRINCIPAL,
                                expected_revision='stale', receipt=receipt))
        with pytest.raises(BrokerError, match='No configured projection source'):
            owner.dispatch(dict(action='projection_install', instrument='controller', principal='someone-else',
                                expected_revision=status['projection_revision'], receipt=receipt))
        assert owner.physical_writer.guard.inspect()['projection_revision'] == status['projection_revision']
        assert not owner.instrument.fault and not cld.writes(raw)
    finally:
        owner.close()


def test_tuning_save_is_fenced_by_the_projection_revision():
    """current_owner.save_tuning: the former 'laser file changed; reload owner' check is a projection-revision fence."""
    from ldcon.owners.current_owner import save_tuning
    from ldcon.core.current_settings import settings_draft
    from ldcon.core.device_conditions import policy_digest
    settings = settings_draft()
    settings.update(enabled=True, output_update_interval_s=.1, activation_range_a=[-.001, .001])
    settings['pid'].update(kp_a_per_hz=1e-12, ki_a_per_hz_s=0., integral_limit_a=.0001)
    conditions = dict(bounds_a=[.045, .055], max_slew_a_per_s=.001, max_step_a=.0001, min_update_interval_s=.1, expressions=[])
    expected = policy_digest({'current': settings})
    context = dict(guard=SimpleNamespace(settings_digest=expected), activation={'current_a': .05}, source_revision='rev-a')
    instrument = SimpleNamespace(config=dict(current_writes_enabled=True, control_settings={'current': settings},
                                             current_conditions=conditions, source_revision='rev-b'),
                                 io=SimpleNamespace(current_activation=context))
    with pytest.raises(ValueError, match='re-projected since activation'):
        save_tuning(instrument, expected, {'pid': dict(settings['pid']), 'output_update_interval_s': .2})


def test_cld_starts_with_an_untrimmed_config_and_restores_its_projection_for_reads(tmp_path):
    raw = cld.MockIO()
    owner = cld.CLDOwner(cld.config(tmp_path, legacy=True, current=True), io_factory=lambda: raw,
                         lock_factory=lambda resource: SimpleNamespace(close=lambda: None))
    try:
        audit = [json.loads(line) for line in owner.audit_file.read_text().splitlines()]
        fallback = [a for a in audit if a['kind'] == 'layer1_copy_fallback']
        assert len(fallback) == 1 and 'temperature_conditions' in fallback[0]['keys'] and 'current_writes_enabled' in fallback[0]['keys']
        assert owner.layer1 == {'current_limits_a': [.045, .055]} and 'controller' not in owner.config
        owner.refresh()
        assert owner.status()['projected'] is False
        # pre-step-3 bias path from the copies until the projection arrives
        bias = owner.dispatch(dict(action='bias_claim', instrument='controller', resource=owner.config['resource']))
        owner.dispatch(dict(action='bias_release', instrument='controller', lease=bias['lease'], native_stopped=True))
        s = cld.system(tmp_path, owner, cld.cld_document(current=True))
        s.client.install()
        assert owner.status()['projected'] and owner.status()['policy_revision'] == s.authority.revision
        audit = [json.loads(line)['kind'] for line in owner.audit_file.read_text().splitlines()]
        assert audit.count('layer1_copy_ignored') == 1 and audit.index('layer1_copy_ignored') > audit.index('projection_installed')
        owner.close()
        # a state file of an owner before step 3 (definition included the policy revision), no retained lease:
        # the next owner starts, audits the definition change, and re-reads the installed projection: reads and
        # the bias bounds are served, a write needs a fresh install (its guard generation changed)
        state = json.loads((tmp_path / 'state.json').read_text())
        state['definition'] = 'legacy-definition-with-policy-revision'
        (tmp_path / 'state.json').write_text(json.dumps(state))
        raw2 = cld.MockIO()
        restarted = cld.CLDOwner(cld.config(tmp_path, current=True), io_factory=lambda: raw2,
                                 lock_factory=lambda resource: SimpleNamespace(close=lambda: None))
        audit = [json.loads(line) for line in restarted.audit_file.read_text().splitlines()]
        assert any(a['kind'] == 'definition_changed_at_start' for a in audit)
        restarted.refresh()
        assert restarted.status()['projected'] and restarted.status()['policy_revision'] == s.authority.revision
        lease = restarted.dispatch(dict(action='claim', instrument='controller', channel=0, profile='cld1015',
                                        resource=restarted.config['resource']))['lease']
        call = lambda method, **v: restarted.dispatch(dict(action='controller', instrument='controller', lease=lease, method=method, **v))
        assert call('describe')['current_writes_enabled'] is True
        with pytest.raises(BrokerError, match='fresh install|stale'):
            call('apply', values={'current_a': .051})
        assert not cld.writes(raw2)
        s.client.link = cld.Link(restarted)
        s.client.install()                                       # the system owner's periodic ensure does this
        assert call('apply', values={'current_a': .051})['current_a'] == .051
        restarted.close()
    finally:
        owner.close()


# ---- step 4: native owner on the laser yaml -------------------------------------------------------------------

def test_native_starts_with_the_old_config_copies_ignored_and_audited(tmp_path, monkeypatch):
    from ldcon.owners.monitor_owner import load_native_assembly, startup_policy
    monkeypatch.setattr('ldcon.core.interface_schema.validate_laser', lambda value: value)
    policy = native.policy()
    laser = laser_for(policy, profile_for(policy))
    import yaml
    laser_file = tmp_path / 'laser.yaml'
    laser_file.write_text(yaml.safe_dump(laser))          # YAML keeps 5.0e-05 numeric (JSON's 5e-05 would be a string)
    config = dict(driver='esp32servocon', device_id='fixture-esp', native_current=copy.deepcopy(policy),
                  native_ff_sweep=profile_for(policy), native_assembly={'laser_file': str(laser_file)},
                  state_file=str(tmp_path / 'state.json'))
    config['native_current']['max_step_a'] = .0002                 # stale copy: ignored, the yaml decides
    monkeypatch.setattr('ldcon.jobs.native_ff_sweep.configuration', lambda laser: {'broker': {'projection': {'principal': 'laser', 'key_env': 'X'}}})
    factory, document, source = load_native_assembly(config, tmp_path / 'config.json')
    started, profile, ignored = startup_policy(config, document)
    assert ignored == ['native_current', 'native_ff_sweep'] and started['max_step_a'] == policy['max_step_a']
    rig = Rig(tmp_path, policy=started, profile=profile)
    rig.owner._report_event('layer1_copy_ignored', keys=ignored)
    assert rig.owner.policy == policy and rig.owner.ff_profile == profile
    assert [e['keys'] for e in rig.events('layer1_copy_ignored')] == [ignored]
    assert rig.owner.fault is None and rig.servo.writes == []


def test_idle_restart_adopts_the_yaml_policy_and_profile_without_firmware_writes(tmp_path):
    policy = native.policy()
    rig = Rig(tmp_path, policy=policy, profile=profile_for(policy))
    assert rig.owner.system_owner.startup['state'] == 'equal'
    rig.shutdown()
    edited = native.policy()
    edited['current_bounds_a'] = [.046, .054]
    edited['native']['integral_limit_count'] = 2.
    profile = profile_for(edited)
    profile['unwind_steps'] = 5
    restarted = rig.start(edited, profile)
    assert restarted.policy == edited and restarted.definition == digest(edited)
    assert restarted.ff_profile == profile and restarted.system_owner.startup['state'] == 'adopted'
    assert rig.events('policy_adopted_at_start')[0]['changed_paths'] == ['current_bounds_a', 'native.integral_limit_count']
    assert rig.events('ff_profile_adopted_at_start')
    state = json.loads(restarted.state_file.read_text())
    assert state['definition'] == digest(edited) and state['ff_profile'] == profile
    assert rig.servo.writes == [] and restarted.fault is None
    assert restarted.snapshot()['policy_authority']['saved_revision'] == digest(edited)
    # the journal is the effective record: a third start on the same yaml sees no difference
    rig.shutdown()
    again = rig.start(edited, profile)
    assert again.system_owner.startup['state'] == 'equal' and again.policy == edited


def test_restart_under_an_active_lease_keeps_the_effective_policy_and_reports_the_yaml_pending(tmp_path):
    policy = native.policy()
    rig = Rig(tmp_path, policy=policy, profile=profile_for(policy))
    rig.lock()
    lease = rig.owner.lease
    rig.shutdown()
    edited = native.policy()
    edited['activation_range_a'] = [-.002, .002]
    profile = profile_for(edited)
    profile['max_half_span_hz'] = 50e6
    restarted = rig.start(edited, profile)
    assert restarted.active and restarted.lease == lease and restarted.fault is None     # re-adoption intact
    assert restarted.policy == policy and restarted.ff_profile == profile_for(policy)
    assert restarted.system_owner.startup['state'] == 'pending'
    pending = rig.events('policy_yaml_pending_at_start')[0]
    assert pending['changed_paths'] == ['activation_range_a'] and pending['saved_revision'] == digest(edited)
    assert rig.events('ff_profile_yaml_pending_at_start')
    status = restarted.snapshot()['policy_authority']
    assert status['startup']['pending'] and status['saved_revision'] == digest(edited) != status['effective_revision']
    assert rig.servo.writes[-1] == {'pid_multiplier': 1} or all('pid_multiplier' not in w or w['pid_multiplier'] == 1 for w in rig.servo.writes)


def test_yaml_differences_outside_the_live_fields_refuse_the_start(tmp_path):
    policy = native.policy()
    rig = Rig(tmp_path, policy=policy)
    rig.shutdown()
    edited = native.policy()
    edited['calibration']['current_a_per_v'] = .16
    with pytest.raises(ValueError, match='outside its live-adoptable fields.*calibration.current_a_per_v'):
        rig.start(edited)


def test_native_ff_sweep_hot_update_is_accepted_with_the_profile_proof(tmp_path):
    policy = native.policy()
    profile = profile_for(policy)
    rig = Rig(tmp_path, policy=policy, profile=profile)
    snapshot_binding(rig.owner)
    rig.lock()
    writes = copy.deepcopy(rig.servo.writes)
    request = rig.decision([('devices.native.control_settings.native_ff_sweep.max_half_span_hz', 100e6, 80e6),
                            ('devices.native.control_settings.native_ff_sweep.unwind_steps', 3, 4)])
    prepared = rig.owner.dispatch(dict(request, action='policy_update_prepare'))
    assert prepared['status'] == 'prepared' and prepared['approved_definition'] == rig.owner.definition
    assert prepared['approved_ff_profile_digest'] != rig.owner.ff_definition
    result = rig.owner.dispatch(request)
    assert result['status'] == 'updated' and result['hardware_writes'] is False and rig.servo.writes == writes
    assert rig.owner.ff_profile['max_half_span_hz'] == 80e6 and rig.owner.ff_profile['unwind_steps'] == 4
    assert rig.owner.policy == policy and rig.owner.active and rig.owner.fault is None
    state = json.loads(rig.owner.state_file.read_text())
    assert state['ff_profile'] == rig.owner.ff_profile and state['ff_profile_digest'] == rig.owner.ff_definition
    assert json.loads(journal_path(rig.owner.state_file).read_text())['status'] == 'committed'
    assert rig.owner.dispatch(request)['idempotent'] is True
    assert 'native_ff_sweep' in LIVE_ALLOWED
    # the next owner starts on the effective profile (the yaml carries the same values after the dashboard save)
    rig.shutdown()
    restarted = rig.start(policy, rig.owner.ff_profile)
    assert restarted.ff_profile['max_half_span_hz'] == 80e6 and restarted.ff_startup['state'] == 'equal'


@pytest.mark.parametrize('case', ['firmware', 'unapproved', 'ff_session', 'invalid'])
def test_profile_changes_without_proof_or_approval_are_refused(tmp_path, case):
    policy = native.policy()
    rig = Rig(tmp_path, policy=policy, profile=profile_for(policy))
    snapshot_binding(rig.owner)
    rig.lock()
    before = rig.owner.ff_profile
    writes = copy.deepcopy(rig.servo.writes)
    if case == 'firmware':
        request = rig.decision([('devices.native.control_settings.native_ff_sweep.firmware_version', policy['firmware_version'], 'other')])
        match = 'FF profile proof failed'
    elif case == 'unapproved':
        request = rig.decision([('devices.native.control_settings.native_ff_sweep.max_half_span_hz', 100e6, 80e6)], authority='free')
        match = 'explicit human approval'
    elif case == 'ff_session':
        rig.owner.ff = dict(state='active', token='t')
        request = rig.decision([('devices.native.control_settings.native_ff_sweep.max_half_span_hz', 100e6, 80e6)])
        match = 'Finish the active target/FF experiment'
    else:
        request = rig.decision([('devices.native.control_settings.native_ff_sweep.adc_valid_bounds', [19970, 20030], [20100, 20200])])
        match = 'FF profile proof failed'
    with pytest.raises(BrokerError, match=match):
        rig.owner.dispatch(request)
    assert rig.owner.ff_profile == before and rig.servo.writes == writes
    assert not journal_path(rig.owner.state_file).exists() and rig.owner.fault is None


# ---- steps 3+4 together: the native owner is the sr1092 system owner that projects to the CLD -------------------

@pytest.fixture
def pair(tmp_path):
    """A real CLD owner (MockIO) and a real native owner whose system authority projects to it."""
    raw = cld.MockIO()
    (tmp_path / 'cld').mkdir(exist_ok=True)
    cfg = cld.config(tmp_path / 'cld')
    cfg['system_id'] = cfg['projection_sources'][cld.PRINCIPAL]['system_id'] = 'test'   # the native fixture laser
    cld_owner = cld.CLDOwner(cfg, io_factory=lambda: raw, lock_factory=lambda resource: SimpleNamespace(close=lambda: None))
    cld_owner.refresh()
    link = cld.Link(cld_owner)
    policy = native.policy()
    laser = laser_for(policy, profile_for(policy), cld_owner)
    rig = Rig(tmp_path, policy=policy, profile=profile_for(policy), laser=laser, cld_link=link)
    snapshot_binding(rig.owner)
    yield rig, cld_owner, raw, link, laser
    cld_owner.close()


def test_system_owner_projects_the_cld_conditions_at_start_and_before_the_bias_claim(pair):
    rig, cld_owner, raw, link, laser = pair
    system = rig.owner.system_owner
    assert system.cld is not None and system.cld_authority.snapshot() == cld_document(laser, 'ldc')
    assert cld_owner.status()['projected'] and cld_owner.status()['policy_revision'] == system.cld_authority.revision
    assert 'projection_install' in link.calls
    described = rig.owner.snapshot()['policy_authority']['cld']
    assert described['effective_revision'] == system.cld_authority.revision and described['projection']['last_error'] is None
    rig.lock()                                                   # claim: cld.require() then the bias claim
    assert rig.bias.lease and rig.owner.active
    assert not cld.writes(raw)


def test_cld_conditions_change_live_through_policy_update_without_a_cld_restart(pair):
    rig, cld_owner, raw, link, laser = pair
    rig.lock()
    system = rig.owner.system_owner
    old = system.cld_authority.revision
    generation = cld_owner.physical_writer.guard.generation
    writes = copy.deepcopy(rig.servo.writes)
    request = rig.decision([('devices.ldc.control_settings.temperature.output_update_interval_s', None, .5),
                            ('devices.ldc.control_settings.current.enabled', False, True)])
    prepared = rig.owner.dispatch(dict(request, action='policy_update_prepare'))
    assert prepared['status'] == 'prepared' and prepared['kind'] == 'cld' and prepared['cld_expected_revision'] == old
    result = rig.owner.dispatch(request)
    assert result['status'] == 'updated' and result['cld_approved_revision'] == system.cld_authority.revision != old
    assert system.cld_authority.snapshot()['control_settings']['temperature'] == {'output_update_interval_s': .5}
    assert cld_owner.status()['policy_revision'] == system.cld_authority.revision
    assert cld_owner.physical_writer.guard.generation == generation          # no CLD restart
    lease = cld_owner.dispatch(dict(action='claim', instrument='controller', channel=0, profile='cld1015', resource=cld_owner.config['resource']))
    described = cld_owner.dispatch(dict(action='controller', instrument='controller', lease=lease['lease'], method='describe'))
    assert described['control_settings']['temperature']['output_update_interval_s'] == .5 and described['current_writes_enabled'] is True
    assert rig.servo.writes == writes and rig.owner.active and rig.owner.fault is None and not cld.writes(raw)
    assert json.loads(journal_path(rig.owner.state_file).read_text())['status'] == 'committed'
    with pytest.raises(BrokerError, match='separate decisions'):
        rig.owner.dispatch(rig.decision([('devices.ldc.control_settings.current.enabled', True, False),
                                         ('devices.native.control_settings.native_current.max_step_a', .00005, .0001)]))
    with pytest.raises(BrokerError, match='Only the CLD operating conditions'):
        rig.owner.dispatch(rig.decision([('devices.ldc.connection.endpoint', 'http://127.0.0.1:2', 'http://127.0.0.1:3')]))


def test_cld_restart_is_re_projected_by_the_periodic_tick(pair):
    rig, cld_owner, raw, link, laser = pair
    system = rig.owner.system_owner
    cld_owner.close()
    cfg = cld.config(rig.tmp_path / 'cld')
    cfg['system_id'] = cfg['projection_sources'][cld.PRINCIPAL]['system_id'] = 'test'
    restarted = cld.CLDOwner(cfg, io_factory=lambda: cld.MockIO(), lock_factory=lambda resource: SimpleNamespace(close=lambda: None))
    try:
        restarted.refresh()
        link.owner = restarted
        before = len(link.calls)
        rig.clock.advance(system.cld_check_interval_s + 1)
        rig.owner.poll()
        assert link.calls[before:].count('projection_install') == 1
        assert restarted.status()['policy_revision'] == system.cld_authority.revision
        assert restarted.physical_writer.guard.inspect()['projection']['instrument_generation'] == restarted.physical_writer.guard.generation
        assert rig.owner.fault is None
    finally:
        restarted.close()


def test_cld_projection_failure_at_claim_is_reported_and_the_bias_path_proceeds(pair):
    """Coordinator 2026-10-27: a missing signing key / unreachable CLD projection must not refuse every lock; the bias
    claim proceeds on the CLD's own policy (pre-step-3 path) and the owner reports it."""
    rig, cld_owner, raw, link, laser = pair
    def down(action, **values):
        raise OSError('connection refused')
    link.rpc = down
    rig.clock.advance(rig.owner.system_owner.cld_check_interval_s + 1)
    rig.owner.poll()
    assert rig.owner.fault is None and 'connection refused' in rig.owner.snapshot()['policy_authority']['cld']['projection']['last_error']
    rig.call('claim', policy_digest=rig.owner.definition)
    assert rig.owner.lease and rig.bias.lease and rig.owner.fault is None
    assert rig.events('cld_projection_unavailable')[0]['error'].endswith('connection refused')


def test_cld_without_projection_sources_derives_the_site_binding_and_falls_back_to_its_copies(tmp_path, monkeypatch):
    """Coordinator 2026-10-27: no config edit before the first start. principal laser-<slug> / LDCON_PROJECTION_<SLUG>
    from system_id; without the key the CLD keeps the pre-step-3 bias path (copies) and audits it; with the key the
    system owner projects under the derived principal and the copies are ignored."""
    monkeypatch.delenv('LDCON_PROJECTION_SR1092', raising=False)
    cfg = cld.config(tmp_path, legacy=True)
    cfg.pop('projection_sources')
    cfg['system_id'] = 'Sr1092'
    raw = cld.MockIO()
    owner = cld.CLDOwner(cfg, io_factory=lambda: raw, lock_factory=lambda resource: SimpleNamespace(close=lambda: None))
    try:
        audit = {a['kind']: a for a in (json.loads(line) for line in owner.audit_file.read_text().splitlines())}
        assert audit['projection_source_derived']['principal'] == 'laser-sr1092'
        assert audit['projection_source_derived']['key_env'] == 'LDCON_PROJECTION_SR1092' and audit['projection_source_derived']['channels'] == [0]
        assert audit['projection_key_absent']['key_env'] == 'LDCON_PROJECTION_SR1092'
        assert audit['layer1_copy_fallback']['keys'] == ['control_settings', 'current_writes_enabled', 'temperature_conditions',
                                                         'temperature_limits_c', 'temperature_writes_enabled']
        owner.refresh()
        status = owner.status()
        assert status['projected'] is False and status['policy_revision'] is None
        lease = owner.dispatch(dict(action='claim', instrument='controller', channel=0, profile='cld1015', resource=cfg['resource']))['lease']
        described = owner.dispatch(dict(action='controller', instrument='controller', lease=lease, method='describe'))
        assert described['temperature_limits_c'] == [20., 30.] and described['policy_authority']['fallback'] == 'layer1 copy'
        assert described['policy_authority']['signed'] is False and described['policy_authority']['principal'] == 'laser-sr1092'
        with pytest.raises(BrokerError, match='no installed system projection'):
            owner.dispatch(dict(action='controller', instrument='controller', lease=lease, method='apply', values={'temperature_c': 25.}))
        owner.dispatch(dict(action='release', instrument='controller', lease=lease))
        bias = owner.dispatch(dict(action='bias_claim', instrument='controller', resource=cfg['resource']))
        assert bias['bias_held']
        owner.dispatch(dict(action='bias_release', instrument='controller', lease=bias['lease'], native_stopped=True))
        assert not cld.writes(raw)
        owner.close()
        # the key provisioned (the site secrets env of that laser): the next owner verifies receipts, the system owner
        # projects under the derived principal, and the copies are ignored from then on
        monkeypatch.setenv('LDCON_PROJECTION_SR1092', 'offline-fixture-sr1092-key-0123456789abcdef0123456789')
        signed = cld.CLDOwner(cfg, io_factory=lambda: cld.MockIO(), lock_factory=lambda resource: SimpleNamespace(close=lambda: None))
        signed.refresh()
        assert signed.system_owner.signed and signed.system_owner.principal == 'laser-sr1092'
        journal = Journal(tmp_path / 'system.sqlite3')
        authority = SystemPolicyAuthority('Sr1092', cld.cld_document(current=True), dependencies={'cld_connection': 'x'}, journal=journal, scope='Sr1092.cld')
        laser = {'id': 'sr1092', 'devices': {'ldc': {'type': CLD_TYPE, 'connection': dict(endpoint='http://127.0.0.1:1', instrument='controller',
                                                                                        resource=cfg['resource'])}}}
        client = CLDProjectionClient(authority, laser, 'ldc', journal=journal, link=cld.Link(signed))
        assert client.principal == 'laser-sr1092' and client.key_env == 'LDCON_PROJECTION_SR1092'    # derived on the system side too
        client.install()
        assert signed.status()['projected'] and signed.status()['policy_revision'] == authority.revision
        audit = [json.loads(line)['kind'] for line in signed.audit_file.read_text().splitlines()]
        assert audit[-2:] == ['projection_installed', 'layer1_copy_ignored'] and 'projection_key_absent' not in audit[len(audit) - 4:]
        signed.close()
    finally:
        owner.close()


def test_live_sr1092_yaml_with_the_numeric_string_starts_both_owners(tmp_path):
    """The live sr1092.yaml carries slow_control.output_resolution as the YAML string '5e-05' (step 2 finding). Neither
    the native owner's startup policy nor the CLD projection refuses it; the effective tool's DIVERGED row is the only
    consequence until the human corrects the yaml."""
    from ldcon.core.interface_schema import load_mapping, validate_laser
    from ldcon.owners.monitor_owner import startup_policy
    fixture_root = TESTS / 'fixtures' / 'effective-settings' / 'var'
    laser = validate_laser(load_mapping((fixture_root / 'lasers' / 'sr1092' / 'config' / 'lasers' / 'sr1092.yaml').read_text()))
    assert laser['devices']['ldc']['control_settings']['slow_control']['output_resolution'] == '5e-05'
    esp = json.loads((FIXTURES / 'esp32servocon-sr1092' / 'config.json').read_text())
    policy, profile, ignored = startup_policy(esp, laser)
    assert policy == laser['devices']['native_esp']['control_settings']['native_current'] and ignored == ['native_current', 'native_ff_sweep']
    document = cld_document(laser, 'ldc')
    constraints = cld_constraints(document)
    assert constraints['control_settings']['slow_control']['output_resolution'] == '5e-05'
    compiled = compile_policy({'current_limits_a': [.045, .055]}, dict(policy_revision='r', constraints=constraints))
    assert compiled['current_limits_a'] == [.045, .055] and compiled['current_writes_enabled'] is True


# ---- ops/trim_owner_config.py ----------------------------------------------------------------------------------

def test_trim_dry_run_lists_the_copies_and_the_trimmed_files_still_start(tmp_path, capsys, monkeypatch):
    from ldcon.owners.cld_owner import validate_config
    from ldcon.owners.monitor_owner import startup_policy
    esp = json.loads((FIXTURES / 'esp32servocon-sr1092' / 'config.json').read_text())
    trimmed, report = trim_tool.trim(esp)
    assert report == dict(kind='native', removed=['native_current', 'native_ff_sweep'], added=['physical'], notes=report['notes'])
    assert set(trimmed['physical']) == {'firmware_version', 'dac', 'adc', 'calibration'} and 'native_current' not in trimmed
    assert trimmed['physical']['calibration'] == esp['native_current']['calibration']
    laser = json.loads(json.dumps({'devices': {'native_esp': {'control_settings': {'native_current': esp['native_current'], 'native_ff_sweep': esp['native_ff_sweep']}}},
                                   'interfaces': {'native_current': {'device': 'native_esp'}}}))
    policy, profile, ignored = startup_policy(trimmed, laser)
    assert ignored == [] and policy == esp['native_current'] and profile == esp['native_ff_sweep']
    cld_config = json.loads((FIXTURES / 'cld1015-M01343851' / 'config.json').read_text())
    trimmed_cld, report = trim_tool.trim(cld_config, principal='laser-sr1092', key_env='LDCON_PROJECTION_SR1092')
    assert report['kind'] == 'cld' and report['added'] == ['projection_sources.laser-sr1092']
    assert set(report['removed']) == {'controller.' + k for k in ('control_settings', 'current_conditions', 'current_writes_enabled',
                                                                 'temperature_conditions', 'temperature_limits_c', 'temperature_writes_enabled')}
    assert trimmed_cld['controller'] == {'current_limits_a': [.045, .055], 'modulation_role': 'repump_1092_current'}
    assert trimmed_cld['projection_sources'] == {'laser-sr1092': dict(system_id='Sr1092', key_env='LDCON_PROJECTION_SR1092', channels=[0])}
    validate_config(trimmed_cld)
    _, report = trim_tool.trim(cld_config)
    assert any('projection_sources is absent' in note for note in report['notes'])
    # the CLI dry run writes nothing
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(esp))
    assert trim_tool.main([str(path), '--dry-run']) == 0
    out = capsys.readouterr().out
    assert 'remove  native_current' in out and 'dry run: nothing written' in out
    assert json.loads(path.read_text()) == esp
    assert trim_tool.main([str(path), '--apply']) == 0
    assert json.loads(path.read_text()) == trimmed and list(tmp_path.glob('config.json.bak-*'))
