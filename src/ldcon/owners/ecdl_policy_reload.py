"""Saved/effective ECDL definitions and approved safe-boundary policy adoption."""
from ldcon.core.state_layout import root_from_laser_file

import copy
import hashlib
import json
import os
import re
import stat
from dataclasses import asdict, replace

from ldcon.core.contracts import DeviceError
from ldcon.core.device_conditions import finite
from ldcon.core.instrument_client import BrokerError
from ldcon.core.interface_schema import digest, load_mapping, validate_laser


def saved(owner):
    path = getattr(owner, 'laser_file', None)
    if path is None:
        return None
    try:
        document = load_mapping(path.read_text())
        validate_laser(document)
    except (OSError, ValueError) as error:
        raise BrokerError('configuration', 'Saved laser definition unavailable: ' + str(error)) from error
    if document.get('id') != owner.laser['id']:
        raise BrokerError('configuration', 'Saved definition names another laser')
    return document


def device_key(owner):
    if hasattr(owner,'device_key'):
        return owner.device_key
    return next(key for key, value in owner.laser['devices'].items() if value is owner.device)


def policy_state(owner):
    request = copy.deepcopy(getattr(owner, 'adoption_request', None))
    try:
        document = saved(owner)
        desired = digest(document['devices'][device_key(owner)]) if document else digest(owner.device)
        return dict(effective_revision=digest(owner.device), saved_revision=desired,
                    pending=desired != digest(owner.device), adoption_request=request)
    except BrokerError as error:
        return dict(effective_revision=digest(owner.device), saved_revision=None,
                    pending=True, reason=str(error), adoption_request=request)


# Rule M step 1 (2026-10-27): control-setting leaves the owner adopts live besides the steady profile.
AXIS_TIMING = ('update_interval_s', 'settle_s')          # re-derive axis policies under prepare_limits
HOT_BLOCKS = ('locked_sweep', 'sweep_api_defaults')      # projected sweep limits / sweep API defaults
HOT_COMPENSATION = ('enabled', 'minimum_interval_s')     # temperature_compensation switch and cadence


def _assign(target, source, parts):
    """Copy one changed leaf (or remove it) from the saved block into the effective block."""
    for key in parts[:-1]:
        if isinstance(source, list):
            source, target = source[int(key)], target[int(key)]
            continue
        if key not in source:
            target.pop(key, None)
            return
        source = source[key]
        target = target.setdefault(key, {})
    key = parts[-1]
    if isinstance(source, list):
        target[int(key)] = copy.deepcopy(source[int(key)])
    elif key in source:
        target[key] = copy.deepcopy(source[key])
    else:
        target.pop(key, None)


def _changes(owner, device):
    from ldcon.core.agent_authority import describe_changes, FORBIDDEN
    key = device_key(owner)
    rows = describe_changes('lasers', {'devices': {key: owner.device}}, {'devices': {key: device}})
    if any(row['class'] == FORBIDDEN for row in rows):
        raise BrokerError('configuration', 'Hardware identity or calibration changed; an explicit idle configuration transition is required')
    return rows


