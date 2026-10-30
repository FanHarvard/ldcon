"""Archive only finished, quiescent job directories; verify before removal."""
from ldcon.core.state_layout import config_root

from datetime import datetime, timezone
import hashlib
import html
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tarfile
import tempfile
import time

from ldcon.web.web_runtime import TERMINAL, atomic_json, read_json, safe_id


def report_html(job, recorded=None):
    from ldcon.web.job_report import render
    return render(job, recorded)


def _local_settings(runtime):
    path = runtime.root/'log-retention.json'
    if not path.exists():
        return None
    raw = path.read_bytes()
    if len(raw) > 16384:
        raise ValueError('Local retention settings are too large')
    # Corrupt local overrides must not silently enable a shared archive policy.
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('Invalid local retention settings')
    return value


def settings(runtime):
    from ldcon.web.operations_settings import BUILTIN, read, shared, validate_retention
    local = _local_settings(runtime)
    if local is not None and local != {'inherit_defaults': True}:
        return validate_retention(local, (runtime.root,))
    path = shared(runtime)
    value = read(path)['retention'] if path else BUILTIN
    return validate_retention(value, (runtime.root,))


def settings_info(runtime):
    from ldcon.web.operations_settings import shared
    local = _local_settings(runtime)
    return dict(settings(runtime), use_defaults=local is None or local == {'inherit_defaults': True},
                defaults_available=shared(runtime) is not None,
                source='override' if local is not None and local != {'inherit_defaults': True} else 'shared' if shared(runtime) else 'built_in')


def configure(runtime, value):
    from ldcon.web.operations_settings import shared, read, validate_retention
    if value == {'use_defaults': True}:
        if shared(runtime) is None:
            raise ValueError('Shared operational defaults are not configured for this service')
        validate_retention(read(shared(runtime))['retention'], (runtime.root,))
        atomic_json(runtime.root/'log-retention.json', {'inherit_defaults': True})
    else:
        result = validate_retention(value, (runtime.root,))
        atomic_json(runtime.root/'log-retention.json', result)
    return settings(runtime)


def archived(runtime):
    return [read_json(p,{}) for p in (runtime.root/'archive-index').glob('*.json')]


def recovery_evidence(runtime, jobs):
    """Keep unresolved recovery and waiting reservation evidence live.

    Observe frozen failure routing before deletion; the asynchronous notifier
    may not have scanned a newly finished job yet. Legacy jobs without routing
    remain unenrolled. Observation only persists local queues and cases.
    """
    from ldcon.agents.failure_policy import observe_job, list_cases
    for job in jobs:
        if job.get('status') in TERMINAL and (job.get('failure_policy_version') == 1
                or job.get('agent_fallbacks') or job.get('teams_fallbacks')):
            observe_job(runtime.root, runtime.directory(job['id']))
    protected = set()
    if hasattr(runtime, 'reservations'):
        for value in runtime.reservations.list():
            if value['status'] in ('pending', 'waiting_agent', 'dispatching', 'review_required'):
                protected.update(p['job_id'] for p in value['prerequisites'])
                protected.add(value['output_job_id'])
    for case in list_cases(runtime.root):
        if case.get('blocks_downstream') or case.get('disposition') not in ('resolved', 'no_agent') or case['job_id'] in protected:
            protected.add(case['job_id'])
            if case.get('replacement_job_id'):
                protected.add(case['replacement_job_id'])
    # A newly observed no-agent failure may have just queued its diagnostic.
    # The notifier needs the original job until delivery is settled.
    outbox=runtime.root/'notifications/notifications.sqlite3'
    if outbox.exists():
        with sqlite3.connect(outbox.resolve().as_uri()+'?mode=ro',uri=True) as db:
            protected.update(row[0] for row in db.execute(
                "SELECT job FROM deliveries WHERE status IN ('pending','sending','unknown')"))
    return protected


