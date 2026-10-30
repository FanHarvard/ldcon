"""Device registry: the CONTROL PLANE for all per-instrument owners (rule G, 2026-10-22).

    python -m ldcon.owners.device_registry --config /opt/ldcon/var/instrument-owners/registry.json

The registry never opens an instrument and never carries instrument data. It knows which owners exist (its
configuration plus every ``<state_dir>/*/owner-state.json`` the owners publish), answers "where is the owner
of X" and starts an owner unit when none is running (idempotent: an owner that already answers is returned,
never duplicated). Leases, limits and safety checks live in the owners; the registry only reports lease
metadata. If the registry is down, running owners and running locks continue; only new discovery/start is
blocked. On restart it re-reads the owners' published state.

RPC (POST /rpc, loopback):
  discover {}                       -> every known instrument with endpoint/state (probe = owner ``status``)
  describe {instrument}             -> one instrument
  endpoint {instrument}             -> the endpoint only (no start)
  ensure   {instrument}             -> start the unit if the owner does not answer; wait; return endpoint/state
  leases   {instrument}             -> lease metadata from the owner
"""
import argparse
import copy
import json
import logging
import os
from pathlib import Path
import subprocess
import threading
import time
from urllib.request import Request, build_opener, ProxyHandler

from ldcon.core.instrument_client import BrokerError
from ldcon.owners.instrument_identity import canonical, unit_name, state_dir_name


def _post(endpoint, payload, timeout_s):
    request = Request(endpoint.rstrip('/') + '/rpc', data=json.dumps(payload).encode(),
                      headers={'Content-Type': 'application/json'}, method='POST')
    token = os.environ.get('LDCON_INSTRUMENT_TOKEN')
    if token:
        request.add_header('Authorization', 'Bearer ' + token)
    with build_opener(ProxyHandler({})).open(request, timeout=timeout_s) as response:
        reply = json.load(response)
    if reply.get('ok') is not True:
        raise BrokerError(reply.get('code', 'owner'), reply.get('error', 'owner request rejected'))
    return reply['result']


def validate_config(config):
    config = copy.deepcopy(config)
    required = {'bind', 'port', 'state_dir', 'instruments'}
    optional = {'start_timeout_s', 'probe_timeout_s', 'systemctl'}
    if not isinstance(config, dict) or not required <= set(config) <= required | optional:
        raise ValueError('Registry configuration keys: %s (+%s)' % (sorted(required), sorted(optional)))
    import ipaddress
    if not ipaddress.ip_address(config['bind']).is_loopback or type(config['port']) is not int:
        raise ValueError('The registry binds to loopback')
    if not isinstance(config['instruments'], dict):
        raise ValueError('instruments must be a mapping')
    for name, entry in config['instruments'].items():
        canonical(name)
        if not isinstance(entry, dict) or not isinstance(entry.get('endpoint'), str) or not entry['endpoint'].startswith('http://'):
            raise ValueError('instrument %s needs an http endpoint' % name)
        entry.setdefault('unit', unit_name(name))
        entry.setdefault('state_dir', str(Path(config['state_dir']) / state_dir_name(name)))
    return config


