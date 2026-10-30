"""CLD system policy and physical projection isolation, using USB-free fakes.

Rule M step 3 (2026-10-27): the CLD owner holds no laser policy. Its operating conditions are the laser system
owner's projection (``cld_projection.CLDProjectionClient``), installed here over a fake link with the real signed
receipt; config.json carries only the physical facts (``current_limits_a`` as the device rating, design Q1).
"""
from copy import deepcopy
import json
import os
from types import SimpleNamespace

import pytest

from experiment_core.errors import AuthorizationRequired
from experiment_core.records import Journal
from ldcon.owners.cld_owner import CLDOwner
from ldcon.owners.cld_projection import CLD_TYPE, CLDProjectionClient
from ldcon.drivers.cld1015 import MockIO
from ldcon.core.instrument_client import BrokerError
from ldcon.owners.instrument_service import Hub
from ldcon.owners.owner_projection import SystemPolicyAuthority

KEY_ENV = 'LDCON_TEST_CLD_PROJECTION_KEY'
os.environ.setdefault(KEY_ENV, 'offline-fixture-projection-key-0123456789abcdef')
PRINCIPAL = 'laser-fixture'


def config(tmp_path, *, bound=True, current=False, conditions=False, legacy=False):
    resource = 'USB0::4883::32847::DUMMY::0::INSTR'
    handoff = tmp_path / 'handoff.json'
    handoff.write_text(json.dumps(dict(schema_version=1, resource=resource, leases_empty=True,
        transport_closed=True, reviewed_by='offline fixture', reviewed_at_utc='fixture',
        evidence='synthetic', disposition='new_device')))
    source = dict(current_limits_a=[.045, .055])       # layer 1: the diode rating (design Q1)
    if legacy:
        # an untrimmed config.json still carries the layer-2 copies: ignored at start, audited once
        source.update(temperature_limits_c=[20., 30.], current_writes_enabled=current, temperature_writes_enabled=True,
                      control_settings={}, temperature_conditions=dict(bounds_c=[20., 30.], max_slew_c_per_s=.01, max_step_c=.005, expressions=[]))
    value = dict(device_id='cld_fixture', instrument='controller', resource=resource,
        identity={'manufacturer': 'THORLABS', 'model': 'CLD1015', 'serial': 'DUMMY'}, controller=source,
        bind='127.0.0.1', port=47142, poll_s=.1, max_age_s=5., io_timeout_s=5.,
        read_unavailable_timeout_s=10., state_file=str(tmp_path / 'state.json'),
        handoff_file=str(handoff), writes_enabled=True)
    if bound:
        value['system_id'] = 'laser'
        value['projection_sources'] = {PRINCIPAL: dict(system_id='laser', key_env=KEY_ENV, channels=[0])}
    return value


def cld_document(*, current=False, conditions=False):
    """The laser system owner's CLD policy document (what devices.ldc of the laser yaml says)."""
    settings = {'current': {'enabled': current}}
    absolute = {'temperature_c': dict(bounds_c=[20., 30.], max_slew_c_per_s=.01, max_step_c=.005, expressions=[])}
    if conditions:
        from ldcon.core.current_settings import settings_draft
        draft = settings_draft()
        draft.update(enabled=True, output_update_interval_s=.1, activation_range_a=[-.001, .001])
        draft['pid'].update(kp_a_per_hz=1e-12, ki_a_per_hz_s=0., integral_limit_a=.0001)
        settings = {'current': draft}
        absolute['current_a'] = dict(bounds_a=[.045, .055], max_slew_a_per_s=.001, max_step_a=.0001,
                                     min_update_interval_s=.1, expressions=[])
    return dict(device='ldc', allow_output=True, absolute_conditions=absolute, control_settings=settings)


class Link:
    """The system side talks to the CLD owner through its RPC dispatch (no HTTP in tests)."""
    def __init__(self, owner):
        self.owner = owner
        self.calls = []

    def rpc(self, action, **values):
        self.calls.append(action)
        return self.owner.dispatch(dict(action=action, instrument='controller', **values))


