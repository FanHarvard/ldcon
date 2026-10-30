"""Small admission index; historical reports/telemetry are never loaded here.

Discover job names once per refresh, then cache immutable metadata and terminal
state by file identity. Replacements invalidate the cache immediately. No TTL
may hide a newly queued job or a worker still releasing its resources.
"""
from pathlib import Path


def signature(path):
    try:
        st = path.stat()
        return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns
    except FileNotFoundError:
        return None


class JobRegistry:
    def __init__(self, runtime):
        self.runtime = runtime
        self.entries = {}

    def header(self, directory):
        from ldcon.jobs.job_service import JobProcess
        from ldcon.web.web_runtime import read_json, TERMINAL, ConflictError
        directory = Path(directory)
        key = directory.name
        signs = tuple(signature(directory / name) for name in ('job.json', 'progress.json', 'worker-process.json'))
        old = self.entries.get(key)
        if old is not None and old['directory'] != directory:
            raise ConflictError('Duplicate job identity; inspect before control operations')
        if old is None or old['signs'] != signs:
            meta = read_json(directory / 'job.json', None)
            if not isinstance(meta, dict) or meta.get('id') != key:
                raise ConflictError('Job metadata is unavailable or inconsistent; inspect before control operations')
            state = read_json(directory / 'progress.json', {})
            if not isinstance(state, dict):
                state = {}
            old = dict(directory=directory, signs=signs, meta=meta, state=state, terminal_confirmed=False)
            self.entries[key] = old
        # Completed metadata is reusable only after its worker has exited. A changed
        # process identity/state invalidates this proof; PID numbers alone are not used.
        if not old['terminal_confirmed']:
            process = self.runtime.processes.get(key)
            if signs[2] is not None:
                identity = read_json(directory / 'worker-process.json', None)
                if (not isinstance(identity, dict) or type(identity.get('pid')) is not int
                        or not isinstance(identity.get('boot_id'), str) or not isinstance(identity.get('start_ticks'), str)):
                    raise ConflictError('Worker identity is unverified; inspect before control operations')
            if process is None:
                process = self.runtime.processes[key] = JobProcess(directory)
            if old['state'].get('status') in TERMINAL and process.poll() is not None:
                old['terminal_confirmed'] = True
        return {**old['meta'], **old['state'], 'status': old['state'].get('status', 'unknown')}, old['terminal_confirmed']

    def headers(self):
        paths = self.runtime.job_metadata()
        seen = set()
        result = []
        for p in paths:
            if p.parent.name in seen:
                from ldcon.web.web_runtime import ConflictError
                raise ConflictError('Duplicate job identity; inspect before control operations')
            seen.add(p.parent.name)
            row, terminal = self.header(p.parent)
            result.append((row, terminal))
        self.entries = {k: v for k, v in self.entries.items() if k in seen}
        return result
