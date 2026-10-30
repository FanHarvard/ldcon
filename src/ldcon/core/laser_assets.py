"""Text storage scoped to one laser; saving never registers or executes a card."""
from ldcon.core.state_layout import asset_parts, workspace_parts, config_file, job_root

from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path, PurePosixPath
import stat
import uuid


MAX_BYTES = 65536
MAX_ENTRIES = 200
AREAS = ('cards', 'scripts', 'data', 'reports')
SUFFIXES = frozenset({'.py', '.yaml', '.yml', '.json', '.csv', '.tsv', '.txt',
                      '.md', '.html', '.css', '.js', '.sh', '.toml', '.ini', '.log'})
REPORT_SUFFIXES = frozenset({'.tex', '.cls', '.sty', '.bib'})
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class AssetConflict(ValueError):
    """The caller did not edit the currently stored revision."""


def relative_path(value, *, directory=False):
    if not isinstance(value, str) or len(value.encode()) > 1024:
        raise ValueError('Invalid laser file path')
    if directory and value == '':
        return PurePosixPath('.')
    path = PurePosixPath(value)
    if (not value or '\\' in value or any(ord(c) < 32 or ord(c) == 127 for c in value)
            or path.is_absolute() or str(path) != value or len(path.parts) > 16
            or any(part.startswith('.') for part in path.parts)
            or path.parts[0] not in AREAS):
        raise ValueError('Path must stay inside this laser\'s cards, scripts, data or reports')
    allowed = SUFFIXES | REPORT_SUFFIXES if path.parts[0] == 'reports' else SUFFIXES
    if not directory and (len(path.parts) < 2 or path.suffix.lower() not in allowed):
        raise ValueError('Laser file must have a supported text extension')
    return path


def validate_content(content):
    if not isinstance(content, str) or '\x00' in content or len(content.encode()) > MAX_BYTES:
        raise ValueError('Laser file content must be UTF-8 text at most 64 KiB')


def _child_directory(parent, name, create=False):
    created = False
    if create:
        try:
            os.mkdir(name, 0o2770, dir_fd=parent)
            created = True
        except FileExistsError:
            pass
    try:
        fd = os.open(name, DIRECTORY_FLAGS, dir_fd=parent)
    except OSError as exc:
        raise ValueError('Laser directories must exist and must not use symbolic links') from exc
    if created:
        os.fchmod(fd, 0o2770)
    return fd


@contextmanager
def _directory(root, parts, create=False):
    fd = os.open(root, DIRECTORY_FLAGS)
    try:
        for part in parts:
            child = _child_directory(fd, part, create)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def _root(root, laser_id):
    from ldcon.core.interface_schema import identifier
    identifier(laser_id)
    return Path(root).resolve()


def layout(root, laser_id, create=False):
    """Absolute locations; only the four content areas are writable by MCP."""
    root = _root(root, laser_id)
    base = root.joinpath(*asset_parts(root, laser_id))
    agent = root.joinpath(*workspace_parts(root, laser_id))
    if create:
        with _directory(root, asset_parts(root, laser_id), True) as fd:
            for area in AREAS:
                os.close(_child_directory(fd, area, True))
        with _directory(root, workspace_parts(root, laser_id), True):
            pass
    return dict(laser_root=str(base), **{area: str(base / area) for area in AREAS},
                agent=str(agent), instructions=str(agent / 'INSTRUCTIONS.md'),
                laser_config=str(config_file(root, 'lasers', laser_id)),
                assignment=str(config_file(root, 'assignments', laser_id)),
                job_logs=str(job_root(root, laser_id)))


