"""Durable, reasoned result dependencies. No instrument access or expression eval."""
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
import uuid

from ldcon.core.interface_schema import readiness, digest
from ldcon.web.web_runtime import ConflictError, TERMINAL, read_json, safe_id, utc_now

ONLINE = frozenset({'tune.pid_factor_reduction','tune.pid_coordinate_search', 'study.ecdl_locked_current_map', 'tune.ecdl_window_center'})
FINISHED = frozenset({'dispatched', 'cancelled', 'expired', 'blocked', 'review_required'})
WAITING = frozenset({'pending', 'waiting_agent'})
RESULT_FIELDS = frozenset({'original_mode_verified', 'single_mode', 'target_reached', 'operating_point_reached', 'stable'})
CONTROLLERS = frozenset({'servo.current_pid', 'servo.slow_pid', 'servo.ecdl_steady_lock', 'servo.native_current'})


def durable_json(path, value):
    """Commit both content and directory before any externally visible dispatch."""
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix='.'+path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            os.fchmod(stream.fileno(), 0o660)
            json.dump(value, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def number(value, label, low, high):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f'{label} must be {low}..{high}')
    return value


class Reservations:
    """One WebRuntime mutex serializes reservations, configuration and starts.

    Pending entries survive restart. A durable dispatch claim is never retried,
    even when process death occurs before the job receipt can be committed.
    """
    def __init__(self, runtime):
        self.runtime = runtime
        self.root = runtime.root/'reservations'
        self.root.mkdir(exist_ok=True)
        self.windows = {}  # Monotonic observations cannot survive process restart.
        for value in self.list():
            if value['status'] == 'dispatching':
                self._finish(value, 'review_required', 'Restart during dispatch; inspect output job/request; never automatically replay')

    def _path(self, reservation_id):
        return self.root/(safe_id(reservation_id)+'.json')

    def _save(self, value):
        value['updated_utc'] = utc_now()
        durable_json(self._path(value['id']), value)
        return value

    def get(self, reservation_id):
        value = read_json(self._path(reservation_id))
        if not isinstance(value, dict): raise FileNotFoundError(reservation_id)
        return value

    def list(self):
        values = [read_json(path) for path in self.root.glob('*.json') if not path.is_symlink()]
        return sorted([v for v in values if isinstance(v, dict)], key=lambda v: v['created_utc'], reverse=True)

    def _finish(self, value, status, message):
        value.update(status=status, message=message)
        self.windows.pop(value['id'], None)
        return self._save(value)

    def _future(self, job_id):
        return next((v for v in self.list() if v['output_job_id'] == job_id and not v['online']), None)

    def _job(self, job_id):
        try: return self.runtime.get_job(job_id)
        except FileNotFoundError:
            future = self._future(job_id)
            if future is None: raise ValueError('Unknown prerequisite job: '+job_id)
            return {'id': job_id, 'status': 'reserved', 'set_id': future['target']['set_id'],
                    'deck_id': future['target']['deck_id'], 'cards': [{'id': future['target']['card_id'], 'card':future['operation']}],
                    'reservation_status': future['status']}

    def create(self, payload):
        with self.runtime.mutex:
            self.runtime.assert_admission_open()
            if not isinstance(payload, dict) or set(payload)-{'request_id', 'reason', 'timeout_s', 'target', 'prerequisites'}:
                raise ValueError('Reservation needs request_id, reason, timeout_s, target and prerequisites')
            identifier = payload.get('request_id')
            if not isinstance(identifier, str) or not re.fullmatch('[0-9a-f]{32}', identifier):
                raise ValueError('request_id must be a new 32-character hexadecimal ID')
            payload_hash = digest(payload)
            if self._path(identifier).exists():
                prior = self.get(identifier)
                if prior['request_digest'] != payload_hash: raise ConflictError('request_id was used with different settings')
                return prior
            reason = payload.get('reason')
            if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
                raise ValueError('A nonempty reason (at most 2000 characters) is required')
            timeout = number(payload.get('timeout_s'), 'timeout_s', 1, 604800)
            target = payload.get('target')
            if not isinstance(target, dict) or set(target)-{'set_id', 'deck_id', 'card_id', 'expected_revision', 'parameters', 'controller_job_id'}:
                raise ValueError('Invalid reservation target')
            bundle = self.runtime.get_set(target.get('set_id'))
            if target.get('expected_revision') != bundle['revision']: raise ConflictError('Configuration changed; reload before reserving')
            definition = next((d for d in bundle['deck_definitions'] if d['id'] == target.get('deck_id')), None)
            if definition is None: raise ValueError('Unknown target deck')
            card_id = target.get('card_id') or definition['document']['cards'][0]['id']
            card = next((c for c in definition['document']['cards'] if c['id'] == card_id), None)
            if card is None: raise ValueError('Unknown target card')
            overrides = target.get('parameters', {})
            if not isinstance(overrides, dict): raise ValueError('Parameters must be a mapping')
            status = readiness(bundle['laser_definition']['document'], definition['document'], card,
                               {**definition['invocation_parameters'].get(card_id, {}), **overrides})
            issues = status['blocked_reasons']
            if card['operation'] in ONLINE:
                # A specified future controller is checked below and again at
                # dispatch. Every other configuration gate remains effective.
                issues = [s for s in issues if s not in ('A running stationary PID controller is required',
                          'A matching running ECDL stationary lock is required')]
            if issues: raise ValueError(' / '.join(issues))
            prerequisites = payload.get('prerequisites')
            if not isinstance(prerequisites, list) or not 1 <= len(prerequisites) <= 16:
                raise ValueError('One to sixteen explicit prerequisite job IDs are required')
            normalized = []
            for condition in prerequisites:
                if not isinstance(condition, dict) or set(condition)-{'job_id', 'condition', 'stable_s', 'max_age_s', 'predicates'}:
                    raise ValueError('Invalid prerequisite')
                job_id = safe_id(condition.get('job_id'))
                job = self._job(job_id)
                kind = condition.get('condition')
                if kind not in ('succeeded', 'locked'): raise ValueError('condition must be succeeded or locked')
                if kind == 'locked' and not any(c.get('card') in CONTROLLERS for c in job.get('cards', [])):
                    raise ValueError('Locked readiness requires a stationary controller job')
                predicates = condition.get('predicates', {})
                if not isinstance(predicates, dict) or set(predicates)-RESULT_FIELDS or any(type(v) is not bool for v in predicates.values()):
                    raise ValueError('Predicates accept only allowlisted boolean result fields')
                if kind == 'locked' and predicates: raise ValueError('Result predicates require a succeeded job')
                normalized.append(dict(job_id=job_id, condition=kind, predicates=predicates,
                    stable_s=number(condition.get('stable_s', 3), 'stable_s', 1, 3600),
                    max_age_s=number(condition.get('max_age_s', 3), 'max_age_s', .1, 10)))
            if len({p['job_id'] for p in normalized}) != len(normalized): raise ValueError('Duplicate prerequisite job')
            online = card['operation'] in ONLINE
            controller_job_id = target.get('controller_job_id')
            if online:
                if not any(p['job_id'] == controller_job_id and p['condition'] == 'locked' for p in normalized):
                    raise ValueError('Online reservation needs its explicit controller_job_id as a locked prerequisite')
                controller = self._job(controller_job_id)
                if (controller.get('set_id') != target['set_id'] or controller.get('deck_id') != target['deck_id']
                    or not any(c['id'] == status['parameters']['controller_card_id'] for c in controller.get('cards', []))):
                    raise ValueError('Online controller does not match the selected set, deck and card')
            elif controller_job_id is not None: raise ValueError('controller_job_id is only for online cards')
            target = {**target, 'card_id': card_id, 'parameters': status['parameters']}
            live_revision = None
            if online:
                live_revision = self._job(controller_job_id).get('live_controller', {}).get('settings_revision')
            value = dict(id=identifier, request_digest=payload_hash, status='pending', reason=reason.strip(),
                created_utc=utc_now(), expires_unix_s=time.time()+timeout, target=target, prerequisites=normalized,
                online=online, operation=card['operation'], output_job_id=controller_job_id if online else uuid.uuid4().hex,
                output_request_id=uuid.uuid4().hex if online else None,
                controller_settings_revision=live_revision, snapshot=dict(set_revision=bundle['revision'],
                    laser_revision=bundle['laser_definition']['revision'], deck_revision=definition['revision'],
                    assignment_yaml=bundle['yaml'], laser_yaml=bundle['laser_definition']['yaml'], deck_yaml=definition['yaml']),
                message='Waiting for prerequisites')
            return self._save(value)

    def cancel(self, reservation_id):
        with self.runtime.mutex:
            value = self.get(reservation_id)
            if value['status'] not in WAITING: raise ConflictError('Only a waiting reservation can be cancelled; stop an existing job separately')
            self._finish(value, 'cancelled', 'Cancelled by user')
            self._cascade()
            return value

    def _cascade(self):
        # Creation can only refer to existing jobs/reservations, forming a DAG.
        changed = True
        while changed:
            changed = False
            for value in self.list():
                if value['status'] not in WAITING: continue
                for prerequisite in value['prerequisites']:
                    future = self._future(prerequisite['job_id'])
                    if future and future['status'] in FINISHED-{'dispatched'}:
                        self._finish(value, 'blocked', 'Prerequisite reservation '+future['id']+' is '+future['status'])
                        changed = True
                        break

    def _hold(self, value, cases, now):
        """Persist a hold before evaluating timeouts or dispatch conditions."""
        identifiers = sorted(set(cases))
        if value['status'] == 'waiting_agent' and value.get('held_case_ids') == identifiers:
            return
        value.setdefault('hold_started_unix_s', now)
        value.update(status='waiting_agent', held_case_ids=identifiers,
                     message='Failure recovery or human review is required; reservation deadline is paused')
        self.windows.pop(value['id'], None)
        self._save(value)

    def _unhold(self, value, now):
        if value['status'] != 'waiting_agent': return
        value['expires_unix_s'] += max(0, now-value.pop('hold_started_unix_s', now))
        value.pop('held_case_ids', None)
        value.update(status='pending', message='Failure recovery verified; checking prerequisites again')
        self._save(value)

    def _observe_failures(self, values):
        """Close the notifier race without sending messages or touching devices.

        Frozen failure targets identify jobs opted into the current policy. Old
        unrelated history is not retroactively enrolled. Explicit prerequisites
        are always observed, including jobs from older workers.
        """
        from ldcon.agents.failure_policy import observe_job, list_cases
        sources = set()
        target_sets = {value['target']['set_id'] for value in values}
        target_lasers = {self.runtime.get_set(set_id)['laser_definition']['document']['id']
                         for set_id in target_sets}
        for value in values:
            for condition in value['prerequisites']:
                try:
                    self.runtime.directory(condition['job_id'])
                    sources.add(condition['job_id'])
                except FileNotFoundError:
                    pass  # A future reserved job has no failure evidence yet.
        for job in self.runtime.job_headers():
            if (job.get('failure_targets') or job.get('agent_fallbacks') or job.get('teams_fallbacks')) and (
                job.get('set_id') in target_sets or job.get('laser', {}).get('id') in target_lasers):
                sources.add(job['id'])
        for job_id in sorted(sources):
            observe_job(self.runtime.root, self.runtime.directory(job_id))
        return list_cases(self.runtime.root)

    def _holds(self, value, cases):
        laser_id = self.runtime.get_set(value['target']['set_id'])['laser_definition']['document']['id']
        source_ids = {condition['job_id'] for condition in value['prerequisites']}
        held = [case['id'] for case in cases if case.get('blocks_downstream') and (
            case.get('job_id') in source_ids or case.get('set_id') == value['target']['set_id']
            or case.get('laser_id') == laser_id)]
        for condition in value['prerequisites']:
            future = self._future(condition['job_id'])
            if future and future['status'] == 'waiting_agent':
                held.extend(future.get('held_case_ids', [future['id']]))
        return held

    def _replacement(self, job, cases, kind):
        """A recovery report is not evidence that the failed job succeeded."""
        recovered = [case for case in cases if case.get('job_id') == job['id']
                     and case.get('instance') == 'main' and case.get('disposition') == 'resolved']
        replacement_ids = {case.get('replacement_job_id') for case in recovered}
        if not recovered or len(replacement_ids) != 1 or None in replacement_ids:
            return None
        candidate = self.runtime.get_job(next(iter(replacement_ids)))
        source_cards = {(card['id'], card.get('card')) for card in job.get('cards', [])}
        replacement_cards = {(card['id'], card.get('card')) for card in candidate.get('cards', [])}
        directory = self.runtime.directory(candidate['id'])
        status_valid = (candidate.get('status') == 'succeeded'
                        and read_json(directory/'summary.json', {}).get('status') == 'succeeded') if kind == 'succeeded' else candidate.get('status') == 'running'
        if (candidate.get('set_id') != job.get('set_id') or candidate.get('deck_id') != job.get('deck_id')
                or source_cards != replacement_cards or not status_valid
                or (directory/'device-review.json').exists()):
            raise ValueError('Recovery replacement has no clean matching successful result')
        return candidate

    def _locked(self, value, condition, job, now, monotonic):
        directory = self.runtime.directory(job['id'])
        reading = read_json(directory/'readings.json', {})
        valid = ((job.get('controller_state') == 'locked' and reading.get('locked') is True)
            or (job.get('controller_state') in ('slow_servo', 'current_servo')
                and reading.get('control_valid') is True and reading.get('within_requested_tolerance') is True))
        valid = valid and job.get('worker_attachment') == 'attached'
        # A progress heartbeat can rewrite old readings; require advancing trace
        # timestamps and compare trace age with worker elapsed time as well.
        try:
            age = now-datetime.fromisoformat(job['updated_utc']).timestamp()
            sample = float(reading['time_s'])
            lag = float(job['simulated_s'])-sample
            valid = valid and all(math.isfinite(x) for x in (age, sample, lag)) and -.001 <= age <= condition['max_age_s'] and 0 <= lag <= condition['max_age_s']
        except (KeyError, TypeError, ValueError): valid = False; sample = None
        windows = self.windows.setdefault(value['id'], {})
        prior = windows.get(job['id'])
        if not valid:
            windows.pop(job['id'], None)
            return False
        if prior is None or monotonic-prior['last_at'] > condition['max_age_s'] or sample < prior['sample']:
            windows[job['id']] = dict(start=monotonic, last_at=monotonic, sample=sample, samples=1)
            return False
        if sample == prior['sample']: return False
        prior.update(last_at=monotonic, sample=sample, samples=prior['samples']+1)
        return prior['samples'] >= 3 and monotonic-prior['start'] >= condition['stable_s']

    def _ready(self, value, now, monotonic, cases=()):
        evidence = []
        failure_context = []
        for condition in value['prerequisites']:
            job = self._job(condition['job_id'])
            status = job.get('status')
            relevant = [case for case in cases if case.get('job_id') == job['id']]
            failure_context.extend(dict(case.get('failure_context') or {},
                case_id=case['id'], job_id=job['id'], card_id=case['card_id'], code=case['code'])
                for case in relevant if case.get('disposition') == 'no_agent' and not case.get('blocks_downstream'))
            if status in (TERMINAL-{'succeeded'}) | {'stopping'}:
                replacement = self._replacement(job, relevant, condition['condition']) if (
                    status in {'failed', 'interrupted', 'completed_with_failures'}) else None
                if replacement:
                    evidence.append(dict(job_id=job['id'], status=status, replacement_job_id=replacement['id']))
                    if value['online'] and condition['condition'] == 'locked' and (
                            value['target']['controller_job_id'] == job['id'] or value.get('recovery_controller', {}).get('job_id') == job['id']):
                        association = dict(job_id=job['id'], replacement_job_id=replacement['id'])
                        prior = value.get('recovery_controller')
                        if prior is None:
                            value['recovery_controller'] = association
                            value['target']['controller_job_id'] = replacement['id']
                            value['output_job_id'] = replacement['id']
                            value['controller_settings_revision'] = replacement.get('live_controller', {}).get('settings_revision')
                            self._save(value)
                        elif prior != association:
                            raise ValueError('Verified recovery controller changed; review the reservation again')
                    job = replacement
                    status = job['status']
                elif (status in {'failed', 'interrupted', 'completed_with_failures'}
                        and condition['condition'] == 'succeeded' and not condition['predicates']
                        and relevant and all(case.get('disposition') == 'no_agent'
                                             and not case.get('blocks_downstream') for case in relevant)
                        and not (self.runtime.directory(job['id'])/'device-review.json').exists()):
                    evidence.append(dict(job_id=job['id'], status=status, failure_forwarded=True))
                    continue
                else:
                    self._finish(value, 'blocked', 'Prerequisite '+job['id']+' is '+str(status))
                    return False
            if condition['condition'] == 'succeeded':
                if status != 'succeeded': return False
                result = read_json(self.runtime.directory(job['id'])/'summary.json', {})
                if result.get('status') != 'succeeded':
                    self._finish(value, 'blocked', 'Missing successful final result for '+job['id'])
                    return False
                if any(result.get(key) is not expected for key, expected in condition['predicates'].items()):
                    self._finish(value, 'blocked', 'Result predicate not met for '+job['id'])
                    return False
                evidence.append(dict(job_id=job['id'], status=status, result=result))
            else:
                if status == 'succeeded':
                    self._finish(value, 'blocked', 'Controller ended before live locked readiness')
                    return False
                if status != 'running': return False
                if not self._locked(value, condition, job, now, monotonic): return False
                evidence.append(dict(job_id=job['id'], status=status, updated_utc=job.get('updated_utc'),
                                     reading=read_json(self.runtime.directory(job['id'])/'readings.json', {})))
        value['evidence'] = evidence
        value['failure_context'] = failure_context
        return True

    def tick(self, now=None, monotonic=None):
        now = time.time() if now is None else now
        monotonic = time.monotonic() if monotonic is None else monotonic
        with self.runtime.mutex:
            if self.runtime.admission_inhibited():
                # Do not count an inhibited interval as continuously locked.
                self.windows.clear()
                return
            self._cascade()
            waiting = [value for value in reversed(self.list()) if value['status'] in WAITING]
            if not waiting: return
            try:
                cases = self._observe_failures(waiting)
            except Exception as error:
                # Failure evidence must be available before releasing any hold.
                for value in waiting:
                    value['failure_observation_error'] = type(error).__name__
                    self._hold(value, value.get('held_case_ids', ['failure-observation-unavailable']), now)
                return
            for value in waiting:
                try:
                    holds = self._holds(value, cases)
                    if holds:
                        self._hold(value, holds, now)
                        continue
                    self._unhold(value, now)
                    if now >= value['expires_unix_s']:
                        self._finish(value, 'expired', 'Reservation deadline exceeded')
                        continue
                    target = value['target']
                    bundle = self.runtime.get_set(target['set_id'])
                    if bundle['revision'] != value['snapshot']['set_revision']:
                        self._finish(value, 'blocked', 'Configuration revision changed; create a new reservation after review')
                        continue
                    if not self._ready(value, now, monotonic, cases): continue
                    if not value['online']:
                        active = [j for j in self.runtime.active_jobs() if j['status'] not in TERMINAL]
                        from ldcon.core.card_admission import admission_class,job_owns_control_slot
                        card = next(c for d in bundle['deck_definitions'] if d['id'] == target['deck_id'] for c in d['document']['cards'] if c['id'] == target['card_id'])
                        laser = bundle['laser_definition']['document']
                        same_laser = [j for j in active if j.get('set_id') == target['set_id'] or j.get('laser', {}).get('id') == laser['id']]
                        independent = admission_class(laser,card,same_laser) != 'exclusive'
                        if len(active) >= self.runtime.max_concurrent_jobs or (not independent and any(job_owns_control_slot(j) for j in same_laser)):
                            continue
                    else:
                        controller = self.runtime.get_job(value['output_job_id'])
                        revision = controller.get('live_controller', {}).get('settings_revision')
                        if not revision: continue
                        if value['controller_settings_revision'] is None:
                            value['controller_settings_revision'] = revision
                            self._save(value)
                        elif revision != value['controller_settings_revision']:
                            self._finish(value, 'blocked', 'Controller settings revision changed')
                            continue
                    # A worker can publish a failure while readiness/capacity is
                    # being checked. Observe once more at the dispatch boundary,
                    # after confirming it is no longer holding those resources.
                    try:
                        cases = self._observe_failures([value])
                    except Exception as error:
                        value['failure_observation_error'] = type(error).__name__
                        self._hold(value, ['failure-observation-unavailable'], now)
                        continue
                    holds = self._holds(value, cases)
                    if holds:
                        self._hold(value, holds, now)
                        continue
                    # This persisted state is the dispatch claim. No exceptions
                    # after this point can return the reservation to pending.
                    value.update(status='dispatching', message='Start claimed; never automatically replay')
                    self._save(value)
                    if value['online']:
                        result = self.runtime.request_online(value['output_job_id'], target['card_id'], target['parameters'],
                            request_id=value['output_request_id'], reservation=value)
                    else:
                        result = self.runtime.start_interface_card(bundle, target['deck_id'], target['card_id'], target['parameters'],
                            job_id=value['output_job_id'], reservation=value)
                    value['dispatched_utc'] = utc_now()
                    self._finish(value, 'dispatched', 'Submitted to the existing job system; execution outcome belongs to the job')
                except Exception as error:
                    self._finish(value, 'review_required' if value['status'] == 'dispatching' else 'blocked', str(error))
            self._cascade()


def install_routes(app, prefix, assets, body):
    """Keep API integration shared with older dashboards during selective rollout."""
    import asyncio
    from fastapi import Request
    from fastapi.responses import FileResponse

    @app.get(prefix+'/reservations')
    def page(): return FileResponse(assets/'reservations.html', media_type='text/html')

    @app.get(prefix+'/api/reservations')
    def listing(): return {'reservations': app.state.runtime.reservations.list()}

    @app.get(prefix+'/api/reservations/{reservation_id}')
    def get(reservation_id: str): return app.state.runtime.reservations.get(reservation_id)

    @app.post(prefix+'/api/reservations', status_code=202)
    async def create(request: Request):
        return await asyncio.to_thread(app.state.runtime.reservations.create, await body(request))

    @app.post(prefix+'/api/reservations/{reservation_id}/cancel')
    async def cancel(reservation_id: str):
        return await asyncio.to_thread(app.state.runtime.reservations.cancel, reservation_id)
