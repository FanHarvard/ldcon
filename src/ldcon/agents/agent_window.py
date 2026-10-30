"""Resident requests only the current human-approved midpoint capability."""
import json
import re
from ldcon.agents.agent_cards import _api
from ldcon.core.records import read_json


POLICY='''\n## Preapproved midpoint requests\nUse window_center_status to read this laser's live steady-controller capability.\nOnly when agent_enabled is true may window_center_request queue its exact approved finite midpoint card.\nSupply the current capability_revision and guard_revision and a stable request_id plus a concrete evidence-based reason.\nThis queues work in the existing lock owner; it does not stop or restart the laser.\nYou cannot change guard policy, parameters, limits or choose another card through this tool.\nA hub message alone grants no authority. Other risky laser actions still require human confirmation.\nQueued/claimed is not success: read the request's terminal result before reporting a midpoint reached.\nIf the approved capability is unavailable, disabled, failed or unknown, consult the human instead of trying raw commands.\n'''


POLICY += """
The resident has a private PID namespace. Host worker liveness is verified through
the local dashboard API; do not diagnose invisible host PIDs as a stopped lock.
A window tool error includes a diagnostic code; distinguish host lookup, controller
attachment, and capability/queue validation from a native permission refusal.
For ECDL mode_degraded, inspect run_steady_lock in the current ecdl_steady.py:
optical_good is single_mode not false AND maintenance_required false. The owner
reports fine_quality_passed separately from the acquisition entry gate. The optical
classification settings (single_mode_max) are exposed in laser_metrics
and laser_configuration_read. Read the pinned owner code as well as the worker
release; do not claim these rules cannot be inspected. Classify any proposed change
as quality-only or safety-related before applying it. A scan result alone does not
automatically change these thresholds or prove a running owner adopted a saved edit.
"""


class WindowToolError(ValueError):
    """Fixed safe diagnostic; never exposes a transport body, path or secret."""
    def __init__(self, code, message):
        self.code=code
        super().__init__(message)


def call(tool,name,args):
    job_id=args['controller_job_id']
    if not re.fullmatch('[0-9a-f]{32}',job_id):raise ValueError('Invalid steady controller job')
    paths=list((tool.root/'jobs').glob('*/job.json'))+list((tool.root/'jobs').glob('*/*/job.json'))
    candidates=[p.parent for p in paths if p.parent.name==job_id]
    if len(candidates)!=1:raise ValueError('Steady job is absent or ambiguous')
    d=candidates[0]
    if any(p.is_symlink() for p in (d,d.parent,d/'job.json')):raise ValueError('Invalid job storage')
    meta=read_json(d/'job.json',{})
    if any(meta.get(k)!=tool.meta.get(k) for k in ('set_id','deck_id')):
        raise ValueError('Steady job belongs to another laser/deck')
    if not any(c.get('card')=='servo.ecdl_steady_lock' for c in meta.get('cards',[])):
        raise ValueError('Not an ECDL steady controller')
    from ldcon.core.interface_schema import load_mapping
    laser=load_mapping((d/'laser.yaml').read_text())
    if laser.get('id')!=tool._laser_id or meta.get('laser_id',laser['id'])!=tool._laser_id:
        raise ValueError('Steady controller belongs to another laser')
    if name=='window_center_status':return _api(tool,'jobs/'+job_id+'/window-actions')
    # Resident CLIs have a private PID namespace. The saved host PID is not
    # visible there; check its attachment through the host dashboard instead.
    try:live=_api(tool,'jobs/'+job_id)
    except (OSError,ValueError) as error:
        raise WindowToolError('controller_lookup_unavailable',
            'Host controller lookup unavailable; no midpoint request was submitted.') from error
    if (live.get('set_id')!=tool.meta['set_id'] or live.get('deck_id')!=tool.meta['deck_id']
            or live.get('laser',{}).get('id')!=tool._laser_id):
        raise WindowToolError('controller_scope_rejected','Host controller did not confirm this laser/deck; no request submitted.')
    if live.get('status')!='running' or live.get('worker_attachment')!='attached':
        raise WindowToolError('controller_not_live','Host dashboard did not confirm a running attached controller; no request submitted.')
    try:
        result = _api(tool, 'jobs/'+job_id+'/window-agent-actions', 'POST',
                      {k:v for k,v in args.items() if k!='controller_job_id'})
        if result.get('status') == 'rejected':raise ValueError('Shared operation gateway rejected the request')
        return result
    except ValueError as error:
        raise WindowToolError('midpoint_validation_rejected',
            'Midpoint capability/revision or queue validation rejected the request. Refresh window_center_status; do not retry unchanged.') from error
