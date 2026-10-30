import importlib.resources
import os
from pathlib import Path
import ldcon
import experiment_core
import subprocess
import sys

from ldcon.core import records
from experiment_core import records as core_records


def test_generic_records_use_public_core_implementation():
    assert records.atomic_json is core_records.atomic_json
    assert records.read_json is core_records.read_json


def test_base_import_has_no_optional_stack_or_transport_side_effect():
    code = """
import sys
def audit(event, args):
    if event in ('socket.__new__', 'socket.connect', 'subprocess.Popen'):
        raise AssertionError('No I/O during import')
sys.addaudithook(audit)
import ldcon
import ldcon.jobs.window_center
import ldcon.core.records
assert not any(name in sys.modules for name in ('fastapi','pyvisa','websocket','uvicorn'))
"""
    subprocess.run([sys.executable, "-c", code], check=True, env={**os.environ, "PYTHONPATH": os.pathsep.join(str(Path(module.__file__).resolve().parent.parent) for module in (ldcon, experiment_core))})


def test_installed_package_contains_ui_resources():
    assets = importlib.resources.files("ldcon").joinpath("web_assets")
    assert "<html" in assets.joinpath("index.html").read_text().lower()
    assert assets.joinpath("cards/tune.ecdl_window_center.html").is_file()
    for name in ("configuration-editor.js", "fleet-settings.js", "i18n.js", "laser-workspace.js", "workspace.css"):
        assert assets.joinpath(name).is_file()


def test_site_bindings_are_explicit(monkeypatch):
    from ldcon.core.site_bindings import path, shared_roots
    import pytest
    monkeypatch.delenv("LDCON_AGENTBRIDGE_PUBLIC", raising=False)
    monkeypatch.delenv("LDCON_SHARED_ROOTS", raising=False)
    with pytest.raises(ValueError):
        path("LDCON_AGENTBRIDGE_PUBLIC")
    with pytest.raises(ValueError):
        shared_roots()
