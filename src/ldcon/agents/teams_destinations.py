"""Private, hot-read Teams destinations. Public views never contain URL values."""
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from urllib.parse import urlsplit
import uuid

MAX_BYTES = 1024 * 1024
ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}')


class Conflict(ValueError):
    pass


def settings_path(root):
    explicit = os.environ.get('LDCON_TEAMS_DESTINATIONS')
    if explicit:
        return Path(explicit)
    root = Path(root).resolve()
    if root.parent.name in ('lasers', 'deployments'):
        return root.parent.parent / 'dashboard' / 'teams-destinations.json'
    return root / 'teams-destinations.json'


def validate_url(value):
    message = 'Use a Microsoft Teams/Workflows HTTPS webhook URL'
    if not isinstance(value, str) or not 1 <= len(value) <= 8192 or any(ord(c) <= 32 or ord(c) >= 127 for c in value):
        raise ValueError(message)
    try:
        u = urlsplit(value)
        hosts = ('webhook.office.com', 'logic.azure.com', 'environment.api.powerplatform.com')
        valid = (u.scheme == 'https' and not u.username and not u.password and not u.fragment
                 and u.port in (None, 443) and u.path not in ('', '/')
                 and any(u.hostname == h or (u.hostname or '').endswith('.' + h) for h in hosts))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError(message)
    return value


def _route(route, global_scope=False):
    modes = ('custom', 'disabled', 'legacy') if global_scope else ('custom', 'disabled', 'inherit')
    if not isinstance(route, dict) or set(route) != {'mode', 'webhook_url'} or route['mode'] not in modes:
        raise ValueError('Invalid Teams destination mode')
    if route['mode'] == 'custom':
        validate_url(route['webhook_url'])
    elif route['webhook_url'] != '':
        raise ValueError('Only a custom destination can store a URL')
    return route


def read(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return dict(schema_version=1, revision=None, default=dict(mode='legacy', webhook_url=''), lasers={})
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_BYTES or info.st_mode & 0o077:
            raise ValueError('Teams destination storage must be a private regular file')
        value = json.loads(stream.read(MAX_BYTES + 1))
    if (not isinstance(value, dict) or set(value) != {'schema_version', 'revision', 'default', 'lasers'}
            or value['schema_version'] != 1 or not isinstance(value['revision'], str)
            or not re.fullmatch(r'[0-9a-f]{32}', value['revision']) or not isinstance(value['lasers'], dict)):
        raise ValueError('Invalid Teams destination storage')
    _route(value['default'], True)
    for laser, route in value['lasers'].items():
        if not ID.fullmatch(laser):
            raise ValueError('Invalid laser ID')
        _route(route)
    return value


def public(document, inventory):
    def safe(route):
        return dict(mode=route['mode'], configured=route['mode'] == 'custom')
    rows = []
    for laser in inventory:
        route = document['lasers'].get(laser['id'], dict(mode='inherit', webhook_url=''))
        effective = document['default'] if route['mode'] == 'inherit' else route
        rows.append(dict(id=laser['id'], name=laser['name'], **safe(route),
                         effective_mode=effective['mode'], effective_configured=effective['mode'] == 'custom'))
    return dict(revision=document['revision'], default=safe(document['default']), lasers=rows)


def save(path, payload, inventory, *, allowed_lasers=None):
    """Compare-and-swap one scope. Blank custom URL retains that scope's secret."""
    if (not isinstance(payload, dict) or set(payload) - {'expected_revision', 'laser_id', 'mode', 'webhook_url'}
            or not {'expected_revision', 'laser_id', 'mode'} <= set(payload)):
        raise ValueError('Expected revision, laser_id, mode and optional webhook_url')
    laser = payload['laser_id']
    known = {row['id'] for row in inventory}
    if laser is not None and (not isinstance(laser, str) or laser not in known):
        raise ValueError('Unknown laser')
    if allowed_lasers is not None and laser not in allowed_lasers:
        raise PermissionError('This page can edit only its own laser destination')
    url = payload.get('webhook_url', '')
    if not isinstance(url, str):
        raise ValueError('Webhook URL must be text')
    if url:
        validate_url(url)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path) + '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        document = read(path)
        if payload['expected_revision'] != document['revision']:
            raise Conflict('Destinations changed. Reload before saving.')
        old = document['default'] if laser is None else document['lasers'].get(laser, dict(mode='inherit', webhook_url=''))
        mode = payload['mode']
        if url and mode != 'custom':
            raise ValueError('Choose a custom destination before entering a URL')
        route = _route(dict(mode=mode, webhook_url=(url or old['webhook_url']) if mode == 'custom' else ''), laser is None)
        if laser is None:
            document['default'] = route
        elif mode == 'inherit':
            document['lasers'].pop(laser, None)
        else:
            document['lasers'][laser] = route
        document['revision'] = uuid.uuid4().hex
        fd, temporary = tempfile.mkstemp(prefix='.teams-destinations-', dir=path.parent)
        try:
            with os.fdopen(fd, 'w') as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(document, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            Path(temporary).unlink(missing_ok=True)
    return public(document, inventory)


def resolve(root, laser_id, environment_reference):
    """Resolve just before sending, including old queued cards. Never requeue."""
    document = read(settings_path(root)) if root is not None else dict(default=dict(mode='legacy'), lasers={})
    route = document['lasers'].get(laser_id, dict(mode='inherit', webhook_url=''))
    if route['mode'] == 'inherit':
        route = document['default']
    if route['mode'] == 'disabled':
        return 'disabled', ''
    if route['mode'] == 'legacy':
        url = os.environ.get(environment_reference, '')
        return ('ready', validate_url(url)) if url else ('missing', '')
    return 'ready', route['webhook_url']
