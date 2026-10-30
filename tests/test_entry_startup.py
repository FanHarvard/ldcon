"""Start-up tests for every ldcon entry module: ``main()`` runs in-process on a copy of the live configuration.

Motivation (2026-10-27): release g8bb1f8545d2d crashed ``ldcon.monitor_owner`` at start
(``monitor._report_event('policy_source', **policy_source)`` -> TypeError) although 1319 tests passed, because
nothing executed ``main()``. Each test here runs the entry module exactly as systemd does (``sys.argv`` in the
unit's shape, the live config/yaml copied under ``tmp_path`` with the paths rewritten) far enough that the
owner/service object is constructed, its state file is written and one poll / one request cycle completes,
then stops it through the module's own stop path (signal handler, ``--once``, server shutdown).

Hardware and network layers are the suite's existing fakes (``jobs/tests/test_esp32_native_current.py`` for the
native owner, ``test_projection_transport.FakeAWG`` for the SDG, ``drivers.cld1015.MockIO`` for the CLD). Every
test runs under a socket guard: only loopback ports the test itself allocated may be contacted, so a start-up
path that would reach a live owner, the wavemeter or Telegraf fails here instead of in production.

``test_help_exits_zero`` additionally runs ``python -m ldcon.<x> --help`` (or the module's harmless usage path)
as a subprocess from the source tree, so import-time failures of the entry modules are caught too.
"""
import copy
import importlib
import importlib.util
import io
import ipaddress
import json
import os
import re
import runpy
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.request import ProxyHandler, Request, build_opener

import pytest
import yaml

TESTS = Path(__file__).resolve().parent
REPO = (TESTS / 'support')
EFFECTIVE = TESTS / 'fixtures' / 'effective-settings' / 'var'
EXTRA = TESTS / 'fixtures' / 'entry-startup'
LIVE_VAR = '/opt/ldcon/var'

_spec = importlib.util.spec_from_file_location('native_entry_fixture', REPO / 'jobs/tests/test_esp32_native_current.py')
native = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(native)
_awg_spec = importlib.util.spec_from_file_location('awg_entry_fixture', TESTS / 'test_projection_transport.py')
awg = importlib.util.module_from_spec(_awg_spec)
_awg_spec.loader.exec_module(awg)

# Entry modules launched by systemd (``python -m ldcon.<x> ...``) and the argv shape of their unit's ExecStart.
ARGPARSE_MODULES = ['job_service', 'web_server', 'web_fleet', 'instrument_service', 'monitor_owner', 'instrument_owner',
                    'cld_owner', 'device_registry', 'dashboard_metrics', 'owner_telemetry', 'esp32_time_telemetry',
                    'fallback_service']
POSITIONAL_MODULES = ['interface_worker', 'agent_tools']      # ``python -m ldcon.<x> <path>``; no argparse


# ---- infrastructure ---------------------------------------------------------------------------------------------

@pytest.fixture
def offline(monkeypatch):
    """No socket leaves the test: only loopback ports the test registered in the returned set may be contacted."""
    allowed = set()
    real_connect = socket.socket.connect
    real_sendto = socket.socket.sendto

    def permitted(address):
        return (isinstance(address, tuple) and len(address) >= 2 and address[0] in ('127.0.0.1', 'localhost')
                and address[1] in allowed)

    def connect(self, address):
        if not permitted(address):
            raise AssertionError('entry start-up test tried to connect to %r' % (address,))
        return real_connect(self, address)

    def sendto(self, data, *rest):
        if not permitted(rest[-1]):
            raise AssertionError('entry start-up test tried to send a datagram to %r' % (rest[-1],))
        return real_sendto(self, data, *rest)

    def resolve(name):
        try:
            return str(ipaddress.ip_address(name))
        except ValueError:
            return '192.0.2.1'          # TEST-NET: never routed; no DNS from the test

    monkeypatch.setattr(socket.socket, 'connect', connect)
    monkeypatch.setattr(socket.socket, 'sendto', sendto)
    monkeypatch.setattr(socket, 'gethostbyname', resolve)
    return allowed


@pytest.fixture
def restore_signals():
    """main() installs SIGTERM/SIGINT handlers on this (the pytest) process; put the previous ones back."""
    saved = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    yield
    for sig, handler in saved.items():
        signal.signal(sig, handler)


@pytest.fixture
def no_locks(monkeypatch):
    """The physical-lock manifest (inode-pinned flocks under /opt/ldcon) is replaced by an idle lock set."""
    import ldcon.owners.migration_physical_lock as locks

    class IdleLockSet:
        closed = False

        def __enter__(self):
            return self

        def __exit__(self, *_):
            self.close()

        def close(self):
            self.closed = True
    for name in ('acquire_endpoint', 'acquire_broker', 'acquire_visa'):
        monkeypatch.setattr(locks, name, lambda *a, **k: IdleLockSet())


