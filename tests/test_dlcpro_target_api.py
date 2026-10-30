"""Offline target HTTP -> attached owner -> synthetic chassis regression tests."""
import asyncio
from types import SimpleNamespace

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
import yaml

from ldcon.jobs.dlcpro_cards import Protocol
from ldcon.owners.dlcpro_target_changes import TargetChanges, TargetError, dispatch
from ldcon.web.dlcpro_target_api import DLCProTargetApi, TargetApi
from ldcon.web.lock_target_api import ApiError, install_routes
from .test_dlcpro_offline import rig, no_network


@pytest.fixture
def target_rig(rig, tmp_path):
    laser = rig['laser']
    laser['allow_output'] = True
    rig['device']['control_settings']['commissioned'] = True
    path = tmp_path / 'laser.yaml'
    path.write_text(yaml.safe_dump(laser, sort_keys=False))
    owner = rig['owner']
    owner.start_lock(3e14, rig['control'])
    protocol = Protocol(owner, rig['device'])
    protocol.client = 'existing-controller-lease'
    manager = TargetChanges(protocol, path, tmp_path / 'requests', wall_clock=lambda: 1700000000 + rig['clock']())
    owner.target_changes = manager
    calls = []

    class FakeSession:
        def __init__(self, device):
            assert device == rig['device']

        def rpc(self, action, **values):
            calls.append((action, values))
            return protocol.dispatch(dict(action=action, laser_id=owner.laser_id, **values))

    repository = SimpleNamespace(read=lambda kind, name: dict(document=yaml.safe_load(path.read_text())))
    runtime = SimpleNamespace(root=tmp_path, interface_repository=repository,
                              assert_admission_open=lambda: None)
    api = DLCProTargetApi(runtime, session_factory=FakeSession)
    rig.update(manager=manager, protocol=protocol, laser_file=path, api=api, calls=calls,
               runtime=runtime, session_factory=FakeSession)
    return rig


def begin(rig, delta=20e6, identity='shot-001', timeout=90):
    return rig['api'].start_change(rig['laser']['id'], dict(
        target_frequency_hz=3e14 + delta, request_id=identity, timeout_s=timeout))[1]


def advance(rig, seconds=1):
    rig['clock'].sleep(seconds)
    rig['owner'].heartbeat()
    with rig['owner'].mutex:
        rig['manager'].tick()


def result(rig, request):
    return rig['api'].change_state(rig['laser']['id'], request['change_id'])


def saved(rig):
    return yaml.safe_load(rig['laser_file'].read_text())['target']['frequency_hz']


def test_multi_step_keeps_controller_lease_and_saves_only_confirmed_targets(target_rig):
    r = target_rig
    request = begin(r, 250e6)
    assert request['state'] == 'accepted' and not request['applied_to_lock']
    assert saved(r) == 3e14
    for _ in range(30):
        advance(r)
        if result(r, request)['final']:
            break
    done = result(r, request)
    assert done['state'] == 'settled' and done['applied_to_lock']
    assert done['applied_steps'] == 3 and len(done['verify']['samples']) == 5
    steps = [e for e in r['events'] if e['event'] == 'target_step']
    assert [e['delta_frequency_hz'] for e in steps] == [100e6, 100e6, 50e6]
    assert saved(r) == r['owner'].target == 3e14 + 250e6
    assert r['owner'].mode == 'lock' and not r['owner'].fault
    assert r['protocol'].client == 'existing-controller-lease'
    assert all(action.startswith('target_change_') for action, _ in r['calls'])


def test_target_progress_and_ordinary_pi_ticks_share_the_existing_owner(target_rig):
    r = target_rig
    request = begin(r, 150e6)
    for _ in range(800):
        advance(r, .05)
        r['owner'].tick()
        if result(r, request)['final']:
            break
    assert result(r, request)['state'] == 'settled'
    assert r['owner'].mode == 'lock' and not r['owner'].fault
    assert r['protocol'].client == 'existing-controller-lease'


