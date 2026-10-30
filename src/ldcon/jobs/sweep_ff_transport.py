"""Finite sweep experiments through registered cards, never a device transport.

Each child retains the existing owner's per-shot checkpoint and restoration.
Cancellation can wait for a buffered record to end; this module never promises
an immediate handoff and never writes a target or stops the parent lock.
"""
import copy
import hashlib
import json
import math
import os
from pathlib import Path

from ldcon.core.contracts import DeviceError
from ldcon.core.interface_cards import CardCancelled
from ldcon.core.records import atomic_json, read_json, safe_id
from ldcon.jobs.progress import CancelledRun

TERMINAL = frozenset(('succeeded', 'completed', 'completed_unsettled', 'failed',
                     'cancelled', 'interrupted', 'inconclusive', 'rejected'))


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _operations(job):
    return {c.get('operation', c.get('card')) for c in job.get('cards', [])}


def _outcome(job):
    return job.get('outcome') or (job.get('shot_sweep') or {}).get('outcome') or {}


def normalize_outcome(job, *, cancelled=False, reason=None, cleanup=None):
    """Preserve unknown hop detection; optical degradation is not a mode hop."""
    original = _outcome(job)
    shot = job.get('shot_sweep') or {}
    restored = original.get('restored') is True
    hop = original.get('mode_hop')
    if type(hop) is not bool:
        hop = shot.get('mode_hop')
    if type(hop) is not bool:
        hop = None
    stopped = cancelled or job.get('status') in ('cancelled', 'interrupted') or original.get('verdict') == 'cancelled'
    complete = original.get('playback_completed') is True
    accepted = original.get('verdict') == 'succeeded'
    return dict(playback_completed=complete, mode_hop=hop, restored=restored,
                outputs_uncertain=not restored or original.get('outputs_uncertain') is True,
                cancelled=stopped, censored=stopped or not complete or not accepted or reason is not None,
                verdict=original.get('verdict'), reason=reason or original.get('reason'),
                cleanup=cleanup, original=copy.deepcopy(original))


def raw_analysis(payload, child_id, *, target_hz=None):
    """Accept recorded point arrays, never the proxy's sparse residual summary."""
    if payload.get('state') != 'ready':
        raise DeviceError('Raw shot analysis is not ready')
    doc = payload.get('result')
    if not isinstance(doc, dict) or doc.get('schema') != 'ldcon.shot-analysis/v1':
        raise DeviceError('Raw shot analysis schema is missing')
    if doc.get('proxy_job') != child_id or doc.get('source') not in ('influx', 'vm'):
        raise DeviceError('Raw shot analysis has the wrong child or source')
    if target_hz is not None:
        actual = ((doc.get('parameters') or {}).get('reference') or {}).get('lock_target_hz')
        tolerance = max(.001, 4*math.ulp(target_hz))
        if (type(actual) not in (int, float) or not math.isfinite(actual) or
                not math.isclose(actual, target_hz, rel_tol=0., abs_tol=tolerance)):
            raise DeviceError('Raw shot analysis target differs from the frozen lock target')
    points = doc.get('points') if doc.get('mode') == 'levels' else (doc.get('series') or {}).get('wavelength')
    if not isinstance(points, dict):
        raise DeviceError('Full timestamped wavelength arrays are unavailable')
    stamps, values = points.get('t_unix_s'), points.get('wavelength_nm')
    if not isinstance(stamps, list) or not isinstance(values, list) or not stamps or len(stamps) != len(values):
        raise DeviceError('Full timestamped wavelength arrays are unavailable or mismatched')
    return copy.deepcopy(doc)


