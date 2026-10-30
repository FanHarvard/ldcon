"""Read-only historical observation helper extracted for regression tests."""
import hashlib
import json
import time
from pathlib import Path
ADAPTIVE = Path("/nonexistent-test-root")

def experiment(laser, job_id):
    directory = ADAPTIVE/laser/job_id
    selector = directory/'adaptive-experiment.json'
    if not selector.exists():
        return dict(state='not_requested', active=False, observed_unix_s=time.time())
    selected = json.loads(selector.read_text())
    path = directory/'adaptive-experiments'/selected['id']/'adaptive-window'/'status.json'
    if not path.exists():
        return dict(state='pending', active=False, selection=selected['id'], observed_unix_s=time.time())
    status = json.loads(path.read_text())
    epoch = status.get('epoch') or {}
    return dict(state=status['state'], active=status.get('active') is True,
                selection=selected['id'], returning=status.get('returning'),
                candidate_value=status.get('target'),
                epoch_state=epoch.get('state'),
                epoch_id=hashlib.sha256(epoch['token'].encode()).hexdigest()[:16] if epoch else None,
                checkpoint={k:v for k,v in epoch.get('checkpoint',{}).items()
                            if k in ('current','temperature','pid_kp','pid_ki','pid_kd','pid_integral_min','pid_integral_max')},
                published_unix_s=path.stat().st_mtime, observed_unix_s=time.time())