def _approved(owner, device, rows, request):
    """Operational bounds are authorized by an exact trusted dashboard decision."""
    from ldcon.core.agent_authority import SAFETY
    absolute = [row for row in rows if row['class'] == SAFETY]
    if not absolute:
        return
    from ldcon.core.agent_authority import approval_root
    if not getattr(owner, 'laser_file', None):
        raise BrokerError('configuration', 'Saved definition and trusted approval root are required')
    identifier = request.get('decision_id')
    if not isinstance(identifier, str) or not re.fullmatch('[0-9a-f]{32}', identifier):
        raise BrokerError('request', 'Exact approved decision id required')
    path = approval_root(root_from_laser_file(owner.laser_file), owner.laser['id']) / 'decisions' / (identifier + '.json')
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 2**20:
                raise ValueError('Bounded single-link decision required')
            decision = json.load(stream)
        if (decision.get('id') != identifier or decision.get('laser_id') != owner.laser['id']
                or decision.get('scope') != dict(kind='lasers', name=owner.laser['id'])):
            raise ValueError('Decision identity mismatch')
    except (OSError, ValueError) as error:
        raise BrokerError('configuration', 'Trusted decision unavailable: ' + str(error)) from error
    if decision['authority'] not in ('human_approval', 'maintainer'):
        raise BrokerError('configuration', 'Operational safety limits require direct human approval')
    if hashlib.sha256(owner.laser_file.read_bytes()).hexdigest() != decision['revision_after']:
        raise BrokerError('configuration', 'Approval does not name the currently saved laser revision')
    for row in absolute:
        if not any(all(approved.get(key) == row.get(key) for key in ('path', 'old', 'new'))
                   for approved in decision['changes']):
            raise BrokerError('configuration', 'Operational bound differs from the exact approved change')
    return dict(human_identity=decision.get('requested_by') or decision['authority'],receipt_id=identifier,
                exact_changes=absolute,saved_revision=decision['revision_after'])


def _busy(owner):
    steady = owner.steady
    if getattr(getattr(steady,'background',None),'epoch',None):return True
    sweep = getattr(steady, 'sweep', None)
    sweep_active = sweep is not None and sweep.state not in ('restored', 'restored_unverified', 'rejected')
    if sweep_active or getattr(steady, 'response_control', False) or getattr(owner, 'recovery', None):
        return True
    if getattr(steady, 'study', None) and steady.study.status == 'running':
        return True
    if getattr(steady, 'pid_tuning', None) and not steady.pid_tuning.closed:
        return True
    window = getattr(getattr(steady, 'planner', None), 'window', None)
    return bool(window and (getattr(window, 'active', False) or getattr(window, 'scan', None)))


def _effective(owner, device, state):
    from ldcon.owners.ecdl_owner import axis_policies
    policies = axis_policies(device)
    owner.validate_state(state, policies)
    result = {}
    activation = owner.activation or state['outputs']
    for name, policy in policies.items():
        bounds = device['absolute_conditions']['axes'][name]['activation_range']
        half = 0. if name == 'piezo' else finite(state[name]['amplitude_vpp']) / 2
        low = max(policy.low, activation[name] + bounds[0]) + half
        high = min(policy.high, activation[name] + bounds[1]) - half
        if not low <= finite(state['outputs'][name]) <= high:
            raise BrokerError('range', name + ' actual output/dither exceeds the new operational interval')
        result[name] = replace(policy, low=low, high=high)
    return result


def _native_envelope(owner, state, policies):
    """Actual running coefficients, raw integral bounds and all accepted inputs.

    Autonomous firmware PI is bounded in output only; host step/slew constraints
    apply to host commands and are deliberately not imposed on firmware motion.
    An active sweep/FF/tuning plan must finish before adopting a new policy.
    """
    config = state['servo_config']
    if not owner.steady.armed:
        if state.get('open_loop') is not True:
            raise BrokerError('range', 'Unowned autonomous output cannot be proved for policy adoption')
        return [state['outputs']['piezo']] * 2
    if any(finite(config[key]) != 0 for key in ('multiplier_control', 'multiplier_target', 'software_modulation_enable', 'pid_kd')):
        raise BrokerError('range', 'Active native feedforward/derivative requires its complete trajectory proof before reload')
    if finite(config['pid_multiplier']) != 1:
        raise BrokerError('range', 'Native PI configuration changed during policy adoption')
    targets = [finite(config['target'])]
    # Include all future target-ramp values, not only the current setpoint.
    target_hz = getattr(owner.steady, 'target', None)
    if target_hz is not None:
        targets.append(299792458e9 / finite(target_hz))
    kp, ki = finite(config['pid_kp']), finite(config['pid_ki'])
    raw = [finite(config['pid_integral_min']), finite(config['pid_integral_max'])]
    inputs = [finite(config['system_min']), finite(config['system_max'])]
    if inputs[0] > inputs[1] or raw[0] > raw[1]:
        raise BrokerError('range', 'Invalid actual native bounds')
    profile = getattr(owner.steady, 'p', None)
    if target_hz is not None and profile is not None:
        capture = finite(profile['capture_hz'])
        if not 0 < capture < target_hz:
            raise BrokerError('range', 'Future target capture interval is invalid')
        inputs += [299792458e9 / (target_hz - capture), 299792458e9 / (target_hz + capture)]
    values = [finite(config['control_offset']) + kp * (target - input_value) + ki * integral
              for target in targets for input_value in inputs for integral in raw]
    low, high = min(values), max(values)
    policy = policies['piezo']
    if not policy.low <= low <= high <= policy.high:
        raise BrokerError('range', 'Entire actual native PI envelope exceeds the new piezo interval')
    return [low, high]


