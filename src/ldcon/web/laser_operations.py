"""Shared operation admission/read contract for Web, API, resident and labscript.

This coordinator owns request identity and scheduling, not physical policy. A
trusted host constructs contexts after transport authentication and supplies an
optional owner-description port. Existing owner adapters remain authoritative
for effective policy, leases, every write, and actual stop verification.
"""
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import uuid

from experiment_core.records import Journal, canonical_json, digest, safe_id
from ldcon.core.operation_registry import spec, validate_card, admission_class

SCHEMA = 'ldcon.operation/1'
_ACTIVE = ContextVar('ldcon_operation_admission', default=None)

# Explicit registration; no handler name or import path arrives from a caller.
ACTIONS = {
    'card.start': ('_start_interface_card', 'cooperative_owner_cleanup'),
    'online.submit': ('_request_online', 'cancel_subordinate_preserve_parent'),
    'runtime.patch': ('_inject_parameters', 'retain_active_controller'),
    'controller.command': ('_request_hot_control', 'command_specific_owner_cleanup'),
    'sweep.trigger': ('_sweep_trigger', 'cancel_sweep_preserve_parent'),
    'online.cancel': ('_stop_online', 'cancel_subordinate_preserve_parent'),
    'job.cancel': ('_stop_job', 'cooperative_owner_cleanup'),
    'window.submit': ('_request_window', 'cancel_subordinate_preserve_parent'),
    'window.amend': ('_amend_window', 'unclaimed_request_only'),
    'window.cancel': ('_cancel_window', 'cancel_subordinate_preserve_parent'),
    'device.change': ('_change_device_parameters', 'stop_after_first_uncertain_change'),
}


@dataclass(frozen=True)
class TrustedOperationContext:
    """In-process host context, never deserialized from a received message."""
    laser_id: str
    caller: str = 'compatibility'


def error_record(error, *, effect='none'):
    name = type(error).__name__
    code = ('conflict' if name in ('ConflictError', 'SweepBlocked', 'Conflict') else
            'not_found' if isinstance(error, FileNotFoundError) else
            'authorization' if name in ('ApprovalError', 'AuthorizationRequired') else
            'unavailable' if name in ('MeasurementUnavailable', 'DeviceError') else
            'invalid_request' if isinstance(error, (ValueError, TypeError, KeyError)) else 'unknown')
    return dict(schema=SCHEMA, code=code, message=str(error), effect=effect,
                retry='inspect_then_new_request' if effect != 'none' else 'revalidate')