def system(tmp_path, owner, document, *, link=None):
    """A real laser-side CLD authority + projection client bound to this owner (signed receipts, fake transport)."""
    journal = Journal(tmp_path / 'system-authority.sqlite3')
    authority = SystemPolicyAuthority('laser', document, dependencies={'cld_connection': 'fixture'}, journal=journal, scope='laser.cld')
    laser = {'devices': {'ldc': {'type': CLD_TYPE, 'connection': dict(endpoint='http://127.0.0.1:1', instrument='controller',
                                  resource=owner.config['resource'], projection=dict(principal=PRINCIPAL, key_env=KEY_ENV))}}}
    client = CLDProjectionClient(authority, laser, 'ldc', journal=journal, link=link or Link(owner))
    return SimpleNamespace(authority=authority, client=client, journal=journal)


def owner_fixture(tmp_path, *, project=True, **kwargs):
    raw = MockIO()
    owner = CLDOwner(config(tmp_path, **kwargs), io_factory=lambda: raw,
                     lock_factory=lambda resource: SimpleNamespace(close=lambda: None))
    owner.refresh()
    if project:
        owner._test_system = system(tmp_path, owner, cld_document(current=kwargs.get('current', False), conditions=kwargs.get('conditions', False)))
        owner._test_system.client.install()
    claimed = owner.dispatch(dict(action='claim', instrument='controller', channel=0, profile='cld1015',
                                  resource=owner.config['resource'], owner='offline'))
    def call(method, **values):
        return owner.dispatch(dict(action='controller', instrument='controller', lease=claimed['lease'],
                                   method=method, **values))
    return owner, raw, call


def writes(raw):
    return [command for command in raw.log if not command.endswith('?')]


def test_unbound_cld_remains_read_only_even_with_claimed_approval(tmp_path):
    owner, raw, call = owner_fixture(tmp_path, bound=False, current=True, project=False)
    try:
        with pytest.raises(BrokerError, match='read-only'):
            call('apply', values={'current_a': .051}, approved=True, system_id='laser')
        assert call('describe')['current_writes_enabled'] is False
        assert not owner.status()['writes_enabled'] and not writes(raw)
        assert owner.status()['projected'] is False
    finally:
        owner.close()


def test_cld_physical_owner_has_no_laser_policy_and_raw_writer_is_guarded(tmp_path):
    owner, raw, call = owner_fixture(tmp_path, current=True)
    try:
        assert 'controller' not in owner.config
        assert owner.instrument.io.config['current_conditions'] is None
        assert owner.instrument.io.config['current_limits_a'] is None
        with owner._operation(), pytest.raises(AuthorizationRequired):
            owner.instrument.io.io.write('SOUR1:CURR 0.051')
        assert not writes(raw)
        result = call('apply', values={'current_a': .051})
        assert result['current_a'] == .051 and writes(raw) == ['SOUR1:CURR 0.051']
        projection = owner.physical_writer.guard.inspect()['projection']
        assert projection['system_id'] == 'laser' and projection['constraints']['current_writes_enabled'] is True
        described = call('describe')
        assert described['projected'] and described['source_revision'] == owner._test_system.authority.revision
        assert described['current_limits_a'] == [.045, .055] and described['temperature_limits_c'] == [20., 30.]
    finally:
        owner.close()


def test_writes_need_a_projection_and_the_bias_claim_needs_projected_bounds(tmp_path):
    """Rule I: no range in the projection, no write and no bias lease; the projection install changes that live."""
    owner, raw, call = owner_fixture(tmp_path, current=True, project=False)
    try:
        assert owner.status()['projected'] is False and owner.status()['policy_revision'] is None
        with pytest.raises(BrokerError, match='no installed system projection'):
            call('apply', values={'current_a': .051})
        assert call('describe')['temperature_limits_c'] is None and call('describe')['current_writes_enabled'] is False
        owner._test_system = system(tmp_path, owner, cld_document(current=True))
        owner._test_system.client.install()
        assert owner.status()['projected'] and owner.status()['policy_revision'] == owner._test_system.authority.revision
        assert call('apply', values={'current_a': .051})['current_a'] == .051
        assert writes(raw) == ['SOUR1:CURR 0.051']
        assert any(json.loads(line)['kind'] == 'projection_installed' for line in owner.audit_file.read_text().splitlines())
    finally:
        owner.close()


