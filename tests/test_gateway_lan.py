import base64
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from ldcon.web.gateway_lan import allowed, config, create_app, upstream

CFG={'host':'192.168.1.5','networks':['192.168.1.0/24'],'lasers':{'sr422':8091,'sr1092':8092,'thf685':8093}}
ORIGIN='http://192.168.1.5:8091'
RECORD={'url':'http://127.0.0.1:41000','password':'PRIVATE','session_id':'ses_test'}


def client(handler, resolver=lambda laser: RECORD, peer='192.168.1.20'):
    app=create_app(CFG,'sr422',resolver=resolver,transport=httpx.MockTransport(handler))
    return TestClient(app,base_url=ORIGIN,client=(peer,1234))


def test_registry_rejects_wildcard_public_and_ambiguous(tmp_path):
    p=tmp_path/'cfg.json'
    for patch in ({'host':'0.0.0.0'},{'host':'140.247.108.25'}, {'networks':['0.0.0.0/0']}, {'lasers':{'a':8091,'b':8091}}):
        p.write_text(json.dumps({**CFG,**patch}))
        with pytest.raises(ValueError):config(p)
    p.write_text(json.dumps(CFG));assert config(p)==CFG


@pytest.mark.parametrize('peer',['140.247.108.20','192.168.2.20','127.0.0.1'])
def test_external_or_unconfigured_peer_rejected(peer):
    with client(lambda r:pytest.fail('Must not contact gateway'),peer=peer) as c:
        assert c.get('/').status_code==403


def test_spoofed_host_forwarded_headers_and_cross_origin_rejected():
    with client(lambda r:pytest.fail('Must not contact gateway')) as c:
        assert c.get('/',headers={'host':'evil.example'}).status_code==403
        assert c.post('/api/session/any/prompt',headers={'Origin':'http://evil.example'}).status_code==403
        assert c.post('/api/session/any/prompt').status_code==403
        assert c.get('/',headers={'Sec-Fetch-Site':'cross-site'}).status_code==403
    scope={'client':('140.247.108.20',1),'server':('192.168.1.5',8091)}
    assert not allowed(scope,{'host':'192.168.1.5:8091','x-forwarded-for':'192.168.1.20'},CFG,8091)
    scope={'client':('192.168.1.20',1),'server':('140.247.108.25',8091)}
    assert not allowed(scope,{'host':'192.168.1.5:8091'},CFG,8091)


def test_proxy_auth_is_private_and_post_is_sent_once():
    calls=[]
    def handler(r):
        calls.append(r)
        assert r.headers['authorization']=='Basic '+base64.b64encode(b'opencode:PRIVATE').decode()
        assert 'cookie' not in r.headers and 'x-forwarded-for' not in r.headers
        assert r.headers['origin']=='http://127.0.0.1:41000'
        assert r.content==b'{"text":"hello"}'
        return httpx.Response(200,stream=httpx.ByteStream(b'{"data":{"id":"one"}}'),headers={'set-cookie':'secret=hidden','content-type':'application/json'})
    with client(handler) as c:
        r=c.post('/api/session/ses_test/prompt',content='{"text":"hello"}',headers={'Origin':ORIGIN,'Cookie':'other-laser=bad','X-Forwarded-For':'evil'})
        assert r.status_code==200 and r.json()['data']['id']=='one'
        assert 'set-cookie' not in r.headers and 'PRIVATE' not in r.text
    assert len(calls)==1


def test_unknown_delivery_is_not_replayed():
    calls=[]
    def handler(r):calls.append(r);raise httpx.ReadError('private upstream details',request=r)
    with client(handler) as c:
        r=c.post('/api/session/ses_test/prompt',headers={'Origin':ORIGIN})
        assert r.status_code==502 and 'may have been delivered' in r.text
        assert 'private upstream details' not in r.text
    assert len(calls)==1


def test_gateway_restart_resolves_new_port_each_request():
    calls=[];records=[RECORD,{**RECORD,'url':'http://127.0.0.1:42000'}]
    def resolve(laser):assert laser=='sr422';return records.pop(0)
    def handler(r):calls.append(r.url.port);return httpx.Response(200,stream=httpx.ByteStream(b'ok'))
    with client(handler,resolver=resolve) as c:
        assert c.get('/').text=='ok';assert c.get('/').text=='ok'
    assert calls==[41000,42000]


def test_absent_gateway_has_actionable_page_and_no_start():
    def resolve(laser):raise RuntimeError('internal credential must not leak')
    with client(lambda r:pytest.fail('Must not forward'),resolver=resolve) as c:
        r=c.get('/',headers={'Accept':'text/html'})
        assert r.status_code==503 and 'sr422 gateway unavailable' in r.text
        assert 'internal credential' not in r.text


def test_event_stream_and_external_redirect():
    def handler(r):
        if r.url.path=='/events':return httpx.Response(200,stream=httpx.ByteStream(b'data: one\n\ndata: two\n\n'),headers={'content-type':'text/event-stream'})
        return httpx.Response(302,headers={'location':'https://evil.example/'})
    with client(handler) as c:
        assert c.get('/events').text=='data: one\n\ndata: two\n\n'
        assert c.get('/').status_code==502


@pytest.mark.parametrize('url',['http://evil:123','http://127.0.0.1:123/path','http://user@127.0.0.1:123','https://127.0.0.1:123'])
def test_resolver_cannot_select_external_upstream(url):
    with pytest.raises(ValueError):upstream({**RECORD,'url':url})


def test_browser_root_opens_exact_shared_session():
    with client(lambda r:httpx.Response(200,stream=httpx.ByteStream(b"native UI"))) as c:
        r=c.get('/',headers={'Accept':'text/html'},follow_redirects=False)
        key=base64.urlsafe_b64encode(ORIGIN.encode()).decode().rstrip('=')
        assert r.status_code==307 and r.headers['location']=='/server/'+key+'/session/ses_test'

def test_fleet_links_are_registered_and_lan_only(tmp_path):
    from ldcon.web.web_fleet import create_fleet_app
    registry=tmp_path/'services.json';registry.write_text(json.dumps({'services':[]}))
    (tmp_path/'gateway-lan.json').write_text(json.dumps(CFG))
    app=create_fleet_app(registry,token='x'*32)
    with TestClient(app,base_url='http://192.168.1.5:8086',client=('192.168.1.20',1234)) as c:
        for laser,port in CFG['lasers'].items():
            r=c.get('/gateway-chat/'+laser,follow_redirects=False)
            assert r.status_code==307 and r.headers['location']==f'http://192.168.1.5:{port}/'
        assert c.get('/gateway-chat/unknown').status_code==503
    with TestClient(app,base_url='http://192.168.1.5:8086',client=('140.247.108.20',1234)) as c:
        assert c.get('/gateway-chat/sr422').status_code==403