def _read_at(fd, name):
    try:
        source = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                         dir_fd=fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError('Laser file must not be a symbolic link') from exc
    with os.fdopen(source, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_BYTES:
            raise ValueError('Laser file must be bounded regular text without hard links')
        content = stream.read(MAX_BYTES + 1)
    if len(content) > MAX_BYTES:
        raise ValueError('Laser file exceeds the text size limit')
    try:
        text = content.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise ValueError('Laser file must be UTF-8 text') from exc
    validate_content(text)
    return text


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest() if text is not None else ''


def prepare_write_lock(root, laser_id):
    """Host prepares only the existing shared asset lock for a narrow task mount."""
    paths=layout(root,laser_id,create=True)
    base=Path(paths['laser_root'])
    with _directory(_root(root,laser_id),asset_parts(root,laser_id)) as directory:
        fd=os.open('.assets.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW|os.O_CLOEXEC,0o660,dir_fd=directory)
        try:
            info=os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:
                raise ValueError('Invalid laser asset lock')
            os.fchmod(fd,0o660)
        finally:os.close(fd)
    return base/'.assets.lock'


class LaserAssets:
    def __init__(self, root, laser_id):
        self.root = _root(root, laser_id)
        self.laser_id = laser_id
        self.parts = asset_parts(self.root, laser_id)

    def manifest(self, create=False):
        paths = layout(self.root, self.laser_id, create)
        return dict(schema='ldcon.laser-assets.v1', laser_id=self.laser_id,
                    root=paths['laser_root'], directories={area: paths[area] for area in AREAS},
                    allowed_suffixes=sorted(SUFFIXES), max_file_bytes=MAX_BYTES,
                    file_mode='0660', new_directory_mode='2770',
                    execution='Storage only; files are not executed or registered as cards',
                    revision='Read sha256; expected_sha256 must match, or be empty for a new file')

    def list(self, path='', after=''):
        relative = relative_path(path, directory=True)
        if (not isinstance(after, str) or len(after.encode()) > 255 or '/' in after or '\\' in after
                or any(ord(c) < 32 or ord(c) == 127 for c in after)):
            raise ValueError('Invalid laser listing cursor')
        self.manifest(create=True)
        with _directory(self.root, (*self.parts, *relative.parts)) as fd:
            entries = []
            for name in sorted(os.listdir(fd)):
                if name <= after or name.startswith('.') or (not path and name not in AREAS):
                    continue
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                kind = 'directory' if stat.S_ISDIR(info.st_mode) else 'file'
                if stat.S_ISLNK(info.st_mode) or (kind == 'file' and
                        (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1)):
                    kind = 'blocked'
                entries.append(dict(path=str(relative / name), kind=kind,
                                    bytes=info.st_size if kind == 'file' else None))
                if len(entries) > MAX_ENTRIES:
                    break
        return dict(manifest=self.manifest(), path=path, entries=entries[:MAX_ENTRIES],
                    truncated=len(entries) > MAX_ENTRIES,
                    next_after=PurePosixPath(entries[MAX_ENTRIES - 1]['path']).name if len(entries) > MAX_ENTRIES else None)

    def read(self, path):
        relative = relative_path(path)
        self.manifest(create=True)
        try:
            with _directory(self.root, (*self.parts, *relative.parts[:-1])) as fd:
                text = _read_at(fd, relative.name)
        except ValueError as exc:
            # Missing nested directories mean a new file; symlink/non-directory parents do not.
            if not isinstance(exc.__cause__, FileNotFoundError):
                raise
            text = None
        return dict(path=path, exists=text is not None, content=text or '', sha256=_sha(text))

    def write(self, path, content, expected_sha256):
        relative = relative_path(path)
        validate_content(content)
        if not isinstance(expected_sha256, str) or (expected_sha256 and
                (len(expected_sha256) != 64 or any(c not in '0123456789abcdef' for c in expected_sha256))):
            raise ValueError('Expected revision must be an SHA-256 or empty for a new file')
        self.manifest(create=True)
        with _directory(self.root, self.parts) as base:
            try:
                lock_fd = os.open('.assets.lock', os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
                                  0o660, dir_fd=base)
                os.fchmod(lock_fd, 0o660)
            except FileExistsError:
                lock_fd = os.open('.assets.lock', os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=base)
            with os.fdopen(lock_fd, 'a') as lock:
                lock_info = os.fstat(lock.fileno())
                if not stat.S_ISREG(lock_info.st_mode) or lock_info.st_nlink != 1:
                    raise ValueError('Invalid laser asset lock')
                fcntl.flock(lock, fcntl.LOCK_EX)
                with _directory(self.root, (*self.parts, *relative.parts[:-1]), True) as parent:
                    current = _read_at(parent, relative.name)
                    if _sha(current) != expected_sha256:
                        raise AssetConflict('Laser file changed; read it again before editing')
                    temporary = '.asset-' + uuid.uuid4().hex
                    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                                 0o660, dir_fd=parent)
                    try:
                        with os.fdopen(fd, 'wb') as stream:
                            stream.write(content.encode())
                            stream.flush()
                            os.fchmod(stream.fileno(), 0o660)
                            os.fsync(stream.fileno())
                        os.replace(temporary, relative.name, src_dir_fd=parent, dst_dir_fd=parent)
                        os.fsync(parent)
                    finally:
                        try:
                            os.unlink(temporary, dir_fd=parent)
                        except FileNotFoundError:
                            pass
        return dict(status='stored', laser_id=self.laser_id, path=path, sha256=_sha(content),
                    artifact=str(self.root.joinpath(*self.parts, *relative.parts)),
                    executed=False, registered=False,
                    message='Saved only in this laser\'s storage; execution and card registration require their normal review')
