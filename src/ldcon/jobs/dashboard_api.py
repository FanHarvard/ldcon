"""Local dashboard HTTP client for cards that must go through the live-change API (configuration save).

The worker reaches its own laser's dashboard through ``web-server.json`` of the deployment state directory
(``jobs/<laser>/<job>`` -> state dir). Only the configuration GET/PUT are wrapped; nothing else."""
import json
from pathlib import Path
from urllib.request import Request, build_opener, ProxyHandler
from urllib.error import HTTPError

from ldcon.core.contracts import DeviceError


def state_dir_of(directory):
    directory = Path(directory)
    return directory.parent.parent if directory.parent.name == 'jobs' else directory.parent.parent.parent


class Dashboard:
    def __init__(self, base, *, client='web', timeout_s=30.):
        # The base ends with '/': web-server.json's path is the token prefix '/s/<token>/' and requests append
        # 'api/...'. (2026-10-22 live 404: rstrip('/') produced '/s/<token>api/configuration/...'.)
        self.base, self.client, self.timeout_s = base.rstrip('/') + '/', client, timeout_s
        self.http = build_opener(ProxyHandler({}))

    @classmethod
    def from_job_directory(cls, directory):
        return cls.from_state_dir(state_dir_of(directory))

    @classmethod
    def from_state_dir(cls, state):
        state = Path(state)
        try:
            server = json.loads((state / 'web-server.json').read_text())
        except (OSError, ValueError) as error:
            raise DeviceError('Dashboard state file unavailable: ' + str(error)) from error
        host = '127.0.0.1'
        prefix = server.get('path') or '/'
        if not prefix.startswith('/'):
            prefix = '/' + prefix
        return cls('http://%s:%d%s' % (host, int(server['port']), prefix))

    def _request(self, method, path, body=None, headers=None):
        request = Request(self.base + path, data=json.dumps(body).encode() if body is not None else None, method=method,
                          headers={'Content-Type': 'application/json', 'X-LDCon-Client': self.client, **(headers or {})})
        try:
            with self.http.open(request, timeout=self.timeout_s) as response:
                return json.load(response)
        except HTTPError as error:
            detail = error.read().decode(errors='replace')[:500]
            raise DeviceError('Dashboard %s %s -> HTTP %d: %s' % (method, path, error.code, detail)) from error

    def get_configuration(self, kind, name):
        return self._request('GET', 'api/configuration/%s/%s' % (kind, name))

    def put_configuration(self, kind, name, text, expected_revision, *, actor='card', maintainer_reference=None, approval_id=None):
        body = dict(yaml=text, expected_revision=expected_revision)
        if maintainer_reference:
            body['maintainer_reference'] = maintainer_reference
        if approval_id:
            body['approval_id'] = approval_id
        return self._request('PUT', 'api/configuration/%s/%s' % (kind, name), body, headers={'X-LDCon-Actor': actor})
