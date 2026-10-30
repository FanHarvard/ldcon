"""Per-step target-shift search and promotion of the saved cap (study.target_response ``shift_hz``).

Target-ff design 2026-10-27 (``preparations/target-ff-20261027/design.md`` section 2.5, human decisions 1-2: the
per-step cap and the slew are FREE operating parameters). The study asks the running lock for round trips of a
shift S (+S then back to the starting target, through the ordinary ``tune.lock_target`` route), judges every round
with the frozen settling profile, and promotes the largest shift that succeeded in BOTH directions:

    max_change_hz := min(requested, S_ok)      and the lock owner's per-step key (steady_lock.target_ramp_step_hz /
                                               native_current.native.target_ramp_step_hz) := the same value.

A trial above the saved cap first SAVES a temporary cap through the revision-checked configuration API (the owner's
``TargetFFStudy.set`` and ``lock_target.check_change`` enforce the saved value), recorded as ``basis: trial`` in the
study's ``target-response-ceiling.json``; the study ends with ``adopted`` or ``restored``. Every save carries an
audit row (basis, before/after, revisions, study id) in ``shift-adoption.json`` and the configuration ledger's
actor ``card:<study>:shift:<basis>``. An interrupted study leaves the ``trial`` record; the next ``tune.lock_target``
restores the saved values before using the cap (``restore_pending_ceiling``).

Success of one round (design 2.5 step 5): both legs settled under the frozen frequency criterion, settling time
<= settling_goal_s, the starting target restored and confirmed, no branch hop (mode_id), sr1092: the integral
fraction stayed below ``abort_integral_fraction_max``, sr422: the PZT was never reported lost. Optical quality
(single_mode, degraded) is REPORTED only (rule K).
"""
import copy
import math
import time
from pathlib import Path

from ldcon.core.contracts import DeviceError
from ldcon.core.records import atomic_json, read_json

CEILING_FILE = 'target-response-ceiling.json'
AUDIT_FILE = 'shift-adoption.json'
TERMINAL = ('cancelled', 'failed', 'interrupted', 'succeeded', 'completed', 'completed_with_failures', 'completed_unsettled')
SEARCH_DEFAULTS = dict(mode='bisect', min_hz=None, max_rounds=6, resolution_hz=10e6)
REQUEST_PATH = ('target', 'max_change_hz')


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _get(document, path):
    for key in path:
        if not isinstance(document, dict) or key not in document:
            return None
        document = document[key]
    return document


def validate_search(value):
    """``shift_search``: {mode: bisect|fixed, min_hz, max_rounds, resolution_hz} with the module defaults."""
    if value is None:
        return dict(SEARCH_DEFAULTS)
    if not isinstance(value, dict) or set(value) - set(SEARCH_DEFAULTS):
        raise ValueError('shift_search accepts mode, min_hz, max_rounds and resolution_hz only')
    p = {**SEARCH_DEFAULTS, **value}
    if p['mode'] not in ('bisect', 'fixed'):
        raise ValueError('shift_search.mode must be bisect or fixed')
    if p['min_hz'] is not None and (not _finite(p['min_hz']) or p['min_hz'] <= 0):
        raise ValueError('shift_search.min_hz must be positive')
    if type(p['max_rounds']) is not int or not 1 <= p['max_rounds'] <= 12:
        raise ValueError('shift_search.max_rounds must be 1..12')
    if not _finite(p['resolution_hz']) or p['resolution_hz'] <= 0:
        raise ValueError('shift_search.resolution_hz must be positive')
    return p


def cap_paths(laser):
    """The two laser-yaml paths of the per-step cap: the request cap and the lock owner's internal step."""
    from ldcon.jobs.target_response_feedforward import configuration
    context = configuration(laser)
    if context.get('status') != 'configured':
        raise DeviceError('The shift study needs one configured lock owner (target FF context): ' + str(context.get('reason')))
    device = context['device_id']
    if context['owner_type'] == 'ecdl.rig.owner':
        owner = ('devices', device, 'control_settings', 'steady_lock', 'target_ramp_step_hz')
    else:
        owner = ('devices', device, 'control_settings', 'native_current', 'native', 'target_ramp_step_hz')
    return dict(request=REQUEST_PATH, owner=owner, owner_type=context['owner_type'], device_id=device)


