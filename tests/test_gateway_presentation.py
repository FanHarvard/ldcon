"""Browser checks against synthetic data only."""
import os
from pathlib import Path
import shutil
import subprocess
import pytest


def test_gateway_display_keeps_routing_and_raw_evidence():
    if not shutil.which('node') or subprocess.run(
        ['node', '-e', "require('jsdom')"], capture_output=True
    ).returncode:
        pytest.skip('Browser checks require Node.js and jsdom')
    test = Path(__file__).with_suffix('.cjs')
    subprocess.run(['node', str(test)], check=True, env=os.environ.copy(), timeout=30)
