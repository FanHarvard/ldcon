"""Directed open-loop search for the first observed frequency discontinuity.

Stop moving at the first suspected hop, confirm with new independent optical
samples, and retain that output. Never recover, restart PI, or change the laser
target. The first observed branch is not proof of a nearest longitudinal mode.
"""
import copy
import math
from dataclasses import asdict

from ldcon.core.contracts import DeviceError
from ldcon.jobs.ecdl_mode_map import BoundedExperiment, finite, seal, compact_row

SCHEMA = 'ldcon.ecdl-branch-hop.v1'
DEFAULTS = dict(axis='current', direction='decrease', step_size=None,
                max_excursion=None, hop_threshold_hz=None, confirmation_span_hz=None,
                confirm_samples=3, confirm_s=1., confirmation_timeout_s=10.,
                dwell_s=.5, sample_interval_s=.5, max_sample_age_s=1.,
                measurement_timeout_s=30., timeout_s=300., max_steps=250)


def parameters(card, overrides):
    if not isinstance(card.get('parameters'), dict) or not isinstance(overrides, dict):
        raise ValueError('Branch-hop parameters must be mappings')
    p = copy.deepcopy({**DEFAULTS, **card['parameters'], **overrides})
    if set(p) - DEFAULTS.keys():
        raise ValueError('Unknown branch-hop parameter')
    if p['axis'] not in ('current', 'piezo') or p['direction'] not in ('increase', 'decrease'):
        raise ValueError('Select current/piezo and increase/decrease (actuator direction)')
    nullable = {'step_size', 'max_excursion', 'hop_threshold_hz', 'confirmation_span_hz'}
    for k in DEFAULTS.keys() - {'axis', 'direction'}:
        if k in nullable and p[k] is None:
            continue
        if not finite(p[k]) or p[k] <= 0:
            raise ValueError(k + ' must be positive and finite')
    if type(p['confirm_samples']) is not int or not 2 <= p['confirm_samples'] <= 100:
        raise ValueError('confirm_samples must be an integer in [2, 100]')
    if type(p['max_steps']) is not int or not 1 <= p['max_steps'] <= 1000:
        raise ValueError('max_steps must be an integer in [1, 1000]')
    if p['timeout_s'] > 86400 or p['sample_interval_s'] < .05:
        raise ValueError('Invalid finite runtime or sample interval')
    if p['confirm_s'] >= p['confirmation_timeout_s']:
        raise ValueError('Confirmation interval must fit its deadline')
    if all(p[k] is not None for k in ('confirmation_span_hz', 'hop_threshold_hz')):
        if p['confirmation_span_hz'] >= p['hop_threshold_hz'] / 2:
            raise ValueError('Confirmation spread must be less than half the hop threshold')
    return p


def require_configured(p):
    for key in ('step_size', 'max_excursion', 'hop_threshold_hz', 'confirmation_span_hz'):
        if p[key] is None:
            raise ValueError(key + ' is required in laser-specific card settings')


