"""Only the target contract and job analysis may cross the public fleet proxy."""
import asyncio
import json

from httpx import ASGITransport, AsyncClient
import pytest

from ldcon.web.web_fleet import create_fleet_app
from .test_dlcpro_offline import no_network


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    state = tmp_path / 'web-server.json'
    state.write_text(json.dumps(dict(path='/s/' + 'u' * 32 + '/')))
    registry = tmp_path / 'services.json'
    registry.write_text(json.dumps(dict(services=[dict(id='thf685', label='ThF685',
        origin='http://127.0.0.1:12345', state_file=str(state))])))
    calls = []
    failure = [False]

    def transport(service, method, route, query='', body=None):
        calls.append(dict(service=service.id, method=method, route=route, query=query, body=body))
        if failure[0]:
            raise TimeoutError('Synthetic lost reply')
        return 200, 'application/json', b'{"state":"accepted"}'

    async def inline(function, *args):
        return function(*args)

    monkeypatch.setattr(asyncio, 'to_thread', inline)
    app = create_fleet_app(registry, token='f' * 32, transport=transport)
    base = '/s/' + 'f' * 32 + '/services/thf685/api/'

    def request(method, route, *, body=None, headers=None):
        async def run():
            async with AsyncClient(transport=ASGITransport(app=app), base_url='http://offline') as client:
                return await client.request(method, base + route, json=body, headers=headers or {})
        return asyncio.run(run())

    return request, calls, failure


@pytest.mark.parametrize('route', ['lasers/thf685/target', 'lasers/thf685/target/changes?request_id=shot%3A001',
                                  'lasers/thf685/target/changes/change-001', 'jobs/shot-001/analysis'])
def test_exact_target_and_job_analysis_reads_are_forwarded(fleet, route):
    request, calls, _ = fleet
    response = request('GET', route)
    assert response.status_code == 200 and len(calls) == 1
    assert calls[0]['route'] == route.split('?')[0]
    if 'request_id' in route:
        assert calls[0]['query'] == 'request_id=shot%3A001'


@pytest.mark.parametrize('route', ['lasers/thf685/target/changes', 'lasers/thf685/target/changes/change-001/cancel'])
def test_target_mutations_preserve_payload_and_existing_web_gate(fleet, route):
    request, calls, _ = fleet
    body = dict(target_frequency_hz=3e14, request_id='shot-001', timeout_s=20)
    assert request('POST', route, body=body).status_code == 403
    assert request('POST', route, body=body, headers={'X-LDCon-Client': 'labscript'}).status_code == 403
    assert request('POST', route, body=body, headers={
        'X-LDCon-Client': 'web', 'Origin': 'http://untrusted.invalid'}).status_code == 403
    assert request('POST', route, body=body, headers={
        'X-LDCon-Client': 'web', 'Sec-Fetch-Site': 'cross-site'}).status_code == 403
    assert not calls
    response = request('POST', route, body=body, headers={'X-LDCon-Client': 'web'})
    assert response.status_code == 200 and len(calls) == 1
    assert json.loads(calls[0]['body']) == body


@pytest.mark.parametrize('method,route,status', [
    ('GET', 'lasers/thf685/target/changes?request_id=a&request_id=b', 422),
    ('GET', 'lasers/thf685/target/changes?request_id=..%2Fescape', 422),
    ('GET', 'lasers/thf685/target/changes?request_id=', 422),
    ('GET', 'lasers/thf685/target/changes?request_id=' + 'a' * 129, 422),
    ('GET', 'lasers/thf685/target?request_id=shot', 422),
    ('GET', 'lasers/thf685/target/changes/change-001?request_id=shot', 422),
    ('GET', 'jobs/shot-001/analysis?path=secret', 422),
    ('GET', 'jobs/shot-001?request_id=shot', 422),
    ('GET', 'jobs/shot-001/analysis/anything', 404),
    ('GET', 'lasers/thf685/target/changes/change-001/cancel', 404),
    ('POST', 'lasers/thf685/target/changes?limit=1', 422),
    ('POST', 'lasers/thf685/target/changes/change-001/cancel?limit=1', 422),
    ('POST', 'lasers/thf685/target', 404),
    ('POST', 'jobs/shot-001/analysis', 404),
    ('PUT', 'lasers/thf685/target/changes', 404),
    ('POST', 'rpc', 404),
])
def test_no_other_routes_or_query_arguments_are_added(fleet, method, route, status):
    request, calls, _ = fleet
    assert request(method, route, body={}, headers={'X-LDCon-Client': 'web'}).status_code == status
    assert not calls


def test_lost_mutation_reply_is_unknown_and_never_retried(fleet):
    request, calls, failure = fleet
    failure[0] = True
    response = request('POST', 'lasers/thf685/target/changes',
                       body={'target_frequency_hz': 3e14, 'request_id': 'shot-001'},
                       headers={'X-LDCon-Client': 'web'})
    assert response.status_code == 503 and 'unknown' in response.json()['detail']
    assert len(calls) == 1
