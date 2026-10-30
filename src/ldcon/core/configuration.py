"""Revision-checked configuration edits, validated before atomic replacement."""
from ldcon.core.state_layout import config_file, config_root

import hashlib
import os
from pathlib import Path
import shutil
import tempfile
import uuid
from urllib.parse import urlsplit
from ldcon.core.interface_schema import InterfaceRepository, load_mapping
from ldcon.web.web_runtime import ConflictError, TERMINAL, safe_id

KINDS=('lasers','interface-decks','assignments')


def preserve_numeric_types(before, after):
    """Keep existing numeric representations across browser JSON serialization.

    Policy digests also distinguish 0 from 0.0. Restore the saved numeric type
    where the conversion is exact, including a changed float-valued setting.
    Never coerce booleans, strings, fractional integers or rounded large ints.
    """
    import math
    if isinstance(before, dict) and isinstance(after, dict):
        return {k:preserve_numeric_types(before[k], v) if k in before else v for k,v in after.items()}
    if isinstance(before, list) and isinstance(after, list) and len(before)==len(after):
        return [preserve_numeric_types(a,b) for a,b in zip(before,after)]
    if type(before) in (int,float) and type(after) in (int,float) and math.isfinite(after):
        converted=type(before)(after)
        if converted==after:return converted
    return after


def _endpoint(value):
    """Normalize documented identities locally; never probe hardware or DNS."""
    if not isinstance(value,str):return None
    parsed=urlsplit(value)
    if not parsed.hostname:return None
    return (parsed.scheme.lower(),parsed.hostname.lower().rstrip('.'),
            parsed.port or (443 if parsed.scheme=='https' else 80),parsed.path.rstrip('/'))


def _writable_identities(laser):
    """Conservative physical/channel and broker identities, excluding readers.

    This is an edit guard, not a replacement for owner leases/physical locks.
    Different DNS names for the same device cannot be resolved from a draft.
    """
    keys=set()
    for device in laser['devices'].values():
        kind=device['type'];connection=device['connection']
        if kind.startswith('metrics.') or kind in ('fizeau.http','fizeau.owner'):continue
        before=len(keys)
        channel=connection.get('channel',0 if kind=='thorlabs.cld1015.broker' else None)
        endpoint=_endpoint(connection.get('endpoint'))
        owner=connection.get('instrument',connection.get('device_id'))
        if endpoint and owner:keys.add(('owner',endpoint,str(owner),channel))
        # ECDL broker routing uses laser ID; resources below identify its devices.
        if endpoint and kind=='ecdl.rig.owner':keys.add(('owner',endpoint,laser['id'],None))
        entries=[connection,*connection.get('resources',{}).values()]
        for entry in entries:
            if not isinstance(entry,dict):continue
            resource=entry.get('resource')
            host=entry.get('host')
            resource_channel=entry.get('channel',channel)
            if isinstance(resource,str) and resource.lower()!='auto':
                normalized=resource.strip().lower()
                keys.add(('resource',normalized,resource_channel))
                parts=normalized.split('::')
                if len(parts)>1 and parts[0].startswith('tcpip'):host=parts[1]
            if isinstance(host,str):keys.add(('host',host.lower().rstrip('.'),resource_channel))
        if len(keys)==before and not endpoint:
            raise ConflictError('Cannot establish writable device identity while another card is active')
        if endpoint and owner is None and kind!='ecdl.rig.owner':
            keys.add(('endpoint',endpoint,channel))
    return keys


def _overlap(left,right):
    # A draft with no channel identifies the whole device, never a free channel.
    return any(a[:-1]==b[:-1] and (a[-1] is None or b[-1] is None or a[-1]==b[-1])
               for a in left for b in right)


def _parameter_only(rows):
    """Assignment edits that touch only per-card invocation parameters."""
    return bool(rows) and all(r['path'].split('.')[:1]==['decks'] and r['path'].split('.')[2:3]==['parameters']
                              and len(r['path'].split('.'))>=4 for r in rows)


def _append_only_deck(runtime, name, data):
    """New definitions cannot alter frozen jobs or any existing card reference.

    Normal maintainer authority, revision checks and complete schema validation
    still apply. Replacing, removing or reordering existing cards remains blocked.
    """
    before = runtime.interface_repository.read('interface-decks', name)['document']
    old_cards, new_cards = before.get('cards', []), data.get('cards', [])
    return (len(new_cards) > len(old_cards) and new_cards[:len(old_cards)] == old_cards
            and {k:v for k,v in before.items() if k != 'cards'} ==
                {k:v for k,v in data.items() if k != 'cards'})