def test_waiting_owner_ticks_do_no_file_io_before_a_new_verification_interval(target_rig, monkeypatch):
    from ldcon.owners import dlcpro_target_changes as changes
    r = target_rig
    request = begin(r)
    counts = dict(read=0, guard=0, receipt=0, verify=0)
    originals = (changes.read_laser, changes.target_file_guard, changes.write_record, r['manager']._verify)

    def read(*args, **kwargs):
        counts['read'] += 1
        return originals[0](*args, **kwargs)

    def guard(*args, **kwargs):
        counts['guard'] += 1
        return originals[1](*args, **kwargs)

    def receipt(*args, **kwargs):
        counts['receipt'] += 1
        return originals[2](*args, **kwargs)

    def verify(*args, **kwargs):
        counts['verify'] += 1
        return originals[3](*args, **kwargs)

    monkeypatch.setattr(changes, 'read_laser', read)
    monkeypatch.setattr(changes, 'target_file_guard', guard)
    monkeypatch.setattr(changes, 'write_record', receipt)
    monkeypatch.setattr(r['manager'], '_verify', verify)
    for _ in range(1000):
        r['manager'].tick()  # The acceptance acquisition is still in the cache.
    assert counts == dict(read=0, guard=0, receipt=0, verify=0)
    advance(r)
    assert result(r, request)['state'] == 'verifying'
    for key in counts:
        counts[key] = 0
    for _ in range(1000):
        r['manager'].tick()  # The last physical-step acquisition fence is unchanged.
    assert counts == dict(read=0, guard=0, receipt=0, verify=0)
    for _ in range(1000):
        advance(r, .0001)  # New cached frames, but less than one verification interval.
    assert counts == dict(read=0, guard=0, receipt=0, verify=1)
    for _ in range(3):
        advance(r)
    assert counts == dict(read=0, guard=0, receipt=0, verify=4)
    advance(r)
    assert result(r, request)['state'] == 'settled'
    assert counts == dict(read=1, guard=1, receipt=1, verify=5)


def test_configuration_superseded_during_verification_is_checked_before_next_step(target_rig):
    r = target_rig
    request = begin(r, 150e6)
    advance(r)
    writes = len(r['driver'].writes)
    newer = r['laser_file'].read_text() + '\n# Newer maintainer definition\n'
    r['laser_file'].write_text(newer)
    for _ in range(5):
        advance(r)
    assert result(r, request)['state'] == 'ramping'
    assert len(r['driver'].writes) == writes
    advance(r)
    assert result(r, request)['state'] == 'failed'
    assert len(r['driver'].writes) == writes and r['laser_file'].read_text() == newer


def test_configuration_superseded_during_final_verification_cannot_report_settled(target_rig):
    r = target_rig
    request = begin(r)
    advance(r)
    newer = r['laser_file'].read_text() + '\n# Newer maintainer definition\n'
    r['laser_file'].write_text(newer)
    for _ in range(4):
        advance(r)
    assert result(r, request)['state'] == 'verifying'
    advance(r)
    done = result(r, request)
    assert done['state'] == 'failed' and not done['applied_to_lock']
    assert r['laser_file'].read_text() == newer


def test_same_target_waits_for_new_acquisitions_and_never_writes_output(target_rig):
    r = target_rig
    before = len(r['driver'].writes)
    request = begin(r, 0)
    r['manager'].tick()
    assert result(r, request)['state'] == 'accepted'
    advance(r, .1)
    r['source'].fixed = r['clock']()
    for _ in range(5):
        advance(r, .01)
    assert result(r, request)['state'] == 'verifying'
    assert not result(r, request)['applied_to_lock']
    r['source'].fixed = None
    for _ in range(5):
        advance(r)
    assert result(r, request)['state'] == 'settled'
    assert len(r['driver'].writes) == before


