"""Device-free resident agent cards and a durable, idempotent request mailbox."""
from ldcon.core.state_layout import config_file

import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

from ldcon.core.interface_cards import CardCancelled

OPERATIONS=('agent.resident','agent.request')


def parameters(card,overrides):
    defaults=(dict(provider='claude',mode='create_or_connect',session_id='',workspace='',sample_interval_s=1.,request_timeout_s=300.,remote_control=None,execution_profile='',model='',effort='',role='gateway')
              if card['operation']=='agent.resident' else dict(agent_card_id='',prompt='',sample_interval_s=1.))
    p={**defaults,**card['parameters'],**overrides}
    if set(p)!=set(defaults):raise ValueError('Unknown agent parameter')
    if type(p['sample_interval_s']) not in (int,float) or not .1<=p['sample_interval_s']<=10:raise ValueError('Invalid agent poll interval')
    if card['operation']=='agent.resident':
        if p['role'] not in ('gateway','operator'):raise ValueError('Unknown agent role')
        if p['remote_control'] is not None and type(p['remote_control']) is not bool:raise ValueError('Remote Control must be boolean or null (provider default)')
        if p['remote_control'] and p['provider'] not in ('claude','codex') and not p['execution_profile']:raise ValueError('Remote Control requires Claude or Codex')
        from ldcon.agents.agent_providers import PROVIDERS, session_id
        from experiment_core.agent_contracts import text
        for key in ('execution_profile','model','effort'):text(p[key], key, 512, empty=True)
        if p['provider'] not in PROVIDERS or p['mode'] not in ('create_or_connect','reconnect'):raise ValueError('Invalid agent provider/mode')
        if not isinstance(p['workspace'],str) or (p['workspace'] and not Path(p['workspace']).is_absolute()):raise ValueError('Agent workspace must be absolute')
        session_id(p['provider'],p['session_id'])
        if type(p['request_timeout_s']) not in (int,float) or not 10<=p['request_timeout_s']<=3600:raise ValueError('Invalid request deadline')
    else:
        from ldcon.core.interface_schema import identifier
        if p['agent_card_id']:identifier(p['agent_card_id'])
        if not isinstance(p['prompt'],str) or len(p['prompt'])>16000:raise ValueError('Invalid agent prompt')
    return p


