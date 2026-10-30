"""Independent job launcher. The dashboard is never the supervisor in deployment."""
import argparse
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
from ldcon.core.paths import source_root
import signal
import subprocess
import sys
import time


def identity(pid=None):
    """Linux boot ID + start ticks prevents mistaking a reused PID for a worker."""
    pid = os.getpid() if pid is None else pid
    try:
        stat = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        if stat[0] in ('Z', 'X'):
            return None
        return dict(pid=pid, boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                    start_ticks=stat[19])
    except (OSError, IndexError):
        return None


class JobProcess:
    """Attach without signalling a PID. Unknown ownership stays blocked for review."""
    def __init__(self, directory):
        self.directory = Path(directory)

    def poll(self):
        from ldcon.web.web_runtime import read_json, TERMINAL
        if not (self.directory/'job.json').is_file():
            return 0  # A completed job may have been archived since attachment.
        saved = read_json(self.directory/'worker-process.json')
        if saved:
            return None if identity(saved['pid']) == saved else -1
        state = read_json(self.directory/'progress.json', {})
        if state.get('status') in TERMINAL:
            return 0
        # Queued and old-format jobs cannot be declared dead based on Web memory.
        return None


@contextmanager
def worker_lifetime(directory):
    """One process per immutable job, including concurrent/replayed launch attempts."""
    from ldcon.core.records import atomic_json
    directory = Path(directory)
    with (directory/'worker.lock').open('a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Never run a job a second time, even if it previously crashed.
        if (directory/'worker-process.json').exists():
            raise RuntimeError('Job was already executed; inspect its outcome instead of replaying')
        value = identity()
        if value is None:
            raise RuntimeError('Cannot establish Linux worker process identity')
        atomic_json(directory/'worker-process.json', value)
        yield


def launch(directory):
    directory = Path(directory)
    environment = os.environ.copy()
    environment['PYTHONPATH'] = str(source_root())+os.pathsep+environment.get('PYTHONPATH', '')
    with (directory/'worker.log').open('ab') as stream:
        return subprocess.Popen([sys.executable, '-m', 'ldcon.interface_worker', str(directory)],
            stdin=subprocess.DEVNULL, stdout=stream, stderr=stream, env=environment,
            start_new_session=True, close_fds=True)


def dispatch_pending(root):
    """Consume durable launch requests once; no resubmission after uncertain spawn."""
    from ldcon.core.records import read_json, atomic_json
    children = []
    for request in sorted([*(Path(root)/'jobs').glob('*/launch.request'), *(Path(root)/'jobs').glob('*/*/launch.request')]):
        directory = request.parent
        if directory.is_symlink() or directory.parent.is_symlink() or request.is_symlink():
            continue
        meta = read_json(directory/'job.json', {})
        if meta.get('worker_backend') != 'service':
            continue
        from ldcon.core.state_layout import single_laser
        selected = single_laser(root)
        if selected and meta.get('laser', {}).get('id', meta.get('laser_id', meta.get('set_id'))) != selected:
            raise ValueError('Job does not belong to this laser root')
        try:
            descriptor = os.open(directory/'launch.claim', os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o660)
        except FileExistsError:
            continue
        os.close(descriptor)
        if (directory/'cancel.request').exists():
            atomic_json(directory/'progress.json', dict(status='cancelled', message='Cancelled before worker launch'))
            continue
        try:
            children.append(launch(directory))
        except OSError as error:
            atomic_json(directory/'progress.json', dict(status='failed', message='Worker could not start: '+str(error)))
    return children


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, required=True)
    args = parser.parse_args()
    args.state_dir.mkdir(parents=True, exist_ok=True)
    stopping = False
    def stop(*unused):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    with (args.state_dir/'job-service.lock').open('a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        children = []
        while not stopping:
            children.extend(dispatch_pending(args.state_dir))
            children = [p for p in children if p.poll() is None]
            time.sleep(.2)
        # Worker lifecycle is independent; no implicit cancel or signal here.


if __name__ == '__main__':
    main()