# ------------------------------------------------------------------------------------------ pure planner
def plan(rounds, *, requested_hz, min_hz, max_rounds, resolution_hz, mode='bisect'):
    """One proposal (``propose_trial`` with ``shift_hz``) or ``stop`` with the best passing shift.

    Order (rule D, design 2.5 steps 2 and 6): a short probe at the saved cap first, then the requested shift, then a
    bisection of the measured pass/fail bracket down to ``resolution_hz`` (``fixed`` mode: no bisection). Invalid
    evidence is repeated once; a failed probe stops the study without adopting anything.
    """
    if not isinstance(rounds, list) or any(not isinstance(r, dict) for r in rounds):
        raise ValueError('Rounds must be a list of records')
    for name, value in (('requested_hz', requested_hz), ('min_hz', min_hz), ('resolution_hz', resolution_hz)):
        if not _finite(value) or value <= 0:
            raise ValueError(name + ' must be positive')
    if type(max_rounds) is not int or max_rounds < 1:
        raise ValueError('max_rounds must be a positive integer')
    passing = [r['shift_hz'] for r in rounds if r.get('verdict') == 'pass']
    failing = [r['shift_hz'] for r in rounds if r.get('verdict') == 'fail']
    best = max(passing) if passing else None
    base = dict(requested_hz=requested_hz, best_passing_hz=best, rounds=len(rounds),
                remaining_rounds=max(0, max_rounds - len(rounds)), mode=mode)

    def stop(reason):
        return dict(base, action='stop', reason=reason)

    def propose(shift, reason):
        return dict(base, action='propose_trial', shift_hz=float(shift), reason=reason)

    if any(r.get('verdict') == 'hazard' for r in rounds):
        return stop('A terminal/uncertain outcome ended the search; nothing further is tried')
    if not rounds:
        return propose(min(min_hz, requested_hz), 'Probe at the saved cap first (rule D), with the current parameters')
    latest = rounds[-1]
    if latest.get('verdict') == 'invalid':
        repeated = sum(1 for r in rounds if r['shift_hz'] == latest['shift_hz'] and r.get('verdict') == 'invalid')
        if repeated >= 2:
            return stop('Repeated invalid/missing evidence at %.1f MHz; do not infer a smaller shift' % (latest['shift_hz'] / 1e6))
        return propose(latest['shift_hz'], 'Repeat the identical shift once for fresh complete evidence')
    if best is None:
        return stop('The probe at the saved cap did not pass; nothing is adopted')
    if best >= requested_hz * (1 - 1e-9):
        return stop('The requested shift passed in both directions')
    if len(rounds) >= max_rounds:
        return stop('Round budget exhausted; the largest passing shift stands')
    higher_failures = [s for s in failing if s > best]
    if not higher_failures:
        return propose(requested_hz, 'The probe passed; try the requested shift')
    if mode == 'fixed':
        return stop('The requested shift failed (fixed mode); the largest passing shift stands')
    high = min(higher_failures)
    if high - best <= resolution_hz:
        return stop('Largest passing shift resolved to shift_search.resolution_hz')
    return propose((best + high) / 2, 'Bisect the measured pass/fail bracket')


