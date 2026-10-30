"""Laser-scoped owner mode and description reads; no connection, lease or hardware command."""
import time
import json
import os
from urllib.request import Request, build_opener, ProxyHandler

from ldcon.core.instrument_client import validate_endpoint
from ldcon.agents.agent_cards import NoRedirect

KINDS = ('ecdl.rig.owner', 'esp32servocon.native_current.owner', 'dlcpro.laser.owner')


def _send(laser, values, what, limit=65536):
    devices = [v for v in laser.get('devices', {}).values() if v.get('type') in KINDS]
    if len(devices) != 1:
        raise ValueError('Exactly one laser owner is required for '+what)
    device = devices[0]; connection = device['connection']
    endpoint = validate_endpoint(connection['endpoint'])
    if device['type'] == KINDS[1]:
        payload = dict(device_id=connection['device_id'], **values)
    elif device['type'] == KINDS[2]:
        if connection['laser_id'] != laser['id']:
            raise ValueError('DLC owner connection belongs to another laser')
        payload = dict(laser_id=connection['laser_id'], **values)
    elif connection.get('instrument'):
        payload = dict(action='ecdl', instrument=connection['instrument'], request=dict(values))
    else:
        payload = dict(values)
    headers = {'Content-Type': 'application/json'}
    token = os.environ.get('LDCON_INSTRUMENT_TOKEN')
    if token:headers['Authorization'] = 'Bearer '+token
    query = Request(endpoint+'/rpc', method='POST', data=json.dumps(payload, allow_nan=False).encode(), headers=headers)
    with build_opener(ProxyHandler({}), NoRedirect()).open(query, timeout=5.) as response:
        raw = response.read(limit+1)
    if len(raw)>limit:raise ValueError(what[:1].upper()+what[1:]+' response exceeds limit')
    reply = json.loads(raw)
    if not isinstance(reply, dict) or reply.get('ok') is not True:
        raise ValueError('Owner rejected '+what+' request; inspect owner audit')
    return reply.get('result')


def request(laser, action='operating_mode_status', **values):
    if action not in ('operating_mode_status', 'operating_mode_change'):
        raise ValueError('Unsupported operating mode action')
    result = _send(laser, dict(action=action, **values), 'operating mode')
    state = result.get('current_operating_mode') if action=='operating_mode_change' and isinstance(result, dict) else result
    if not isinstance(state, dict) or state.get('laser_id') != laser['id']:
        raise ValueError('Operating mode belongs to another laser')
    if state.get('mode') not in ('measurement', 'characterization') or type(state.get('revision')) is not int:
        raise ValueError('Owner operating mode is unavailable')
    return result


def describe(laser):
    """Lease-less ECDL rig owner ``describe``: adopted device revision and runtime-adjustment envelope."""
    rigs = [v for v in laser.get('devices', {}).values() if v.get('type') == KINDS[0]]
    if len(rigs) != 1:raise ValueError('Exactly one ECDL rig owner is required for describe')
    result = _send(dict(laser, devices=dict(rig=rigs[0])), dict(action='describe'), 'owner describe', limit=2**20)
    if not isinstance(result, dict) or result.get('laser_id') != laser['id']:
        raise ValueError('Owner description belongs to another laser')
    return result


def observation(laser):
    """Audit unavailable owners honestly; this never grants adjustment authority."""
    start = time.time()
    try:
        result = dict(status='observed', **request(laser))
    except (OSError, ValueError, KeyError, TypeError) as error:
        # Exceptions may contain an endpoint. Do not expose connection details.
        result = dict(status='unavailable', laser_id=laser['id'], error_type=type(error).__name__,
                      reason='No current operating mode was obtained from this laser owner')
    return dict(result, read_started_unix_s=start, read_finished_unix_s=time.time())


def revision(value):
    if not isinstance(value, dict) or value.get('mode') not in ('measurement', 'characterization'):
        raise ValueError('Current owner operating mode is required for autonomous adjustment')
    number = value.get('revision')
    if type(number) is not int or number < 0:
        raise ValueError('Current owner operating mode revision is required')
    return number


def instructions(value):
    revision(value)
    common = (' Current owner operating mode is '+value['mode']+', revision '+str(value['revision'])+'. '
              'This owner-held mode controls exploration intent. Never change the mode yourself. '
              'Physical safety limits, calibration, freshness checks, foreground priority, trial budget and '
              'checkpoint restoration rules are unchanged. A mode is not authority to invent new tools or bounds. ')
    if value['mode'] == 'characterization':
        return common + ('The laser is being characterized, not used for measurement. Proactively test useful '
            'hypotheses within the admitted envelope, including increases and decreases where evidence warrants. '
            'An uncertain benefit is a reason for a bounded informative comparison, not by itself a reason to do nothing. '
            'Substantial state changes or mode hops may be acceptable only through supported recovery-capable procedures; '
            'a hop is not automatically an experiment failure. Plan recovery and observe enough time to distinguish drift '
            'from short-term noise. Report what was actually tried, learned, adopted or left unresolved. '
            'No existing locked/tracking or quality threshold is an optimization stopping target by itself. '
            'Do not force a boundary or exhaustive search when it has little information value.')
    return common + ('The laser is being used for measurement. Prioritize continuity and avoid mode hops or large '
        'state changes. Tolerate ordinary drift when practical measurement quality remains adequate; justify any small '
        'reversible comparison by a likely operational benefit. If more intrusive characterization is needed, report '
        'the evidence and proposed test without performing it. A warning prompts assessment, not unconditional motion.')
