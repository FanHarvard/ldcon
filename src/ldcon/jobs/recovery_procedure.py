"""One branch-independent relative current-route recovery algorithm.

The target is inferred from stable pre-hop observations. Routes specialize only
parameters; optical quality is recorded separately from frequency return and PI
capture. Runtime never consults a branch map or executes archived v1 artifacts.
"""
import copy
import time

from ldcon.core.contracts import DeviceError
from ldcon.jobs.ecdl_mode_map import BoundedExperiment, seal, checked_artifact
from ldcon.jobs.recovery_reference import finite, infer, select, observation, parameters as reference_parameters

SCHEMA = 'ldcon.ecdl-recovery.v2'
ALGORITHM = 'relative_current_route.v1'
DEFAULTS = dict(reference=None, fallback=None, specializations=[],
                tolerance_hz=None, stable_s=None, confirm_samples=None, sample_interval_s=None,
                max_sample_age_s=None, measurement_timeout_s=None, timeout_s=None,
                hop_threshold_hz=None, history=[], event_time_s=None, operation_epoch=None,
                perturbations=[], execution=None, execution_mode=None,
                execution_profile='', agent_card_id='gateway-agent', agent_timeout_s=None)

REQUIRED_EXECUTION = frozenset(('tolerance_hz', 'stable_s', 'confirm_samples',
    'sample_interval_s', 'max_sample_age_s', 'measurement_timeout_s', 'timeout_s',
    'hop_threshold_hz', 'execution', 'execution_mode'))


def validate_route(route):
    if not isinstance(route, list) or not 1 <= len(route) <= 1000:
        raise ValueError('An explicit bounded relative-current route is required')
    for step in route:
        if (not isinstance(step, dict) or set(step) != {'offset', 'dwell_s'}
                or not finite(step['offset']) or not finite(step['dwell_s']) or step['dwell_s'] < 0):
            raise ValueError('Route points require current-axis offset and nonnegative dwell_s')
    return copy.deepcopy(route)


