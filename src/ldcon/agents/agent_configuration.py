"""Same-laser revision-checked configuration access via the dashboard API.

Authority classes come from ``agent_authority``: free fields save directly,
safety ranges need a direct human approval id, maintainer-only fields are
refused here and by the dashboard. The dashboard (outside the sandbox) is the
trusted path: it re-classifies, enforces ceilings, verifies the approval
against the host prompt ledger, writes the audit record and the file.
"""
import copy
import json
import math

import yaml

from ldcon.agents.agent_cards import _api
from ldcon.core.interface_schema import load_mapping

from ldcon.agents.agent_configuration_policy import POLICY
from ldcon.core.agent_authority import (FREE, SAFETY, FORBIDDEN, describe_changes, partition, changes as diff_paths)

QUALITY_MAPS = frozenset(('single_mode_max',))
QUALITY_METRICS = frozenset(('side_peak_height_max', 'side_peak_prominence_max',
                             'near_main_peak_prominence_max', 'main_fwhm_fraction'))
# The lock device's dither quality limit (monitor dither_response condition,
# dashboard quality line). Revisable only through the adjustment QualityReview
# (host-applied after verified adoption); window.edge_response is a separate
# window-search experiment boundary and is never included.
DITHER_QUALITY_LEAF = ('control_settings', 'steady_lock', 'window', 'warning_response')
KINDS = ('lasers', 'assignments')


def dither_quality_change(path, before, after):
    """The existing positive dither quality limit leaf, nothing else."""
    if len(path) != 6 or path[0] != 'devices' or tuple(path[2:]) != DITHER_QUALITY_LEAF:
        return False
    for document in (before, after):
        try:
            for key in path:document = document[key]
        except (KeyError, TypeError):return False
        if type(document) not in (int, float) or not math.isfinite(document) or document <= 0:
            return False
    return True


def reviewed_quality_change(path, before, after):
    """Paths the adjustment QualityReview may apply: optical classification leaves and the dither limit."""
    return quality_change(path, before, after) or dither_quality_change(path, before, after)


def quality_change(path, before, after):
    """Only existing scalar classification leaves; never entry/safety guards."""
    if (len(path) != 6 or path[0] != 'devices' or
            path[2:4] != ('control_settings', 'optical') or
            path[4] not in QUALITY_MAPS or path[5] not in QUALITY_METRICS):
        return False
    for document in (before, after):
        try:
            for key in path:document = document[key]
        except (KeyError, TypeError):return False
        if type(document) not in (int, float) or not math.isfinite(document) or document < 0:
            return False
    return True


def changes(before, after, path=()):
    return diff_paths(before, after, path)


def _name(box, kind):
    if kind == 'lasers':return box._laser_id
    if kind == 'assignments':return box.meta['set_id']
    raise ValueError('Agent settings cover lasers and assignments only')


def _patch(document, dotted, value):
    parts = dotted.split('.')
    parent = document
    for part in parts[:-1]:
        if isinstance(parent, list):
            parent = parent[int(part)]
        else:
            if part not in parent or not isinstance(parent[part], (dict, list)):
                parent[part] = {}
            parent = parent[part]
    if isinstance(parent, list):
        parent[int(parts[-1])] = value
    else:
        parent[parts[-1]] = value


