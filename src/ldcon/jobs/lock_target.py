"""Target wavelength/frequency: ONE source and ONE card to change it (rule F, human decision 2026-10-22).

Single source: the laser definition's top-level ``target`` block::

    target:
      frequency_hz: 710962838507871.5     # the lock target, the recovery/procedure target, the window-centre target
      allowed_change_hz: [-lo, +hi]       # admitted change relative to the saved target (legacy bounds_hz: [lo, hi] absolute)
      max_change_hz: 2.0e9                # the largest change one card run may request

Cards whose ``target_frequency_hz`` parameter is null take it from here (``resolve_target``); the recovery procedure,
the window-centre session and the FF/sweep proxies use the lock card's copy, which follows the owner's ramped target.

``tune.lock_target`` changes it: it validates the new value against ``allowed_change_hz``/``max_change_hz``, saves the laser
definition through the dashboard configuration API (the live-change path: the dashboard queues a laser reload to the
running lock job; the lock worker calls the owner's bounded target ramp; nothing restarts), then waits for the ramp
and verifies frequency arrival at the new target (``verify_samples`` fresh samples within ``tolerance_hz``), for both
lasers. The ECDL's TEC centering (the outer loop returning the PZT to its centre) continues in the background and is
REPORTED (``verify.tec_centering``, the live state) -- never a completion gate (rule K; human decision 2026-10-27,
which replaced the 2026-10-26 thermal gate). The owners report the room for the next step (``pzt_headroom_hz`` /
``dac_headroom_hz``); queued Web steps are capped by min(saved cap, headroom). Optical quality is reported
independently, never a completion gate for a manual target change. Without an active lock job, save the final target
once within the admitted target range, without a step cap, split, hardware command or settling wait (the next lock
start uses it).
"""
import copy
import math
from ldcon.core.target_policy import laser_target, target_bounds, validate_destination
import time
from pathlib import Path

from ldcon.core.contracts import DeviceError
from ldcon.core.interface_cards import CardCancelled

C_NM = 299792458e9
DEFAULTS = dict(target_frequency_hz=None, target_wavelength_nm=None, offset_hz=None, tolerance_hz=None,
                verify_samples=5, sample_interval_s=1., timeout_s=900., max_change_hz=None, queue_steps=False)
PATH = ('target', 'frequency_hz')


def _finite(value, name, *, positive=False):
    if type(value) not in (int, float) or not math.isfinite(value) or (positive and value <= 0):
        raise ValueError(name + (' must be positive' if positive else ' must be finite'))
    return float(value)


def validate(p, *, require_target=True):
    p = copy.deepcopy(p)
    if set(p) - DEFAULTS.keys():
        raise ValueError('Unknown lock-target parameter: ' + ', '.join(sorted(set(p) - DEFAULTS.keys())))
    for key in DEFAULTS:
        p.setdefault(key, DEFAULTS[key])
    if type(p['queue_steps']) is not bool:
        raise ValueError('queue_steps must be boolean')
    given = [k for k in ('target_frequency_hz', 'target_wavelength_nm', 'offset_hz') if p[k] is not None]
    if len(given) > 1:
        raise ValueError('Give one of target_frequency_hz, target_wavelength_nm or offset_hz')
    if require_target and not given:
        raise ValueError('A new target (frequency, wavelength or offset) is required')
    for key in ('target_frequency_hz', 'target_wavelength_nm', 'tolerance_hz', 'max_change_hz'):
        if p[key] is not None:
            _finite(p[key], key, positive=True)
    if p['offset_hz'] is not None and _finite(p['offset_hz'], 'offset_hz') == 0:
        raise ValueError('offset_hz must be nonzero')
    _finite(p['sample_interval_s'], 'sample_interval_s', positive=True)
    _finite(p['timeout_s'], 'timeout_s', positive=True)
    if not .1 <= p['sample_interval_s'] <= 5 or p['timeout_s'] > 86400:
        raise ValueError('sample_interval_s must be in [0.1, 5] and timeout_s <= 24 h')
    if type(p['verify_samples']) is not int or not 0 <= p['verify_samples'] <= 100:
        raise ValueError('verify_samples must be 0..100')
    return p