def free_port(allowed):
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    allowed.add(port)
    return port


def udp_sink(allowed):
    sink = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sink.bind(('127.0.0.1', 0))
    sink.settimeout(2.)
    allowed.add(sink.getsockname()[1])
    return sink


def datagrams(sink, at_least=1, timeout_s=2.):
    """Everything the sink received (at least ``at_least`` datagrams within ``timeout_s``)."""
    received = []
    deadline = time.monotonic() + timeout_s
    while len(received) < at_least and time.monotonic() < deadline:
        try:
            received.append(sink.recv(65536).decode())
        except socket.timeout:
            break
    sink.settimeout(.05)
    try:
        while True:
            received.append(sink.recv(65536).decode())
    except (socket.timeout, OSError):
        pass
    return received


WAVEMETER = 'http://192.168.1.22:8765'      # the live Fizeau wavemeter named by both laser yamls


@pytest.fixture
def var(tmp_path, offline):
    """A copy of the live ``/opt/ldcon/var`` configuration (the effective-settings fixture) with its paths rewritten.

    The wavemeter endpoint becomes a loopback port nothing listens on (connection refused is the collectors'
    ordinary "wavemeter down" path); every other network address stays as in production and is blocked by ``offline``.
    """
    tree = tmp_path / 'var'
    shutil.copytree(EFFECTIVE, tree)
    shutil.copy(EXTRA / 'assembly-definition.yaml', tree / 'device-owners/esp32servocon-sr1092/assembly-definition.yaml')
    (tree / 'device-owners/sr422/compat').mkdir()
    shutil.copy(EXTRA / 'sr1092-broker.yaml', tree / 'device-owners/sr422/compat/sr1092-broker.yaml')
    dead_wavemeter = 'http://127.0.0.1:%d' % free_port(offline)
    for path in tree.rglob('*'):
        if path.suffix in ('.json', '.yaml'):
            path.write_text(path.read_text().replace(LIVE_VAR, str(tree)).replace(WAVEMETER, dead_wavemeter))
    for laser in ('sr1092', 'sr422'):
        (tree / 'lasers' / laser / 'jobs').mkdir()
        (tree / 'lasers' / laser / 'data').mkdir()
    (tree / 'agent-approvals').mkdir()
    (tree / 'instrument-locks').mkdir()
    (tree / 'dashboard').mkdir()
    return tree


def load(path):
    return json.loads(Path(path).read_text())


def save(path, value):
    Path(path).write_text(json.dumps(value, indent=1))


def run_main(monkeypatch, module, *argv):
    """``python -m ldcon.<module> <argv>`` in-process: the module's own ``main()`` on the unit's argv shape."""
    monkeypatch.setattr(sys, 'argv', ['ldcon.' + module, *map(str, argv)])
    return importlib.import_module('ldcon.' + module).main()


def http(url, payload=None, timeout_s=5.):
    request = Request(url, data=json.dumps(payload).encode() if payload is not None else None,
                      headers={'Content-Type': 'application/json'} if payload is not None else {},
                      method='POST' if payload is not None else 'GET')
    with build_opener(ProxyHandler({})).open(request, timeout=timeout_s) as response:
        body = response.read()
        return response.status, body


def rpc(port, **request):
    status, body = http('http://127.0.0.1:%d/rpc' % port, request)
    assert status == 200
    return json.loads(body)


def wait_for(predicate, timeout_s=5., what='condition'):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.02)
    raise AssertionError('timed out waiting for ' + what)


def capture_server(monkeypatch, module_name):
    """Wrap the module's ``make_server`` so the test can shut the real server down from a helper thread."""
    module = importlib.import_module(module_name)
    real = module.make_server
    servers = []

    def make_server(*args, **kwargs):
        server = real(*args, **kwargs)
        servers.append(server)
        return server
    monkeypatch.setattr(module, 'make_server', make_server)
    return servers


class Helper:
    """A thread that waits for the service, performs the test's requests and stops the service; errors propagate."""
    def __init__(self, body):
        self.body = body
        self.error = None
        self.result = None
        self.thread = threading.Thread(target=self._run, name='entry-startup-helper', daemon=True)
        self.thread.start()

    def _run(self):
        try:
            self.result = self.body()
        except BaseException as error:    # noqa: BLE001 - reported from the test thread
            self.error = error

    def finish(self):
        self.thread.join(10)
        assert not self.thread.is_alive(), 'helper thread did not finish'
        if self.error is not None:
            raise self.error
        return self.result