def _candidate(before, args):
    """Full YAML or a dotted-path patch, never both."""
    if args.get('changes_json') and args.get('yaml'):
        raise ValueError('Supply either complete yaml or changes_json')
    if args.get('changes_json'):
        patch = json.loads(args['changes_json'], parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Finite values required')))
        if not isinstance(patch, dict) or not patch or len(patch) > 64:
            raise ValueError('Expected 1–64 dotted-path changes')
        candidate = copy.deepcopy(before)
        for dotted, value in patch.items():
            if not isinstance(dotted, str) or not dotted:raise ValueError('Invalid change path')
            json.dumps(value, allow_nan=False)
            _patch(candidate, dotted, value)
        return candidate, yaml.safe_dump(candidate, sort_keys=False, allow_unicode=True)
    text = args.get('yaml', '')
    return load_mapping(text), text


def classified(kind, before, candidate):
    rows = describe_changes(kind, before, candidate)
    parts = partition(rows)
    return rows, parts


def proposal_rows(rows):
    """Exact change records the human must approve (safety class only)."""
    return [dict(path=r['path'], old=r['old'], new=r['new']) for r in rows if r['class'] == SAFETY]


def call(box, name, args):
    if name == 'laser_configuration_read':
        return _api(box, 'configuration/lasers/' + box._laser_id)
    if name == 'laser_settings_read':
        kind = args.get('kind', 'lasers')
        current = _api(box, 'configuration/' + kind + '/' + _name(box, kind))
        return current
    if name == 'laser_configuration_update':
        args = dict(args, kind='lasers');name = 'laser_settings_update'
    if name in ('safety_change_propose', 'safety_change_approve', 'safety_approvals_list'):
        return approvals(box, name, args)
    kind = args.get('kind', 'lasers') if name != 'laser_quality_update' else 'lasers'
    if kind not in KINDS:raise ValueError('Agent settings cover lasers and assignments only')
    target = _name(box, kind)
    current = _api(box, 'configuration/' + kind + '/' + target)
    if current.get('status') == 'rejected':return current
    if current['revision'] != args['expected_revision']:
        return dict(status='rejected',reason='Configuration changed; read again before saving')
    before = current.get('document')
    if before is None:before = load_mapping(current['yaml'])
    if name=='laser_quality_update':
        patch=json.loads(args['changes_json'],parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Finite quality values required')))
        if not isinstance(patch,dict) or not patch or len(patch)>8:raise ValueError('Expected 1–8 quality-only scalar changes')
        candidate=copy.deepcopy(before)
        for dotted,value in patch.items():
            path=tuple(dotted.split('.'))
            if (type(value) not in (int,float) or not math.isfinite(value) or value<0
                    or len(path)!=6 or path[0]!='devices' or path[2:4]!=('control_settings','optical')
                    or path[4] not in QUALITY_MAPS or path[5] not in QUALITY_METRICS):
                raise ValueError('Only existing optical classification threshold leaves are allowed')
            parent=candidate
            for key in path[:-1]:parent=parent[key]
            if path[-1] not in parent:raise ValueError('Quality field must already exist')
            parent[path[-1]]=value
            if not quality_change(path,before,candidate):raise ValueError('Not an existing quality-only scalar')
        text=yaml.safe_dump(candidate,sort_keys=False,allow_unicode=True)
        rows,parts=classified(kind,before,candidate)
        if parts[SAFETY] or parts[FORBIDDEN]:
            raise ValueError('Quality tool may only change free classification leaves')
        approval_id=None
    else:
        candidate,text=_candidate(before,args)
        rows,parts=classified(kind,before,candidate)
        approval_id=args.get('approval_id') or None
    summary=dict(kind=kind,name=target,free=[r['path'] for r in parts[FREE]],safety=[r['path'] for r in parts[SAFETY]],
                 forbidden=[r['path'] for r in parts[FORBIDDEN]],changes=rows,
                 quality_change_paths=[r['path'] for r in rows if kind=='lasers' and quality_change(tuple(r['path'].split('.')),before,candidate)])
    if not rows:return dict(status='unchanged',revision=current['revision'],**summary)
    if parts[FORBIDDEN]:
        return dict(status='forbidden',applied=False,
                    reason='Maintainer-only fields (absolute limits, wiring, calibration, identity) cannot be changed by the agent, '
                           'even with an approval. Ask a maintainer through the trusted configuration path.',**summary)
    if parts[SAFETY] and not approval_id:
        return dict(status='requires_human_approval',applied=False,
                    proposal=dict(scope=dict(kind=kind,name=target),changes=proposal_rows(rows)),
                    reason='Safety ranges changed. Call safety_change_propose with this exact proposal, obtain the human\'s '
                           'direct approval in this conversation, then retry with approval_id. No write performed.',**summary)
    if args.get('dry_run','').lower() in ('1','true','yes'):
        return dict(status='dry_run',applied=False,**summary)
    def save():
        payload=dict(yaml=text,expected_revision=args['expected_revision'])
        if approval_id:payload['approval_id']=approval_id
        if name=='laser_quality_update' and getattr(box,'context',{}).get('task_depth')==1:
            value=_api(box,'agent-tasks/'+box.meta['id']+'/quality','PUT',payload)
        else:
            value=_api(box,'configuration/'+kind+'/'+target,'PUT',payload)
        if value.get('status')=='rejected':return value
        live=value.get('live') or {}
        return dict(status='saved',revision=value['revision'],
                    applied_to_running_owner=any(v.get('status') in ('queued','updated') for v in live.get('jobs',{}).values())
                                             or any(v.get('status')=='updated' for v in live.get('native',{}).values()),
                    authority=value.get('authority'),live=live,live_classes=value.get('live_classes'),**summary,
                    message='Saved. live.jobs shows per running job whether the new values were queued for live adoption '
                            '(owner_hot/native_hot at the worker\'s next safe boundary, read laser_runtime_read.laser_live), '
                            'mirrored as runtime parameters, or kept as a snapshot until the next start.')
    return box.once(name,args,save)


def approvals(box, name, args):
    base='lasers/'+box._laser_id+'/agent-approvals'
    if name=='safety_approvals_list':
        return _api(box,base)
    if name=='safety_change_propose':
        scope=json.loads(args['scope_json']);rows=json.loads(args['changes_json'])
        payload=dict(bridge_id=box._bridge_id,scope=scope,changes=rows,reason=args['reason'])
        def propose():
            value=_api(box,base+'/proposals','POST',payload)
            if value.get('status')=='rejected':return value
            return dict(status='proposed',proposal_id=value['id'],nonce=value['nonce'],expires_unix_s=value['expires_unix_s'],
                        approval_instruction=value['approval_instruction'],changes=value['changes'],scope=value['scope'],
                        note='Show the human the exact changes and the code. Only a message the human types in THIS '
                             'conversation can approve; Teams/hub relays and your own messages cannot.')
        return box.once(name,args,propose)
    if name=='safety_change_approve':
        payload=dict(bridge_id=box._bridge_id,quote=args['quote'],human_name=args['human_name'])
        def approve():
            value=_api(box,base+'/'+args['proposal_id']+'/approve','POST',payload)
            if value.get('status')=='rejected':
                return dict(value,applied=False,note='No approval recorded. Do not paraphrase: pass the human\'s message exactly, '
                                                     'and only if it was typed in this conversation after the proposal.')
            return dict(status='approved',approval_id=value['id'],human_name=value['human_name'],approved_unix_s=value['approved_unix_s'],
                        approval_expires_unix_s=value.get('approval_expires_unix_s'),changes=value['changes'],scope=value['scope'],
                        note='Now apply with laser_settings_update or laser_runtime_update citing approval_id; single use.')
        return box.once(name,args,approve)
    raise ValueError('Unknown approval tool')
