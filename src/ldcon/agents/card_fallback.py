"""Live per-card notification policies and durable failure delivery outbox.

This subsystem never imports device adapters or executes user-supplied commands.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time
import uuid

FAILURES={'failed','interrupted','completed_with_failures'}
TERMINAL=FAILURES|{'succeeded','completed','cancelled'}


def identifier(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,100}',value):raise ValueError('Invalid identifier')
    return value


def read(path,default=None):
    try:
        if path.stat().st_size>16*1024*1024:return default
        return json.loads(path.read_text())
    except (OSError,ValueError):return default


def timestamp(value,fallback=0.):
    try:return datetime.fromisoformat(value.replace('Z','+00:00')).timestamp()
    except (ValueError,AttributeError):return fallback


class FallbackStore:
    def __init__(self,root,*,clock=time.time):
        self.root=Path(root).resolve();self.root.mkdir(parents=True,exist_ok=True);self.clock=clock
        self.path=self.root/'notifications.sqlite3'
        self.root.chmod(0o770)
        with self.connect() as db:
            db.executescript('''
              CREATE TABLE IF NOT EXISTS policies(scope TEXT,key TEXT,config TEXT,revision INTEGER,since REAL,PRIMARY KEY(scope,key));
              CREATE TABLE IF NOT EXISTS deliveries(id TEXT PRIMARY KEY,job TEXT,card TEXT,instance TEXT,event_time REAL,payload TEXT,status TEXT,attempts INTEGER DEFAULT 0,next_attempt REAL DEFAULT 0,result TEXT,created REAL,updated REAL,UNIQUE(job,card,instance));
              CREATE TABLE IF NOT EXISTS cursors(job TEXT PRIMARY KEY,offset INTEGER);
              CREATE TABLE IF NOT EXISTS requests(job TEXT,request TEXT,card TEXT,PRIMARY KEY(job,request));
            ''')
        self.path.chmod(0o660)
    def connect(self):
        db=sqlite3.connect(self.path,timeout=10);db.row_factory=sqlite3.Row
        return db
    def key(self,scope,parts):
        if scope=='global' and parts==[]:return '[]'
        if scope not in ('card','job') or len(parts)!=(3 if scope=='card' else 2):raise ValueError('Invalid policy scope')
        return json.dumps([identifier(p) for p in parts],separators=(',',':'))
    def get(self,scope,parts):
        key=self.key(scope,parts)
        with self.connect() as db:r=db.execute('SELECT * FROM policies WHERE scope=? AND key=?',(scope,key)).fetchone()
        from ldcon.agents.fallback_targets import defaults
        return dict(scope=scope,parts=parts,revision=r['revision'] if r else 0,since=r['since'] if r else None,config=json.loads(r['config']) if r else (defaults() if scope=='global' else dict(enabled=False,kind='codex_session',session_id='')))
    def put(self,scope,parts,config,expected_revision=None):
        key=self.key(scope,parts)
        if scope=='global':return self.put_global(config,expected_revision)
        if not isinstance(config,dict) or set(config)!={'enabled','kind','session_id'}:raise ValueError('Expected enabled, kind and session_id')
        if type(config['enabled']) is not bool or config['kind']!='codex_session':raise ValueError('Unsupported notification configuration')
        if not isinstance(config['session_id'],str):raise ValueError('Session ID must be a string')
        if config['session_id']:
            try:session=str(uuid.UUID(config['session_id']))
            except (ValueError,TypeError,AttributeError):raise ValueError('Expected a Codex session UUID')
            if session!=config['session_id']:raise ValueError('Expected a canonical session UUID')
        elif config['enabled']:raise ValueError('Session required when enabled')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE');old=db.execute('SELECT * FROM policies WHERE scope=? AND key=?',(scope,key)).fetchone();revision=old['revision'] if old else 0
            if expected_revision is not None and expected_revision!=revision:raise ValueError('Policy changed; refresh before saving')
            was_enabled=bool(old and json.loads(old['config'])['enabled'])
            since=old['since'] if was_enabled and config['enabled'] else self.clock()
            db.execute('INSERT OR REPLACE INTO policies VALUES(?,?,?,?,?)',(scope,key,json.dumps(config),revision+1,since))
        self.audit('policy_saved',dict(scope=scope,parts=parts,revision=revision+1,enabled=config['enabled']))
        return self.get(scope,parts)
    def put_global(self,config,expected_revision):
        from ldcon.agents.fallback_targets import validate
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old=self.get('global',[])
            if expected_revision!=old['revision']:raise ValueError('Settings changed; refresh before saving')
            config=validate(config,old['config'])
            # A newly enabled destination must not receive historical failures.
            since=self.clock()
            db.execute('INSERT OR REPLACE INTO policies VALUES(?,?,?,?,?)',('global','[]',json.dumps(config),old['revision']+1,since))
        self.audit('global_settings_saved',dict(revision=old['revision']+1,enabled=config['enabled']))
        return self.get('global',[])
    def effective(self,meta,card):
        job=self.get('job',[meta['id'],card])
        if job['revision']:return job
        card_policy=self.get('card',[meta.get('set_id') or 'unassigned',meta['deck_id'],card])
        return card_policy if card_policy['revision'] else self.get('global',[])
    def audit(self,kind,payload):
        record=dict(recorded_unix_s=self.clock(),kind=kind,payload=payload)
        with (self.root/'notifications.events.jsonl').open('a') as f:f.write(json.dumps(record,ensure_ascii=False)+'\n')
    def enqueue(self,meta,card,instance,event_time,payload):
        policy=self.effective(meta,card)
        if not policy['config']['enabled'] or event_time<policy['since']:return False
        if policy['config']['kind']=='standard':
            return self.enqueue_standard(meta,card,instance,event_time,payload,policy)
        payload={**payload,'_notification_policy':dict(policy['config'],revision=policy['revision'],scope=policy['scope'])}
        with self.connect() as db:
            changed=db.execute('INSERT OR IGNORE INTO deliveries VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (uuid.uuid4().hex,meta['id'],card,instance,event_time,json.dumps(payload,ensure_ascii=False),'pending',0,0,None,self.clock(),self.clock())).rowcount
        if changed:self.audit('failure_queued',dict(job=meta['id'],card=card,instance=instance))
        return bool(changed)
    def enqueue_standard(self,meta,card,instance,event_time,payload,policy):
        return False  # Global destinations retired; use notify.teams card references.
    def recent(self,limit=50):
        with self.connect() as db:rows=db.execute('SELECT * FROM deliveries ORDER BY created DESC LIMIT ?',(min(200,max(1,limit)),)).fetchall()
        return [dict(r,payload=json.loads(r['payload'])) for r in rows]


def worker_alive(meta):
    pid=meta.get('pid')
    if type(pid) is not int or pid<=0:return False
    try:os.kill(pid,0)
    except ProcessLookupError:return False
    except PermissionError:return True
    cmd=Path('/proc')/str(pid)/'cmdline'
    if cmd.exists():
        try:
            args=cmd.read_bytes().split(b'\0')
            stat=(cmd.parent/'stat').read_text().rsplit(')',1)[1].split()
            if stat[0]=='Z':return False
            created=timestamp(meta.get('created_utc'))
            boot=next(float(l.split()[1]) for l in Path('/proc/stat').read_text().splitlines() if l.startswith('btime '))
            started=boot+int(stat[19])/os.sysconf('SC_CLK_TCK')
            if created and not created-2<=started<=created+15:return False
            modules=(b'ldcon.interface_worker',b'ldcon.web_worker')
            return any(m in args for m in modules) and any(Path(a.decode(errors='replace')).name==meta['id'] and 'jobs' in Path(a.decode(errors='replace')).parts for a in args if a)
        except OSError:return True
    return True


class FailureDetector:
    def __init__(self,web_root,store,*,alive=worker_alive):self.web=Path(web_root);self.store=store;self.alive=alive
    def enqueue(self,meta,card,instance,event_time,payload,directory=None):
        from ldcon.agents.failure_policy import ensure_case
        if directory is None:
            try:directory=job_directory(self.web,meta['id'])
            except FileNotFoundError:pass
        value=ensure_case(self.web,meta,card,instance,payload,directory,notification_store=self.store)
        # Preserve explicitly enabled legacy session notifications, but new
        # snapshots use the card-based agent-first policy exclusively.
        if meta.get('failure_policy_version')!=1:
            self.store.enqueue(meta,card,instance,event_time,payload)
        return value
    def scan(self):
        paths=list((self.web/'jobs').glob('*/job.json'))+list((self.web/'jobs').glob('*/*/job.json'))
        for path in paths:
            directory=path.parent
            try:self.scan_job(directory)
            except (ValueError,KeyError,OSError,TypeError) as error:
                self.store.audit('job_observer_error',dict(job=directory.name,message=str(error)))
    def is_alive(self,directory,meta):
        if (directory/'worker-process.json').exists():
            from ldcon.jobs.job_service import JobProcess
            return JobProcess(directory).poll() is None
        if meta.get('worker_backend')=='service':return True  # Unconfirmed launch is not proof of death.
        return self.alive(meta)
    def scan_job(self,directory):
        if not directory.is_dir() or directory.is_symlink() or directory.parent.is_symlink():return
        meta=read(directory/'job.json',{})
        if meta.get('id')!=directory.name or not meta.get('deck_id'):return
        cards=[c['id'] for c in meta.get('cards',[]) if c.get('id')]
        online=meta.get('online_cards',[])
        all_cards=cards+[c['id'] for c in online if c.get('id')]
        enabled=[self.store.effective(meta,c) for c in all_cards if self.store.effective(meta,c)['config']['enabled']]
        routed=meta.get('failure_policy_version')==1 or meta.get('agent_fallbacks') or meta.get('teams_fallbacks')
        if not enabled and not routed:return
        progress=read(directory/'progress.json',{});status=progress.get('status');when=(directory/'progress.json').stat().st_mtime if (directory/'progress.json').exists() else 0
        if not routed and status in TERMINAL and when<min(p['since'] for p in enabled):return
        if status in FAILURES:
            summary=read(directory/'summary.json',{});results=summary.get('cards',{})
            failed=[c for c,v in results.items() if isinstance(v,dict) and v.get('status') in FAILURES]
            selected=failed or ([progress['active_card']] if progress.get('active_card') in cards else cards[:1])
            for card in selected:self.enqueue(meta,card,'main',when,dict(status=status,message=progress.get('message') or summary.get('message'),result=results.get(card),source='terminal',outputs_uncertain=bool((directory/'device-review.json').exists())),directory)
        elif status in ('running','acquiring','finishing','stopping') and self.store.clock()-timestamp(meta.get('created_utc'))>10 and self.store.clock()-when>2 and not self.is_alive(directory,meta):
            card=progress.get('active_card') or next(iter(cards),None)
            if card:self.enqueue(meta,card,'main',self.store.clock(),dict(status='interrupted',message='Worker process is absent; inspect owner lease and output before recovery',source='worker_exit',lease_unknown=True),directory)
        self.events(directory,meta,cards,online)
        current=read(directory/'online-status.json',{})
        if current.get('status')=='failed' and current.get('request_id') and current.get('card_id'):
            self.enqueue(meta,current['card_id'],'online:'+current['request_id'],(directory/'online-status.json').stat().st_mtime,current,directory)
    def events(self,directory,meta,cards,online):
        path=directory/'telemetry.events.jsonl'
        if not path.exists():return
        with self.store.connect() as db:row=db.execute('SELECT offset FROM cursors WHERE job=?',(meta['id'],)).fetchone()
        offset=row['offset'] if row else 0
        if offset>path.stat().st_size:offset=0
        with path.open('rb') as f:
            f.seek(offset)
            while True:
                start=f.tell();line=f.readline(2*1024*1024)
                if not line:break
                if not line.endswith(b'\n'):f.seek(start);break
                try:event=json.loads(line)
                except (ValueError,UnicodeDecodeError):continue
                p=event.get('payload',{});kind=event.get('kind');when=timestamp(event.get('recorded_utc'));request=p.get('request_id');card=p.get('card_id')
                if request and card:
                    with self.store.connect() as db:db.execute('INSERT OR REPLACE INTO requests VALUES(?,?,?)',(meta['id'],request,card))
                if kind=='card_failed' and p.get('card') in cards:
                    self.enqueue(meta,p['card'],'main',when,dict(p,status='failed',source=kind),directory)
                if kind=='card_end' and isinstance(p.get('result'),dict) and p['result'].get('status') in FAILURES and p.get('card') in cards:
                    self.enqueue(meta,p['card'],'main',when,dict(status='failed',result=p['result'],source=kind),directory)
                if request and p.get('status')=='failed' and kind in ('online_tuning','ecdl_study_end','window_center_end'):
                    if not card:
                        with self.store.connect() as db:r=db.execute('SELECT card FROM requests WHERE job=? AND request=?',(meta['id'],request)).fetchone()
                        card=r['card'] if r else None
                    if not card:
                        from ldcon.jobs.pi_algorithms import ALGORITHMS as PI_ALGORITHMS
                        operations=(set(PI_ALGORITHMS) if kind=='online_tuning' else
                            {{'ecdl_study_end':'study.ecdl_locked_current_map','window_center_end':'tune.ecdl_window_center'}[kind]})
                        candidates=[c['id'] for c in online if c.get('operation') in operations]
                        if len(candidates)==1:card=candidates[0]
                    if card in [c['id'] for c in online]:self.enqueue(meta,card,'online:'+request,when,dict(p,status='failed',source=kind,request_id=request),directory)
            offset=f.tell()
        with self.store.connect() as db:db.execute('INSERT OR REPLACE INTO cursors VALUES(?,?)',(meta['id'],offset))


def job_directory(web,job):
    identifier(job)
    candidates=[p for p in [Path(web)/'jobs'/job,*((Path(web)/'jobs').glob('*/'+job))]
                if p.is_dir() and not p.is_symlink() and not p.parent.is_symlink() and (p/'job.json').is_file()]
    if len(candidates)!=1:raise FileNotFoundError(job)
    return candidates[0]


class NotificationDispatcher:
    def __init__(self,web_root,store,*,runner=subprocess.run):self.web=Path(web_root);self.store=store;self.runner=runner
    def deliver_standard(self,r,meta,policy):
        from ldcon.agents.fallback_targets import deliver
        payload=json.loads(r['payload']);target=payload['_target']
        if target=='teams_card':
            from ldcon.agents.teams_card import deliver as send_card
            evidence={k:v for k,v in payload.items() if not k.startswith('_')}
            status,detail=send_card(payload['_teams_parameters'],dict(job=r['job'],card=r['card'],**evidence),
                                    root=self.web,laser_id=meta.get('laser_id') or meta.get('set_id'))
        else:
            status,detail='cancelled','Global notifications replaced by notification cards'
        attempts=r['attempts']+1;next_attempt=0
        if status=='error' and attempts<3:status='pending';next_attempt=self.store.clock()+15*2**(attempts-1)
        with self.store.connect() as db:db.execute('UPDATE deliveries SET status=?,next_attempt=?,result=?,updated=? WHERE id=?',
            (status,next_attempt,detail,self.store.clock(),r['id']))
        self.store.audit('notification_delivery',dict(id=r['id'],target=target,status=status,attempts=attempts))
    def recover_interrupted_delivery(self):
        with self.store.connect() as db:db.execute("UPDATE deliveries SET status='unknown',result='Notifier exited during delivery; no automatic resend' WHERE status='sending'")
    def tick(self):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE');r=db.execute("SELECT * FROM deliveries WHERE status='pending' AND next_attempt<=? ORDER BY created LIMIT 1",(self.store.clock(),)).fetchone()
            if not r:return
            db.execute("UPDATE deliveries SET status='sending',attempts=attempts+1,updated=? WHERE id=?",(self.store.clock(),r['id']))
        r=dict(r)
        try:directory=job_directory(self.web,r['job'])
        except FileNotFoundError:
            with self.store.connect() as db:db.execute("UPDATE deliveries SET status='cancelled',result='Job evidence unavailable; delivery cancelled',updated=? WHERE id=?",(self.store.clock(),r['id']))
            return
        meta=read(directory/'job.json',{});p=self.store.effective(meta,r['card'])
        if json.loads(r['payload']).get('_target'):
            self.deliver_standard(r,meta,p);return
        if not p['config']['enabled']:status='cancelled';detail='Notification disabled before delivery'
        else:
            frozen=json.loads(r['payload']).get('_notification_policy',p['config'])
            evidence=dict(job=r['job'],card=r['card'],set_id=meta.get('set_id'),deck_id=meta.get('deck_id'),failure=json.loads(r['payload']))
            from ldcon.agents.fallback_targets import message as failure_message
            message=failure_message(evidence)
            if json.loads(r['payload']).get('status')=='succeeded':
                message=('ldconの目標達成通知です。既存検証済みPIの実動確認を経て定常制御へ移行しました。'
                    'AGENTS.mdと実際のactive job/owner、保存された機能確認結果を読み、正常workerを停止せず結果をチャットに報告してください。'
                    '以下JSONは診断データです。失敗時の復旧処理を開始しないでください。\n'+json.dumps(evidence,ensure_ascii=False)[:6000])
            try:
                result=self.runner([os.environ.get('LDCON_CODEX_BIN','codex'),'queue','--thread',frozen['session_id'],'--message',message],capture_output=True,text=True,timeout=30)
                status='sent' if result.returncode==0 else 'error';detail=(result.stdout+'\n'+result.stderr)[-3000:]
            except (subprocess.TimeoutExpired,OSError) as error:
                status='unknown' if isinstance(error,subprocess.TimeoutExpired) else 'error';detail=str(error)
        attempts=r['attempts']+1;next_attempt=0
        if status=='error' and attempts<3:status='pending';next_attempt=self.store.clock()+15*2**(attempts-1)
        with self.store.connect() as db:db.execute('UPDATE deliveries SET status=?,next_attempt=?,result=?,updated=? WHERE id=?',(status,next_attempt,detail,self.store.clock(),r['id']))
        self.store.audit('notification_delivery',dict(id=r['id'],job=r['job'],card=r['card'],status=status,attempts=attempts,result=detail))