def serve_once(requests):
    """Replacement for ``uvicorn.run``: a real uvicorn server in a thread, the requests, then ``should_exit``."""
    def run(app, host='127.0.0.1', port=8000, **kwargs):
        import uvicorn
        server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level='warning', access_log=False))
        thread = threading.Thread(target=server.run, name='entry-startup-uvicorn', daemon=True)
        thread.start()
        try:
            wait_for(lambda: server.started or not thread.is_alive(), what='uvicorn start')
            assert server.started, 'uvicorn did not start'
            run.results = [http('http://%s:%d%s' % (host, port, path(app))) for path in requests]
        finally:
            server.should_exit = True
            thread.join(5)
            assert not thread.is_alive(), 'uvicorn did not stop'
    run.results = None
    return run


# ---- job_service ------------------------------------------------------------------------------------------------

def test_job_service_dispatches_one_cycle_then_stops_on_sigterm(var, monkeypatch, offline, restore_signals):
    import ldcon.jobs.job_service as job_service
    root = var / 'lasers/sr1092'
    job = root / 'jobs' / 'job1'
    job.mkdir()
    save(job / 'job.json', dict(id='job1', worker_backend='service', laser=dict(id='sr1092')))
    (job / 'cancel.request').touch()           # cancelled before launch: the dispatch path without a subprocess
    (job / 'launch.request').touch()
    real = job_service.dispatch_pending
    cycles = []

    def dispatch_pending(state_dir):
        children = real(state_dir)
        cycles.append(children)
        signal.raise_signal(signal.SIGTERM)    # the unit's stop: main() installed the handler before its loop
        return children
    monkeypatch.setattr(job_service, 'dispatch_pending', dispatch_pending)
    run_main(monkeypatch, 'job_service', '--state-dir', root)
    assert cycles == [[]]
    assert load(job / 'progress.json')['status'] == 'cancelled'
    assert (job / 'launch.claim').exists() and (root / 'job-service.lock').exists()


# ---- web_server / web_fleet / fallback_service (uvicorn apps) ------------------------------------------------------

def test_web_server_starts_writes_state_file_and_serves_the_dashboard(var, monkeypatch, offline):
    import uvicorn
    root = var / 'lasers/sr1092'
    state_file = root / 'web-server.json'
    port = free_port(offline)
    save(state_file, dict(pid=0, host='0.0.0.0', port=8085, path='/s/' + 'k' * 32 + '/', backend='interface-live'))
    run = serve_once([lambda app: app.state.prefix + '/', lambda app: app.state.prefix + '/api/sets'])
    monkeypatch.setattr(uvicorn, 'run', run)
    monkeypatch.setenv('LDCON_NOTIFICATION_PORT', str(free_port(offline)))
    run_main(monkeypatch, 'web_server', '--state-dir', root, '--state-file', state_file, '--host', '127.0.0.1', '--port', port)
    assert [status for status, _ in run.results] == [200, 200]
    state = load(state_file)
    assert state['pid'] == os.getpid() and state['port'] == port and state['path'] == '/s/' + 'k' * 32 + '/'
    assert state['backend'] == 'interface-live'
    assert json.loads(run.results[1][1])['sets'][0]['id'] == 'sr1092'


def test_web_fleet_starts_writes_state_file_and_serves_the_fleet_page(var, monkeypatch, offline):
    import uvicorn
    sink = udp_sink(offline)
    registry = var / 'dashboard/services.json'
    save(registry, dict(services=[dict(id=laser, label=laser.capitalize(), origin='http://127.0.0.1:%d' % free_port(offline),
                                       state_file=str(var / 'lasers' / laser / 'web-server.json'),
                                       lasers=[dict(id=laser, name=laser.capitalize())]) for laser in ('sr1092', 'sr422')],
                        telemetry_endpoint='udp://127.0.0.1:%d' % sink.getsockname()[1]))
    state_file = var / 'dashboard/web-server.json'
    port = free_port(offline)
    run = serve_once([lambda app: app.state.prefix + '/'])
    monkeypatch.setattr(uvicorn, 'run', run)
    run_main(monkeypatch, 'web_fleet', '--registry', registry, '--state-file', state_file, '--host', '127.0.0.1', '--port', port)
    assert [status for status, _ in run.results] == [200]
    state = load(state_file)
    assert state['port'] == port and state['backend'] == 'fleet' and re.fullmatch(r'/s/[A-Za-z0-9_-]{24,80}/', state['path'])
    assert oct(state_file.stat().st_mode)[-3:] == '600'


