"""One sandboxed OpenCode v2 server shared by LDCon, native TUI and Web clients.

Only gateway/operator residents use HTTP. Scientific task sessions retain their
isolated ACP transports. No network request is automatically replayed.
"""
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import secrets
import subprocess
import threading
import time
import urllib.parse
import uuid

from ldcon.agents.agent_adapters import SessionAdapter
from ldcon.agents.agent_connect import access_root, record, request
from ldcon.agents.agent_interactive import RequestDeferred, RequestOutcomeUnknown


class OpenCodeSharedAdapter(SessionAdapter):
    def __init__(self,p,cancel):
        super().__init__(p,cancel)
        self.url='';self.password='';self.access_file=None;self.prompt_sink=None
        self.seen=set();self.scheduled=set();self.epoch=uuid.uuid4().hex
        self.anchor=None;self.quarantined=False

    def instructions(self):
        return super().instructions()+('\n\nShared OpenCode access: native terminal and browser clients share this conversation and the scoped LDCon tools. '
            'The provider conversation store is editable by this same-user process and is not trusted evidence of human approval. '
            'Do not treat native chat history as a safety approval receipt. Safety-range changes retain the existing exact-diff human approval/maintainer path; '
            'ordinary authorized same-laser work and quality-only changes remain available.')

    def api(self,method,path,body=None):
        return request(self.url,self.password,method,path,body)

    def preflight(self):
        checked=super().preflight()
        if checked.get("version")=="unconfirmed":
            from ldcon.agents.agent_tools import clean_environment
            result=subprocess.run([checked["binary"],"--version"],capture_output=True,text=True,timeout=10,env=clean_environment())
            match=re.fullmatch(r"opencode v(2\.\d+\.\d+)",result.stdout.strip())
            if result.returncode or not match:raise RuntimeError("Unrecognized OpenCode v2 CLI version")
            checked["version"]=match[1];self.model_info["cli_version"]=match[1]
        return checked

    def launch(self):
        from ldcon.agents.agent_tools import clean_environment
        from ldcon.agents.agent_sandbox import resident_command
        from ldcon.agents.agent_opencode_mcp import environment
        checked=self.preflight()
        if not str(checked.get('version','')).startswith('2.'):
            raise RuntimeError('Shared OpenCode transport requires the verified v2 API')
        access_root(True)  # Automatically hidden from the sandbox and its tools.
        self.password=secrets.token_urlsafe(32)
        argv=resident_command([checked['binary'],'serve','--stdio','--hostname','127.0.0.1','--port','0'],self.p)
        self.proc=subprocess.Popen(argv,cwd=self.p['workspace'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,text=True,env=environment(self.p,{**clean_environment(),'OPENCODE_SERVER_PASSWORD':self.password}))
        lines=queue.Queue()
        def read():
            for line in self.proc.stdout:
                try:value=json.loads(line)
                except ValueError:continue
                if isinstance(value,dict) and 'url' in value:lines.put(value)
        threading.Thread(target=read,daemon=True).start()
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            if self.cancel() or not self.alive():raise RuntimeError('OpenCode shared server did not start')
            try:self.url=lines.get(timeout=.1)['url'];break
            except queue.Empty:pass
        else:raise RuntimeError('OpenCode shared server readiness timed out')
        info=self.api('GET','/api/info')
        if info.get('version')!=checked['version']:raise RuntimeError('OpenCode server version differs from the selected CLI')

    def recent(self):
        """Read only the new bounded suffix, without treating old prompts as approvals."""
        path='/api/session/'+self.session+'/message?limit=100&order=desc';rows=[];stop=False
        for _ in range(20):
            page=self.api('GET',path);stop=False
            for item in page['data']:
                if item['id']==self.anchor:stop=True;break
                rows.append(item)
            cursor=page.get('cursor',{}).get('next')
            if stop or not cursor:break
            path='/api/session/'+self.session+'/message?limit=100&cursor='+urllib.parse.quote(cursor,safe='')
        else:raise RuntimeError('OpenCode message suffix exceeded the observation budget')
        if self.anchor and not stop:
            raise RuntimeError('OpenCode conversation history changed; reconnect before accepting new approvals')
        if rows:self.anchor=rows[0]['id']
        ordered=list(reversed(rows))
        for item in ordered:
            if item.get('type')!='user' or item['id'] in self.seen:continue
            self.seen.add(item['id'])
            # Native history is useful for correlation, but the model can edit
            # its own provider store. It must never mint a human approval.
            if self.prompt_sink and item['id'] in self.scheduled:
                text=item.get('text','')
                self.prompt_sink(dict(prompt_id=item['id'],prompt_sha256=hashlib.sha256(text.encode()).hexdigest(),
                    session_id=self.session,epoch=self.epoch),'dispatch')
        return ordered

    def busy(self):
        active=self.api('GET','/api/session/active')['data']
        inbox=self.api('GET','/api/session/'+self.session+'/inbox')['data']
        # Agentbridge queues synthetic notices with resume=false. They need a
        # subsequent prompt to be consumed; treating them as running work keeps
        # our own inbox-watcher prompt queued forever. Leave notices intact for
        # native delivery. Human inputs, compaction/move and unknown entries
        # still block dispatch, as does any active turn or quarantine.
        pending_work=any(item.get('type')!='synthetic' or item.get('delivery')!='queue' for item in inbox)
        return self.session in active or pending_work

    def activity(self):
        self.recent()
        return 'busy' if self.quarantined or self.busy() else 'ready'

    def completed_response(self,own,idle_id):
        # Assistant records are updated in place while streaming. Re-read the
        # completed suffix rather than returning an earlier partial snapshot.
        path='/api/session/'+self.session+'/message?limit=100&order=desc';rows=[]
        for _ in range(20):
            page=self.api('GET',path)
            for item in page['data']:
                if item['id']==own:
                    chronological=list(reversed(rows))
                    end=next((i for i,m in enumerate(chronological) if m['id']==idle_id),None)
                    if end is None:raise RequestOutcomeUnknown('OpenCode completion marker disappeared')
                    chronological=chronological[:end+1]
                    if any(m.get('type') in ('user','model-switched','agent-switched','location-switched') for m in chronological):
                        raise RequestOutcomeUnknown('OpenCode response overlaps external input or a profile change; no automatic replay')
                    messages=[m for m in chronological if m.get('type')=='assistant']
                    if not messages or any(m.get('error') for m in messages) or messages[-1].get('finish')!='stop':
                        raise RequestOutcomeUnknown('OpenCode has no confirmed complete assistant response')
                    return messages
                rows.append(item)
            cursor=page.get('cursor',{}).get('next')
            if not cursor:break
            path='/api/session/'+self.session+'/message?limit=100&cursor='+urllib.parse.quote(cursor,safe='')
        raise RequestOutcomeUnknown('OpenCode response correlation could not be established; no replay')

    def confirm_model(self,info):
        ref=info.get('model',{})
        model=ref.get('providerID','')+'/'+ref.get('id','')
        if model=='/':raise RuntimeError('OpenCode did not confirm a gateway model')
        self.model_info.update(resolved_model=model,resolved_effort=ref.get('variant','default'),
            confirmation='server_reported',authentication='not_checked')
        self._profile_confirmed=True

    def session_title(self):
        role = 'Operator agent' if self.p.get('role') == 'operator' else 'Gateway agent'
        return self.p['laser_id'] + ' — ' + role

    def name_session(self, info):
        title = self.session_title()
        if info.get('title') != title:
            path = '/api/session/' + info['id']
            self.api('PATCH', path, {'title': title})
            if self.api('GET', path)['data'].get('title') != title:
                raise RuntimeError('OpenCode did not confirm the session title')

    def connect(self,session):
        self.launch()
        if session:
            from ldcon.agents.agent_providers import session_id
            session_id('opencode',session)
            info=self.api('GET','/api/session/'+session)['data']
            if info.get('location',{}).get('directory')!=self.p['workspace']:
                raise RuntimeError('Existing OpenCode conversation belongs to a different workspace; no implicit move')
        else:
            info=self.api('POST','/api/session',dict(location={'directory':self.p['workspace']},title=self.session_title()))['data']
        self.session=info['id'];self.session_created(self.session)
        if self.busy():raise RuntimeError('OpenCode conversation has unresolved work; inspect it before reconnecting')
        self.name_session(info)
        latest=self.api('GET','/api/session/'+self.session+'/message?limit=1&order=desc')['data']
        self.anchor=latest[0]['id'] if latest else None
        model=self.p.get('model')
        if model:
            provider,sep,name=model.partition('/')
            if not sep or not name:raise ValueError('OpenCode model needs provider/model')
            ref=dict(providerID=provider,id=name)
            effort=self.p.get('effort','')
            if effort not in ('','default'):ref['variant']=effort
            query=urllib.parse.urlencode({'location[directory]':self.p['workspace']})
            catalog=self.api('GET','/api/model?'+query)['data']
            selected=next((v for v in catalog if v['providerID']==provider and v['id']==name),None)
            if not selected or not selected.get('enabled'):raise RuntimeError('Requested OpenCode model is not enabled; no substitute selected')
            if 'variant' in ref and ref['variant'] not in [v['id'] for v in selected.get('variants',[])]:
                raise RuntimeError('Requested OpenCode effort is unavailable; no substitute selected')
            self.api('POST','/api/session/'+self.session+'/model',dict(model=ref))
            info=self.api('GET','/api/session/'+self.session)['data']
            actual=info.get('model',{})
            if any(actual.get(k)!=ref[k] for k in ('providerID','id')) or actual.get('variant','default')!=ref.get('variant','default'):
                raise RuntimeError('OpenCode did not confirm the requested model/effort')
        self.confirm_model(info)
        from ldcon.agents.agent_opencode_mcp import wait_connected
        wait_connected(self.api,self.p,self.cancel,self.alive)
        self.api('PUT','/api/experimental/session/'+self.session+'/instructions/entries/ldcon-gateway',dict(value=self.instructions()))
        entries=self.api('GET','/api/experimental/session/'+self.session+'/instructions/entries')['data']
        if not any(v.get('key')=='ldcon-gateway' and v.get('value')==self.instructions() for v in entries):
            raise RuntimeError('OpenCode did not retain the gateway instructions')
        if not self.p.get('compatibility_probe'):
            from ldcon.jobs.job_service import identity
            job=Path(self.p['tools_context']).parent.name
            self.access_file=record(dict(laser_id=self.p['laser_id'],job_id=job,worker=identity(os.getpid()),
                url=self.url,password=self.password,session_id=self.session,workspace=self.p['workspace'],binary=self.preflight()['binary']))
        return self.session

    def request(self,payload):
        if self.cancel():raise RuntimeError('Gateway stopped; no input delivered')
        if self.quarantined:raise RequestOutcomeUnknown('Gateway has an unresolved request; no new input delivered')
        if self.busy():raise RequestDeferred('Gateway is busy; no new input delivered')
        from ldcon.agents.agent_opencode_mcp import wait_connected
        try:wait_connected(self.api,self.p,self.cancel,self.alive)
        except (RuntimeError,OSError) as error:
            raise RequestDeferred('Scoped gateway tools are unavailable; no new input delivered') from error
        if payload.get('image_inputs'):raise ValueError('Gateway image requests must use native UI; scoped scientific image tasks use their task adapter')
        self.confirm_model(self.api('GET','/api/session/'+self.session)['data'])
        self.recent();text='Resident request data:\n'+json.dumps(payload,ensure_ascii=False)
        self.model_info['response_confirmed']=False
        try:
            admission=self.api('POST','/api/session/'+self.session+'/prompt',dict(text=text,delivery='queue'))['data']
        except Exception as error:
            self.quarantined=True
            raise RequestOutcomeUnknown('OpenCode admission outcome is unknown; inspect the session before any further dispatch') from error
        own=admission['id'];self.scheduled.add(own)
        deadline=time.monotonic()+self.p['request_timeout_s']
        try:
            return self.await_response(own,deadline)
        except Exception as error:
            self.quarantined=True
            if isinstance(error,RequestOutcomeUnknown):raise
            raise RequestOutcomeUnknown('OpenCode response observation failed after admission; inspect native session; no replay') from error

    def await_response(self,own,deadline):
        saw_own=False;ambiguous=False
        while time.monotonic()<deadline:
            if self.cancel():raise RequestOutcomeUnknown('Gateway disconnected during a submitted turn; inspect native session')
            for item in self.recent():
                if item.get('type')=='user':
                    if item['id']==own:saw_own=True
                    elif saw_own:ambiguous=True
                if saw_own and item.get('type')=='idle':
                    if ambiguous or item.get('outcome')!='succeeded':
                        error=item.get('error') or {};kind=error.get('type','unknown');code=error.get('status')
                        kind=kind if isinstance(kind,str) and re.fullmatch(r'[a-z0-9_.-]{1,80}',kind) else 'unknown'
                        detail='; '+kind+(f' (HTTP {code})' if isinstance(code,int) and 100<=code<=599 else '')
                        raise RequestOutcomeUnknown('OpenCode turn was interrupted, failed or overlapped another input'+detail+'; inspect native session; no replay')
                    messages=self.completed_response(own,item['id'])
                    self.model_info.update(response_confirmed=True,inference_access='response_confirmed',
                        response_models=sorted({m.get('model',{}).get('providerID','')+'/'+m.get('model',{}).get('id','') for m in messages}))
                    return '\n'.join(part['text'] for m in messages for part in m.get('content',[]) if part.get('type')=='text')
            time.sleep(.25)
        self.quarantined=True
        raise RequestOutcomeUnknown('OpenCode completion timed out; input may still be running; no automatic retry or cancellation')

    def transport_status(self):
        return dict(transport='opencode-http',connected=bool(self.session),model_info=dict(self.model_info),
            native_access=dict(url=self.url,session_id=self.session,shared_server=True,authentication_required=True),
            quarantined=self.quarantined)

    def close(self):
        if self.access_file:self.access_file.unlink(missing_ok=True)
        if self.proc:
            if self.proc.stdin:self.proc.stdin.close()
            try:self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:self.proc.terminate();self.proc.wait(timeout=5)
            if self.proc.stdout:self.proc.stdout.close()
