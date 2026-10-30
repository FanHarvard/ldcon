"""No real messages: validate secret boundaries, scoped writes and live routing."""
import asyncio
import json
import os
from pathlib import Path

import httpx
import pytest

from ldcon.agents import teams_destinations as d
from ldcon.agents.teams_card import deliver
from ldcon.web.web_fleet import create_fleet_app

URL = 'https://example.environment.api.powerplatform.com/workflows/test/invoke?sig=SECRET_ONE'
SECOND = 'https://example.webhook.office.com/webhook/test/SECRET_TWO'
LASERS = [dict(id='sr422', name='Sr422'), dict(id='sr1092', name='Sr1092')]


def save(path, mode, laser=None, url='', revision='current'):
    rev = d.read(path)['revision'] if revision == 'current' else revision
    return d.save(path, dict(expected_revision=rev, laser_id=laser, mode=mode, webhook_url=url), LASERS)


def test_precedence_blank_keep_clear_and_future_laser(tmp_path):
    path = d.settings_path(tmp_path)
    first = save(path, 'custom', url=URL)
    assert 'SECRET' not in json.dumps(first)
    assert path.stat().st_mode & 0o777 == 0o600
    assert d.resolve(tmp_path, 'sr422', '') == ('ready', URL)
    save(path, 'custom', 'sr422', SECOND)
    save(path, 'custom', url=SECOND)
    assert d.resolve(tmp_path, 'future-laser', '') == ('ready', SECOND)
    save(path, 'custom', 'sr422')  # preserve own URL, not inherit
    assert d.resolve(tmp_path, 'sr422', '') == ('ready', SECOND)
    save(path, 'disabled')
    assert d.resolve(tmp_path, 'sr422', '') == ('ready', SECOND)
    assert d.resolve(tmp_path, 'sr1092', '') == ('disabled', '')
    save(path, 'inherit', 'sr422')
    assert d.resolve(tmp_path, 'sr422', '') == ('disabled', '')
    assert 'sr422' not in d.read(path)['lasers']
    with pytest.raises(ValueError):save(path, 'custom')  # cleared URL not resurrected


def test_old_environment_fallback_and_invalid_storage_does_not_fall_back(tmp_path, monkeypatch):
    monkeypatch.setenv('LDCON_TEAMS_TEST', URL)
    assert d.resolve(tmp_path, 'sr422', 'LDCON_TEAMS_TEST') == ('ready', URL)
    path = d.settings_path(tmp_path)
    save(path, 'disabled', 'sr422')
    assert d.resolve(tmp_path, 'sr422', 'LDCON_TEAMS_TEST') == ('disabled', '')
    path.write_text('{bad')
    with pytest.raises(ValueError):d.resolve(tmp_path, 'sr422', 'LDCON_TEAMS_TEST')


def test_revision_conflict_unknown_laser_and_no_partial_save(tmp_path):
    path = d.settings_path(tmp_path)
    save(path, 'custom', url=URL)
    original = path.read_bytes()
    with pytest.raises(d.Conflict):save(path, 'disabled', revision=None)
    with pytest.raises(ValueError):save(path, 'custom', 'unknown', SECOND)
    with pytest.raises(ValueError):save(path, 'custom', 'sr422', 'https://untrusted.invalid/?secret=S')
    assert path.read_bytes() == original


@pytest.mark.parametrize('url', ['http://example.webhook.office.com/x', 'https://webhook.office.com.attacker.invalid/x',
    'https://webhook.office.com:SECRET/x', 'https://SECRET@webhook.office.com/x',
    'https://webhook.office.com/x#SECRET', 'https://webhook.office.com/x\nSECRET', 'https://127.0.0.1/SECRET',
    'https://webhook.office.com/x?x='+'a'*8192, 'https://[SECRET/'])
def test_url_validation_errors_do_not_reflect_input(url):
    with pytest.raises(ValueError) as error:d.validate_url(url)
    assert 'SECRET' not in str(error.value)


def test_private_storage_and_symlinks(tmp_path):
    path=d.settings_path(tmp_path);save(path,'custom',url=URL)
    path.chmod(0o644)
    with pytest.raises(ValueError):d.read(path)
    path.chmod(0o600);link=tmp_path/'link';link.symlink_to(path)
    with pytest.raises(OSError):d.read(link)


