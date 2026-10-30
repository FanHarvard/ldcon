"""Explicit study adoption: verified next-run criteria, separate from actuators."""
import copy
import time
import yaml

from experiment_core.records import atomic_json, digest, safe_id
from ldcon.core.records import read_json
from ldcon.jobs.response_algorithms import ALGORITHMS


def submit(directory, result, api):
    mode = result['parameters'].get('adoption_mode', 'report_only')
    proposal = result.get('optimization') or {}
    from experiment_core.agent_roles import adoption_disposition
    disposition=adoption_disposition(mode,verified=True,within_approved_scope=True)
    if disposition!='eligible_for_host_adoption':
        return dict(status=disposition,applied=False,mode=mode)
    # This evidence is host-owned and the endpoint reconstructs its evaluation.
    atomic_json(directory/'target-response.json', result)
    try:
        return api._request('POST', 'api/target-response/'+directory.name+'/adopt',
                            dict(result_revision=digest(result)))
    except Exception as error:
        return dict(status='unconfirmed_no_retry', applied=False, reason=str(error))


def apply(runtime, job_id, value):
    from ldcon.jobs.target_response_decisions import validate_proposal, FIELDS
    from ldcon.core.interface_schema import load_mapping
    from ldcon.core import configuration
    if not isinstance(value, dict) or set(value) != {'result_revision'}:
        raise ValueError('Expected exact host study-result revision')
    with runtime.mutex:
        directory = runtime.directory(safe_id(job_id))
        path = directory/'study-adoption.json'
        old = read_json(path, {})
        if old:
            if old['request'] != value:
                raise ValueError('Study adoption is already bound to another result')
            return old['result']
        job = runtime.get_job(job_id)
        invocation = read_json(directory/'invocation.json')
        result = read_json(directory/'target-response.json')
        if (job['status'] != 'running' or (directory/'cancel.request').exists() or
                digest(result) != value['result_revision'] or
                result['parameters'].get('adoption_mode') != 'auto_within_approved' or
                invocation['parameters'].get('adoption_mode') != 'auto_within_approved' or
                result.get('measurement_status') != 'succeeded' or
                result.get('restoration') != 'starting_target_confirmed'):
            raise ValueError('Active verified automatic study adoption required')
        if not any(c.get('operation', c.get('card')) in ALGORITHMS for c in job['cards']):
            raise ValueError('Not a response-study job')
        decision = read_json(directory/'study-decision.json')['proposal']
        candidate = validate_proposal(result, decision)
        if candidate['status'] != 'candidate_only' or not candidate['all_trials_settled']:
            raise ValueError('Original full-rate trials do not validate the candidate')
        proposed = candidate['parameters']
        baseline = result['parameters']
        # A model cannot redefine the precision requirement merely to pass.
        for key in ('mean_error_max_hz', 'rms_error_max_hz', 'peak_error_max_hz'):
            if baseline[key] is not None and (proposed[key] is None or proposed[key] > baseline[key]):
                return dict(status='requires_human_approval', applied=False,
                    reason='Candidate weakens the study precision requirement', field=key)
        laser = runtime.interface_repository.read('lasers', result['laser_id'])
        if laser['revision'] != result.get('final_configuration_revision'):
            raise ValueError('Configuration/target changed after study; candidate is obsolete')
        assignment = runtime.interface_repository.read('assignments', job['set_id'])
        frozen = load_mapping((directory/'set.yaml').read_text())
        if assignment['document'] != frozen:
            raise ValueError('Assignment changed while studying; reassess next-run defaults')
        document = copy.deepcopy(assignment['document'])
        deck = next(d for d in document['decks'] if d.get('deck_id', d.get('id')) == job['deck_id'])
        params = deck.setdefault('parameters', {}).setdefault(invocation['card_id'], {})
        params.update({k:proposed[k] for k in FIELDS})
        # target-ff design (2026-10-27): the per-step cap / shift promotion is saved by the card itself through the
        # revision-checked configuration API (FREE keys, audit rows); this host receipt only reports that outcome.
        shift = result.get('shift') or {}
        receipt = dict(status='dispatching', applied=False,
            scope='next_response_study_defaults_only', physical_settings_changed=False,
            feedforward='requires_separate_persistent_adoption_authority' if candidate.get('feedforward') else 'not_requested',
            shift=dict(status=(shift.get('adoption') or {}).get('status'), adopted_hz=shift.get('adopted_hz'),
                       best_passing_hz=shift.get('best_passing_hz'), audit=shift.get('audit'),
                       basis='card_configuration_api') if shift else 'not_requested')
        record = dict(request=value, result=receipt, proposal=candidate, before=assignment['document'], after=document, created_unix_s=time.time())
        atomic_json(path, record)
        saved = configuration.save(runtime, 'assignments', job['set_id'],
            yaml.safe_dump(document, sort_keys=False), assignment['revision'], actor='study-adoption:'+job_id, study_criteria_binding=dict(job_id=job_id,result_revision=value['result_revision']))
        actual = runtime.interface_repository.read('assignments', job['set_id'])
        confirmed = actual['document'] == document and actual['revision'] == saved['revision']
        receipt.update(status='applied' if confirmed else 'saved_unconfirmed', applied=confirmed,
                       revision=saved['revision'], fields=list(FIELDS))
        atomic_json(path, {**record, 'result':receipt})
        return receipt


def authorize_criteria(runtime, kind, name, before, after, rows, binding):
    """Internal host binding only; no HTTP accepts this authorization argument."""
    from ldcon.jobs.target_response_decisions import validate_proposal, FIELDS
    if set(binding)!={'job_id','result_revision'} or kind!='assignments':
        raise ValueError('Invalid study-criteria adoption binding')
    job=runtime.get_job(safe_id(binding['job_id']));directory=runtime.directory(job['id'])
    record=read_json(directory/'study-adoption.json',{})
    result=read_json(directory/'target-response.json',{})
    invocation=read_json(directory/'invocation.json',{})
    if (job['status']!='running' or job['set_id']!=name or (directory/'cancel.request').exists()
        or record.get('result',{}).get('status')!='dispatching'
        or record.get('request',{}).get('result_revision')!=binding['result_revision']
        or digest(result)!=binding['result_revision'] or record.get('before')!=before or record.get('after')!=after
        or invocation.get('parameters',{}).get('adoption_mode')!='auto_within_approved'):
        raise ValueError('Study authorization does not match the active frozen transaction')
    candidate=validate_proposal(result,read_json(directory/'study-decision.json')['proposal'])
    if candidate['status']!='candidate_only' or not candidate['all_trials_settled']:
        raise ValueError('Unverified study criteria')
    for key in ('mean_error_max_hz','rms_error_max_hz','peak_error_max_hz'):
        old=result['parameters'][key];new=candidate['parameters'][key]
        if old is not None and (new is None or new>old):raise ValueError('Cannot weaken study precision')
    expected=copy.deepcopy(before)
    deck=next(d for d in expected['decks'] if d.get('deck_id',d.get('id'))==job['deck_id'])
    deck.setdefault('parameters',{}).setdefault(invocation['card_id'],{}).update(
        {k:candidate['parameters'][k] for k in FIELDS})
    if expected!=after:raise ValueError('Study authority covers only evaluated next-run criteria')
    return [{**row,'class':'free','authority_basis':'verified_next_study_criteria'} for row in rows]