def run_branch_hop(ports, p, *, clock, sleep, cancel, emit, publish=lambda value: None):
    p = parameters(dict(parameters=p), {})
    require_configured(p)
    if ports['loop'].is_open_loop() is not True:
        raise DeviceError('Stop the lock first; intentional branch hop never stops PI')
    ex = BoundedExperiment(ports, p, clock=clock, sleep=sleep, cancel=cancel, emit=emit)
    axis, sign = p['axis'], 1 if p['direction'] == 'increase' else -1
    policy = ex.policies[axis]
    step = min(p['step_size'], policy.max_step, policy.max_slew_per_s * policy.update_interval_s)
    if policy.resolution:
        step = math.floor((step + 1e-12) / policy.resolution) * policy.resolution
    if step <= 0:
        raise ValueError('Requested step is below axis resolution')
    initial_outputs = dict(ex.expected)
    available = policy.high - ex.expected[axis] if sign > 0 else ex.expected[axis] - policy.low
    span = min(p['max_excursion'], available)
    steps = min(p['max_steps'], int(math.floor((span + 1e-12) / step)))
    if steps < 1:
        raise ValueError('No whole step fits the approved owner interval')
    data = dict(schema=SCHEMA, parameters=p, policies={k: asdict(v) for k, v in ex.policies.items()},
                initial_outputs=initial_outputs, effective_step=step, step_budget=steps,
                points=[], commands=ex.commands, status='running', hop_confirmed=False,
                adjacency_verified=False, target_changed=False, lock_started=False,
                restored=False, recovery_permitted=False, completion_action='hold_last_output')

    def record(row):
        data['points'].append(row)
        data['last_observation'] = compact_row(row)
        publish(copy.deepcopy(data))

    def window(stage, previous=None, direction=None):
        began = clock(); samples = []
        while clock() - began < p['confirmation_timeout_s']:
            row = ex.read(stage); record(row)
            samples.append(row)
            # A gap cannot be counted as sustained confirmation.
            if len(samples) > 1 and row['acquired_s'] - samples[-2]['acquired_s'] > max(3*p['sample_interval_s'], p['max_sample_age_s']):
                samples = [row]
            frequencies = [r['frequency_hz'] for r in samples]
            displaced = previous is None or all(direction * (f - previous) > p['hop_threshold_hz'] for f in frequencies)
            stable = max(frequencies) - min(frequencies) <= p['confirmation_span_hz']
            if not stable or not displaced:
                samples = [row] if previous is None else []
            if (len(samples) >= p['confirm_samples']
                    and samples[-1]['acquired_s'] - samples[0]['acquired_s'] >= p['confirm_s']):
                return samples
            ex.wait(p['sample_interval_s'])
        return []

    def finish(status, reason, confirmation=()):
        ex.check()
        final = {k: v.readback() for k, v in ex.axes.items()}
        if any(abs(final[k]-ex.expected[k]) > max(ex.policies[k].resolution, 1e-8) for k in final):
            raise DeviceError('Output changed before final hold verification')
        data.update(status=status, reason=reason, final_outputs=final,
                    hop_confirmed=status=='succeeded', confirmation=list(confirmation),
                    elapsed_s=clock()-ex.started, output_hold_verified=True)
        if confirmation:
            frequency = sum(r['frequency_hz'] for r in confirmation) / len(confirmation)
            data.update(held_frequency_hz=frequency, held_wavelength_nm=299792458e9/frequency)
        artifact = seal(copy.deepcopy(data)); publish(artifact)
        result = {k: v for k, v in artifact.items() if k not in ('points', 'commands', 'policies', 'confirmation')}
        if 'initial_observation' in result:
            result['initial_observation'] = compact_row(result['initial_observation'])
        result.update(message=reason, artifact=artifact)
        emit('intentional_branch_hop_result', {k: v for k,v in result.items() if k!='artifact'})
        return result

    try:
        baseline = window('branch-hop initial baseline')
        if not baseline:
            return finish('inconclusive', 'Initial optical frequency was not stable; no output moved')
        data['initial_observation'] = baseline[-1]
        previous = baseline[-1]
        # A connect-time spectral-only claim must return to normal fresh-frequency
        # checks before any deliberate move. This changes no physical output.
        ex.current_recovery(False)
        class HopObserved(Exception):
            def __init__(self, before, after):
                self.before, self.after = before, after

        latest = [previous]
        def watch(row):
            record(row)
            prior = latest[0]; latest[0] = row
            if abs(row['frequency_hz']-prior['frequency_hz']) > p['hop_threshold_hz']:
                raise HopObserved(prior, row)

        for index in range(1, steps+1):
            ex.check()
            ex.observation_observer = watch
            try:
                # Include the common move primitive's final prewrite read.
                # A natural hop observed there must stop before another move.
                ex.read('branch-hop before step')
                destination = initial_outputs[axis] + sign * index * step
                ex.move(axis, destination, 'intentional branch hop')
                ex.wait(p['dwell_s'])
                ex.read('branch-hop after step')
            except HopObserved as transition:
                ex.observation_observer = None
                previous, observed = transition.before, transition.after
                jump = observed['frequency_hz']-previous['frequency_hz']
                data['suspected_transition'] = dict(before=compact_row(previous), after=compact_row(observed), jump_hz=jump)
                # Freeze immediately, even if this first candidate cannot be
                # confirmed. Never continue into a second branch or restore.
                confirmation = window('branch-hop confirmation', previous['frequency_hz'], 1 if jump>0 else -1)
                if confirmation:
                    return finish('succeeded', 'First observed hop confirmed; outputs held, PI off; adjacency not proven', confirmation)
                return finish('inconclusive', 'Suspected hop was not confirmed; no further moves, last output held')
            finally:
                ex.observation_observer = None
        return finish('inconclusive', 'No hop observed within the finite scan; last output held')
    except BaseException as error:
        # Preserve partial evidence; do not command a return on cancellation,
        # stale observations, or an unknown write result.
        data.update(status='cancelled' if cancel() else 'interrupted', error_type=type(error).__name__,
                    output_hold_verified=False, elapsed_s=clock()-ex.started)
        publish(copy.deepcopy(data))
        raise
