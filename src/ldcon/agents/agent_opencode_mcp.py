"""Gateway-local MCP configuration that survives OpenCode location recreation.

The experimental MCP PUT endpoint is only a location-lifetime overlay. Do not
use it as the source of truth or write gateway authority into shared config.
"""
import json
import sys
import time
import urllib.parse

from ldcon.core.paths import source_root


def environment(parameters, base):
    """Configure this server process only, never another laser or ACP task."""
    result = dict(base)
    if not parameters.get('tools_context'):
        return result
    document = json.loads(result.get('OPENCODE_CONFIG_CONTENT') or '{}')
    if not isinstance(document, dict):
        raise ValueError('OpenCode inline configuration must be an object')
    mcp = document.setdefault('mcp', {})
    # OpenCode v2 accepts both forms. Normalize an inherited v1 server map.
    if not isinstance(mcp, dict):
        raise ValueError('OpenCode MCP configuration must be an object')
    if 'servers' not in mcp:
        mcp = document['mcp'] = {'servers': mcp}
    servers = mcp['servers']
    if not isinstance(servers, dict):
        raise ValueError('OpenCode MCP servers must be an object')
    servers['ldcon'] = dict(
        type='local',
        command=[sys.executable, '-m', 'ldcon.agents.agent_tools', parameters['tools_context']],
        cwd=parameters['workspace'],
        environment={'PYTHONPATH': str(source_root())},
    )
    experimental = document.setdefault('experimental', {})
    if not isinstance(experimental, dict):
        raise ValueError('OpenCode experimental configuration must be an object')
    # This server exists for the lifetime of a resident, even between turns.
    # Durable MCP config additionally covers explicit native location reloads.
    experimental['location_idle_timeout'] = False
    result['OPENCODE_CONFIG_CONTENT'] = json.dumps(document)
    return result


def wait_connected(api, parameters, cancel, alive, timeout=15):
    """Read readiness; never recreate a disabled server or replay a tool call."""
    if not parameters.get('tools_context'):
        return
    query = urllib.parse.urlencode({'location[directory]': parameters['workspace']})
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cancel() or not alive():
            raise RuntimeError('Gateway stopped before scoped tool readiness')
        rows = api('GET', '/api/mcp?' + query)['data']
        entry = next((row for row in rows if row.get('name') == 'ldcon'), None)
        status = entry.get('status', {}).get('status') if entry else None
        if status == 'connected':
            return
        if status not in (None, 'pending'):
            raise RuntimeError('OpenCode scoped LDCon tool server is not connected; inspect native MCP status')
        time.sleep(.1)
    raise RuntimeError('OpenCode scoped LDCon tool readiness timed out; no request delivered')
