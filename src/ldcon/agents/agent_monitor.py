"""Independent schedule/condition observer; no model polling or instrument I/O."""
import copy
import json
import math
from pathlib import Path
import time
from types import SimpleNamespace

from experiment_core.agent_contracts import Conditions
from experiment_core.records import atomic_json, digest
from ldcon.core.interface_cards import CardCancelled


def parameters(card, overrides):
    defaults = dict(agent_card_id='', task_card_id='', interval_s=3600., lookback_s=3600.,
        sample_interval_s=2., conditions=[], prompt='', enabled=True, follow_quality_thresholds=False,
        history_task_card_id='', history_interval_s=0., history_min_new_samples=1000,
        background=dict(enabled=False))
    p = {**defaults, **card['parameters'], **overrides}
    if set(p) != set(defaults):
        raise ValueError('Unknown agent monitor parameter')
    from ldcon.core.interface_schema import identifier
    for key in ('agent_card_id', 'task_card_id', 'history_task_card_id'):
        if p[key]:identifier(p[key])
    if type(p['follow_quality_thresholds']) is not bool:
        raise ValueError('Quality binding must be boolean')
    if type(p['enabled']) is not bool:
        raise ValueError('Monitor enabled must be boolean')
    for key, lower, upper in (('interval_s', 0, 31536000), ('lookback_s', 1, 31536000),
                              ('sample_interval_s', .1, 60), ('history_interval_s', 0, 31536000)):
        if type(p[key]) not in (int, float) or not math.isfinite(p[key]) or not lower <= p[key] <= upper:
            raise ValueError('Invalid ' + key)
    if type(p['history_min_new_samples']) is not int or not 1 <= p['history_min_new_samples'] <= 10**9:
        raise ValueError('Invalid history sample threshold')
    if not isinstance(p['prompt'], str) or len(p['prompt']) > 4000:
        raise ValueError('Invalid monitoring prompt')
    Conditions.validate(p['conditions'])
    from ldcon.core.adjustment_plan import approved_adjustment_plan
    p['background'] = approved_adjustment_plan(p['background'])
    if p['background']['enabled'] and not p['task_card_id']:
        raise ValueError('Automatic adjustment requires a task and explicit approved bounds')
    return copy.deepcopy(p)


def snapshot(box):
    """Read only active indexed jobs and their recorded snapshots."""
    from ldcon.agents.agent_cards import _api
    from ldcon.agents.agent_log_read import read
    rows = _api(box, 'jobs?active_only=true').get('jobs', [])
    jobs = [j for j in rows if j.get('set_id') == box.meta['set_id'] and
            j.get('laser', {}).get('id') == box._laser_id and any(
                str(c.get('operation', c.get('card', ''))).startswith('servo.') for c in j.get('cards', []))]
    metrics = {}
    for job in jobs:
        value = read(box.root, box._laser_id, job_id=job['id'], kind='snapshot', filename='readings.json')
        def collect(node, prefix='', acquired=None):
            if not isinstance(node, dict):return
            # Native cards also record monotonic acquired_s. Never interpret it
            # as Unix time or substitute the time this file was read.
            stamp = node.get('source_acquired_unix_s', node.get('acquired_unix_s', node.get('source_unix_s', acquired)))
            for key, item in node.items():
                path = (prefix+'.' if prefix else '')+key
                if isinstance(item, dict):collect(item, path, stamp)
                elif type(item) in (int, float) and math.isfinite(item):
                    source = node.get('dither_source_unix_s', stamp) if key == 'dither_response' else stamp
                    metrics[path] = dict(value=item, acquired_unix_s=source,
                        valid=source is not None and node.get(key+'_valid', node.get('valid', True)) is not False)
        document=value.get('content', value.get('data', value))
        collect(document)
        # DLC optics have their own HTTP acquisition clock. A new WSU UDP
        # frequency sample must not refresh a cached/invalid fringe frame.
        quality=(document.get('observation') or {}).get('quality') if isinstance(document,dict) else None
        if isinstance(quality,dict):
            for name in quality.get('metrics') or {}:
                key='observation.quality.metrics.'+name
                if key in metrics:
                    metrics[key]['valid']=quality.get('fresh') is True and quality.get('quality_valid') is True
                    metrics[key]['acquired_unix_s']=quality.get('acquired_unix_s')
    for source, target, scale, absolute in (
            ('error_hz','abs_error_hz',1.,True),
            ('native_interval_us','source_interval_s',1e-6,False),
            ('native_interval_last_s','source_interval_s',1.,False)):
        if source in metrics:
            raw=metrics[source]
            metrics[target]={**raw,'value':(abs(raw['value']) if absolute else raw['value'])*scale,
                             'derived_from':source}
    if 'integral_v' in metrics and 'control.integral_limit_v' in metrics:
        integral,limit=metrics['integral_v'],metrics['control.integral_limit_v']
        if limit['value']>0:
            metrics['integral_utilization']={**integral,'value':abs(integral['value'])/limit['value'],
                'valid':integral['valid'] and limit['valid'],'derived_from':['integral_v','control.integral_limit_v']}
    return dict(jobs=jobs, metrics=metrics,
                authority='recorded data only; no physical state proof or new grant')


