"""Refuse a release from a branch or a tag for another version."""
from email.parser import BytesParser
import os
from pathlib import Path
import zipfile

wheel, = Path('dist').glob('*.whl')
with zipfile.ZipFile(wheel) as archive:
    metadata, = [n for n in archive.namelist() if n.endswith('.dist-info/METADATA')]
    version = BytesParser().parsebytes(archive.read(metadata))['Version']
if os.environ.get('GITHUB_REF') != 'refs/tags/v' + version:
    raise SystemExit('Run this release at tag v' + version)
