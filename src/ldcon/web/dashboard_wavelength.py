"""Display-only wavelength publication, independent of lock/recording jobs.

Only the metadata sidecar polls the wavemeter's cached HTTP acquisition API.
Web handlers read a bounded publication; they never call a control owner. These
samples carry no control authority and never change action admission or PI state.
"""
import hashlib
import json
import math
from pathlib import Path
import time
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler

from ldcon.core.state_layout import config_root
from ldcon.web.dashboard_snapshot import read, ID

MAX_AGE_S = 15.


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def binding(laser):
    candidates = []
    for device in laser.get('devices', {}).values():
        connection = device.get('connection', {})
        kind = device.get('type')
        source = (connection.get('wavemeter') if kind == 'ecdl.rig.owner' else
                  connection.get('source') if kind == 'fizeau.owner' else
                  connection if kind == 'fizeau.http' else None)
        if isinstance(source, dict):
            candidates.append({k: source.get(k) for k in
                ('endpoint', 'channel', 'cycle', 'expected_laser', 'trust_source_clock')})
    unique = {json.dumps(c, sort_keys=True): c for c in candidates}
    if len(unique) != 1:
        raise ValueError('No unique configured wavelength source')
    value = next(iter(unique.values()))
    url = urlsplit(value.get('endpoint') or '')
    if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError('Invalid wavelength endpoint')
    if type(value['channel']) is not int or not 1 <= value['channel'] <= 64:
        raise ValueError('Explicit wavelength channel required')
    if value['cycle'] is not None and (type(value['cycle']) is not int or value['cycle'] < 0):
        raise ValueError('Invalid wavelength cycle')
    return value


def fingerprint(source):
    return hashlib.sha256(json.dumps(source, sort_keys=True).encode()).hexdigest()


def definitions(root):
    from ldcon.core.interface_schema import load_mapping
    root = Path(root)
    for path in (config_root(root) / 'lasers').glob('*.yaml'):
        if not ID.fullmatch(path.stem):
            continue
        laser = read(root, path, {}, limit=256*1024, parser=lambda raw: load_mapping(raw.decode()))
        if isinstance(laser, dict) and laser.get('id') == path.stem:
            yield path.stem, laser


def fetch(source):
    query = dict(channel=source['channel'], interferometer='both')
    if source['cycle'] is not None:
        query['cycle'] = source['cycle']
    request = Request(source['endpoint'].rstrip('/') + '/api/pattern?' + urlencode(query), method='GET')
    with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=2.) as response:
        raw = response.read(4*1024*1024+1)
    if len(raw) > 4*1024*1024:
        raise ValueError('Wavelength response too large')
    return json.loads(raw)


def measurement(source, data, now):
    if not isinstance(data, dict) or not isinstance(data.get('record', {}), dict):
        raise ValueError('Malformed wavelength observation')
    if data.get('ok') is not True or data.get('channel') != source['channel']:
        raise ValueError('Wavelength source/channel mismatch')
    record = data.get('record', {})
    if source['expected_laser'] and record.get('laser') != source['expected_laser']:
        raise ValueError('Wavelength laser mismatch')
    if source['cycle'] is not None and data.get('cycle', record.get('cycle')) != source['cycle']:
        raise ValueError('Wavelength cycle mismatch')
    if data.get('status', 0) != 0 or data.get('quality', 0) != 0:
        raise ValueError('Wavelength measurement quality invalid')
    if source['trust_source_clock'] is not True or record.get('aligned') is not True:
        raise ValueError('Wavelength acquisition clock unverified')
    stamp = data.get('t_exposure_end_unix_ms')
    if type(stamp) not in (int, float) or not math.isfinite(stamp):
        raise ValueError('Missing wavelength acquisition timestamp')
    stamp /= 1000.
    if not 0 <= now-stamp <= MAX_AGE_S:
        raise ValueError('Wavelength acquisition stale or future dated')
    thz = data.get('frequency_thz'); nm = data.get('wavelength_nm')
    if type(thz) in (int, float) and math.isfinite(thz) and thz > 0:
        hz = thz * 1e12
    elif type(nm) in (int, float) and math.isfinite(nm) and nm > 0:
        hz = 299792458e9 / nm
    else:
        raise ValueError('Missing finite wavelength')
    if not math.isfinite(hz) or not math.isfinite(299792458e9/hz):
        raise ValueError('Invalid frequency conversion')
    return dict(valid=True, frequency_hz=hz, wavelength_nm=299792458e9/hz,
                acquired_unix_s=stamp)


def publish(root, *, fetcher=fetch, clock=time.time):
    from ldcon.core.records import atomic_json
    root = Path(root)
    for ident, laser in definitions(root):
        row = dict(schema_version=1, laser_id=ident, valid=False, source='wavemeter_cached_acquisition')
        try:
            source = binding(laser)
            row.update(binding_revision=fingerprint(source), channel=source['channel'], cycle=source['cycle'])
            data = fetcher(source)
            row.update(measurement(source, data, clock()))
        except (OSError, ValueError, TypeError, KeyError, OverflowError):
            row.update(valid=False, reason='Wavelength source unavailable, invalid, or stale')
        row['published_unix_s'] = clock()
        atomic_json(root / ('dashboard-wavelength-' + ident + '.json'), row)


def view(root, ident, laser, *, now=None):
    now = time.time() if now is None else now
    missing = dict(valid=False, reason='No fresh independent wavelength observation')
    if not isinstance(ident, str) or not ID.fullmatch(ident):
        return missing
    try:
        source = binding(laser)
        row = read(Path(root), Path(root) / ('dashboard-wavelength-' + ident + '.json'), {}, limit=8192)
        if row.get('laser_id') != ident or row.get('binding_revision') != fingerprint(source) or row.get('valid') is not True:
            return missing
        for key in ('acquired_unix_s', 'published_unix_s'):
            if type(row.get(key)) not in (int,float) or not 0 <= now-row[key] <= MAX_AGE_S:
                return missing
        hz = row.get('frequency_hz')
        if type(hz) not in (int,float) or not math.isfinite(hz) or hz <= 0:
            return missing
        return {**{k:row[k] for k in ('valid','acquired_unix_s','channel','cycle','source')},
                'frequency_hz':hz, 'wavelength_nm':299792458e9/hz, 'age_s':now-row['acquired_unix_s']}
    except (OSError, ValueError, TypeError, KeyError):
        return missing


def views(root):
    return {ident: view(root, ident, laser) for ident,laser in definitions(root)}