def run(directory, p, *, clock, sleep, cancel, emit, runtime=None, snapshot_fn=None, task_start=None):
    from ldcon.agents.resident_agent import context
    from ldcon.core.interface_schema import load_mapping
    from ldcon.core.records import read_json
    from ldcon.agents.agent_cards import _api
    directory = Path(directory)
    store, meta = context(directory)
    laser_document = load_mapping((directory/'laser.yaml').read_text())
    laser_id = laser_document['id']
    box = SimpleNamespace(root=store.root.parent, meta=meta, _laser_id=laser_id)
    status_path = directory/'agent-monitor-status.json'
    state = read_json(status_path, {})
    if not state:
        state = dict(schema_version=1, job_id=meta['id'], generation=digest([meta['id'], time.time()]),
                     sequence=0, last_review=None, last_history=None, samples=0, conditions={},
                     request_id=None, task_job_id=None, pending_events=[])
    if state['job_id'] != meta['id']:
        raise ValueError('Monitor state belongs to another job')
    detector = Conditions(p['conditions'], state['conditions'])
    quality_read_at=0.;quality_document=None
    last_stamps = state.setdefault('last_stamps', {})
    last_snapshot = {}
    key = store.key(meta, p['agent_card_id']) if p['agent_card_id'] else None

    def publish(phase):
        previous_phase=state.get('state')
        if phase!='control_data_unavailable' and state.get('data_unavailable_since') is not None:
            emit('agent_monitor_data_recovered',dict(duration_s=max(0,time.time()-state['data_unavailable_since'])))
            state.pop('data_unavailable_since',None)
        state.update(state=phase, updated_unix_s=time.time(), conditions=detector.state,
                     enabled=p['enabled'], background=p['background'])
        atomic_json(status_path, state)
        if phase != previous_phase:
            emit('state',dict(state=phase,monitor_task_job_id=state.get('task_job_id'),
                reason=('An adjustment result needs inspection; new automatic trials are paused'
                        if phase=='task_requires_inspection' else None)))
        try:
            from ldcon.agents.agent_background import control_root
            grant_dir=control_root(box.root)/laser_id/'monitors'
            if grant_dir.is_symlink():raise ValueError('Monitor grant directory must not be a symlink')
            grant_dir.mkdir(parents=True,exist_ok=True)
            atomic_json(grant_dir/(meta['id']+'.json'),dict(generation=state['generation'],bounds=p['background'],
                enabled=p['enabled'] and p['background']['enabled'] and not phase.startswith('stopped') and phase not in ('control_data_unavailable','waiting_controller'),
                last_seen=time.time(),max_age_s=max(30,p['sample_interval_s']*3),parameters_hash=digest(p)))
        except (ValueError,OSError):
            state['background_grant']='unavailable; passive review remains available'

    def defer_read(error):
        from ldcon.agents.agent_background import revoke_monitor
        revoke_monitor(box.root,laser_id,meta['id'],state['generation'])
        if state.get('data_unavailable_since') is None:
            state['data_unavailable_since']=time.time()
            emit('agent_monitor_data_wait',dict(reason=type(error).__name__,
                message='Control API unavailable; automatic effects revoked, deterministic lock unchanged'))
        publish('control_data_unavailable')
        sleep(p['sample_interval_s'])

    def start(admission):
        if task_start:return task_start(admission['card_id'], admission['prompt'], state)
        return _api(box, 'agent-tasks/start', 'POST', admission['payload'])

    try:
        while not cancel():
            if runtime:
                runtime.poll(boundary='between_reviews', busy=bool(state['request_id'] or state['task_job_id']))
            now = time.time()
            rules=p['conditions']
            if p['follow_quality_thresholds']:
                if now-quality_read_at>=10 or quality_document is None:
                    try:
                        record=_api(box,'configuration/lasers/'+laser_id)
                        quality_document=record['document'];quality_read_at=now
                        state['quality_source_revision']=record['revision']
                    except OSError as error:
                        defer_read(error);continue
                from ldcon.agents.monitor_quality_binding import conditions as bind_quality
                rules=bind_quality(p['conditions'],quality_document)
            if detector.rules != rules:
                detector = Conditions(rules, detector.state)
            state['effective_conditions']=rules
            try:last_snapshot = (snapshot_fn or snapshot)(box)
            except OSError as error:
                defer_read(error);continue
            for name, sample in last_snapshot.get('metrics', {}).items():
                stamp = sample.get('acquired_unix_s')
                if sample.get('valid') and stamp is not None and stamp != last_stamps.get(name):
                    state['samples'] += 1; last_stamps[name] = stamp
            events = detector.observe(last_snapshot.get('metrics', {}), now)
            # An event queued behind a task must still describe a fresh,
            # unresolved condition when the next admission is considered.
            rules={r['id']:r for r in detector.rules}
            state['pending_events']=[e for e in state['pending_events'] if
                e['condition'] in rules and detector.state.get(e['condition'],{}).get('active')
                and detector.state[e['condition']].get('since') is not None
                and e['rule_revision']==digest(rules[e['condition']])
                and 0<=now-detector.state[e['condition']].get('last_sample',-float('inf'))<=rules[e['condition']]['max_age_s']]
            for event in events:
                if event['condition'] not in {e['condition'] for e in state['pending_events']}:
                    state['pending_events'].append(event)
            if not p['enabled']:
                from ldcon.agents.agent_background import revoke_monitor
                revoke_monitor(box.root,laser_id,meta['id'],state['generation'])
                if state.get('request_id') and key:store.cancel_queued(key,state['request_id'])
            if state['task_job_id']:
                try:job = _api(box, 'agent-tasks/'+state['task_job_id']) if not task_start else task_start(None, None, state)
                except OSError as error:
                    defer_read(error);continue
                task_outcome=job.get('task',{}).get('status')
                if task_outcome=='unknown':
                    publish('task_requires_inspection'); sleep(p['sample_interval_s']); continue
                if job.get('status') in ('succeeded', 'failed', 'cancelled', 'inconclusive'):
                    state['task_job_id'] = None
                else:
                    publish('waiting_task'); sleep(p['sample_interval_s']); continue
            if state['request_id']:
                row = store.request(key, state['request_id'])
                if row and row['status'] in ('completed', 'cancelled'):
                    state['request_id'] = None
                else:
                    publish('review_unresolved'); sleep(p['sample_interval_s']); continue
            key = store.key(meta, p['agent_card_id']) if p['agent_card_id'] else None
            if not p['enabled']:
                from ldcon.agents.agent_background import revoke_monitor
                revoke_monitor(box.root,laser_id,meta['id'],state['generation'])
                publish('disabled'); sleep(p['sample_interval_s']); continue
            if p['background'].get('controller_operation')=='servo.dlcpro_frequency' and not any(
                    j.get('status')=='running' and any(c.get('operation',c.get('card'))=='servo.dlcpro_frequency'
                    for c in j.get('cards',[])) for j in last_snapshot.get('jobs',[])):
                publish('waiting_controller'); sleep(p['sample_interval_s']); continue
            due = bool(p['interval_s'] and (state['last_review'] is None or now-state['last_review'] >= p['interval_s']))
            research = bool(p['history_task_card_id'] and p['history_interval_s'] and
                (state['last_history'] is None or now-state['last_history'] >= p['history_interval_s']) and
                state['samples'] >= p['history_min_new_samples'])
            if not (due or state['pending_events'] or research or state.get('pending_admission')):
                publish('watching'); sleep(p['sample_interval_s']); continue
            from ldcon.agents.operating_mode_client import observation as observe_mode
            state['operating_mode']=observe_mode(laser_document)
            prompt = ('Analyze historical correlations, normal and failed periods, and propose methods with counterevidence and validation plans. '
                      if research else 'Assess the recorded control health and relevant history. ')
            prompt += ('Use admitted background tools only when an enabled monitor grant and a healthy idle lock allow it. '
                       if p['background']['enabled'] and not research else 'This review authorizes no physical operation. ')
            prompt += 'Do not restart intentionally stopped locks. '+p['prompt']+'\nConditions: '+json.dumps(state['pending_events'])
            prompt += '\nOwner operating mode at review: '+json.dumps(state['operating_mode'])
            prompt += '\nRead the current owner mode before deciding; an unavailable mode grants no autonomous adjustment.'
            card_id = p['history_task_card_id'] if research else p['task_card_id']
            consumed=copy.deepcopy(state['pending_events'])
            if card_id or state.get('pending_admission'):
                # A lost HTTP response must retry the exact request, even when
                # more conditions arrive or configuration changes meanwhile.
                admission=state.get('pending_admission')
                if not admission:
                    admission=dict(card_id=card_id,prompt=prompt,research=research,created_unix_s=now,
                        events=copy.deepcopy(state['pending_events']),payload=dict(source_job_id=meta['id'],
                        card_id=card_id,prompt=prompt,request_id='review-'+str(state['sequence']+1)))
                    if not research:admission['payload']['lookback_s']=p['lookback_s']
                    state['pending_admission']=admission
                publish('admitting_task')
                try:response = start(admission)
                except OSError:
                    publish('admission_unconfirmed'); sleep(p['sample_interval_s']); continue
                if response.get('status')=='rejected':
                    raise ValueError('Task admission rejected; inspect its configuration: '+str(response.get('reason','')))
                if not response.get('id'):
                    publish('task_unavailable'); sleep(p['sample_interval_s']); continue
                state['task_job_id'] = response['id']
                research=admission['research']
                consumed=admission['events']
                state.pop('pending_admission',None)
            elif key:
                request_id = 'monitor:'+meta['id']+':'+str(state['sequence']+1)
                queued = store.enqueue_when_ready(key, request_id, dict(kind='monitor_review',
                    job=meta['id'], laser_id=laser_id, prompt=prompt, events=state['pending_events'],
                    window=dict(since_unix_s=now-p['lookback_s'], until_unix_s=now)))
                if not queued:
                    publish('waiting_resident'); sleep(p['sample_interval_s']); continue
                state['request_id'] = request_id
            else:
                publish('unconfigured'); sleep(p['sample_interval_s']); continue
            consumed_ids={e['condition'] for e in consumed}
            state.update(sequence=state['sequence']+1, last_review=now,
                         pending_events=[e for e in state['pending_events'] if e['condition'] not in consumed_ids])
            if research:state.update(last_history=now, samples=0)
            publish('review_started')
            emit('agent_monitor_review', dict(sequence=state['sequence'], research=research,
                                            task_job_id=state['task_job_id'], request_id=state['request_id']))
            sleep(p['sample_interval_s'])
        raise CardCancelled('Agent monitor stopped; deterministic lock remains independent')
    finally:
        if state.get('request_id') and key:
            store.cancel_queued(key, state['request_id'])
        from ldcon.agents.agent_background import revoke_monitor
        revoke_monitor(box.root, laser_id, meta['id'], state['generation'])
        if state.get('task_job_id') and task_start is None:
            try:_api(box,'jobs/'+state['task_job_id']+'/stop','POST',{})
            except (OSError,ValueError):state['child_stop']='unconfirmed; grant revoked, owner restores independently'
        publish('stopped_restoration_requested')