class Registry:
    def __init__(self, config, *, starter=None, probe=None, clock=time.monotonic, sleep=time.sleep):
        self.config = validate_config(config)
        self.lock = threading.RLock()
        self.clock, self.sleep = clock, sleep
        self.starter = starter or self._systemctl_start
        self.probe = probe or self._probe
        self.started = {}

    # ---- owner contact ------------------------------------------------------------------------
    def _probe(self, endpoint):
        return _post(endpoint, dict(action='status'), float(self.config.get('probe_timeout_s', 2.)))

    def _systemctl_start(self, unit):
        env = dict(os.environ)
        env.setdefault('XDG_RUNTIME_DIR', '/run/user/%d' % os.getuid())
        argv = [self.config.get('systemctl', 'systemctl'), '--user', 'start', unit]
        result = subprocess.run(argv, capture_output=True, text=True, timeout=30, env=env)
        if result.returncode != 0:
            raise BrokerError('start', 'systemctl --user start %s failed: %s' % (unit, (result.stderr or result.stdout).strip()[:500]))
        return True

    def published(self, name):
        path = Path(self.config['instruments'][name]['state_dir']) / 'owner-state.json'
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return None

    def _entry(self, name, *, probe=True):
        entry = self.config['instruments'][name]
        record = dict(instrument=name, endpoint=entry['endpoint'], unit=entry['unit'], state_dir=entry['state_dir'],
                      description=entry.get('description'), kind=entry.get('kind'), published=self.published(name),
                      reachable=False, state=None, error=None)
        if probe:
            try:
                record['state'] = self.probe(entry['endpoint'])
                record['reachable'] = True
                if record['state'].get('instrument') not in (None, name):
                    record['reachable'] = False
                    record['error'] = 'endpoint answers for %r, not %s' % (record['state'].get('instrument'), name)
            except Exception as exc:
                record['error'] = str(exc)[:300]
        return record

    # ---- RPC ------------------------------------------------------------------------------------
    def dispatch(self, request):
        action = request.get('action')
        if action == 'discover':
            with self.lock:
                return dict(instruments={name: self._entry(name) for name in self.config['instruments']},
                            registry=dict(pid=os.getpid(), state_dir=self.config['state_dir']))
        name = request.get('instrument')
        if name not in self.config['instruments']:
            raise BrokerError('unknown_instrument', str(name))
        with self.lock:
            if action == 'describe':
                return self._entry(name)
            if action == 'endpoint':
                return dict(instrument=name, endpoint=self.config['instruments'][name]['endpoint'])
            if action == 'leases':
                entry = self._entry(name)
                if not entry['reachable']:
                    raise BrokerError('unavailable', 'owner of %s does not answer: %s' % (name, entry['error']))
                return dict(instrument=name, leases=entry['state'].get('leases', []), fault=entry['state'].get('fault'))
            if action == 'ensure':
                return self.ensure(name)
            raise BrokerError('action', 'unsupported registry request')

    def ensure(self, name):
        """Idempotent: return the running owner, or start its unit once and wait for it to answer."""
        entry = self._entry(name)
        if entry['reachable']:
            return dict(entry, started=False)
        unit = self.config['instruments'][name]['unit']
        self.starter(unit)
        self.started[name] = self.clock()
        deadline = self.clock() + float(self.config.get('start_timeout_s', 20.))
        while True:
            entry = self._entry(name)
            if entry['reachable']:
                return dict(entry, started=True)
            if self.clock() >= deadline:
                raise BrokerError('unavailable', 'owner unit %s started but %s does not answer at %s: %s'
                                  % (unit, name, entry['endpoint'], entry['error']))
            self.sleep(0.5)


class RegistryClient:
    """Resolve an instrument's owner endpoint with graceful degradation.

    Order: registry ``ensure`` (starts the owner if absent) -> the static endpoint the caller configured ->
    the owner's published ``owner-state.json`` (registry down and no static endpoint). ``state_dir=None``
    disables the published lookup entirely (a client built without a registry resolves its static endpoint
    only). Owners are never contacted through the registry for data; the caller talks to the endpoint directly.

    2026-10-22 14:55Z: the earlier order (published before static) let a test with a static test endpoint
    resolve the LIVE BK owner through /opt/ldcon/var/instrument-owners and claim a lease on it. A static
    endpoint is explicit configuration and wins over any published file.
    """
    def __init__(self, registry_endpoint=None, *, state_dir=None, timeout_s=25.,
                 post=_post):
        self.registry_endpoint = registry_endpoint
        self.state_dir = Path(state_dir) if state_dir is not None else None
        self.timeout_s = timeout_s
        self.post = post
        self.last_source = None

    def published(self, instrument):
        if self.state_dir is None:
            return None
        path = self.state_dir / state_dir_name(instrument) / 'owner-state.json'
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return None

    def resolve(self, instrument, *, static_endpoint=None, ensure=True):
        canonical(instrument)
        errors = []
        if self.registry_endpoint:
            try:
                result = self.post(self.registry_endpoint, dict(action='ensure' if ensure else 'endpoint', instrument=instrument), self.timeout_s)
                self.last_source = 'registry'
                return result['endpoint']
            except Exception as exc:
                errors.append('registry: %s' % exc)
        if static_endpoint:
            self.last_source = 'static'
            return static_endpoint
        published = self.published(instrument)
        if published and published.get('endpoint') and not published.get('closed'):
            self.last_source = 'published'
            return published['endpoint']
        raise BrokerError('unavailable', 'cannot resolve the owner of %s (%s)' % (instrument, '; '.join(errors) or 'no registry, no static endpoint, no published state'))


def main():
    from ldcon.owners.instrument_service import make_server
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--validate-only', action='store_true')
    args = parser.parse_args()
    config = validate_config(json.loads(args.config.read_text()))
    if args.validate_only:
        print(json.dumps(dict(valid=True, instruments=sorted(config['instruments']))))
        return
    logging.basicConfig(level=logging.INFO)
    registry = Registry(config)
    server = make_server(registry, config['bind'], config['port'], token=os.environ.get('LDCON_INSTRUMENT_TOKEN'))
    logging.info('ldcon device registry at http://%s:%d; instruments %s', config['bind'], config['port'], sorted(config['instruments']))
    import signal
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