def parameters(card, overrides):
    if not isinstance(card.get('parameters'), dict) or not isinstance(overrides, dict):
        raise ValueError('Lock-target parameters must be mappings')
    return validate({**card['parameters'], **overrides}, require_target=False)


# ------------------------------------------------------------------------------------------ single source


def resolve_target(laser, p, *, keys=('target_frequency_hz', 'target_wavelength_nm')):
    """A card's target: its own parameter when pinned, else the laser's single source, else None."""
    hz = p.get(keys[0]) if p else None
    nm = p.get(keys[1]) if p and len(keys) > 1 else None
    if hz is not None:
        return float(hz)
    if nm is not None:
        return C_NM / float(nm)
    block = laser_target(laser)
    return float(block['frequency_hz']) if block else None


def check_change(laser, new_hz, *, max_change_hz=None, enforce_step=True):
    block = laser_target(laser)
    if block is None:
        raise DeviceError('The laser definition has no target block; a maintainer must add target.frequency_hz first')
    present = float(block['frequency_hz'])
    try:
        validate_destination(laser, new_hz)
    except ValueError as error:
        raise DeviceError(str(error)) from error
    if not enforce_step:
        return present
    cap = max_change_hz if max_change_hz is not None else block.get('max_change_hz')
    if cap is None:
        raise DeviceError('No max_change_hz (card or target.max_change_hz) is configured')
    if abs(new_hz - present) > cap * (1 + 1e-12):
        raise DeviceError('Change of %.1f MHz exceeds the cap %.1f MHz' % (abs(new_hz - present) / 1e6, cap / 1e6))
    return present


def edit_scalar_text(text, path, value):
    """Replace only the parsed numeric scalar at ``path`` (tuple of mapping keys) in block/flow YAML or JSON.

    Preserve comments and unrelated numeric spelling; configuration saves may
    legitimately use JSON (a YAML subset). Reject ambiguous paths or aliases that
    would change another field instead of guessing from indentation or regexes.
    """
    import yaml
    name = '.'.join(path)
    value = _finite(value, name, positive=True)
    try:
        node = yaml.compose(text, Loader=yaml.SafeLoader)
        before = yaml.safe_load(text)
        for key in path:
            if not isinstance(node, yaml.MappingNode):
                raise ValueError('Path must contain explicit mappings')
            matches = [v for k, v in node.value if isinstance(k, yaml.ScalarNode)
                       and k.tag == 'tag:yaml.org,2002:str' and k.value == key]
            if len(matches) != 1:
                raise ValueError('Path must be present and unambiguous')
            node = matches[0]
        if not isinstance(node, yaml.ScalarNode) or node.tag not in (
                'tag:yaml.org,2002:int', 'tag:yaml.org,2002:float'):
            raise ValueError('Value must be a numeric scalar')
        existing = before
        for key in path:
            existing = existing[key]
        _finite(existing, 'existing ' + name, positive=True)
        edited = text[:node.start_mark.index] + repr(value) + text[node.end_mark.index:]
        expected = copy.deepcopy(before)
        # Break any shared YAML mapping identity before constructing the intent.
        parent = expected
        for key in path[:-1]:
            parent[key] = dict(parent[key])
            parent = parent[key]
        parent[path[-1]] = value
        if yaml.safe_load(edited) != expected:
            raise ValueError('Edit would change another configuration field')
        return edited
    except (yaml.YAMLError, ValueError, KeyError, TypeError) as error:
        raise DeviceError('Cannot edit %s: %s' % (name, error)) from error


def edit_target_text(text, new_hz):
    """Replace only the parsed target scalar (``target.frequency_hz``); see ``edit_scalar_text``."""
    return edit_scalar_text(text, PATH, new_hz)