def test_request_identity_is_immutable_before_and_after_configuration_changes(target_rig):
    r = target_rig
    request = begin(r)
    assert begin(r) == request
    with pytest.raises(ApiError) as error:
        begin(r, timeout=91)
    assert error.value.code == 'request_conflict'
    with pytest.raises(ApiError) as error:
        begin(r, delta=21e6)
    assert error.value.code == 'request_conflict'
    advance(r)
    same = begin(r)
    assert same['change_id'] == request['change_id'] and same['state'] == 'verifying'
    assert r['api'].change_by_request(r['laser']['id'], 'shot-001') == same
    assert len([e for e in r['events'] if e['event'] == 'target_step']) == 1


def test_cancellation_retains_partial_target_and_cannot_cancel_new_request(target_rig):
    r = target_rig
    first = begin(r, 250e6)
    advance(r)
    _, cancelled = r['api'].cancel(r['laser']['id'], first['change_id'])
    assert cancelled['state'] == 'cancelled' and not cancelled['applied_to_lock']
    assert saved(r) == r['owner'].target == 3e14 + 100e6
    second = begin(r, 200e6, 'shot-002')
    assert r['api'].cancel(r['laser']['id'], first['change_id'])[1] == cancelled
    assert result(r, second)['state'] == 'accepted'
    advance(r)
    assert r['owner'].target == 3e14 + 200e6


def test_newer_owner_target_and_newer_configuration_are_never_overwritten(target_rig):
    r = target_rig
    request = begin(r)
    r['clock'].sleep(1)
    r['owner'].set_target(3e14 - 10e6, r['owner'].target_revision)
    writes = len(r['driver'].writes)
    advance(r)
    assert result(r, request)['state'] == 'failed'
    assert r['owner'].target == 3e14 - 10e6 and len(r['driver'].writes) == writes
    next_request = begin(r, 15e6, 'newer-config')
    laser = yaml.safe_load(r['laser_file'].read_text())
    laser['target']['frequency_hz'] = 3e14 + 50e6
    r['laser_file'].write_text(yaml.safe_dump(laser))
    advance(r)
    assert result(r, next_request)['state'] == 'failed'
    assert saved(r) == 3e14 + 50e6 and len(r['driver'].writes) == writes


def test_lost_transport_response_recovered_by_lookup_without_resubmission(target_rig):
    r = target_rig
    factory = r['session_factory']

    class LostReply(factory):
        def rpc(self, action, **values):
            reply = super().rpc(action, **values)
            if action == 'target_change_start':
                raise TimeoutError('reply lost after acceptance')
            return reply

    r['api'].session_factory = LostReply
    with pytest.raises(ApiError) as error:
        begin(r)
    assert error.value.status == 503
    assert len(r['manager'].records) == 1
    found = r['api'].change_by_request(r['laser']['id'], 'shot-001')
    advance(r)
    assert result(r, found)['state'] == 'verifying'
    assert len([action for action, _ in r['calls'] if action == 'target_change_start']) == 1


def test_unknown_write_fault_never_replays_and_keeps_saved_target(target_rig):
    r = target_rig
    request = begin(r)
    r['driver'].fail = True
    advance(r)
    failed = result(r, request)
    assert failed['state'] == 'failed' and failed['review_required']
    assert saved(r) == 3e14 and r['owner'].fault
    writes = len(r['driver'].writes)
    r['driver'].fail = False
    assert begin(r)['change_id'] == request['change_id']
    for _ in range(3):
        advance(r)
    assert len(r['driver'].writes) == writes
    assert r['protocol'].client == 'existing-controller-lease'


