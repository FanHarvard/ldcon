"""Read-only DLC owner telemetry, independent of any control card lifetime."""
from copy import deepcopy
import json
import math
import os
from urllib.request import Request, ProxyHandler, build_opener

from ldcon.core.instrument_client import validate_endpoint


def finite(value):
    return type(value) in (float, int) and math.isfinite(value)


class DLCReader:
    def __init__(self, connection, timeout_s=5):
        self.endpoint = validate_endpoint(connection['endpoint'])
        self.laser_id = connection['laser_id']
        self.timeout_s = timeout_s
        self.http = build_opener(ProxyHandler({}))

    def read(self):
        request = Request(self.endpoint + '/rpc', method='POST',
            data=json.dumps(dict(action='inspect', laser_id=self.laser_id)).encode(),
            headers={'Content-Type': 'application/json'})
        token = os.environ.get('LDCON_INSTRUMENT_TOKEN')
        if token:
            request.add_header('Authorization', 'Bearer ' + token)
        with self.http.open(request, timeout=self.timeout_s) as response:
            result = json.load(response)
        if not result.get('ok'):
            raise RuntimeError('DLC owner telemetry read failed')
        return result['result']


def summarize(result, *, now, max_age_s):
    state = result['snapshot']
    row = {key: deepcopy(state[key]) for key in (
        'observed_s', 'hardware', 'outputs', 'target_frequency_hz',
        'target_revision', 'mode', 'pi_enabled', 'integral_v', 'control',
        'frequency_input', 'hardware_readback', 'open_loop') if key in state}
    row.update(read_ok=True, owner_fault=bool(state.get('fault')),
               owner_busy=result.get('client_claimed') is True)
    if type(state.get('pi_enabled')) is bool:
        row['native_pi_enabled'] = state['pi_enabled']
    emission = state.get('hardware', {}).get('emission')
    if type(emission) is bool:
        row['output_enabled'] = emission
    obs = state.get('observation') or {}
    acquired = obs.get('acquired_unix_s')
    age = now-acquired if finite(acquired) else None
    fresh = finite(age) and 0 <= age <= max_age_s
    frequency = obs.get('frequency_hz')
    valid = fresh and finite(frequency) and frequency > 0
    quality = obs.get('quality') or {}
    optical = (valid and obs.get('quality_valid') is True
               and quality.get('fresh') is True and quality.get('quality_valid') is True)
    row.update(frequency_valid=valid, optical_fresh=fresh, optical_valid=optical,
               observation=dict(quality_valid=optical))
    if finite(age):
        row['sample_age_s'] = age
    if finite(acquired):
        row['acquired_unix_s'] = acquired
    if valid:
        row['frequency_hz'] = frequency
        row['wavelength_nm'] = 299792458e9/frequency
    if optical:
        metrics = {k: v for k, v in (obs.get('metrics') or {}).items() if finite(v)}
        row['observation']['metrics'] = metrics
        if type(quality.get('single_mode')) is bool:
            row['single_mode'] = quality['single_mode']
    return row
