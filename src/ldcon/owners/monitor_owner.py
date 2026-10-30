"""Independent cached monitor service; HTTP clients never touch hardware."""

import argparse
import copy
import fcntl
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import logging
import math
import os
from pathlib import Path
import signal
import socket
import threading
import time


class ResourceLock:
    """Retain former and shared lock inodes across service-account migration."""

    def __init__(self, address, port, kinds=('physical', 'device')):
        from ldcon.owners.migration_physical_lock import acquire_endpoint
        self.stream = acquire_endpoint(address, port, kinds)

    def close(self):
        self.stream.close()


class CachedMonitor:
    def __init__(self, driver, interval_s, max_age_s, status_file=None):
        self.driver = driver
        self.interval_s = interval_s
        self.max_age_s = max_age_s
        self.status_file = status_file
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.state = dict(schema_version=1, state='starting', read_ok=False,
                          sample=None, last_success_unix_s=None, failures=0)

    def snapshot(self):
        with self.lock:
            state = copy.deepcopy(self.state)
        stamp = state['last_success_unix_s']
        age = time.time() - stamp if stamp is not None else None
        state.update(owner_pid=os.getpid(), device_id=self.driver.device_id,
                     capabilities=list(getattr(self.driver, 'capabilities', ['oscilloscope_monitor'])), read_only=True,
                     age_s=age, fresh=bool(state['read_ok'] and age is not None
                                          and 0 <= age <= self.max_age_s),
                     acquisition_time_known=False)
        # Host read time is never advertised as the instrument acquisition time.
        return state

    def poll(self):
        try:
            sample = self.driver.read()
        except (OSError, ValueError) as exc:
            self.driver.close()
            with self.lock:
                self.state.update(state='unavailable', read_ok=False, error=str(exc),
                                  failures=self.state['failures'] + 1)
        else:
            with self.lock:
                self.state.update(state='connected', read_ok=True, sample=sample,
                                  last_success_unix_s=time.time(), failures=0, error=None)
        if self.status_file:
            temporary = self.status_file.with_suffix('.tmp')
            temporary.write_text(json.dumps(self.snapshot(), allow_nan=False))
            temporary.replace(self.status_file)

    def run(self):
        try:
            while not self.stop.is_set():
                self.poll()
                with self.lock:
                    failures = self.state['failures']
                self.stop.wait(min(30, self.interval_s * 2 ** min(failures, 3)))
        finally:
            self.driver.close()


def make_handler(monitor):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            from ldcon.core.instrument_client import BrokerError
            if not callable(getattr(monitor, 'dispatch', None)):
                self.send_error(501)
                return
            try:
                if self.path != '/rpc':
                    raise BrokerError('path', 'Use /rpc')
                token = os.environ.get('LDCON_INSTRUMENT_TOKEN')
                if token and not hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + token):
                    raise BrokerError('authorization', 'Invalid native owner token')
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 16384:
                    raise BrokerError('request', 'Native RPC must be at most 16 KiB')
                request = json.loads(self.rfile.read(size))
                if not isinstance(request, dict):
                    raise BrokerError('request', 'Native RPC must be an object')
                reply = dict(ok=True, result=monitor.dispatch(request))
            except BrokerError as exc:
                reply = dict(ok=False, code=exc.code, error=str(exc))
            except (ValueError, TypeError, KeyError) as exc:
                reply = dict(ok=False, code='request', error=str(exc))
            except Exception:
                reply = dict(ok=False, code='server', error='Owner operation failed; inspect retained state')
            body = json.dumps(reply, allow_nan=False).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path not in ('/v1/status', '/v1/snapshot'):
                self.send_error(404)
                return
            body = json.dumps(monitor.snapshot(), allow_nan=False).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

        def setup(self):
            super().setup()
            self.connection.settimeout(2)

    return Handler


def serve_monitor(server, monitor, worker):
    """Serve until requested stop; a reader exit without stop remains fatal."""
    while not monitor.stop.is_set():
        server.handle_request()
        # SIGTERM can wake the reader while handle_request is returning. A
        # reader that exits after that explicit stop is not an unexpected loss.
        if not worker.is_alive() and not monitor.stop.is_set():
            raise RuntimeError('Monitor reader stopped unexpectedly')


LAYER1_COPIES = ('native_current', 'native_ff_sweep')


