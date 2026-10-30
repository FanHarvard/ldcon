"""Generic DEVICE-PARAMETER changes through the instrument registry (rule I, human decision 2026-10-22).

One-shot card ``tune.device_parameters``::

    changes: [{instrument, channel?, parameter, value}]     1..16 changes, applied in order, stop at the first failure
    approval_id: <32 hex> | null                             a direct human approval (host approval ledger)
    refresh: true                                            catalogs read fresh from the owners (read-only)

and the resident MCP tool ``device_parameters`` (``catalog`` / ``change``) use the SAME functions here, through the
dashboard route ``/api/lasers/{laser}/device-parameters`` (the card runs them in its worker).

Where the checks live:
  owner     every safety check of the instrument: lease and channel write permission, catalog range/class, conflicts
            with running loops (a leased channel only through its holder; burst/modulation active; PI armed), the write,
            the readback and the verification; an uncertain write faults the owner (no retry). See owner runtime
            ``device_parameters.py`` / ``instrument_owner.set_parameter``.
  registry  routing by instrument id and the laser's channel attribution (``lasers`` in registry.json); legacy owners
            mapped with a backend: ``disabled`` (CLD: writes disabled, bias lease holds CH0), ``read_only`` (scope),
            ``laser_definition`` (sr1092 native owner: its policy keys are laser-definition fields).
  here      the laser's authority: free = allowed; safety = needs a direct human approval id (``agent_authority``
            ledger; device rows use scope ``{kind: devices, name: <laser>}``, single use); maintainer = refused.
            ``laser_definition`` parameters are saved through the laser's own dashboard configuration API (the single
            source; live ``native_hot`` -> owner ``policy_update``); their safety approval is the ordinary laser-settings
            approval (scope ``lasers``) that the dashboard verifies on the PUT.
Every refusal happens before the first write (whole request planned first). Nothing is retried.

Rule I: operating-point outputs (TEC, DAC, LDC: close to direct outputs) are changed with ``tune.operating_point``;
device settings with this card. This path CAN move an operating point (catalog ``operating_point: true``), bounded by the
owner's catalog range, but the operating-point card remains the convenient knob (locked references, bounded ramps).
"""
import copy
import json
import math
import os
import re
from urllib.request import Request, build_opener, ProxyHandler

from ldcon.core.contracts import DeviceError

DEFAULT_REGISTRY = 'http://127.0.0.1:47150'
DEFAULTS = dict(changes=None, approval_id=None, refresh=True, timeout_s=300.)
MAX_CHANGES = 16
_INSTRUMENT = re.compile(r'[a-z0-9][a-z0-9_-]{0,40}:[A-Za-z0-9._-]{1,64}')
_NAME = re.compile(r'[a-z][a-z0-9_]{0,63}')