# ------------------------------------------------------------------------------------------ judge
def judge(legs, p, *, owner_type, monitors):
    """One round's verdict (pass|fail|invalid|hazard) with its reasons; quality is reported, never judged."""
    reasons, flags = [], []
    if len(legs) != 2:
        return dict(verdict='invalid', reasons=['A round needs the outward and the return leg'], quality=flags)
    settle = []
    for leg, monitor in zip(legs, monitors):
        command = leg.get('command') or {}
        if command.get('status') != 'succeeded' or not command.get('locked'):
            return dict(verdict='hazard', reasons=['target command did not succeed under the lock'], quality=flags)
        analysis = leg.get('analysis') or {}
        if (leg.get('history') or {}).get('integrity_errors'):
            return dict(verdict='invalid', reasons=['history integrity: ' + str(leg['history']['integrity_errors'])], quality=flags)
        status = analysis.get('status')
        if status == 'inconclusive':
            return dict(verdict='invalid', reasons=['settling evidence inconclusive: ' + str(analysis.get('reason'))], quality=flags)
        if status != 'settled':
            reasons.append('leg %+.1f MHz not settled: %s' % (leg['offset_hz'] / 1e6, analysis.get('reason')))
        elif not (_finite(analysis.get('settling_from_request_s')) and analysis['settling_from_request_s'] <= p['settling_goal_s']):
            reasons.append('leg %+.1f MHz settled in %s s > goal %.2f s' % (leg['offset_hz'] / 1e6, analysis.get('settling_from_request_s'), p['settling_goal_s']))
        settle.append(analysis.get('settling_from_request_s'))
        before = monitor.get('mode_id_before')
        after = [s.get('mode_id') for s in ((command.get('verify') or {}).get('quality_samples') or []) if s.get('mode_id') is not None]
        if before is not None and after and any(m != before for m in after):
            reasons.append('branch changed during leg %+.1f MHz (mode_id %s -> %s)' % (leg['offset_hz'] / 1e6, before, after[-1]))
        if monitor.get('lost'):
            reasons.append('lock reported lost (PZT at boundary / outside capture) during leg %+.1f MHz' % (leg['offset_hz'] / 1e6))
        fraction = monitor.get('max_integral_fraction')
        if owner_type != 'ecdl.rig.owner' and _finite(fraction) and fraction >= p['abort_integral_fraction_max']:
            reasons.append('integral fraction %.2f reached abort_integral_fraction_max %.2f' % (fraction, p['abort_integral_fraction_max']))
        for key in ('degraded', 'single_mode'):
            values = [s.get(key) for s in ((command.get('verify') or {}).get('quality_samples') or [])]
            if (key == 'degraded' and any(v is True for v in values)) or (key == 'single_mode' and any(v is False for v in values)):
                flags.append('%s during leg %+.1f MHz (report only)' % (key, leg['offset_hz'] / 1e6))
    if legs[-1]['offset_hz'] != 0:
        reasons.append('the round did not return to the starting target')
    return dict(verdict='fail' if reasons else 'pass', reasons=reasons, quality=flags,
                worst_settle_s=max((s for s in settle if _finite(s)), default=None) if all(_finite(s) for s in settle) else None)


class LegMonitor:
    """Samples the lock's live row during one leg: integral usage, lost state, mode id, headroom (report + judge)."""
    def __init__(self, lock_directory):
        self.path = Path(lock_directory) / 'lock-target.json'
        row = read_json(self.path, {}) or {}
        self.record = dict(mode_id_before=row.get('mode_id'), max_integral_fraction=None, lost=False, samples=0,
                           min_headroom_hz=None, tec_centering_last=None, recentre_seen=False)

    def sample(self):
        row = read_json(self.path, {}) or {}
        r = self.record
        r['samples'] += 1
        fraction = row.get('integral_fraction')
        if _finite(fraction):
            r['max_integral_fraction'] = fraction if r['max_integral_fraction'] is None else max(r['max_integral_fraction'], fraction)
        if row.get('state') == 'lost':
            r['lost'] = True
        if row.get('recentre_active'):
            r['recentre_seen'] = True
        for key in ('pzt_headroom_hz', 'dac_headroom_hz'):
            room = row.get(key)
            if isinstance(room, dict):
                low = min(v for v in (room.get('up_hz'), room.get('down_hz')) if _finite(v)) if any(_finite(room.get(k)) for k in ('up_hz', 'down_hz')) else None
                if low is not None:
                    r['min_headroom_hz'] = low if r['min_headroom_hz'] is None else min(r['min_headroom_hz'], low)
        if isinstance(row.get('tec_centering'), dict):
            r['tec_centering_last'] = row['tec_centering'].get('status')
        return r