def _task_execution_only_deck(runtime,name,data):
    """Change future task routing/selection, preserving every physical card.

    Running workers retain their frozen policies. Approved envelopes, task
    instruction profiles, identities, operations and fallbacks cannot change.
    """
    before=runtime.interface_repository.read('interface-decks',name)['document']
    if {k:v for k,v in before.items() if k!='cards'}!={k:v for k,v in data.items() if k!='cards'}:return False
    old,new=before.get('cards',[]),data.get('cards',[])
    if len(old)!=len(new):return False
    fields={'agent_card_id','execution_profile','request_delegation','adjustment_family',
            'adjustment_plan_card_id','adjustment_objective'}
    changed=False
    for a,b in zip(old,new):
        if a==b:continue
        changed=True
        if a.get('operation') not in ('agent.task','agent.history_review','optimize.settling_criteria','optimize.target_feedforward','optimize.target_thermal'):return False
        if {k:v for k,v in a.items() if k not in ('agent_execution','parameters')}!={k:v for k,v in b.items() if k not in ('agent_execution','parameters')}:return False
        if {k:v for k,v in a.get('parameters',{}).items() if k not in fields}!={k:v for k,v in b.get('parameters',{}).items() if k not in fields}:return False
    return changed


def _check_active(runtime,active,kind,name,data,rows=None):
    """Protect active definitions and reject aliases of their writable channels.

    Device-free jobs (resident agents, log watches, notifications, reports)
    own no instrument and never block an edit. While device-owning cards run:
    laser edits are allowed when every changed path is hot or snapshot class
    (``agent_authority.live_class``); hard limits and identity (idle_only)
    still need the card stopped. Assignment edits that change only card
    invocation parameters are allowed (running jobs keep frozen snapshots).
    """
    for job in active:
        if job.get('read_only'):continue
        laser_id=job.get('laser',{}).get('id')
        blocked=False
        if kind=='lasers' and laser_id==name:
            blocked=rows is None or any(r.get('live')=='idle_only' for r in rows)
        elif kind=='interface-decks' and job.get('deck_id')==name:
            blocked=not (_append_only_deck(runtime,name,data) or _task_execution_only_deck(runtime,name,data))
        elif kind=='assignments' and job.get('set_id')==name:
            blocked=not (rows is not None and _parameter_only(rows))
        if blocked:
            raise ConflictError('This configuration is used by an active card; stop that card and verify its owner leases before editing')
        if not laser_id:
            raise ConflictError('An active card has unknown laser ownership; inspect it before editing')
        if kind!='lasers' or laser_id==name:continue
        snapshot=runtime.directory(job['id'])/'laser.yaml'
        if not snapshot.is_file() or snapshot.is_symlink():
            raise ConflictError('An active card has no verified laser snapshot; inspect it before adding or editing devices')
        frozen=load_mapping(snapshot.read_text())
        if _overlap(_writable_identities(data),_writable_identities(frozen)):
            raise ConflictError('A writable device/channel overlaps an active laser; preserve its owner configuration until idle')


NATIVE_OWNER='esp32servocon.native_current.owner'


def owner_rpc(endpoint,request,timeout_s=10.):
    """One POST to an independent owner; mutations are never retried."""
    import json as _json
    from urllib.request import Request,urlopen
    from ldcon.core.instrument_client import validate_endpoint
    url=validate_endpoint(endpoint)+'/rpc'
    with urlopen(Request(url,method='POST',data=_json.dumps(request,allow_nan=False).encode(),
                         headers={'Content-Type':'application/json'}),timeout=timeout_s) as response:
        result=_json.load(response)
    if not result.get('ok'):
        raise ConflictError('Owner refused the live policy update: '+str(result.get('error','unknown error')))
    return result['result']


RIG_OWNER='ecdl.rig.owner'


def _adopt_rig_settings(name,after,revision_after,rows,decision,rpc):
    """Rule M step 1 (2026-10-27): after the yaml is published, ask every ECDL rig owner of this laser to adopt it.

    Lease-less owner: adopted at once (previously it waited for the next claim). Held lease: the owner defers to
    its own safe boundary (the worker's queued reload may adopt first). The save never fails on this call: the
    file is already published and the worker reload is already queued; the owner's answer or refusal is reported.
    """
    from ldcon.core.agent_authority import SAFETY
    results={}
    for key,device in (after.get('devices') or {}).items():
        if not isinstance(device,dict) or device.get('type')!=RIG_OWNER:continue
        endpoint=(device.get('connection') or {}).get('endpoint')
        if not endpoint:
            results[key]=dict(status='unavailable',reason='rig owner endpoint is not configured');continue
        request=dict(action='settings_adopt',saved_revision=revision_after,
                     decision_id=decision['id'] if any(r['class']==SAFETY for r in rows) else None)
        try:
            results[key]=rpc(endpoint,dict(action='ecdl',instrument=device['connection'].get('instrument',name),request=request))
        except ConflictError as error:
            results[key]=dict(status='refused',reason=str(error))
        except Exception as error:   # connection refused / timeout: the owner is down or restarting
            results[key]=dict(status='unavailable',reason=str(error))
    return results


