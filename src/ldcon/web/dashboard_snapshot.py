"""Bounded read-only display models from worker publications.

No WebRuntime, SQLite, owner calls, output requests, or lifecycle writes. Roots
come only from the administrator's service registry. These observations never
admit a control action; the existing upstream API and owner retain authority.
"""
from ldcon.core.state_layout import config_root

import json
import os
from pathlib import Path
import re
import stat
import threading
import time

from ldcon.jobs.job_service import identity

ID=re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}')
TERMINAL=frozenset(('succeeded','completed','completed_with_failures','completed_unsettled','failed','cancelled','interrupted','inconclusive'))
META=('id','set_id','set_name','deck_id','label','created_utc','planned_s','mode','read_only','engine')
PROGRESS=('status','simulated_s','active_card','controller_state','mode_transitions','updated_utc','message','shot_sweep','lock_target')
_cache={}
_mutex=threading.Lock()


def parse_yaml(raw):
    """Stable parser identity for cached display-only YAML publications."""
    import yaml
    from ldcon.core.deck_yaml import DeckLoader
    try:return yaml.load(raw,Loader=DeckLoader)
    except yaml.YAMLError as error:raise ValueError('Invalid laser YAML') from error


def parse_frozen_yaml(raw):
    import yaml
    try:return yaml.safe_load(raw)
    except yaml.YAMLError as error:raise ValueError('Invalid frozen laser') from error


def read(root,path,default=None,limit=2*1024*1024,parser=json.loads):
    """Cache only unchanged files, bounded and non-symlink; never refresh time."""
    root=Path(root);path=Path(path)
    try:
        relative=path.relative_to(root)
        if '..' in relative.parts or root.resolve()!=root:raise ValueError('Invalid display root')
        if path.parent.resolve()!=path.parent:raise ValueError('Symlink in display path')
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(fd,'rb') as stream:
            info=os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size>limit:raise ValueError('Display file is not bounded regular JSON')
            stamp=(info.st_dev,info.st_ino,info.st_mtime_ns,info.st_size)
            # A file may be read as JSON, bytes or a header. Never reuse a
            # different parser's result (PDF listing and delivery share paths).
            key=(str(path),parser)
            with _mutex:cached=_cache.get(key)
            if cached and cached[0]==stamp:return cached[1]
            value=parser(stream.read(limit+1))
        with _mutex:
            if len(_cache)>10000:_cache.clear()
            _cache[key]=(stamp,value)
        return value
    except (OSError,ValueError,UnicodeError):return default


def scalars(value,depth=0):
    """Keep scalar telemetry and small nested mappings; arrays stay in reports."""
    if value is None or type(value) in (str,int,float,bool):return value
    if isinstance(value,dict) and depth<4:
        return {k:scalars(v,depth+1) for k,v in value.items() if isinstance(v,dict) or v is None or type(v) in (str,int,float,bool)}
    return None


def root_for(service):
    root=service.state_file.parent
    if not root.is_absolute() or root.resolve()!=root or not (root/'jobs').is_dir() or not os.access(root/'jobs',os.R_OK|os.X_OK):raise ValueError('Invalid registered display root')
    return root


