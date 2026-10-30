"""Continuous read-only ECDL telemetry through the existing instrument owner.

This process has no device drivers or leases. The owner serializes diagnostic
reads with card commands. No modulation measurement or output change is made.
"""
import argparse
import json
import math
import os
from pathlib import Path
import signal
import threading
import time
from urllib.request import Request, ProxyHandler, build_opener

from ldcon.core.instrument_client import validate_endpoint
from ldcon.core.interface_schema import load_mapping
from ldcon.core.telegraf import TelegrafExporter
from ldcon.core.telemetry import TelemetryStore


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def summarize(result, *, now, max_age_s):
    """Never publish an old optical value as a new valid measurement."""
    state = result.get('snapshot', {})
    payload = dict(read_ok=True, owner_fault=bool(result.get('fault')),
                   owner_busy=bool(result.get('lease_retained')))
    payload['outputs'] = {k: v for k, v in state.get('outputs', {}).items()
                          if k in ('current', 'temperature', 'piezo') and finite(v)}
    if type(state.get('open_loop')) is bool:
        payload['servo_open_loop'] = state['open_loop']
    multiplier = state.get('servo_config', {}).get('pid_multiplier')
    if finite(multiplier):
        payload['native_pi_enabled'] = multiplier != 0
    # Read cached owner analysis only; never fit or write from this collector.
    for key,value in result.get('dither',{}).items():
        if key.startswith('dither_') and (type(value) in (bool,str) or finite(value)):
            if key!='dither_response' or result['dither'].get('dither_response_valid') is True:
                payload[key]=value
    obs = result.get('observation', {})
    sample = obs.get('frequency', {})
    acquired = sample.get('acquired_unix_s')
    age = now - acquired if finite(acquired) else None
    fresh = finite(age) and 0 <= age <= max_age_s and sample.get('acquisition_known') is True
    payload['optical_fresh'] = fresh
    payload['optical_valid'] = fresh and obs.get('quality_valid') is True
    payload['frequency_valid'] = (payload['optical_valid'] and sample.get('quality') == 'valid'
                                  and sample.get('unit') == 'Hz'
                                  and finite(sample.get('frequency_hz')) and sample['frequency_hz'] > 0)
    if finite(age):
        payload['optical_age_s'] = age
    if finite(acquired):
        payload['acquired_unix_s'] = acquired
    if payload['optical_valid']:
        payload['fringe_metrics'] = {k: v for k, v in (obs.get('metrics') or {}).items()
                                    if isinstance(k, str) and finite(v)}
        analysis = obs.get('fringe_analysis') or {}
        payload['fringe_analysis_valid'] = analysis.get('qualified') is True
        payload['fringe_period_qualified'] = analysis.get('period_qualified') is True
        payload['fringe_shape_resolved'] = analysis.get('shape_resolved') is True
        if type(obs.get('single_mode')) is bool:
            payload['single_mode'] = obs['single_mode']
        payload['mode_id'] = str(obs.get('mode_id', 'unclassified'))
    if payload['frequency_valid']:
        payload['frequency_hz'] = sample['frequency_hz']
        payload['wavelength_nm'] = 299792458e9 / sample['frequency_hz']
    if result.get('observation_error'):
        payload['observation_error'] = str(result['observation_error'])
    return payload


class OwnerReader:
    def __init__(self, connection, timeout_s=5):
        self.endpoint = validate_endpoint(connection['endpoint'])
        self.instrument = connection['instrument']
        self.timeout_s = timeout_s
        self.http = build_opener(ProxyHandler({}))

    def read(self):
        request = Request(self.endpoint + '/rpc', method='POST',
            data=json.dumps(dict(action='ecdl', instrument=self.instrument,
                                 request=dict(action='inspect'))).encode(),
            headers={'Content-Type': 'application/json'})
        token = os.environ.get('LDCON_INSTRUMENT_TOKEN') or os.environ.get('AWG_BROKER_TOKEN')
        if token:
            request.add_header('Authorization', 'Bearer ' + token)
        with self.http.open(request, timeout=self.timeout_s) as response:
            result = json.load(response)
        if not result.get('ok'):
            raise RuntimeError(result.get('error', 'Owner telemetry read failed'))
        return result['result']


def collect(reader, store, run_id, elapsed_s, max_age_s, *, now=time.time, project=summarize):
    # A failed diagnostic never issues a retry, takes a lease or stops a card.
    start = time.monotonic()
    try:
        payload = project(reader.read(), now=now(), max_age_s=max_age_s)
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
        payload = dict(read_ok=False, optical_valid=False, frequency_valid=False,
                       optical_fresh=False, error=str(error))
    payload['read_duration_s'] = time.monotonic() - start
    store.record(run_id, elapsed_s, 'owner_sample', payload)
    return payload


def collector(laser):
    """Resolve one existing owner; never create a device client or a lease."""
    dlc = [d for d in laser['devices'].values() if d['type']=='dlcpro.laser.owner']
    if dlc:
        if len(dlc)!=1:
            raise ValueError('One DLC laser owner required')
        from ldcon.owners.dlcpro_telemetry import DLCReader, summarize as dlc_summary
        device=dlc[0]
        return (DLCReader(device['connection']), dlc_summary, 'monitor.dlcpro',
                device['control_settings']['policy']['max_sample_age_s'])
    rig=laser['devices']['rig']
    if rig['type']!='ecdl.rig.owner':
        raise ValueError('An ECDL or DLC laser owner is required')
    return (OwnerReader(rig['connection']), summarize, 'monitor.ecdl',
            rig['control_settings']['max_sample_age_s'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--laser', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--interval-s', type=float, default=10.)
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    if not math.isfinite(args.interval_s) or args.interval_s < 5:
        parser.error('Monitoring interval must be at least 5 seconds')
    laser = load_mapping(args.laser.read_text())
    reader, project, operation, max_age_s = collector(laser)
    exporter = TelegrafExporter(laser['telemetry']['telegraf_endpoint'], laser['name'],
        simulation=False, tags=dict(job='monitor', card='instrument-monitor',
                                    operation=operation, producer='owner-monitor'))
    args.output.mkdir(parents=True, exist_ok=True)
    # Prevent a second collector from duplicating instrument-owner requests.
    import fcntl
    with (args.output / 'collector.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        stop = threading.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stop.set())
        start = time.monotonic()
        with TelemetryStore(args.output / 'telemetry.sqlite3', exporter=exporter) as store:
            run = store.start_run(dict(laser=dict(id=laser['id']), monitor=True))
            while not stop.is_set():
                try:
                    collect(reader, store, run, time.monotonic()-start,
                            max_age_s, project=project)
                except (OSError, ValueError) as error:
                    # Local logging failure must not affect the instrument owner.
                    print('Telemetry persistence failed: ' + str(error), flush=True)
                if args.once:
                    break
                stop.wait(args.interval_s)
            store.finish(run, 'completed')


if __name__ == '__main__':
    main()
