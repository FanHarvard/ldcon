"""Explicit host bindings, resolved only when a selected integration starts.

Values are paths/references, never credentials. Deployment supplies these via
its environment; importing the package does not inspect a live installation.
"""
import json
import os
from pathlib import Path


def path(name):
    value = os.environ.get(name)
    if not value or not Path(value).is_absolute():
        raise ValueError("Configure an absolute deployment path in " + name)
    return Path(value)


def shared_roots():
    value = os.environ.get("LDCON_SHARED_ROOTS")
    if not value:
        raise ValueError("Configure LDCON_SHARED_ROOTS as a JSON array of immutable source roots")
    roots = json.loads(value)
    if not isinstance(roots, list) or not roots or any(not isinstance(p, str) or not Path(p).is_absolute() for p in roots):
        raise ValueError("Invalid LDCON_SHARED_ROOTS")
    return [Path(p) for p in roots]