def jobs(service,include_archived=False):
    root=root_for(service);values={}
    if include_archived:
        for path in (root/'archive-index').glob('*.json'):
            row=read(root,path,{})
            if isinstance(row,dict) and isinstance(row.get('id'),str) and ID.fullmatch(row['id']):
                values[row['id']]={k:row.get(k) for k in (*META,'status','archived')}
    paths=list((root/'jobs').glob('*/job.json'))+list((root/'jobs').glob('*/*/job.json'))
    for path in paths:
        directory=path.parent;meta=read(root,path,{})
        if not isinstance(meta,dict) or meta.get('id')!=directory.name or not ID.fullmatch(directory.name):
            raise ValueError('Invalid job publication; cannot establish the active slot')
        progress=read(root,directory/'progress.json',{})
        if not isinstance(progress,dict):progress={}
        row={k:meta[k] for k in META if k in meta}
        row.update({k:scalars(progress[k]) for k in PROGRESS if k in progress})
        row.setdefault('status','queued')
        row['cards']=[{k:c[k] for k in ('id','card','operation') if k in c} for c in meta.get('cards',[]) if isinstance(c,dict)]
        saved=read(root,directory/'worker-process.json',{})
        alive=bool(isinstance(saved,dict) and type(saved.get('pid')) is int and identity(saved['pid'])==saved)
        if row['status'] in TERMINAL:
            if alive:
                row['status']='finishing'
                row['worker_attachment']='attached'
        else:
            row['worker_attachment']='attached' if alive else 'pending_or_unverified'
            if (directory/'cancel.request').is_file():row['status']='stopping'
        if row['status'] not in TERMINAL:
            row['latest']=scalars(read(root,directory/'readings.json',{})) if alive else {}
            monitor=read(root,directory/'agent-monitor-status.json',{}) if alive else {}
            row['monitor_activity']={k:monitor.get(k) for k in ('state','enabled','updated_unix_s')} if isinstance(monitor,dict) else {}
            if alive and any(c.get('card',c.get('operation'))=='servo.ecdl_steady_lock' for c in row['cards']):
                from ldcon.web.dither_quality import policy
                frozen=read(root,directory/'laser.yaml',{},limit=256*1024,parser=parse_frozen_yaml)
                row['dither_quality_policy']=policy(frozen) if isinstance(frozen,dict) else None
            if not alive:
                row['controller_state']='unknown'
            for name,filename in [('online_tuning','online-status.json'),('live_controller','controller-live.json'),('window_controller','window-controller.json')]:
                row[name]=read(root,directory/filename,{})
            row['online_supported']=meta.get('engine')=='interface-cards' and any(c.get('card',c.get('operation')) in ('servo.ecdl_steady_lock','servo.native_current','servo.current_pid','servo.slow_pid') for c in row['cards'])
        values[row['id']]=row
    return sorted(values.values(),key=lambda r:r.get('created_utc',''),reverse=True)


def snapshot(service):
    rows=jobs(service)
    from ldcon.web.dashboard_wavelength import views
    return {'wavelengths':views(root_for(service)), 'jobs':[r for r in rows if r['status'] not in TERMINAL],
            'exports':[r for r in rows if any(c.get('card',c.get('operation'))=='report.grafana_dashboard' for c in r.get('cards',[]))][:30],
            'read_at_unix_s':time.time(),'source':'worker_publications'}


def history(service,q='',limit=25,offset=0,set_id=None):
    if limit not in (10,25,50,100) or offset<0:raise ValueError('Invalid page size or offset')
    rows=[r for r in jobs(service,include_archived=True) if (set_id is None or r.get('set_id')==set_id) and q.casefold() in ' '.join(str(r.get(k,'')) for k in ('id','set_name','set_id','deck_id','status','created_utc')).casefold()]
    return {'total':len(rows),'jobs':rows[offset:offset+limit]}


def inventory(service):
    """Saved targets only; no query to a control API or inferred lock reference."""
    root=root_for(service);rows=[]
    for path in (config_root(root)/'lasers').glob('*.yaml'):
        if not ID.fullmatch(path.stem):continue
        value=read(root,path,{},limit=256*1024,parser=parse_yaml)
        if not isinstance(value,dict) or value.get('id')!=path.stem:continue
        rows.append({'id':path.stem,'name':value.get('name',path.stem),'target':value.get('target',{})})
    return rows


def study_jobs(service, set_id=None, operation=None):
    """Read completed-result indexes without taking the control API's mutex."""
    root=root_for(service)
    rows=jobs(service,True)
    for row in rows:
        if set_id and row.get('set_id')!=set_id:continue
        if operation and not any(c.get('operation',c.get('card'))==operation for c in row.get('cards',[])):continue
        directory=root/'jobs'/row['id']
        row['available_artifacts']=[name for name in ('target-response.json','measurement.json','window-center.json','mode-map.json','recovery.json','recovery-plan.json','recovery-test.json')
            if read(root,directory/name) is not None]
        row['pdf_available']=any(isinstance(payload,bytes) and payload.startswith(b'%PDF-')
            for payload in (read(root,path,limit=16*1024*1024,parser=bytes)
            for path in (directory/'report.pdf',root/'reports'/row['id']/'build/report.pdf'))
        )
        yield row
