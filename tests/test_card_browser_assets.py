"""Actual HTML and static routes; no control backend, service or device calls."""
from html.parser import HTMLParser
from urllib.parse import urljoin

from fastapi.testclient import TestClient

from ldcon.core.records import atomic_json
from ldcon.web.web_fleet import create_fleet_app
from ldcon.web.web_server import create_app


class LocalAssets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.paths = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        value = attributes.get('src') if tag == 'script' else attributes.get('href') if tag == 'link' else None
        if value and value.startswith('assets/'):
            self.paths.append(value)


def assert_page_assets(client, route, *, card_browser):
    page = client.get(route)
    assert page.status_code == 200
    assert page.headers['content-type'].startswith('text/html')
    parser = LocalAssets()
    parser.feed(page.text)
    assert parser.paths
    assert ('assets/card-browser.js' in parser.paths) is card_browser
    for relative in parser.paths:
        response = client.get(urljoin(route, relative))
        assert response.status_code == 200, relative
        assert response.content, relative
        if relative.endswith('.js'):
            assert 'javascript' in response.headers['content-type'], relative
        elif relative.endswith('.css'):
            assert response.headers['content-type'].startswith('text/css'), relative
    if card_browser:
        assert 'ldconCardBrowser' in client.get(urljoin(route, 'assets/card-browser.js')).text
    assert client.get(urljoin(route, 'assets/unknown-script.js')).status_code == 404
    assert client.get(urljoin(route, 'assets/web_server.py')).status_code == 404


def test_direct_laser_index_serves_every_declared_asset():
    # Static routes require no runtime creation/startup or owner access.
    app = create_app(token='asset-test')
    client = TestClient(app)
    assert_page_assets(client, '/s/asset-test/', card_browser=True)


def test_fleet_and_laser_workspaces_serve_their_own_assets_without_proxy(tmp_path):
    registry = tmp_path / 'services.json'
    atomic_json(registry, dict(services=[dict(
        id='laser', label='Synthetic laser', origin='http://127.0.0.1:1',
        state_file=str(tmp_path / 'laser' / 'web-server.json'))]))

    def forbidden_transport(*args, **kwargs):
        raise AssertionError('Static presentation routes must not access a laser backend')

    app = create_fleet_app(registry, token='a' * 32, transport=forbidden_transport)
    client = TestClient(app)
    prefix = app.state.prefix
    assert_page_assets(client, prefix + '/', card_browser=False)
    assert_page_assets(client, prefix + '/services/laser/', card_browser=True)
    assert client.get(prefix + '/services/absent/assets/card-browser.js').status_code == 404