# ------------------------------------------------------------------------------------------ the saved cap
class Ceiling:
    """The per-step cap in the laser yaml during the study: trial saves, promotion, restoration, audit."""
    def __init__(self, directory, ctx, paths, *, actor_base, clock, sleep, timeout_s):
        self.directory, self.ctx, self.paths = Path(directory), ctx, paths
        self.actor_base, self.clock, self.sleep, self.timeout_s = actor_base, clock, sleep, timeout_s
        document = ctx.document
        self.saved = {name: _get(document, path) for name, path in (('request', paths['request']), ('owner', paths['owner']))}
        if not _finite(self.saved['request']) or self.saved['request'] <= 0:
            raise DeviceError('target.max_change_hz must be a positive saved value before a shift study')
        if not _finite(self.saved['owner']) or self.saved['owner'] <= 0:
            raise DeviceError('.'.join(paths['owner']) + ' must be present in the laser yaml so the study can raise and restore it')
        self.state = 'saved'
        self.current = dict(self.saved)
        self.audit = []
        self.record = None

    def effective_cap(self):
        return min(self.current['request'], self.current['owner'])

    def _write(self, value_request, value_owner, basis):
        laser_id = self.ctx.laser['id']
        current = self.ctx.get_configuration('lasers', laser_id)
        text, document = current['yaml'], current['document']
        before = dict(request=_get(document, self.paths['request']), owner=_get(document, self.paths['owner']))
        from ldcon.jobs.lock_target import edit_scalar_text
        edited = text
        if before['request'] != value_request:
            edited = edit_scalar_text(edited, self.paths['request'], value_request)
        if before['owner'] != value_owner:
            edited = edit_scalar_text(edited, self.paths['owner'], value_owner)
        row = dict(basis=basis, study_id=self.directory.name, laser_id=laser_id, unix_s=time.time(),
                   paths={'.'.join(self.paths['request']): dict(before=before['request'], after=value_request),
                          '.'.join(self.paths['owner']): dict(before=before['owner'], after=value_owner)},
                   revision_before=current['revision'], revision_after=None, status='submitted')
        self.audit.append(row)
        # Until the save is confirmed the yaml holds the PREVIOUS values: a crash here leaves a 'trial' record only
        # when a trial is in the yaml (readiness restores by comparing the yaml with the recorded trial values).
        self._persist('trial' if basis == 'trial' else self.state, pending=True)
        if edited == text:
            row.update(status='unchanged', revision_after=current['revision'])
            self.current = dict(request=value_request, owner=value_owner)
            self._persist(basis, pending=False)
            return row
        reply = self.ctx.put_configuration('lasers', laser_id, edited, current['revision'], actor=self.actor_base + ':shift:' + basis)
        row.update(revision_after=reply.get('revision'), status='saved')
        self.current = dict(request=value_request, owner=value_owner)
        self.state = basis
        self._persist(basis, pending=False)
        adopted = self._wait_adopted(current['revision'], reply.get('revision'))
        row.update(lock_adoption=adopted, status='adopted_by_lock' if adopted.get('status') == 'applied' else 'saved_unadopted')
        self.ctx.pending = False
        self._persist(basis, pending=False)
        if adopted.get('status') != 'applied':
            raise DeviceError('The running lock did not adopt the saved per-step cap (%s): %s' % (basis, adopted.get('reason')))
        return row

    def _wait_adopted(self, revision_before, revision_after):
        """Wait for the lock worker's reload result of this save (owner_hot: the owner adopts at its safe boundary;
        native_hot: the dashboard updated the owner); a 'deferred' result keeps waiting until applied or timeout."""
        lock_directory = Path(self.ctx.lock_directory)
        deadline = self.clock() + self.timeout_s
        last = None
        while True:
            for path in sorted(lock_directory.glob('laser-reload-result-*.json'), key=lambda x: x.stat().st_mtime, reverse=True):
                result = read_json(path, {}) or {}
                if result.get('revision_before') != revision_before or result.get('revision_after') != revision_after:
                    continue
                last = result
                if result.get('status') in ('applied', 'snapshot') and not result.get('deferred'):
                    return dict(status='applied', request_id=result.get('request_id'), applied=result.get('applied'),
                                snapshot=result.get('snapshot'))
                if result.get('status') == 'rejected':
                    return dict(status='rejected', reason=result.get('reason'))
            if self.clock() >= deadline:
                return dict(status='timeout', reason=(last or {}).get('reason') or 'no reload result for the saved revision')
            self.ctx.check(force=True, ignore_cancel=True)
            self.sleep(.2)

    def trial(self, shift_hz):
        """Raise both caps to ``shift_hz`` for a trial (no-op when the saved caps already admit it)."""
        if shift_hz <= self.effective_cap() * (1 + 1e-12):
            return None
        return self._write(max(shift_hz, self.current['request']), max(shift_hz, self.current['owner']), 'trial')

    def adopt(self, value_hz):
        return self._write(float(value_hz), float(value_hz), 'adopted')

    def restore(self):
        if self.current == self.saved and self.state in ('saved', 'restored'):
            self._persist('restored', pending=False)
            return None
        return self._write(self.saved['request'], self.saved['owner'], 'restored')

    def _persist(self, status, *, pending):
        self.record = dict(schema='ldcon.target-response-ceiling/1', status=status, pending_write=pending,
                           study_id=self.directory.name, laser_id=self.ctx.laser['id'],
                           paths={'.'.join(self.paths['request']): dict(before=self.saved['request'], after=self.current['request']),
                                  '.'.join(self.paths['owner']): dict(before=self.saved['owner'], after=self.current['owner'])},
                           updated_unix_s=time.time(), audit=self.audit)
        atomic_json(self.directory / CEILING_FILE, self.record)
        atomic_json(self.directory / AUDIT_FILE, dict(schema='ldcon.target-response-shift-audit/1', rows=self.audit))