def parameters(card, overrides, *, require_configured=False):
    base = card.get('parameters', {})
    if not isinstance(base, dict) or not isinstance(overrides, dict):
        raise ValueError('Recovery parameters must be mappings')
    obsolete = {'classes', 'branch_match', 'map_ref', 'map_artifact', 'recovery_ref', 'recovery_artifact',
                'home_tolerance_hz', 'success_tolerance_hz', 'context_mode', 'recipes', 'procedure'}
    if (set(base)|set(overrides)) & obsolete:
        raise ValueError('Legacy recovery policy retired; migrate explicitly to relative_current_route.v1')
    if (set(base)|set(overrides))-set(DEFAULTS):
        raise ValueError('Unknown recovery parameter')
    p = copy.deepcopy({**DEFAULTS, **base, **overrides})
    if p['reference'] is not None:p['reference'] = reference_parameters(p['reference'])
    elif require_configured:raise ValueError('Recovery reference criteria must be configured')
    if p['fallback'] is not None:p['fallback'] = validate_route(p['fallback'])
    elif require_configured:raise ValueError('Recovery fallback route must be configured')
    missing = sorted(key for key in REQUIRED_EXECUTION if p[key] is None)
    if require_configured and missing:
        raise ValueError('Recovery execution settings must be configured explicitly: '+', '.join(missing))
    for key in ('tolerance_hz', 'stable_s', 'sample_interval_s', 'max_sample_age_s',
                'measurement_timeout_s', 'timeout_s', 'hop_threshold_hz'):
        if p[key] is None and not require_configured:continue
        if not finite(p[key]) or p[key] <= 0:
            raise ValueError('Explicit positive recovery parameter required: '+key)
    if p['tolerance_hz'] is not None and p['hop_threshold_hz'] is not None and p['tolerance_hz'] >= p['hop_threshold_hz']:
        raise ValueError('Recovery return tolerance must be narrower than the observed hop threshold')
    if (p['timeout_s'] is not None and p['timeout_s'] > 86400
            or p['stable_s'] is not None and p['timeout_s'] is not None and p['stable_s'] >= p['timeout_s']):
        raise ValueError('Invalid finite recovery deadline')
    if p['confirm_samples'] is not None and (type(p['confirm_samples']) is not int or not 3 <= p['confirm_samples'] <= 100000):
        raise ValueError('Recovery requires at least three confirmation samples')
    from ldcon.core.interface_schema import identifier
    if p['execution_mode'] is not None and p['execution_mode'] not in ('deterministic', 'agent'):
        raise ValueError('Recovery execution mode must be deterministic or agent')
    identifier(p['agent_card_id'])
    if p['execution_profile']:identifier(p['execution_profile'])
    if p['agent_timeout_s'] is not None and (not finite(p['agent_timeout_s']) or not 10 <= p['agent_timeout_s'] <= 900):
        raise ValueError('Recovery agent deadline must be 10..900 seconds')
    if require_configured and p['execution_mode'] == 'agent' and p['agent_timeout_s'] is None:
        raise ValueError('Recovery agent deadline must be configured explicitly')
    if p['execution_mode'] == 'agent' and p['execution'] != 'prepare':
        raise ValueError('Only recovery preparation accepts agent parameter proposals')
    if p['execution'] is not None and p['execution'] not in ('recover', 'prepare'):
        raise ValueError('Unknown recovery execution')
    if not isinstance(p['history'], list) or len(p['history']) > 100000:
        raise ValueError('Bounded recovery history required')
    if p['event_time_s'] is not None and not finite(p['event_time_s']):
        raise ValueError('Invalid recovery event time')
    if p['operation_epoch'] is not None and (not isinstance(p['operation_epoch'], str) or not p['operation_epoch']):
        raise ValueError('Invalid recovery epoch')
    if not isinstance(p['specializations'], list) or len(p['specializations']) > 100:
        raise ValueError('Invalid recovery specializations')
    ids = set()
    for item in p['specializations']:
        if (not isinstance(item, dict) or not {'id', 'frequency_range_hz', 'route'} <= set(item)
                or set(item)-{'id', 'frequency_range_hz', 'route', 'context', 'evidence'}):
            raise ValueError('Invalid recovery specialization')
        if not isinstance(item['id'], str) or not item['id'] or item['id'] in ids:
            raise ValueError('Unique specialization id required')
        ids.add(item['id'])
        contexts = {'frequency': item['frequency_range_hz'], **item.get('context', {})}
        if set(contexts)-{'frequency', 'current', 'temperature', 'piezo'}:
            raise ValueError('Unknown specialization context')
        for limits in contexts.values():
            if not isinstance(limits, list) or len(limits) != 2 or not all(map(finite, limits)) or limits[0] > limits[1]:
                raise ValueError('Invalid specialization interval')
        item['route'] = validate_route(item['route'])
    if not isinstance(p['perturbations'], list) or len(p['perturbations']) > 100:
        raise ValueError('Invalid perturbation trial list')
    for value in p['perturbations']:
        if not finite(value):
            raise ValueError('Perturbations use current-axis offsets')
    return p


def require_configured(p):
    return parameters({'parameters':p},{},require_configured=True)


def checked_recovery(value):
    artifact = checked_artifact(value, SCHEMA)
    if artifact.get('algorithm') != ALGORITHM:
        raise ValueError('Recovery algorithm differs')
    parameters({'parameters': artifact['parameters']}, {}, require_configured=True)
    return artifact


def policy_from_artifact(value):
    """Extract an explicit v2 execution policy, never old absolute branch context."""
    artifact = checked_recovery(value)
    p = copy.deepcopy(artifact['parameters'])
    p.update(execution='recover', execution_mode='deterministic', perturbations=[],
             history=[], event_time_s=None, operation_epoch=None)
    return require_configured(p)