def test_sender_hot_reads_overrides_and_disabled_without_replay(tmp_path, monkeypatch):
    path=d.settings_path(tmp_path);calls=[]
    monkeypatch.setattr('ldcon.agents.fallback_targets.send_teams', lambda url,text:(calls.append((url,text)) or ('sent','ok')))
    p=dict(webhook_env='LDCON_TEAMS_TEST',message='Short summary',sample_interval_s=1.)
    save(path,'custom',url=URL)
    assert deliver(p,dict(source='notification_card'),root=tmp_path,laser_id='sr422')[0]=='sent'
    save(path,'custom','sr422',SECOND)
    assert deliver(p,dict(source='notification_card'),root=tmp_path,laser_id='sr422')[0]=='sent'
    save(path,'disabled','sr422')
    assert deliver(p,{},root=tmp_path,laser_id='sr422')[0]=='cancelled'
    assert calls == [(URL,'Short summary'),(SECOND,'Short summary')]


def test_fleet_api_global_local_scope_and_no_upstream_or_secret_echo(tmp_path):
    registry=tmp_path/'services.json'
    registry.write_text(json.dumps(dict(services=[dict(id=row['id'],label=row['name'],origin='http://127.0.0.1:9999',state_file=str(tmp_path/(row['id']+'.json')),lasers=[row]) for row in LASERS])))
    def forbidden(*a,**kw):raise AssertionError('No upstream or hardware operation')
    app=create_fleet_app(registry,token='x'*32,transport=forbidden)
    prefix=app.state.prefix
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as c:
            endpoint=prefix+'/api/teams-destinations'
            assert (await c.get('/api/teams-destinations')).status_code==404
            initial=(await c.get(endpoint)).json();assert initial['revision'] is None
            payload=dict(laser_id=None,expected_revision=None,mode='custom',webhook_url=URL)
            assert (await c.put(endpoint,json=payload)).status_code==403
            headers={'X-LDCon-Client':'web'}
            assert (await c.put(endpoint,json=payload,headers={**headers,'Origin':'http://elsewhere'})).status_code==403
            result=await c.put(endpoint,json=payload,headers=headers)
            assert result.status_code==200 and 'SECRET' not in result.text and 'webhook_url' not in result.text
            rev=result.json()['revision']
            assert (await c.put(endpoint,json=payload,headers=headers)).status_code==409
            local=prefix+'/services/sr422/api/teams-destinations'
            assert len((await c.get(local)).json()['lasers'])==1
            payload.update(expected_revision=rev)
            assert (await c.put(local,json=payload,headers=headers)).status_code==403
            payload.update(laser_id='sr1092')
            assert (await c.put(local,json=payload,headers=headers)).status_code in (403,422)
            payload.update(laser_id='sr422',webhook_url=SECOND)
            result=await c.put(local,json=payload,headers=headers)
            assert result.status_code==200 and 'SECRET' not in result.text
            payload.update(expected_revision=result.json()['revision'],webhook_url='https://webhook.office.com:SECRET/foo')
            bad=await c.put(local,json=payload,headers=headers)
            assert bad.status_code==422 and 'SECRET' not in bad.text
            assert (await c.get(prefix+'/assets/teams-settings.js')).status_code==200
            assert 'teams-settings' in (await c.get(prefix+'/')).text
    asyncio.run(run())


def test_local_api_can_only_write_its_laser(tmp_path):
    from fastapi import FastAPI
    from types import SimpleNamespace
    from ldcon.web.teams_destinations_api import install_local
    app=FastAPI()
    app.state.runtime=SimpleNamespace(root=tmp_path,set_library=lambda:[LASERS[0]])
    install_local(app,'/test')
    path=d.settings_path(tmp_path);save(path,'custom',url=URL)
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as c:
            endpoint='/test/api/teams-destinations'
            view=(await c.get(endpoint)).json()
            payload=dict(expected_revision=view['revision'],laser_id=None,mode='disabled')
            assert (await c.put(endpoint,json=payload)).status_code==403
            payload['laser_id']='sr422'
            result=await c.put(endpoint,json=payload)
            assert result.status_code==200
            assert d.resolve(tmp_path,'sr422','')==('disabled','')
            assert d.resolve(tmp_path,'sr1092','')==('ready',URL)
    asyncio.run(run())


def test_dispatcher_routes_by_job_identity_not_payload(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace
    from ldcon.agents.card_fallback import NotificationDispatcher
    calls=[]
    def send(p,evidence,**kw):
        calls.append(kw)
        return 'cancelled','Disabled'
    monkeypatch.setattr('ldcon.agents.teams_card.deliver',send)
    @contextmanager
    def connect():
        yield SimpleNamespace(execute=lambda *a:None)
    store=SimpleNamespace(connect=connect,clock=lambda:0,audit=lambda *a:None)
    row=dict(payload=json.dumps(dict(_target='teams_card',_teams_parameters={},notification_summary=dict(laser_id='sr1092'))),
             job='test',card='teams',id='delivery',attempts=0)
    NotificationDispatcher(tmp_path,store).deliver_standard(row,dict(laser_id='sr422'),{})
    assert calls==[dict(root=tmp_path,laser_id='sr422')]