def test_current_flag_cannot_be_bypassed_when_conditions_are_absent(tmp_path):
    owner, raw, call = owner_fixture(tmp_path, current=False)
    try:
        with pytest.raises(BrokerError, match='current writes are not enabled'):
            call('apply', values={'current_a': .051})
        assert not owner.instrument.fault and owner.pending is None and not writes(raw)
    finally:
        owner.close()


def test_cld_physical_checks_remain_independent_of_system_planner(tmp_path):
    owner, raw, call = owner_fixture(tmp_path, current=True)
    try:
        with owner._operation(), pytest.raises(ValueError, match='physical or projected'):
            owner.system_owner.controller.command('SOUR1:CURR 0.06')
        original = raw.query
        raw.query = lambda command: '.049' if command == 'SOUR1:CURR:LIM?' else original(command)
        with owner._operation(), pytest.raises(ValueError, match='physical or projected'):
            owner.system_owner.controller.command('SOUR1:CURR 0.05')
        assert not writes(raw) and owner.physical_writer.guard.inspect()['fault'] is None
    finally:
        owner.close()


def test_device_rating_narrows_the_projected_bounds(tmp_path):
    """Layer 1 (current_limits_a, the diode rating) always intersects the projected operating bounds."""
    owner, raw, call = owner_fixture(tmp_path, current=True, project=False)
    try:
        document = cld_document(current=True, conditions=True)
        document['absolute_conditions']['current_a']['bounds_a'] = [.040, .060]
        owner._test_system = system(tmp_path, owner, document)
        owner._test_system.client.install()
        described = call('describe')
        assert described['current_limits_a'] == [.045, .055] and described['current_conditions']['bounds_a'] == [.040, .060]
        with owner._operation(), pytest.raises(ValueError, match='physical or projected'):
            owner.system_owner.controller.command('SOUR1:CURR 0.058')
        assert not writes(raw)
    finally:
        owner.close()


def test_generic_hub_lease_alone_never_authorizes_cld_mutation():
    hub = Hub({'cld': {'profile': 'cld1015', 'channels': [0], 'resource': 'USB::fixture',
                       'temperature_writes_enabled': True, 'current_writes_enabled': True}})
    lease = hub.dispatch({'action': 'claim', 'instrument': 'cld', 'channel': 0, 'profile': 'cld1015',
                          'resource': 'USB::fixture'})['lease']
    for method in ('apply', 'enable', 'activate_current', 'save_current_tuning'):
        with pytest.raises(BrokerError, match='configured laser system owner'):
            hub.dispatch({'action': 'controller', 'instrument': 'cld', 'lease': lease, 'method': method})
    assert hub.instruments['cld'].io is None


def test_hub_ignores_the_retired_temperature_conditions_file_key(tmp_path):
    """Rule M step 3: the Hub never reads a laser yaml for CLD conditions any more (the sr422 instruments entry)."""
    missing = tmp_path / 'does-not-exist.yaml'
    hub = Hub({'cld': {'profile': 'cld1015', 'channels': [0], 'resource': 'USB::fixture',
                       'temperature_conditions_file': str(missing), 'policy_device': 'ldc'}})
    assert hub.instruments['cld'].config.get('temperature_conditions') is None
    lease = hub.dispatch({'action': 'claim', 'instrument': 'cld', 'channel': 0, 'profile': 'cld1015', 'resource': 'USB::fixture'})['lease']
    described = hub.dispatch({'action': 'controller', 'instrument': 'cld', 'lease': lease, 'method': 'describe'})
    assert described['temperature_conditions'] is None and described['current_writes_enabled'] is False


def test_cld_ambiguous_terminal_write_retains_lease_and_never_replays(tmp_path):
    owner, raw, call = owner_fixture(tmp_path, current=True)
    original = raw.write
    def lost(command):
        original(command)
        raise OSError('lost acknowledgement')
    raw.write = lost
    try:
        with pytest.raises(BrokerError):
            call('apply', values={'current_a': .051})
        assert owner.pending and owner.instrument.leases and owner.instrument.fault
        assert owner.physical_writer.guard.inspect()['fault']
        with pytest.raises(BrokerError):
            call('apply', values={'current_a': .051})
        assert writes(raw) == ['SOUR1:CURR 0.051']
    finally:
        owner.close()