def test_fallback_service_starts_its_observer_and_answers_health(var, monkeypatch, offline):
    import uvicorn
    root = var / 'lasers/sr1092'
    prefix = '/s/' + 'n' * 32
    save(root / 'web-server.json', dict(pid=0, host='0.0.0.0', port=8085, path=prefix + '/', backend='interface-live'))
    port = free_port(offline)
    run = serve_once([lambda app: prefix + '/api/health'])
    monkeypatch.setattr(uvicorn, 'run', run)
    run_main(monkeypatch, 'fallback_service', '--web-root', root, '--notification-root', root / 'notifications',
             '--server-file', root / 'web-server.json', '--host', '127.0.0.1', '--port', port)
    status, body = run.results[0]
    health = json.loads(body)
    assert status == 200 and health['ok'] is True and health['monitor']['error'] is None
    assert (root / 'notifications' / 'service.lock').exists()


# ---- instrument_service (the sr422 rig broker) --------------------------------------------------------------------

def test_instrument_service_starts_the_rig_hub_and_serves_status(var, monkeypatch, offline, no_locks, restore_signals):
    config_file = var / 'device-owners/sr422/config.json'
    config = load(config_file)
    port = free_port(offline)
    config.update(bind='127.0.0.1', port=port)
    save(config_file, config)
    servers = capture_server(monkeypatch, 'ldcon.owners.instrument_service')

    def body():
        wait_for(lambda: servers, what='broker server')
        reply = rpc(port, action='status')
        os.kill(os.getpid(), signal.SIGTERM)      # the unit's stop: install_termination -> shutdown -> leases released
        return reply
    helper = Helper(body)
    run_main(monkeypatch, 'instrument_service', '--config', config_file)
    reply = helper.finish()
    assert reply['ok'] is True
    instruments = reply['result']['instruments']
    assert set(instruments) == {'repump_1092_controller', 'repump_1092'}
    assert instruments['repump_1092_controller']['fault'] == '[Errno 110] Operation timed out'   # retained, not cleared
    assert instruments['repump_1092']['relayed_to'] == 'sdg2042x:SDG2XFBCA00839' and instruments['repump_1092']['leases'] == []
    rig = reply['result']['ecdl_rigs']['sr422']
    assert rig['rig'] == 'sr422' and rig['connected'] is False and rig['fault'] is None   # instruments unopened at start


# ---- monitor_owner: the sr1092 native current owner and the scope owner -------------------------------------------

def native_policy(var):
    laser = yaml.safe_load((var / 'lasers/sr1092/config/lasers/sr1092.yaml').read_text())
    device = laser['devices'][laser['interfaces']['native_current']['device']]
    return device['control_settings']['native_current']


def serve_monitor_once(monkeypatch, port, status_file, after):
    """Keep the real ``serve_monitor``; a helper performs one status read once the first poll is published."""
    import ldcon.owners.monitor_owner as monitor_owner
    real = monitor_owner.serve_monitor
    seen = {}

    def serve_monitor(server, monitor, worker):
        seen['monitor'] = monitor

        def body():
            wait_for(lambda: status_file.exists(), what='first poll')
            status, body = http('http://127.0.0.1:%d/v1/status' % port)
            assert status == 200
            seen['status'] = json.loads(body)
            after(monitor)
            monitor.stop.set()
        seen['helper'] = Helper(body)
        return real(server, monitor, worker)
    monkeypatch.setattr(monitor_owner, 'serve_monitor', serve_monitor)
    return seen


def test_monitor_owner_native_starts_on_the_laser_yaml_and_publishes(var, monkeypatch, offline, no_locks, restore_signals):
    import ldcon.owners.esp32_native_client
    import ldcon.drivers.esp32_servo
    import ldcon.drivers.fizeau
    config_file = var / 'device-owners/esp32servocon-sr1092/config.json'
    config = load(config_file)
    port = free_port(offline)
    config['api_port'] = port
    save(config_file, config)
    monkeypatch.setenv('LDCON_BACKGROUND_LASER_ID', 'sr1092')
    policy = native_policy(var)
    servo = native.Servo(policy)
    servo.device_id = config['device_id']
    monkeypatch.setattr(ldcon.drivers.esp32_servo, 'Esp32Servo', lambda host, port, device_id: servo)
    monkeypatch.setattr(ldcon.owners.esp32_native_client, 'BiasLease', lambda config: native.Bias())
    monkeypatch.setattr(ldcon.drivers.fizeau, 'HttpFizeauWavemeter', lambda *a, **k: native.Optical(time.monotonic))
    status_file = Path(config['status_file'])
    seen = serve_monitor_once(monkeypatch, port, status_file, lambda monitor: None)
    run_main(monkeypatch, 'monitor_owner', '--config', config_file)
    seen['helper'].finish()
    owner = seen['monitor']
    status = seen['status']
    assert status['device_id'] == config['device_id'] and status['read_ok'] is True and status['fault'] is None
    assert status['policy_digest'] == owner.definition
    assert load(config['state_file'])['definition'] == owner.definition
    events = {e['kind']: e for e in owner.input_events}
    assert events['policy_source']['source']['kind'] == 'live'      # g8bb1f8545d2d: 'kind' clashed with _report_event
    assert events['policy_source']['source']['path'] == str(var / 'lasers/sr1092/config/lasers/sr1092.yaml')
    assert 'layer1_copy_ignored' in events                           # the untrimmed live config.json still carries copies
    assert servo.writes == []                                        # a start never writes to the firmware


