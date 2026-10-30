"""All caller forms share registration/admission without acquiring physical I/O."""
from pathlib import Path
import json

import pytest
import yaml

from ldcon.core.interface_schema import CARD_TYPES, parameters, readiness
from ldcon.web.laser_operations import ACTIONS, TrustedOperationContext
from ldcon.core.operation_registry import definitions, normalize_parameters, spec, validate_card
from ldcon.core.records import atomic_json
from ldcon.web.web_runtime import ConflictError, WebRuntime


def configure(root):
    laser = dict(schema_version=1, id='fixture', name='Synthetic monitor', allow_output=False,
        devices=dict(wm=dict(type='fizeau.http', connection=dict(endpoint='http://127.0.0.1:1',
            channel=1, cycle=0, trust_source_clock=True, expected_laser='fixture'), absolute_conditions={})),
        interfaces=dict(wavelength=dict(device='wm', capability='frequency_monitor')))
    card = dict(id='observe', operation='monitor.wavelength', uses=['wavelength'], parameters={})
    deck = dict(schema_version=1, id='fixture-deck', name='Fixture',
                interfaces=dict(wavelength='frequency_monitor'), cards=[card])
    assignment = dict(schema_version=1, id='fixture', laser_id='fixture',
                      decks=[dict(deck_id='fixture-deck', parameters={})])
    for directory, name, value in (('lasers', 'fixture', laser), ('interface-decks', 'fixture-deck', deck),
                                   ('assignments', 'fixture', assignment)):
        (root/directory).mkdir(parents=True, exist_ok=True)
        (root/directory/(name+'.yaml')).write_text(yaml.safe_dump(value))
    return laser, deck, card


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    configure(tmp_path)
    monkeypatch.setenv('LDCON_JOB_LAUNCHER', 'service')
    runtime = WebRuntime(tmp_path)
    yield runtime
    runtime.close()


def proposal(runtime, request_id='a'*32):
    value = runtime.get_set('fixture')
    return dict(subject_id='fixture', set_id='fixture', deck_id='fixture-deck', card_id='observe',
                parameters={}, expected_revision=value['revision'], request_id=request_id)


def test_registered_catalog_is_the_only_current_id_and_validator_source(tmp_path):
    laser, deck, card = configure(tmp_path)
    assert {row['operation_id'] for row in definitions()} == set(CARD_TYPES)
    assert normalize_parameters(card, {}) == parameters(card, {})
    assert validate_card(laser, deck, card) == readiness(laser, deck, card)
    with pytest.raises(ValueError, match='Unknown operation'):
        parameters(dict(card, operation='invented.operation'), {})
    with pytest.raises(ValueError, match='Retired card'):
        spec('tune.ecdl_initial_point')


def test_system_port_and_legacy_call_return_same_idempotent_admission(runtime):
    p = proposal(runtime)
    owner = runtime.operations('fixture')
    context = TrustedOperationContext('fixture', 'labscript')
    preflight = owner.preflight(p, context)
    assert preflight['runnable'] and preflight['effective_policy_revision'] is None
    assert preflight['physical_validation'] == 'owner_revalidation_required'
    first = owner.submit(p, context)
    again = runtime.start_interface_card(runtime.get_set('fixture'), 'fixture-deck', 'observe', {}, job_id=p['request_id'])
    assert first == again
    assert len(list(runtime.jobs.glob('*/*/job.json'))) == 1
    assert first['operation_contract']['operation_id'] == 'monitor.wavelength'
    description = owner.describe('fixture')
    assert description['owner']['status'] == 'not_connected'
    assert description['cards'][0]['operation_id'] == preflight['operation_id']


def test_reused_id_with_changed_parameters_conflicts_before_second_launch(runtime):
    value = runtime.get_set('fixture')
    runtime.start_interface_card(value, 'fixture-deck', 'observe', {}, job_id='b'*32)
    with pytest.raises(ConflictError, match='different content'):
        runtime.start_interface_card(value, 'fixture-deck', 'observe', {'duration_s': 2}, job_id='b'*32)
    assert len(list(runtime.jobs.glob('*/*/launch.request'))) == 1


def test_existing_receipt_survives_configuration_revision_change(runtime):
    owner = runtime.operations('fixture')
    p = proposal(runtime)
    first = owner.submit(p, TrustedOperationContext('fixture'))
    path = runtime.root/'lasers/fixture.yaml'
    value = yaml.safe_load(path.read_text())
    value['name'] = 'Changed configured display name'
    path.write_text(yaml.safe_dump(value))
    assert owner.submit(p, TrustedOperationContext('fixture')) == first
    assert len(list(runtime.jobs.glob('*/*/launch.request'))) == 1


def test_frontend_restart_preserves_receipts_and_does_not_cancel_worker(runtime):
    root = runtime.root
    first = runtime.operations('fixture').submit(proposal(runtime), TrustedOperationContext('fixture'))
    runtime.close()
    replacement = WebRuntime(root)
    try:
        again = replacement.operations('fixture').submit(proposal(replacement), TrustedOperationContext('fixture'))
        assert again == first
        assert not (replacement.directory(first['id'])/'cancel.request').exists()
    finally:
        replacement.close()


def test_unknown_submission_survives_restart_without_replay(runtime, monkeypatch):
    backend = runtime._start_interface_card
    calls = []
    def lose_reply(*args, **kwargs):
        calls.append(1)
        backend(*args, **kwargs)
        raise ConnectionError('lost acceptance response')
    monkeypatch.setattr(runtime, '_start_interface_card', lose_reply)
    p = proposal(runtime)
    with pytest.raises(ConnectionError):
        runtime.operations('fixture').submit(p, TrustedOperationContext('fixture'))
    root = runtime.root
    runtime.close()
    replacement = WebRuntime(root)
    try:
        with pytest.raises(ConflictError, match='no replay'):
            replacement.operations('fixture').submit(proposal(replacement), TrustedOperationContext('fixture'))
        assert calls == [1]
        assert len(list(root.glob('jobs/*/*/launch.request'))) == 1
    finally:
        replacement.close()


