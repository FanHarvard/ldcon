"""Versioned atomic PI transaction shared by system and physical owners.

No host ramp or optical-settling budget. A bounded transport deadline remains:
unknown delivery/commit blocks further effects; it never grants a handoff.
"""
import math
import re
import time
import uuid

from ldcon.core.contracts import DeviceError

FIELDS = ('pid_kp', 'pid_ki', 'pid_kd', 'pid_integral_min', 'pid_integral_max',
          'pid_multiplier', 'target', 'control_offset', 'system_min', 'system_max',
          'multiplier_target', 'multiplier_control', 'software_modulation_enable',
          'dac_min_count', 'dac_max_count')
CHANGED = {'pid_kp', 'pid_ki', 'pid_integral_min', 'pid_integral_max', 'control_offset'}
KEYS = {'version', 'request_id', 'expected_boot_id', 'expected_revision', 'expected',
        'kp', 'ki', 'integral_limit_count', 'output_min_count', 'output_max_count',
        'deadline_uptime_ms', 'max_sample_age_ms'}


def number(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError('Finite PI transaction value required')
    return value


def same(a, b):
    return math.isclose(number(a), number(b), rel_tol=1e-10, abs_tol=1e-12)


def capability(cfg):
    if (cfg.get('pi_handover_capability') != 1 or not isinstance(cfg.get('pi_handover_boot_id'), str)
            or not re.fullmatch(r'[A-Za-z0-9_-]{1,63}', cfg['pi_handover_boot_id'])
            or type(cfg.get('pi_handover_revision')) is not int):
        raise ValueError('Atomic PI handover firmware capability v1 is required; legacy writes are not a fallback')
    return {key: number(cfg[key]) for key in FIELDS}


def validate_request(request, cfg, low, high):
    """Physical owner admits only this exact protocol within projected bounds."""
    if not isinstance(request, dict) or request.get('version') != 1 or not isinstance(request.get('request_id'), str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,63}', request['request_id']):
        raise ValueError('Invalid atomic PI transaction identity')
    if set(request) == {'version', 'request_id', 'query'} and request['query'] is True:
        return  # Receipt lookup, no configuration effect or replay.
    if set(request) != KEYS:
        raise ValueError('Invalid atomic PI transaction fields')
    actual = capability(cfg)
    if (request['expected_boot_id'] != cfg['pi_handover_boot_id'] or
            type(request['expected_revision']) is not int or request['expected_revision'] != cfg['pi_handover_revision']):
        raise ValueError('PI transaction firmware context changed')
    expected = request['expected']
    if not isinstance(expected, dict) or set(expected) != set(FIELDS) or any(not same(expected[k], actual[k]) for k in FIELDS):
        raise ValueError('PI transaction configuration changed')
    if actual['pid_multiplier'] != 1 or actual['pid_kd'] != 0 or any(actual[k] != 0 for k in ('multiplier_target','multiplier_control','software_modulation_enable')):
        raise ValueError('Atomic handover requires active PI without external or software modulation')
    for k in ('kp', 'ki'):
        if number(request[k])*actual['pid_'+k] <= 0:
            raise ValueError('PI candidate must preserve the commissioned feedback sign')
    lim = number(request['integral_limit_count'])
    lo, hi = number(request['output_min_count']), number(request['output_max_count'])
    if not (lim > 0 and max(0., low, actual['dac_min_count']) <= lo < hi <= min(65535., high, actual['dac_max_count'])):
        raise ValueError('PI transaction exceeds physical PZT output bounds')
    p = [request['kp']*(actual[k]-actual['target']) for k in ('system_min','system_max')]
    if min(p) - lim + hi < max(p) + lim + lo:
        raise ValueError('PI candidate exceeds the physical PZT output envelope')
    now = number(cfg['pi_handover_uptime_ms'])
    if not (now < number(request['deadline_uptime_ms']) <= now+5000 and
            0 < number(request['max_sample_age_ms']) <= 1000):
        raise ValueError('Invalid PI transport deadline or native freshness')


def validate_receipt(reply, request, cfg):
    if not isinstance(reply, dict) or reply.get('request_id') != request['request_id'] or reply.get('state') != 'committed':
        raise DeviceError('PI transaction was not confirmed committed')
    before, after = reply['before'], reply['after']
    if set(before) != set(FIELDS) or set(after) != set(FIELDS):
        raise DeviceError('Incomplete PI handover receipt')
    if any(not same(before[k], request['expected'][k]) for k in FIELDS):
        raise DeviceError('PI handover receipt has a different baseline')
    if any(not same(after[k], before[k]) for k in set(FIELDS)-CHANGED):
        raise DeviceError('PI handover changed an unrequested reference or routing field')
    raw = request['integral_limit_count']/abs(request['ki'])
    desired = dict(pid_kp=request['kp'], pid_ki=request['ki'], pid_integral_min=-raw, pid_integral_max=raw)
    if any(not same(after[k], v) for k,v in desired.items()):
        raise DeviceError('PI handover coefficient readback mismatch')
    lo, hi = request['output_min_count'], request['output_max_count']
    dac = number(reply['dac_after'])
    if (number(reply['dac_before']) != dac or dac != int(dac) or not lo <= dac <= hi
            or not same(reply['error_before'], reply['error_after'])
            or not 0 <= number(reply['sample_age_ms']) <= request['max_sample_age_ms']
            or not 0 <= number(reply['committed_uptime_ms']) <= request['deadline_uptime_ms']):
        raise DeviceError('PI handover output continuity/freshness was not proved')
    integral = number(reply['raw_integral_after'])
    error = number(reply['error_after'])
    reconstructed = after['control_offset']+request['kp']*error+request['ki']*integral
    if not -raw <= integral <= raw or not lo <= reconstructed <= hi or int(reconstructed) != dac:
        raise DeviceError('PI handover state does not reproduce held DAC')
    envelope = [after['control_offset']+request['kp']*(after[k]-after['target'])+i
                for k in ('system_min','system_max') for i in (-request['integral_limit_count'],request['integral_limit_count'])]
    if min(envelope) < lo-1e-7 or max(envelope) > hi+1e-7:
        raise DeviceError('PI handover final output envelope exceeded')
    if (cfg.get('pi_handover_boot_id') != request['expected_boot_id'] or
            reply['revision'] != request['expected_revision']+1 or cfg.get('pi_handover_revision') != reply['revision'] or
            any(not same(cfg[k], after[k]) for k in FIELDS)):
        raise DeviceError('PI handover final configuration context mismatch')
    return after


def transact(native, expected, pid):
    """Send once; poll receipts only. Unknown effects are never retried."""
    owner = native.o
    if owner.fault:
        raise DeviceError('Owner fault prevents PI handover')
    servo = owner.hw().connections['piezo']
    cfg = servo.get('config')
    before = capability(cfg)
    if any(not same(cfg[k], v) for k,v in expected.items()):
        raise ValueError('External native PID configuration change before handover')
    policy = owner.policies()['piezo']
    request = dict(version=1, request_id=uuid.uuid4().hex,
        expected_boot_id=cfg['pi_handover_boot_id'], expected_revision=cfg['pi_handover_revision'],
        expected=before, kp=pid['kp'], ki=pid['ki'], integral_limit_count=pid['integral_limit_count'],
        output_min_count=max(0.,policy.low,before['dac_min_count']),
        output_max_count=min(65535.,policy.high,before['dac_max_count']),
        deadline_uptime_ms=number(cfg['pi_handover_uptime_ms'])+4000.,
        max_sample_age_ms=min(1000.,number(native.p.get('native_input',{}).get('max_age_s',1.))*1000.))
    validate_request(request, cfg, policy.low, policy.high)
    owner.audit('pi_handover_intent', request=request)
    deadline = time.monotonic()+5.
    try:
        reply = servo.set({'pi_handover':request})['pi_handover']
        while reply.get('state') == 'accepted' and time.monotonic() < deadline:
            time.sleep(.01)
            reply = servo.set({'pi_handover':dict(version=1,request_id=request['request_id'],query=True)})['pi_handover']
        if reply.get('request_id') != request['request_id']:
            raise DeviceError('PI handover receipt identity mismatch')
        if reply.get('state') == 'rejected':
            # Explicit firmware rejection is not an uncertain output write.
            owner.audit('pi_handover_rejected', receipt=reply)
        else:
            after = servo.get('config')
            validate_receipt(reply,request,after)
            native.note_servo_config(after)
            native.persist()
            owner.audit('pi_handover_committed', receipt=reply)
            return after, reply
    except Exception as error:
        owner.fault = 'Uncertain atomic PI handover; inspect receipt '+request['request_id']
        native.persist()
        owner.audit('pi_handover_uncertain', request_id=request['request_id'],error_type=type(error).__name__)
        raise DeviceError(owner.fault) from error
    raise ValueError('PI handover rejected before effect: '+str(reply.get('error','unspecified')))