class AgentStore:
    def __init__(self,root):
        self.root=Path(root)/'agents';self.root.mkdir(parents=True,exist_ok=True)
        self.root.chmod(0o770);self.path=self.root/'agents.sqlite3'
        with self.connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS sessions(key TEXT PRIMARY KEY,provider TEXT,session TEXT,status TEXT,job TEXT,updated REAL);
            CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY,key TEXT,payload TEXT,status TEXT,result TEXT,created REAL,updated REAL);
            CREATE TABLE IF NOT EXISTS consultations(id TEXT PRIMARY KEY,key TEXT,case_id TEXT,request_id TEXT,
                question TEXT,reason TEXT,status TEXT,answer TEXT,created REAL,updated REAL);''')
            db.execute('BEGIN IMMEDIATE')
            columns={r[1] for r in db.execute('PRAGMA table_info(sessions)')}
            for name in ('laser_id','template_revision','bridge_id','workspace'):
                if name not in columns:db.execute('ALTER TABLE sessions ADD COLUMN '+name+' TEXT')
            db.execute('CREATE TABLE IF NOT EXISTS provider_sessions(key TEXT,provider TEXT,session TEXT,status TEXT,job TEXT,updated REAL,laser_id TEXT,template_revision TEXT,bridge_id TEXT,workspace TEXT,PRIMARY KEY(key,provider))')
            db.execute('INSERT OR IGNORE INTO provider_sessions SELECT key,provider,session,status,job,updated,laser_id,template_revision,bridge_id,workspace FROM sessions')
            columns={r[1] for r in db.execute('PRAGMA table_info(consultations)')}
            for name,definition in (('notification_status',"TEXT DEFAULT 'not_requested'"),('delivery_id','TEXT')):
                if name not in columns:db.execute('ALTER TABLE consultations ADD COLUMN '+name+' '+definition)
        self.path.chmod(0o660)
        self.session_context={}
    def connect(self):
        db=sqlite3.connect(self.path,timeout=10);db.row_factory=sqlite3.Row;return db
    @staticmethod
    def key(meta,card):return json.dumps([meta['set_id'],meta['deck_id'],card],separators=(',',':'))
    def session(self,key,provider=None):
        with self.connect() as db:
            r=(db.execute('SELECT * FROM provider_sessions WHERE key=? AND provider=?',(key,provider)).fetchone()
               if provider else db.execute('SELECT * FROM sessions WHERE key=?',(key,)).fetchone())
        return dict(r) if r else None
    def save(self,key,provider,session,status,job):
        row=(key,provider,session,status,job,time.time(),self.session_context.get('laser_id'),
             self.session_context.get('template_revision'),self.session_context.get('bridge_id'),self.session_context.get('workspace'))
        with self.connect() as db:
            for table in ('sessions','provider_sessions'):
                db.execute('INSERT OR REPLACE INTO '+table+'(key,provider,session,status,job,updated,laser_id,template_revision,bridge_id,workspace) VALUES(?,?,?,?,?,?,?,?,?,?)',row)
    def enqueue(self,key,request_id,payload):
        encoded=json.dumps(payload,ensure_ascii=False)
        if len(encoded)>32000:raise ValueError('Agent request too large')
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO requests VALUES(?,?,?,?,?,?,?)',(request_id,key,encoded,'queued',None,time.time(),time.time()))
            old=db.execute('SELECT * FROM requests WHERE id=?',(request_id,)).fetchone()
            if old['key']!=key or old['payload']!=encoded:raise ValueError('Agent request ID conflict')
        return request_id
    def recover(self,key):
        with self.connect() as db:db.execute("UPDATE requests SET status='unknown',result='Resident disconnected during request; no automatic replay',updated=? WHERE key=? AND status='running'",(time.time(),key))
    def claim(self,key):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute("SELECT * FROM requests WHERE key=? AND status='queued' ORDER BY created LIMIT 1",(key,)).fetchone()
            if row:db.execute("UPDATE requests SET status='running',updated=? WHERE id=?",(time.time(),row['id']))
        return dict(row) if row else None
    def finish(self,request_id,status,result):
        with self.connect() as db:db.execute('UPDATE requests SET status=?,result=?,updated=? WHERE id=?',(status,result[:64000],time.time(),request_id))
    def defer(self,request_id):
        # Only the typed pre-send refusal may return a claim to the queue.
        # Unknown post-send outcomes must never reach this path.
        with self.connect() as db:
            db.execute("UPDATE requests SET status='queued',result=NULL,updated=? WHERE id=? AND status='running'",(time.time(),request_id))

    def requests(self,key):
        with self.connect() as db:return [dict(r) for r in db.execute('SELECT * FROM requests WHERE key=? ORDER BY created DESC LIMIT 20',(key,))]
    def request(self,key,request_id):
        """Look up a scoped request even after it leaves the recent UI page."""
        with self.connect() as db:
            row=db.execute('SELECT * FROM requests WHERE key=? AND id=?',(key,request_id)).fetchone()
        return dict(row) if row else None
    def blocking_request(self,key):
        with self.connect() as db:
            row=db.execute("SELECT * FROM requests WHERE key=? AND status IN ('queued','running','unknown') ORDER BY created LIMIT 1",(key,)).fetchone()
        return dict(row) if row else None
    def enqueue_when_ready(self,key,request_id,payload):
        """Atomically reserve an idle, recently connected resident for a review.

        Foreground/fallback requests keep their ordinary durable queue behavior.
        Periodic requests may not pile up behind them or replay unknown turns.
        """
        encoded=json.dumps(payload,ensure_ascii=False)
        if len(encoded)>32000:raise ValueError('Agent request too large')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT * FROM requests WHERE id=?',(request_id,)).fetchone()
            if old:
                if old['key']!=key or old['payload']!=encoded:raise ValueError('Agent request ID conflict')
                return request_id
            session=db.execute('SELECT * FROM sessions WHERE key=?',(key,)).fetchone()
            now=time.time()
            if not session or session['status']!='ready' or not -5<=now-session['updated']<=30:return None
            if db.execute("SELECT 1 FROM requests WHERE key=? AND status IN ('queued','running','unknown') LIMIT 1",(key,)).fetchone():return None
            db.execute('INSERT INTO requests VALUES(?,?,?,?,?,?,?)',(request_id,key,encoded,'queued',None,now,now))
        return request_id
    def cancel_queued(self,key,request_id):
        """Withdraw only work not yet claimed; never interrupt an agent turn."""
        with self.connect() as db:
            changed=db.execute("UPDATE requests SET status='cancelled',result='Periodic review card stopped before the request was claimed',updated=? WHERE key=? AND id=? AND status='queued'",(time.time(),key,request_id)).rowcount
        return bool(changed)
    def consult(self,key,case_id,request_id,question,reason):
        for value in (case_id,request_id,question,reason):
            if not isinstance(value,str) or not value.strip() or len(value)>16000 or '\x00' in value:
                raise ValueError('Invalid human consultation')
        identity='question:'+hashlib.sha256((key+'\0'+request_id).encode()).hexdigest()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT * FROM consultations WHERE id=?',(identity,)).fetchone()
            if old:
                if any(old[name]!=value for name,value in dict(key=key,case_id=case_id,request_id=request_id,question=question,reason=reason).items()):
                    raise ValueError('Consultation request ID conflict')
            else:db.execute('INSERT INTO consultations(id,key,case_id,request_id,question,reason,status,answer,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?)',
                (identity,key,case_id,request_id,question,reason,'pending',None,time.time(),time.time()))
        return self.consultation(identity)
    def consultation(self,question_id):
        with self.connect() as db:row=db.execute('SELECT * FROM consultations WHERE id=?',(question_id,)).fetchone()
        if not row:raise ValueError('Unknown human consultation')
        return dict(row)
    def consultations(self,key=None,case_id=None):
        clauses=[];values=[]
        if key is not None:clauses.append('key=?');values.append(key)
        if case_id is not None:clauses.append('case_id=?');values.append(case_id)
        query='SELECT * FROM consultations'+(' WHERE '+' AND '.join(clauses) if clauses else '')+' ORDER BY created DESC LIMIT 100'
        with self.connect() as db:return [dict(row) for row in db.execute(query,values)]
    def consultation_notification(self,question_id,status,delivery_id=None):
        if status not in ('not_configured','queued','unknown'):raise ValueError('Invalid consultation notification status')
        with self.connect() as db:
            db.execute('UPDATE consultations SET notification_status=?,delivery_id=?,updated=? WHERE id=?',
                       (status,delivery_id,time.time(),question_id))
        return self.consultation(question_id)
    def answer_consultation(self,question_id,answer):
        """Trusted operator UI only. A human answer requeues work, never clears a hold."""
        if not isinstance(answer,str) or not answer.strip() or len(answer)>16000 or '\x00' in answer:
            raise ValueError('Human answer must be nonempty and at most 16000 characters')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM consultations WHERE id=?',(question_id,)).fetchone()
            if not row:raise ValueError('Unknown human consultation')
            if row['status']!='pending':
                if row['status']=='answered' and row['answer']==answer:return dict(row)
                raise ValueError('Consultation already answered')
            now=time.time()
            payload=json.dumps(dict(kind='human_answer',case_id=row['case_id'],question_id=question_id,
                question=row['question'],answer=answer,prompt='Apply this human answer only to its stated question. Continue the failure task, verify and report; do not automatically release reservations.'),ensure_ascii=False)
            db.execute('INSERT INTO requests VALUES(?,?,?,?,?,?,?)',('answer:'+question_id,row['key'],payload,'queued',None,now,now))
            db.execute("UPDATE consultations SET status='answered',answer=?,updated=? WHERE id=?",(answer,now,question_id))
        return self.consultation(question_id)


def context(directory):
    from ldcon.core.records import read_json
    directory=Path(directory);meta=read_json(directory/'job.json')
    # New per-laser jobs and legacy flat jobs are both supported.
    root=directory.parent.parent if directory.parent.name=='jobs' else directory.parent.parent.parent
    return AgentStore(root),meta


def submit(directory,p):
    store,meta=context(directory)
    request_id='request:'+meta['id']
    store.enqueue(store.key(meta,p['agent_card_id']),request_id,dict(kind='request',job=meta['id'],prompt=p['prompt']))
    return dict(status='succeeded',message='Queued for resident agent; execution not yet confirmed',request_id=request_id)


def public_agent_id(root, meta, card, laser_id, provider):
    """Readable stable identity, unique across installations and resident cards."""
    from ldcon.core.interface_schema import identifier
    for value in (laser_id, card):
        identifier(value)
    from ldcon.agents.agent_providers import PROVIDERS
    if provider not in PROVIDERS:
        raise ValueError('Unknown agent provider')
    from ldcon.core.state_layout import single_laser
    if single_laser(root, laser_id) and (Path(root)/'agents'/'agents.sqlite3').exists():
        previous = AgentStore(root).session(AgentStore.key(meta,card), provider)
        if previous and previous.get('laser_id') == laser_id and previous.get('bridge_id'):
            # A storage migration is not a new public identity or inbox.
            identifier(previous['bridge_id'])
            return previous['bridge_id']
    namespace=str(Path(root).resolve())+'\0'+AgentStore.key(meta,card)
    suffix=hashlib.sha256(namespace.encode()).hexdigest()[:12]
    card_length=min(22,79-(6+len(laser_id[:24])+1+len(provider)+1+1+len(suffix)))
    return f'laser-{laser_id[:24].replace(".", "-")}-{provider}-{card[:card_length].replace(".", "-")}-{suffix}'


def register_bridge(p):
    """Register before a model connects; no inbox read or message send."""
    from ldcon.agents.agent_tools import Toolbox
    from ldcon.core.site_bindings import path
    box=Toolbox(p['tools_context'])
    result=box.call('bridge_register',{'summary':'Resident laser agent: log review, diagnostics and Teams conversations.'})
    try:
        profile=json.loads(result)
    except (TypeError, ValueError) as error:
        raise RuntimeError('agentbridge returned an invalid registration record') from error
    if not isinstance(profile,dict) or profile.get('id')!=p['bridge_id']:
        raise RuntimeError('agentbridge registration identity did not match this laser agent')
    return dict(status='registered',bridge_id=p['bridge_id'],previous_bridge_id=p.get('previous_bridge_id'),workspace=str(path('LDCON_AGENTBRIDGE_PUBLIC')),
                registered_unix_s=time.time())


def prepare_context(directory,p,store,meta,card, *, task_context=False):
    from ldcon.agents.agent_workspace import (read,save,render,storage_instructions,save_workspace_manifest,
                                  communication_instructions,save_agent_instructions)
    from ldcon.core.laser_assets import layout
    from ldcon.core.interface_schema import load_mapping
    from ldcon.agents.teams_card import parameters as teams_parameters
    from ldcon.core.records import atomic_json, read_json
    directory=Path(directory);root=store.root.parent
    laser=load_mapping((directory/'laser.yaml').read_text()) if (directory/'laser.yaml').exists() else {}
    laser_id=laser.get('id',meta.get('laser_id',meta['set_id']))
    template=read(root,laser_id)
    if not Path(template['path']).exists():template=save(root,laser_id,template['markdown'],template['revision'])
    paths=layout(root,laser_id,create=True)
    from ldcon.core.interface_schema import identifier
    identifier(meta['set_id'])
    paths['assignment']=str(config_file(root.resolve(),'assignments',meta['set_id']))
    workspace=p['workspace'] or paths['laser_root']
    bridge_id=public_agent_id(root,meta,card,laser_id,p['provider'])
    old=store.session(store.key(meta,card))
    previous_bridge_id=old.get('bridge_id') if old and old.get('bridge_id')!=bridge_id else None
    candidates=meta.get('agent_teams_cards')
    if candidates is None:
        # Legacy snapshots can expose only explicitly configured deck defaults.
        deck=load_mapping((directory/'interface-deck.yaml').read_text()) if (directory/'interface-deck.yaml').exists() else {'cards':[]}
        candidates={c['id']:teams_parameters(c,{}) for c in deck['cards'] if c['operation']=='notify.teams'}
    candidates={k:teams_parameters({'parameters':v},{}) for k,v in candidates.items() if v.get('webhook_env')}
    values=dict(laser_id=laser_id,laser_name=laser.get('name',laser_id),deck_id=meta['deck_id'],card_id=card,state_root=root,
                workspace=workspace,bridge_id=bridge_id,teams_card_ids=', '.join(candidates) or '(not configured)',
                **{key+'_dir':value for key,value in paths.items()})
    instructions=render(template['markdown'],values)+'\n\n'+storage_instructions(paths)
    protocol=communication_instructions(values)
    if protocol not in instructions:instructions+='\n\n'+protocol
    from ldcon.agents.agent_metrics import build as metric_manifest, instructions as metric_instructions
    deck=load_mapping((directory/'interface-deck.yaml').read_text()) if (directory/'interface-deck.yaml').exists() else {'id':meta['deck_id'],'cards':[]}
    assignment=load_mapping((directory/'set.yaml').read_text()) if (directory/'set.yaml').exists() else {}
    metric_parameters=next((d.get('parameters',{}) for d in assignment.get('decks',[]) if d.get('deck_id',d.get('id'))==meta['deck_id']),{})
    metrics=metric_manifest({**laser,'id':laser_id},deck,card_parameters=metric_parameters)
    atomic_json(directory/'laser-metrics.json',metrics)
    instructions+=metric_instructions(metrics)
    from ldcon.agents.agent_window import POLICY as window_policy
    instructions+='\n'+window_policy
    from ldcon.agents.agent_execution import POLICY as execution_policy,profile
    execution=profile(p['provider'])
    if execution_policy not in instructions:instructions+='\n'+execution_policy
    atomic_json(directory/'agent-execution.json',execution)
    if not task_context:
        instruction_files=save_agent_instructions(paths)
        save_workspace_manifest(paths,dict(schema_version=1,laser_id=laser_id,provider=p['provider'],
            workspace=workspace,paths=paths,bridge_id=bridge_id,previous_bridge_id=previous_bridge_id,job_id=meta['id'],
            instruction_files=instruction_files,write_tool='laser_files_write',automatic_execution=False))
    role='task' if task_context else p.get('role','gateway')
    if role=='gateway':
        from ldcon.agents.gateway_role import POLICY
        instructions+='\n'+POLICY
    (directory/'agent-instructions.md').write_text(instructions)
    context_path=directory/'agent-tools-context.json'
    atomic_json(context_path,dict(job_id=meta['id'],laser_id=laser_id,laser_name=laser.get('name',laser_id),
        deck_id=meta['deck_id'],card_id=card,provider=p['provider'],
        workspace=workspace,asset_paths=paths,bridge_id=bridge_id,previous_bridge_id=previous_bridge_id,
        teams_cards=candidates,template_revision=template['revision'],metric_manifest=metrics,execution_profile=execution))
    context=read_json(context_path,{})
    context['agent_role']=role
    if role=='gateway':
        from ldcon.agents.gateway_role import TOOLS
        context['task_allowed_tools']=sorted(TOOLS)
    atomic_json(context_path,context)
    store.session_context=dict(laser_id=laser_id,template_revision=template['revision'],bridge_id=bridge_id,workspace=workspace)
    return {**p,'workspace':workspace,'bridge_id':bridge_id,'previous_bridge_id':previous_bridge_id,'laser_id':laser_id,
            'instructions':instructions,'tools_context':str(context_path),
            'config_read_paths':[paths['laser_config'],paths['assignment'],
                str(config_file(root.resolve(),'interface-decks',meta['deck_id']))]}


def run_resident(directory,p,*,clock,sleep,cancel,emit,adapter_factory=None,bridge_registrar=None):
    from ldcon.agents.agent_adapters import SessionAdapter
    from ldcon.core.records import atomic_json,read_json
    store,meta=context(directory);card=read_json(Path(directory)/'invocation.json')['card_id'];key=store.key(meta,card)
    from ldcon.agents.agent_profiles import resolve
    from ldcon.core.interface_schema import load_mapping
    laser=load_mapping((Path(directory)/'laser.yaml').read_text()) if (Path(directory)/'laser.yaml').exists() else {}
    selected=resolve(store.root.parent,p.get('execution_profile',''),laser_id=laser.get('id',meta.get('laser_id',meta['set_id'])),resident=True)
    if selected:
        p={**p,'provider':selected['provider'],'model':selected['model'],'effort':selected['effort']}
        atomic_json(Path(directory)/'agent-selected-profile.json',selected)
    if p.get('remote_control') is None:
        p={**p,'remote_control':p['provider'] in ('claude','codex')}
    if p.get('remote_control') and p['provider'] not in ('claude','codex'):
        raise ValueError('The selected execution profile does not support Remote Control')
    lock=(store.root/(hashlib.sha256(key.encode()).hexdigest()+'.lock')).open('a')
    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close();raise ValueError('Resident agent card already running')
    adapter=None;session='';session_lock=None;connection_started=False;inbox_receiver=None
    try:
        old=store.session(key,p['provider'])
        if old and old['session'] and p['session_id'] and old['session']!=p['session_id']:raise ValueError('Use a new card ID to change the resident session')
        session=p['session_id'] or (old['session'] if old else '')
        if not session and (p['mode']=='reconnect' or old):raise ValueError('Existing session required; creation may have been interrupted. Supply its session ID or use a new card ID')
        if old and session:
            previous_workspace=old.get('workspace')
            if previous_workspace and p['workspace'] and p['workspace']!=previous_workspace:
                raise ValueError('Existing session workspace differs; use its recorded workspace or another card ID')
            if not p['workspace']:
                from ldcon.agents.agent_workspace import directory as legacy_directory
                p={**p,'workspace':previous_workspace or str(legacy_directory(store.root.parent,old.get('laser_id') or meta['set_id'],True))}
        p=prepare_context(directory,p,store,meta,card)
        from ldcon.agents.agent_bundle import build,turn_origin
        from ldcon.agents.agent_adapters import INSTRUCTIONS
        bundle=build(directory,'diagnosis',context=dict(laser_id=p['laser_id'],role='resident',profile=selected),evidence={})
        p['instructions']=INSTRUCTIONS+'\n\n'+bundle['instructions']+'\n\n'+p['instructions']
        p['instructions_resolved']=True
        atomic_json(Path(directory)/'agent-effective-instructions.json',dict(text=p['instructions'],bundle_revision=bundle['bundle_revision']))
        if p.get('remote_control') and p['provider']=='claude' and adapter_factory is None:
            from ldcon.agents.agent_interactive import InteractiveSessionAdapter
            adapter_factory=InteractiveSessionAdapter
        if p['provider']=='opencode' and adapter_factory is None:
            from ldcon.agents.agent_opencode_http import OpenCodeSharedAdapter
            adapter_factory=OpenCodeSharedAdapter
        adapter=(adapter_factory or SessionAdapter)(p,cancel)
        if hasattr(adapter,'prompt_sink') and adapter.prompt_sink is None and p.get('laser_id'):
            # Host-only ledger (outside the sandbox-writable deployment root).
            from ldcon.core.agent_authority import ApprovalLedger
            adapter.prompt_sink=ApprovalLedger(store.root.parent,p['laser_id']).record_prompt
        if hasattr(adapter,'preflight'):adapter.preflight()
        if cancel():raise CardCancelled('Cancelled before agentbridge registration')
        registration=(bridge_registrar or register_bridge)(p)
        atomic_json(Path(directory)/'bridge-registration.json',registration)
        emit('agent_registered',registration)
        if cancel():raise CardCancelled('Cancelled before resident connection')
        store.save(key,p['provider'],session,'connecting',meta['id']);store.recover(key)
        connection_started=True
        def lock_session(value):
            path=store.root/('session-'+hashlib.sha256((p['provider']+value).encode()).hexdigest()+'.lock')
            handle=path.open('a')
            try:fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:
                handle.close();raise ValueError('Session already connected to another resident card')
            return handle
        if session:session_lock=lock_session(session)
        adapter.session_created=lambda value:store.save(key,p['provider'],value,'connecting',meta['id'])
        reconnected=bool(session)
        session=adapter.connect(session)
        if not session_lock:session_lock=lock_session(session)
        store.save(key,p['provider'],session,'ready',meta['id'])
        emit('agent_connected',dict(provider=p['provider'],session_id=session,bridge_id=p['bridge_id'],
                                    model_info=dict(getattr(adapter,'model_info',{}))))
        from ldcon.agents.agent_startup_notice import notify_startup
        notice=notify_startup(directory,p,adapter,session,reconnected=reconnected,connection_id=uuid.uuid4().hex)
        emit('agent_startup_notification',dict(status=notice['status'],connection_id=notice['connection_id'],
                                              delivery_id=notice.get('delivery_id'),delivered=False))
        from ldcon.agents.agent_inbox_watch import InboxWatcher, InboxReceiver
        inbox_receiver=InboxReceiver(InboxWatcher(p['bridge_id']),store,key,Path(directory)/'hub-watch-status.json').start()
        while not cancel():
            if not adapter.alive():raise RuntimeError('Agent transport disconnected')
            activity=adapter.activity() if hasattr(adapter,'activity') else 'ready'
            row=store.claim(key) if activity=='ready' else None
            if row:
                store.save(key,p['provider'],session,'busy',meta['id'])
                # Requests may take minutes. Native clients must retain the
                # same connection link/model metadata throughout that turn.
                transport=adapter.transport_status() if hasattr(adapter,'transport_status') else {}
                atomic_json(Path(directory)/'agent-status.json',dict(session=store.session(key),requests=store.requests(key),**transport))
                emit('agent_request',dict(request_id=row['id'],status='running'))
                try:
                    payload=json.loads(row['payload'])
                    turn_origin(directory,row['id'],payload.get('kind','unknown'))
                    result=adapter.request(payload)
                    turn_origin(directory,row['id'],payload.get('kind','unknown'),active=False)
                    store.finish(row['id'],'completed',result)
                    emit('agent_result',dict(request_id=row['id'],status='completed',result=result))
                except Exception as error:
                    from ldcon.agents.agent_interactive import RequestDeferred
                    if isinstance(error,RequestDeferred):
                        turn_origin(directory,row['id'],'deferred_before_send',active=False)
                        store.defer(row['id'])
                        emit('agent_request',dict(request_id=row['id'],status='queued',reason=str(error)))
                        sleep(p['sample_interval_s'])
                        continue
                    turn_origin(directory,row['id'],'unknown',active=False)
                    store.finish(row['id'],'unknown','Agent turn interrupted or failed; inspect session before retrying')
                    from ldcon.agents.agent_interactive import RequestOutcomeUnknown
                    if not isinstance(error,RequestOutcomeUnknown):raise
                    emit('agent_result',dict(request_id=row['id'],status='unknown',reason=str(error)))
            activity=adapter.activity() if hasattr(adapter,'activity') else 'ready'
            store.save(key,p['provider'],session,activity,meta['id'])
            transport=adapter.transport_status() if hasattr(adapter,'transport_status') else {}
            atomic_json(Path(directory)/'agent-status.json',dict(session=store.session(key),requests=store.requests(key),**transport))
            emit('state',dict(state='agent_'+activity,session_id=session,**transport))
            sleep(p['sample_interval_s'])
        raise CardCancelled('Resident disconnected; conversation and pending requests retained')
    finally:
        if inbox_receiver:inbox_receiver.close()
        if adapter:adapter.close()
        # Never stop an externally running coding session or any device owner.
        if adapter and connection_started:
            session=session or getattr(adapter,'session','')
            store.save(key,p['provider'],session,'disconnected',meta['id'])
            atomic_json(Path(directory)/'agent-status.json',dict(session=store.session(key),requests=store.requests(key),
                        model_info=dict(getattr(adapter,'model_info',{}))))
        if session_lock:session_lock.close()
        lock.close()