def test_cancel_names_subordinate_and_never_stops_parent(runtime):
    parent = runtime.operations('fixture').submit(proposal(runtime), TrustedOperationContext('fixture'))
    directory = runtime.directory(parent['id'])
    atomic_json(directory/'progress.json', dict(status='running'))
    atomic_json(directory/'online-status.json', dict(status='running', request_id='child', card_id='tune'))
    owner = runtime.operations('fixture')
    owner.cancel('fixture', parent['id'], 'child', TrustedOperationContext('fixture'))
    assert json.loads((directory/'online-cancel.json').read_text()) == {'request_id': 'child'}
    assert not (directory/'cancel.request').exists()
    with pytest.raises(FileNotFoundError):
        owner.cancel('fixture', parent['id'], 'another-child', TrustedOperationContext('fixture'))
    assert not (directory/'cancel.request').exists()


def test_context_cannot_escalate_to_another_laser(runtime):
    owner = runtime.operations('fixture')
    with pytest.raises(ValueError, match='another laser'):
        owner.submit(proposal(runtime), TrustedOperationContext('other'))
    with pytest.raises(ValueError, match='another laser'):
        owner.submit(proposal(runtime), {'laser_id': 'fixture'})
    assert not list(runtime.jobs.glob('*/*/job.json'))


def test_every_registered_action_requires_a_known_backend(runtime):
    assert all(callable(getattr(runtime, backend)) for backend, _ in ACTIONS.values())
    with pytest.raises(ValueError, match='Unknown operation action'):
        runtime.operation_gateway().call('instrument.raw_write', 'fixture', (), {})


def test_cancellation_rechecks_exact_child_at_dispatch(runtime, monkeypatch):
    parent = runtime.operations('fixture').submit(proposal(runtime), TrustedOperationContext('fixture'))
    directory = runtime.directory(parent['id'])
    atomic_json(directory/'progress.json', dict(status='running'))
    atomic_json(directory/'online-status.json', dict(status='running', request_id='first', card_id='tune'))
    gateway = runtime.operation_gateway()
    dispatch = gateway.call
    def replace_child(*args, **kwargs):
        atomic_json(directory/'online-status.json', dict(status='running', request_id='replacement', card_id='tune'))
        return dispatch(*args, **kwargs)
    monkeypatch.setattr(gateway, 'call', replace_child)
    with pytest.raises(ConflictError, match='Subordinate changed'):
        runtime.stop_online(parent['id'], request_id='first')
    assert not (directory/'online-cancel.json').exists()
    assert not (directory/'cancel.request').exists()


def test_admission_guard_does_not_block_cancellation_during_command(runtime, monkeypatch):
    import threading
    parent = runtime.operations('fixture').submit(proposal(runtime), TrustedOperationContext('fixture'))
    directory = runtime.directory(parent['id'])
    atomic_json(directory/'progress.json', dict(status='running'))
    started, finish = threading.Event(), threading.Event()
    errors = []
    def backend(*args, **kwargs):
        started.set()
        assert finish.wait(3)
        return dict(status='accepted')
    monkeypatch.setattr(runtime, '_change_device_parameters', backend)
    def submit():
        try:
            runtime.change_device_parameters('fixture', {}, request_id='device-change')
        except BaseException as error:
            errors.append(error)
    thread = threading.Thread(target=submit)
    thread.start()
    try:
        assert started.wait(3)
        runtime.stop(parent['id'])
        assert (directory/'cancel.request').exists()
    finally:
        finish.set()
        thread.join(3)
    assert not thread.is_alive() and errors == []


def test_attached_command_status_and_cancel_never_stop_parent(runtime):
    parent = runtime.operations('fixture').submit(proposal(runtime), TrustedOperationContext('fixture'))
    directory = runtime.directory(parent['id'])
    atomic_json(directory/'progress.json', dict(status='running'))
    atomic_json(directory/'runtime-parameter-result-patch.json', dict(status='applied', request_id='patch'))
    port = runtime.operations('fixture')
    result = port.status('fixture', parent['id'], 'patch')
    assert result['attachment'] == 'runtime.patch' and result['status'] == 'applied'
    with pytest.raises(ValueError, match='parent is retained'):
        port.cancel('fixture', parent['id'], 'patch', TrustedOperationContext('fixture'))
    assert not (directory/'cancel.request').exists()


def test_device_command_has_durable_status_without_fake_worker(runtime, monkeypatch):
    monkeypatch.setattr(runtime, '_change_device_parameters', lambda *a, **k: dict(status='applied'))
    runtime.change_device_parameters('fixture', {}, request_id='device-change')
    port = runtime.operations('fixture')
    result = port.status('fixture', 'device-change', 'device-change')
    assert result['status'] == 'accepted' and result['outcome']['receipt']['status'] == 'applied'
    with pytest.raises(ValueError, match='parent is retained'):
        port.cancel('fixture', 'device-change', 'device-change', TrustedOperationContext('fixture'))
    assert not list(runtime.jobs.glob('*/*/job.json'))


def test_missing_common_fields_are_domain_refusal_before_admission(runtime):
    with pytest.raises(ValueError, match='Missing operation proposal fields'):
        runtime.operations('fixture').submit(dict(request_id='missing'), TrustedOperationContext('fixture'))
    assert not runtime.operation_gateway().journal.list('fixture')