class OperationGateway:
    def __init__(self, runtime, *, owner_description=None):
        self.runtime = runtime
        self.owner_description = owner_description
        self.journal = Journal(runtime.root / 'operation-requests.sqlite3')

    def close(self):
        self.journal.close()  # No worker stop or physical lease operation.

    def for_laser(self, laser_id):
        return LaserOperations(self, safe_id(laser_id))

    def receipt(self, action, laser_id, request_id, identity):
        """Resolve an existing immutable submission before current-state checks."""
        key = hashlib.sha256(canonical_json([laser_id, action, request_id]).encode()).hexdigest()
        try:
            record = self.journal.get(key)
        except KeyError:
            return None
        from ldcon.web.web_runtime import ConflictError
        if record['request']['fingerprint'] != digest(identity):
            raise ConflictError('Request ID already binds different content or scope')
        if record['state'] != 'accepted':
            error = ConflictError('Prior operation outcome is unconfirmed; inspect request ' + request_id + '; no replay')
            error.operation_error = error_record(error, effect='unknown')
            raise error
        return deepcopy(record['result']['receipt'])

    def call(self, action, laser_id, args, kwargs, *, request_id=None, identity=None):
        """Compatibility path: registered backend, identical admission contract."""
        context = TrustedOperationContext(safe_id(laser_id))
        from experiment_core.errors import Conflict
        try:
            return self._dispatch(action, context, args, kwargs, request_id=request_id, identity=identity)
        except Conflict as error:
            from ldcon.web.web_runtime import ConflictError
            converted = ConflictError(str(error))
            converted.operation_error = error_record(converted)
            raise converted from error

    def _dispatch(self, action, context, args, kwargs, *, request_id=None, identity=None):
        if not isinstance(context, TrustedOperationContext):
            raise ValueError('A trusted host operation context is required')
        if action not in ACTIONS:
            raise ValueError('Unknown operation action')
        laser_id = safe_id(context.laser_id)
        backend = getattr(self.runtime, ACTIONS[action][0])
        active = _ACTIVE.get()
        if active is not None:
            if active['laser_id'] != laser_id:
                raise ValueError('Nested operation cannot change laser scope')
            return backend(*args, **kwargs)
        rid = str(request_id or uuid.uuid4().hex)
        if not rid or len(rid) > 256 or any(ord(c) < 32 for c in rid):
            raise ValueError('Invalid operation request ID')
        key = hashlib.sha256(canonical_json([laser_id, action, rid]).encode()).hexdigest()
        # Keep secrets and large configuration snapshots out of the journal.
        fingerprint = digest(identity if identity is not None else {'args': args, 'kwargs':
            {k: v for k, v in kwargs.items() if k != 'maintainer_token'}})
        intent = dict(schema=SCHEMA, laser_id=laser_id, action=action, request_id=rid,
                      fingerprint=fingerprint, cancellation=ACTIONS[action][1])
        # Hold the cross-process admission guard only while publishing intent.
        # Domain backends own their own mutex/queue/owner boundaries. Keeping a
        # frontend lock across dispatch would obstruct cancellation and local
        # configuration callbacks, and would invert the legacy mutex order.
        with self.journal.guard('operation-admission', laser_id):
            fresh = self.journal.admit(key, laser_id, intent)
            record = self.journal.get(key)
            if fresh:
                self.journal.transition(key, 0, 'dispatching', {'kind': 'dispatch_intent'})
        if not fresh:
            if action == 'sweep.trigger':
                # Inspect the previously published trigger intent only. This
                # path cannot create a missing intent or re-fire hardware.
                return self.runtime._sweep_trigger(*args, **kwargs, _existing_only=True)
            if record['state'] == 'accepted':
                if action in ('job.cancel', 'online.cancel'):
                    return self.runtime.get_job(args[0])
                if action in ('window.submit', 'window.amend', 'window.cancel'):
                    from ldcon.jobs.window_actions import WindowQueue
                    target = args[1]['request_id'] if action == 'window.submit' else args[1]
                    return WindowQueue(self.runtime.directory(args[0])).get(target)
                if action == 'controller.command':
                    from ldcon.core.records import read_json
                    result = read_json(self.runtime.directory(args[0]) / ('hot-control-result-' + rid + '.json'))
                    if result is not None:
                        return result
                return deepcopy(record['result']['receipt'])
            from ldcon.web.web_runtime import ConflictError
            error = ConflictError('Prior operation outcome is unconfirmed; inspect request ' + rid + '; no replay')
            error.operation_error = error_record(error, effect='unknown')
            raise error
        token = _ACTIVE.set(dict(laser_id=laser_id, action=action, request_id=rid))
        try:
            if action == 'card.start':
                with self.runtime.mutex:
                    result = backend(*args, **kwargs)
            else:
                result = backend(*args, **kwargs)
            canonical_json(result)
        except BaseException as error:
            from ldcon.web.web_runtime import TriggerPending
            if isinstance(error, TriggerPending):
                self.journal.transition(key, 1, 'accepted', {'kind': 'trigger_intent_accepted'},
                                        {'receipt': error.record, 'physical_completion': 'not_implied'})
                raise
            value = error_record(error, effect='unknown')
            self.journal.transition(key, 1, 'uncertain', {'kind': 'dispatch_unconfirmed'}, value)
            if isinstance(error, Exception):
                error.operation_error = value
            raise
        finally:
            _ACTIVE.reset(token)
        self.journal.transition(key, 1, 'accepted', {'kind': 'admission_receipt'},
                                {'receipt': deepcopy(result), 'physical_completion': 'not_implied'})
        return result