def live_laser_file(config, snapshot):
    """The laser's LIVE definition (the file configuration.save writes), located like the rest of the code:
    ``<root>/lasers/<id>`` with the single-laser layout marker (``state_layout.config_file``). Explicit
    ``laser_file`` in the owner config wins; otherwise the root is the parent of ``approvals_root`` (or
    ``laser_root``) and the laser id is ``LDCON_BACKGROUND_LASER_ID`` or the snapshot's ``id``."""
    explicit = config.get('laser_file')
    if explicit:
        return Path(explicit)
    laser_id = os.environ.get('LDCON_BACKGROUND_LASER_ID') or (snapshot or {}).get('id')
    base = config.get('laser_root')
    if base is None and config.get('approvals_root') and laser_id:
        base = Path(config['approvals_root']).resolve().parent / 'lasers' / laser_id
    if base is None or not laser_id:
        return None
    from ldcon.core.state_layout import config_file
    try:
        return config_file(base, 'lasers', laser_id)
    except ValueError:
        return None


def load_native_assembly(config, config_file):
    """Resolve the trusted laser document once, before DNS, sockets or device writes: ``(factory, laser, source)``.

    Rule M step 4 (2026-10-27): the laser's LIVE yaml (``live_laser_file``) is the layer-2 source of the operating
    policy, the FF profile and the CLD conditions (see ``startup_policy``); the owner adopts it at start. The
    ``native_assembly.laser_file`` snapshot is used only when the live yaml is absent (``source['kind']`` says which,
    the owner audits it). The assembly wiring stays pinned as the ``assembly_definition`` dependency (its digest
    excludes the operating policy and the target, so policy edits never invalidate it).
    """
    spec = config.get('native_assembly')
    if spec is None:
        return None, None, None
    if not isinstance(spec, dict) or set(spec) != {'laser_file'} or not isinstance(spec['laser_file'], str):
        raise ValueError('native_assembly requires exactly one startup laser_file')
    path = Path(spec['laser_file'])
    if not path.is_absolute():
        path = Path(config_file).resolve().parent / path
    from ldcon.core.interface_schema import load_mapping, validate_laser
    snapshot = validate_laser(load_mapping(path.read_text()))
    live = live_laser_file(config, snapshot)
    if live is not None and live.is_file():
        laser = validate_laser(load_mapping(live.read_text()))
        if laser.get('id') != snapshot.get('id'):
            raise ValueError('Live laser definition %s names another laser than the assembly snapshot' % live)
        source = dict(kind='live', path=str(live), snapshot=str(path))
    else:
        laser = snapshot
        source = dict(kind='snapshot', path=str(path), live_missing=None if live is None else str(live))
    native = laser['devices'][laser['interfaces']['native_current']['device']]
    if native['connection']['device_id'] != config['device_id']:
        raise ValueError('Native assembly names another physical device than the monitor configuration')
    from ldcon.owners.native_assembly import make_assembly_factory
    return make_assembly_factory(laser), laser, source