def restore_pending_ceiling(jobs_root, laser, api, *, emit=lambda k, v: None, actor='card:lock-target', self_id=None):
    """Restore the saved per-step cap left as a TRIAL by a study that did not finish (readiness of tune.lock_target).

    Only records of this laser whose study job is no longer running are touched; the values are reverted only where
    the yaml still holds the trial values. ``laser`` (the caller's copy) is updated in place."""
    restored = []
    root = Path(jobs_root)
    try:
        candidates = sorted(root.glob('*/' + CEILING_FILE))
    except OSError:
        return restored
    for path in candidates:
        record = read_json(path, {}) or {}
        if record.get('status') != 'trial' or record.get('laser_id') != laser.get('id') or record.get('study_id') == self_id:
            continue
        progress = read_json(path.parent / 'progress.json', {}) or {}
        if progress.get('status') not in TERMINAL:
            continue        # running (or unknown): the study restores its own ceiling
        try:
            current = api.get_configuration('lasers', laser['id'])
        except Exception as error:
            emit('target_ceiling_restore_unavailable', dict(study_id=record.get('study_id'), reason=str(error)))
            continue
        text, document = current['yaml'], current['document']
        from ldcon.jobs.lock_target import edit_scalar_text
        edited, reverted = text, {}
        for dotted, values in (record.get('paths') or {}).items():
            segments = tuple(dotted.split('.'))
            if _get(document, segments) == values.get('after') and values.get('before') is not None and values['after'] != values['before']:
                edited = edit_scalar_text(edited, segments, values['before'])
                reverted[dotted] = values
        outcome = dict(record, status='superseded' if not reverted else 'restored_by_' + actor, restored_paths=reverted,
                       restored_unix_s=time.time())
        if reverted:
            try:
                reply = api.put_configuration('lasers', laser['id'], edited, current['revision'], actor=actor + ':ceiling-restore')
            except Exception as error:
                emit('target_ceiling_restore_failed', dict(study_id=record.get('study_id'), reason=str(error)))
                continue
            outcome['restore_revision'] = reply.get('revision')
            for dotted, values in reverted.items():
                parent = laser
                segments = dotted.split('.')
                for key in segments[:-1]:
                    parent = parent.setdefault(key, {}) if isinstance(parent, dict) else {}
                if isinstance(parent, dict):
                    parent[segments[-1]] = values['before']
        atomic_json(path, outcome)
        emit('target_ceiling_restored', dict(study_id=record.get('study_id'), paths=reverted, status=outcome['status']))
        restored.append(outcome)
    return restored


