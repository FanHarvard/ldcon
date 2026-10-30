"""Offline adversarial authority/projection boundary tests; no instrument I/O."""
from copy import deepcopy
from dataclasses import replace
import pickle
from threading import Event
from concurrent.futures import ThreadPoolExecutor

import pytest

from experiment_core.errors import AuthorizationRequired, Conflict, StaleContext, UncertainWrite
from experiment_core.records import Journal, digest
from experiment_core.ownership import Reservations
from ldcon.owners.owner_projection import PhysicalProjectionGuard, SystemPolicyAuthority, VerifiedSystemSource


@pytest.fixture
def boundary(tmp_path):
    journal = Journal(tmp_path / 'state.sqlite')
    Reservations(journal, 'other-core-owner')  # shared state root must not collide with the core's schema
    authority = SystemPolicyAuthority('laser', {'upper': 10}, dependencies={'calibration': 'v1'}, journal=journal)
    def validate(operation, constraints):
        if operation['value'] > constraints['upper']:
            raise ValueError('outside physical projected limit')
    guard = PhysicalProjectionGuard('device', {'device/ch1', 'device/ch2'},
        definition_revision='physical-v1', intrinsic_validator=validate, journal=journal)
    binding = guard.bind(authority, {'device/ch1'})
    projection = authority.project(binding, {'upper': 10, 'cleanup_operations': [{'value': 0}]})
    guard.install(binding, projection, expected_revision=None)
    yield journal, authority, guard, binding
    journal.close()


def test_caller_claimed_identity_and_serialized_bindings_cannot_authorize(boundary):
    _, authority, guard, binding = boundary
    with pytest.raises(AuthorizationRequired):
        guard.execute({'system_id': 'laser', 'approved': True}, {'value': 1}, lambda: pytest.fail('write'))
    with pytest.raises(TypeError):
        pickle.dumps(binding)
    permit = authority.permit(binding, {'value': 1}, request_id='one')
    with pytest.raises(AuthorizationRequired):
        guard.execute(permit, {'value': 2}, lambda: pytest.fail('write'))


def test_projection_cas_resources_dependency_and_intrinsic_checks(boundary):
    _, authority, guard, binding = boundary
    revision = guard.inspect(binding)['projection_revision']
    with pytest.raises(Conflict):
        guard.install(binding, authority.project(binding, {'upper': 8}), expected_revision='stale')
    with pytest.raises(AuthorizationRequired):
        authority.permit(binding, {'value': 1}, request_id='wide', resources={'device/ch2'})
    permit = authority.permit(binding, {'value': 11}, request_id='too-high')
    with pytest.raises(ValueError):
        guard.execute(permit, {'value': 11}, lambda: pytest.fail('write'))
    assert guard.inspect(binding)['fault'] is None
    permit = authority.permit(binding, {'value': 2}, request_id='old-calibration')
    authority.configuration.set_dependencies({'calibration': 'v2'})
    with pytest.raises(StaleContext):
        guard.execute(permit, {'value': 2}, lambda: pytest.fail('write'))
    assert guard.inspect(binding)['projection_revision'] == revision


def test_ambiguous_write_quarantines_but_distinct_admitted_cleanup_is_available(boundary):
    _, authority, guard, binding = boundary
    calls = []
    def lost():
        calls.append('mutation')
        raise OSError('reply lost')
    op = {'value': 5}
    permit = authority.permit(binding, op, request_id='maybe-written')
    with pytest.raises(OSError):
        guard.execute(permit, op, lost)
    with pytest.raises(UncertainWrite):
        guard.execute(permit, op, lost)
    with pytest.raises(AuthorizationRequired):
        authority.permit_cleanup(binding, {'value': 1}, request_id='invented-cleanup')
    cleanup = authority.permit_cleanup(binding, {'value': 0}, request_id='bounded-disable')
    assert guard.execute_cleanup(cleanup, {'value': 0}, lambda: calls.append('disable') or {'confirmed': True}) == {'confirmed': True}
    assert calls == ['mutation', 'disable']
    assert guard.inspect(binding)['fault'] is not None


def test_old_system_and_physical_objects_are_fenced_after_replacement(boundary):
    journal, authority, guard, binding = boundary
    permit = authority.permit(binding, {'value': 1}, request_id='before-restart')
    SystemPolicyAuthority('laser', {'upper': 10}, dependencies={'calibration': 'v1'}, journal=journal)
    with pytest.raises(StaleContext):
        authority.project(binding, {'upper': 10})
    with pytest.raises(StaleContext):
        guard.execute(permit, {'value': 1}, lambda: pytest.fail('write'))
    PhysicalProjectionGuard('device', {'device/ch1'}, definition_revision='physical-v1',
                            intrinsic_validator=lambda *args: None, journal=journal)
    with pytest.raises(StaleContext):
        guard.inspect()
    with pytest.raises(StaleContext):
        guard.reconcile(binding, lambda state: {'settled': True, 'quiesced': True})


