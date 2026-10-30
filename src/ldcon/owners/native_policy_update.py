"""Live native policy update while the lock holds its lease (no idle requirement).

Companion of ``native_policy_reload`` (idle-only, artifact-driven). The RPC
``policy_update`` carries only identities: ``device_id``, ``expected_definition``
(the running policy digest), ``laser_id`` and ``decision_id``. The decision
record is written by the dashboard (host process, outside the resident
sandbox) into ``<approvals_root>/<laser_id>/decisions/<decision_id>.json``
after it authorized the change (direct human approval or maintainer
reference). The owner applies only the ``native_current`` leaves of that
decision, only from ``LIVE_ALLOWED``. Laser current/activation/step/slew bounds
are approved operating policy. Physical DAC/ADC capability, calibration, firmware
and wiring remain immutable here. Requested gains and actual gains are distinct.
An integral-limit-only edit uses actual Ki and verified raw-bound writes without
changing coefficients or resetting PI. Coefficient requests remain deferred.

Rule M step 4 (2026-10-27): ``native_ff_sweep`` leaves are adopted here too (proof: ``native_ff_proof.validate_profile``
against the effective policy, no FF session in flight), and rule M step 3: the CLD device's operating conditions
(``devices.<ldc>.absolute_conditions`` / ``control_settings``) are adopted by the laser's CLD authority and installed
on the independent CLD owner as a revisioned projection. The owner's config.json carries no policy copy any more; the
durable record is the authority journal plus the owner state file.

Atomicity: ``dispatch`` runs under the owner ``io_lock`` shared with ``poll``
and every write, so the swap happens between poll cycles. Before the swap the
current hardware state is re-read and checked against the NEW policy
(activation-relative current, measured CLD anchor, full PI envelope at the
current DAC offset and prepared target); any violation refuses the update
with no state change. A journal (``<state_file>.policy-update.json``) records
intent and commit; an incomplete journal blocks the next owner start.
"""
import copy
import json
import os
from pathlib import Path
import re
import stat
import time

from ldcon.core.instrument_client import BrokerError

TARGET_FF_PATHS = frozenset(('native.target_feedforward_scale',
    'native.target_feedforward_scale_bounds', 'native.target_ramp_step_hz', 'native.recentre_integral_fraction'))
MODULATION_PATHS = frozenset(('external_modulation_step_slew_limited',))
FF_PROFILE = 'native_ff_sweep'
LIVE_ALLOWED = frozenset((
    'revision', 'current_bounds_a', 'activation_range_a', 'max_step_a', 'max_slew_a_per_s', 'update_interval_s',
    'max_sample_age_s', 'missing_timeout_s', 'heartbeat_timeout_s',
    'calibration.evidence', 'native.evidence',
    'native.kp_count_per_nm', 'native.ki_count_per_nm_s', 'native.integral_limit_count',
    'native.capture_hz', 'native.acceptance_hz', 'native.arm_enabled', 'native.interval_bounds_s',
    FF_PROFILE)) | TARGET_FF_PATHS | MODULATION_PATHS
GAIN_PATHS = frozenset(('native.kp_count_per_nm', 'native.ki_count_per_nm_s', 'native.integral_limit_count'))
INTEGRAL_PATH = 'native.integral_limit_count'
MARKER = '.control_settings.native_current.'
FF_MARKER = '.control_settings.' + FF_PROFILE + '.'
# Free paths need no human approval record (agent_authority FREE rows). target-ff design (human decisions 2026-10-27):
# the per-step target cap, the FF scale and the post-step integral recentre fraction are free; the scale bounds stay safety.
FREE_PATHS = frozenset(('revision', 'calibration.evidence', 'native.evidence', INTEGRAL_PATH,
                        'native.target_ramp_step_hz', 'native.target_feedforward_scale', 'native.recentre_integral_fraction'))


def journal_path(state_file):
    return Path(str(state_file) + '.policy-update.json')


def check_journal(state_file):
    path = journal_path(state_file)
    if os.path.lexists(path):
        from ldcon.owners.native_policy_reload import read
        value, _ = read(path)
        if value.get('status') == 'failed' and value.get('kind') == 'cld':
            return      # a CLD projection adoption that did not complete; its authority reconciles from readback
        if value.get('status') != 'committed':
            raise ValueError('Incomplete live policy update; offline review required')


