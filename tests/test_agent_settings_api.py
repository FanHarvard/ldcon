"""Settings routes against a filesystem-only fake runtime; no model/hardware."""
import asyncio
import inspect
import json
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from fastapi.responses import FileResponse
from starlette.routing import Match
import pytest

from ldcon.agents import agent_profiles
from ldcon.agents import notification_policy
from ldcon.web.agent_settings_api import install_routes, notification_view
from ldcon.web.web_runtime import ConflictError


ASSETS = Path(__file__).parents[1] / 'src/ldcon/web_assets'
PREFIX = '/s/test-session'


class RouteClient:
    """Invoke registered routes without the environment's blocked thread portal."""
    def __init__(self, app):
        self.app = app

    def request(self, method, path, payload=None):
        scope = dict(type='http', path=path, method=method, root_path='')
        for route in self.app.routes:
            match, child = route.matches(scope)
            if match != Match.FULL:
                continue
            kwargs = dict(child.get('path_params', {}))
            if method == 'PUT':
                async def request_json():
                    return payload
                kwargs['request'] = SimpleNamespace(json=request_json)
            try:
                result = route.endpoint(**kwargs)
                if inspect.isawaitable(result):
                    result = asyncio.run(result)
                if isinstance(result, FileResponse):
                    return SimpleNamespace(status_code=200, text=Path(result.path).read_text(),
                                           headers={'content-type': result.media_type})
                return SimpleNamespace(status_code=200, json=lambda: result)
            except HTTPException as error:
                return SimpleNamespace(status_code=error.status_code)
            except ValueError as error:
                return SimpleNamespace(status_code=409 if isinstance(error, ConflictError) else 422,
                                       json=lambda: dict(detail=str(error)))
        return SimpleNamespace(status_code=404)

    def get(self, path):
        return self.request('GET', path)

    def put(self, path, json):
        return self.request('PUT', path, json)


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.delenv('LDCON_AGENT_PROFILE_REGISTRY', raising=False)
    # Discovery is a stub too: this suite never checks actual CLI/login state.
    monkeypatch.setattr('ldcon.agents.agent_providers.catalog', lambda: [dict(
        id='codex', label='Codex', available=True, authentication='not_checked',
        model_catalog='connection_required', inference_access='not_checked')])
    app = FastAPI()
    app.state.runtime = SimpleNamespace(root=tmp_path)

    @app.exception_handler(ValueError)
    async def invalid(request, error):
        return JSONResponse(dict(detail=str(error)), status_code=409 if isinstance(error, ConflictError) else 422)

    async def body(request: Request):
        value = await request.json()
        if not isinstance(value, dict):
            raise ValueError('Expected a JSON object')
        return value

    install_routes(app, PREFIX, ASSETS, body)
    agent_profiles.install_routes(app, PREFIX, body)
    return SimpleNamespace(client=RouteClient(app), root=tmp_path, app=app)


def test_settings_page_prefixed_assets_and_closed_asset_allowlist(settings):
    client = settings.client
    page = client.get(PREFIX + '/agent-settings')
    assert page.status_code == 200 and 'text/html' in page.headers['content-type']
    assert '<html lang="en">' in page.text and 'Authentication is not checked here.' in page.text
    assert 'agent-settings-assets/agent-settings.js' in page.text
    for filename in ('agent-settings.js', 'agent-settings.css'):
        assert client.get(PREFIX + '/agent-settings-assets/' + filename).status_code == 200
    assert client.get(PREFIX + '/agent-settings-assets/agent_profiles.py').status_code == 404
    assert client.get('/agent-settings').status_code == 404
    assert client.get('/api/notification-policy').status_code == 404