# ------------------------------------------------------------------------------------------ card
def running_lock(directory, laser):
    """The running lock job of this laser, locked by controller_state OR by its own fresh per-cycle report.

    2026-10-22 18:04Z: the sr422 steady-lock card's controller_state is a display state that flips to
    'waiting_for_measurement' for ~1.3 s on each wavelength-meter dropout (and 'tracking'/'mode_degraded'); the lock
    keeps running. Taking such a lock as 'unlocked' would save the target as if unlocked while the dashboard still
    queues the reload to the running lock (unverified ramp). lock-target.json 'state' comes from the owner row."""
    from ldcon.jobs.operating_point import find_running_lock, lock_operation
    from ldcon.core.records import read_json
    found = find_running_lock(directory, laser)
    if found is not None:
        return found
    try:
        operation = lock_operation(laser)
    except Exception:
        return None
    for path in sorted(Path(directory).parent.glob('*/job.json')):
        meta = read_json(path, {})
        if meta.get('id') == Path(directory).name or (meta.get('laser') or {}).get('id') != laser['id']:
            continue
        if not any(c.get('operation', c.get('card')) == operation for c in meta.get('cards', [])):
            continue
        progress = read_json(path.parent / 'progress.json', {})
        # Display quality/freshness is not proof that a controller is stopped.
        # Include a starting/stopping lock: its live-reload path must own any change.
        if progress.get('status') not in ('cancelled', 'failed', 'interrupted', 'succeeded',
                                          'completed', 'completed_with_failures'):
            return path.parent, meta, progress
    return None


RECENTRE_INTEGRAL_FRACTION = .9
MAX_RECENTRES = 3


def _file_recentre(lock_directory, lock_id, index):
    import uuid
    from ldcon.jobs.operating_point import OperatingPointQueue
    rid = uuid.uuid4().hex
    OperatingPointQueue(lock_directory).submit(dict(
        request_id=rid, job_id=lock_id, source='tune.lock_target (integral fraction > %.2f)' % RECENTRE_INTEGRAL_FRACTION,
        parameters=dict(moves=[dict(axis='current', mode='locked_reference', target=dict(reference='integral_neutral'))],
                        verify_samples=0)))
    return rid


def _recentre_pending(lock_directory, recentres):
    """The last filed re-centre is still queued or running in the lock."""
    if not recentres:
        return False
    from ldcon.jobs.operating_point import OperatingPointQueue
    status = OperatingPointQueue(lock_directory).read_status(recentres[-1]) or {}
    return status.get('status') not in ('succeeded', 'failed', 'rejected', 'cancelled')


def optical_gate(laser):
    """(desired mode id, optical.single_mode_max) of the laser's lock device (ECDL); ('desired', {}) when absent."""
    try:
        from ldcon.jobs.operating_point import lock_device
        settings = (lock_device(laser)[2].get('control_settings') or {})
    except Exception:
        return 'desired', {}
    gate = settings.get('entry_gate') or {}
    return gate.get('mode_id', 'desired'), (settings.get('optical') or {}).get('single_mode_max') or {}


def trace_degraded(lock_directory, laser, fresh_s=30.):
    """'Clearly degraded' from the lock's live trace (locks whose worker predates the 'degraded' field in
    lock-target.json): the newest row's fringe metrics above optical.single_mode_max. None when unknown."""
    from ldcon.core.records import read_json
    rows = read_json(Path(lock_directory) / 'live-trace.json', []) or []
    if not rows:
        return None
    row = rows[-1]
    stamp = row.get('acquired_s')
    if not isinstance(stamp, (int, float)) or time.time() - stamp > fresh_s:
        return None
    limits = optical_gate(laser)[1]
    metrics = row.get('fringe_metrics') or {}
    return any(type(metrics.get(k)) in (int, float) and metrics[k] > v for k, v in limits.items())


def sample_mode_ok(state, laser, lock_directory):
    """Report whether a mode/quality problem is detected; this does not gate target completion.

    Missing quality information is not evidence of single-mode operation. Preserve the raw flags in the result.
    """
    degraded = state.get('degraded')
    if degraded is None:
        degraded = trace_degraded(lock_directory, laser)
    return degraded is not True


