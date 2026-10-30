"""Reusable Teams notification card. Secret URLs never enter deck/job snapshots."""
import json
import math
import os
import re
import uuid
from pathlib import Path


def parameters(card,overrides):
    p={**dict(webhook_env='',message='',sample_interval_s=1.),**card['parameters'],**overrides}
    if set(p)!={'webhook_env','message','sample_interval_s'}:raise ValueError('Unknown Teams card parameter; use a secret environment reference, not a URL')
    if not isinstance(p['webhook_env'],str) or (p['webhook_env'] and not re.fullmatch(r'LDCON_TEAMS_[A-Z0-9_]{1,80}',p['webhook_env'])):raise ValueError('Expected LDCON_TEAMS_* secret environment name')
    if not isinstance(p['message'],str) or len(p['message'])>6000:raise ValueError('Teams message exceeds 6000 characters')
    if type(p['sample_interval_s']) not in (int,float) or not math.isfinite(p['sample_interval_s']) or not .1<=p['sample_interval_s']<=60:raise ValueError('Invalid notification interval')
    return p


def enqueue(store,meta,card,instance,p,payload):
    from ldcon.agents.notification_policy import prepare,resolve_policy,decide,defaults
    p=parameters({'parameters':p},{})
    if not p['webhook_env']:raise ValueError('Teams card secret reference is unset')
    now=store.clock()
    # Frozen old rows are never reformatted or put back into pending by a
    # notification policy/formatter upgrade.
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        old=db.execute('SELECT id FROM deliveries WHERE job=? AND card=? AND instance=?',
                       (meta['id'],card,instance+':teams_card')).fetchone()
        if old:return old['id']
        current=dict(meta)
        if not current.get('laser_id') and not current.get('set_id'):
            from ldcon.agents.card_fallback import job_directory,read
            try:current={**read(job_directory(store.root.parent,meta['id'])/'job.json',{}),**meta}
            except FileNotFoundError:pass
        summary=prepare(current,card,instance,p,payload,now=now)
        try:policy=resolve_policy(store.root.parent,summary['laser_id'],summary['task_id'],now)
        except (OSError,ValueError):
            # A malformed notification preference cannot hide an urgent event.
            policy=dict(revision=None,effective=defaults()['global_policy'],source='invalid_policy_fallback')
        summary=prepare(current,card,instance,p,payload,now=now,detail=policy['effective']['preset'])
        status,reason=decide(db,summary,policy,now)
        summary.update(policy=policy,decision=status,reason=reason)
        frozen={**p,'message':summary['message']}
        parameters({'parameters':frozen},{})
        body={**payload,'_target':'teams_card','_teams_parameters':frozen,
              '_original_teams_parameters':p,'notification_summary':summary}
        delivery=uuid.uuid4().hex
        db.execute('INSERT INTO deliveries VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
            (delivery,meta['id'],card,instance+':teams_card',now,json.dumps(body,ensure_ascii=False),
             status,0,0,reason,now,now))
    return delivery


def receipt(store,delivery_id):
    with store.connect() as db:
        row=db.execute('SELECT status,result FROM deliveries WHERE id=?',(delivery_id,)).fetchone()
    status=row['status'] if row else 'unknown'
    return dict(status='queued' if status=='pending' else status,delivery_id=delivery_id,
                message=row['result'] if row else 'Notification outcome unknown; no replay')


def submit(directory,p):
    from ldcon.agents.card_fallback import FallbackStore,read
    directory=Path(directory);meta=read(directory/'job.json');invocation=read(directory/'invocation.json')
    root=directory.parent.parent if directory.parent.name=='jobs' else directory.parent.parent.parent
    delivery_id=enqueue(FallbackStore(root/'notifications'),meta,invocation['card_id'],'manual',p,
                        dict(message=p['message'],source='notification_card'))
    return dict(status='succeeded',message='Notification queued; HTTP delivery is not yet confirmed',
                delivery_id=delivery_id,output_writes=0)


def deliver(p,evidence,*,root=None,laser_id=None):
    from ldcon.agents.fallback_targets import defaults,validate,send_teams
    p=parameters({'parameters':p},{})
    from ldcon.agents.teams_destinations import resolve
    try:
        state,url=resolve(root,laser_id or evidence.get('notification_summary',{}).get('laser_id'),p['webhook_env'])
    except (ValueError,OSError):
        return 'error','Teams destination settings are unavailable or invalid'
    if state=='disabled':return 'cancelled','Teams notifications disabled for this laser'
    if not url:return 'error','Teams destination is not configured'
    config=defaults();config['teams'].update(enabled=True,webhook_url=url)
    try:validate(config)
    except ValueError:return 'error','Teams secret URL is invalid'
    text=p['message']
    if evidence.get('notification_summary',{}).get('schema_version')==1:
        pass  # New events already contain their frozen short body.
    elif evidence.get('source')!='notification_card':
        text+='\nLDCon card failure (diagnostic data):\n'+json.dumps(evidence,ensure_ascii=False)[:6000]
    return send_teams(url,text)


def history(root,job):
    import sqlite3
    path=Path(root)/'notifications'/'notifications.sqlite3'
    if not path.is_file():return []
    with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
        db.row_factory=sqlite3.Row
        return [dict(r) for r in db.execute('SELECT id,card,status,attempts,result FROM deliveries WHERE job=? ORDER BY created DESC LIMIT 20',(job,))]
