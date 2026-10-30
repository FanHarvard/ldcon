"""Paths anchored on the ldcon package root, so a module's depth in the package never changes them.

Package data (`web_assets`, `resources`) stays directly under `ldcon/` (pyproject package-data,
test_package_boundary). The source-tree roots serve the spawned workers' PYTHONPATH, the resident sandbox's
read-only shared mount and the repair/metadata directories of a checkout.
"""
from pathlib import Path
import ldcon

PACKAGE_DIR = Path(ldcon.__file__).parent  # .../ldcon (site-packages or src)


def web_assets_dir(): return PACKAGE_DIR / 'web_assets'


def resources_dir(): return PACKAGE_DIR / 'resources'


def source_root():
    """`src`: the directory that makes `import ldcon` resolve (PYTHONPATH of spawned workers and tools)."""
    return PACKAGE_DIR.resolve().parent


def checkout_root():
    """`packages/ldcon`: the checkout holding src/, tests/, scripts/, doc/ and data/ (sandbox mount, repairs)."""
    return PACKAGE_DIR.resolve().parents[1]
