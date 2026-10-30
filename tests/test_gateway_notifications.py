"""Notification reception and safe pre-send deferral; no external services."""
import json
import threading
import time

import pytest

from ldcon.agents.agent_inbox_watch import InboxWatcher, InboxReceiver
from ldcon.agents.agent_interactive import InteractiveSessionAdapter, RequestDeferred
from ldcon.agents.resident_agent import AgentStore


def hub(tmp_path):
    p=tmp_path/'hub/agents/laser-test';p.mkdir(parents=True)
    (p/'profile.json').write_text(json.dumps(dict(id='laser-test',inbox='agents/laser-test/inbox.jsonl')))
    (p/'inbox.jsonl').write_text(json.dumps(dict(id='first',body='untrusted'))+'\n')
    return p, InboxWatcher('laser-test',tmp_path/'hub')


def test_busy_native_turn_still_queues_and_coalesces_without_ack(tmp_path):
    p,w=hub(tmp_path);store=AgentStore(tmp_path/'runtime')
    assert w.tick(store,'key','busy',clock=lambda:0)['state']=='notice_recorded'
    first=store.requests('key')[0];assert first['status']=='queued'
    (p/'inbox.jsonl').write_text(json.dumps(dict(id='second'))+'\n')
    assert w.tick(store,'key','busy',clock=lambda:6)['state']=='delivery_pending'
    assert len(store.requests('key'))==1 and not (p/'.read_offset').exists()
    store.claim('key');store.finish(first['id'],'completed','done')
    w.tick(store,'key','ready',clock=lambda:12)
    assert len(store.requests('key'))==2


def test_receiver_runs_while_main_thread_waits_for_model(tmp_path):
    _,w=hub(tmp_path);store=AgentStore(tmp_path/'runtime')
    seen=threading.Event();original=w.tick
    def tick(*args):
        result=original(*args);seen.set();return result
    w.tick=tick
    receiver=InboxReceiver(w,store,'key',tmp_path/'status.json',interval_s=.01).start()
    try:
        # Represents a blocking adapter.request on the resident main thread.
        assert seen.wait(2)
        assert store.requests('key')[0]['status']=='queued'
    finally:receiver.close()
    assert not receiver.thread.is_alive()
    assert json.loads((tmp_path/'status.json').read_text())['reception_independent']


def test_pre_send_defer_preserves_exact_request_and_unknown_is_not_requeued(tmp_path):
    s=AgentStore(tmp_path);s.enqueue('key','one',dict(prompt='once'));r=s.claim('key')
    s.defer(r['id']);assert s.request('key','one')['status']=='queued'
    assert s.claim('key')['payload']==r['payload']
    s.finish('one','unknown','delivery uncertain');s.defer('one')
    assert s.request('key','one')['status']=='unknown' and s.claim('key') is None


def test_claude_human_turn_is_known_unsent_not_unknown():
    a=InteractiveSessionAdapter(dict(provider='claude',request_timeout_s=1),lambda:False)
    a.poll=lambda:True
    a.stage=lambda _:pytest.fail('Must not stage input during a human turn')
    with pytest.raises(RequestDeferred):a.request(dict(prompt='later'))


def test_resident_busy_request_keeps_live_connection_metadata(tmp_path,monkeypatch):
    from ldcon.agents import resident_agent as module
    from ldcon.core.interface_cards import CardCancelled
    directory=tmp_path/'job';directory.mkdir()
    (directory/'invocation.json').write_text(json.dumps(dict(card_id='gateway')))
    store=AgentStore(tmp_path/'runtime');meta=dict(id='job',set_id='laser',deck_id='deck')
    key=store.key(meta,'gateway');store.enqueue(key,'review',dict(kind='task_completed',prompt='Read-only result'))
    monkeypatch.setattr(module,'context',lambda _: (store,meta))
    monkeypatch.setattr('ldcon.agents.agent_profiles.resolve',lambda *a,**k:None)
    monkeypatch.setattr(module,'prepare_context',lambda d,p,*a:{**p,'bridge_id':'test-gateway','laser_id':'laser','instructions':'test'})
    monkeypatch.setattr('ldcon.agents.agent_bundle.build',lambda *a,**k:dict(instructions='common',bundle_revision='test'))
    monkeypatch.setattr('ldcon.agents.agent_startup_notice.notify_startup',lambda *a,**k:dict(status='notification_suppressed',connection_id='test'))
    class Receiver:
        def __init__(self,*a):pass
        def start(self):return self
        def close(self):pass
    monkeypatch.setattr('ldcon.agents.agent_inbox_watch.InboxReceiver',Receiver)
    stopped=[False];observed=[]
    class Adapter:
        def __init__(self,*a):self.model_info=dict(resolved_model='confirmed-model',resolved_effort='medium')
        def connect(self,session):return 'same-conversation'
        def alive(self):return True
        def activity(self):return 'ready'
        def transport_status(self):return dict(model_info=self.model_info,remote_control=dict(url='https://example.invalid/test-session',status='link_observed'))
        def request(self,payload):
            d=json.loads((directory/'agent-status.json').read_text())
            assert d['session']['status']=='busy'
            assert d['remote_control']['url']=='https://example.invalid/test-session'
            assert d['model_info']['resolved_model']=='confirmed-model'
            observed.append(True)
            return 'Result acknowledged'
        def close(self):pass
    def sleep(_):stopped[0]=True
    params=module.parameters(dict(operation='agent.resident',parameters={}),{})
    with pytest.raises(CardCancelled):
        module.run_resident(directory,params,clock=time.monotonic,sleep=sleep,cancel=lambda:stopped[0],
            emit=lambda *a:None,adapter_factory=Adapter,bridge_registrar=lambda _:dict(status='registered'))
    assert observed==[True] and store.request(key,'review')['status']=='completed'
