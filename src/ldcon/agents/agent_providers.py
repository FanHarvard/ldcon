"""Resident provider discovery without authentication or model requests."""
import os
from pathlib import Path
import re
import shutil
import uuid

PROVIDERS = {
    'claude': dict(label='Claude Code', transport='stream-json', remote_control=True),
    'codex': dict(label='Codex', transport='app-server', remote_control=True),
    'opencode': dict(label='OpenCode', transport='HTTP (resident), ACP (task)', remote_control=False),
    'deepseek_harness': dict(label='DeepSeek Harness', transport='ACP', remote_control=False),
}


def session_id(provider, value):
    if provider not in PROVIDERS or not isinstance(value, str):
        raise ValueError('Invalid agent provider/session')
    if not value:
        return value
    if provider in ('opencode', 'deepseek_harness'):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,159}', value):
            raise ValueError('Invalid ACP session ID')
    else:
        try:
            if str(uuid.UUID(value)) != value:
                raise ValueError
        except ValueError:
            raise ValueError('Expected canonical session UUID') from None
    return value


def executable(provider):
    if provider not in PROVIDERS:
        raise ValueError('Unknown agent provider')
    configured = os.environ.get('LDCON_' + provider.upper() + '_BIN')
    name = 'dsh' if provider == 'deepseek_harness' else provider
    binary = shutil.which(configured or name)
    if not binary and not configured:
        candidates = [Path.home() / '.local/bin' / name,
                      Path.home() / '.local/share/ldcon-agent-tools/bin' / name]
        if provider == 'opencode':
            candidates.append(Path.home() / '.opencode/bin/opencode')
        binary = next((str(p) for p in candidates if p.is_file() and os.access(p, os.R_OK | os.X_OK)), None)
    return str(Path(binary).absolute()) if binary and Path(binary).is_file() and os.access(binary, os.R_OK | os.X_OK) else None


def catalog():
    """Availability is executable access by this service user, not login proof."""
    return [dict(id=name, **info, available=bool(executable(name)),
                 environment='LDCON_' + name.upper() + '_BIN', authentication='not_checked',
                 model_catalog='connection_required', inference_access='not_checked')
            for name, info in PROVIDERS.items()]
