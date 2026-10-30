"""Audit source/wheel contents and strongly identifiable credential formats."""
from pathlib import Path, PurePosixPath
import re
import sys
import tarfile
import zipfile

pattern = re.compile(rb'(?:gh[pousr]_[A-Za-z0-9_]{30,}|github_pat_[A-Za-z0-9_]{30,}|pypi-[A-Za-z0-9_-]{30,}|AKIA[A-Z0-9]{16}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)')
for artifact in sorted(Path(sys.argv[1]).iterdir()):
    if artifact.suffix == '.whl':
        with zipfile.ZipFile(artifact) as archive:
            items = [(n, archive.read(n)) for n in archive.namelist() if not n.endswith('/')]
    elif artifact.name.endswith('.tar.gz'):
        with tarfile.open(artifact) as archive:
            items = [(m.name.partition('/')[2], archive.extractfile(m).read()) for m in archive if m.isfile()]
    else:
        raise SystemExit('Unexpected distribution file: ' + artifact.name)
    for name, data in items:
        path = PurePosixPath(name)
        if path.is_absolute() or '..' in path.parts:
            raise SystemExit('Invalid archive path: ' + name)
        if any(p in {'.git', '.venv', '__pycache__', 'node_modules', '.env', '.pypirc'} for p in path.parts) or path.suffix in {'.pem', '.key', '.sqlite3', '.jsonl', '.pyc'}:
            raise SystemExit('Private/generated file in artifact: ' + name)
        if artifact.suffix == '.whl' and any(p in {'tests', 'support'} for p in path.parts):
            raise SystemExit('Test data in wheel: ' + name)
        if pattern.search(data):
            raise SystemExit('Potential credential in artifact (redacted): ' + name)
    print(artifact.name, len(items), 'files audited')
