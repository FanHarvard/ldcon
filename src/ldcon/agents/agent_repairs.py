"""Bounded, reviewable repair artifacts. Never write installed code or device state."""
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
from ldcon.core.paths import checkout_root
import tempfile

from ldcon.agents.agent_workspace import directory

MAX_BYTES=65536
SOURCE_ROOT=checkout_root()
ALLOWED_ROOTS={'src','tests','scripts','doc'}
ALLOWED_SUFFIXES={'.py','.js','.html','.css','.md','.sh','.json','.txt'}


def digest(text):return hashlib.sha256(text.encode()).hexdigest()


def relative_path(value):
    path=PurePosixPath(value)
    if (not isinstance(value,str) or '\\' in value or '\x00' in value or path.is_absolute()
            or value!=str(path) or len(path.parts)<2 or any(part in ('.','..') for part in path.parts)
            or path.parts[0] not in ALLOWED_ROOTS or path.suffix not in ALLOWED_SUFFIXES):
        raise ValueError('Repair path must name a source or test text file')
    return path


def safe_file(base,relative,create=False):
    current=Path(base)
    if current.is_symlink():raise ValueError('Repair roots must not use symbolic links')
    for part in relative.parts[:-1]:
        current=current/part
        if current.is_symlink():raise ValueError('Repair paths must not use symbolic links')
        if create:current.mkdir(exist_ok=True)
        if current.exists() and not current.is_dir():raise ValueError('Repair parent is not a directory')
    result=current/relative.name
    if result.is_symlink():raise ValueError('Repair files must not use symbolic links')
    return result


def read_text(path):
    if not path.exists():return None
    if not path.is_file() or path.stat().st_size>MAX_BYTES:raise ValueError('Repair file is not bounded text')
    text=path.read_text()
    if '\x00' in text:raise ValueError('Repair file is not text')
    return text


def write_text(path,text):
    fd,name=tempfile.mkstemp(prefix='.repair-',dir=path.parent)
    try:
        with os.fdopen(fd,'w') as stream:
            stream.write(text);stream.flush();os.fsync(stream.fileno());os.fchmod(stream.fileno(),0o660)
        os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)


class RepairWorkspace:
    def __init__(self,root,laser_id,case_id,source_root=None):
        self.source=Path(source_root or SOURCE_ROOT).resolve()
        self.folder=directory(root,laser_id,True)/'repairs'
        if self.folder.is_symlink():raise ValueError('Repair directory must not be a symbolic link')
        self.folder.mkdir(exist_ok=True)
        self.folder=self.folder/hashlib.sha256(case_id.encode()).hexdigest()
        if self.folder.is_symlink():raise ValueError('Repair directory must not be a symbolic link')
        self.folder.mkdir(exist_ok=True);self.case_id=case_id
    def manifest(self):
        path=safe_file(self.folder,PurePosixPath('manifest.json'))
        text=read_text(path)
        result=json.loads(text) if text else dict(case_id=self.case_id,source_root=str(self.source),files={})
        if result['case_id']!=self.case_id or result['source_root']!=str(self.source):raise ValueError('Repair context changed')
        return result
    def read(self,name):
        path=relative_path(name);manifest=self.manifest()
        source=read_text(safe_file(self.source,path));entry=manifest['files'].get(name)
        source_sha=digest(source) if source is not None else ''
        if entry:
            if entry['source_sha256']!=source_sha:raise ValueError('Source changed after repair began; review drift before continuing')
            content=read_text(safe_file(self.folder,PurePosixPath('proposed')/path))
            if content is None or digest(content)!=entry['proposed_sha256']:raise ValueError('Repair artifact differs from manifest')
        else:content=source
        return dict(path=name,content=content or '',sha256=digest(content) if content is not None else '',
                    exists=content is not None,source_sha256=source_sha,staged=bool(entry))
    def patch(self,name,content,expected_sha256):
        path=relative_path(name)
        if not isinstance(content,str) or len(content.encode())>MAX_BYTES or '\x00' in content:
            raise ValueError('Repair content must be text at most 64 KiB')
        fd=os.open(self.folder/'.repair.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o660)
        with os.fdopen(fd,'a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            current=self.read(name)
            if current['sha256']!=expected_sha256:raise ValueError('Repair file changed; read again before editing')
            manifest=self.manifest();entry=manifest['files'].get(name)
            if not entry:
                if current['exists']:write_text(safe_file(self.folder,PurePosixPath('original')/path,True),current['content'])
                entry=dict(source_sha256=current['source_sha256'],source_exists=current['exists'])
            destination=safe_file(self.folder,PurePosixPath('proposed')/path,True)
            write_text(destination,content)
            manifest['files'][name]={**entry,'proposed_sha256':digest(content)}
            write_text(safe_file(self.folder,PurePosixPath('manifest.json')),json.dumps(manifest,ensure_ascii=False,indent=2))
        return dict(status='staged',case_id=self.case_id,path=name,sha256=digest(content),artifact=str(destination),
                    manifest=str(self.folder/'manifest.json'),message='Repair artifact only; installed code and device configuration unchanged; testing and deployment not verified')