class SweepExperiment:
    """One serialized campaign; an unresolved dispatch/cleanup closes it.

    All timing budgets are explicit. ``run`` returns failed/censored evidence as
    well as successful evidence. ``check`` and ``wait`` raise before new work if
    cancelled, superseded or closed. An uncertain POST is never replayed.
    """

    def __init__(self, directory, laser, deck, sweep_card_id, *, timeout_s, poll_s,
                 cleanup_timeout_s, analysis_wait_s, clock, sleep, cancel, emit,
                 publish, api=None):
        from ldcon.jobs.dashboard_api import Dashboard
        from ldcon.jobs.ecdl_locked_sweep_proxy import rig_locked_route
        from ldcon.jobs.native_ff_proxy import feedforward_route, lock_report

        for name, value in dict(timeout_s=timeout_s, poll_s=poll_s,
                                cleanup_timeout_s=cleanup_timeout_s, analysis_wait_s=analysis_wait_s).items():
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(name + ' must be positive and finite')
        self.directory, self.laser, self.deck = Path(directory), copy.deepcopy(laser), copy.deepcopy(deck)
        self.card = next((c for c in deck['cards'] if c['id'] == sweep_card_id), None)
        if self.card is None or self.card.get('operation') not in ('sweep.servo_modulation', 'sweep.current_modulation'):
            raise ValueError('A registered locked-sweep card is required')
        role = (self.card.get('uses') or [None])[0]
        self.lock_operation = ('servo.ecdl_steady_lock' if rig_locked_route(laser, role) else
                               'servo.native_current' if feedforward_route(laser, role) else None)
        if self.lock_operation is None:
            raise ValueError('No supported locked-sweep route; no standalone/device fallback')
        self.clock, self.sleep, self.cancel, self.emit, self.publish = clock, sleep, cancel, emit, publish
        self.poll_s, self.cleanup_timeout_s, self.analysis_wait_s = poll_s, cleanup_timeout_s, analysis_wait_s
        self.deadline = clock() + timeout_s
        if api is None:
            from ldcon.jobs.dashboard_api import state_dir_of
            from ldcon.jobs.shot_analysis import load_config
            if os.environ.get('LDCON_SHOT_ANALYSIS', '').lower() in ('off', '0', 'false'):
                raise DeviceError('Post-shot analysis is disabled; no FF experiment may start')
            if load_config(state_dir_of(directory)) is None:
                raise DeviceError('Configure full-rate post-shot analysis before starting an FF experiment')
        self.api = api or Dashboard.from_job_directory(directory)
        if api is None:
            self.api.timeout_s = min(self.api.timeout_s, timeout_s, cleanup_timeout_s, analysis_wait_s)
        self.meta = read_json(self.directory/'job.json', {})
        self.set_id = safe_id(self.meta['set_id'])
        self.set_revision = self.meta.get('set_revision')
        if not isinstance(self.set_revision, str) or not self.set_revision:
            raise DeviceError('Frozen assignment revision is required before dispatch')
        self.parent_id, self.deck_id = safe_id(self.directory.name), safe_id(deck['id'])
        self.journal_path = self.directory/'sweep-ff-transport.json'
        if self.journal_path.exists():
            raise DeviceError('Existing sweep experiment journal requires inspection; no replay')
        current = self.api.get_configuration('lasers', laser['id'])
        if _digest(current['document']) != _digest(laser):
            raise DeviceError('Frozen laser configuration differs from current configuration')
        locks = [j for j in self._active() if self.lock_operation in _operations(j) and j.get('status') == 'running']
        if len(locks) != 1:
            raise DeviceError('Exactly one running lock is required; the experiment never starts a lock')
        self.lock_id = safe_id(locks[0]['id'])
        self.lock_directory = self.directory.parent/self.lock_id
        target = lock_report(self.lock_directory)
        if (not target or type(target.get('target_frequency_hz')) not in (int, float) or
                not math.isfinite(target['target_frequency_hz']) or target['target_frequency_hz'] <= 0):
            raise DeviceError('A fresh finite lock target is required before starting the experiment')
        self.context = dict(laser_id=laser['id'], laser_revision=current['revision'],
                            laser_sha256=_digest(laser), target=copy.deepcopy(laser.get('target')),
                            lock_target_hz=target.get('target_frequency_hz'),
                            lock_job_id=self.lock_id, lock_operation=self.lock_operation,
                            set_id=self.set_id, set_revision=self.set_revision,
                            deck_id=self.deck_id, sweep_card_id=sweep_card_id,
                            sweep_card_sha256=_digest(self.card))
        self.child_id = None
        self.child_verified = False
        self.closed_reason = None
        self.journal = dict(schema='ldcon.sweep-ff-transport/v1', context=self.context, children=[])
        self.check()
        self._save()

    def _active(self):
        return [j for j in self.api._request('GET', 'api/jobs?active_only=true')['jobs']
                if (j.get('laser') or {}).get('id') == self.laser['id'] or j.get('set_id') == self.set_id]

    def _save(self):
        self.journal['closed_reason'] = self.closed_reason
        atomic_json(self.journal_path, self.journal)

    def check(self):
        from ldcon.core.operation_registry import OBSERVERS
        if self.closed_reason:
            raise DeviceError('Sweep experiment closed: ' + self.closed_reason)
        if self.cancel() or self.clock() >= self.deadline:
            raise CardCancelled('Sweep experiment cancelled or deadline reached')
        current = self.api.get_configuration('lasers', self.laser['id'])
        if current['revision'] != self.context['laser_revision'] or _digest(current['document']) != self.context['laser_sha256']:
            raise CardCancelled('Configuration or target superseded; no further trial or target restoration')
        if self.api._request('GET', 'api/sets/'+self.set_id)['revision'] != self.set_revision:
            raise CardCancelled('Assigned deck or configuration changed; no further trial')
        jobs = self._active()
        if not any(j['id'] == self.lock_id and j.get('status') == 'running' and
                   self.lock_operation in _operations(j) for j in jobs):
            raise CardCancelled('Parent lock ended or changed; no further trial')
        for job in jobs:
            if job['id'] in (self.parent_id, self.lock_id, self.child_id):
                continue
            operations = _operations(job)
            if not operations or not operations <= (OBSERVERS-{'agent.task', 'agent.history_review'}):
                raise CardCancelled('Foreground operation takes priority; no further trial')
        live = read_json(self.lock_directory/'lock-target.json', {})
        # The lock worker services its own finite sweep synchronously and may
        # not rewrite this report until the child restores. That child's owner
        # and READY protocol own freshness while it runs; do not time it out
        # merely because the enclosing lock report ages during the record.
        if self.child_id is None:
            from ldcon.jobs.native_ff_proxy import lock_report
            live = lock_report(self.lock_directory)
            if not live:
                raise CardCancelled('Lock target report is stale or missing; no further trial')
        if live.get('ramp_active') or live.get('target_frequency_hz') != self.context['lock_target_hz']:
            raise CardCancelled('Lock target changed or is moving; no further trial')
        for pattern in ('operating-point-requests/pending/*.json', 'runtime-parameter-request-*.json',
                        'runtime-parameter-inflight-*.json', 'laser-reload-request-*.json',
                        'target-ff-policy-request-*.json', 'online-request.json',
                        'hot-control-request.json', 'ff-requests/pending/*.json'):
            for path in self.lock_directory.glob(pattern):
                own = pattern.startswith('ff-requests/') and self.child_id and read_json(path, {}).get('proxy_job') == self.child_id
                if not own:
                    raise CardCancelled('Another controller request is pending; no further trial')
        online = read_json(self.lock_directory/'online-status.json', {})
        hot = read_json(self.lock_directory/'hot-control-status.json', {})
        if ((online and online.get('status') not in TERMINAL|{'unchanged'}) or
                hot.get('status') in ('queued', 'dispatching')):
            raise CardCancelled('Another controller operation is active; no further trial')

    def wait(self, duration_s):
        if type(duration_s) not in (int, float) or not math.isfinite(duration_s) or duration_s < 0:
            raise ValueError('Wait duration must be nonnegative and finite')
        end = self.clock() + duration_s
        while True:
            self.check()
            if self.clock() >= end:
                return
            self.sleep(min(self.poll_s, end-self.clock()))

    def _read_child(self):
        job = self.api._request('GET', 'api/jobs/'+self.child_id)
        cards = job.get('cards') or []
        if (job.get('id') != self.child_id or self.child_id in (self.parent_id, self.lock_id) or
                (job.get('laser') or {}).get('id') != self.laser['id'] or job.get('set_id') != self.set_id or
                job.get('deck_id') != self.deck_id or len(cards) != 1 or
                cards[0].get('id') != self.card['id'] or _operations(job) != {self.card['operation']}):
            raise DeviceError('Sweep child identity mismatch; no unrelated job will be stopped')
        self.child_verified = True
        return job

    def _cleanup(self, entry, last):
        if not self.child_id or not self.child_verified:
            return last, dict(state='unverified', reason='No verified child identity; no stop sent')
        if last.get('status') in TERMINAL:
            return last, dict(state='terminal', restored=_outcome(last).get('restored') is True)
        entry['stop_intent'] = dict(child_id=self.child_id, reason='experiment ended or superseded')
        self._save()
        cleanup = dict(state='requested', record_may_finish_before_restore=True)
        try:
            self.api._request('POST', 'api/jobs/'+self.child_id+'/stop', {})
        except Exception as error:
            cleanup['stop_response'] = 'unknown_'+type(error).__name__  # no replay
        end = self.clock()+self.cleanup_timeout_s
        while self.clock() < end:
            try:
                last = self._read_child()
                if last.get('status') in TERMINAL:
                    return last, dict(cleanup, state='terminal', restored=_outcome(last).get('restored') is True)
            except Exception as error:
                cleanup['read_error'] = type(error).__name__
            self.sleep(min(self.poll_s, max(0., end-self.clock())))
        return last, dict(cleanup, state='timeout', restored=False)

    def _analysis(self):
        end = min(self.deadline, self.clock()+self.analysis_wait_s)
        while True:
            self.check()
            payload = self.api._request('GET', 'api/jobs/'+self.child_id+'/analysis')
            if payload.get('state') == 'ready':
                return raw_analysis(payload, self.child_id, target_hz=self.context['lock_target_hz'])
            if payload.get('state') not in ('pending', 'running'):
                raise DeviceError('Raw post-shot analysis unavailable; no sparse-data fallback')
            if self.clock() >= end:
                raise DeviceError('Raw post-shot analysis deadline reached')
            self.sleep(min(self.poll_s, end-self.clock()))

    def run(self, parameters):
        """Run one finite child, return raw evidence and owner-confirmed outcome."""
        self.check()
        if self.child_id is not None:
            raise DeviceError('Previous sweep child has not been resolved')
        if parameters.get('shape') != 'staircase' or parameters.get('trigger') != 'auto' or parameters.get('calibrate_only'):
            raise ValueError('Sweep comparison requires a finite auto-triggered staircase')
        if parameters.get('shot_sweep') is not None:
            raise ValueError('External shot requests are not comparison trials')
        body = dict(expected_revision=self.set_revision, card_id=self.card['id'], parameters=copy.deepcopy(parameters))
        entry = dict(index=len(self.journal['children']), state='intent', request=body, request_sha256=_digest(body))
        self.journal['children'].append(entry)
        self._save()  # durable before the one allowed POST
        last, analysis, reason, cleanup = {}, None, None, None
        cancelled = False
        try:
            self.check()
            entry['state'] = 'dispatching'
            self._save()
            response = self.api._request('POST', f'api/sets/{self.set_id}/decks/{self.deck_id}/start', body)
            child = safe_id(response['id'])
            if child in (self.parent_id, self.lock_id) or any(c.get('child_id') == child for c in self.journal['children']):
                raise DeviceError('Start returned an unrelated or repeated child identity')
            self.child_id = child
            entry.update(state='started', child_id=child)
            self._save()
            last = self._read_child()
            self.emit('sweep_ff_child_started', dict(child_id=child, index=entry['index']))
            while last.get('status') not in TERMINAL:
                self.check()
                self.publish(dict(state='sweep_trial', child_id=child, child_status=last.get('status')))
                self.sleep(min(self.poll_s, max(0., self.deadline-self.clock())))
                last = self._read_child()
            self.check()
            if _outcome(last).get('restored') is not True:
                raise DeviceError('Sweep child ended without verified hardware restoration')
            analysis = self._analysis()
        except (Exception, CancelledRun) as error:
            cancelled = isinstance(error, (CardCancelled, CancelledRun))
            reason = str(error) if isinstance(error, DeviceError) else type(error).__name__
            if not self.child_id and entry['state'] == 'dispatching':
                entry['state'] = 'unknown_start'
                reason = 'Sweep start outcome unknown; request may have been accepted; no replay'
            last, cleanup = self._cleanup(entry, last)
            self.closed_reason = reason
        outcome = normalize_outcome(last, cancelled=cancelled, reason=reason, cleanup=cleanup)
        if outcome['outputs_uncertain']:
            self.closed_reason = self.closed_reason or 'Hardware restoration unverified'
        if outcome['verdict'] != 'succeeded':
            self.closed_reason = self.closed_reason or 'Sweep child did not report a successful outcome'
        result = dict(child_id=self.child_id, job=last, analysis=analysis, owner_outcome=outcome, context=self.context)
        entry.update(state='finished' if not self.closed_reason else entry.get('state'),
                     status=last.get('status'), owner_outcome=outcome,
                     analysis_available=analysis is not None)
        atomic_json(self.directory/('sweep-ff-trial-%04d.json' % entry['index']), result)
        self._save()
        if not self.closed_reason:
            self.child_id = None
            self.child_verified = False
        return result