def test_configuration_failure_after_step_retains_newer_file_and_stops(target_rig, monkeypatch):
    r = target_rig
    request = begin(r, 250e6)
    original = r['owner'].set_target

    def concurrent_editor(*args):
        receipt = original(*args)
        text = r['laser_file'].read_text()
        r['laser_file'].write_text(text + '\n# Newer concurrent maintainer edit\n')
        return receipt

    monkeypatch.setattr(r['owner'], 'set_target', concurrent_editor)
    advance(r)
    failed = result(r, request)
    assert failed['state'] == 'failed' and failed['review_required']
    assert failed['ramped_target_hz'] == r['owner'].target == 3e14 + 100e6
    assert saved(r) == 3e14 and 'Newer concurrent' in r['laser_file'].read_text()
    writes = len(r['driver'].writes)
    advance(r)
    assert len(r['driver'].writes) == writes and r['owner'].mode == 'lock'


def test_prewrite_measurement_rejection_waits_but_timeout_is_terminal(target_rig, monkeypatch):
    from ldcon.core.contracts import MeasurementUnavailable
    r = target_rig
    request = begin(r, timeout=3)
    original = r['owner'].set_target
    count = [0]

    def prewrite(*args):
        count[0] += 1
        if count[0] == 1:
            raise MeasurementUnavailable('synthetic prewrite rejection')
        return original(*args)

    monkeypatch.setattr(r['owner'], 'set_target', prewrite)
    writes = len(r['driver'].writes)
    advance(r)
    assert result(r, request)['state'] == 'ramping' and len(r['driver'].writes) == writes
    advance(r)
    assert result(r, request)['state'] == 'verifying'
    advance(r)
    assert result(r, request)['state'] == 'failed' and saved(r) == r['owner'].target


@pytest.mark.parametrize('state', ['accepted', 'applying', 'verifying'])
def test_owner_restart_interrupts_without_resuming(state, target_rig):
    r = target_rig
    request = begin(r)
    record = r['manager'].find(change_id=request['change_id'])
    record['state'] = state
    r['manager']._save(record)
    writes = len(r['driver'].writes)
    restarted = TargetChanges(r['protocol'], r['laser_file'], r['manager'].root)
    restored = restarted.find(change_id=request['change_id'])
    assert restored['state'] == 'failed' and restored['review_required'] == (state == 'applying')
    restarted.tick()
    assert len(r['driver'].writes) == writes


def test_optical_gaps_and_out_of_tolerance_reset_settlement_without_relaxing(target_rig):
    r = target_rig
    request = begin(r)
    advance(r)
    advance(r)
    advance(r)
    assert len(r['manager'].find(change_id=request['change_id'])['samples']) == 2
    r['source'].missing = True
    advance(r)
    assert not r['manager'].find(change_id=request['change_id'])['samples']
    r['source'].missing = False
    r['source'].bias = 2 * r['owner'].control['tolerance_hz']
    for _ in range(6):
        advance(r)
    assert not result(r, request)['final']
    r['source'].bias = 0
    r['source'].degraded = True  # Quality warning is independent of frequency arrival.
    for _ in range(5):
        advance(r)
    assert result(r, request)['state'] == 'settled'


def test_armed_sweep_and_missing_lease_reject_without_claiming(target_rig):
    r = target_rig
    r['owner'].awg_sweep = SimpleNamespace(active=lambda: {'state': 'armed'})
    with pytest.raises(ApiError) as error:
        begin(r)
    assert error.value.code == 'busy'
    r['owner'].awg_sweep = SimpleNamespace(active=lambda: None)
    r['protocol'].client = None
    with pytest.raises(ApiError) as error:
        begin(r)
    assert error.value.code == 'busy' and not r['manager'].records


def test_attached_steps_do_not_renew_the_controller_heartbeat(target_rig):
    r = target_rig
    before = r['owner'].last_client
    request = begin(r)
    r['clock'].sleep(1)
    r['manager'].tick()
    assert r['owner'].last_client == before
    r['clock'].sleep(2)
    r['manager'].tick()
    assert result(r, request)['state'] == 'failed'


