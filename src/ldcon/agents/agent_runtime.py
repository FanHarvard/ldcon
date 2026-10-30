"""Same-laser runtime parameter transactions for a running card.

The agent reads a running card's injectable schema and queues one versioned
patch through the dashboard (POST jobs/J/runtime-parameters). Free fields are
applied by the existing worker at its next safe boundary. Safety-class fields
(for example the midpoint traversal span, steps and limits in ``window_guard``)
need an approval id from ``safety_change_approve``; the dashboard verifies it.
"""
import json
import re

from ldcon.agents.agent_cards import _api
from ldcon.core.agent_authority import describe_changes, partition, FREE, SAFETY, FORBIDDEN
from ldcon.agents.failure_policy import sanitize


def _job(box, job_id):
    if not re.fullmatch('[0-9a-f]{32}', job_id):raise ValueError('Expected a job ID, not a path')
    job = _api(box, 'jobs/' + job_id)
    if job.get('status') == 'rejected':return job
    if job.get('set_id') != box.meta['set_id'] or job.get('laser', {}).get('id') != box._laser_id:
        raise ValueError('Job belongs to another laser/deck')
    return job


def call(box, name, args):
    job = _job(box, args['job_id'])
    if job.get('status') == 'rejected':return job
    live = _api(box, 'jobs/' + args['job_id'] + '/runtime-parameters')
    if live.get('status') == 'rejected':return live
    if name == 'laser_runtime_read':
        laser_live = _api(box, 'jobs/' + args['job_id'] + '/laser-live')
        return dict(job_id=args['job_id'], job_status=job.get('status'), runtime=sanitize(live), laser_live=sanitize(laser_live),
                    note='Patch only fields listed in schema; while_busy fields may change during a search. '
                         'Safety-class window_guard fields need safety_change_propose/approve first.')
    if job.get('status') != 'running':
        return dict(status='rejected', reason='Runtime changes require a running card')
    patch = json.loads(args['patch_json'], parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Finite values required')))
    if not isinstance(patch, dict) or not patch:raise ValueError('Patch must be a nonempty JSON mapping')
    if args['expected_revision'] != live.get('revision'):
        return dict(status='rejected', reason='Runtime revision changed; read again before patching')
    current = live.get('parameters', {})
    rows = describe_changes('runtime', {k: current.get(k) for k in patch}, patch)
    parts = partition(rows)
    summary = dict(job_id=args['job_id'], card_id=live.get('card_id'), free=[r['path'] for r in parts[FREE]],
                   safety=[r['path'] for r in parts[SAFETY]], forbidden=[r['path'] for r in parts[FORBIDDEN]], changes=rows)
    if not rows:return dict(status='unchanged', revision=live.get('revision'), **summary)
    if parts[FORBIDDEN]:
        return dict(status='forbidden', applied=False, reason='Maintainer-only runtime fields cannot be changed by the agent', **summary)
    approval_id = args.get('approval_id') or None
    if parts[SAFETY] and not approval_id:
        return dict(status='requires_human_approval', applied=False,
                    proposal=dict(scope=dict(kind='runtime', job_id=args['job_id'], card_id=live.get('card_id')),
                                  changes=[dict(path=r['path'], old=r['old'], new=r['new']) for r in parts[SAFETY]]),
                    reason='Safety ranges changed. Call safety_change_propose with this exact proposal, obtain the human\'s '
                           'direct approval in this conversation, then retry with approval_id. Nothing queued.', **summary)
    if args.get('dry_run', '').lower() in ('1', 'true', 'yes'):
        return dict(status='dry_run', applied=False, **summary)
    def queue():
        payload = dict(request_id=args['request_id'], job_id=args['job_id'], card_id=live.get('card_id'),
                       expected_revision=args['expected_revision'], patch=patch)
        if approval_id:payload['approval_id'] = approval_id
        value = _api(box, 'jobs/' + args['job_id'] + '/runtime-parameters', 'POST', payload)
        if value.get('status') == 'rejected':return value
        return dict(status='queued', request_id=args['request_id'], expires_unix_s=value.get('expires_unix_s'),
                    authority=value.get('authority'), **summary,
                    note='Queued only. The worker applies it at its next safe boundary and records '
                         'runtime-parameter-result; read laser_runtime_read until the revision advances. '
                         'For window_guard, re-read window_center_status: guard_revision changes.')
    return box.once(name, args, queue)
