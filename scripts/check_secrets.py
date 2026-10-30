"""Fail on newly detected secrets without printing their values."""
import json
from pathlib import Path
import subprocess
import sys

baseline = json.loads(Path('.secrets.baseline').read_text())
allowed = {(p, item['type'], item['hashed_secret']) for p, items in baseline['results'].items()
           for item in items if item.get('is_secret') is False}
result = subprocess.run([sys.executable, '-m', 'detect_secrets', 'scan', '--no-verify', '--exclude-files', r'^\.secrets\.baseline$'],
                        check=True, capture_output=True, text=True)
current = json.loads(result.stdout)
findings = [(p, item['line_number'], item['type']) for p, items in current['results'].items()
            for item in items if (p, item['type'], item['hashed_secret']) not in allowed]
for path, line, kind in findings:
    print(f'{path}:{line}: {kind} (value redacted)')
if findings:
    raise SystemExit('Unreviewed potential credential detected')
print('No unreviewed credential findings')