def test_notification_policy_roundtrip_inheritance_and_expiry(settings):
    client = settings.client
    initial = client.get(PREFIX + '/api/notification-policy').json()
    assert initial['revision'] == 0 and initial['document'] == notification_policy.defaults()
    document = initial['document']
    document['global_policy']['preset'] = 'quiet'
    document['lasers']['laser1'] = dict(preset='standard')
    document['tasks']['active1'] = dict(laser_id='laser1', policy=dict(preset='investigation'))
    document['tasks']['expired1'] = dict(laser_id='laser1', policy=dict(preset='investigation'), expires_unix_s=1)
    document['tasks']['complete1'] = dict(laser_id='laser1', policy=dict(preset='investigation'), completed=True)
    response = client.put(PREFIX + '/api/notification-policy', json=dict(document=document, expected_revision=0))
    assert response.status_code == 200
    value = response.json()
    assert value['revision'] == 1 and 'Future notification events' in value['activation']
    assert value['resolved']['lasers']['laser1']['source'] == 'laser'
    assert value['resolved']['tasks']['active1']['effective']['preset'] == 'investigation'
    assert value['resolved']['tasks']['active1']['inherited']['preset'] == 'standard'
    assert value['resolved']['tasks']['expired1']['task_override_expired']
    assert value['resolved']['tasks']['complete1']['task_override_expired']
    assert value['resolved']['tasks']['expired1']['effective']['preset'] == 'standard'
    assert notification_policy.read_policy(settings.root) == value['document']
    # A stale browser cannot silently overwrite another operator's save.
    conflict = client.put(PREFIX + '/api/notification-policy', json=dict(document=document, expected_revision=0))
    assert conflict.status_code == 409
    assert client.get(PREFIX + '/api/notification-policy').json()['document'] == value['document']
    assert not (settings.root / 'notifications').exists()  # No dispatch/backlog mutation.


@pytest.mark.parametrize('payload', [dict(unknown=True), [],
    dict(document={'schema_version': 999}, expected_revision=0)])
def test_invalid_notification_documents_are_rejected(settings, payload):
    response = settings.client.put(PREFIX + '/api/notification-policy', json=payload)
    assert response.status_code == 422
    assert not (settings.root / 'notification-policy.json').exists()


def test_profile_table_api_roundtrip_does_not_claim_authentication(settings):
    client = settings.client
    initial = client.get(PREFIX + '/api/agent-profiles').json()
    assert initial['providers'][0]['available'] is True
    assert initial['providers'][0]['authentication'] == 'not_checked'
    assert initial['providers'][0]['inference_access'] == 'not_checked'
    document = initial['document']
    document['profiles']['analysis'] = dict(provider='codex', model='gpt-6.1-sol', effort='high', timeout_s=90,
        max_turns=2, max_input_bytes=24000, required_tools=['laser_log_read'], required_modalities=['text'])
    document['resident_profile'] = 'analysis'
    document['lasers']['laser1'] = dict(resident_profile='analysis')
    response = client.put(PREFIX + '/api/agent-profiles', json=dict(document=document, expected_revision=None))
    assert response.status_code == 200
    result = response.json()
    assert result['document'] == document and result['revision']
    assert agent_profiles.resolve(settings.root, laser_id='laser1', resident=True)['model'] == 'gpt-6.1-sol'
    assert client.put(PREFIX + '/api/agent-profiles', json=dict(document=document, expected_revision=None)).status_code == 409


def test_notification_view_uses_one_consistent_revision_snapshot(tmp_path, monkeypatch):
    calls = []
    document = notification_policy.defaults()
    document['lasers']['laser1'] = dict(preset='quiet')
    document['tasks']['task1'] = dict(laser_id='laser1', policy=dict(preset='investigation'))
    def read(root):
        calls.append(root)
        if len(calls) > 1:
            raise AssertionError('A settings view must not mix revisions during concurrent saves')
        return document
    monkeypatch.setattr(notification_policy, 'read_policy', read)
    view = notification_view(tmp_path)
    assert view['revision'] == 0 and len(calls) == 1
    assert view['resolved']['tasks']['task1']['source'] == 'task'


def test_all_new_card_guides_exist_and_do_not_claim_live_validation():
    for name in ('agent.task', 'agent.monitor', 'agent.history_review'):
        text = (ASSETS / 'cards' / (name + '.html')).read_text()
        assert '<html lang="en">' in text and name in text
        assert 'agent-settings-assets/agent-settings.css' in text
    task = (ASSETS / 'cards/agent.task.html').read_text()
    assert 'does not prove' in task and 'at least two' in task
