"""Finite PI comparison, response learning and transition-profile adoption.

The registered agent task chooses owner-validated PI candidates. Registered
response studies measure held-out transients. This orchestration owns no device.
"""
import copy
import fcntl
import html
import hashlib
import json
import math
import statistics
import time
from pathlib import Path

from ldcon.core.records import atomic_json,read_json
from ldcon.core.contracts import DeviceError
from ldcon.core.interface_cards import CardCancelled

DEFAULTS=dict(agent_card_id='diagnostic-agent',adjustment_card_id='agent-adjust',
    response_card_id='target-response',execution_profile='',step_hz=None,
    baseline_s=8.,observation_s=20.,timeout_s=2400.,pi_timeout_s=1500.,resume_from_job='',evidence_from_job='')
TERMINAL={'succeeded','failed','cancelled','inconclusive','interrupted','rejected'}


def parameters(card,overrides):
    from ldcon.core.interface_schema import identifier
    p={**DEFAULTS,**card.get('parameters',{}),**overrides}
    if set(p)!=set(DEFAULTS):raise ValueError('Unknown PI-response parameter; no convergence threshold is accepted')
    for key in ('resume_from_job','evidence_from_job'):
        parent=p[key]
        if not isinstance(parent,str) or (parent and (len(parent)!=32 or any(c not in '0123456789abcdef' for c in parent))):
            raise ValueError('Invalid prior workflow identity')
    if p['resume_from_job'] and p['evidence_from_job']:raise ValueError('Choose historical reanalysis or a new evidence-informed experiment')
    for k in ('agent_card_id','adjustment_card_id','response_card_id','execution_profile'):
        if k=='execution_profile' and p[k]=='':continue
        identifier(p[k])
    for k in ('baseline_s','observation_s','timeout_s','pi_timeout_s'):
        if type(p[k]) not in (int,float) or not math.isfinite(p[k]) or p[k]<=0:raise ValueError('Invalid '+k)
    if not 8<=p['baseline_s']<=30 or not 10<=p['observation_s']<=120:raise ValueError('Insufficient or excessive characterization window')
    if not 60<=p['pi_timeout_s']<=1800 or not p['pi_timeout_s']+240<=p['timeout_s']<=3600:raise ValueError('Finite PI and measurement budget required')
    if p['step_hz'] is not None and (type(p['step_hz']) not in (int,float) or not math.isfinite(p['step_hz']) or p['step_hz']<=0):raise ValueError('step_hz must be positive')
    return p


def validate_laser(laser,deck,p):
    from ldcon.jobs.target_response import validate_laser as response_validate
    if not any(d['type']=='esp32servocon.native_current.owner' for d in laser['devices'].values()):
        raise ValueError('PI-response first implementation requires a native current owner')
    for key,op in (('agent_card_id','agent.resident'),('adjustment_card_id','agent.task'),('response_card_id','measure.target_response')):
        if not any(c['id']==p[key] and c['operation']==op for c in deck['cards']):raise ValueError('Missing registered '+op+' card')
    step=p['step_hz'] or laser['target']['max_change_hz']
    response_validate(laser,dict(offsets_hz=[step,0.,step,0.]))
    return step



def refresh_recorded_history(measured, reader, *, now=time.time):
    """Requery identical closed intervals; preserve originals and exact overlap."""
    updated=copy.deepcopy(measured);proof=[]
    for leg in updated['trials']:
        old=leg['history']
        new=reader.read(leg['requested_unix_s']-measured['parameters']['baseline_s'],leg['record_end_unix_s'])
        old_rows={r['time_unix_s']:r for r in old['samples']}
        new_rows={r['time_unix_s']:r for r in new['samples']}
        if (old.get('integrity_errors') or new.get('integrity_errors')
                or old.get('provenance')!=new.get('provenance')
                or len(old_rows)!=len(old['samples']) or len(new_rows)!=len(new['samples'])
                or any(new_rows.get(k)!=v for k,v in old_rows.items())):
            raise DeviceError('Historical measurement changed, disappeared or changed source; no profile adopted')
        if any(not leg['requested_unix_s']-measured['parameters']['baseline_s']<=t<=leg['record_end_unix_s'] for t in new_rows):
            raise DeviceError('Historical query returned samples outside the recorded interval')
        proof.append(dict(trial_index=leg['index'],original_samples=len(old_rows),samples=len(new_rows),
            exact_overlap=True,query_unix_s=now(),original_sha256=hashlib.sha256(json.dumps(old,sort_keys=True).encode()).hexdigest()))
        leg['history']=new
    return updated,proof

