"""Durable coordination of the authoritative laser file and native policy.

No recovery operation repeats an uncertain mutation. A committed owner's exact
identity authorizes publication of the already approved file; a concurrent edit
or incomplete owner journal remains visibly pending and blocks new starts.
"""
from ldcon.core.state_layout import config_file

import hashlib
import json
import os
from pathlib import Path
import tempfile
import time


def _revision(raw):
    return hashlib.sha256(raw).hexdigest()


def _sync(directory):
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            os.fchmod(stream.fileno(), 0o660)
            json.dump(value, stream, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        _sync(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def transaction_directory(root):
    """Host-only journal beside the trusted decision ledger, outside the sandbox."""
    from ldcon.core.agent_authority import approval_root
    identity = hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()[:24]
    return approval_root(root, 'policy-transactions').parent / 'policy-transactions' / identity


def pending_transactions(root):
    result = []
    for path in sorted(transaction_directory(root).glob('*.json')):
        try:
            value = json.loads(path.read_text())
            if not isinstance(value, dict) or value.get('schema_version') != 1:
                raise ValueError('Invalid policy transaction')
        except (OSError, ValueError) as error:
            value = dict(laser_id=None, status='reconciliation_required', reason=str(error))
        if value.get('status') not in ('published', 'aborted'):
            result.append(dict(value, journal=str(path)))
    return result


def _publish(path, value):
    from ldcon.web.web_runtime import ConflictError
    raw = value['candidate_text'].encode()
    if _revision(raw) != value['revision_after']:
        raise ConflictError('Policy transaction candidate digest is invalid')
    actual = _revision(path.read_bytes())
    if actual == value['revision_after']:
        return
    if actual != value['revision_before']:
        raise ConflictError('Concurrent laser edit: owner policy committed; exact file reconciliation required')
    descriptor, name = tempfile.mkstemp(prefix='.' + path.stem + '-', suffix='.yaml.new', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            os.fchmod(stream.fileno(), 0o660)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        # CAS immediately before publication as well as before owner commit.
        if _revision(path.read_bytes()) != value['revision_before']:
            raise ConflictError('Laser changed during transaction publication; reconciliation required')
        temporary.replace(path)
        _sync(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _matches(value, response):
    return (response.get('decision_id') == value['decision_id']
            and response.get('expected_definition') == value['expected_definition']
            and response.get('approved_definition') == value['approved_definition'])


def _finish_publication(runtime, value):
    """Publish recovered worker requests and complete single-use approval."""
    authority = value.get('authority', {})
    if authority.get('authority') == 'human_approval':
        from ldcon.core.agent_authority import ApprovalLedger
        ApprovalLedger(runtime.root, value['laser_id']).finish_reserved(authority['approval_id'],
            decision_id=value['decision_id'], result=dict(status='published',
                decision_id=value['decision_id'], revision_after=value['revision_after']))
    if hasattr(runtime, 'active_jobs'):
        from ldcon.core.configuration import _queue_laser_reloads
        value['jobs'] = _queue_laser_reloads(runtime, value['laser_id'], value['changes'],
            value['revision_before'], value['revision_after'], dict(authority, decision_id=value['decision_id']))


def reconcile_pending(runtime, rpc=None):
    if rpc is None:
        from ldcon.core.configuration import owner_rpc
        rpc = owner_rpc
    results = []
    for value in pending_transactions(runtime.root):
        path = Path(value.pop('journal'))
        try:
            if value.get('schema_version') != 1:
                results.append(value)
                continue
            response = rpc(value['endpoint'], dict(action='policy_update_status',
                device_id=value['device_id'], laser_id=value['laser_id'], decision_id=value['decision_id']))
            if response.get('status') == 'committed' and _matches(value, response):
                _publish(config_file(runtime.root, 'lasers', value['laser_id']), value)
                _finish_publication(runtime, value)
                value.update(status='published', owner_result=response.get('result', response))
            elif (response.get('status') == 'absent' and not value.get('owner_result')
                    and (value['status'] == 'prepared' or response.get('policy_digest') == value['expected_definition'])
                    and _revision((config_file(runtime.root, 'lasers', value['laser_id'])).read_bytes()) == value['revision_before']):
                # Status is serialized behind the owner's transaction lock. No
                # journal plus the original policy/file proves no commit, even
                # when the caller lost a prewrite refusal response.
                authority = value.get('authority', {})
                if authority.get('authority') == 'human_approval':
                    from ldcon.core.agent_authority import ApprovalLedger
                    ledger = ApprovalLedger(runtime.root, value['laser_id'])
                    approval = ledger.proposal(authority['approval_id'])
                    if approval.get('status') == 'reserved':
                        ledger.abort_reserved(authority['approval_id'], decision_id=value['decision_id'],
                            reason='Owner confirmed no commit; original laser policy remains effective')
                value.update(status='aborted', reason='Owner confirmed no commit; original laser policy remains effective')
            else:
                value.update(status='reconciliation_required', reason='Owner commit status is unresolved or identities differ')
        except Exception as error:
            value.update(status='reconciliation_required', reason=str(error))
        value['updated_unix_s'] = time.time()
        try:
            _write(path, value)
        except OSError:
            pass  # Existing durable intent still blocks admission.
        results.append(value)
    return results


def commit_native_file(runtime, *, name, path, before, after, text, expected, rows, decision, rpc):
    """Prepare -> durable intent -> owner commit -> exact YAML publication."""
    from ldcon.core.interface_schema import digest
    from ldcon.web.web_runtime import ConflictError
    from ldcon.owners.cld_projection import CLD_TYPE
    # Rule M steps 3-4: one native owner adopts its native_current policy, its native_ff_sweep profile and the
    # laser's CLD operating settings (projected to the CLD owner) through the same decision.
    natives = [(key, device) for key, device in after.get('devices', {}).items()
               if device.get('type') == 'esp32servocon.native_current.owner']
    if len(natives) != 1:
        raise ConflictError('One native owner per policy transaction is required')
    key, device = natives[0]
    prefixes = ['devices.' + key + '.control_settings.native_current.', 'devices.' + key + '.control_settings.native_ff_sweep.']
    prefixes += ['devices.' + other + '.' for other, entry in after['devices'].items() if entry.get('type') == CLD_TYPE]
    mine = [row for row in rows if any(row['path'].startswith(prefix) for prefix in prefixes)]
    if not mine:
        raise ConflictError('One native owner per policy transaction is required')
    if any(row.get('live') != 'native_hot' for row in mine):
        raise ConflictError('Native identity/calibration changes require the idle reviewed path')
    request = dict(device_id=device['connection']['device_id'], laser_id=name,
        expected_definition=digest(before['devices'][key]['control_settings']['native_current']), decision_id=decision['id'])
    approved = digest(device['control_settings']['native_current'])
    prepared = rpc(device['connection']['endpoint'], dict(action='policy_update_prepare', **request))
    if (prepared.get('status') not in ('prepared', 'unchanged')
            or prepared.get('approved_definition') != approved):
        raise ConflictError('Native owner did not prepare the exact approved policy')
    # Test directory write/fsync before any owner commit. The journal itself holds
    # the full candidate and is sufficient to finish publication after a crash.
    journal = transaction_directory(runtime.root) / (decision['id'] + '.json')
    value = dict(schema_version=1, status='prepared', laser_id=name, device_id=request['device_id'],
        endpoint=device['connection']['endpoint'], decision_id=decision['id'],
        expected_definition=request['expected_definition'], approved_definition=approved,
        revision_before=expected, revision_after=_revision(text.encode()), candidate_text=text,
        created_unix_s=time.time(), changes=rows, authority=decision)
    _write(journal, value)
    if _revision(path.read_bytes()) != expected:
        value.update(status='aborted', reason='Laser changed before owner commit')
        _write(journal, value)
        raise ConflictError('Configuration changed during validation; owner commit not dispatched')
    value['status'] = 'committing'
    _write(journal, value)
    try:
        if decision.get('authority') == 'human_approval':
            from ldcon.core.agent_authority import ApprovalLedger, partition, SAFETY
            ApprovalLedger(runtime.root, name).reserve(decision['approval_id'], decision_id=decision['id'],
                scope=dict(kind='lasers',name=name), safety_rows=partition(rows)[SAFETY])
        result = rpc(value['endpoint'], dict(action='policy_update', **request))
        # Read-only status identifies the durable exact commit even when a legacy
        # successful reply has fewer identity fields.
        status = rpc(value['endpoint'], dict(action='policy_update_status', device_id=request['device_id'],
            laser_id=name, decision_id=decision['id']))
        if status.get('status') != 'committed' or not _matches(value, status):
            raise ConflictError('Owner commit requires durable identity reconciliation')
        value.update(status='owner_committed', owner_result=result)
        _write(journal, value)
        _publish(path, value)
        if decision.get('authority') == 'human_approval':
            ApprovalLedger(runtime.root, name).finish_reserved(decision['approval_id'],
                decision_id=decision['id'], result=dict(status='published', decision_id=decision['id'],
                    revision_after=value['revision_after']))
        value['status'] = 'published'
        _write(journal, value)
        return {key: result}
    except Exception as error:
        value.update(status='reconciliation_required', reason=str(error))
        _write(journal, value)
        raise ConflictError('Policy transaction pending reconciliation; owner may have adopted the approved policy: ' + str(error)) from error