def startup_policy(config, laser):
    """The native operating policy and FF profile at start (rule M step 4): ``(policy, ff_profile, ignored_keys)``.

    Layer 2 = the laser yaml's ``control_settings.native_current`` / ``native_ff_sweep``. Layer 1 = the config's
    ``physical`` block (firmware_version, dac, adc, calibration), which must agree with the yaml's physical facts
    (calibration.evidence excepted). An older config.json still carrying ``native_current`` / ``native_ff_sweep``
    copies starts too: its physical block is taken from the copy and the copies are ignored (``ignored_keys``;
    the owner audits ``layer1_copy_ignored`` once; ``ops/trim_owner_config.py`` removes them).
    """
    from ldcon.owners.esp32_native_owner import validate_policy
    from ldcon.owners.native_ff_proof import validate_profile
    from ldcon.owners.native_system_owner import PHYSICAL_KEYS, physical_mismatch
    native = laser['devices'][laser['interfaces']['native_current']['device']]
    settings = native['control_settings']
    if 'native_current' not in settings:
        raise ValueError('The laser definition carries no native_current policy for this owner')
    policy = validate_policy(settings['native_current'])
    profile = settings.get('native_ff_sweep')
    if profile is not None:
        profile = validate_profile(profile, policy)
    ignored = [key for key in LAYER1_COPIES if key in config]
    physical = config.get('physical')
    if physical is None and isinstance(config.get('native_current'), dict):
        physical = {key: config['native_current'][key] for key in PHYSICAL_KEYS if key in config['native_current']}
    if not isinstance(physical, dict) or set(physical) != set(PHYSICAL_KEYS):
        raise ValueError('Native owner configuration needs its physical block: ' + ', '.join(PHYSICAL_KEYS))
    mismatch = physical_mismatch(physical, policy)
    if mismatch:
        raise ValueError('Laser definition physical facts differ from the owner configuration (layer 1): ' + ', '.join(mismatch))
    return policy, profile, ignored


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--validate-only', action='store_true', help='Validate without DNS, locks, sockets or hardware')
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    assembly_factory, laser, policy_source = load_native_assembly(config, args.config)
    native = laser is not None or config.get('native_current') is not None
    policy = ff_profile = None
    ignored = []
    if native:
        if laser is None:
            parser.error('A native current owner needs native_assembly.laser_file: the laser yaml is its policy source (rule M)')
        policy, ff_profile, ignored = startup_policy(config, laser)
    interval = float(config.get('interval_s', 5))
    max_age = float(config.get('max_age_s', 15))
    minimum_interval = .1 if native else 2
    if not math.isfinite(interval) or not minimum_interval <= interval <= 60:
        parser.error('interval_s is outside supported bounds')
    if not math.isfinite(max_age) or not interval <= max_age <= 300:
        parser.error('max_age_s must be within interval_s..300')
    # Loopback only until authentication is explicitly configured in a future API.
    host = config.get('bind', '127.0.0.1')
    if host != '127.0.0.1':
        parser.error('Monitor API must bind to 127.0.0.1')
    if native and (config.get('driver') != 'esp32servocon' or not config.get('state_file')):
        parser.error('Native current requires shared ESP driver and durable state_file')
    if args.validate_only:
        print(json.dumps(dict(valid=True, device_id=config['device_id'], native_current=native,
                              layer1_copies_ignored=ignored, policy_source=policy_source)))
        return
    address = socket.gethostbyname(config['host'])
    kind = config.get('driver', 'rigol.dho924')
    if kind == 'rigol.dho924':
        from ldcon.drivers.rigol_scope import RigolScope
        port = int(config.get('scpi_port', 5555))
        driver = RigolScope(address, port, config['device_id'], config.get('serial'))
    elif kind == 'esp32servocon':
        from ldcon.drivers.esp32_servo import Esp32Servo
        port = int(config.get('device_port', 80))
        driver = Esp32Servo(address, port, config['device_id'])
    else:
        parser.error('Unknown driver')
    resource_lock = ResourceLock(address, port, ('device',) if kind=='rigol.dho924' else ('physical','device'))
    status_file = Path(config['status_file']).resolve() if config.get('status_file') else None
    if status_file:
        status_file.parent.mkdir(parents=True, exist_ok=True)
    if native:
        if kind != 'esp32servocon':
            parser.error('native_current is only supported by the shared ESP driver')
        from ldcon.owners.esp32_native_owner import NativeCurrentOwner
        monitor = NativeCurrentOwner(driver, interval, max_age, status_file,
                                     policy, config['state_file'],
                                     ff_profile=ff_profile, assembly_factory=assembly_factory, laser=laser)
        monitor._report_event('policy_source', source=policy_source)   # keys kind/path; 'kind' is _report_event's own
        logging.warning('native owner policy source: %s', json.dumps(policy_source, sort_keys=True))
        if ignored:
            monitor._report_event('layer1_copy_ignored', keys=ignored,
                                  note='rule M step 4: the operating policy is the laser yaml; run ops/trim_owner_config.py')
    else:
        monitor = CachedMonitor(driver, interval, max_age, status_file)
    if native:
        monitor.policy_config_file = args.config.resolve()
        # Host-only decision records written by the dashboard (read-only in the agent sandbox).
        monitor.approvals_root = Path(config['approvals_root']) if config.get('approvals_root') else None
    worker = None
    try:
        server = HTTPServer((host, int(config['api_port'])), make_handler(monitor))
        server.timeout = 0.5
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: monitor.stop.set())
        worker = threading.Thread(target=monitor.run, name='device-reader', daemon=True)
        worker.start()
        serve_monitor(server, monitor, worker)
        server.server_close()
    finally:
        monitor.stop.set()
        if worker:
            worker.join(timeout=8)
        resource_lock.close()


if __name__ == '__main__':
    main()