def test_monitor_owner_scope_starts_polls_and_publishes(var, monkeypatch, offline, no_locks, restore_signals):
    import ldcon.drivers.rigol_scope
    config_file = var / 'device-owners/scope-sr422-sr1092/config.json'
    config = load(config_file)
    port = free_port(offline)
    config['api_port'] = port
    save(config_file, config)
    reads = []

    class FakeScope:
        capabilities = ['oscilloscope_monitor']

        def __init__(self, host, port, device_id, serial=None):
            self.device_id, self.serial = device_id, serial

        def read(self):
            reads.append(time.time())
            return dict(serial=self.serial, channels={'1': dict(vpp=.1)})

        def close(self):
            pass
    monkeypatch.setattr(ldcon.drivers.rigol_scope, 'RigolScope', FakeScope)
    status_file = Path(config['status_file'])
    seen = serve_monitor_once(monkeypatch, port, status_file, lambda monitor: None)
    run_main(monkeypatch, 'monitor_owner', '--config', config_file)
    seen['helper'].finish()
    status = seen['status']
    assert status['device_id'] == 'scope-sr422-sr1092' and status['read_ok'] is True and status['state'] == 'connected'
    assert status['sample']['serial'] == config['serial'] and status['read_only'] is True
    assert len(reads) >= 1 and load(status_file)['read_ok'] is True


# ---- instrument_owner (SDG2042X), cld_owner (CLD1015), device_registry ---------------------------------------------

class FakeSDG(awg.FakeAWG):
    """The projection-transport AWG fake plus the live owner's identity and the global ROSC setting."""
    def __init__(self, identity):
        super().__init__()
        self.identity = identity
        self.offsets = {1: .5, 2: 0.}       # CH1 = sr422 TEC DC inside the safe-start value_range
        self.reference = 'INT'

    def query(self, command):
        if command.startswith('ROSC'):
            return 'ROSC ' + self.reference
        return super().query(command)

    def write(self, command):
        if command.startswith('ROSC'):
            self.reference = command.split()[1]
            return
        super().write(command)


def test_instrument_owner_sdg_starts_connects_applies_settings_and_publishes(var, monkeypatch, offline, no_locks, restore_signals):
    import ldcon.drivers.siglent
    config_file = var / 'instrument-owners/sdg2042x-SDG2XFBCA00839/config.json'
    config = load(config_file)
    port = free_port(offline)
    config['port'] = port
    save(config_file, config)
    raw = FakeSDG(config['expected_identity'])
    monkeypatch.setattr(ldcon.drivers.siglent, 'VisaTransport', lambda resource, timeout, backend=None: raw)
    for source in config['projection_sources'].values():          # the unit's service.env supplies the signing keys
        monkeypatch.setenv(source['key_env'], 'entry-startup-fixture-key-' + source['system_id'] + '-' + '0' * 32)
    servers = capture_server(monkeypatch, 'ldcon.owners.instrument_service')

    def body():
        wait_for(lambda: servers, what='owner server')
        reply = rpc(port, action='status')
        published = load(Path(config['state_dir']) / 'owner-state.json')   # what the registry reads while the owner runs
        os.kill(os.getpid(), signal.SIGTERM)
        return reply, published
    helper = Helper(body)
    run_main(monkeypatch, 'instrument_owner', '--config', config_file)
    reply, published = helper.finish()
    assert reply['ok'] is True
    status = reply['result']
    assert status['instrument'] == 'sdg2042x:SDG2XFBCA00839' and status['connected'] is True and status['fault'] is None
    assert status['identity'] == config['expected_identity']
    assert published['connected'] is True and published['fault'] is None and published['endpoint'] == 'http://127.0.0.1:%d' % port
    assert load(Path(config['state_dir']) / 'owner-state.json')['connected'] is False       # re-published at close
    assert load(Path(config['state_dir']) / 'state.json')['leases'] == {}
    assert raw.reference == 'EXT' and 'ROSC EXT' not in raw.writes          # the global setting was applied (ROSC path)
    assert not [w for w in raw.writes if 'OFST' in w]                         # channel outputs untouched at start


