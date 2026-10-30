"""Passive native notices must not deadlock the durable resident wakeup queue."""
import json

import pytest

from ldcon.agents.agent_inbox_watch import InboxWatcher
from ldcon.agents.agent_interactive import RequestDeferred, RequestOutcomeUnknown
from ldcon.agents.agent_opencode_http import OpenCodeSharedAdapter
from ldcon.agents.resident_agent import AgentStore


class Native:
    def __init__(self):
        self.active = {}
        self.inbox = [dict(id='notice', type='synthetic', delivery='queue')]
        self.messages = []
        self.calls = []
        self.uncertain = False

    def api(self, method, path, body=None):
        self.calls.append((method, path))
        if path == '/api/session/active':return {'data': self.active}
        if path.endswith('/inbox'):return {'data': list(self.inbox)}
        if '/message?' in path:return {'data': self.messages, 'cursor': {}}
        if method == 'POST':
            assert path.endswith('/prompt') and body['delivery'] == 'queue'
            notices = list(self.inbox)
            self.inbox.clear()
            self.messages = [
                dict(id='idle', type='idle', outcome='succeeded'),
                dict(id='answer', type='assistant', finish='stop', content=[dict(type='text', text='read')]),
                dict(id='own', type='user', text=body['text']),
                *notices,
            ]
            if self.uncertain:raise TimeoutError('Response lost after admission')
            return {'data': {'id': 'own'}}
        assert method == 'GET'
        return {'data': {'model': {'providerID': 'test', 'id': 'fake'}}}

    def adapter(self):
        a = OpenCodeSharedAdapter(dict(provider='opencode', request_timeout_s=1), lambda: False)
        a.session = 'ses_test';a.api = self.api
        return a


@pytest.mark.parametrize('item', [
    dict(type='user', delivery='queue'), dict(type='user', delivery='steer'),
    dict(type='move', delivery='queue'), dict(type='compaction', delivery='queue'),
    dict(type='future-kind', delivery='queue'), {},
    dict(type='synthetic', delivery='steer'), dict(type='synthetic'),
])
def test_real_or_unknown_work_blocks_dispatch_without_touching_native_queue(item):
    native = Native();native.inbox.append(item);a = native.adapter()
    before = list(native.inbox)
    with pytest.raises(RequestDeferred):a.request(dict(prompt='read inbox'))
    assert native.inbox == before
    assert all(method == 'GET' for method, _ in native.calls)
    assert not a.quarantined


def test_native_active_turn_takes_priority_over_passive_notices():
    native=Native();native.active={'ses_test': {}};a=native.adapter()
    assert a.activity() == 'busy'
    with pytest.raises(RequestDeferred):a.request(dict(prompt='read inbox'))
    assert native.inbox and all(method == 'GET' for method, _ in native.calls)
    native.active.clear()
    assert a.activity() == 'ready'
    assert a.request(dict(prompt='read inbox')) == 'read'
    assert [method for method, _ in native.calls].count('POST') == 1


def fixture(tmp_path):
    folder=tmp_path/'hub/agents/test';folder.mkdir(parents=True)
    (folder/'profile.json').write_text(json.dumps(dict(id='test',inbox='agents/test/inbox.jsonl')))
    (folder/'inbox.jsonl').write_text(json.dumps(dict(id='message',text='Untrusted peer data'))+'\n')
    return folder,InboxWatcher('test',tmp_path/'hub'),AgentStore(tmp_path/'runtime')


def test_watcher_wakes_idle_native_notice_without_manual_input_or_duplicate(tmp_path):
    folder,watcher,store=fixture(tmp_path);native=Native();a=native.adapter()
    assert watcher.tick(store,'gateway','receiving',clock=lambda:0)['state']=='notice_recorded'
    # The old bool(inbox) predicate would prevent claim forever here.
    assert native.inbox and not native.active
    assert a.activity() == 'ready'
    row=store.claim('gateway');assert row
    answer=a.request(json.loads(row['payload']))
    store.finish(row['id'],'completed',answer)
    assert not (folder/'.read_offset').exists()  # reception/dispatch do not acknowledge
    watcher.tick(store,'gateway','receiving',clock=lambda:6)
    assert store.claim('gateway') is None  # stable message ID is not dispatched twice
    assert [method for method, _ in native.calls].count('POST') == 1
    assert not native.inbox


def test_uncertain_notification_delivery_is_not_replayed(tmp_path):
    _,watcher,store=fixture(tmp_path);native=Native();native.uncertain=True;a=native.adapter()
    watcher.tick(store,'gateway','receiving',clock=lambda:0);row=store.claim('gateway')
    with pytest.raises(RequestOutcomeUnknown):a.request(json.loads(row['payload']))
    store.finish(row['id'],'unknown','No replay')
    watcher.tick(store,'gateway','receiving',clock=lambda:6)
    assert store.claim('gateway') is None
    assert a.quarantined and a.activity() == 'busy'
    assert [method for method, _ in native.calls].count('POST') == 1


def test_quarantine_is_not_cleared_by_a_passive_notification():
    native=Native();a=native.adapter();a.quarantined=True
    assert a.activity() == 'busy'
    with pytest.raises(RequestOutcomeUnknown):a.request(dict(prompt='read inbox'))
    assert native.inbox and all(method == 'GET' for method, _ in native.calls)
