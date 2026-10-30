"""Same-laser card gateway; no raw instrument transport or API token exposure."""
import json
import re
from urllib.request import Request,build_opener,ProxyHandler,HTTPRedirectHandler
from urllib.error import HTTPError
from ldcon.core.interface_schema import identifier,RETIRED_OPERATIONS,parameters
from ldcon.core.records import read_json
from ldcon.agents.failure_policy import sanitize

# Laser-agent authority policy (2026-10-21): card operations and settings are
# the agent's own work. Safety ranges are gated by the host approval ledger
# (agent_authority), not by the Claude auto-mode classifier, so every scoped
# MCP tool is preapproved. Put a tool name here to hand it back to native review.
REVIEWED_TOOLS=frozenset()


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        raise ValueError('Local card API must not redirect')


def _api(box,tail,method='GET',payload=None):
    server=read_json(box.root/'web-server.json',{})
    port=server.get('port');prefix=server.get('path')
    if (type(port) is not int or not 1<=port<=65535 or not isinstance(prefix,str)
            or not re.fullmatch(r'/s/[A-Za-z0-9_-]{24,80}/',prefix)):
        raise ValueError('Local dashboard is not configured')
    request=Request('http://127.0.0.1:'+str(port)+prefix+'api/'+tail,
        data=None if payload is None else json.dumps(payload,allow_nan=False).encode(),method=method,
        headers={'Content-Type':'application/json','X-LDCon-Client':'web'})
    try:
        with build_opener(ProxyHandler({}),NoRedirect()).open(request,timeout=15) as response:
            raw=response.read(2**20+1)
            if len(raw)>2**20:raise ValueError('Card API response exceeds limit')
            return json.loads(raw)
    except HTTPError as error:
        if error.code not in (400,403,404,409,422):raise
        try:
            rejected=json.loads(error.read(65536))
            detail=rejected.get('detail','Card API rejected the request')
            operation_error=rejected.get('operation_error')
        except (ValueError,AttributeError):
            detail='Card API rejected the request';operation_error=None
        result=dict(status='rejected',http_status=error.code,reason=sanitize(detail))
        if isinstance(operation_error,dict):result['operation_error']=sanitize(operation_error)
        return result


def _scope(box):
    identifier(box.meta['set_id']);identifier(box.meta['deck_id'])
    bundle=_api(box,'sets/'+box.meta['set_id'])
    if bundle.get('status')=='rejected':return bundle,None
    if bundle.get('laser_definition',{}).get('document',{}).get('id')!=box._laser_id:
        raise ValueError('Current assignment belongs to another laser')
    deck=next((d for d in bundle['deck_definitions'] if d['id']==box.meta['deck_id']),None)
    if not deck:raise ValueError('Resident deck is no longer assigned')
    return bundle,deck


def _overrides(text):
    if len(text.encode())>32768:raise ValueError('Card parameter override exceeds limit')
    def reject(_):raise ValueError('Finite JSON required')
    result=json.loads(text,parse_constant=reject)
    if not isinstance(result,dict):raise ValueError('Card overrides must be a JSON mapping')
    # JSON can parse 1e999 as infinity without calling parse_constant.
    json.dumps(result,allow_nan=False)
    return result



def active_jobs(box):
    """Same-laser discovery across assigned decks; never infer a device lease."""
    value=_api(box,'jobs?active_only=true')
    if value.get('status')=='rejected':return value
    rows=[]
    for job in value.get('jobs',[]):
        if (job.get('set_id')!=box.meta['set_id'] or job.get('laser',{}).get('id')!=box._laser_id
                or job.get('status') in ('failed','cancelled','interrupted','succeeded','completed','completed_with_failures')):
            continue
        rows.append(dict(job_id=job['id'],deck_id=job.get('deck_id'),status=job.get('status'),
            controller_state=job.get('controller_state'),read_only=bool(job.get('read_only')),
            blocks_new_control_card=not job.get('read_only',False),
            cards=[dict(id=c.get('id'),operation=c.get('operation',c.get('card'))) for c in job.get('cards',[])],
            created_utc=job.get('created_utc'),message=sanitize(job.get('message'))))
    return dict(laser_id=box._laser_id,jobs=rows,
        control_slot_blockers=[r['job_id'] for r in rows if r['blocks_new_control_card']],
        evidence='Current dashboard job metadata, not an independent owner lease or physical lock verification. '
                 'Use laser_card_job then laser_log_read for current readings. Never clear or stop an existing card solely because a duplicate start was rejected.')