class RegistryLink:
    """The registry RPC (loopback). Tests pass ``post``; production resolves LDCON_DEVICE_REGISTRY or the default."""
    def __init__(self, endpoint=None, *, timeout_s=40., post=None):
        self.endpoint = (endpoint or os.environ.get('LDCON_DEVICE_REGISTRY') or DEFAULT_REGISTRY).rstrip('/')
        self.timeout_s, self.post = timeout_s, post
        self.http = build_opener(ProxyHandler({}))

    def call(self, action, **values):
        payload = dict(action=action, **values)
        if self.post is not None:
            return self.post(payload)
        request = Request(self.endpoint + '/rpc', data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'}, method='POST')
        token = os.environ.get('LDCON_INSTRUMENT_TOKEN')
        if token:
            request.add_header('Authorization', 'Bearer ' + token)
        try:
            with self.http.open(request, timeout=self.timeout_s) as response:
                reply = json.load(response)
        except OSError as error:
            raise DeviceError('Device registry unavailable at %s: %s' % (self.endpoint, error)) from error
        if reply.get('ok') is not True:
            raise DeviceError('%s refused: %s' % (action, reply.get('error', 'registry request rejected')))
        return reply['result']


# ------------------------------------------------------------------------------------------ parameters
def validate_change(change):
    if not isinstance(change, dict) or not {'instrument', 'parameter', 'value'} <= set(change) or set(change) - {'instrument', 'channel', 'parameter', 'value'}:
        raise ValueError('A change is {instrument, channel?, parameter, value}')
    if not isinstance(change['instrument'], str) or not _INSTRUMENT.fullmatch(change['instrument']):
        raise ValueError('change.instrument must be a canonical instrument id (<model>:<serial>)')
    if not isinstance(change['parameter'], str) or not _NAME.fullmatch(change['parameter']):
        raise ValueError('change.parameter must be a parameter name from the catalog')
    if change.get('channel') is not None and (type(change['channel']) is not int or not 0 <= change['channel'] <= 64):
        raise ValueError('change.channel must be a channel number')
    value = change['value']
    if not (type(value) in (bool, int, str) or (type(value) is float and math.isfinite(value))) or (isinstance(value, str) and len(value) > 64):
        raise ValueError('change.value must be a finite number, a bool or a short text')
    return dict(change)


def validate(p, *, require_changes=True):
    p = copy.deepcopy(p)
    if set(p) - DEFAULTS.keys():
        raise ValueError('Unknown device-parameter parameter: ' + ', '.join(sorted(set(p) - DEFAULTS.keys())))
    for key, value in DEFAULTS.items():
        p.setdefault(key, value)
    if p['changes'] is not None:
        if not isinstance(p['changes'], list) or not 1 <= len(p['changes']) <= MAX_CHANGES:
            raise ValueError('changes must list 1..%d changes' % MAX_CHANGES)
        p['changes'] = [validate_change(c) for c in p['changes']]
        keys = [(c['instrument'], c.get('channel'), c['parameter']) for c in p['changes']]
        if len(set(keys)) != len(keys):
            raise ValueError('a parameter appears twice in one request')
    elif require_changes:
        raise ValueError('changes is required')
    if p['approval_id'] is not None and (not isinstance(p['approval_id'], str) or not re.fullmatch('[0-9a-f]{32}', p['approval_id'])):
        raise ValueError('approval_id must be the 32-hex id from safety_change_approve')
    if type(p['refresh']) is not bool:
        raise ValueError('refresh must be true/false')
    if type(p['timeout_s']) not in (int, float) or not 0 < p['timeout_s'] <= 3600:
        raise ValueError('timeout_s must be in (0, 3600]')
    return p


def parameters(card, overrides):
    if not isinstance(card.get('parameters'), dict) or not isinstance(overrides, dict):
        raise ValueError('Device-parameter parameters must be mappings')
    return validate({**card['parameters'], **overrides}, require_changes=False)


def row_path(instrument, channel, name):
    return '%s/CH%d/%s' % (instrument, channel, name)


def parse_row_path(path):
    m = re.fullmatch(r'([^/]+)/CH(\d+)/([a-z][a-z0-9_]*)', path or '')
    if not m:
        raise ValueError('device change paths are <instrument>/CH<n>/<parameter>')
    return m[1], int(m[2]), m[3]


# ------------------------------------------------------------------------------------------ catalog
def _lookup(document, dotted):
    value = document
    for key in dotted.split('.'):
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


def catalog(laser, registry, *, refresh=False):
    """{instrument: catalog} for this laser; laser-definition values come from the laser definition (single source)."""
    result = registry.call('parameter_catalog', laser=laser['id'], refresh=bool(refresh))['instruments']
    for name, cat in result.items():
        if cat.get('backend') == 'laser_definition':
            if cat.get('laser') != laser['id']:
                cat['channels'] = {}
                continue
            for channel in cat.get('channels', {}).values():
                for entry in channel['parameters']:
                    entry['value'] = _lookup(laser, entry['path'])
                    entry['class'] = _laser_class(entry['path'])
                    entry['approval_scope'] = dict(kind='lasers', name=laser['id'])
    return result


def _laser_class(path):
    from ldcon.core.agent_authority import classify, FORBIDDEN
    cls = classify('lasers', tuple(path.split('.')))
    return 'maintainer' if cls == FORBIDDEN else cls


def _check_value(entry, value):
    t = entry['type']
    if t == 'bool':
        if type(value) is not bool:
            raise ValueError('%s takes true/false' % entry['name'])
        return value
    if t == 'enum':
        allowed = [str(v).upper() for v in entry.get('values') or []]
        if not isinstance(value, (str, int)) or type(value) is bool or str(value).upper() not in allowed:
            raise ValueError('%s must be one of %s' % (entry['name'], entry.get('values')))
        return entry['values'][allowed.index(str(value).upper())]
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError('%s must be a finite number' % entry['name'])
    if t == 'integer':
        if float(value) != int(value):
            raise ValueError('%s must be an integer' % entry['name'])
        value = int(value)
    if entry.get('range') is not None:
        lo, hi = entry['range']
        if not lo <= value <= hi:
            raise ValueError('%s %r is outside the catalog range [%g, %g]' % (entry['name'], value, lo, hi))
    return value


def plan(laser, changes, catalogs):
    """Resolve every change against the catalogs; refuse anything that cannot be applied, before any write."""
    plans = []
    for change in changes:
        cat = catalogs.get(change['instrument'])
        if cat is None:
            raise DeviceError('%s is not available to laser %s (registry attribution / legacy map)' % (change['instrument'], laser['id']))
        if cat.get('available') is False:
            raise DeviceError('%s owner does not answer: %s' % (change['instrument'], cat.get('error')))
        channels = cat.get('channels') or {}
        channel = change.get('channel')
        if channel is None:
            if len(channels) != 1:
                raise DeviceError('%s: give the channel (%s)' % (change['instrument'], ', '.join(sorted(channels)) or 'none attributed'))
            channel = int(next(iter(channels)))
        info = channels.get(str(channel))
        if info is None:
            raise DeviceError('%s CH%d is not attributed to laser %s' % (change['instrument'], channel, laser['id']))
        entry = next((e for e in info['parameters'] if e['name'] == change['parameter']), None)
        if entry is None:
            raise DeviceError('%s CH%d has no parameter %r' % (change['instrument'], channel, change['parameter']))
        backend = cat.get('backend', 'owner')
        cls = entry['class']
        if cls == 'maintainer':
            raise DeviceError('%s CH%d %s is maintainer-only: %s' % (change['instrument'], channel, entry['name'], entry.get('reason') or 'never through this path'))
        if not entry.get('writable', False):
            raise DeviceError('%s CH%d %s cannot be changed: %s' % (change['instrument'], channel, entry['name'], entry.get('reason')))
        if backend == 'owner' and info.get('leased_by'):
            raise DeviceError('%s CH%d is leased by %r: its lease holder (the running card) changes it, not this request'
                              % (change['instrument'], channel, info['leased_by']))
        if backend == 'owner' and entry.get('access') == 'lease_holder_only':
            raise DeviceError('%s CH%d changes only under its channel lease (%s)' % (change['instrument'], channel, info.get('role')))
        try:
            value = _check_value(entry, change['value'])
        except ValueError as error:
            raise DeviceError('%s CH%d: %s' % (change['instrument'], channel, error)) from error
        row = dict(instrument=change['instrument'], channel=channel, parameter=entry['name'], value=value,
                   old=entry.get('value'), backend=backend, operating_point=bool(entry.get('operating_point')),
                   unit=entry.get('unit'), **{'class': cls})
        if backend == 'laser_definition':
            row.update(path=entry['path'], scope=dict(kind='lasers', name=laser['id']))
        else:
            row.update(path=row_path(change['instrument'], channel, entry['name']), scope=dict(kind='devices', name=laser['id']))
        plans.append(row)
    return plans


def authorize(plans, approval_id, ledger):
    """free: allowed; safety: a direct human approval (device rows verified here, laser rows by the dashboard PUT)."""
    from ldcon.core.agent_authority import ApprovalError
    device_safety = [dict(path=p['path'], old=p['old'], new=p['value']) for p in plans if p['class'] == 'safety' and p['backend'] != 'laser_definition']
    laser_safety = [p for p in plans if p['class'] == 'safety' and p['backend'] == 'laser_definition']
    decision = dict(authority='free', approval_id=None, device_safety=[r['path'] for r in device_safety],
                    laser_safety=[p['path'] for p in laser_safety])
    if device_safety and laser_safety:
        raise ApprovalError('One approval covers one scope: split device-owner settings and laser-definition settings into two requests')
    if device_safety:
        if ledger is None:
            raise ApprovalError('No approval ledger for this laser')
        approval = ledger.verify(approval_id, scope=plans[0]['scope'], safety_rows=device_safety)
        decision.update(authority='human_approval', approval_id=approval_id, human_name=approval.get('human_name'),
                        safety_rows=device_safety, scope=plans[0]['scope'])
    elif laser_safety:
        if not approval_id:
            raise ApprovalError('safety-class laser-definition parameters need approval_id (a laser-settings approval, '
                                'scope {kind: lasers}); the dashboard verifies it on the save')
        decision.update(authority='human_approval (verified by the dashboard save)', approval_id=approval_id)
    return decision


def classify_rows(laser, rows, registry):
    """Classes for an approval proposal of device rows (dashboard propose route): rejects stale old values/maintainer."""
    catalogs = catalog(laser, registry, refresh=True)
    result = []
    for row in rows:
        instrument, channel, name = parse_row_path(row['path'])
        p = plan(laser, [dict(instrument=instrument, channel=channel, parameter=name, value=row['new'])], catalogs)[0]
        if p['backend'] == 'laser_definition':
            raise ValueError('%s is a laser-definition field: propose it with scope {kind: lasers} and path %s' % (row['path'], p['path']))
        from ldcon.core.agent_authority import ApprovalLedger
        if not ApprovalLedger._same(p['old'], row.get('old')):
            raise ValueError('Proposal old value differs from the current device value at %s (%r)' % (row['path'], p['old']))
        result.append(dict(path=row['path'], old=p['old'], new=p['value'], removed=False, added=False, live='device',
                           **{'class': 'forbidden' if p['class'] == 'maintainer' else p['class']}))
    return result


# ------------------------------------------------------------------------------------------ laser-definition backend
def _yaml_scalar(value):
    if type(value) is bool:
        return 'true' if value else 'false'
    if type(value) is int:
        return str(value)
    if type(value) is float:
        return repr(value)
    return json.dumps(value)


def set_leaf(text, path, value):
    """Verbatim edit of ONE existing block-style mapping leaf (no reformatting of the rest of the document)."""
    lines = text.splitlines(keepends=True)
    start, end, parent_indent = 0, len(lines), -1
    keys = path.split('.')
    for depth, key in enumerate(keys):
        child_indent = None
        found = None
        for i in range(start, end):
            stripped = lines[i].lstrip(' ')
            if not stripped.strip() or stripped.startswith('#'):
                continue
            indent = len(lines[i]) - len(stripped)
            if indent <= parent_indent:
                break
            if child_indent is None:
                child_indent = indent
            if indent == child_indent and re.match(r'%s:(\s|$)' % re.escape(key), stripped):
                found = (i, indent)
                break
        if found is None:
            raise DeviceError('laser definition has no %s (stopped at %s)' % (path, key))
        i, indent = found
        if depth == len(keys) - 1:
            m = re.match(r'(\s*%s:[ \t]*)([^#\n]*?)([ \t]*(#.*)?)(\r?\n?)$' % re.escape(key), lines[i])
            if not m or not m[2] or m[2].strip() in ('|', '>') or m[2].lstrip().startswith(('{', '[')):
                raise DeviceError('%s is not a scalar leaf on one line; it cannot be edited verbatim' % path)
            lines[i] = m[1] + _yaml_scalar(value) + m[3] + m[5]
            return ''.join(lines)
        start, parent_indent = i + 1, indent
        end = len(lines)
        for j in range(start, len(lines)):
            stripped = lines[j].lstrip(' ')
            if stripped.strip() and not stripped.startswith('#') and len(lines[j]) - len(stripped) <= indent:
                end = j
                break
    raise DeviceError('empty path')


def apply_laser_definition(laser_id, plans, approval_id, dashboard, actor):
    from ldcon.core.agent_authority import changes
    from ldcon.core.interface_schema import load_mapping
    current = dashboard.get_configuration('lasers', laser_id)
    text, before = current['yaml'], current.get('document') or load_mapping(current['yaml'])
    for p in plans:
        present = _lookup(before, p['path'])
        if not _same_value(present, p['old']):
            raise DeviceError('%s changed since the catalog was read (%r != %r); read the catalog again' % (p['path'], present, p['old']))
        text = set_leaf(text, p['path'], p['value'])
    after = load_mapping(text)
    changed = sorted('.'.join(c) for c in changes(before, after))
    if changed != sorted(p['path'] for p in plans):
        raise DeviceError('verbatim edit changed %s, expected exactly %s' % (changed, sorted(p['path'] for p in plans)))
    saved = dashboard.put_configuration('lasers', laser_id, text, current['revision'], actor=actor, approval_id=approval_id)
    readback = dashboard.get_configuration('lasers', laser_id)
    document = readback.get('document') or load_mapping(readback['yaml'])
    rows = []
    for p in plans:
        got = _lookup(document, p['path'])
        if not _same_value(got, p['value']):
            raise DeviceError('laser definition readback %s = %r, expected %r' % (p['path'], got, p['value']))
        rows.append(dict(instrument=p['instrument'], channel=p['channel'], name=p['parameter'], path=p['path'], old=p['old'],
                         new=p['value'], readback=got, verified=True, backend='laser_definition', revision=saved.get('revision'),
                         live=saved.get('live'), **{'class': p['class']}))
    return rows


def _same_value(a, b):
    if type(a) in (int, float) and type(b) in (int, float) and type(a) is not bool and type(b) is not bool:
        return math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-15)
    return a == b and type(a) is type(b)