class LaserOperations:
    """SystemOwner operation port, delegated to existing domain/physical owners."""
    def __init__(self, gateway, laser_id):
        self.gateway, self.runtime, self.laser_id = gateway, gateway.runtime, laser_id

    def _context(self, context):
        if not isinstance(context, TrustedOperationContext) or context.laser_id != self.laser_id:
            raise ValueError('Operation context belongs to another laser')

    @staticmethod
    def _proposal(proposal):
        allowed = {'schema', 'subject_id', 'action', 'set_id', 'deck_id', 'card_id',
                   'parameters', 'expected_revision', 'request_id', 'run_id', 'approval_id', 'dry_run'}
        if not isinstance(proposal, dict) or set(proposal) - allowed:
            raise ValueError('Unknown operation proposal fields')
        if proposal.get('schema', SCHEMA) != SCHEMA:
            raise ValueError('Unsupported operation contract schema')
        action = proposal.get('action', 'card.start')
        required = ({'set_id','deck_id','card_id','expected_revision'} if action == 'card.start'
                    else set() if action == 'device.change' else {'run_id'})
        if action == 'online.submit':required.add('card_id')
        if not required <= proposal.keys():
            raise ValueError('Missing operation proposal fields: ' + ', '.join(sorted(required-proposal.keys())))
        canonical_json(proposal)

    def _subject(self, subject_id):
        if subject_id != self.laser_id:
            raise ValueError('Operation subject belongs to another laser')

    def _selection(self, proposal):
        self._subject(proposal.get('subject_id', self.laser_id))
        value = self.runtime.get_set(safe_id(proposal['set_id']))
        laser = value['laser_definition']['document']
        if laser['id'] != self.laser_id:
            raise ValueError('Assignment belongs to another laser')
        if proposal.get('expected_revision') != value['revision']:
            from ldcon.web.web_runtime import ConflictError
            raise ConflictError('Configuration changed. Reload before validating.')
        definition = next((d for d in value['deck_definitions'] if d['id'] == proposal['deck_id']), None)
        if definition is None:
            raise FileNotFoundError(proposal['deck_id'])
        card = next((c for c in definition['document']['cards'] if c['id'] == proposal['card_id']), None)
        if card is None:
            raise FileNotFoundError(proposal['card_id'])
        spec(card['operation'])
        overrides = proposal.get('parameters', {})
        if not isinstance(overrides, dict):
            raise ValueError('Parameters must be a mapping')
        return value, definition, card, overrides

    def describe(self, subject_id):
        self._subject(subject_id)
        cards = []
        configured = None
        for item in self.runtime.interface_repository.list('assignments'):
            value = self.runtime.get_set(item['id'])
            if value['laser_definition']['document']['id'] != self.laser_id:
                continue
            configured = value['laser_definition']['revision']
            for definition in value['deck_definitions']:
                for card in definition['document']['cards']:
                    try:
                        registration = spec(card['operation'])
                    except ValueError:
                        continue
                    proposal = dict(subject_id=self.laser_id, set_id=value['id'], deck_id=definition['id'],
                                    card_id=card['id'], expected_revision=value['revision'], parameters={})
                    status = self.preflight(proposal, TrustedOperationContext(self.laser_id))
                    cards.append(dict(registration.describe(), set_id=value['id'], deck_id=definition['id'],
                                      card_id=card['id'], **status))
        owner = (self.gateway.owner_description(self.laser_id) if self.gateway.owner_description
                 else {'status': 'not_connected', 'effective_policy_revision': None, 'observed': None})
        return dict(schema=SCHEMA, subject_id=self.laser_id, configured_policy_revision=configured,
                    owner=owner, cards=cards, actions=list(ACTIONS),
                    authority='Configured capability is not a physical write grant')

    def preflight(self, proposal, trusted_context):
        self._context(trusted_context)
        self._proposal(proposal)
        if proposal.get('action', 'card.start') != 'card.start':
            action = proposal.get('action')
            if action not in ACTIONS:
                raise ValueError('Unknown operation action')
            self._subject(proposal.get('subject_id', self.laser_id))
            if action != 'device.change':
                job = self.runtime.get_job(safe_id(proposal['run_id']))
                if job.get('laser', {}).get('id') != self.laser_id:
                    raise ValueError('Run belongs to another laser')
            if not isinstance(proposal.get('parameters', {}), dict):
                raise ValueError('Parameters must be a mapping')
            return dict(schema=SCHEMA, action=action, runnable=True,
                        validation_level='request_shape_and_scope',
                        physical_validation='owner_revalidation_required',
                        cancellation=ACTIONS[action][1])
        value, definition, card, overrides = self._selection(proposal)
        laser = value['laser_definition']['document']
        p = {**definition['invocation_parameters'].get(card['id'], {}), **overrides}
        reference_error = None
        if card['operation']=='tune.ecdl_operating_point':
            # Same resolution as card.start (2026-10-27 fix): a same-laser artifact reference in the assignment defaults
            # or the overrides is resolved before readiness, so the dashboard does not show the false block
            # "Resolve the same-laser recovery reference before starting" (observed on sr422 tec-branch-alignment).
            from ldcon.jobs.workflow_artifacts import resolve_inputs
            try:
                p = resolve_inputs(self.runtime, laser['id'], p)
            except (ValueError, KeyError, FileNotFoundError, OSError) as error:
                reference_error = str(error)
        result = validate_card(laser, definition['document'], card, p)
        if reference_error:
            result['blocked_reasons'] = [reference_error] + list(result['blocked_reasons'])
            result['runnable'] = False
        jobs = self.runtime.active_jobs(set_id=value['id'])
        admission = admission_class(laser, card, jobs)
        registration = spec(card['operation'])
        reasons = list(result['blocked_reasons'])
        controller_job_id = None
        if registration.execution in ('attached', 'job_or_attached'):
            target = result['parameters'].get('controller_card_id')
            candidates = [j for j in jobs if j.get('deck_id') == definition['id'] and j.get('status') == 'running'
                          and any(c.get('id') == target for c in j.get('cards', []))]
            if len(candidates) == 1:
                controller_job_id = candidates[0]['id']
                controller = self.runtime.get_job(controller_job_id)
                reasons = [reason for reason in reasons if reason not in (
                    'A matching running ECDL stationary lock is required',
                    'A running stationary PID controller is required')]
                if not controller.get('online_supported'):
                    reasons.append('Online tuning requires a running stationary PID owner')
                if registration.operation_id == 'tune.ecdl_window_center':
                    capability = controller.get('window_controller', {})
                    if capability and capability.get('status') != 'running':
                        reasons.append('The live midpoint capability is unavailable')
                elif controller.get('online_tuning', {}).get('status') in ('queued', 'running'):
                    reasons.append('An online operation is already active')
            elif registration.execution == 'attached' or result['parameters'].get('measure_dither',True) or result['parameters'].get('mode')!='center_only':
                reasons.append('One matching live controller is required; no controller will be stopped or started automatically')
        from ldcon.core.card_admission import job_owns_control_slot
        blockers = [j['id'] for j in jobs if job_owns_control_slot(j)] if admission == 'exclusive' and controller_job_id is None else []
        if blockers:
            reasons.append('This laser already has an active card')
        return dict(result, schema=SCHEMA, operation_id=registration.operation_id, version=registration.version,
                    runnable=not reasons, blocked_reasons=reasons, admission=admission,
                    controller_job_id=controller_job_id, blockers=blockers,
                    configured_policy_revision=value['laser_definition']['revision'],
                    effective_policy_revision=None, physical_validation='owner_revalidation_required',
                    cancellation=registration.cancellation)

    def submit(self, proposal, trusted_context):
        self._context(trusted_context)
        self._proposal(proposal)
        safe_id(proposal.get('request_id'))
        if proposal.get('action', 'card.start') != 'card.start':
            self.preflight(proposal, trusted_context)
            return self._command(proposal, trusted_context)
        self._subject(proposal.get('subject_id', self.laser_id))
        receipt = self.gateway.receipt('card.start', self.laser_id, proposal['request_id'],
            dict(set_id=proposal['set_id'], revision=proposal['expected_revision'], deck_id=proposal['deck_id'],
                 card_id=proposal['card_id'], parameters=proposal.get('parameters', {}), reservation=None))
        if receipt is not None:
            return dict(receipt, operation_request_id=proposal['request_id'])
        value, definition, card, overrides = self._selection(proposal)
        check = self.preflight(proposal, trusted_context)
        if not check['runnable']:
            from ldcon.web.web_runtime import ConflictError
            raise ConflictError(' / '.join(check['blocked_reasons']))
        return self.runtime.start_interface_card(value, definition['id'], card['id'], overrides,
                                                  job_id=proposal.get('request_id'))

    def _command(self, proposal, context):
        action = proposal['action']
        run_id = proposal.get('run_id')
        p = deepcopy(proposal.get('parameters', {}))
        rid = proposal.get('request_id')
        if not isinstance(rid, str) or not rid:
            raise ValueError('A stable request_id is required')
        if action == 'online.submit':
            return self.runtime.request_online(run_id, proposal['card_id'], p, request_id=rid)
        if action in ('runtime.patch', 'controller.command', 'sweep.trigger', 'window.submit'):
            if p.get('request_id', rid) != rid:
                raise ValueError('Command and proposal request IDs differ')
            p['request_id'] = rid
            if action == 'runtime.patch':
                return self.runtime.inject_parameters(run_id, p, approval_id=proposal.get('approval_id'), actor=context.caller)
            if action == 'controller.command':
                return self.runtime.request_hot_control(run_id, p)
            if action == 'sweep.trigger':
                return self.runtime.sweep_trigger(run_id, p)
            return self.runtime.request_window(run_id, p, source='agent' if context.caller == 'agent' else 'manual')
        if action == 'window.amend':
            return self.runtime.amend_window(run_id, rid, p)
        if action == 'window.cancel':
            return self.runtime.cancel_window(run_id, rid, expected_revision=p.get('expected_revision'))
        if action == 'online.cancel':
            return self.runtime.stop_online(run_id, request_id=rid)
        if action == 'job.cancel':
            if rid != run_id:
                raise ValueError('Use subordinate cancellation for an attached request')
            return self.runtime.stop(run_id)
        if action == 'device.change':
            return self.runtime.change_device_parameters(self.laser_id, p, actor=context.caller,
                                                        request_id=rid, dry_run=proposal.get('dry_run', False))
        raise ValueError('Unknown operation action')

    def status(self, subject_id, run_id, request_id):
        self._subject(subject_id)
        safe_id(request_id)
        try:
            job = self.runtime.get_job(safe_id(run_id))
        except FileNotFoundError:
            if run_id != request_id:
                raise
            key = hashlib.sha256(canonical_json([self.laser_id, 'device.change', request_id]).encode()).hexdigest()
            try:
                record = self.gateway.journal.get(key)
            except KeyError:
                raise FileNotFoundError(run_id) from None
            return dict(schema=SCHEMA, subject_id=self.laser_id, request_id=request_id,
                        run_id=run_id, attachment='device.change', status=record['state'],
                        outcome=record['result'], physical_stop='independent_owner_verification_required')
        if job.get('laser', {}).get('id') != self.laser_id:
            raise ValueError('Run belongs to another laser')
        if request_id != run_id:
            from ldcon.jobs.window_actions import WindowQueue
            try:
                request = WindowQueue(self.runtime.directory(run_id)).get(request_id)
            except KeyError:
                request = None
            kind = 'window'
            if request is None:
                request = job.get('online_tuning')
                kind = 'online'
                if not request or request.get('request_id') != request_id:
                    request, kind = self._attached_command_status(run_id, request_id, job)
            return dict(schema=SCHEMA, subject_id=self.laser_id, request_id=request_id,
                        run_id=run_id, attachment=kind, status=request.get('status'), request=request,
                        parent_status=job['status'], physical_stop='parent_retained')
        return dict(schema=SCHEMA, subject_id=self.laser_id, request_id=request_id,
                    run_id=run_id, admission='accepted', status=job['status'],
                    outcome=job.get('summary', {}).get('operation_outcome'), job=job,
                    owner=self.gateway.owner_description(self.laser_id) if self.gateway.owner_description else None,
                    physical_stop='independent_owner_verification_required')

    def _attached_command_status(self, run_id, request_id, job):
        from ldcon.core.records import read_json
        directory = self.runtime.directory(run_id)
        for stage in ('result', 'inflight', 'request'):
            request = read_json(directory / ('runtime-parameter-' + stage + '-' + request_id + '.json'))
            if isinstance(request, dict) and request.get('request_id') == request_id:
                return request, 'runtime.patch'
        for name in ('hot-control-result-' + request_id + '.json', 'hot-control-status.json'):
            request = read_json(directory / name)
            if isinstance(request, dict) and request.get('request_id') == request_id:
                return request, 'controller.command'
        shot = job.get('shot_sweep') or {}
        if shot.get('request_id') == request_id:
            return dict(shot, status=shot.get('state')), 'sweep.trigger'
        raise FileNotFoundError(request_id)

    def cancel(self, subject_id, run_id, request_id, trusted_context):
        self._subject(subject_id)
        self._context(trusted_context)
        current = self.status(subject_id, run_id, request_id)
        if current.get('attachment') == 'window':
            return self.runtime.cancel_window(run_id, request_id)
        if current.get('attachment') == 'online':
            return self.runtime.stop_online(run_id, request_id=request_id)
        if current.get('attachment') == 'sweep.trigger':
            # This is the dedicated sweep job; the proxy worker owns cleanup
            # of its action and preserves its separately running controller.
            return self.runtime.stop(run_id)
        if current.get('attachment') is not None or request_id != run_id:
            raise ValueError('This accepted command has no independent cancellation; the parent is retained')
        return self.runtime.stop(run_id)