# ------------------------------------------------------------------------------------------ the study phase
def run(directory, result, ctx, p, *, measure_round, clock, sleep, publish, emit, wall_clock=time.time):
    """The shift search of study.target_response (called instead of the plain round / FF comparison)."""
    from ldcon.core.target_policy import target_bounds
    laser = ctx.document
    paths = cap_paths(laser)
    base = result['starting_target_hz']
    search = validate_search(p.get('shift_search'))
    requested = float(p['shift_hz'])
    effective, clips = requested, []
    bounds = target_bounds(laser, reference_hz=base)
    if bounds is not None:
        room = min(bounds[1] - base, base - bounds[0])
        if effective > room:
            clips.append(dict(reason='allowed_change_hz', from_hz=effective, to_hz=room))
            effective = room
    live = read_json(Path(ctx.lock_directory) / 'lock-target.json', {}) or {}
    for key in ('pzt_headroom_hz', 'dac_headroom_hz'):
        headroom = live.get(key)
        if isinstance(headroom, dict) and all(_finite(headroom.get(k)) for k in ('up_hz', 'down_hz')):
            room = min(headroom['up_hz'], headroom['down_hz']) / 1.2
            if effective > room:
                clips.append(dict(reason=key, from_hz=effective, to_hz=room, headroom=headroom))
                effective = room
    if effective <= 0:
        raise DeviceError('No room for a target shift (allowed range / owner headroom); no target was changed')
    ceiling = Ceiling(directory, ctx, paths, actor_base='card:' + directory.name, clock=clock, sleep=sleep,
                      timeout_s=p['command_timeout_s'])
    min_hz = min(search['min_hz'] or ceiling.effective_cap(), effective)
    block = dict(schema='ldcon.target-response-shift/1', requested_hz=requested, effective_requested_hz=effective, clips=clips,
                 min_hz=min_hz, mode=search['mode'], max_rounds=search['max_rounds'], resolution_hz=search['resolution_hz'],
                 saved=dict(ceiling.saved), paths={k: '.'.join(v) for k, v in paths.items() if isinstance(v, tuple)},
                 owner_type=paths['owner_type'], rounds=[], best_passing_hz=None, adopted_hz=None, shortfall_hz=None,
                 adoption=dict(status='pending'), audit=ceiling.audit, headroom_at_start={k: live.get(k) for k in ('pzt_headroom_hz', 'dac_headroom_hz')})
    result['shift'] = block
    ctx.shift_ceiling = ceiling
    mode = p.get('adoption_mode', 'report_only')
    try:
        while True:
            choice = plan(block['rounds'], requested_hz=effective, min_hz=min_hz, max_rounds=search['max_rounds'],
                          resolution_hz=search['resolution_hz'], mode=search['mode'])
            block['last_plan'] = choice
            emit('target_response_shift_plan', choice)
            if choice['action'] != 'propose_trial':
                break
            shift = choice['shift_hz']
            index = len(block['rounds'])
            publish(dict(state='shift_trial', round_index=index, shift_hz=shift, reason=choice['reason']))
            trial_save = ceiling.trial(shift)
            monitors = []

            def leg_monitor():
                monitor = LegMonitor(ctx.lock_directory)
                monitors.append(monitor.record)
                return monitor
            legs = measure_round(index, offsets=[shift, 0.], joint_pzt=False, monitor_factory=leg_monitor)
            verdict = judge(legs, p, owner_type=paths['owner_type'], monitors=monitors)
            block['rounds'].append(dict(shift_hz=shift, index=index, status='completed', reason=choice['reason'],
                                        trial_save=trial_save, trials=[dict(index=l['index'], offset_hz=l['offset_hz'],
                                            analysis={k: (l.get('analysis') or {}).get(k) for k in ('status', 'reason', 'settling_from_request_s', 'sample_count')},
                                            monitor=m, verify=(l.get('command') or {}).get('verify')) for l, m in zip(legs, monitors)],
                                        parameters=dict(target_feedforward_scale=(result.get('feedforward_context') or {}).get('target_feedforward_scale'),
                                                        settling_goal_s=p['settling_goal_s'], verify_samples=1, sample_interval_s=.1),
                                        **verdict))
            block['best_passing_hz'] = max((r['shift_hz'] for r in block['rounds'] if r['verdict'] == 'pass'), default=None)
            atomic_json(Path(directory) / 'target-response.json', result)
            emit('target_response_shift_round', dict(shift_hz=shift, verdict=verdict['verdict'], reasons=verdict['reasons']))
        best = block['best_passing_hz']
        block['search_reason'] = block['last_plan']['reason']
        if best is None:
            block['adoption'] = dict(status='no_success', basis='restored', mode=mode)
            block['restore'] = ceiling.restore()
        elif mode != 'auto_within_approved':
            block['adopted_hz'] = None
            block['candidate_hz'] = min(requested, best)
            block['adoption'] = dict(status='proposal_only' if mode == 'report_only' else 'awaiting_human', basis='restored',
                                     candidate_hz=min(requested, best), mode=mode)
            block['restore'] = ceiling.restore()
        else:
            adopt = min(requested, best)
            passing = [r for r in block['rounds'] if r['verdict'] == 'pass' and r['shift_hz'] == best][-1]
            block['adopted_hz'] = adopt
            block['shortfall_hz'] = max(0., requested - adopt)
            block['adopted_parameters'] = passing['parameters']
            block['adoption'] = dict(status='applied', basis='adopted', adopted_hz=adopt, before=dict(ceiling.saved),
                                     after=dict(request=adopt, owner=adopt), mode=mode, save=ceiling.adopt(adopt))
        block['ceiling_state'] = ceiling.state
    finally:
        block['audit'] = ceiling.audit
        atomic_json(Path(directory) / 'target-response.json', result)
    return block


def finalize(ctx, result, *, reason):
    """On an interrupted study: put the saved cap back when the study still may write (same revision, no pending
    command); otherwise the ``trial`` record stays for ``restore_pending_ceiling``."""
    ceiling = getattr(ctx, 'shift_ceiling', None)
    block = (result or {}).get('shift')
    if ceiling is None or block is None or ceiling.state != 'trial':
        return None
    if ctx.pending:
        outcome = dict(status='left_for_readiness', reason='unconfirmed command; no replay (' + reason + ')')
    else:
        try:
            ctx.check(force=True, ignore_cancel=True)
            row = ceiling.restore()
            outcome = dict(status='restored', reason=reason, save=row)
        except Exception as error:
            outcome = dict(status='left_for_readiness', reason='%s: %s' % (type(error).__name__, error))
    block['restore'] = outcome
    block['ceiling_state'] = ceiling.state
    return outcome
