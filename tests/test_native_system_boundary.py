"""Offline native physical authority and owner-hosted assembly contracts."""
from copy import deepcopy
from threading import Event, RLock
from types import SimpleNamespace
import time

import pytest

from experiment_core.errors import AuthorizationRequired, StaleContext
from experiment_core.records import Journal
from ldcon.core.instrument_client import BrokerError
from ldcon.owners.native_assembly_host import LocalNativePort, NativeAssemblyHost
from ldcon.owners.native_instrument_owner import NativeInstrumentOwner, NativePrewriteRefusal
from ldcon.owners.owner_projection import SystemPolicyAuthority
from ldcon.owners.esp32_native_owner import NativeCurrentOwner


@pytest.fixture
def physical(tmp_path):
    journal = Journal(tmp_path / 'physical.sqlite')
    physical = {'firmware_version': 'fixture', 'adc': {'bits': 16, 'min_voltage': 0., 'max_voltage': 3.3},
                'dac': {'bits': 16, 'min_voltage': -.25, 'max_voltage': .25}}
    sample = dict(firmware_version='fixture', metadata=dict(mode='normal', voltage_conversion_valid=True,
        adc=deepcopy(physical['adc']), dac=deepcopy(physical['dac'])),
        servo_config=dict(control_offset=150, mod_valid_min=0, mod_valid_max=65535,
                          multiplier_control=0, baseline_control=0, pid_multiplier=0))
    writes = []
    def write(values):
        writes.append(deepcopy(values)); sample['servo_config'].update(values)
    driver = SimpleNamespace(device_id='esp', read=lambda: deepcopy(sample), set_control=write,
                             capabilities=[], close=lambda: None)
    authority = SystemPolicyAuthority('native', {'limit': 200}, dependencies={}, journal=journal)
    owner = NativeInstrumentOwner(driver, authority, physical, journal=journal)
    owner.install({'code_bounds': [100, 200], 'allowed_settings': ['control_offset', 'pid_multiplier', 'log_adc_change_threshold'],
                   'cleanup_operations': [{'kind': 'native_control', 'values': {'pid_multiplier': 0}}]})
    yield authority, owner, sample, writes
    journal.close()


def test_native_terminal_writer_requires_binding_and_intrinsic_proof(physical):
    authority, owner, sample, writes = physical
    with pytest.raises(AuthorizationRequired):
        owner.set_control({'control_offset': 155})
    with owner.authorized(authority):
        with pytest.raises(NativePrewriteRefusal):
            owner.set_control({'control_offset': 201})
        assert owner.guard.inspect(owner.binding)['fault'] is None
        owner.set_control({'control_offset': 155})
        sample['metadata']['dac']['max_voltage'] = .5
        with pytest.raises(NativePrewriteRefusal):
            owner.set_control({'log_adc_change_threshold': 4})
    assert writes == [{'control_offset': 155}]


@pytest.fixture
def assembly(tmp_path, monkeypatch):
    journal = Journal(tmp_path / 'assembly.sqlite')
    policy = {'max_sample_age_s': 1.}
    authority = SystemPolicyAuthority('native', policy, dependencies={}, journal=journal)
    calls, made = [], []
    entered, release = Event(), Event()
    runtime = SimpleNamespace(lease='parent-lease', active=True, fault=None, ff_definition='profile',
                              io_lock=RLock(), stop=Event(), _report_event=lambda *a, **k: None)
    runtime.input_events = []
    runtime.clock = lambda: 123.
    runtime._report_event = lambda kind, **v: NativeCurrentOwner._report_event(runtime, kind, **v)
    runtime._report_assembly_event = lambda kind, v: NativeCurrentOwner._report_assembly_event(runtime, kind, v)
    profile = {'unwind_steps': 2, 'unwind_interval_s': .01, 'missing_timeout_s': .1}
    system = SimpleNamespace(journal=journal, authority=authority, runtime=runtime, policy=policy,
                             ff_profile=profile, instrument=SimpleNamespace(device_id='esp'),
                             dispatch=lambda request: calls.append(request) or {})
    def factory(system_owner, request):
        made.append(request)
        return object()
    class Session:
        def __init__(self, port, awg, request, queue, **kwargs):
            self.port, self.awg, self.request, self.queue = port, awg, request, queue
        def run(self, target):
            self.queue.write_status(self.request['request_id'], {'state': 'ready', 'armed': True, 'ready': True})
            entered.set()
            assert release.wait(5)
            cancelled = self.queue.cancelled(None)
            self.port.close()
            return {'status': 'cancelled' if cancelled else 'succeeded', 'restored': True}
    monkeypatch.setattr('ldcon.jobs.native_ff_proxy.ProxiedFFSession', Session)
    host = NativeAssemblyHost(system, factory)
    def call(action, **values):
        return host.dispatch(dict(action=action, device_id='esp', lease='parent-lease', **values))
    request = {'request_id': 'one', 'plan': {}, 'trigger': 'software', 'plan_sha256': 'exact'}
    yield host, system, request, call, made, calls, entered, release
    release.set()
    for thread in host._threads.values():
        thread.join(5)
    journal.close()