def test_cld_owner_starts_polls_and_serves_status(var, monkeypatch, offline, no_locks):
    import ldcon.owners.cld_owner
    config_file = var / 'device-owners/cld1015-M01343851/config.json'
    config = load(config_file)
    port = free_port(offline)
    config['port'] = port
    save(config_file, config)
    save(config['handoff_file'], dict(schema_version=1, resource=config['resource'], leases_empty=True, transport_closed=True,
                                      reviewed_by='entry start-up fixture', reviewed_at_utc='2026-10-27T00:00:00Z',
                                      evidence='synthetic', disposition='new_device'))
    from ldcon.drivers.cld1015 import MockIO

    class PinnedMockIO(MockIO):
        def query(self, command):
            if command == '*IDN?':
                self.log.append(command)
                return 'THORLABS,CLD1015,%s,1.0' % config['identity']['serial']
            return super().query(command)
    raw = PinnedMockIO()
    monkeypatch.setattr(ldcon.owners.cld_owner, 'VisaIO', lambda resource, timeout_s, physical_lock=None: raw)
    servers = capture_server(monkeypatch, 'ldcon.owners.cld_owner')

    def body():
        wait_for(lambda: servers, what='CLD server')
        wait_for(lambda: Path(config['state_file']).exists(), what='CLD state file')
        reply = rpc(port, action='status')
        servers[0].shutdown()                  # cld_owner.main has no signal handler; systemd's SIGTERM ends serve_forever
        return reply
    helper = Helper(body)
    run_main(monkeypatch, 'cld_owner', '--config', config_file)
    status = helper.finish()
    assert status['ok'] is True
    assert status['result']['device_id'] == 'cld1015_M01343851' and status['result']['fault'] is None
    assert status['result']['writes_enabled'] is False
    assert [c for c in raw.log if not c.endswith('?')] == []          # polling reads only
    assert (Path(config['state_file']).with_name('audit.jsonl')).exists()


def test_device_registry_starts_and_answers_endpoint_queries(var, monkeypatch, offline, restore_signals):
    registry_file = var / 'instrument-owners/registry.json'
    port = free_port(offline)
    save(registry_file, dict(bind='127.0.0.1', port=port, state_dir=str(var / 'instrument-owners'), start_timeout_s=30.,
                             probe_timeout_s=3., instruments={
                                 'sdg2042x:SDG2XFBCA00839': dict(endpoint='http://127.0.0.1:47151', kind='awg', description='CH1 sr422 TEC'),
                                 'sdg2042x:SDG2XFBCA00845': dict(endpoint='http://127.0.0.1:47153', kind='awg', description='sr422 LDC/PZT')}))
    servers = capture_server(monkeypatch, 'ldcon.owners.instrument_service')

    def body():
        wait_for(lambda: servers, what='registry server')
        reply = rpc(port, action='endpoint', instrument='sdg2042x:SDG2XFBCA00839')
        os.kill(os.getpid(), signal.SIGTERM)
        return reply
    helper = Helper(body)
    run_main(monkeypatch, 'device_registry', '--config', registry_file)
    reply = helper.finish()
    assert reply['ok'] is True and reply['result']['endpoint'] == 'http://127.0.0.1:47151'


# ---- collectors: dashboard_metrics, owner_telemetry, esp32_time_telemetry ------------------------------------------

def test_dashboard_metrics_collects_one_cycle_and_sends_metadata(var, monkeypatch, offline):
    sink = udp_sink(offline)
    registry = var / 'dashboard/services.json'
    for laser in ('sr1092', 'sr422'):
        save(var / 'lasers' / laser / 'web-server.json', dict(pid=0, host='0.0.0.0', port=1, path='/s/' + 'w' * 32 + '/', backend='interface-live'))
    save(registry, dict(services=[dict(id=laser, label=laser.capitalize(), origin='http://127.0.0.1:%d' % free_port(offline),
                                       state_file=str(var / 'lasers' / laser / 'web-server.json'),
                                       lasers=[dict(id=laser, name=laser.capitalize())]) for laser in ('sr1092', 'sr422')],
                        telemetry_endpoint='udp://127.0.0.1:%d' % sink.getsockname()[1]))
    run_main(monkeypatch, 'dashboard_metrics', '--registry', registry, '--interval', '5', '--once')
    lines = datagrams(sink, at_least=2)
    assert len(lines) >= 2 and all('dashboard_metadata' in line for line in lines)
    assert {re.search(r'laser_id=([^, ]+)', line).group(1) for line in lines} == {'sr1092', 'sr422'}