def archive_finished(runtime, now=None):
    now=time.time() if now is None else now
    result={'archived':[],'errors':[], 'checked_utc':datetime.now(timezone.utc).isoformat()}
    with runtime.mutex:
        config=settings(runtime)
        if not config['enabled']:return result
        destination=Path(config['backup_directory'])
        if not destination.is_dir():raise ValueError('Backup directory is unavailable; logs retained')
        cutoff=now-config['retention_days']*86400
        index=runtime.root/'archive-index';index.mkdir(exist_ok=True)
        jobs=runtime.list_jobs()
        try:
            protected=recovery_evidence(runtime,jobs)
        except Exception as error:
            result['errors'].append({'job':'failure-evidence','error':'Recovery evidence could not be checked; logs retained ('+type(error).__name__+')'})
            atomic_json(runtime.root/'archive-status.json',result)
            return result
        for job in jobs:
            if job['id'] in protected:continue
            process=runtime.processes.get(job['id'])
            if job['status'] not in TERMINAL or process and process.poll() is None:continue
            directory=runtime.directory(job['id'])
            # Keep artifacts referenced by current laser configuration as live evidence.
            if any(str(directory) in p.read_text() for p in (config_root(runtime.root)/'lasers').glob('*.yaml')):continue
            paths=list(directory.rglob('*'))
            if any(p.is_symlink() for p in paths):continue
            files=[p for p in paths if p.is_file()]
            if not files or max(p.stat().st_mtime for p in [directory,*paths])>=cutoff:continue
            temporary=None
            try:
                laser=safe_id(job.get('laser',{}).get('id') or job.get('set_id') or 'unassigned')
                target=destination/laser
                if target.is_symlink():raise ValueError('Backup laser directory cannot be a symlink')
                target.mkdir(exist_ok=True)
                archive=target/(safe_id(job['id'])+'.tar.gz')
                # Never overwrite an existing backup, including one from another installation.
                if archive.exists():
                    prior=read_json(index/(job['id']+'.json'),{})
                    if (prior.get('archive_path')==str(archive) and prior.get('archive_sha256')==_hash(archive)
                            and prior.get('source_hashes')=={str(p.relative_to(directory)):_hash(p) for p in files}):
                        shutil.rmtree(directory);result['archived'].append(job['id'])
                        continue
                    raise ValueError('Backup already exists without a matching committed index')
                descriptor,name=tempfile.mkstemp(prefix='.ldcon-',suffix='.tmp',dir=target)
                os.close(descriptor);temporary=Path(name)
                hashes={str(p.relative_to(directory)):_hash(p) for p in files}
                with tarfile.open(temporary,'w:gz') as tar:
                    for p in files:tar.add(p,arcname=str(p.relative_to(directory)),recursive=False)
                with tarfile.open(temporary,'r:gz') as tar:
                    for member in tar.getmembers():
                        with tar.extractfile(member) as stream:
                            if _stream_hash(stream)!=hashes[member.name]:
                                raise OSError('Archive verification failed')
                if any(_hash(directory/name)!=checksum for name,checksum in hashes.items()):
                    raise OSError('Job files changed during archival')
                with temporary.open('rb') as stream:os.fsync(stream.fileno())
                os.link(temporary,archive);temporary.unlink();temporary=None
                from ldcon.web.job_report import evidence
                entry={**job,'archived':True,'archive_path':str(archive),'archive_sha256':_hash(archive),'source_hashes':hashes,
                       'report_evidence':evidence(directory)}
                report=index/(job['id']+'.html');report.write_text(report_html(entry),encoding='utf-8')
                atomic_json(index/(job['id']+'.json'),entry)
                for path in (report,index/(job['id']+'.json')):
                    with path.open('rb') as stream:os.fsync(stream.fileno())
                for path in (target,index):
                    fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY)
                    try:os.fsync(fd)
                    finally:os.close(fd)
                shutil.rmtree(directory)
                result['archived'].append(job['id'])
            except (OSError,ValueError,tarfile.TarError) as error:
                result['errors'].append({'job':job['id'],'error':str(error)})
            finally:
                if temporary is not None:temporary.unlink(missing_ok=True)
        atomic_json(runtime.root/'archive-status.json',result)
    return result


def _stream_hash(stream):
    digest=hashlib.sha256()
    for block in iter(lambda:stream.read(1024*1024),b''):digest.update(block)
    return digest.hexdigest()


def _hash(path):
    with path.open('rb') as stream:return _stream_hash(stream)