def prepare_limits(owner, device):
    """Read/validate twice under the owner lock; preserve moving PI output."""
    if _busy(owner):
        return None,lambda:None
    first = owner.hw().snapshot()
    policies = _effective(owner, device, first)
    proof = _native_envelope(owner, first, policies)
    second = owner.hw().snapshot()
    if first['servo_config'] != second['servo_config']:
        raise BrokerError('configuration', 'Native configuration changed during policy proof')
    policies = _effective(owner, device, second)
    _native_envelope(owner, second, policies)
    steady = owner.steady
    planner = None
    if steady.armed and steady.planner is not None:
        from ldcon.owners.ecdl_steady_policy import SteadyPlanner
        planner = SteadyPlanner(steady.p, policies, second['outputs'], steady.strategy,
                               tec_reference_dac=steady.tec_reference_dac)
    def apply():
        if getattr(steady, 'planner', None) is not None:
            steady.discard_proposals('approved operational policy adoption')
        if planner is not None:
            steady.planner = planner
    return dict(policies={key: asdict(value) for key, value in policies.items()},
                native_envelope=proof, autonomous_step_slew_limited=False),apply


def adopt_limits(owner, device):
    if hasattr(owner,'policy_authority'):
        raise BrokerError('configuration','Limits require the system policy adoption transaction')
    proof,apply=prepare_limits(owner,device)
    if proof is not None:
        owner.device['absolute_conditions']=copy.deepcopy(device['absolute_conditions'])
        apply()
    return proof


def commit(owner, candidate, receipt, proof, apply=lambda:None):
    """Publish effective policy only after participant projections are installed."""
    authority=getattr(owner,'policy_authority',None)
    if authority is None:
        owner.laser.clear();owner.laser.update(copy.deepcopy(candidate))
        owner.device=owner.laser['devices'][device_key(owner)] if hasattr(owner,'device_key') else owner.laser['devices']['rig']
        apply()
        if owner.hardware is not None:owner.hardware.device=copy.deepcopy(owner.device)
        return
    from ldcon.owners.ecdl_projection import install,verify
    revision=authority.revision
    approval=None
    if receipt is not None:
        approval=authority.approve(candidate,expected_revision=revision,authorize_human=lambda _:receipt)
    projections={}
    def adopt(document, prepared):
        projections.update(install(owner,document))
        apply()
    authority.adopt(candidate,expected_revision=revision,approval=approval,
        prepare=lambda _:dict(domain_proof=proof),apply=adopt,
        verify=lambda document:verify(owner,document),
        quality_authorized=True,operational_authorized=True)