# ------------------------------------------------------------------------------------------ the change
def change(laser, p, *, registry, ledger=None, dashboard=None, actor='device-parameters', emit=None, cancel=None, dry_run=False):
    """Plan -> authorize -> apply (stop at the first failure) -> consume the device approval. Shared by card and API."""
    emit = emit or (lambda kind, value: None)
    p = validate(p)
    catalogs = catalog(laser, registry, refresh=p['refresh'])
    plans = plan(laser, p['changes'], catalogs)
    decision = authorize(plans, p['approval_id'], ledger)
    emit('device_parameters_plan', dict(changes=plans, authority=decision['authority']))
    if dry_run:
        return dict(status='dry_run', applied=[], plan=plans, authority=decision)
    applied = []
    laser_rows = [x for x in plans if x['backend'] == 'laser_definition']
    try:
        for row in plans:
            if cancel and cancel():
                from ldcon.core.interface_cards import CardCancelled
                raise CardCancelled('Device-parameter change cancelled (applied: %d of %d)' % (len(applied), len(plans)))
            if row['backend'] == 'laser_definition':
                continue
            result = registry.call('set_parameter', instrument=row['instrument'], laser=laser['id'], channel=row['channel'],
                                   name=row['parameter'], value=row['value'], actor=actor,
                                   **({'approval_id': p['approval_id']} if row['class'] == 'safety' else {}))
            if not result.get('verified'):
                raise DeviceError('%s CH%d %s: the owner did not verify the readback' % (row['instrument'], row['channel'], row['parameter']))
            result.update(path=row['path'], backend=row['backend'])
            applied.append(result)
            emit('device_parameter_applied', result)
        if laser_rows:
            if dashboard is None:
                raise DeviceError('laser-definition parameters need the laser dashboard')
            if not hasattr(dashboard, 'get_configuration'):
                dashboard = dashboard()                       # lazy: built only when a laser-definition row exists
            rows = apply_laser_definition(laser['id'], laser_rows, p['approval_id'] if any(r['class'] == 'safety' for r in laser_rows) else None,
                                          dashboard, actor)
            applied.extend(rows)
            for r in rows:
                emit('device_parameter_applied', r)
    except Exception as error:
        error.device_parameters_applied = applied
        if applied:
            raise DeviceError('%s (already applied and verified: %s; not rolled back)' % (error, ', '.join(a['path'] for a in applied))) from error
        raise
    if decision.get('safety_rows'):
        ledger.consume(p['approval_id'], scope=decision['scope'], safety_rows=decision['safety_rows'],
                       result=dict(applied=[a['path'] for a in applied]))
    if ledger is not None:
        try:
            ledger.audit(dict(event='device_parameters', actor=actor, authority=decision['authority'], approval_id=decision.get('approval_id'),
                              changes=[dict(path=a['path'], old=a.get('old'), new=a.get('new'), readback=a.get('readback')) for a in applied]))
        except OSError:
            pass
    return dict(status='succeeded', applied=applied, authority=decision['authority'], approval_id=decision.get('approval_id'))


def run_device_parameters(directory, laser, card, p, *, clock, sleep, cancel, emit, publish=None, registry=None, ledger=None, dashboard=None):
    """The one-shot card (worker)."""
    from ldcon.core.agent_authority import ApprovalLedger
    from ldcon.jobs.dashboard_api import Dashboard, state_dir_of
    p = validate(p)
    publish = publish or (lambda value: None)
    registry = registry or RegistryLink()
    ledger = ledger if ledger is not None else ApprovalLedger(state_dir_of(directory), laser['id'])
    if dashboard is None:
        dashboard = lambda: Dashboard.from_job_directory(directory)        # noqa: E731 (lazy)
    publish(dict(status='planning', changes=p['changes']))
    result = change(laser, p, registry=registry, ledger=ledger, dashboard=dashboard, actor='card:' + card['id'],
                    emit=emit, cancel=cancel)
    publish(dict(status=result['status'], applied=[a['path'] for a in result['applied']]))
    return result

