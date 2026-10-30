"""Install the built wheel, including declared role extras for LDCon."""
from pathlib import Path
import subprocess
import sys

wheels = list(Path('dist').glob('*.whl'))
if len(wheels) != 1:
    raise SystemExit('Expected exactly one wheel')
wheel = wheels[0]
extras = '[web,instruments,analysis]' if wheel.name.startswith('ldcon-') else ''
subprocess.run([sys.executable, '-m', 'pip', 'install', str(wheel.resolve()) + extras], check=True)