def _queue_laser_reloads(runtime,name,rows,revision_before,revision_after,decision):
    from ldcon.jobs.laser_live import enqueue_reload
    results={}
    for job in runtime.active_jobs():
        if job['status'] in TERMINAL or job.get('read_only') or job.get('laser',{}).get('id')!=name:continue
        results[job['id']]=enqueue_reload(runtime.directory(job['id']),laser_id=name,revision_before=revision_before,
                                          revision_after=revision_after,rows=rows,decision=decision)
    return results


def _mirror_assignment_runtime(runtime,name,before,after,rows,decision):
    """Push changed invocation parameters into running cards of this set (runtime-parameters)."""
    from ldcon.core.records import read_json
    results={}
    touched={}
    for r in rows:
        parts=r['path'].split('.')
        if parts[:1]==['decks'] and parts[2:3]==['parameters'] and len(parts)>=5:
            touched.setdefault((int(parts[1]),parts[3]),set()).add(parts[4])
    if not touched:return results
    for job in runtime.active_jobs():
        if job['status']!='running' or job.get('set_id')!=name:continue
        card_id=next((c.get('id') for c in job.get('cards',[])),None)
        index=next((i for i,d in enumerate(after.get('decks',[])) if d.get('deck_id')==job.get('deck_id')),None)
        if card_id is None or index is None or (index,card_id) not in touched:continue
        directory=runtime.directory(job['id'])
        live=read_json(directory/'runtime-parameters.json',{})
        schema=live.get('schema',{}) if live.get('status')=='running' else {}
        fields=touched[(index,card_id)]
        values=after['decks'][index]['parameters'].get(card_id,{})
        patch={f:values[f] for f in fields if f in schema and f in values}
        snapshot=sorted(fields-set(patch))
        if not patch:
            results[job['id']]=dict(status='snapshot',fields=snapshot,reason='fields are not injectable into this running card; next start uses them')
            continue
        payload=dict(request_id=uuid.uuid4().hex,job_id=job['id'],card_id=card_id,expected_revision=live.get('revision'),patch=patch)
        try:
            value=runtime.inject_parameters(job['id'],payload,preauthorized=decision,actor='configuration-mirror')
            results[job['id']]=dict(status=value.get('status','queued'),request_id=payload['request_id'],fields=sorted(patch),snapshot=snapshot)
        except (ValueError,ConflictError) as error:
            results[job['id']]=dict(status='rejected',reason=str(error),fields=sorted(patch),snapshot=snapshot)
    return results