def adopt_saved(owner, expected, decision_id=None):
    """Next claim (or a lease-less settings_adopt) adopts the authoritative snapshot after the last lease ends."""
    if owner.lease is not None or owner.steady.armed or owner.fault:
        raise BrokerError('busy', 'Saved definition adoption requires an idle fault-free owner')
    document = saved(owner)
    if document is None:
        return
    key = device_key(owner)
    device = document['devices'][key]
    if digest(device) != expected:
        raise BrokerError('configuration', 'Worker snapshot differs from the saved definition')
    authority = getattr(owner, 'policy_authority', None)
    if authority is not None and document == owner.laser:
        # An unchanged claim is not a configuration transaction. Reinstalling
        # every physical projection adds broker round trips and durable writes
        # to each manual move. Verify the effective authority and participants
        # instead; uncertain/pending adoption must never take this shortcut.
        with authority._mutex, authority._fence():
            revision = digest(document)
            if authority._projection_revision != revision or authority.snapshot() != document:
                raise BrokerError('configuration', 'Effective policy changed before claim')
            for binding in authority._bindings:
                state = binding._guard.inspect(binding)
                projection = state.get('projection') or {}
                if (state['fault'] or projection.get('policy_revision') != revision
                        or projection.get('dependencies') != authority.dependencies):
                    raise BrokerError('configuration', 'Existing physical projection requires inspection before claim')
        owner.audit('saved_definition_reused', revision=digest(owner.device))
        return
    _changes(owner, device)  # Preserve the hardware/calibration transition gate.
    from ldcon.core.agent_authority import describe_changes
    rows = describe_changes('lasers', owner.laser, document)
    # Include top-level target bounds in the exact approval evidence: the
    # system authority adopts the whole laser document, not just its device.
    # Operational limit differences must have been adopted by a trusted live
    # decision, or be explicitly supplied with the next claim decision id.
    from ldcon.core.agent_authority import SAFETY
    receipt=None;proof=None;apply=lambda:None
    if any(row['class']==SAFETY for row in rows):
        from ldcon.core.agent_authority import approval_root
        directory = approval_root(root_from_laser_file(owner.laser_file), owner.laser['id']) / 'decisions'
        candidates = sorted(directory.glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True)
        if isinstance(decision_id, str) and (directory / (decision_id + '.json')) in candidates:
            candidates.remove(directory / (decision_id + '.json'))
            candidates.insert(0, directory / (decision_id + '.json'))
        for path in candidates:
            try:
                receipt=_approved(owner, device, rows, dict(decision_id=path.stem))
                break
            except BrokerError:
                continue
        else:
            raise BrokerError('configuration', 'Saved operational limits lack an exact trusted decision')
        proof,apply=prepare_limits(owner, device)
        if proof is None:
            raise BrokerError('busy', 'An active trajectory still owns the previous operational limits')
    # Nothing is applied to a running lease. Rebuild all profile/axis snapshots
    # together, so a later worker cannot see a mixture of desired revisions.
    commit(owner,document,receipt,proof,apply)
    owner.audit('saved_definition_adopted', revision=digest(owner.device))


