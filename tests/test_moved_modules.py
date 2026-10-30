"""Phase 2 package moves: every old flat name is an alias of the new module, entry stubs still run, paths stay anchored."""
import importlib, importlib.resources, os, re, subprocess, sys
from pathlib import Path

import pytest

import experiment_core
import ldcon
from ldcon.core import paths

SRC = Path(ldcon.__file__).parent
ENV = {**os.environ, 'PYTHONPATH': os.pathsep.join([str(Path(m.__file__).resolve().parent.parent) for m in (ldcon, experiment_core)] + list(filter(None, [os.environ.get('PYTHONPATH')])))}
MOVED = sorted(ldcon.MOVED.items())
ENTRY_STUBS = sorted(old for old, _ in MOVED if (SRC / (old.split('.')[1] + '.py')).is_file())
# Old-path importers kept on purpose until their own module moves (G10/G12; phase2/config-step3-4 touched these files).
DEFERRED_IMPORTERS = set()  # every importer of a moved module names the new path


@pytest.mark.parametrize('old,new', MOVED, ids=[old for old, _ in MOVED])
def test_old_name_is_the_new_module(old, new):
    module = importlib.import_module(new)
    assert importlib.import_module(old) is module
    assert sys.modules[old] is module and module.__name__ == new and module.__spec__.name == new
    attribute = old.split('.')[1]
    assert getattr(ldcon, attribute) is module
    namespace = {}
    exec(f'from ldcon import {attribute} as m; from {old} import __name__ as n', namespace)
    assert namespace['m'] is module and namespace['n'] == new
    assert new.rsplit('.', 1)[1] == attribute, 'moved modules keep their file name'


def test_table_targets_exist_and_old_files_are_stubs_or_gone():
    for old, new in MOVED:
        assert importlib.util.find_spec(new) is not None
        stub = SRC / (old.split('.')[1] + '.py')
        if stub.is_file():
            text = stub.read_text()
            assert 'from ldcon._moved import alias' in text and len(text.splitlines()) <= 6, stub


@pytest.mark.parametrize('old', ENTRY_STUBS)
def test_entry_stub_runs_the_moved_main(old):
    new = ldcon.MOVED[old]
    runs = [subprocess.run([sys.executable, '-m', name, '--help'], env=ENV, capture_output=True, text=True, timeout=60) for name in (old, new)]
    # the old name exits exactly as the new one: rc 0 and the usage text for argparse entries, the same
    # error for positional ones (agent_tools reads argv[1] as a path; test_entry_startup runs it with a context)
    assert runs[0].returncode == runs[1].returncode, [r.stderr[-500:] for r in runs]
    assert runs[0].stdout.replace(old.split('.')[1], 'PROG') == runs[1].stdout.replace(new.rsplit('.', 1)[1], 'PROG')
    assert runs[0].stderr.strip().splitlines()[-1:] == runs[1].stderr.strip().splitlines()[-1:]


def test_old_names_resolve_in_a_fresh_interpreter_and_run_as_module_without_a_stub():
    code = 'import importlib, ldcon\nprint(all(importlib.import_module(old) is importlib.import_module(new) for old, new in ldcon.MOVED.items()))'
    run = subprocess.run([sys.executable, '-c', code], env=ENV, capture_output=True, text=True)
    assert run.returncode == 0 and run.stdout.strip() == 'True', run.stderr
    old = 'ldcon.dither_quality'  # no __main__ block, no stub file: runpy goes through the finder's get_code
    if old in ldcon.MOVED:
        assert not (SRC / 'dither_quality.py').exists()
        run = subprocess.run([sys.executable, '-m', old], env=ENV, capture_output=True, text=True, timeout=60)
        assert run.returncode == 0, run.stderr


def test_new_packages_import_without_io_or_optional_stacks():
    packages = sorted(p.name for p in SRC.iterdir() if p.is_dir() and (p / '__init__.py').is_file() and p.name != 'drivers')
    code = '\n'.join(['import sys',
                      'def audit(event, args):',
                      "    if event in ('socket.__new__', 'socket.connect', 'subprocess.Popen'): raise AssertionError('No I/O during import')",
                      'sys.addaudithook(audit)',
                      *[f'import ldcon.{p}' for p in packages],
                      "assert not any(name in sys.modules for name in ('fastapi','pyvisa','websocket','uvicorn')), sorted(sys.modules)"])
    subprocess.run([sys.executable, '-c', code], check=True, env=ENV)


def test_no_internal_importer_relies_on_the_aliases():
    old_names = {old.split('.')[1] for old, _ in MOVED}
    pattern = re.compile(r'^\s*(?:from ldcon\.(\w+) import|from ldcon import ([\w, ]+)|import ldcon\.(\w+))', re.M)
    offenders = []
    for file in SRC.rglob('*.py'):
        if file.name == '_moved.py' or file.name in DEFERRED_IMPORTERS or file.parent == SRC and file.stem in old_names:
            continue
        for match in pattern.finditer(file.read_text()):
            names = [match.group(1), match.group(3)] + [n.strip().split(' as ')[0] for n in (match.group(2) or '').split(',')]
            hit = sorted(set(filter(None, names)) & old_names)
            if hit: offenders.append((str(file.relative_to(SRC)), hit))
    assert not offenders, offenders


def test_worker_spawn_argv_still_matches_the_card_fallback_cmdline_check(tmp_path, monkeypatch):
    """G14 moved interface_worker into ldcon.jobs, but the spawned argv must keep the old name: agents.card_fallback
    recognises a live worker by the exact argv element b'ldcon.interface_worker' (the entry stub keeps that cmdline)."""
    import inspect, time
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from ldcon.agents import card_fallback
    from ldcon.jobs import job_service
    from ldcon.web import web_runtime
    spawned = []
    monkeypatch.setattr(job_service, 'subprocess', SimpleNamespace(DEVNULL=subprocess.DEVNULL, Popen=lambda argv, **kw: spawned.append(argv) or SimpleNamespace(pid=1)))
    job_service.launch(tmp_path)
    assert spawned and spawned[0][1:3] == ['-m', 'ldcon.interface_worker'] and 'ldcon.interface_worker' in ldcon.MOVED
    assert (SRC / 'interface_worker.py').is_file(), 'the entry stub keeps `-m ldcon.interface_worker` runnable'
    assert "'-m','ldcon.interface_worker'" in inspect.getsource(web_runtime), 'web_runtime spawns the same cmdline'
    assert b'ldcon.interface_worker' in inspect.getsource(card_fallback.worker_alive).encode()
    # A live process whose argv carries the old name (as the spawn does) is recognised; the new dotted path would not be.
    job = tmp_path / 'jobs' / 'job1'
    job.mkdir(parents=True)
    created = datetime.now(timezone.utc).isoformat()
    for module, expected in (('ldcon.interface_worker', True), ('ldcon.jobs.interface_worker', False)):
        process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)', '-m', module, str(job)])
        try:
            time.sleep(0.2)
            assert card_fallback.worker_alive(dict(pid=process.pid, id='job1', created_utc=created)) is expected, module
        finally:
            process.kill(); process.wait()


def test_package_paths_are_anchored_on_the_package():
    assert paths.web_assets_dir() == Path(str(importlib.resources.files('ldcon') / 'web_assets')) and (paths.web_assets_dir() / 'index.html').is_file()
    assert (paths.resources_dir() / 'agent-model-options.json').is_file() and (paths.resources_dir() / 'report_template').is_dir()
    assert paths.source_root() / 'ldcon' == paths.PACKAGE_DIR.resolve() and paths.checkout_root() == paths.source_root().parent