def test_file_editor_lock_defers_without_blocking_the_owner(target_rig):
    from ldcon.owners.dlcpro_target_store import target_file_guard
    r = target_rig
    request = begin(r)
    writes = len(r['driver'].writes)
    with target_file_guard(r['laser_file']):
        advance(r)
        assert result(r, request)['state'] == 'accepted'
        assert len(r['driver'].writes) == writes
    advance(r)
    assert result(r, request)['state'] == 'verifying'


def test_receipt_save_failure_stops_instead_of_becoming_an_optical_retry(target_rig, monkeypatch):
    from ldcon.owners import dlcpro_target_changes
    r = target_rig
    request = begin(r)
    original = dlcpro_target_changes.write_record
    writes = len(r['driver'].writes)
    count = [0]

    def failed_once(*args):
        count[0] += 1
        if count[0] == 1:
            raise OSError('synthetic fsync failure')
        return original(*args)

    monkeypatch.setattr(dlcpro_target_changes, 'write_record', failed_once)
    advance(r)
    state = result(r, request)
    assert state['state'] == 'failed' and state['review_required']
    advance(r)
    assert len(r['driver'].writes) == writes


@pytest.mark.parametrize('body', [None, {}, {'target_frequency_hz': True, 'request_id': 'x'},
                                {'target_frequency_hz': 3e14, 'request_id': '../escape'},
                                {'target_frequency_hz': 3e14, 'request_id': 'x', 'timeout_s': float('inf')},
                                {'target_frequency_hz': 3e14, 'request_id': 'x', 'force': True}])
def test_malformed_requests_are_rejected_before_owner_io(body, target_rig):
    r = target_rig
    with pytest.raises(ApiError) as error:
        r['api'].start_change(r['laser']['id'], body)
    assert error.value.status == 400 and not r['calls']


def test_outside_admitted_range_never_steps(target_rig):
    r = target_rig
    with pytest.raises(ApiError) as error:
        begin(r, 1.01e9)
    assert error.value.code == 'out_of_bounds' and not r['manager'].records


def test_http_contract_and_routing_leave_sr_facade_unchanged(target_rig, monkeypatch):
    r = target_rig
    app = FastAPI()
    app.state.runtime = r['runtime']
    wrapper = TargetApi(r['runtime'])
    wrapper.dlc = r['api']
    app.state.lock_target_api = wrapper

    async def body(request):
        return await request.json()

    # Keep the HTTP contract fixture single-threaded and independent of the
    # runner's event-loop wakeups; production retains its normal thread offload.
    async def inline(function, *args):
        return function(*args)

    monkeypatch.setattr(asyncio, 'to_thread', inline)
    install_routes(app, '', body)
    async def exercise():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://offline") as client:
            base = '/api/lasers/' + r['laser']['id'] + '/target'
            initial = (await client.get(base)).json()
            assert initial['locked'] and initial['max_change_hz'] == 100e6
            response = await client.post(base + '/changes', json=dict(target_frequency_hz=3e14, request_id='blacs-001'))
            assert response.status_code == 202
            change = response.json()
            assert change['state'] == 'accepted' and change['laser'] == r['laser']['id']
            by_id = await client.get(base + '/changes/' + change['change_id'])
            by_request = await client.get(base + '/changes?request_id=blacs-001')
            assert by_id.json() == by_request.json() == change
            for _ in range(6):
                advance(r)
            done = (await client.get(base + '/changes/' + change['change_id'])).json()
            assert done['state'] == 'settled' and done['applied_to_lock']
            assert (await client.post(base + '/changes/' + change['change_id'] + '/cancel')).json() == done
            assert (await client.get(base + '/changes?request_id=missing')).status_code == 404
    asyncio.run(exercise())
    original = r['runtime'].interface_repository.read
    r['runtime'].interface_repository.read = lambda *_: dict(document={'interfaces': {}, 'devices': {}})
    assert wrapper._api('sr422') is wrapper.ordinary
    assert wrapper._api('sr1092') is wrapper.ordinary
    r['runtime'].interface_repository.read = original