def test_generation_replacement_cannot_race_an_inflight_write(boundary):
    journal, authority, guard, binding = boundary
    entered, finish = Event(), Event()
    def blocked():
        entered.set()
        assert finish.wait(5)
        return {'done': True}
    permit = authority.permit(binding, {'value': 1}, request_id='blocked')
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(guard.execute, permit, {'value': 1}, blocked)
        try:
            assert entered.wait(5)
            with pytest.raises(Conflict):
                SystemPolicyAuthority('laser', {'upper': 10}, dependencies={'calibration': 'v1'}, journal=journal)
            with pytest.raises(Conflict):
                PhysicalProjectionGuard('device', {'device/ch1'}, definition_revision='v1',
                                        intrinsic_validator=lambda *args: None, journal=journal)
        finally:
            finish.set()
        assert future.result(timeout=5) == {'done': True}


def test_independent_systems_keep_disjoint_channel_projections(boundary):
    journal, authority, guard, binding = boundary
    other = SystemPolicyAuthority('other-laser', {'upper': 20}, dependencies={}, journal=journal)
    second = guard.bind(other, {'device/ch2'})
    guard.install(second, other.project(second, {'upper': 20}), expected_revision=None)
    assert guard.execute(authority.permit(binding, {'value': 9}, request_id='first'), {'value': 9}, lambda: 9) == 9
    assert guard.execute(other.permit(second, {'value': 19}, request_id='second'), {'value': 19}, lambda: 19) == 19
    assert len(guard.inspect()['projections']) == 2


def test_exact_approval_and_candidate_projection_are_published_together(boundary):
    _, authority, guard, binding = boundary
    old = authority.revision
    candidate = {'upper': 8}
    token = authority.approve(candidate, expected_revision=old,
                             authorize_human=lambda p: {'human_identity': 'human', 'receipt_id': 'exact'})
    def apply(document, proof):
        guard.install(binding, authority.project(binding, {'upper': document['upper']}),
                      expected_revision=guard.inspect(binding)['projection_revision'])
        assert guard.execute(authority.permit(binding, {'value': 7}, request_id='adoption-write'),
                             {'value': 7}, lambda: 7) == 7
    with pytest.raises(AuthorizationRequired):
        authority.adopt({'upper': 7}, expected_revision=old, approval=token,
                        prepare=lambda p: {}, apply=apply,
                        verify=lambda p: {'settled': True, 'source_revision': digest(p)})
    authority.adopt(candidate, expected_revision=old, approval=token, prepare=lambda p: {}, apply=apply,
                    verify=lambda p: {'settled': True, 'source_revision': digest(p)})
    assert authority.snapshot() == candidate
    assert guard.inspect(binding)['projection']['policy_revision'] == authority.revision
    copy = authority.snapshot(); copy['upper'] = 1000
    assert authority.snapshot()['upper'] == 8


def test_shared_reconciliation_requires_every_participant_quiesced(boundary):
    journal, authority, guard, binding = boundary
    second_authority = SystemPolicyAuthority('second', {'upper': 10}, dependencies={}, journal=journal)
    second = guard.bind(second_authority, {'device/ch2'})
    guard.install(second, second_authority.project(second, {'upper': 10}), expected_revision=None)
    guard.quarantine('unresolved shared transport')
    with pytest.raises(UncertainWrite, match='every participant'):
        guard.reconcile(binding, lambda state: {'settled': True, 'quiesced': True})
    assert len(guard.inspect()['projections']) == 2 and guard.inspect()['fault']
    guard.reconcile(binding, lambda state: {'settled': True, 'quiesced': True,
                                           'quiesced_resources': ['device/ch1', 'device/ch2']})
    assert not guard.inspect()['projections'] and guard.inspect()['fault'] is None


def test_restart_cannot_silently_replace_calibration_dependencies(boundary):
    journal, authority, guard, binding = boundary
    with pytest.raises(StaleContext, match='dependencies'):
        SystemPolicyAuthority('laser', {'upper': 10}, dependencies={'calibration': 'v2'}, journal=journal)
    assert authority.dependencies == {'calibration': 'v1'}


def test_failed_adoption_blocks_writes_until_all_projections_reconcile(boundary):
    _, authority, guard, binding = boundary
    candidate, old = {'upper': 8}, authority.revision
    token = authority.approve(candidate, expected_revision=old,
                             authorize_human=lambda p: {'human_identity': 'human', 'receipt_id': 'fault'})
    def apply(document, proof):
        guard.install(binding, authority.project(binding, {'upper': 8, 'cleanup_operations': [{'value': 0}]}),
                      expected_revision=guard.inspect(binding)['projection_revision'])
        raise OSError('reply after adoption lost')
    with pytest.raises(UncertainWrite):
        authority.adopt(candidate, expected_revision=old, approval=token, prepare=lambda p: {}, apply=apply,
                        verify=lambda p: pytest.fail('unverified'))
    with pytest.raises(UncertainWrite, match='Pending policy'):
        authority.project(binding, {'upper': 10})
    permit = authority.permit(binding, {'value': 1}, request_id='pending')
    with pytest.raises(UncertainWrite, match='Pending policy'):
        guard.execute(permit, {'value': 1}, lambda: pytest.fail('write'))
    with pytest.raises(UncertainWrite, match='physical participant'):
        authority.reconcile(lambda p: {'settled': True, 'source_revision': old})
    authority.reconcile(lambda p: {'settled': True, 'source_revision': digest(p)})
    assert authority.snapshot() == candidate