def test_assembly_requires_host_binding_and_exact_parent_lease(assembly):
    host, system, request, call, made, calls, entered, release = assembly
    host.factory = None
    with pytest.raises(BrokerError, match='startup binding'):
        call('assembly_start', session_request=request, target_hz=1e14)
    host.factory = lambda *a: None
    with pytest.raises(BrokerError, match='lease'):
        host.dispatch(dict(action='assembly_start', device_id='esp', lease='claimed-system',
                           session_request=request, target_hz=1e14))
    assert not made and not calls


def test_assembly_start_deduplicates_and_cancel_never_stops_parent_pi(assembly):
    host, system, request, call, made, calls, entered, release = assembly
    first = call('assembly_start', session_request=request, target_hz=1e14)
    assert entered.wait(5)
    repeated = call('assembly_start', session_request=request, target_hz=1e14)
    assert repeated['session_id'] == first['session_id']
    with pytest.raises(BrokerError, match='different content'):
        call('assembly_start', session_request=request, target_hz=1e14 + 1.)
    call('assembly_cancel', session_id=first['session_id'])
    call('assembly_cancel', session_id=first['session_id'])
    release.set(); host._threads[first['session_id']].join(5)
    result = call('assembly_status', session_id=first['session_id'])
    assert result['result'] == {'status': 'cancelled', 'restored': True}
    assert len(made) == 1 and not calls
    assert system.runtime.active and system.runtime.lease == 'parent-lease'


def test_assembly_trigger_binds_exact_ready_plan_once(assembly):
    host, system, request, call, made, calls, entered, release = assembly
    first = call('assembly_start', session_request=request, target_hz=1e14)
    assert entered.wait(5)
    sid = first['session_id']
    with pytest.raises(BrokerError, match='exact armed ready plan'):
        call('assembly_trigger', session_id=sid, record={'plan_sha256': 'changed'})
    record = {'plan_sha256': 'exact', 'requested_unix_s': 1.}
    call('assembly_trigger', session_id=sid, record=record)
    call('assembly_trigger', session_id=sid, record=record)
    with pytest.raises(BrokerError, match='different content'):
        call('assembly_trigger', session_id=sid, record=dict(record, requested_unix_s=2.))
    assert host._queue(sid).trigger_record(None) == record


def test_replacement_owner_never_relaunches_uncertain_assembly(assembly):
    host, system, request, call, made, calls, entered, release = assembly
    first = call('assembly_start', session_request=request, target_hz=1e14)
    assert entered.wait(5)
    replacement = SimpleNamespace(**vars(system))
    replacement.authority = SystemPolicyAuthority('native', system.policy, dependencies={}, journal=system.journal)
    new = NativeAssemblyHost(replacement, lambda *a: pytest.fail('replayed'))
    result = new.dispatch(dict(action='assembly_start', device_id='esp', lease='parent-lease',
                               session_request=request, target_hz=1e14))
    assert result['session_id'] == first['session_id'] and result['state'] == 'uncertain'
    release.set(); host._threads[first['session_id']].join(5)
    assert new._row(first['session_id'])['state'] == 'uncertain'
    assert len(made) == 1


def test_local_assembly_port_cannot_release_parent_lease(assembly):
    _, system, _, _, _, calls, _, _ = assembly
    port = LocalNativePort(system, 'parent-lease')
    with pytest.raises(BrokerError, match='parent PI lease'):
        port._call('release')
    port.close()
    assert not calls and system.runtime.lease == 'parent-lease'


def test_owner_hosted_recentre_can_emit_step_metadata_and_reach_ready(assembly, monkeypatch):
    host, system, request, call, made, calls, entered, release = assembly
    host.factory.laser_snapshot = {'laser': 'fixture', 'target': {'frequency_hz': 1e14-20e6}}
    def recentre(port, laser, target, *, emit, **kwargs):
        assert target == 1e14  # Caller target, never the frozen startup document's old target.
        assert laser['target']['frequency_hz'] != target
        emit('operating_point_step', dict(kind='native_current_centre', time=12., value=32829))
        return dict(status='succeeded')
    monkeypatch.setattr('ldcon.jobs.native_ff_proxy.recentre_before_ff', recentre)
    request['recentre'] = True
    first = call('assembly_start', session_request=request, target_hz=1e14)
    assert entered.wait(5), 'Logging a completed re-centre step must not abort admission'
    assert system.runtime.input_events == [dict(kind='operating_point_step', time=123.,
        details=dict(kind='native_current_centre', time=12., value=32829))]
    release.set()
    host._threads[first['session_id']].join(5)
    assert call('assembly_status', session_id=first['session_id'])['result']['restored']
    assert system.runtime.active and system.runtime.lease == 'parent-lease'