class FakeRigOwner(ThreadingHTTPServer):
    """An ECDL rig owner that answers ``ecdl inspect`` with a snapshot (owner_telemetry's only request)."""
    def __init__(self):
        server = self
        server.requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                size = int(self.headers.get('Content-Length', '0'))
                server.requests.append(json.loads(self.rfile.read(size)))
                body = json.dumps(dict(ok=True, result=dict(snapshot=dict(outputs=dict(current=.1, temperature=25., piezo=1.), open_loop=True),
                                                            observation={}, fault=None))).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass
        super().__init__(('127.0.0.1', 0), Handler)


def test_owner_telemetry_reads_the_rig_owner_once_and_records(var, monkeypatch, offline):
    sink = udp_sink(offline)
    owner = FakeRigOwner()
    offline.add(owner.server_address[1])
    threading.Thread(target=owner.serve_forever, daemon=True).start()
    laser_file = var / 'lasers/sr422/config/lasers/sr422.yaml'
    laser = yaml.safe_load(laser_file.read_text())
    laser['devices']['rig']['connection']['endpoint'] = 'http://127.0.0.1:%d' % owner.server_address[1]
    laser['telemetry']['telegraf_endpoint'] = 'udp://127.0.0.1:%d' % sink.getsockname()[1]
    laser_file.write_text(yaml.safe_dump(laser, sort_keys=False))
    output = var / 'lasers/sr422/data/owner-telemetry'
    try:
        run_main(monkeypatch, 'owner_telemetry', '--laser', laser_file, '--output', output, '--interval-s', '10', '--once')
    finally:
        owner.shutdown()
        owner.server_close()
    assert owner.requests == [dict(action='ecdl', instrument=laser['devices']['rig']['connection']['instrument'], request=dict(action='inspect'))]
    assert (output / 'telemetry.sqlite3').exists() and (output / 'collector.lock').exists()
    lines = datagrams(sink)
    assert lines and all('owner_sample' in line or 'producer=owner-monitor' in line for line in lines)
    assert any('read_ok=true' in line for line in lines)


def test_esp32_time_telemetry_forwards_the_status_file_once(var, monkeypatch, offline):
    sink = udp_sink(offline)
    status_file = var / 'device-owners/esp32servocon-sr1092/native-status.json'
    save(status_file, dict(schema_version=1, state='connected', read_ok=True, last_success_unix_s=time.time(),
                           sample=dict(host='192.168.1.6', port=80, servo_status=dict(
                               time_synced=True, sntp_update_delay_s=3600, sntp_syncs=3, sntp_last_sync_age_s=12.,
                               sntp_last_step_us=154., sntp_max_step_us=300., uptime_s=1234.))))
    code = run_main(monkeypatch, 'esp32_time_telemetry', '--status-file', status_file, '--host', '192.168.1.6',
                    '--device', 'esp32servocon-sr1092', '--laser', 'Sr1092', '--laser-id', 'sr1092',
                    '--endpoint', 'udp://127.0.0.1:%d' % sink.getsockname()[1], '--interval-s', '10', '--once')
    assert code == 0
    lines = datagrams(sink)
    assert len(lines) == 1 and lines[0].startswith('esp32_time_sync,')
    assert 'host=192.168.1.6' in lines[0] and 'laser_id=sr1092' in lines[0] and 'sntp_last_step_us=154' in lines[0]


# ---- interface_worker, agent_tools (positional argv; the ``__main__`` block is the entry) --------------------------