def _reference(p):
    if p['event_time_s'] is None or p['operation_epoch'] is None:
        raise ValueError('Recovery requires a pre-hop history, event time and operation epoch')
    return infer(p['history'], p['event_time_s'], p['operation_epoch'], p['reference'])


def _destinations(experiment, reference, route):
    axis = experiment.policies['current']
    values = [reference['outputs']['current']+step['offset'] for step in route]
    if any(not axis.low <= value <= axis.high for value in values):
        raise DeviceError('Recovery route exceeds the current owner envelope')
    return values


class ReturnNotConfirmed(DeviceError):
    """The bounded route completed, but fresh frequency return was not observed."""


def verify(experiment, reference, p):
    """Fresh frequency return only. Quality and PI capture are separate facts."""
    start = None
    count = 0
    previous = None
    began = experiment.clock()
    while True:
        if experiment.clock()-began >= p['measurement_timeout_s']:
            raise ReturnNotConfirmed('Frequency did not return within the configured recovery verification window')
        row = experiment.read('recovery frequency verification')
        stamp = row['acquired_s']
        if previous is not None and stamp-previous > p['max_sample_age_s']:
            count = 0
            start = None
        previous = stamp
        if abs(row['frequency_hz']-reference['frequency_hz']) <= p['tolerance_hz']:
            start = stamp if start is None else start
            count += 1
            if count >= p['confirm_samples'] and stamp-start >= p['stable_s']:
                return row
        else:
            start = None
            count = 0
        experiment.wait(p['sample_interval_s'])


def execute(experiment, p, reference):
    """The same executor for standalone recovery and qualification trials."""
    route, selection = select(reference, p['fallback'], p['specializations'])
    values = _destinations(experiment, reference, route)  # validate complete path before any write
    experiment.emit('recovery_reference_fixed', dict(reference=reference, selection=selection,
                                                     algorithm=ALGORITHM))
    experiment.current_recovery(True)
    for step, value in zip(route, values):
        experiment.between_points()
        experiment.move('current', value, 'relative recovery route')
        experiment.wait(step['dwell_s'])
    final = verify(experiment, reference, p)
    experiment.current_recovery(False)
    return dict(status='succeeded', recovered=True, algorithm=ALGORITHM,
                reference=reference, selection=selection, final=final,
                pi_rearmed=False, quality=dict(single_mode=final.get('single_mode'), metrics=final.get('metrics', {})),
                physical_mode_identity_verified=False)


def apply_proposal(p, answer):
    """Validate task-model data within the admitted current route corridor.

    Model text cannot grant new output authority, alter target epochs, extend
    execution deadlines, or mark a proposal tested. Explanations stay attributed
    to the task model by the caller's immutable execution profile.
    """
    if (not isinstance(answer, dict) or set(answer) != {'parameters', 'reason'}
            or not isinstance(answer['parameters'], dict) or not isinstance(answer['reason'], str)
            or not answer['reason'].strip()):
        raise ValueError('Recovery proposal requires parameters and an evidence-based reason')
    allowed = {'reference', 'fallback', 'specializations', 'tolerance_hz', 'stable_s', 'confirm_samples'}
    if set(answer['parameters'])-allowed:
        raise ValueError('Recovery proposal cannot change execution authority or deadlines')
    proposed = parameters({'parameters': {**p, **answer['parameters']}}, {}, require_configured=True)
    corridor = [step['offset'] for route in [p['fallback']]+[s['route'] for s in p['specializations']] for step in route]
    for route in [proposed['fallback']]+[s['route'] for s in proposed['specializations']]:
        if any(not min(corridor) <= step['offset'] <= max(corridor) for step in route):
            raise ValueError('Recovery proposal expands the admitted current excursion; human approval required')
    # Evidence is generated by the runner, never accepted from a model claim.
    for item in proposed['specializations']:
        item.pop('evidence', None)
    return proposed


