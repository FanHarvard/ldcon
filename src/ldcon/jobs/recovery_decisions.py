"""Pinned task-model preparation over the shared analysis-only decision transport.

The task proposes route/reference parameters; the deterministic recovery executor
owns validation, physical qualification and receipts. A proposal never proves a
hop or authorizes a wider current excursion. No resident model rewrites reports.
"""
import copy
from pathlib import Path

from ldcon.core.records import atomic_json, read_json
from ldcon.jobs.recovery_procedure import apply_proposal

PROPOSABLE = ('reference', 'fallback', 'specializations', 'tolerance_hz', 'stable_s', 'confirm_samples')
ACTIONS = dict(propose_parameters=dict(type='object', additionalProperties=False,
    required=['parameters', 'reason'], properties=dict(
        parameters=dict(type='object', additionalProperties=False,
                        properties={name: {} for name in PROPOSABLE}),
        reason=dict(type='string', minLength=1, maxLength=8000))),
    inconclusive=dict(type='object', additionalProperties=False, required=['reason'],
                      properties=dict(reason=dict(type='string', minLength=1, maxLength=8000))))
OBJECTIVE = '''Propose parameters for the one relative-current-route recovery algorithm.
Use the full recorded pre-hop history, timestamps, frequency scatter/drift and output context.
Infer a stable return frequency independently of branch labels and optical-quality thresholds.
Keep a general fallback; specialize the same algorithm only when return-frequency and TEC/PZT
context distinguish evidence. Ambiguous context must use fallback. Do not treat historical
absolute branch voltages as universal defaults. Direction and excursion are evidence-based:
start with small trials when supported; an observed departure AND fresh sustained return is
required before qualification. A no-hop trial is inconclusive, not a recovery success.
You may propose reference criteria, fallback route, specializations, return tolerance, stable
confirmation duration and count. The host validates all values and confines every current
route to the admitted offset corridor. Do not alter perturbations, deadlines, target epochs,
output bounds, gains, calibration, or tools. Never claim a route verified, supply fabricated
owner evidence, or loosen criteria merely to pass. Empty perturbations mean proposal only.
Return propose_parameters with parameters and reason, or inconclusive. No physical actions,
configuration changes, additional cards, notifications or agent spawning. Use English.'''


def execution_card(card):
    """Default task difficulty is explicit, independent of gateway model/effort."""
    return {**card, 'agent_execution': copy.deepcopy(card.get('agent_execution',
        dict(mode='required', difficulty='standard')))}


def evidence(p):
    metadata = {key: copy.deepcopy(value) for key, value in p.items() if key != 'history'}
    chunks = []
    for start in range(0, len(p['history']), 80):
        chunks.append(('history-%d' % start, dict(first_index=start, samples=p['history'][start:start+80])))
    metadata['history_files'] = [name+'.json' for name, _ in chunks]
    metadata['sample_count'] = len(p['history'])
    return [('parameters', metadata), *chunks]


def _admission_matches(admitted, p):
    """Hydrated history is evidence; every admitted execution setting stays fixed."""
    from ldcon.jobs.ecdl_recovery import parameters
    original = parameters({'parameters': admitted}, {})
    dynamic = {'history', 'event_time_s', 'operation_epoch'} if not original['history'] else set()
    return all(original[key] == p[key] for key in original if key not in dynamic)


def run_card(directory, laser, card, ports, p, *, clock, sleep, cancel, emit,
             runtime=None, decision_provider=None, resident_selection=None,
             publish=lambda _: None):
    from ldcon.jobs.ecdl_recovery import parameters, run_recovery_card
    from ldcon.jobs.recovery_procedure import require_configured
    p = require_configured(parameters({'parameters': p}, {}))
    if p['execution_mode'] == 'deterministic':
        return run_recovery_card(ports, p, runtime=runtime, clock=clock, sleep=sleep,
                                 cancel=cancel, emit=emit)
    from ldcon.jobs.study_execution import decision_parent, evaluate
    from ldcon.web.agent_task_api import admission_path
    directory = Path(directory)
    selected = resident_selection or decision_parent(directory, p['agent_card_id'])
    store, meta = selected[:2]
    admission = read_json(admission_path(store.root.parent, meta['id']), {})
    if admission:
        if (admission.get('card_id') != card['id'] or admission.get('laser_id') != laser['id']
                or not _admission_matches(admission.get('parameters', {}), p)):
            raise ValueError('Recovery preparation differs from its frozen task admission')
        profile = admission['profile']
    else:
        from ldcon.agents.agent_profiles import select_task
        profile, _ = select_task(store.root.parent, execution_card(card), p, meta=meta)
    if not profile:
        raise ValueError('Recovery preparation requires an admitted task execution profile')
    atomic_json(directory/'recovery-task-evidence.json', dict(parameters=p, profile=profile))
    child = None
    result = dict(status='failed', output_writes=0, recovery_verified=False, profile=profile)
    stage = ['analysis']
    try:
        if decision_provider is None:
            from ldcon.agents.task_decisions import TaskDecisions
            child = TaskDecisions(directory, selected, profile, clock=clock, cancel=cancel,
                                  deadline=clock()+min(profile['timeout_s'], p['agent_timeout_s']+p['timeout_s']))
            child.connect()
            decision_provider = child

        def decide(context):
            proposal = evaluate(directory, selected, laser_id=laser['id'], documents=evidence(context['parameters']),
                objective=OBJECTIVE, actions=ACTIONS, timeout_s=p['agent_timeout_s'],
                clock=clock, sleep=sleep, cancel=cancel, publish=publish, provider=decision_provider)
            if proposal['action'] == 'inconclusive':
                raise ValueError('Recovery parameter analysis inconclusive: '+proposal['parameters']['reason'])
            answer = proposal['parameters']
            apply_proposal(p, answer)  # Domain validation before any owner access.
            atomic_json(directory/'recovery-parameter-proposal.json', dict(
                proposal=answer, profile=profile, validated=True, applied=False, verified=False))
            stage[0] = 'execution'
            return answer

        result = run_recovery_card(ports, p, runtime=runtime, clock=clock, sleep=sleep,
                                   cancel=cancel, emit=emit, decide=decide)
        result['execution_profile'] = profile
        result['profile'] = profile
        atomic_json(directory/'recovery.json', result['artifact'])
        return result
    except Exception as error:
        result.update(status='failed', error_type=type(error).__name__, message=str(error))
        # The runner's detailed receipts, not this classification, establish effects.
        result['output_writes'] = 0 if stage[0]=='analysis' else 'inspect_executor_receipts'
        raise
    finally:
        try:
            if child:
                result['explanation'] = child.explain(result)
                from ldcon.agents.agent_task import _parent_result
                _parent_result(store, meta, p, directory, {**result, 'report_author':'task_model'})
            atomic_json(directory/'recovery-preparation-result.json', result)
        finally:
            if child:child.close()