def _read_json(path, limit=2**20):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
            raise ValueError('Bounded single-link decision file required')
        return json.loads(stream.read(limit + 1))


def read_decision(root, laser_id, decision_id):
    if not isinstance(laser_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', laser_id):
        raise BrokerError('request', 'Invalid laser id')
    if not isinstance(decision_id, str) or not re.fullmatch(r'[0-9a-f]{32}', decision_id):
        raise BrokerError('request', 'Invalid decision id')
    path = Path(root) / laser_id / 'decisions' / (decision_id + '.json')
    try:
        value = _read_json(path)
    except (OSError, ValueError) as exc:
        raise BrokerError('configuration', 'Decision record unavailable: ' + str(exc)) from exc
    if (not isinstance(value, dict) or value.get('schema_version') != 1 or value.get('id') != decision_id
            or value.get('laser_id') != laser_id or value.get('authority') not in ('free', 'human_approval', 'maintainer')
            or not isinstance(value.get('scope'), dict) or value['scope'].get('kind') != 'lasers'
            or value['scope'].get('name') != laser_id or not isinstance(value.get('changes'), list)):
        raise BrokerError('configuration', 'Invalid decision record')
    return value


def allowed(path):
    """A native-policy-relative path the owner may adopt live (FF profile leaves as a block)."""
    return path in LIVE_ALLOWED or path.split('.', 1)[0] == FF_PROFILE


def policy_changes(decision, *, cld_device=None):
    """Native-policy-relative (path, old, new) rows of a laser-definition decision.

    FF profile leaves come back as ``native_ff_sweep.<leaf>``; the CLD device's leaves (``devices.<ldc>.``) as
    ``cld.<relative>`` when ``cld_device`` names that device.
    """
    rows = []
    cld_prefix = 'devices.' + cld_device + '.' if cld_device else None
    for row in decision['changes']:
        path = row.get('path') if isinstance(row, dict) else None
        if not isinstance(path, str) or not path.startswith('devices.'):
            continue
        if MARKER in path:
            rows.append((path.split(MARKER, 1)[1], row.get('old'), row.get('new')))
        elif FF_MARKER in path:
            rows.append((FF_PROFILE + '.' + path.split(FF_MARKER, 1)[1], row.get('old'), row.get('new')))
        elif cld_prefix and path.startswith(cld_prefix):
            rows.append(('cld.' + path[len(cld_prefix):], row.get('old'), row.get('new')))
    if not rows:
        raise BrokerError('request', 'Decision carries no native policy change')
    return rows


def _canonical_laser_id(owner):
    """Bind approvals to startup laser identity, never the optical source label.

    The assembly factory snapshot was validated against the native physical
    device/policy by monitor_owner.load_native_assembly at startup. Standalone
    owners can use the same host-owned identity as their background authority.
    Conflicting bindings fail closed; no case folding or alias inference.
    """
    host_id = os.environ.get('LDCON_BACKGROUND_LASER_ID')
    assemblies = getattr(owner.system_owner, 'assemblies', None)
    snapshot = getattr(getattr(assemblies, 'factory', None), 'laser_snapshot', None)
    canonical = None
    if snapshot is not None:
        if not isinstance(snapshot, dict):
            raise BrokerError('configuration', 'Invalid startup canonical laser binding')
        canonical = snapshot.get('id')
        binding = (snapshot.get('interfaces') or {}).get('native_current') or {}
        device = (snapshot.get('devices') or {}).get(binding.get('device')) or {}
        if (device.get('type') != 'esp32servocon.native_current.owner'
                or (device.get('connection') or {}).get('device_id') != owner.driver.device_id):
            raise BrokerError('configuration', 'Canonical laser binding names another native device')
        if host_id is not None and host_id != canonical:
            raise BrokerError('configuration', 'Startup canonical laser bindings disagree')
    else:
        canonical = host_id
    if not isinstance(canonical, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', canonical):
        raise BrokerError('configuration', 'A trusted canonical laser identity is required for native path approval')
    return canonical


def _get(document, path):
    for key in path.split('.'):
        if not isinstance(document, dict) or key not in document:
            return None, False
        document = document[key]
    return document, True


def _set(document, path, value):
    parts = path.split('.')
    parent = document
    for key in parts[:-1]:
        parent = parent.setdefault(key, {})
    parent[parts[-1]] = value


def _same(a, b):
    import math
    if type(a) in (int, float) and type(b) in (int, float):
        return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-15)
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        return all(_same(x, y) for x, y in zip(a, b))
    return type(a) is type(b) and a == b


def _check_envelope_with(owner, policy, proof):
    from ldcon.owners.native_policy_safety import check_envelope
    return check_envelope(owner, policy, proof)


def status(owner, request):
    """Read-only exact-decision recovery query; never replays a policy commit."""
    from ldcon.owners.native_policy_reload import read
    if set(request) != {'action', 'device_id', 'laser_id', 'decision_id'}:
        raise BrokerError('request', 'Exact policy transaction identities required')
    path = journal_path(owner.state_file)
    result = dict(status='absent', decision_id=request['decision_id'], policy_digest=owner.definition)
    if not os.path.lexists(path):
        return result
    record, _ = read(path)
    if record.get('decision_id') != request['decision_id'] or record.get('laser_id') != request['laser_id']:
        return result
    result.update(expected_definition=record.get('expected_definition'),
                  approved_definition=record.get('approved_definition'))
    if record.get('status') == 'committed' and record.get('approved_definition') == owner.definition:
        result.update(status='committed', result=copy.deepcopy(record.get('result')))
    else:
        result.update(status='incomplete', reason='Policy commit requires reconciliation')
    return result


def update(owner, request):
    """Compatibility entry; only the laser system authority adopts policy."""
    return owner.system_owner.policy_update(request)


def _apply_rows(candidate, rows, *, optional):
    """Apply (path, old, new) rows; ``optional`` names the paths that may be added (None: any leaf, old None)."""
    for path, old, new in rows:
        current, present = _get(candidate, path)
        optional_addition = (optional is None or path in optional) and not present and old is None
        if not optional_addition and (not present or not _same(current, old)):
            raise BrokerError('configuration', 'Decision old value differs from the running policy at ' + path)
        _set(candidate, path, copy.deepcopy(new))
    return candidate


def _receipt(decision):
    if decision['authority'] in ('human_approval', 'maintainer'):
        return dict(human_identity=dict(name=decision.get('human_name'),
                    protected_decision=decision['id'], authority=decision['authority']),
                    receipt_id=decision['id'])
    return None


def _update(owner, request):
    from ldcon.owners.esp32_native_owner import validate_policy, digest
    from ldcon.owners.native_policy_reload import read, write, check_activation_range
    from ldcon.owners.native_policy_safety import reachable_proofs, gain_state, integral_limit_transition
    from ldcon.owners.native_current_tuning import effective_policy, pid_from, saved_integral_limit
    from ldcon.owners.native_ff_proof import validate_profile
    if set(request) != {'action', 'device_id', 'expected_definition', 'decision_id', 'laser_id'}:
        raise BrokerError('request', 'Exact update identities required; no policy values or paths accepted')
    if request['action'] not in ('policy_update', 'policy_update_prepare'):
        raise BrokerError('request', 'Unknown native policy transaction action')
    root = getattr(owner, 'approvals_root', None)
    if root is None:
        raise BrokerError('action', 'Live policy update is not configured (approvals_root)')
    previous = status(owner, {k: request[k] for k in ('action', 'device_id', 'laser_id', 'decision_id')})
    if previous['status'] == 'committed':
        if previous['expected_definition'] != request['expected_definition']:
            raise BrokerError('configuration', 'Transaction retry differs from its original expected definition')
        if previous.get('result') is None:
            raise BrokerError('configuration', 'Committed transaction has no result; reconcile before replay')
        return dict(previous['result'], idempotent=True)
    if owner.fault or owner.pending:
        raise BrokerError('busy', 'Owner fault or pending write; offline review required')
    if request['expected_definition'] != owner.definition:
        raise BrokerError('configuration', 'Current policy changed; do not replay')
    check_journal(owner.state_file)
    from ldcon.owners.native_policy_reload import check_journal as check_reload_journal
    check_reload_journal(owner.state_file)
    decision = read_decision(root, request['laser_id'], request['decision_id'])
    cld = getattr(owner.system_owner, 'cld', None)
    rows = policy_changes(decision, cld_device=cld.device_key if cld is not None else None)
    cld_rows = [(p[4:], o, n) for p, o, n in rows if p.startswith('cld.')]
    if cld_rows:
        if len(cld_rows) != len(rows):
            raise BrokerError('configuration', 'Adopt the CLD conditions and the native policy in separate decisions')
        return _update_cld(owner, request, decision, cld_rows)
    paths = {path for path, _, _ in rows}
    refused = sorted(p for p in paths if not allowed(p))
    if refused:
        raise BrokerError('configuration', 'Only approved operating policy fields may change live; physical capability, calibration and wiring are immutable: '
                          + ', '.join(refused))
    if paths - FREE_PATHS and decision['authority'] not in ('human_approval', 'maintainer'):
        raise BrokerError('configuration', 'Native operating policy changes require explicit human approval')
    ff_rows = [(p.split('.', 1)[1], o, n) for p, o, n in rows if p.split('.', 1)[0] == FF_PROFILE]
    policy_rows = [(p, o, n) for p, o, n in rows if p.split('.', 1)[0] != FF_PROFILE]
    policy_paths = {p for p, _, _ in policy_rows}
    integral_only = (policy_paths & GAIN_PATHS) == {INTEGRAL_PATH}
    if policy_paths & (TARGET_FF_PATHS | MODULATION_PATHS) or integral_only or ff_rows:
        if request['laser_id'] != _canonical_laser_id(owner):
            raise BrokerError('configuration', 'Native path approval names another laser')
        study = getattr(owner, 'target_ff_study', None)
        active_study = study.status(*owner._target_ff_study_context())['active'] if study else False
        if (active_study or owner.retarget and owner.retarget.get('active')
                or owner.ff and owner.ff.get('state') not in ('restored', 'fault')):
            raise BrokerError('busy', 'Finish the active target/FF experiment before changing its policy')
        if policy_paths & MODULATION_PATHS and (owner.pid_tuning is not None
                or owner.background.coordinator.epoch is not None):
            raise BrokerError('busy', 'Finish the active PI/background trajectory before changing modulation policy')
    candidate = _apply_rows(copy.deepcopy(owner.policy), policy_rows, optional=TARGET_FF_PATHS | MODULATION_PATHS)
    policy = validate_policy(candidate)
    approved = digest(policy)
    # Rule M step 4: the FF profile changes live with the validate_profile proof against the (candidate) policy.
    profile = owner.ff_profile
    if ff_rows:
        if profile is None:
            raise BrokerError('configuration', 'This owner has no native FF profile to update')
        profile = _apply_rows(copy.deepcopy(profile), ff_rows, optional=frozenset())
        try:
            profile = validate_profile(profile, policy)
        except (ValueError, KeyError, TypeError) as exc:
            raise BrokerError('range', 'FF profile proof failed: ' + str(exc)) from exc
    elif profile is not None:
        try:
            validate_profile(profile, policy)       # the effective profile must still be admitted by the new policy
        except (ValueError, KeyError, TypeError) as exc:
            raise BrokerError('range', 'The effective FF profile is not admitted by the candidate policy: ' + str(exc)) from exc
    approved_profile = digest(profile) if profile is not None else None
    if approved == owner.definition and approved_profile == owner.ff_definition:
        return dict(status='unchanged', policy_digest=owner.definition, decision_id=decision['id'],
                    expected_definition=owner.definition, approved_definition=approved, ff_profile_digest=owner.ff_definition)
    check_activation_range(policy)
    interval = getattr(owner, 'interval_s', None)
    if interval is not None and interval > policy['max_sample_age_s']:
        raise BrokerError('range', 'Owner poll interval exceeds the new native freshness budget')
    gains = policy_paths & GAIN_PATHS
    if gains and owner.pid_tuning is not None:
        raise BrokerError('busy', 'Finish the active gain comparison before replacing its requested baseline; range edits remain available')
    # Configuration is stable under io_lock; autonomous DAC motion is expected.
    # Both readings and ALL already-admitted future trajectories must fit.
    hot_integral = integral_only and bool(owner.lease and owner.prepared and owner.prepared.get('mode') == 'pi')
    before = owner._read_before(require_input=hot_integral and owner.active)
    proofs = reachable_proofs(owner, policy, before)
    state, state_sha = read(owner.state_file)
    if state['definition'] != owner.definition:
        raise BrokerError('configuration', 'Durable and running policy differ; offline review required')
    after = owner._read_before(require_input=hot_integral and owner.active)
    if after['servo_config'] != before['servo_config'] or after.get('dac_limits') != before.get('dac_limits'):
        raise BrokerError('busy', 'Hardware configuration changed during validation; no policy update')
    if not owner.active and after['dac']['native_code'] != before['dac']['native_code']:
        raise BrokerError('busy', 'PI-off fixed output changed during validation; no policy update')
    proofs += reachable_proofs(owner, policy, after)
    transition = integral_limit_transition(owner, policy, after) if hot_integral else None
    retained_pid = None
    if integral_only:
        # Preserve coefficients already adopted by the owner. An idle owner uses
        # its next-prepare coefficients; no dormant/reset hardware is rewritten.
        if hot_integral:
            retained_pid = pid_from(after['servo_config'])
        else:
            n = effective_policy(owner.policy, owner.saved_pid)['native']
            retained_pid = dict(kp=n['kp_count_per_nm'], ki=n['ki_count_per_nm_s'], kd=0.)
        retained_pid = saved_integral_limit(policy, retained_pid)
    if transition:
        proofs += transition['proofs']
        if owner.clock() - owner.last_write < max(owner.policy['update_interval_s'], policy['update_interval_s']):
            raise BrokerError('measurement', 'Integral limit update interval has not elapsed; no mutation attempted')
    measured = (getattr(owner, 'bias_sample', None) or {}).get('sample', {}).get('current_read_a')
    if measured is not None:
        lo, hi = policy['current_bounds_a']
        if not lo <= measured <= hi:
            raise BrokerError('range', 'Measured CLD current exceeds the candidate absolute current range')
        anchor = getattr(owner, 'activation_measured_a', None)
        if anchor is not None and not anchor + policy['activation_range_a'][0] <= measured <= anchor + policy['activation_range_a'][1]:
            raise BrokerError('range', 'Measured CLD current exceeds the candidate relative current range')
    if read(owner.state_file)[1] != state_sha:
        raise BrokerError('configuration', 'Configuration changed during validation')
    prepared_result = dict(status='prepared', decision_id=decision['id'], authority=decision['authority'],
        expected_definition=owner.definition, approved_definition=approved, policy_digest=owner.definition,
        ff_profile_digest=owner.ff_definition, approved_ff_profile_digest=approved_profile,
        changed_paths=sorted(paths), hardware_writes=False, leased=bool(owner.lease),
        native_pi_active=bool(owner.active), dac_code=after['dac']['native_code'], proofs=proofs,
        integral_limit_transition=transition)
    if request['action'] == 'policy_update_prepare':
        return prepared_result
    intent = dict(status='intent', kind='native', decision_id=decision['id'], laser_id=request['laser_id'], authority=decision['authority'],
                  expected_definition=owner.definition, approved_definition=approved,
                  expected_ff_profile_digest=owner.ff_definition, approved_ff_profile_digest=approved_profile,
                  changed_paths=sorted(paths), state_before=state,
                  started_unix_s=time.time(), hardware_writes=False,
                  hardware_before=copy.deepcopy(after['servo_config']), integral_limit_transition=transition)
    journal = journal_path(owner.state_file)
    try:
        write(journal, intent)
        if transition:
            # The physical owner independently rechecks its unchanged projection.
            # Bounds only: no reset, target, offset, gains, enable or DAC-cap edit.
            intent.update(hardware_writes=True, hardware_stage='intent')
            write(journal, intent)
            with owner.driver.authorized(owner.system_owner.authority):
                after = owner._write(transition['values'], after, 'verified integral output-limit hot apply')
            intent.update(hardware_stage='verified', hardware_after=copy.deepcopy(after['servo_config']))
            write(journal, intent)
        new_state = copy.deepcopy(state)
        new_state['definition'] = approved
        new_state['ff_profile'] = profile
        new_state['ff_profile_digest'] = approved_profile
        # Coefficient requests retain their existing next-prepare behavior. A
        # limit-only request retains the actual coefficients and new raw bounds.
        if gains:
            new_state['saved_pid'] = retained_pid
        def commit(candidate, proof):
            write(owner.state_file, new_state)
            if ff_rows:
                owner.system_owner.set_ff_profile(profile)
            if gains:
                owner.saved_pid = retained_pid
        receipt = _receipt(decision)
        if approved != owner.definition:
            owner.system_owner.adopt(policy, receipt=receipt, expected_revision=owner.definition,
                                     prepare=lambda candidate: {'reachable_proofs': proofs}, commit=commit)
        else:
            commit(policy, None)           # FF-profile-only decision: the policy document is unchanged
        applied = gain_state(owner, after)
        result = dict(prepared_result, status='updated', policy_digest=approved, ff_profile_digest=approved_profile,
                      gain_state=applied, hardware_gains_applied=applied['hardware_gains_applied'],
                      gains_pending=applied['gains_pending'], firmware_caps_changed=False,
                      hardware_writes=bool(transition), integral_limit_applied=bool(transition),
                      note=('Integral output bounds verified with actual Ki; coefficients, PI enable, target, offset and physical caps preserved; no integrator reset.' if transition else
                            'Laser operating policy adopted; PI/FF, lease and physical caps preserved. Requested coefficient changes apply through verified prepare or tuning.'))
        intent.update(status='committed', finished_unix_s=time.time(), result=result)
        write(journal, intent)
    except Exception as error:
        owner.pending = {'kind': 'policy_update', 'decision_id': decision['id']}
        raise owner._latch('Live policy update persistence incomplete; offline review required') from error
    return result


def _update_cld(owner, request, decision, rows):
    """Rule M step 3: adopt the laser's CLD operating conditions and re-project them to the CLD owner (no restart)."""
    from ldcon.owners.cld_projection import cld_constraints, CLD_LIVE_PREFIXES
    from ldcon.owners.native_policy_reload import read, write
    system = owner.system_owner
    authority, client = system.cld_authority, system.cld
    paths = {path for path, _, _ in rows}
    refused = sorted(p for p in paths if not any(p.startswith(prefix) for prefix in CLD_LIVE_PREFIXES))
    if refused:
        raise BrokerError('configuration', 'Only the CLD operating conditions may change live: ' + ', '.join(refused))
    classes = {row.get('class') for row in decision['changes'] if isinstance(row, dict)}
    if classes - {'free'} and decision['authority'] not in ('human_approval', 'maintainer'):
        raise BrokerError('configuration', 'CLD operating condition changes require explicit human approval')
    candidate = _apply_rows(copy.deepcopy(authority.snapshot()), rows, optional=None)
    try:
        constraints = cld_constraints(candidate)
    except (ValueError, KeyError, TypeError) as exc:
        raise BrokerError('range', 'CLD conditions proof failed: ' + str(exc)) from exc
    approved = authority._revision(candidate)
    expected = authority.revision
    base = dict(decision_id=decision['id'], authority=decision['authority'], kind='cld',
                expected_definition=owner.definition, approved_definition=owner.definition, policy_digest=owner.definition,
                cld_expected_revision=expected, cld_approved_revision=approved, changed_paths=sorted('cld.' + p for p in paths),
                hardware_writes=False, leased=bool(owner.lease), native_pi_active=bool(owner.active))
    if approved == expected:
        return dict(base, status='unchanged')
    if request['action'] == 'policy_update_prepare':
        return dict(base, status='prepared', constraints=constraints)
    state, state_sha = read(owner.state_file)
    if state['definition'] != owner.definition:
        raise BrokerError('configuration', 'Durable and running policy differ; offline review required')
    intent = dict(base, status='intent', laser_id=request['laser_id'], state_before=state, started_unix_s=time.time())
    journal = journal_path(owner.state_file)
    try:
        write(journal, intent)
        system.adopt_cld(candidate, receipt=_receipt(decision), expected_revision=expected)
        result = dict(base, status='updated', projection=client.describe(),
                      note='CLD operating conditions adopted and re-projected to the CLD owner; no CLD restart, no output write.')
        intent.update(status='committed', finished_unix_s=time.time(), result=result)
        write(journal, intent)
    except Exception as error:
        # The CLD authority keeps its adoption pending for reconcile (cld_tick, from the CLD readback); the native
        # lease and PI are untouched, so the native owner is not latched. The journal records the failure for the
        # dashboard's policy_update_status (reconciliation_required) and does not block the next start.
        intent.update(status='failed', finished_unix_s=time.time(), error=str(error))
        try:
            write(journal, intent)
        except OSError:
            pass
        raise BrokerError('configuration', 'CLD policy adoption did not complete (the CLD authority reconciles from readback): '
                          + str(error)) from error
    return result
