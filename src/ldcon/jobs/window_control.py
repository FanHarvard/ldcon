"""Midpoint admission and scalar supervision inside the existing lock worker."""
import copy
import time
import uuid

from ldcon.core.interface_schema import digest
from ldcon.core.records import atomic_json, read_json
from ldcon.jobs.window_center import parameters as window_parameters
from ldcon.jobs.window_actions import WindowQueue, ScalarGuard, normalize_guard


def validate_selection(deck, controller, config):
    config=normalize_guard(config)
    if not config['card_id']:return None
    card=next((c for c in deck['cards'] if c['id']==config['card_id'] and
               c['operation']=='tune.ecdl_window_center'),None)
    if card is None:raise ValueError('Select a midpoint card from the running deck')
    p=window_parameters(card,config['parameters'])
    if p['controller_card_id']!=controller or p['mode']!='center_only':
        raise ValueError('Select a finite center_only card for this steady controller')
    if not p['boundary_limits'] and p['measure_dither']:raise ValueError('Midpoint search needs explicit measured boundary limits')
    retired={'metrics.A','metrics.B','metrics.C','metrics.F2'}
    if retired.intersection(p['observables']):raise ValueError('Retired fringe metrics cannot drive midpoint search')
    return p


class WindowControl:
    def __init__(self,bridge):
        self.bridge=bridge;self.directory=bridge.directory
        self.queue=WindowQueue(self.directory);self.guard=None;self.config=None
        self.active_id=None;self.capability=None;self.last_reason=None
        self.sync()

    def sync(self):
        b=self.bridge
        config=normalize_guard(b.steady.get('window_guard'))
        selected=validate_selection(b.deck,b.card_id,config)
        if config!=self.config:
            self.config=copy.deepcopy(config)
            if self.guard:self.guard.set_config(config)
            else:self.guard=ScalarGuard(config)
        self.capability=dict(status='running',job_id=self.directory.name,controller_card_id=b.card_id,
            capability_revision=digest(dict(device=b.revision,owner_runtime=read_json(self.directory/'owner-runtime-context.json',{}))),guard_revision=digest(config),updated_unix_s=time.time(),
            config=config,selected_parameters=selected,last_reason=self.last_reason,
            evaluation=getattr(self.guard,'last_evaluation',None),
            cards=[c for c in b.deck['cards'] if c['operation']=='tune.ecdl_window_center'])
        atomic_json(self.directory/'window-controller.json',self.capability)

    def invalidate(self,reason):
        self.guard.invalidate(reason);self.last_reason=reason
        for request in self.queue.list():
            if request['source']=='auto' and request['status']=='queued':
                self.queue.cancel(request['request_id'],reason='Fresh guard confirmation required: '+reason)

    def finish(self,value):
        if self.active_id and value.get('request_id')==self.active_id and value.get('status') in (
                'succeeded','failed','cancelled','rejected'):
            self.queue.complete(self.active_id,value['status'],result=value,reason=value.get('reason'))
            self.active_id=None
            self.invalidate('Midpoint action completed; acquire new confirmations')

    def cycle(self,row,lock,busy):
        self.sync();c=self.capability
        if busy:
            self.invalidate('Another operation or recovery is active');return
        rate=self.queue.rate_status(cooldown_s=self.config['cooldown_s'],max_per_hour=self.config['max_per_hour'])
        evidence=self.guard.observe(row,context=(row.get('mode_id'),c['guard_revision'])) if not rate['limited'] else None
        if evidence:
            try:self.queue.enqueue(dict(request_id=uuid.uuid4().hex,source='auto',reason='Scalar guard threshold confirmed',
                card_id=self.config['card_id'],controller_card_id=self.bridge.card_id,
                parameters=c['selected_parameters'],capability_revision=c['capability_revision'],
                guard_revision=c['guard_revision'],ttl_s=300.,evidence=evidence),
                capability_revision=c['capability_revision'])
            except ValueError as error:
                self.last_reason=str(error)
                self.guard.invalidate('Queue admission deferred')
            else:self.guard.note_trigger()
        request=self.queue.claim(capability_revision=c['capability_revision'],guard_revision=c['guard_revision'],
            cooldown_s=self.config['cooldown_s'],max_per_hour=self.config['max_per_hour'])
        if not request:return
        self.active_id=request['request_id']
        try:
            selected=dict(self.config,card_id=request['card_id'],parameters=request['parameters'])
            p=validate_selection(self.bridge.deck,self.bridge.card_id,selected)
            if not p:raise ValueError('No midpoint card selected')
            from ldcon.jobs.ecdl_window_center import WindowCenterSession
            # A trigger requests a fresh two-sided measurement. Old seeds lack
            # a threshold/observable signature and cannot qualify this action.
            seed=None
            self.bridge.window=WindowCenterSession(request,p,lock,seed,self.bridge.emit,self.bridge.publish,self.bridge.steady)
            self.bridge.publish(dict(request_id=request['request_id'],card_id=request['card_id'],status='running'))
            self.bridge.emit('window_action_started',dict(request_id=request['request_id'],source=request['source'],reason=request.get('reason')))
        except (ValueError,KeyError) as error:
            self.bridge.publish(dict(request_id=request['request_id'],card_id=request['card_id'],status='rejected',reason=str(error)))
            if lock.uncertain_output:raise

    def close(self):
        if self.active_id:
            self.queue.complete(self.active_id,'unknown',reason='Worker exit before confirmed action completion')
        atomic_json(self.directory/'window-controller.json',{**self.capability,'status':'ended','updated_unix_s':time.time()})


def state(directory):
    c=read_json(directory/'window-controller.json',{})
    if not c:return dict(status='unavailable',requests=[])
    q=WindowQueue(directory)
    return {**c,'requests':q.list(),'rate':q.rate_status(cooldown_s=c['config']['cooldown_s'],max_per_hour=c['config']['max_per_hour'])}


def enqueue(directory,payload,*,source='manual'):
    c=read_json(directory/'window-controller.json',{})
    if c.get('status')!='running' or time.time()-c.get('updated_unix_s',0)>30:
        raise ValueError('Steady worker midpoint capability is unavailable or stale')
    if payload.get('capability_revision')!=c['capability_revision']:
        raise ValueError('Controller changed; reload midpoint capabilities')
    if source=='agent':
        if not c['config']['agent_enabled'] or payload.get('guard_revision')!=c['guard_revision']:
            raise ValueError('Agent midpoint action is not approved for this revision')
        card_id=c['config']['card_id'];p=c['selected_parameters']
    else:
        card_id=payload.get('card_id',c['config']['card_id'])
        selection=dict(c['config'],card_id=card_id,parameters=payload.get('parameters',{}))
        p=validate_selection(dict(cards=c['cards']),c['controller_card_id'],selection)
    if not p:raise ValueError('No approved finite midpoint action selected')
    request=dict(request_id=payload.get('request_id') or uuid.uuid4().hex,source=source,
        card_id=card_id,controller_card_id=c['controller_card_id'],parameters=p,
        capability_revision=c['capability_revision'],guard_revision=c['guard_revision'],
        reason=payload.get('reason','Manual midpoint request'),ttl_s=payload.get('ttl_s',3600.))
    if 'not_before_unix_s' in payload:request['not_before_unix_s']=payload['not_before_unix_s']
    return WindowQueue(directory).enqueue(request,capability_revision=c['capability_revision'])
