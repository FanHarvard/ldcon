"""Attached hardware staircase; never claim or release the parent lock lease."""
from copy import deepcopy
import math
import uuid

from ldcon.core.interface_cards import CardCancelled
from ldcon.core.records import atomic_json
from ldcon.jobs.modulation_sweep import validate_request
from ldcon.owners.dlcpro_awg_plan import configuration


def parameters(card, overrides):
    p = {**card['parameters'], **overrides}
    if set(p) != {'shot_sweep', 'pzt_ff_gain_scale', 'sample_interval_s'}:
        raise ValueError('DLC AWG card requires shot_sweep, pzt_ff_gain_scale, sample_interval_s')
    for key in ('pzt_ff_gain_scale', 'sample_interval_s'):
        if type(p[key]) not in (int, float) or not math.isfinite(p[key]) or p[key] <= 0:
            raise ValueError('Invalid ' + key)
    if p['shot_sweep'] is not None:
        plan = validate_request(p['shot_sweep'])
        if (plan.get('shape', 'staircase') != 'staircase' or plan['unit'] != 'Hz'
                or plan['axis'] != 'servo_modulation' or not plan['levels']
                or plan['trigger_mode'] not in ('external', 'external_rising')):
            raise ValueError('DLC AWG requires an external frequency staircase')
    return p


def device_of(laser):
    return laser['devices'][laser['interfaces']['dlc_lock']['device']]


def readiness(laser, deck, card, overrides=None):
    p = parameters(card, overrides or {})
    issues = []
    try:
        device = device_of(laser)
        if device['type'] != 'dlcpro.laser.owner' or not device['connection']['endpoint']:
            raise ValueError('DLC owner required')
        configuration(device['control_settings']['awg_sweep'])
    except (KeyError, TypeError, ValueError) as error:
        issues.append('DLC AWG commissioning required: ' + str(error))
    if not laser['allow_output']:
        issues.append('Laser output writes disabled')
    return dict(runnable=not issues, blocked_reasons=issues, parameters=p)


def envelope(request, state):
    result = dict(schema=request['schema'], request_id=request['request_id'],
        plan_sha256=request['plan_sha256'], simulated=False,
        trigger_mode=request['plan']['trigger_mode'], trigger_source='EXT')
    for key in ('state', 'ready', 'armed', 'restored', 'playback_completed',
                'trigger_s', 'trigger_bounds_s', 'reason', 'outputs_uncertain'):
        if key in state:
            result[key] = deepcopy(state[key])
    return result


def run(directory, laser, card, p, *, clock, sleep, cancel, emit, publish, session_factory=None):
    p = parameters(card, p)
    request = p['shot_sweep']
    if request is None:
        raise ValueError('Invoke from an explicitly compiled labscript staircase')
    cfg = configuration(device_of(laser)['control_settings']['awg_sweep'])
    if p['sample_interval_s'] >= cfg['watchdog_s']/2:
        raise ValueError('Proxy heartbeat interval exceeds the owner deadline')
    if session_factory is None:
        from ldcon.jobs.dlcpro_cards import Session
        session_factory = Session
    session = session_factory(device_of(laser))
    token = uuid.uuid4().hex
    # Persist identity before the single mutation; never repeat it after a lost reply.
    atomic_json(directory/'awg-request.json', dict(request=request, token=token))
    snapshot = session.rpc('inspect')['snapshot']
    if cancel():
        raise CardCancelled('Cancelled before arming')
    try:
        state = session.rpc('awg_sweep_begin', request=request, token=token,
            target_revision=snapshot['target_revision'], pzt_scale=p['pzt_ff_gain_scale'])
    except Exception:
        state = session.rpc('awg_sweep_status', request_id=request['request_id'])
        if not state or state.get('request', {}).get('request_id') != request['request_id']:
            raise RuntimeError('AWG arm outcome unknown; inspect owned request before retrying') from None
    deadline = clock()+cfg['arm_timeout_s']+cfg['max_duration_s']+cfg['watchdog_s']
    cancelled = False
    while state.get('state') not in ('complete', 'failed'):
        publish(envelope(request, state))
        if (cancel() or clock() >= deadline) and not cancelled:
            cancelled = True
            # One cancellation only. Unknown replies are resolved through status.
            try:
                state = session.rpc('awg_sweep_cancel', token=token, request_id=request['request_id'])
            except Exception:
                state = session.rpc('awg_sweep_status', request_id=request['request_id'])
            if state.get('state') in ('complete', 'failed'):
                break
        if clock() > deadline+cfg['watchdog_s']:
            raise RuntimeError('AWG cleanup unconfirmed; retain request for inspection')
        sleep(p['sample_interval_s'])
        state = session.rpc('awg_sweep_heartbeat', token=token, request_id=request['request_id'])
    full = session.rpc('awg_sweep_result', request_id=request['request_id'])
    atomic_json(directory/'awg-owner-result.json', full)
    from ldcon.jobs.dlcpro_awg_analysis import save
    analysis = save(directory, request, full)
    verdict = full.get('verdict', 'failed')
    outcome = dict(verdict=verdict, final=True, restored=full.get('restored') is True,
        playback_completed=full.get('playback_completed') is True,
        reason=full.get('reason', full.get('error')), analysis=analysis)
    publication = envelope(request, full)
    publication['outcome'] = outcome
    publish(publication)
    emit('awg_sweep_outcome', outcome)
    if verdict == 'cancelled':
        raise CardCancelled(outcome['reason'] or 'AWG sweep cancelled')
    if verdict != 'succeeded' or not outcome['restored']:
        raise RuntimeError(outcome['reason'] or 'AWG sweep failed')
    return dict(status='succeeded', message='Hardware staircase complete; parent lock resumed', outcome=outcome)