def save(runtime, kind, name, text, expected, *, approval_id=None, maintainer_reference=None,
         maintainer_token=None, actor='dashboard', native_rpc=owner_rpc, target_response_binding=None, study_criteria_binding=None):
    """Validate, authorize and atomically replace one definition.

    Authority (``agent_authority``): free fields need nothing; safety ranges
    need a direct human approval id (agent) or a maintainer reference; fields
    classed forbidden are maintainer-only. Creation stays a maintainer action.
    """
    from ldcon.core.agent_authority import ApprovalLedger, describe_changes, check_ceilings, authorize, partition, SAFETY
    if kind not in KINDS:raise ValueError('Unknown configuration kind')
    safe_id(name)
    from ldcon.owners.dlcpro_target_store import configuration_guard
    with runtime.mutex, configuration_guard(runtime, kind, name):
        runtime.assert_admission_open()
        from ldcon.core.configuration_transaction import reconcile_pending, pending_transactions, commit_native_file
        reconcile_pending(runtime, rpc=native_rpc)
        unresolved = pending_transactions(runtime.root)
        if any(t.get('laser_id') in (None, name) for t in unresolved):
            raise ConflictError('A policy transaction is pending reconciliation; inspect its durable journal before another edit')
        active=[j for j in runtime.active_jobs() if j['status'] not in TERMINAL]
        path=config_file(runtime.root,kind,name)
        creating=expected is None
        if creating:
            if path.exists() or path.is_symlink():raise ConflictError('Configuration already exists; reload before editing')
            before=None
        else:
            current=runtime.interface_repository.read(kind,name)
            if current['revision']!=expected:raise ConflictError('Configuration changed. Reload before saving.')
            before=current['document']
        data=load_mapping(text)
        if before is not None:
            import json
            normalized=preserve_numeric_types(before,data)
            # Preserve submitted YAML/comments unless a numeric representation
            # actually changed. JSON is valid YAML and gives an exact digest.
            if json.dumps(normalized,sort_keys=True)!=json.dumps(data,sort_keys=True):
                data=normalized
                text=json.dumps(data,indent=2,ensure_ascii=False,allow_nan=False)+'\n'
        if data.get('id')!=name:raise ValueError('Configuration ID must match filename')
        rows=[] if creating else describe_changes(kind,before,data)
        if study_criteria_binding is not None:
            from ldcon.jobs.study_adoption import authorize_criteria
            rows=authorize_criteria(runtime,kind,name,before,data,rows,study_criteria_binding)
        if kind == 'lasers' and rows:
            pending_jobs = [job for job in active if job.get('laser', {}).get('id') == name
                            and any(runtime.directory(job['id']).glob('laser-reload-request-*.json'))]
            if pending_jobs:
                raise ConflictError('A previous saved laser reload is still pending; wait for its automatic safe-boundary adoption before another edit')
        ledger=None;decision=dict(authority='creation' if creating else 'free',free=[],safety=[],forbidden=[])
        if rows:
            laser_id=name if kind=='lasers' else data.get('laser_id') if kind=='assignments' else None
            laser_document=data if kind=='lasers' else runtime.interface_repository.read('lasers',laser_id)['document'] if laser_id else {}
            check_ceilings(kind,data,laser_document)
            if laser_id:
                ledger=ApprovalLedger(runtime.root,laser_id)
                decision=authorize(ledger,kind=kind,scope=dict(kind=kind,name=name),rows=rows,approval_id=approval_id,
                                   maintainer_reference=maintainer_reference,maintainer_token=maintainer_token)
            else:
                decision=authorize(None,kind=kind,scope=dict(kind=kind,name=name),rows=rows,approval_id=None,
                                   maintainer_reference=maintainer_reference,maintainer_token=maintainer_token)
        if kind == 'lasers' and before is not None:
            from ldcon.core.target_policy import validate_transition
            validate_transition(before, data)
        with tempfile.TemporaryDirectory() as temporary:
            stage=Path(temporary)
            for directory in KINDS:shutil.copytree(config_root(runtime.root)/directory,stage/directory)
            (stage/kind/(name+'.yaml')).write_text(text)
            repository=InterfaceRepository(stage)
            repository.read(kind,name)
            for assignment in repository.list('assignments'):repository.assignment(assignment['id'])
            _check_active(runtime,active,kind,name,data,rows if not creating else None)
        live={}
        decision_record=None
        if ledger is not None and rows:
            decision_record=ledger.record_decision(scope=dict(kind=kind,name=name),rows=rows,decision=decision,actor=actor,
                                                   revision_before=expected,revision_after=hashlib.sha256(text.encode()).hexdigest())
            if kind=='lasers' and any(r.get('live')=='native_hot' for r in rows):
                live['native']=commit_native_file(runtime,name=name,path=path,before=before,after=data,text=text,
                    expected=expected,rows=rows,decision=decision_record,rpc=native_rpc)
        if 'native' not in live:
            # Recheck after validation: a CLI editor may have changed the file.
            if not creating and hashlib.sha256(path.read_bytes()).hexdigest()!=expected:raise ConflictError('Configuration changed during validation')
            descriptor,temporary_name=tempfile.mkstemp(prefix='.'+name+'-',suffix='.yaml.new',dir=path.parent)
            temporary=Path(temporary_name)
            try:
                with os.fdopen(descriptor,'w',encoding='utf-8') as stream:
                    os.fchmod(stream.fileno(),0o660)
                    stream.write(text);stream.flush();os.fsync(stream.fileno())
                if creating:
                    try:os.link(temporary,path)  # Atomic create-only publication.
                    except FileExistsError as error:raise ConflictError('Configuration was created during validation') from error
                else:temporary.replace(path)
            finally:temporary.unlink(missing_ok=True)
        saved=runtime.interface_repository.read(kind,name)
        if ledger is not None and rows:
            if kind=='lasers':
                live['jobs']=_queue_laser_reloads(runtime,name,rows,expected,saved['revision'],
                    dict(decision, decision_id=decision_record['id'],
                         **({'target_response_binding':target_response_binding} if target_response_binding else {})))
                live['owner']=_adopt_rig_settings(name,data,saved['revision'],rows,decision_record,native_rpc)
            elif kind=='assignments':
                live['jobs']=_mirror_assignment_runtime(runtime,name,before,data,rows,decision)
            result=dict(revision_before=expected,revision_after=saved['revision'],live=live,
                        decision_id=decision_record['id'] if decision_record else None)
            if decision.get('authority')=='human_approval' and 'native' not in live:
                ledger.consume(approval_id,scope=dict(kind=kind,name=name),safety_rows=partition(rows)[SAFETY],result=result)
            ledger.audit(dict(event='configuration_saved',actor=actor,changes=rows,result=result,**decision))
        return {**saved,'authority':decision,'live':live,
                'live_classes':{r['path']:r.get('live') for r in rows} if kind=='lasers' else {}}