def test_interface_worker_main_runs_one_card_under_the_worker_lifetime(tmp_path, monkeypatch, offline):
    import ldcon.core.interface_cards as interface_cards
    import ldcon.jobs.interface_devices as interface_devices
    job = tmp_path / 'jobs' / 'job1'
    job.mkdir(parents=True)
    (job / 'laser.yaml').write_text(yaml.safe_dump(dict(id='fixture', name='Fixture')))
    (job / 'interface-deck.yaml').write_text(yaml.safe_dump(dict(id='fixture', cards=[
        dict(id='observe', operation='monitor.wavelength', uses=['wavelength'], parameters={})])))
    save(job / 'invocation.json', dict(card_id='observe', parameters=dict(duration_s=1.)))
    save(job / 'job.json', dict(id='job1', worker_backend='service', laser=dict(id='fixture'), cards=[dict(id='observe', operation='monitor.wavelength')]))
    opened = []
    monkeypatch.setattr(interface_devices, 'open_interfaces', lambda *a: opened.append(True) or {'wavelength': SimpleNamespace(close=lambda: None)})
    monkeypatch.setattr(interface_cards, 'monitor_wavelength', lambda *a, **k: dict(status='succeeded'))
    monkeypatch.setattr(sys, 'argv', ['ldcon.interface_worker', str(job)])
    import ldcon.jobs.interface_worker as interface_worker
    # The module's ``__main__`` block (worker_lifetime + run) executed as ``python -m`` would; run_path rather than
    # run_module so an earlier import of the module by another test does not trigger runpy's sys.modules warning.
    runpy.run_path(interface_worker.__file__, run_name='__main__')
    assert opened == [True]
    assert load(job / 'progress.json')['status'] == 'succeeded'
    identity = load(job / 'worker-process.json')
    assert identity['pid'] == os.getpid() and identity['boot_id']


def resident_job(root):
    job = root / 'jobs' / 'task1'
    job.mkdir(parents=True)
    card = dict(id='resident', operation='agent.resident', parameters={})
    save(job / 'job.json', dict(id='task1', set_id='sr1092', laser_id='sr1092', deck_id='deck', status='running', cards=[card]))
    (job / 'laser.yaml').write_text(yaml.safe_dump(dict(schema_version=1, id='sr1092', name='Sr1092', devices={}, interfaces={})))
    context = job / 'agent-tools-context.json'
    save(context, dict(job_id='task1', laser_id='sr1092', laser_name='Sr1092', deck_id='deck', card_id='resident', provider='claude',
                       workspace=str(root / 'agents/workspace'), asset_paths=[], bridge_id='fixture-bridge', previous_bridge_id=None,
                       teams_cards=[], template_revision='fixture', metric_manifest={}, execution_profile={}, agent_role='gateway'))
    return context


def test_agent_tools_main_answers_mcp_initialize_and_tools_list(tmp_path, monkeypatch, offline, capsys):
    context = resident_job(tmp_path)
    requests = [dict(jsonrpc='2.0', id=1, method='initialize', params=dict(protocolVersion='2024-11-05')),
                dict(jsonrpc='2.0', id=2, method='tools/list'),
                dict(jsonrpc='2.0', method='notifications/initialized')]
    monkeypatch.setattr(sys, 'stdin', io.StringIO(''.join(json.dumps(r) + '\n' for r in requests)))
    run_main(monkeypatch, 'agent_tools', context)
    replies = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [r['id'] for r in replies] == [1, 2]
    assert replies[0]['result']['serverInfo']['name'] == 'ldcon-resident'
    assert {t['name'] for t in replies[1]['result']['tools']} >= {'laser_metrics'}


# ---- every entry module as a subprocess from the source tree --------------------------------------------------------

def entry_environment():
    environment = {k: v for k, v in os.environ.items() if not k.startswith(('LDCON_', 'AWG_'))}
    environment.update(PYTHONPATH=os.pathsep.join(str(REPO / p) for p in ('packages/ldcon/src', 'packages/experiment-core/src')),
                       PYTHONDONTWRITEBYTECODE='1', PYTHONNOUSERSITE='1')
    return environment


@pytest.mark.parametrize('module', ARGPARSE_MODULES)
def test_help_exits_zero(module):
    result = subprocess.run([sys.executable, '-m', 'ldcon.' + module, '--help'], cwd=REPO, env=entry_environment(),
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith('usage:') and ('--config' in result.stdout or '--' in result.stdout)
    assert 'Traceback' not in result.stderr


def test_agent_tools_subprocess_exits_zero_on_closed_stdin(tmp_path):
    context = resident_job(tmp_path)
    result = subprocess.run([sys.executable, '-m', 'ldcon.agents.agent_tools', str(context)], cwd=REPO, env=entry_environment(),
                            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stdout == '' and 'Traceback' not in result.stderr


def test_interface_worker_subprocess_refuses_a_replayed_job_cleanly(tmp_path):
    """The harmless usage path: a job whose ``worker-process.json`` exists is never executed a second time."""
    job = tmp_path / 'jobs' / 'job1'
    job.mkdir(parents=True)
    save(job / 'worker-process.json', dict(pid=1, boot_id='x', start_ticks='0'))
    result = subprocess.run([sys.executable, '-m', 'ldcon.interface_worker', str(job)], cwd=REPO, env=entry_environment(),
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 1 and 'Job was already executed' in result.stderr
    assert not (job / 'progress.json').exists()