def test_native_bias_interlock_needs_projected_bounds_and_blocks_other_lease(tmp_path):
    raw = MockIO()
    owner = CLDOwner(config(tmp_path), io_factory=lambda: raw,
                     lock_factory=lambda resource: SimpleNamespace(close=lambda: None))
    try:
        owner.refresh()
        with pytest.raises(BrokerError, match='explicit, satisfied current/temperature bounds'):
            owner.dispatch(dict(action='bias_claim', instrument='controller', resource=owner.config['resource']))
        owner._test_system = system(tmp_path, owner, cld_document())
        owner._test_system.client.install()
        result = owner.dispatch(dict(action='bias_claim', instrument='controller', resource=owner.config['resource']))
        assert result['bias_held'] and result['fresh']
        with pytest.raises(BrokerError):
            owner.dispatch(dict(action='claim', instrument='controller', channel=0, profile='cld1015',
                                resource=owner.config['resource']))
        assert owner.dispatch(dict(action='bias_status', instrument='controller', lease=result['lease']))['bias_held']
        owner.dispatch(dict(action='bias_release', instrument='controller', lease=result['lease'], native_stopped=True))
        assert not writes(raw)
    finally:
        owner.close()


def test_cld_tuning_is_a_laser_setting_and_reprojection_changes_it_without_restart(tmp_path):
    from ldcon.core.device_conditions import policy_digest
    owner, raw, call = owner_fixture(tmp_path, current=True, conditions=True)
    try:
        policy = owner.system_owner.authority.snapshot()
        expected = policy_digest(policy['control_settings'])
        activated = call('activate_current', policy_digest=policy_digest(policy['current_conditions']), settings_digest=expected)
        assert activated['source_revision'] == owner._test_system.authority.revision
        proposal = {'pid': deepcopy(policy['control_settings']['current']['pid']), 'output_update_interval_s': .2}
        with pytest.raises(BrokerError, match='laser setting'):
            call('save_current_tuning', expected_digest=expected, proposal=proposal, approved=True)
        assert not owner.instrument.fault and not writes(raw)
        # the laser system owner adopts the tuning in its document and re-projects: no CLD restart, no write
        s = owner._test_system
        candidate = s.authority.snapshot()
        candidate['control_settings']['current']['output_update_interval_s'] = .2
        old = s.authority.revision
        token = s.authority.approve(candidate, expected_revision=old,
                                    authorize_human=lambda p: {'human_identity': 'fixture human', 'receipt_id': 'exact'})
        s.authority.adopt(candidate, expected_revision=old, approval=token, prepare=lambda p: {},
                          apply=lambda p, proof: s.client.install(p),
                          verify=lambda p: dict(s.client.verify(p), settled=True))
        described = call('describe')
        assert described['control_settings']['current']['output_update_interval_s'] == .2
        assert described['source_revision'] == s.authority.revision != old
        assert owner.physical_writer.guard.inspect()['projection']['policy_revision'] == s.authority.revision
        assert not writes(raw)
    finally:
        owner.close()


def test_current_client_transmits_opaque_approval_without_claimed_boolean():
    from ldcon.jobs.current_devices import BrokerCurrentPort
    from ldcon.core.instrument_client import RemoteCLD1015
    port = object.__new__(BrokerCurrentPort)
    port.settings = {'current': {'pid': {'kp': 1.}}}
    remote = RemoteCLD1015({})
    received = []
    remote.transport = SimpleNamespace(instrument='cld', lease='lease',
        _rpc=lambda action, **request: received.append(dict(action=action, **request)) or {'saved': True})
    port.controller = remote
    assert port.save_tuning({'pid': {}}, approval_token='opaque-exact-grant')['saved']
    assert received[-1]['approval_token'] == 'opaque-exact-grant'
    assert 'approved' not in received[-1]
    port.save_tuning({'pid': {}})
    assert 'approval_token' not in received[-1]