def reload_settings(owner, request):
    if getattr(getattr(owner.steady,'background',None),'epoch',None):
        owner.steady.reserve_background(['direct:settings_reload'])
        raise BrokerError('configuration', 'Background checkpoint restoration is pending before settings reload')
    device = request.get('device')
    paths = request.get('paths')
    if not isinstance(device, dict) or not isinstance(paths, list):
        raise BrokerError('request', 'settings_reload needs a device and paths')
    document = saved(owner)
    if document is not None and digest(document['devices'][device_key(owner)]) != digest(device):
        raise BrokerError('configuration', 'Worker requested a definition other than the saved laser')
    candidate = copy.deepcopy(owner.laser)
    candidate['devices'][device_key(owner)] = copy.deepcopy(device)
    validate_laser(candidate)
    rows = _changes(owner, device)
    receipt=_approved(owner, device, rows, request)
    effective=copy.deepcopy(owner.laser)
    effective_device=effective['devices'][device_key(owner)]
    current = effective_device['control_settings']
    new = device['control_settings']
    applied, deferred, snapshot = [], [], []
    absolute = [row for row in rows if '.absolute_conditions.' in row['path']]
    # Axis timing (rule M step 1): the policies derive max_step / slew from update_interval_s, so a timing change is
    # proved like a limit change (prepare_limits on the whole candidate device) and installed with it.
    timing = [row for row in rows if row['live'] == 'owner_hot'
              and row['path'].split('.', 2)[2].startswith('control_settings.axes.')
              and row['path'].split('.')[-1] in AXIS_TIMING]
    proof = None
    apply_limits=lambda:None
    if absolute or timing:
        proof,apply_limits = prepare_limits(owner, device)
        if proof is not None:
            effective_device['absolute_conditions']=copy.deepcopy(device['absolute_conditions'])
            for row in timing:
                _assign(current, new, row['path'].split('.', 3)[3].split('.'))
        target = deferred if proof is None else applied
        target.extend(row['path'].split('.', 2)[2] for row in absolute + timing)
    steady_keys = []
    busy = _busy(owner)
    for row in rows:
        relative = row['path'].split('.', 2)[2]
        if relative.startswith('absolute_conditions.') or row in timing:
            continue
        if row['live'] != 'owner_hot':
            snapshot.append(relative)
            continue
        path = relative.removeprefix('control_settings.')
        parts = path.split('.')
        if parts[0] in HOT_BLOCKS:
            # Projected sweep limits / sweep API defaults: copied into the projection between trajectories only.
            if busy:deferred.append(relative)
            else:
                _assign(current, new, parts)
                applied.append(relative)
        elif parts[0] == 'temperature_compensation' and len(parts) == 2 and parts[1] in HOT_COMPENSATION:
            _assign(current, new, parts)
            applied.append(relative)
        elif parts[0] == 'optical' and len(parts) >= 2 and parts[1] in owner.HOT_OPTICAL:
            # A complete threshold block is one coherent decision, with removals.
            if parts[1] in new['optical']:
                current['optical'][parts[1]] = copy.deepcopy(new['optical'][parts[1]])
            else:
                current['optical'].pop(parts[1], None)
            applied.append(relative)
        elif path == 'max_sample_age_s':
            current[path] = new[path]
            applied.append(relative)
        elif parts[0]=='background_adjustments':
            from ldcon.core.adjustment_registry import PARAMETER_FAMILIES,owner_policy
            block=new.get('background_adjustments',{})
            if not isinstance(block,dict) or set(block)-set(PARAMETER_FAMILIES):
                raise BrokerError('configuration','Unknown background adjustment family')
            for family,policy in block.items():
                if policy != {'enabled':False}:owner_policy(device,family)
            if _busy(owner):deferred.append(relative)
            else:
                if 'background_adjustments' in new:current['background_adjustments']=copy.deepcopy(block)
                else:current.pop('background_adjustments',None)
                applied.append(relative)
        elif path.startswith('steady_lock.'):
            steady_keys.append(path.removeprefix('steady_lock.'))
        else:
            snapshot.append(relative)
    apply_profile=lambda:None
    if steady_keys:
        from ldcon.owners.ecdl_prelock import AxisPolicy
        policies=None if proof is None else {key:AxisPolicy(**value) for key,value in proof['policies'].items()}
        result,apply_profile = owner.steady.prepare_profile_reload(new.get('steady_lock', {}), steady_keys,policies=policies)
        target=current.setdefault('steady_lock',{})
        for key in result['applied']:
            parts=key.split('.')
            if len(parts)>1:_assign(target,new.get('steady_lock',{}),parts)   # one live leaf of a block (dither.paused)
            elif key in new.get('steady_lock',{}):target[key]=copy.deepcopy(new['steady_lock'][key])
            else:target.pop(key,None)
        applied += ['control_settings.steady_lock.' + key for key in result['applied']]
        deferred += ['control_settings.steady_lock.' + key for key in result['deferred']]
        snapshot += ['control_settings.steady_lock.' + key for key in result['rejected']]
    if effective != owner.laser:
        def apply():
            apply_limits();apply_profile()
        commit(owner,effective,receipt,proof,apply)
    owner.audit('settings_reloaded', applied=applied, deferred=deferred, snapshot=snapshot,
                revision=digest(owner.device), saved_revision=digest(device))
    return dict(applied=applied, deferred=deferred, snapshot=snapshot, rejected={},
                revision=digest(owner.device), saved_revision=digest(device), proof=proof,
                policies=None if proof is None else proof['policies'], path_base='device')


