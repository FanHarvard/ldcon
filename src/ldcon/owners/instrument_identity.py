"""Canonical physical-instrument identity and the OS-level exclusion that goes with it (rule G, 2026-10-22).

An instrument is named by what it IS, never by a hostname alias or an IP: ``<model>:<serial>`` for instruments
that report a serial (``*IDN?``, USB serial), ``esp32servocon:<resolved address>`` for the ESP32 servo boards
until their firmware exposes a MAC. ``awg-ldcon-0.localnet``, ``192.168.1.37`` and
``awg-ldcon-sr422-sr1092.localnet`` were the same SDG2042X and produced two owners; the canonical id cannot.

Two exclusion layers, both independent of the registry:
  1. ``IdentityLock``: flock on ``<lock_dir>/<sha256(canonical id)>.lock``. The directory is owned by the owner
     account and is NOT sticky, so ``O_CREAT`` is permitted under ``fs.protected_regular=1`` (the sticky-/tmp case
     that broke the 09:17 sr1092 owner start is avoided by never using /tmp).
  2. the manifest-pinned legacy endpoint lock (``migration_physical_lock.acquire_endpoint``) for the resolved
     address, so a process of the OLD design (an owner that opens ``tcp:<ip>:5025`` itself) cannot open the same
     instrument while the new owner runs, whatever name it used.
"""
import fcntl
import hashlib
import os
from pathlib import Path
import re
import stat

_CANONICAL = re.compile(r'^[a-z0-9]+(?:-[a-z0-9]+)*:[A-Za-z0-9._-]+$')


def canonical(instrument):
    """Validate and return a canonical id ``<model>:<serial-or-address>``."""
    if not isinstance(instrument, str) or not _CANONICAL.fullmatch(instrument) or len(instrument) > 120:
        raise ValueError('Instrument identity must be <model>:<serial> (e.g. sdg2042x:SDG2XFBCA00839)')
    return instrument


def unit_name(instrument):
    """systemd user unit for an instrument owner: ``ldcon-instrument-<model>-<serial>.service``."""
    model, serial = canonical(instrument).split(':', 1)
    return 'ldcon-instrument-%s-%s.service' % (model, serial.replace('.', '-'))


def state_dir_name(instrument):
    model, serial = canonical(instrument).split(':', 1)
    return '%s-%s' % (model, serial.replace('.', '-'))


# ``*IDN?`` parsing per model family: manufacturer, model, serial, firmware.
_MODELS = {
    'sdg2042x': ('SIGLENT', 'SDG2042X'),
    'sdg2122x': ('SIGLENT', 'SDG2122X'),
    'bk4054b': ('BK', '4054B'),
    'dho924': ('RIGOL', 'DHO924'),
    'cld1015': ('THORLABS', 'CLD1015'),
}


def identity_from_idn(idn):
    """``(canonical id, fields)`` from an ``*IDN?`` reply, or ValueError for an unknown model."""
    parts = [p.strip() for p in str(idn).strip().split(',')]
    if len(parts) != 4 or not all(parts[:3]):
        raise ValueError('IDN reply is not manufacturer,model,serial,firmware: %r' % idn)
    manufacturer, model, serial, firmware = parts
    for key, (vendor, name) in _MODELS.items():
        if model.upper().replace(' ', '') == name and manufacturer.upper().replace(' ', '').startswith(vendor):
            return '%s:%s' % (key, serial), dict(manufacturer=manufacturer, model=model, serial=serial, firmware=firmware)
    raise ValueError('Unknown instrument model in IDN reply: %r' % idn)


def _regular_file(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError('Identity lock must be a regular file with exactly one link')
    return info


class IdentityLock:
    """Exclusive, process-lifetime flock keyed by the canonical instrument identity.

    Never truncates, chmods or unlinks. Two opens of the same file in ONE process also conflict (flock is per open
    file description), so an accidental second owner object in the same process is refused too.
    """
    def __init__(self, instrument, lock_dir):
        self.instrument = canonical(instrument)
        directory = Path(lock_dir)
        if not directory.is_absolute() or '..' in directory.parts:
            raise ValueError('Lock directory must be absolute without traversal')
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError('Identity lock directory is missing or a symlink: %s' % directory)
        mode = directory.stat().st_mode
        if mode & stat.S_ISVTX or mode & stat.S_IWOTH:
            raise ValueError('Identity lock directory must not be sticky or world-writable (fs.protected_regular)')
        if directory.stat().st_uid != os.geteuid():
            raise ValueError('Identity lock directory must be owned by the owner account')
        self.path = directory / (hashlib.sha256(self.instrument.encode()).hexdigest() + '.lock')
        if self.path.is_symlink():
            raise ValueError('Identity lock path is a symlink')
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o660)
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                raise ValueError('Identity lock is not a regular file')
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError('Another owner holds the identity lock for %s (%s)' % (self.instrument, self.path)) from exc
            after = _regular_file(self.path)
            if (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino):
                raise RuntimeError('Identity lock inode changed while acquiring')
        except BaseException:
            os.close(fd)
            raise
        self.fd = fd
        os.write(fd, b'')   # keep the inode untouched: no content, no truncation

    @property
    def closed(self):
        return self.fd is None

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