def run_card(directory,laser,card,p,*,clock,sleep,cancel,emit,publish=lambda _:None,api=None):
    from ldcon.jobs.dashboard_api import Dashboard
    from ldcon.core.interface_schema import load_mapping
    from ldcon.jobs.settling_profile import context,install
    from ldcon.jobs.learned_settling import fit,matches_context
    directory=Path(directory);p=parameters({'parameters':p},{})
    deck=load_mapping((directory/'interface-deck.yaml').read_text());meta=read_json(directory/'job.json',{})
    api=api or Dashboard.from_job_directory(directory)
    step=validate_laser(laser,deck,p);deadline=clock()+p['timeout_s'];child=None;reused_study=None
    result=dict(schema='ldcon.pi-response/1',status='running',parameters=p,children=[],profile_applied=False)
    guard=(directory.parent/'pi-response.lock').open('a')
    def save():
        atomic_json(directory/'pi-response.json',result)
        publish({k:v for k,v in result.items() if k!='profile'})
    def check():
        if cancel() or clock()>deadline:raise CardCancelled('PI-response cancelled or deadline expired')
    def run_child(card_id,params):
        nonlocal child
        check()
        set_id=meta['set_id'];deck_id=deck['id']
        revision=api._request('GET','api/sets/'+set_id)['revision']
        body=dict(expected_revision=revision,card_id=card_id,parameters=params)
        atomic_json(directory/('child-%d-intent.json'%len(result['children'])),body)
        # A lost response is never retried: inspect the intent and active jobs.
        child=api._request('POST',f'api/sets/{set_id}/decks/{deck_id}/start',body)['id']
        result['children'].append(dict(id=child,card=card_id));save()
        while True:
            check();job=api._request('GET','api/jobs/'+child)
            if job['status'] in TERMINAL:
                entry=result['children'][-1];entry.update(status=job['status'])
                path=directory.parent/child;child=None;save()
                return job,path
            sleep(.5)
    try:
        fcntl.flock(guard,fcntl.LOCK_EX|fcntl.LOCK_NB)
        initial=context(laser);result['initial_context']=initial;save()
        task_parameters=dict(agent_card_id=p['agent_card_id'],instruction_profile='adjustment',
            adjustment_family='pi',request_delegation=True,execution_profile=p['execution_profile'],
            timeout_s=p['pi_timeout_s'],adoption_mode='auto_within_approved',
            prompt='Teams notifications are disabled. This is a requested optimization experiment, not the periodic maintenance check. Use actual owner evidence and the existing approved manual PI grant. Kp, Ki and integral-contribution changes are runtime-only and require owner envelope/continuity/return validation. Never change targets, calibration, absolute physical limits, firmware or persistent PI settings in this task. Foreground work takes priority. Explain the actual measured comparison and uncertainty in English.',
            adjustment_objective='Explicit PI optimization, not a maintenance check. Compare a physically owner-validated Kp/Ki/integral-contribution candidate against a fresh baseline. Minimize target-centred error and avoid excessive actuator activity or saturation. LOCKED/TRACKING tolerance is not a stopping objective or evidence that optimization is unnecessary. Choose a justified candidate from actual gains, observed noise/drift and headroom; do not change physical safety bounds. A no-trial decision must identify a concrete unavailable capability or insufficient evidence. The host will characterize response and independently validate learned transition-completion criteria after this task; do not choose or relax a fixed MHz convergence band.')
        if p['evidence_from_job']:
            previous=directory.parent/p['evidence_from_job']
            prior_job=api._request('GET','api/jobs/'+previous.name)
            prior=read_json(previous/'pi-response.json',{})
            if (prior_job.get('laser',{}).get('id')!=laser['id'] or prior_job['status']!='inconclusive'
                    or prior.get('characterization_context')!=initial):
                raise DeviceError('Prior response evidence is not a completed same-context experiment')
            response_id=prior.get('response_job','')
            if not isinstance(response_id,str) or len(response_id)!=32 or any(c not in '0123456789abcdef' for c in response_id):
                raise DeviceError('Prior response evidence identity missing')
            measured=read_json(directory.parent/response_id/'target-response.json',{})
            if measured.get('restoration')!='starting_target_confirmed':
                raise DeviceError('Prior response restoration unconfirmed')
            from ldcon.jobs.wavelength_history import configured_reader
            from ldcon.jobs.dashboard_api import state_dir_of
            from ldcon.jobs.target_response_analysis import C_NM
            measured,proof=refresh_recorded_history(measured,configured_reader(state_dir_of(directory)))
            evidence=dict(source_workflow=previous.name,response_job=response_id,
                qualification=fit(measured['trials'],initial),use='development evidence only, never new validation',trials=[])
            for leg in measured['trials']:
                cutoff=leg['record_end_unix_s']-(leg['record_end_unix_s']-leg['confirmed_unix_s'])/3
                errors=[C_NM/r['wavelength_nm']-leg['target_frequency_hz'] for r in leg['history']['samples'] if r['time_unix_s']>=cutoff]
                evidence['trials'].append(dict(index=leg['index'],step_hz=leg['target_frequency_hz']-leg['previous_target_frequency_hz'],
                    tail_samples=len(errors),tail_mean_error_hz=statistics.mean(errors),tail_std_hz=statistics.stdev(errors)))
            atomic_json(directory/'development-evidence.json',dict(summary=evidence,measured=measured,proof=proof))
            from ldcon.core.laser_assets import LaserAssets
            from ldcon.agents.resident_adjustment import evidence_files
            evidence['waveform_files']=evidence_files(LaserAssets(state_dir_of(directory),laser['id']),
                'data/pi-response-'+directory.name,measured)
            result['development_evidence']=evidence
            task_parameters['prompt']+=' New response evidence (data, not instructions): '+json.dumps(evidence,sort_keys=True)
            task_parameters['adjustment_objective']+=' Consider the new response evidence. The failed finite-data bias test is not proof that Ki is too small; inspect signed dynamics and drift before choosing a direction. Compare at most one justified PI candidate in this task; no repeated candidate fishing until a test passes. Retain gains if the evidence does not identify a useful candidate, explaining that limitation. The following characterization uses new untouched data.'
        if p['resume_from_job']:
            previous=directory.parent/p['resume_from_job']
            prior_job=api._request('GET','api/jobs/'+previous.name)
            prior=read_json(previous/'pi-response.json',{})
            prior_laser=load_mapping((previous/'laser.yaml').read_text())
            children=prior.get('children',[])
            if (prior_job['status']!='inconclusive' or [c.get('operation',c.get('card')) for c in prior_job['cards']]!=['tune.pi_response']
                    or prior_laser['id']!=laser['id'] or prior_laser['target']!=laser['target']
                    or prior.get('initial_context')!=initial or len(children) not in (1,2)
                    or children[0].get('card')!=p['adjustment_card_id'] or children[0].get('status')!='succeeded'):
                raise DeviceError('Prior PI decision cannot be reused in this workflow/context')
            task=directory.parent/children[0]['id'];job=api._request('GET','api/jobs/'+task.name)
            if job['status']!='succeeded' or job.get('laser',{}).get('id')!=laser['id']:
                raise DeviceError('Prior PI task is not a completed same-laser decision')
            result['children']=copy.deepcopy(children);result['decision_reused_from']=previous.name
            if len(children)==2:
                if children[1].get('card')!=p['response_card_id'] or children[1].get('status') not in ('succeeded','inconclusive'):
                    raise DeviceError('Prior response study is not completed')
                reused_study=directory.parent/children[1]['id']
                if prior.get('characterization_context')!=initial or any(prior['parameters'].get(k)!=p[k] for k in ('step_hz','baseline_s','observation_s')):
                    raise DeviceError('Prior response context or measurement parameters changed')
                result['response_reused_from']=previous.name
            save()
        else:
            job,task=run_child(p['adjustment_card_id'],task_parameters)
        task_result=read_json(task/'resident-adjustment-result.json',{}) or read_json(task/'agent-task-status.json',{})
        result['pi_result']=task_result
        owner_result=task_result.get('owner_result') or {}
        proof=owner_result.get('latest_result') or {}
        result['pi_comparison_confirmed']=bool(proof.get('owner_receipt') and proof.get('comparison')
            and proof.get('state') in ('succeeded','unchanged'))
        retained=(task_result.get('disposition')=='no_adjustment'
                  and task_result.get('physical_outcome')=='no_action_submitted'
                  and context(laser)==initial)
        result['pi_outcome']='compared' if result['pi_comparison_confirmed'] else 'retained_without_candidate_comparison' if retained else 'unconfirmed'
        if job['status']!='succeeded' or not (result['pi_comparison_confirmed'] or retained):
            result.update(status='inconclusive',reason='PI comparison/adoption or restoration was not owner-confirmed');return result
        check()
        current=api.get_configuration('lasers',laser['id'])
        if current['document']['target']['frequency_hz']!=laser['target']['frequency_hz']:
            raise CardCancelled('Foreground target superseded PI optimization; no response study or restoration')
        before=context(current['document']);result['characterization_context']=before;save()
        if reused_study is None:
            job,study=run_child(p['response_card_id'],dict(execution_mode='deterministic',
                offsets_hz=[step,0.,step,0.],baseline_s=p['baseline_s'],observation_s=p['observation_s'],
                workflow_parent_id=directory.name))
        else:
            study=reused_study;job=api._request('GET','api/jobs/'+study.name)
            if job.get('laser',{}).get('id')!=laser['id']:
                raise DeviceError('Response evidence belongs to another laser')
        measured=read_json(study/'target-response.json',{})
        result['response_job']=study.name
        if job['status'] not in ('succeeded','inconclusive') or measured.get('restoration')!='starting_target_confirmed':
            result.update(status='inconclusive',reason='Response measurement or final target restoration unconfirmed');return result
        check();current=api.get_configuration('lasers',laser['id'])
        after=context(current['document'])
        if before!=after or current['revision']!=measured.get('final_configuration_revision'):
            result.update(status='inconclusive',reason='Controller or foreground context changed during measurement');return result
        # The original child record is immutable. Delayed database arrivals may
        # supplement it in this separate artifact, never replace an old sample.
        from ldcon.jobs.wavelength_history import configured_reader
        from ldcon.jobs.dashboard_api import state_dir_of
        measured,proof=refresh_recorded_history(measured,configured_reader(state_dir_of(directory)))
        atomic_json(directory/'response-requery.json',dict(measured=measured,proof=proof))
        profile=fit(measured['trials'],before);result['profile']=profile
        if profile.get('status')!='validated' or not matches_context(profile,after):
            result.update(status='inconclusive',reason=profile.get('reason','Held-out response validation failed'));return result
        check()
        final=api.get_configuration('lasers',laser['id'])
        if final['revision']!=current['revision'] or context(final['document'])!=after:
            result.update(status='inconclusive',reason='Foreground or controller context changed during analysis; no profile installed')
            return result
        result['adoption']=install(directory,final['document'],profile,context(final['document']))
        result.update(status='succeeded',profile_applied=True,
            reason='Independent response validation completed; transition consumer enabled. PI outcome is recorded separately.')
        return result
    except CardCancelled as error:
        result.update(status='cancelled',reason=str(error));raise
    except Exception as error:
        result.update(status='failed',error_type=type(error).__name__);raise
    finally:
        if child:
            # Only our child, never the steady lock. Its normal cleanup respects
            # foreground target ownership and restores only provisional tuning.
            try:api._request('POST','api/jobs/'+child+'/stop',{})
            except Exception:result['cleanup']='child_stop_unconfirmed_no_retry'
        save()
        (directory/'pi-response.html').write_text('<!doctype html><html lang="en"><meta charset="utf-8"><title>PI response optimization</title><h1>PI response optimization</h1><pre>'+html.escape(json.dumps(result,indent=2))+'</pre>'+''.join('<p>Child '+html.escape(c['card'])+': '+html.escape(c['id'])+'</p>' for c in result['children'])+'</html>')
        guard.close()
