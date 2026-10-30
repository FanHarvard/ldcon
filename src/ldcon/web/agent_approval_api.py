"""Dashboard routes for laser-agent safety proposals and direct human approvals.

The dashboard process runs outside the resident sandbox, so it is the only
writer of the approval ledger besides the resident worker's prompt records.
No route touches hardware; an approval only becomes effective when the same
dashboard later applies a configuration or runtime change that cites it.
"""
import asyncio

from fastapi import Request

from ldcon.core.agent_authority import ApprovalLedger, ApprovalError, describe_changes, rules_summary, _lookup
from ldcon.core.interface_schema import identifier, load_mapping
from ldcon.core.records import read_json


def install_routes(app, prefix, body):
    def ledger(laser_id):
        runtime = app.state.runtime
        runtime.interface_repository.read('lasers', identifier(laser_id))
        return ApprovalLedger(runtime.root, laser_id)

    @app.get(prefix + '/api/agent-authority/rules')
    def rules():
        return rules_summary()

    @app.get(prefix + '/api/lasers/{laser_id}/agent-approvals')
    def listing(laser_id: str):
        book = ledger(laser_id)
        return dict(laser_id=laser_id, directory=str(book.directory), proposals=book.proposals(),
                    audit=book.recent_audit(), human_prompts=len(book.human_prompts()))

    @app.get(prefix + '/api/lasers/{laser_id}/agent-approvals/{proposal_id}')
    def detail(laser_id: str, proposal_id: str):
        return ledger(laser_id).proposal(proposal_id)

    @app.post(prefix + '/api/lasers/{laser_id}/agent-approvals/proposals', status_code=201)
    async def propose(laser_id: str, request: Request):
        data = await body(request)
        if set(data) != {'bridge_id', 'scope', 'changes', 'reason'}:
            raise ValueError('Expected bridge_id, scope, changes and reason')

        def run():
            runtime = app.state.runtime
            with runtime.mutex:
                runtime.assert_admission_open()
                book = ledger(laser_id)
                scope = ApprovalLedger.normalize_scope(data['scope'])
                rows = ApprovalLedger.normalize_changes(data['changes'])
                classified = _classify_against_current(runtime, laser_id, scope, rows)
                return book.propose(bridge_id=data['bridge_id'], scope=scope, changes=rows,
                                    reason=data['reason'], classified=classified)
        return await asyncio.to_thread(run)

    @app.post(prefix + '/api/lasers/{laser_id}/agent-approvals/{proposal_id}/approve')
    async def approve(laser_id: str, proposal_id: str, request: Request):
        data = await body(request)
        if set(data) != {'bridge_id', 'quote', 'human_name'}:
            raise ValueError('Expected bridge_id, quote and human_name')

        def run():
            runtime = app.state.runtime
            with runtime.mutex:
                runtime.assert_admission_open()
                try:
                    return ledger(laser_id).approve(proposal_id, quote=data['quote'],
                                                    human_name=data['human_name'], bridge_id=data['bridge_id'])
                except ApprovalError as error:
                    # 403: the request was well-formed but no direct approval exists.
                    from fastapi import HTTPException
                    raise HTTPException(403, str(error)) from error
        return await asyncio.to_thread(run)


def _classify_against_current(runtime, laser_id, scope, rows):
    """Reject proposals that could never be applied: forbidden paths, wrong old values."""
    if scope['kind'] == 'runtime':
        directory = runtime.directory(scope['job_id'])
        meta = read_json(directory / 'job.json', {})
        if meta.get('laser', {}).get('id') != laser_id:
            raise ValueError('Runtime scope belongs to another laser')
        live = read_json(directory / 'runtime-parameters.json', {})
        current = live.get('parameters', {})
        _check_old_values(current, rows)
        candidate = _apply_rows(current, rows)
        return describe_changes('runtime', current, candidate)
    if scope['name'] != laser_id:
        raise ValueError('Scope name must be this laser')
    if scope['kind'] == 'devices':
        # device-owner parameters (rule I): classes and old values come from the owners' catalogs via the registry
        from ldcon.jobs.device_parameters import RegistryLink, classify_rows
        laser = runtime.interface_repository.read('lasers', laser_id)['document']
        return classify_rows(laser, rows, RegistryLink())
    current = runtime.interface_repository.read(scope['kind'], scope['name'])['document']
    _check_old_values(current, rows)
    candidate = _apply_rows(current, rows)
    return describe_changes(scope['kind'], current, candidate)


def _check_old_values(current, rows):
    stale = []
    for row in rows:
        value, present = _lookup(current, tuple(row['path'].split('.')))
        if not ApprovalLedger._same(value if present else None, row['old']):
            stale.append(row['path'])
    if stale:
        raise ValueError('Proposal old values do not match the current definition at: ' + ', '.join(sorted(stale)))


def _apply_rows(document, rows):
    import copy
    candidate = copy.deepcopy(document)
    for row in rows:
        parts = row['path'].split('.')
        parent = candidate
        for part in parts[:-1]:
            if isinstance(parent, list):
                parent = parent[int(part)]
            else:
                parent = parent.setdefault(part, {})
        key = parts[-1]
        if isinstance(parent, list):
            parent[int(key)] = row['new']
        else:
            parent[key] = row['new']
    return candidate
