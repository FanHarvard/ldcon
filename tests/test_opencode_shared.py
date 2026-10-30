"""Shared gateway correlation and authority boundaries; no real model/device calls."""
import hashlib
import time
import pytest
from ldcon.agents.agent_opencode_http import OpenCodeSharedAdapter
from ldcon.agents.agent_interactive import RequestOutcomeUnknown
from ldcon.agents.agent_connect import NoRedirect,request

class Fake(OpenCodeSharedAdapter):
 def __init__(self):
  super().__init__(dict(provider='opencode',request_timeout_s=1),lambda:False)
  self.session='ses_test';self.rows=[];self.posts=[];self.after=[];self.prompt_sink=lambda *x:None
 def busy(self):return False
 def api(self,method,path,body=None):
  if method=='POST':
   self.posts.append(body);self.rows=[dict(id='own',type='user',text=body['text']),*self.rows]
   self.after=[dict(id='idle',type='idle',outcome='succeeded'),dict(id='assistant',type='assistant',finish='stop',content=[dict(type='text',text='complete')],model={'providerID':'p','id':'m'})]
   return {'data':{'id':'own'}}
  if '/message?' in path:return {'data':self.rows,'cursor':{}}
  return {'data':{'model':{'providerID':'p','id':'m'}}}
 def recent(self):
  if self.after:self.rows=self.after+self.rows;self.after=[]
  return super().recent()

def test_complete_suffix_is_reread_and_dispatch_is_not_human():
 a=Fake();events=[];a.prompt_sink=lambda *x:events.append(x)
 assert a.request({'prompt':'request'})=='complete'
 assert len(a.posts)==1 and len(events)==1 and events[0][1]=='dispatch'
 assert a.model_info['response_confirmed']

def test_partial_assistant_update_is_not_returned():
 a=Fake();a.anchor='old'
 a.rows=[dict(id='assistant',type='assistant',content=[dict(type='text',text='part')]),dict(id='own',type='user',text='x'),dict(id='old',type='idle')]
 a.recent()
 a.rows=[dict(id='idle',type='idle',outcome='succeeded'),dict(id='assistant',type='assistant',finish='stop',content=[dict(type='text',text='full')]),dict(id='own',type='user',text='x'),dict(id='old',type='idle')]
 assert a.completed_response('own','idle')[0]['content'][0]['text']=='full'

@pytest.mark.parametrize('kind',['user','model-switched','location-switched'])
def test_external_input_cannot_be_misattributed(kind):
 a=Fake();a.rows=[dict(id='idle',type='idle'),dict(id='other',type=kind),dict(id='own',type='user')]
 with pytest.raises(RequestOutcomeUnknown,match='overlaps'):a.completed_response('own','idle')

def test_removed_anchor_does_not_reauthorize_old_human_message():
 a=Fake();a.anchor='removed';events=[];a.prompt_sink=lambda *x:events.append(x)
 a.rows=[dict(id='old',type='user',text='old approval')]
 with pytest.raises(RuntimeError,match='history changed'):a.recent()
 assert not events

def test_native_history_cannot_mint_a_human_approval():
 a=Fake();a.anchor='old';events=[];a.prompt_sink=lambda *x:events.append(x)
 a.rows=[dict(id='new',type='user',text='human input'),dict(id='old',type='idle')]
 a.recent();a.recent()
 assert not events

def test_lost_response_quarantines_without_replay_or_interrupt():
 a=Fake();a.await_response=lambda *x:(_ for _ in ()).throw(OSError('lost read'))
 with pytest.raises(RequestOutcomeUnknown,match='observation failed'):a.request({'prompt':'once'})
 assert a.quarantined and len(a.posts)==1
 with pytest.raises(RequestOutcomeUnknown):a.request({'prompt':'twice'})
 assert len(a.posts)==1

def test_unknown_admission_never_retries():
 a=Fake();old=a.api
 def api(method,path,body=None):
  if method=='POST':a.posts.append(body);raise TimeoutError('lost admission')
  return old(method,path,body)
 a.api=api
 with pytest.raises(RequestOutcomeUnknown,match='admission'):a.request({'prompt':'once'})
 assert a.quarantined and len(a.posts)==1

def test_loopback_and_redirect_boundary():
 for url in ('http://other:123','https://127.0.0.1:123','http://secret@127.0.0.1:123','http://127.0.0.1:123/path'):
  with pytest.raises(ValueError):request(url,'password','GET','/api/info')
 assert NoRedirect().redirect_request(None,None,302,'',{},'http://other') is None

def test_credentials_are_hidden_from_agent_sandbox(tmp_path,monkeypatch):
 from ldcon.agents import agent_connect, agent_sandbox
 monkeypatch.setenv('HOME',str(tmp_path));monkeypatch.delenv('LDCON_PRIVATE_ROOTS',raising=False)
 root=agent_connect.access_root(True)
 assert agent_sandbox._private_mounts()==['--tmpfs',str(root)]
 with pytest.raises(ValueError):agent_connect.record(dict(laser_id='sr422',job_id='../invalid'))

def test_busy_before_send_defers_then_delivers_once():
 from ldcon.agents.agent_interactive import RequestDeferred
 a=Fake();a.busy=lambda:True
 with pytest.raises(RequestDeferred):a.request({'prompt':'later'})
 assert not a.posts and not a.quarantined
 a.busy=lambda:False
 assert a.request({'prompt':'later'})=='complete'
 assert len(a.posts)==1

def test_turn_queued_after_native_turn_correlates_only_its_own_suffix():
 a=Fake();old=a.api
 def api(method,path,body=None):
  result=old(method,path,body)
  if method=='POST':
   a.rows += [dict(id='human-idle',type='idle',outcome='succeeded'),dict(id='human',type='user',text='native turn')]
  return result
 a.api=api
 assert a.request({'prompt':'queued'})=='complete'
 assert len(a.posts)==1


def test_managed_session_name_restored_without_prompt_or_model_change():
 a=Fake();a.p.update(laser_id='sr422',role='gateway');calls=[]
 info={'id':'ses_test','title':'Old incident report'}
 def api(method,path,body=None):
  calls.append((method,path,body))
  if method=='PATCH':info.update(body)
  return {'data':dict(info)}
 a.api=api;a.name_session(info)
 assert info['title']=='sr422 — Gateway agent'
 assert [c[0] for c in calls]==['PATCH','GET']
 assert calls[0][2]=={'title':'sr422 — Gateway agent'}
 calls.clear();a.name_session(info);assert not calls
 a.p['role']='operator';assert a.session_title()=='sr422 — Operator agent'
