"""Reviewed, two-phase dashboard migration; retained owners never receive writes.

The first release transition deliberately preserves each canonical state root.
Owner/config relocation requires an owner-specific procedure, not file copying.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import ipaddress
import math
import os
import pwd
import grp
from pathlib import Path
import re
import secrets
import shutil
import socket
import sqlite3
import struct
import subprocess
import sys
import time
from urllib.parse import urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler

TERMINAL = {'succeeded', 'completed', 'completed_with_failures', 'failed', 'cancelled', 'interrupted', 'inconclusive'}
CONFIG_DIRS = ('lasers', 'interface-decks', 'assignments')
NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}')


def read(path):
    return json.loads(Path(path).read_text())


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def save(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w') as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(0o660)
    temporary.replace(path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def files(root, directories=None):
    root = Path(root)
    result = {}
    paths = [root / d for d in directories] if directories else [root]
    for directory in paths:
        if directory.is_symlink():
            raise ValueError('Symlinks require a separate migration review: ' + str(directory))
        if not directory.exists():
            continue
        for path in sorted(directory.rglob('*')):
            if path.is_symlink():
                raise ValueError('Symlink in migration input: ' + str(path))
            if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc':
                result[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def configuration_hashes(root):
    result = files(root, CONFIG_DIRS)
    for name in ('service.env', 'log-retention.json'):
        path=Path(root)/name
        if path.is_symlink():
            raise ValueError('Configuration sidecar cannot be a symlink')
        if path.exists():
            result[name]=hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def identity(pid):
    from ldcon.jobs.job_service import identity as process_identity
    result = process_identity(pid)
    if result is None:
        raise ValueError('Process identity is absent: ' + str(pid))
    return result


def identity_gone(saved):
    from ldcon.jobs.job_service import identity as process_identity
    return process_identity(saved['pid']) != saved


def pointer(value, path):
    if not isinstance(path, str) or not path.startswith('/'):
        raise ValueError('Expected a JSON pointer')
    for part in path[1:].split('/'):
        part = part.replace('~1', '/').replace('~0', '~')
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


def predicates(value, expected):
    return all(type(pointer(value, path)) is type(want) and pointer(value, path) == want
               for path, want in expected.items())


def local_host(host):
    if host in ('localhost','0.0.0.0','::'):
        return '127.0.0.1' if host!='::' else '::1'
    try:
        address=ipaddress.ip_address(host)
    except ValueError:
        raise ValueError('Use a literal local interface address') from None
    if address.is_loopback:
        return host
    addresses=set()
    try:
        with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as handle:
            for _,name in socket.if_nameindex():
                try:
                    packed=fcntl.ioctl(handle.fileno(),0x8915,struct.pack('256s',name.encode()[:15]))
                    addresses.add(socket.inet_ntoa(packed[20:24]))
                except OSError:
                    pass
    except OSError:
        pass
    ipv6=Path('/proc/net/if_inet6')
    if ipv6.exists():
        addresses.update(str(ipaddress.IPv6Address(int(line.split()[0],16))) for line in ipv6.read_text().splitlines())
    if str(address) not in addresses:
        raise ValueError('Endpoint is not a verified local network interface')
    return host


def no_pending_reservations(root):
    folder=Path(root)/'reservations'
    if folder.is_symlink():
        raise ValueError('Reservations directory cannot be a symlink')
    for path in folder.glob('*.json'):
        if path.is_symlink() or read(path).get('status') not in ('dispatched','cancelled','expired','blocked'):
            raise ValueError('Pending or unknown reservation exists; resolve it before migration')
    # Migration inspection must not create queues, policy tables or notifications.
    # An unresolved case protects its laser even without a next reservation.
    notifications=Path(root)/'notifications'
    cases=notifications/'failure-cases.sqlite3'
    if notifications.is_symlink() or cases.is_symlink():
        raise ValueError('Failure recovery storage cannot be a symlink')
    if not cases.exists():return
    try:
        with sqlite3.connect(cases.resolve().as_uri()+'?mode=ro',uri=True,timeout=10) as db:
            rows=db.execute('SELECT data FROM cases').fetchall()
        for row in rows:
            value=json.loads(row[0])
            if (not isinstance(value,dict) or value.get('blocks_downstream') is not False
                    or value.get('disposition') not in ('resolved','no_agent')):
                raise ValueError('Unresolved failure recovery case exists; resolve it before migration')
    except (sqlite3.Error,OSError,json.JSONDecodeError) as error:
        raise ValueError('Failure recovery evidence cannot be read; migration is blocked') from error


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *unused, **kwargs):
        raise ValueError('Migration HTTP redirects are forbidden')


class IO:
    def http(self, url, method='GET', data=None, token_env=None, migration_token=None):
        parsed = urlsplit(url)
        if parsed.scheme != 'http' or parsed.username or parsed.password or parsed.fragment:
            raise ValueError('Migration endpoints must be direct local HTTP')
        local_host(parsed.hostname)
        headers = {'X-LDCon-Client': 'web', 'Content-Type': 'application/json'}
        if migration_token:
            headers['X-LDCon-Migration'] = migration_token
        if token_env:
            headers['Authorization'] = 'Bearer ' + os.environ[token_env]
        request = Request(url, method=method, headers=headers,
                          data=None if data is None else json.dumps(data).encode())
        # No HTTP mutation retries: a lost reply is uncertain, including job start.
        try:
            with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=10) as response:
                return json.load(response)
        except (HTTPError, URLError) as error:
            raise RuntimeError('Local HTTP request failed; outcome may be uncertain ('+type(error).__name__+')') from None

    def run(self, argv):
        return subprocess.run(argv, check=True, capture_output=True, text=True, timeout=30,
                              env={**os.environ, 'PYTHONDONTWRITEBYTECODE':'1'}).stdout

    def sleep(self, seconds):
        time.sleep(seconds)

    def now(self):
        return time.monotonic()


def endpoint(server_file):
    server = read(server_file)
    host=local_host(server.get('host'))
    if not re.fullmatch(r'/s/[A-Za-z0-9_-]{24,80}/', server.get('path', '')):
        raise ValueError('Invalid local dashboard server file')
    host = '[' + host + ']' if ':' in host else host
    return f"http://{host}:{int(server['port'])}" + server['path'].rstrip('/')


def api(io, server_file, tail, method='GET', data=None, migration_token=None):
    if migration_token is not None:
        return io.http(endpoint(server_file) + '/api/' + tail, method, data, migration_token=migration_token)
    return io.http(endpoint(server_file) + '/api/' + tail, method, data)


def active_jobs(io, server_file):
    """Older dashboards ignore active_only; terminal history must not block us.

    Unknown or absent status remains active and therefore fails the normal
    reviewed-job checks. Never infer that an unrecognized job has stopped.
    """
    jobs = api(io, server_file, 'jobs?active_only=true')['jobs']
    if not isinstance(jobs, list) or any(not isinstance(job, dict) for job in jobs):
        raise ValueError('Source returned an invalid job inventory')
    return [job for job in jobs if job.get('status') not in TERMINAL]


def process_cgroups(pid):
    return [line.split(':',2)[2] for line in Path(f'/proc/{pid}/cgroup').read_text().splitlines()]


def verify_stop_boundary(io, service, owners, enumerated):
    cgroup=io.run(service_argv(service,'show')+['--property=ControlGroup','--value']).strip()
    if not cgroup or not cgroup.startswith('/'):
        raise ValueError('Source service has no verified control group')
    for owner in owners:
        if any(p==cgroup or p.startswith(cgroup.rstrip('/')+'/') for p in process_cgroups(owner['pid'])):
            raise ValueError('Source service contains a retained owner process')
    for field in ('PropagatesStopTo','ConsistsOf','BoundBy','RequiredBy'):
        dependencies=io.run(service_argv(service,'show')+['--property='+field,'--value']).split()
        if any(unit not in enumerated for unit in dependencies):
            raise ValueError('Source service stop can propagate beyond reviewed units: '+field)


def verify_service_access(deployment, release, python):
    system=any(s['scope']=='system' for s in deployment['target_services'])
    account=pwd.getpwnam(deployment.get('service_user','FanLabAdmin')) if system else pwd.getpwuid(os.geteuid())
    gid=grp.getgrnam(deployment.get('service_group','FanLabAdmin')).gr_gid if system else os.getegid()
    groups=set(os.getgrouplist(account.pw_name,gid))
    def allowed(path,need):
        path=Path(path).resolve()
        for parent in reversed(path.parents):
            check(parent,1)
        check(path,need)
    def check(path,need):
        stat=path.stat()
        if account.pw_uid==0:
            return
        shift=6 if stat.st_uid==account.pw_uid else 3 if stat.st_gid in groups else 0
        if (stat.st_mode>>shift)&need != need:
            raise ValueError('Target service account lacks required access: '+str(path))
    allowed(python,5)
    for path in Path(release).rglob('*'):
        allowed(path,5 if path.is_dir() else 4)
    root=Path(deployment['state_root'])
    allowed(root,7)
    for name in (*CONFIG_DIRS,'jobs'):
        allowed(root/name,7)
    for name in CONFIG_DIRS:
        for path in (root/name).glob('*.yaml'):
            allowed(path,6)
    marker=root/'migration-inhibit.json'
    if marker.exists():allowed(marker,6)
    # A later atomic replacement inherits this directory's group (setgid) or
    # the coordinator's effective group. Verify that the target can read/write it.
    directory_stat=root.stat()
    publication_gid=directory_stat.st_gid if directory_stat.st_mode&0o2000 else os.getegid()
    if account.pw_uid not in (0,os.geteuid()) and publication_gid not in groups:
        raise ValueError('Coordinator marker replacements would be inaccessible to target service; prepare setgid service-group state directory')
    environment=root/'service.env'
    if environment.exists():allowed(environment,4)
    parent=Path(deployment['target_server_file']).parent
    allowed(parent,7)


def service_argv(service, action):
    if not re.fullmatch(r'ldcon-[A-Za-z0-9_.-]+\.service', service['unit']):
        raise ValueError('Only enumerated ldcon service units are accepted')
    if any(word in service['unit'] for word in ('owner', 'instrument')):
        raise ValueError('Owner units are outside this migration scope')
    if service['scope'] not in ('system', 'user'):
        raise ValueError('Service scope must be system or user')
    return ['/usr/bin/systemctl'] + (['--user'] if service['scope'] == 'user' else []) + [action, service['unit']]


def probe(io, owner):
    spec = owner['probe']
    data = spec.get('rpc')
    if data is not None:
        command = data.get('request') if data.get('action') == 'ecdl' else data
        if command.get('action') not in ('status', 'inspect', 'describe'):
            raise ValueError('Owner probe must be a typed read-only operation')
        allowed={'action'} if data.get('action')=='ecdl' else {'action','instrument','device_id'}
        if set(command)-allowed or (data.get('action') == 'ecdl' and set(data) != {'action', 'instrument', 'request'}):
            raise ValueError('Unexpected read-only probe arguments')
        if any(not isinstance(v,str) or not NAME.fullmatch(v) for k,v in command.items() if k!='action'):
            raise ValueError('Invalid read-only owner identifier')
    value = io.http(spec['url'], 'POST' if data is not None else 'GET', data, spec.get('token_env'))
    if data is not None:
        if value.get('ok') is not True:
            raise ValueError('Owner read-only probe failed: ' + owner['id'])
        value = value['result']
    return value


def job_directory(root, job_id):
    if not NAME.fullmatch(job_id):
        raise ValueError('Invalid job ID')
    root = Path(root)
    matches = [p for p in (root/'jobs'/job_id, *list((root/'jobs').glob('*/'+job_id))) if (p/'job.json').is_file()]
    if len(matches) != 1 or matches[0].is_symlink() or matches[0].parent.is_symlink():
        raise ValueError('Job directory is missing or ambiguous')
    return matches[0]


def source_healthy(job, deployment):
    if not predicates(job,deployment['healthy_equals']):
        return False
    try:
        stamp=datetime.fromisoformat(job['updated_utc'].replace('Z','+00:00'))
        if stamp.tzinfo is None:
            return False
        age=time.time()-stamp.timestamp()
        return -2 <= age <= deployment.get('source_max_age_s',15)
    except (KeyError,TypeError,ValueError):
        return False


def job_snapshot(root, job):
    directory = job_directory(root, job['id'])
    invocation = read(directory/'invocation.json')
    if job.get('mode') != 'continuous' or job.get('status') != 'running' or job.get('read_only'):
        raise ValueError('Only running continuous control cards can be captured')
    process = read(directory/'worker-process.json') if (directory/'worker-process.json').exists() else identity(job['pid'])
    if identity(process['pid']) != process:
        raise ValueError('Worker identity changed')
    for name in ('online-request.json', 'hot-control-request.json'):
        if (directory/name).exists():
            raise ValueError('Finish active online/hot control before migration')
    for name in ('online-status.json','hot-control-status.json'):
        status=read(directory/name) if (directory/name).exists() else {}
        if status and status.get('status') not in ('succeeded','unchanged','failed','cancelled','rejected'):
            raise ValueError('Finish consumed online/hot control before migration')
    return {'id':job['id'], 'set_id':job['set_id'], 'deck_id':job['deck_id'],
            'invocation':invocation, 'process':process,
            'snapshots':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in directory.iterdir()
                         if p.name in ('job.json','invocation.json','laser.yaml','deck.yaml','interface-deck.yaml','set.yaml')}}


def validate_request(request):
    if request.get('schema_version') != 1 or not request.get('deployments') or not request.get('owners'):
        raise ValueError('Migration request needs version, deployments and owner probes')
    seen = set()
    target_units = set()
    source_units = set()
    if not Path(request['python']).is_absolute() or not Path(request['python']).is_file():
        raise ValueError('Use a prepared absolute Python interpreter')
    if not 1 <= request.get('timeout_s',120) <= 600 or not 60 <= request.get('valid_for_s',3600) <= 86400:
        raise ValueError('Bounded timeout (1..600s) and plan lifetime (60..86400s) required')
    for deployment in request['deployments']:
        if not NAME.fullmatch(deployment['id']) or deployment['id'] in seen:
            raise ValueError('Unique deployment IDs are required')
        seen.add(deployment['id'])
        root = Path(deployment['state_root']).resolve()
        if str(root) != deployment['state_root'] or deployment.get('target_state_root', str(root)) != str(root):
            raise ValueError('Owner-safe first migration must preserve each canonical state root')
        if 'legacy_exclusive_maintenance' in deployment and type(deployment['legacy_exclusive_maintenance']) is not bool:
            raise ValueError('Legacy maintenance acknowledgement must be boolean')
        if deployment.get('legacy_exclusive_maintenance') and not str(deployment.get('legacy_maintenance_reason','')).strip():
            raise ValueError('Legacy maintenance needs a reviewed exclusive-access reason')
        if not 1 <= deployment.get('source_max_age_s',15) <= 60:
            raise ValueError('Source progress freshness must be 1..60 seconds')
        if not deployment.get('selected_sets') or not deployment.get('healthy_equals'):
            raise ValueError('Select sets and explicit healthy job predicates')
        if not deployment.get('source_services'):
            raise ValueError('Enumerate source Web/launcher services')
        for service in deployment['source_services'] + deployment['target_services']:
            if service.get('role') not in ('web','jobs'):
                raise ValueError('Only Web and launcher services are in migration scope')
            service_argv(service, 'status')
        for service in deployment['source_services']:
            source_units.add((service['scope'],service['unit']))
        for service in deployment['target_services']:
            key=(service['scope'],service['unit'])
            if key in target_units:
                raise ValueError('Target service names must be unique across deployments')
            target_units.add(key)
        if len({s['scope'] for s in deployment['target_services']})!=1:
            raise ValueError('Use one target service manager per deployment')
        if len(deployment['target_services'])!=2 or {s['role'] for s in deployment['target_services']} != {'web', 'jobs'}:
            raise ValueError('Each target needs a separate web and jobs unit')
    if source_units & target_units:
        raise ValueError('Use distinct target units; never replace a loaded source unit in preparation')
    roots = [d['state_root'] for d in request['deployments']]
    if len(roots) != len(set(roots)):
        raise ValueError('State roots must remain distinct')
    for owner in request['owners']:
        if not owner.get('idle_equals') or not owner.get('healthy_equals') or not owner.get('hold_paths') or not owner.get('config_files'):
            raise ValueError('Owner idle, health, hold and config authority checks are mandatory')
        if owner.get('relocate'):
            raise ValueError('Owner relocation needs a separately reviewed per-owner procedure')
        identity(owner['pid'])
        if any(type(t) not in (int,float) or not math.isfinite(t) or t<0 for t in owner['hold_paths'].values()):
            raise ValueError('Output hold tolerances must be finite non-negative values')


def offline_check(root, jobs):
    from ldcon.core.interface_schema import InterfaceRepository, readiness
    repository = InterfaceRepository(root)
    assignments = {item['id']:repository.assignment(item['id']) for item in repository.list('assignments')}
    for job in jobs:
        bundle = assignments[job['set_id']]
        deck = next(d for d in bundle['deck_definitions'] if d['id'] == job['deck_id'])
        card = next(c for c in deck['document']['cards'] if c['id'] == job['invocation']['card_id'])
        result = readiness(bundle['laser_definition']['document'], deck['document'], card, job['invocation']['parameters'])
        if not result['runnable'] or result['parameters'] != job['invocation']['parameters']:
            raise ValueError('Captured invocation is not exactly runnable in target schema: ' + job['id'])


def unit_text(deployment, role, release, python):
    root = deployment['state_root']
    if any(c in str(x) for x in (root, release, python, deployment['target_server_file']) for c in ('\n', '"', '%')):
        raise ValueError('Unit paths contain unsupported quoting')
    command = f'"{python}" "{release}/migration-service.py" {role} --state-dir "{root}"'
    if role == 'web':
        command += f' --state-file "{deployment["target_server_file"]}" --host 127.0.0.1 --port {int(deployment["target_port"])}'
    user = deployment.get('service_user', 'FanLabAdmin')
    group = deployment.get('service_group', 'FanLabAdmin')
    if not NAME.fullmatch(user) or not NAME.fullmatch(group):
        raise ValueError('Invalid service user/group')
    target = next(s for s in deployment['target_services'] if s['role'] == role)
    account = f'User={user}\nGroup={group}\n' if target['scope'] == 'system' else ''
    return ('[Unit]\nDescription=LDCon prepared '+role+' '+deployment['id']+'\nAfter=network-online.target\n'
            '[Service]\nType=simple\n'+account+f'WorkingDirectory="{root}"\n'
            f'Environment="PYTHONPATH={release}/src"\nEnvironment=PYTHONDONTWRITEBYTECODE=1\n'
            'Environment=LDCON_JOB_LAUNCHER=service\n'+f'EnvironmentFile=-{root}/service.env\n'
            f'ExecStart={command}\nUMask=0007\nKillMode=process\nRestart=no\nTimeoutStopSec=30\n'
            '[Install]\nWantedBy='+('multi-user.target' if target['scope']=='system' else 'default.target')+'\n')


def prepare(request, output, *, apply=False, io=None):
    io = io or IO()
    validate_request(request)
    output = Path(output).resolve()
    code = Path(request['source_code']).resolve()
    if code/'src/ldcon/migration/__init__.py' != Path(__file__).resolve():
        raise ValueError('Run preparation using the selected source release via PYTHONPATH')
    if output == code or code in output.parents:
        raise ValueError('Prepare outside the source checkout')
    io.run([request['python'],'-c','import yaml, fastapi, uvicorn, numpy, scipy; from importlib.metadata import version; print(version("PyYAML"), version("fastapi"), version("uvicorn"), version("numpy"), version("scipy"))'])
    result = {'python_sha256':hashlib.sha256(Path(request['python']).read_bytes()).hexdigest(), 'schema_version':1, 'created_unix_s':time.time(), 'expires_unix_s':time.time()+request.get('valid_for_s',3600),
              'request':request, 'release':str(output/'release'), 'deployments':[], 'owners':[], 'applied':apply}
    for owner in request['owners']:
        value = probe(io, owner)
        if not predicates(value, owner['healthy_equals']):
            raise ValueError('Owner not healthy at preparation: ' + owner['id'])
        result['owners'].append({'id':owner['id'], 'process':identity(owner['pid']),
                                'config_hashes':{p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in owner['config_files']}})
    for deployment in request['deployments']:
        no_pending_reservations(deployment['state_root'])
        jobs = active_jobs(io, deployment['source_server_file'])
        if {j['set_id'] for j in jobs} != set(deployment['selected_sets']) or len(jobs) != len(deployment['selected_sets']):
            raise ValueError('Every active source job must match the selected continuous locks exactly')
        if not all(source_healthy(j,deployment) for j in jobs):
            raise ValueError('Selected source locks do not meet reviewed healthy predicates')
        captured = [job_snapshot(deployment['state_root'], j) for j in jobs]
        hashes = configuration_hashes(deployment['state_root'])
        if not hashes:
            raise ValueError('Empty configuration inventory')
        # Validate a temporary copy: repository constructors may mkdir; never write live config.
        import tempfile
        with tempfile.TemporaryDirectory(prefix='ldcon-schema-') as temporary:
            for kind in CONFIG_DIRS:
                shutil.copytree(Path(deployment['state_root'])/kind, Path(temporary)/kind)
            offline_check(temporary, captured)
        result['deployments'].append({'id':deployment['id'], 'jobs':captured, 'config_hashes':hashes,
                                      'source_server_hash':hashlib.sha256(Path(deployment['source_server_file']).read_bytes()).hexdigest(),
                                      'source_server_pid':read(deployment['source_server_file'])['pid'],
                                      'source_server_process':identity(read(deployment['source_server_file'])['pid'])})
    if not apply:
        return result
    if output.exists():
        raise ValueError('Preparation directory already exists; use a new release directory')
    output.mkdir(parents=True, mode=0o770)
    release = output/'release'
    release.mkdir()
    for name in ('src', 'scripts', 'doc'):
        if (code/name).exists():
            files(code/name)  # reject symlinks before copying
            shutil.copytree(code/name, release/name, ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    for name in ('pyproject.toml', 'AGENTS.md', 'CLAUDE.md'):
        if (code/name).is_file():
            shutil.copy2(code/name, release/name)
    (release/'migration-service.py').write_text("import os, pathlib, runpy, sys\n"
        "root = str(pathlib.Path(__file__).resolve().parent / 'src')\n"
        "sys.path.insert(0, root)\n"
        "os.environ['PYTHONPATH'] = root\n"
        "os.environ['LDCON_JOB_LAUNCHER'] = 'service'\n"
        "role = sys.argv.pop(1)\n"
        "module = {'web':'ldcon.web_server', 'jobs':'ldcon.job_service'}[role]\n"
        "runpy.run_module(module, run_name='__main__')\n")
    result['release_hashes'] = files(release)
    snapshot=output/'configuration-snapshot'
    snapshot.mkdir()
    for deployment,captured in zip(request['deployments'],result['deployments']):
        destination=snapshot/deployment['id']
        destination.mkdir()
        for kind in CONFIG_DIRS:
            shutil.copytree(Path(deployment['state_root'])/kind,destination/kind)
        if files(destination,CONFIG_DIRS)!={k:v for k,v in captured['config_hashes'].items() if k.split('/')[0] in CONFIG_DIRS}:
            raise ValueError('Configuration changed during preparation')
        instructions=destination/'captured-jobs.json'
        save(instructions,captured['jobs'])
        io.run([request['python'],'-c',
                'import sys,json;sys.path.insert(0,sys.argv[1]);from ldcon.migration import offline_check;offline_check(sys.argv[2],json.load(open(sys.argv[3])))',
                str(release/'src'),str(destination),str(instructions)])
    (output/'units').mkdir()
    for deployment in request['deployments']:
        for service in deployment['target_services']:
            (output/'units'/service['unit']).write_text(unit_text(deployment,service['role'],release,request['python']))
    result['unit_hashes'] = files(output/'units')
    # Pin source bytes; do not mutate any source release used by retained owners.
    for path in release.rglob('*'):
        path.chmod(0o550 if path.is_dir() else 0o440)
    release.chmod(0o550)
    result['plan_digest'] = digest(result)
    save(output/'plan.json', result)
    return result


class Cutover:
    def __init__(self, plan_path, io=None):
        self.path = Path(plan_path)
        self.plan = read(self.path)
        self.request = self.plan['request']
        self.io = io or IO()
        self.journal = self.path.parent/'cutover-journal.json'
        self.events = []
        self.held = {}
        self.migration_token = secrets.token_hex(32)

    def record(self, phase, **details):
        self.events.append({'phase':phase, 'unix_s':time.time(), **details})
        save(self.journal, {'plan_digest':self.plan['plan_digest'], 'events':self.events})

    def wait(self, check, label):
        deadline = self.io.now() + self.request.get('timeout_s',120)
        while self.io.now() < deadline:
            if check():
                return
            self.io.sleep(.5)
        raise TimeoutError(label)

    def verify(self, check_units=True):
        payload = dict(self.plan)
        expected = payload.pop('plan_digest', None)
        if not self.plan.get('applied') or digest(payload) != expected or time.time() > self.plan['expires_unix_s']:
            raise ValueError('Preparation is incomplete, changed, or expired')
        if hashlib.sha256(Path(self.request['python']).read_bytes()).hexdigest() != self.plan['python_sha256']:
            raise ValueError('Prepared Python interpreter changed')
        if files(self.plan['release']) != self.plan['release_hashes']:
            raise ValueError('Prepared release changed')
        if files(self.path.parent/'units') != self.plan['unit_hashes']:
            raise ValueError('Prepared units changed')
        for owner, captured in zip(self.request['owners'],self.plan['owners']):
            if identity(owner['pid']) != captured['process']:
                raise ValueError('Owner process identity changed')
            if any(hashlib.sha256(Path(p).read_bytes()).hexdigest() != h for p,h in captured['config_hashes'].items()):
                raise ValueError('Owner configuration changed after preparation')
            if not predicates(probe(self.io,owner),owner['healthy_equals']):
                raise ValueError('Owner is not currently healthy')
        for deployment,captured in zip(self.request['deployments'],self.plan['deployments']):
            if configuration_hashes(deployment['state_root']) != captured['config_hashes']:
                raise ValueError('Configuration changed; prepare again without stopping locks')
            if identity(captured['source_server_pid'])!=captured['source_server_process']:
                raise ValueError('Source dashboard process identity changed')
            if hashlib.sha256(Path(deployment['source_server_file']).read_bytes()).hexdigest() != captured['source_server_hash']:
                raise ValueError('Source dashboard identity changed')
            config = api(self.io,deployment['source_server_file'],'config')
            if config.get('migration_admission_gate_supported') is not True and not deployment.get('legacy_exclusive_maintenance'):
                raise ValueError('Source lacks admission gate; explicit legacy exclusive-maintenance review is required')
            no_pending_reservations(deployment['state_root'])
            active = active_jobs(self.io, deployment['source_server_file'])
            if {j['id'] for j in active} != {j['id'] for j in captured['jobs']}:
                raise ValueError('Active jobs changed')
            for job in active:
                if not source_healthy(job,deployment):
                    raise ValueError('Source lock is not in the reviewed healthy state')
                before = next(j for j in captured['jobs'] if j['id']==job['id'])
                if job_snapshot(deployment['state_root'],job) != before:
                    raise ValueError('Job identity or frozen invocation changed')
            if check_units:
                verify_service_access(deployment,self.plan['release'],self.request['python'])
                for service in deployment['source_services']:
                    enumerated={s['unit'] for d in self.request['deployments'] for s in d['source_services'] if s['scope']==service['scope']}
                    verify_stop_boundary(self.io,service,self.request['owners'],enumerated)
                    if service['role']=='web':
                        main_pid=self.io.run(service_argv(service,'show')+['--property=MainPID','--value']).strip()
                        if main_pid != str(captured['source_server_pid']):
                            raise ValueError('Source Web unit is not the captured dashboard process')
                for service in deployment['target_services']:
                    loaded = self.io.run(service_argv(service,'cat'))
                    wanted = (self.path.parent/'units'/service['unit']).read_text()
                    # systemctl cat adds '# /path' header. Reject drop-ins/extra directives.
                    actual = '\n'.join(line for line in loaded.splitlines() if not line.startswith('#'))+'\n'
                    if actual.strip() != wanted.strip():
                        raise ValueError('Target unit not installed exactly as reviewed: '+service['unit'])
                    if self.io.run(service_argv(service,'show')+['--property=ActiveState','--value']).strip() not in ('inactive','failed'):
                        raise ValueError('Target units must be inactive before cutover')

    def owner_idle(self):
        for owner in self.request['owners']:
            value = probe(self.io,owner)
            if not predicates(value,owner['idle_equals']):
                return False
            held={p:pointer(value,p) for p in owner['hold_paths']}
            if any(type(v) not in (float,int) or not math.isfinite(v) for v in held.values()):
                raise ValueError('Physical hold fields must contain finite numeric readbacks')
            self.held[owner['id']] = held
        return True

    def verify_holds(self):
        for owner,captured in zip(self.request['owners'],self.plan['owners']):
            if identity(owner['pid']) != captured['process']:
                raise ValueError('Retained owner restarted during cutover')
            value = probe(self.io,owner)
            if not predicates(value,owner['idle_equals']):
                raise ValueError('Owner no longer idle')
            for path,tolerance in owner['hold_paths'].items():
                before,after = self.held[owner['id']][path],pointer(value,path)
                if type(before) not in (int,float) or type(after) not in (int,float) or not math.isfinite(before) or not math.isfinite(after) or abs(after-before)>tolerance:
                    raise ValueError('Held physical output changed: '+owner['id']+path)

    def execute(self, *, apply=False, reviewed_digest=None):
        if self.journal.exists():
            raise ValueError('Prior cutover journal exists; inspect it, never replay uncertain starts')
        self.verify()
        if not apply:
            return {'applied':False,'plan_digest':self.plan['plan_digest'],
                    'jobs':[j['id'] for d in self.plan['deployments'] for j in d['jobs']],
                    'scope':'Web/launcher release only; canonical state and owner processes preserved'}
        if reviewed_digest != self.plan['plan_digest']:
            raise ValueError('Pass the exact reviewed plan digest')
        with (self.path.parent/'cutover.lock').open('a+b') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            if self.journal.exists():
                raise ValueError('Another cutover already created the journal')
            try:
                self.record('preflight_verified')
                for deployment in self.request['deployments']:
                    marker=Path(deployment['state_root'])/'migration-inhibit.json'
                    descriptor=os.open(marker,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o660)
                    with os.fdopen(descriptor,'w') as stream:
                        json.dump({'schema_version':1,'plan_digest':self.plan['plan_digest'],
                                   'migration_token':self.migration_token,'allowed_starts':[]},stream)
                        stream.flush();os.fsync(stream.fileno())
                # Synchronize with gated source API after marker publication.
                self.verify()
                self.record('admission_inhibited')
                # Submit all graceful stops first; one laser need not wait for the other.
                for deployment,captured in zip(self.request['deployments'],self.plan['deployments']):
                    for job in captured['jobs']:
                        self.record('stop_requested',deployment=deployment['id'],job=job['id'])
                        api(self.io,deployment['source_server_file'],'jobs/'+job['id']+'/stop','POST',{})
                for deployment,captured in zip(self.request['deployments'],self.plan['deployments']):
                    for job in captured['jobs']:
                        def stopped():
                            value=api(self.io,deployment['source_server_file'],'jobs/'+job['id'])
                            if value['status'] in TERMINAL and value['status'] != 'cancelled':
                                raise ValueError('Unexpected terminal result after stop: '+value['status'])
                            return value['status']=='cancelled' and identity_gone(job['process'])
                        self.wait(stopped,'Graceful card stop timed out')
                for deployment in self.request['deployments']:
                    no_pending_reservations(deployment['state_root'])
                    if active_jobs(self.io, deployment['source_server_file']):
                        raise ValueError('A new job appeared during graceful stop; leave services intact for review')
                self.wait(self.owner_idle,'Owner idle/held verification timed out')
                self.record('cards_stopped_and_outputs_held',holds=self.held)
                for deployment in self.request['deployments']:
                    for service in deployment['source_services']:
                        self.record('source_service_stop_requested',service=service)
                        self.io.run(service_argv(service,'stop'))
                # Source Web is gone; reject any unexpected durable job admitted in the stop boundary.
                for deployment in self.request['deployments']:
                    root=Path(deployment['state_root'])
                    no_pending_reservations(root)
                    for metadata in list((root/'jobs').glob('*/job.json'))+list((root/'jobs').glob('*/*/job.json')):
                        progress=read(metadata.parent/'progress.json')
                        if progress.get('status') not in TERMINAL:
                            raise ValueError('Unfinished job remains after source service stop')
                # State stays canonical, so no stale state copy or changed owner laser_file.
                self.verify_holds()
                for deployment,captured in zip(self.request['deployments'],self.plan['deployments']):
                    if configuration_hashes(deployment['state_root']) != captured['config_hashes']:
                        raise ValueError('Configuration changed during stop; leave held for review')
                self.record('canonical_state_preserved')
                for deployment in self.request['deployments']:
                    for role in ('jobs','web'):
                        service=next(s for s in deployment['target_services'] if s['role']==role)
                        self.record('target_service_start_requested',service=service)
                        self.io.run(service_argv(service,'start'))
                self.verify_holds()
                restarted=[]
                for deployment,captured in zip(self.request['deployments'],self.plan['deployments']):
                    def ready():
                        try:
                            server=read(deployment['target_server_file'])
                            service=next(s for s in deployment['target_services'] if s['role']=='web')
                            main_pid=self.io.run(service_argv(service,'show')+['--property=MainPID','--value']).strip()
                            if main_pid!=str(server['pid']) or int(server['port'])!=deployment['target_port']:
                                return False
                            value=api(self.io,deployment['target_server_file'],'config')
                            return value.get('state_directory')==deployment['state_root'] and value.get('migration_admission_gate_supported') is True
                        except (OSError,ValueError,RuntimeError):
                            return False
                    self.wait(ready,'Target dashboard startup timed out')
                    for job in captured['jobs']:
                        current=api(self.io,deployment['target_server_file'],'sets/'+job['set_id'])
                        body={'card_id':job['invocation']['card_id'],'parameters':job['invocation']['parameters'],
                              'expected_revision':current['revision']}
                        marker=Path(deployment['state_root'])/'migration-inhibit.json'
                        gate=read(marker)
                        if gate.get('migration_token')!=self.migration_token or gate.get('allowed_starts'):
                            raise ValueError('Migration admission marker changed')
                        gate['allowed_starts']=[dict(body,set_id=job['set_id'],deck_id=job['deck_id'])]
                        save(marker,gate)
                        self.record('card_start_requested',deployment=deployment['id'],old_job=job['id'],body=body)
                        new=api(self.io,deployment['target_server_file'],f"sets/{job['set_id']}/decks/{job['deck_id']}/start",'POST',body,migration_token=self.migration_token)
                        restarted.append((deployment,new['id']))
                        self.record('card_start_acknowledged',old_job=job['id'],new_job=new['id'])
                for deployment,new_id in restarted:
                    previous=None
                    count=0
                    def healthy():
                        nonlocal previous,count
                        job=api(self.io,deployment['target_server_file'],'jobs/'+new_id)
                        if job['status'] in TERMINAL:
                            raise ValueError('Restarted card terminated: '+new_id)
                        stamp=job.get('simulated_s')
                        if predicates(job,deployment['healthy_equals']) and isinstance(stamp,(float,int)) and (previous is None or stamp>previous):
                            count+=1
                            previous=stamp
                        elif not predicates(job,deployment['healthy_equals']):
                            count=0
                            previous=None
                        return count>=3
                    self.wait(healthy,'Restarted card did not produce three advancing healthy observations')
                for owner,captured in zip(self.request['owners'],self.plan['owners']):
                    if identity(owner['pid']) != captured['process']:
                        raise ValueError('Retained owner changed during card restart')
                    if not predicates(probe(self.io,owner),owner['healthy_equals']):
                        raise ValueError('Owner post-start health check failed')
                for deployment in self.request['deployments']:
                    marker=Path(deployment['state_root'])/'migration-inhibit.json'
                    gate=read(marker)
                    if gate.get('migration_token')!=self.migration_token or gate.get('allowed_starts'):
                        raise ValueError('Migration start permission was not consumed')
                    marker.unlink()
                self.record('completed',jobs=[j for _,j in restarted])
                return {'applied':True,'jobs':[j for _,j in restarted]}
            except Exception as error:
                self.record('stopped_for_review',error_type=type(error).__name__,message=str(error),
                            action='No automatic start retry, owner operation, or rollback. Inspect acknowledged jobs and live owners.')
                raise


def main_prepare():
    parser=argparse.ArgumentParser(description='Prepare a pinned dashboard release without stopping any card or owner')
    parser.add_argument('--request',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--apply',action='store_true',help='Write a new release, unit proposals and manifest; never install/start services')
    args=parser.parse_args()
    request = read(args.request)
    from ldcon.migration.migration_relocation import MODE, prepare_relocation
    result = (prepare_relocation(request, args.output, apply=args.apply)
              if request.get('mode') == MODE else prepare(request, args.output, apply=args.apply))
    print(json.dumps(result, ensure_ascii=False, indent=2))


def main_cutover():
    parser=argparse.ArgumentParser(description='Review or execute one guarded Web/jobs cutover; owner relocation is forbidden')
    parser.add_argument('--plan',type=Path,required=True)
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--reviewed-digest')
    args=parser.parse_args()
    from ldcon.migration.migration_relocation import MODE, dry_run_prepared
    if read(args.plan).get('mode') == MODE:
        if args.apply:
            parser.error('Full relocation is preparation-only; --apply cannot stop, synchronize or activate owners')
        result = dry_run_prepared(args.plan)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if not result['snapshot_integrity_ok'] or result['missing_staged_references']:
            raise SystemExit(2)
    else:
        print(json.dumps(Cutover(args.plan).execute(apply=args.apply,reviewed_digest=args.reviewed_digest),ensure_ascii=False,indent=2))