def call(box,name,args):
    if name=='agent_work_capabilities':
        return box.context.get('execution_profile',dict(status='unavailable',reason='Reconnect with a current work profile'))
    bundle,deck=_scope(box)
    if deck is None:return bundle
    if name=='laser_active_jobs':return active_jobs(box)
    if name=='laser_cards':
        description=_api(box,'lasers/'+box._laser_id+'/operations')
        if description.get('status')=='rejected':return description
        capabilities={row['card_id']:row for row in description.get('cards',[])
            if row.get('set_id')==box.meta['set_id'] and row.get('deck_id')==box.meta['deck_id']}
        rows=[]
        for card in deck['document']['cards']:
            info=capabilities.get(card['id'],dict(runnable=False,
                blocked_reasons=['Operation is not registered for this laser'],parameters={}))
            rows.append(dict(id=card['id'],operation=card['operation'],retired=card['operation'] in RETIRED_OPERATIONS,
                runnable=info['runnable'],blocked_reasons=sanitize(info.get('blocked_reasons',[])),
                parameters=sanitize(info.get('parameters',{}))))
        return dict(laser_id=box._laser_id,set_id=box.meta['set_id'],deck_id=box.meta['deck_id'],
            expected_revision=bundle['revision'],cards=rows,
            parameter_note='Displayed parameters may be redacted. Supply only explicit overrides; defaults remain in the backend.',
            execution_note='Hourly monitoring is read-only; separately authorized card work uses native permission review and existing owners.')
    if name in ('laser_card_job','laser_card_stop'):
        if not re.fullmatch('[0-9a-f]{32}',args['job_id']):raise ValueError('Expected a job ID, not a path')
        job=_api(box,'jobs/'+args['job_id'])
        if job.get('status')=='rejected':return job
        if (job.get('set_id')!=box.meta['set_id'] or job.get('laser',{}).get('id')!=box._laser_id):
            raise ValueError('Job belongs to another laser/deck')
        if name=='laser_card_stop':
            reference=args['human_confirmation'].strip()
            if not reference or len(reference)>2000:raise ValueError('Record the actual human stop/switch request reference')
            if job['id']==box.meta['id']:raise ValueError('Do not stop your own resident transport; use the dashboard for resident replacement')
            def stop():
                result=_api(box,'jobs/'+job['id']+'/stop','POST',{})
                if result.get('status')=='rejected':return result
                return dict(status='stop_requested',job_id=job['id'],job_status=result.get('status'),
                    stop_reconciliation=sanitize(result.get('stop_reconciliation')),
                    human_confirmation=sanitize(reference),
                    note='Poll laser_card_job until terminal. For an orphan operating-point proxy, inspect stop_reconciliation for the correlated parent/queue and independent owner proof; unknown prior effects remain unknown. For a stopped lock, use laser_stop_verify. Never kill, clear holds or infer physical state from pre-stop readings.')
            return box.once(name,args,stop)
        return dict(id=job['id'],status=job['status'],controller_state=job.get('controller_state'),
            stop_reconciliation=sanitize(job.get('stop_reconciliation')),
            message=sanitize(job.get('message')),online_tuning=sanitize(job.get('online_tuning',{})),
            summary=sanitize(job.get('summary',{})),readings=sanitize(job.get('readings',{})),
            cards=[dict(id=c.get('id'),operation=c.get('operation',c.get('card'))) for c in job.get('cards',[])],
            evidence='Recorded dashboard state; use laser_log_read for details. Not independent hardware verification.')
    identifier(args['card_id'])
    card=next((c for c in deck['document']['cards'] if c['id']==args['card_id']),None)
    if not card or card['operation'] in RETIRED_OPERATIONS:raise ValueError('Select a current card in this laser deck')
    if card['operation'] in ('agent.resident','agent.log_watch','agent.request','notify.teams'):
        raise ValueError('Use the dedicated resident/notification tools; no recursive agent launch')
    if args['expected_revision']!=bundle['revision']:
        return dict(status='rejected',reason='Assignment changed; refresh laser_cards and validate again')
    overrides=_overrides(args.get('parameters_json','{}'))
    # Validate locally before any HTTP mutation, with current assignment defaults.
    p=parameters(card,{**deck.get('invocation_parameters',{}).get(card['id'],{}),**overrides})
    route=f"sets/{box.meta['set_id']}/decks/{box.meta['deck_id']}"
    payload=dict(parameters=overrides,expected_revision=bundle['revision'])
    if name=='laser_card_validate':
        result=_api(box,route+'/cards/'+card['id']+'/validate','POST',payload)
        return dict(status='rejected' if result.get('status')=='rejected' else 'validated',
            runnable=result.get('runnable',False),reason=result.get('reason'),
            blocked_reasons=sanitize(result.get('blocked_reasons',[])),
            parameters=sanitize(result.get('parameters',p)),expected_revision=bundle['revision'],
            operation_error=sanitize(result.get('operation_error')))
    if name!='laser_card_start':raise ValueError('Unknown card tool')
    reference=args.get('human_confirmation','').strip()
    if not reference or len(reference)>2000:raise ValueError('Record the actual human request/confirmation reference')
    def start():
        # No automatic stop, service restart, lease takeover or hold release.
        result=_api(box,route+'/start','POST',dict(payload,card_id=card['id']))
        if result.get('status')=='rejected':
            if result.get('http_status')==409:
                try:result=dict(result,active_jobs=active_jobs(box))
                except (OSError,ValueError):result=dict(result,active_jobs=dict(status='unavailable'))
            return result
        return dict(status='submitted',job_id=result['id'],card_id=card['id'],
            job_status=result.get('status'),online_tuning=sanitize(result.get('online_tuning',{})),
            operation_request_id=result.get('operation_request_id'),
            human_confirmation=sanitize(reference),note='Accepted by card system; not proof of completion. Inspect laser_card_job.')
    return box.once(name,args,start)