def settings_adopt(owner, request):
    """Rule M step 1 (2026-10-27): lease-less adoption entry for the dashboard save (``configuration.save``).

    No lease: the saved definition is adopted now through ``adopt_saved`` (same FORBIDDEN gate, same exact human
    decision for safety rows, same ``prepare_limits`` proof as the next claim). A held lease: the request is kept
    and this owner applies it at its own next safe boundary (``adopt_deferred``); the worker's own reload request
    may get there first, in which case the boundary finds nothing left to adopt.
    """
    import time
    document = saved(owner)
    if document is None:
        raise BrokerError('configuration', 'Saved laser definition is not configured for this owner')
    expected = request.get('saved_revision')
    if expected is not None and hashlib.sha256(owner.laser_file.read_bytes()).hexdigest() != expected:
        raise BrokerError('configuration', 'Saved laser revision differs from the adoption request')
    key = device_key(owner)
    saved_revision = digest(document['devices'][key])
    decision_id = request.get('decision_id')
    if decision_id is not None and not (isinstance(decision_id, str) and re.fullmatch('[0-9a-f]{32}', decision_id)):
        raise BrokerError('request', 'decision_id must be an exact decision id')
    if document == owner.laser:
        owner.adoption_request = None
        return dict(status='unchanged', revision=digest(owner.device), saved_revision=saved_revision, pending=False)
    if owner.fault:
        raise BrokerError('fault', 'Owner fault requires review before adopting saved settings: ' + str(owner.fault))
    if owner.lease is None and not owner.steady.armed:
        adopt_saved(owner, saved_revision, decision_id=decision_id)
        owner.adoption_request = None
        owner.audit('settings_adopted', revision=digest(owner.device), saved_revision=saved_revision, decision_id=decision_id)
        return dict(status='adopted', revision=digest(owner.device), saved_revision=saved_revision, pending=False)
    owner.adoption_request = dict(saved_revision=expected, device_revision=saved_revision, decision_id=decision_id,
                                  requested_unix_s=time.time(), attempts=0, last_result=None)
    owner.audit('settings_adopt_deferred', saved_revision=saved_revision, decision_id=decision_id, lease=owner.lease)
    return dict(status='deferred', revision=digest(owner.device), saved_revision=saved_revision, pending=True,
                reason='rig lease is held; the owner adopts the saved definition at its next safe boundary')


def adopt_deferred(owner, boundary):
    """Apply a deferred ``settings_adopt`` between two lease-holder requests when no trajectory is active.

    Device rows go through ``reload_settings`` (same proof/approval/projection path the worker uses); rows outside
    the device (the laser target) stay with the worker's target_hot path and the next claim, as before.
    """
    request = owner.adoption_request
    if request is None or _busy(owner):
        return None
    request['attempts'] = request.get('attempts', 0) + 1
    try:
        document = saved(owner)
        key = device_key(owner)
        if document is None or document['devices'][key] == owner.device:
            owner.adoption_request = None
            return None
        from ldcon.core.agent_authority import describe_changes
        prefix = 'devices.' + key + '.'
        paths = [row['path'][len(prefix):] for row in describe_changes('lasers', owner.laser, document)
                 if row['path'].startswith(prefix)]
        result = reload_settings(owner, dict(device=document['devices'][key], paths=paths, decision_id=request.get('decision_id')))
    except (BrokerError, DeviceError, ValueError, TypeError, KeyError) as error:
        owner.adoption_request = None
        owner.audit('settings_adopt_refused', boundary=boundary, reason=str(error), attempts=request['attempts'])
        return dict(status='refused', reason=str(error))
    request['last_result'] = dict(boundary=boundary, applied=result['applied'], deferred=result['deferred'],
                                  snapshot=result['snapshot'], revision=result['revision'])
    if result['deferred']:
        return request['last_result']   # an active plan still owns part of it; retried at the next boundary
    owner.adoption_request = None
    owner.audit('settings_adopt_applied', **request['last_result'])
    return request['last_result']
