"""Bounded historical evidence and proposed knowledge, never a physical grant."""
from ldcon.core.state_layout import knowledge_root

import json
import math
from pathlib import Path
import time

from experiment_core.records import atomic_json, digest, safe_id
from ldcon.agents.agent_log_context import collect_log_context


def history(root, laser_id, since, until, *, windows=4):
    if (type(windows) is not int or not 1 <= windows <= 12 or
            any(type(x) not in (int, float) or not math.isfinite(x) for x in (since, until))
            or not 0 <= since < until or until-since > 31536000):
        raise ValueError('Invalid bounded historical window')
    result = []
    # Uniform temporal strata deliberately include ordinary operation, rather
    # than selecting only failures or a parent's interesting anecdotes.
    for i in range(windows):
        start = since+(until-since)*i/windows
        end = since+(until-since)*(i+1)/windows
        summary = collect_log_context(root, laser_id, since=start, until=end)
        result.append(dict(since_unix_s=start, until_unix_s=end, context=summary))
    return dict(schema_version=1, laser_id=laser_id, selection='uniform_time_strata',
                windows=result, complete=False,
                limitations=['Bounded discovery, not all history; use laser_log_read to query each recorded interval.',
                             'Snapshots are latest records, not reconstructed historical state.',
                             'Include normal and failure periods; comparisons require matching revisions and source cadence.'])


def knowledge_directory(root, laser_id):
    safe_id(laser_id)
    return knowledge_root(root, laser_id)


def propose(root, laser_id, value, *, task_id):
    required = {'hypothesis', 'evidence', 'counterevidence', 'applicability', 'validation_plan'}
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError('Knowledge requires hypothesis/evidence/counterevidence/applicability/validation_plan')
    if len(json.dumps(value, allow_nan=False).encode()) > 16000:
        raise ValueError('Knowledge record exceeds 16 KiB')
    if any(not isinstance(value[k], (str, list, dict)) for k in required):
        raise ValueError('Invalid knowledge content')
    path = knowledge_directory(root, laser_id)
    path.mkdir(parents=True, exist_ok=True)
    row = dict(schema_version=1, laser_id=laser_id, task_id=task_id,
               status='proposed', content=value, created_unix_s=time.time(),
               authority='none; hypothesis requires independent validation')
    identifier = digest([laser_id, task_id, value])
    file = path/(identifier+'.json')
    if not file.exists():
        atomic_json(file, row)
    return dict(id=identifier, status='proposed', path=str(file))


def knowledge_summary(root, laser_id, *, limit=5):
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError('Invalid knowledge page size')
    files = sorted(knowledge_directory(root, laser_id).glob('*.json'),
                   key=lambda p:p.stat().st_mtime, reverse=True)
    rows = []
    for path in files[:limit]:
        if path.is_symlink() or path.stat().st_size > 32768:
            continue
        value = json.loads(path.read_text())
        if value.get('laser_id') != laser_id:
            continue
        rows.append(dict(id=path.stem, status=value['status'], task_id=value['task_id'],
                         hypothesis=str(value['content']['hypothesis'])[:1200],
                         applicability=value['content']['applicability'], path=str(path)))
    return dict(records=rows, truncated=len(files)>limit, authority='none')


def call(box, name, args):
    if name == 'laser_history_read':
        return history(box.root, box._laser_id, args['since_unix_s'], args['until_unix_s'],
                       windows=args.get('windows', 4))
    if name == 'laser_knowledge_propose':
        if box.context.get('task_depth') == 1:
            from ldcon.agents.agent_cards import _api
            return _api(box, 'agent-history/propose', 'POST',
                        dict(job_id=box.meta['id'], record=json.loads(args['record_json'])))
        return propose(box.root, box._laser_id, json.loads(args['record_json']), task_id=box.meta['id'])
    if args.get('record_id'):
        identifier = args['record_id']
        if len(identifier) != 64 or any(c not in '0123456789abcdef' for c in identifier):
            raise ValueError('Invalid knowledge record ID')
        path = knowledge_directory(box.root, box._laser_id)/(identifier+'.json')
        if path.is_symlink() or path.stat().st_size > 32768:
            raise ValueError('Invalid knowledge record')
        return json.loads(path.read_text())
    return knowledge_summary(box.root, box._laser_id, limit=args.get('limit', 5))


def install_routes(app, prefix, body):
    from fastapi import Request
    @app.post(prefix+'/api/agent-history/propose')
    async def submit(request: Request):
        from ldcon.core.records import read_json
        value = await body(request)
        if set(value) != {'job_id','record'}:
            raise ValueError('Invalid knowledge proposal')
        job = app.state.runtime.get_job(safe_id(value['job_id']))
        directory = app.state.runtime.directory(value['job_id'])
        context = read_json(directory/'agent-tools-context.json', {})
        if job.get('status') != 'running' or context.get('task_depth') != 1:
            raise ValueError('Knowledge task is not active')
        from ldcon.web.agent_task_api import task_permission
        task_permission(app.state.runtime,job['id'],job['laser']['id'],'laser_knowledge_propose')
        return propose(app.state.runtime.root, job['laser']['id'], value['record'], task_id=job['id'])