def run_lock_target(directory, laser, card, p, *, clock, sleep, cancel, emit, publish=None, api=None):
    from ldcon.jobs.dashboard_api import Dashboard
    from ldcon.jobs.operating_point import find_running_lock
    from ldcon.core.records import read_json
    p = validate(p)
    api = api or Dashboard.from_job_directory(directory)
    # target-ff design (2026-10-27): a study.target_response that saved a TRIAL per-step cap and did not finish
    # (crash/interruption) left it in the laser yaml; restore the saved values before using the cap here.
    from ldcon.jobs.target_response_shift import restore_pending_ceiling
    restore_pending_ceiling(Path(directory).parent, laser, api, emit=emit, actor='card:' + card['id'],
                            self_id=Path(directory).name)
    found = running_lock(directory, laser)
    if p['queue_steps'] and found is not None:
        from ldcon.jobs.target_sequence import run_target_sequence
        return run_target_sequence(directory, laser, card, p, clock=clock, sleep=sleep,
                                   cancel=cancel, emit=emit, publish=publish, api=api)
    publish = publish or (lambda value: None)
    present = laser_target(laser)
    if present is None:
        raise DeviceError('The laser definition has no target block; a maintainer must add target.frequency_hz first')
    if p['target_frequency_hz'] is not None:
        new_hz = float(p['target_frequency_hz'])
    elif p['target_wavelength_nm'] is not None:
        new_hz = C_NM / float(p['target_wavelength_nm'])
    else:
        new_hz = float(present['frequency_hz']) + float(p['offset_hz'])
    before_hz = check_change(laser, new_hz, max_change_hz=p['max_change_hz'], enforce_step=found is not None)
    lock_directory = meta = None
    if found is not None:
        lock_directory, meta, progress = found
        params = (meta.get('cards') or [{}])[0].get('params', {})
        if params.get('target_frequency_hz') is not None or params.get('target_wavelength_nm') is not None:
            raise DeviceError('Lock job %s pins its own target_frequency_hz; set it to null in the assignment so the lock follows the laser target' % meta.get('id', '')[:8])
        live = read_json(Path(lock_directory) / 'laser-live.json', {})
        if not live.get('supported'):
            raise DeviceError('The running lock worker predates live laser reload; relock with the current release first')
    emit('lock_target_plan', dict(before_hz=before_hz, new_hz=new_hz, change_hz=new_hz - before_hz, locked=found is not None, lock_job=meta.get('id') if meta else None))
    current = api.get_configuration('lasers', laser['id'])
    text = current.get('yaml')
    if text is None:
        raise DeviceError('The dashboard did not return the laser YAML text')
    if abs(float((current['document'].get('target') or {}).get('frequency_hz', 0.)) - before_hz) > 1e-6:
        raise DeviceError('The saved laser target differs from this job\'s copy; reload and retry')
    edited = edit_target_text(text, new_hz)
    # A round trip can restore an earlier content revision. Remember the receipts already present before this
    # submission so even an older dashboard without a returned request_id cannot acknowledge it with history.
    previous_results = {}
    if lock_directory is not None:
        previous_results = {path.name: read_json(path, {}).get('request_id')
                            for path in Path(lock_directory).glob('laser-reload-result-*.json')}
    if cancel():
        raise CardCancelled('Target change cancelled before save; no target changed')
    if found is None and running_lock(directory, laser) is not None:
        raise CardCancelled('A lock started while preparing the unlocked target save; read current status')
    submitted_unix_s = time.time()
    saved = api.put_configuration('lasers', laser['id'], edited, current['revision'], actor='card:' + card['id'])
    revision_after = saved.get('revision')
    emit('lock_target_saved', dict(revision_before=current['revision'], revision_after=revision_after, new_hz=new_hz))
    publish(dict(status='saved', target_frequency_hz=new_hz, revision=revision_after))
    if found is None:
        return dict(status='succeeded', locked=False, before_hz=before_hz, target_frequency_hz=new_hz, revision=revision_after,
                    note='laser unlocked: the saved target applies at the next lock start')
    # Fail fast when the dashboard did not queue the reload for the running lock (worker revision differs, a reload
    # is still pending, or the worker predates live reload): no reload result will ever come, so waiting until
    # timeout_s only delays the failure. The saved target stands either way.
    queued = ((saved.get('live') or {}).get('jobs') or {}).get(meta.get('id'))
    if isinstance(queued, dict) and queued.get('status') in ('rejected', 'unsupported_worker'):
        raise DeviceError('The dashboard did not queue the live reload for lock job %s: %s (the saved target stands; '
                          'the lock keeps its previous target until it adopts a reload or restarts)'
                          % (meta.get('id', '')[:8], queued.get('reason') or queued.get('status')))
    request_id = queued.get('request_id') if isinstance(queued, dict) else None
    # Locked: the dashboard queued a laser reload for the lock job; the worker adopts it at its cycle boundary and
    # calls the owner's bounded ramp. Wait for the reload result, then for the ramp, then verify.
    started = clock()
    reload_result = None
    while reload_result is None:
        if cancel():
            raise CardCancelled('Lock-target change cancelled while waiting for the lock worker (the saved target stands)')
        for path in sorted(Path(lock_directory).glob('laser-reload-result-*.json'), key=lambda x: x.stat().st_mtime, reverse=True):
            result = read_json(path, {})
            if (result.get('revision_before') != current['revision']
                    or result.get('revision_after') != revision_after):
                continue
            if request_id:
                if result.get('request_id') != request_id:
                    continue
            else:
                stamp = result.get('updated_unix_s')
                if (path.name in previous_results or not result.get('request_id')
                        or result.get('request_id') in previous_results.values()
                        or type(stamp) not in (int, float) or not math.isfinite(stamp)
                        or stamp < submitted_unix_s):
                    continue
            reload_result = result
            break
        if reload_result is None:
            if clock() - started > p['timeout_s']:
                raise DeviceError('The lock worker did not adopt the saved target within the timeout (saved target stands)')
            sleep(p['sample_interval_s'])
    if reload_result.get('status') != 'applied' or 'target.frequency_hz' not in reload_result.get('applied', []):
        raise DeviceError('The lock worker did not apply the target live: %s %s' % (reload_result.get('status'), reload_result.get('reason') or reload_result.get('snapshot')))
    accepted_unix_s = reload_result.get('updated_unix_s')
    if type(accepted_unix_s) not in (int, float) or not math.isfinite(accepted_unix_s):
        accepted_unix_s = time.time()
    accepted_unix_s = max(submitted_unix_s, accepted_unix_s)
    publish(dict(status='ramping', target_frequency_hz=new_hz))
    samples = []
    tolerance = p['tolerance_hz']
    recentres = []
    completion = None
    completion_checked = False
    # ECDL TEC centering is REPORTED only (human decision 2026-10-27, rule K): the Completion tracker keeps producing
    # the same evidence record (status centering/confirming/settled...) for the result and the live state, but it no
    # longer resets the frequency samples or gates completion.
    from ldcon.owners.ecdl_target_settling import Completion as TECompletion, required as needs_tec_center
    thermal_completion = TECompletion() if needs_tec_center(laser) else None
    thermal = None

    def report(state):
        """Report-only fields of the owner's ramp (headroom, integral usage, hybrid recentre) for publish/verify."""
        return dict(tec_centering=thermal,
                    pzt_headroom_hz=state.get('pzt_headroom_hz'), dac_headroom_hz=state.get('dac_headroom_hz'),
                    integral_fraction=state.get('integral_fraction'),
                    owner_recentre=state.get('owner_recentre'), recentre_active=state.get('recentre_active'),
                    ldc_feedforward=state.get('ldc_feedforward'))

    while True:
        if cancel():
            raise CardCancelled('Target-change observation cancelled; the saved target stands and any owner ramp continues')
        if clock() - started > p['timeout_s']:
            raise DeviceError('The lock did not settle at the new target within the timeout')
        state = read_json(Path(lock_directory) / 'lock-target.json', {})
        stamp = state.get('updated_unix_s')
        fresh = (type(stamp) in (int, float) and math.isfinite(stamp)
                 and stamp >= accepted_unix_s and 0 <= time.time() - stamp <= 30)
        # The worker can publish a pre-reload row with its newly updated requested-target label. Only the
        # owner's actual ramped target establishes completion; an old sample must not trigger a re-centre either.
        at_target = fresh and not state.get('ramp_active') and all(
            type(state.get(key)) in (int, float) and math.isfinite(state[key])
            and abs(state[key] - new_hz) <= 1e-3 for key in ('target_frequency_hz', 'ramped_target_hz'))
        if thermal_completion:
            thermal = thermal_completion.poll(state, accepted_unix_s=accepted_unix_s,
                                              now=time.time(), at_target=at_target)
        if at_target and not completion_checked:
            # A response study deliberately observes a transient without using
            # the criterion it is trying to learn (no circular stopping rule).
            from ldcon.jobs.response_algorithms import ALGORITHMS
            if card.get('operation') not in (*ALGORITHMS, 'measure.change', 'measure.batch'):
                from ldcon.jobs.settling_profile import Completion
                completion=Completion(directory,laser,new_hz,max(accepted_unix_s,stamp),origin_hz=before_hz)
            completion_checked=True
        if completion and completion.enabled:
            if api.get_configuration('lasers',laser['id'])['revision'] != revision_after:
                raise CardCancelled('A newer target/configuration superseded this completion observation')
            if at_target:
                learned=completion.poll(time.time())
                if learned['status']=='settled':
                    verify=dict(learned,ok=True,tec_centering_used_for_completion=False,**report(state))
                    emit('lock_target_verified',verify)
                    return dict(status='succeeded',locked=True,lock_job=meta.get('id'),before_hz=before_hz,
                        target_frequency_hz=new_hz,revision=revision_after,verify=verify,
                        reload_request_id=reload_result.get('request_id'),accepted_unix_s=accepted_unix_s)
        if samples and time.time() - samples[-1]['unix_s'] > 30:
            samples = []
        if at_target:
            f = state.get('frequency_hz')
            if (type(f) in (int, float) and math.isfinite(f) and f > 0
                    and (not samples or stamp > samples[-1]['unix_s'])):
                samples.append(dict(frequency_hz=f, error_hz=f - new_hz, single_mode=state.get('single_mode'), mode_id=state.get('mode_id'),
                                    mode_ok=sample_mode_ok(state, laser, lock_directory), degraded=state.get('degraded'), unix_s=stamp,
                                    integral_fraction=state.get('integral_fraction')))
                samples = samples[-max(1, p['verify_samples']):]
                # The owner has adopted the target and ended its bounded ramp. Frequency arrival and optical
                # quality are independent: mode_degraded must not leave BLACS waiting on an applied change.
                ok = len(samples) >= max(1, p['verify_samples'])
                if tolerance is not None:
                    ok = ok and max(abs(s['error_hz']) for s in samples) <= tolerance
                if ok and not (completion and completion.enabled):
                    verify = dict(samples=len(samples), max_abs_error_hz=max(abs(s['error_hz']) for s in samples), ok=True,
                                  completion_basis='frequency_arrival', optical_quality_used_for_completion=False,
                                  tec_centering_used_for_completion=False,
                                  mode_quality_ok=all(s['mode_ok'] for s in samples),
                                  max_integral_fraction=max((s['integral_fraction'] for s in samples
                                                             if isinstance(s['integral_fraction'], (int, float))), default=None),
                                  quality_samples=[{k: s[k] for k in ('unix_s', 'single_mode', 'mode_id', 'degraded')} for s in samples],
                                  **report(state))
                    emit('lock_target_verified', verify)
                    return dict(status='succeeded', locked=True, lock_job=meta.get('id'), before_hz=before_hz,
                                target_frequency_hz=new_hz, revision=revision_after, verify=verify,
                                reload_request_id=reload_result.get('request_id'), accepted_unix_s=accepted_unix_s)
        if (at_target and not (completion and completion.enabled) and state.get('automatic_recentre', True)
                and isinstance(state.get('integral_fraction'), (int, float))
                and state['integral_fraction'] > RECENTRE_INTEGRAL_FRACTION and len(recentres) < MAX_RECENTRES
                and not _recentre_pending(lock_directory, recentres)):
            # 2026-10-22 live finding: a ~15 % retarget feed-forward error saturated the integral (fraction ~0.98), the
            # error stayed above tolerance and this card waited out its timeout. Rule F: file the SAME
            # tune.operating_point procedure (target integral_neutral) into the running lock instead.
            # (sr1092 since 2026-10-27: the owner's own hybrid background recentre runs instead; the owner reports
            # automatic_recentre=False so this card-side path stays off there.)
            recentres.append(_file_recentre(lock_directory, meta.get('id'), len(recentres)))
            samples = []
            emit('lock_target_recentre', dict(integral_fraction=state['integral_fraction'], request_id=recentres[-1]))
        publish(dict(status='ramping' if state.get('ramp_active') else 'verifying', target_frequency_hz=new_hz,
                     recentres=len(recentres),
                     ramped_target_hz=state.get('ramped_target_hz'), frequency_hz=state.get('frequency_hz'),
                     samples=len(samples), **report(state)))
        sleep(p['sample_interval_s'])