def capture_reference(experiment, p):
    """Read a fresh baseline for preparation; this performs no output changes."""
    history = []
    epoch = 'preparation:'+str(experiment.started)
    while True:
        row = observation(experiment.read('recovery preparation baseline'), experiment.clock())
        history.append({**row, 'operation_epoch':epoch})
        try:
            infer(history, experiment.clock(), epoch, p['reference'])
            return {**p, 'history':history, 'event_time_s':experiment.clock(), 'operation_epoch':epoch}
        except ValueError:
            experiment.wait(p['sample_interval_s'])


def run(ports, p, *, clock=time.monotonic, sleep=time.sleep, cancel=lambda: False,
        emit=lambda *args: None, runtime=None, decide=None, **unused):
    p = parameters({'parameters': p}, {}, require_configured=True)
    if p['execution_mode'] == 'agent' and decide is None:
        raise ValueError('Agent preparation requires the admitted task decision transport')
    experiment = None
    if p['execution'] == 'prepare' and not p['history']:
        experiment = BoundedExperiment(ports, p, clock=clock, sleep=sleep, cancel=cancel, emit=emit, runtime=runtime)
        p = capture_reference(experiment, p)
    if decide is not None:
        if p['execution'] != 'prepare':raise ValueError('Recovery execution never asks a model to rewrite the route')
        answer = decide(dict(kind='recovery_parameter_proposal', parameters=copy.deepcopy(p)))
        p = apply_proposal(p, answer)
        emit('recovery_parameter_proposal', dict(answer=answer, validated=True, verified=False))
    reference = _reference(p)  # failures happen before any port access
    if not 0 <= clock()-p['event_time_s'] <= p['reference']['lookback_s']:
        raise ValueError('Recovery event is stale or in the future on the owner monotonic clock')
    experiment = experiment or BoundedExperiment(ports, p, clock=clock, sleep=sleep, cancel=cancel, emit=emit, runtime=runtime)
    if p['execution'] == 'recover':
        return execute(experiment, p, reference)
    # Preparation can publish a proposal without a write. Tests require explicit
    # perturbations, already included in owner-path preflight before the first one.
    route, _ = select(reference, p['fallback'], p['specializations'])
    _destinations(experiment, reference, route)
    axis = experiment.policies['current']
    origin = reference['outputs']['current']
    if any(not axis.low <= origin+offset <= axis.high for offset in p['perturbations']):
        raise DeviceError('Recovery test perturbation exceeds owner envelope')
    trials = []
    initial = execute(experiment, p, reference) if p['perturbations'] else None
    for offset in p['perturbations']:
        before = experiment.read('before recovery qualification perturbation')
        experiment.current_recovery(True)
        experiment.move('current', origin+offset, 'explicit recovery qualification perturbation')
        samples = []
        for _ in range(p['confirm_samples']):
            samples.append(experiment.read('recovery qualification displaced observation'))
            experiment.wait(p['sample_interval_s'])
        observed = (abs(samples[0]['frequency_hz']-before['frequency_hz']) > p['hop_threshold_hz']
                    and all(abs(row['frequency_hz']-reference['frequency_hz']) > p['hop_threshold_hz'] for row in samples))
        result = execute(experiment, p, reference)
        trials.append(dict(offset=offset, displaced_samples=samples, hop_observed=observed,
                           recovery_verified=observed and result['recovered'], result=result))
    stored = {k: copy.deepcopy(v) for k, v in p.items() if k not in ('history', 'event_time_s', 'operation_epoch')}
    artifact = seal(dict(schema=SCHEMA, algorithm=ALGORITHM, parameters=stored,
                         reference=reference, initial=initial, trials=trials, commands=copy.deepcopy(experiment.commands),
                         verified=bool(trials) and all(t['recovery_verified'] for t in trials),
                         qualification='Only the recorded trials are evidence; no universal branch guarantee'))
    emit('ecdl_recovery_artifact', artifact)
    return dict(status='succeeded', artifact=artifact, recovery_verified=artifact['verified'],
                output_writes=len(experiment.commands))
