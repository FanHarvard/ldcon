"""Durable, asynchronous FF assembly hosted by the native laser system owner.

The caller supplies a plan and an existing native lease. Instrument endpoints,
trust bindings and the AWG factory are installed by the host at startup. A start
is never replayed after lost acknowledgement, thread failure or owner restart.
"""
from copy import deepcopy
from contextlib import contextmanager
import json
import math
import threading
import time

from experiment_core.records import canonical_json, digest
from ldcon.owners.esp32_native_client import NativeCurrentPort
from ldcon.core.instrument_client import BrokerError


class LocalNativePort(NativeCurrentPort):
    """Typed native port attached to an existing PI lease, without a second RPC."""
    def __init__(self, system, lease):
        self.system = system
        self.endpoint = 'local-system-owner'
        self.device_id = system.instrument.device_id
        self.policy = system.policy
        self.ff_profile = deepcopy(system.ff_profile)
        self.ff_profile_digest = system.runtime.ff_definition
        self.lease = lease
        self.source_revision = system.authority.revision
        self.uncertain_output = False
        self.active = system.runtime.active
        self.gain_state = None
        self.ff_token = None
        self.timeout_s = 30.
        self.unwind_timeout_s = (self.ff_profile['unwind_steps'] * self.ff_profile['unwind_interval_s'] +
            self.ff_profile['missing_timeout_s'] + 2 * self.policy['max_sample_age_s']) if self.ff_profile else 0.

    def _call(self, action, **values):
        if action in {'claim', 'release', 'stop', 'arm'}:
            raise BrokerError('ownership', 'An assembly cannot replace or stop its parent PI lease')
        if self.system.runtime.lease != self.lease:
            raise BrokerError('ownership', 'Assembly parent lease changed')
        if (action not in {'status', 'describe', 'heartbeat', 'ff_status', 'ff_owner_state', 'ff_heartbeat', 'ff_end'}
                and self.system.authority.revision != self.source_revision):
            raise BrokerError('configuration', 'Assembly policy changed; new writes refused')
        return self.system.dispatch(dict(action=action, device_id=self.device_id, **values))

    def connect(self):
        return self

    def close(self):
        # ProxiedFFSession owns FF/AWG cleanup; only its parent owns PI stop/release.
        return None


class NativeAssemblyHost:
    def __init__(self, system, factory):
        self.system, self.factory = system, factory
        self.journal = system.journal
        self._threads = {}
        self._mutex = threading.RLock()
        self._parent_stopping = False
        with self.journal.transaction() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS native_assembly_sessions (
                session_id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL,
                request TEXT NOT NULL, lease_digest TEXT NOT NULL, state TEXT NOT NULL,
                status TEXT NOT NULL, result TEXT, trigger TEXT, cancelled INTEGER NOT NULL,
                finished INTEGER NOT NULL)''')
            # Never restart a previous thread or infer cleanup from process death.
            db.execute("UPDATE native_assembly_sessions SET state='uncertain' WHERE state='running'")

    def _row(self, session_id):
        with self.journal._mutex:
            row = self.journal._db.execute('SELECT * FROM native_assembly_sessions WHERE session_id=?',
                                         (session_id,)).fetchone()
        if row is None:
            raise BrokerError('request', 'Unknown native assembly session')
        return dict(row)

    @staticmethod
    def _response(row):
        return dict(session_id=row['session_id'], state=row['state'],
                    result=json.loads(row['result']) if row['result'] else None,
                    status=json.loads(row['status']))

    def active(self):
        with self.journal._mutex:
            return self.journal._db.execute("SELECT 1 FROM native_assembly_sessions WHERE state IN ('running','uncertain','review_required') LIMIT 1").fetchone() is not None

    def dispatch(self, request):
        self.system.authority._current()
        if request.get('device_id') != self.system.instrument.device_id:
            raise BrokerError('configuration', 'Native device identity mismatch')
        action = request['action']
        if action == 'assembly_start':
            return self.start(request)
        with self._mutex:
            row = self._row(request.get('session_id'))
            if not request.get('lease') or digest(request['lease']) != row['lease_digest']:
                raise BrokerError('ownership', 'Exact assembly parent lease required')
            if action == 'assembly_status':
                return self._response(row)
            if action not in {'assembly_trigger', 'assembly_cancel', 'assembly_finish'}:
                raise BrokerError('action', 'Unknown native assembly operation')
            if row['state'] != 'running':
                return self._response(row)
            with self.journal.transaction() as db:
                if action == 'assembly_cancel':
                    db.execute('UPDATE native_assembly_sessions SET cancelled=1 WHERE session_id=?', (row['session_id'],))
                elif action == 'assembly_finish':
                    db.execute('UPDATE native_assembly_sessions SET finished=1 WHERE session_id=?', (row['session_id'],))
                else:
                    trigger = deepcopy(request.get('record'))
                    status = json.loads(row['status'])
                    plan = json.loads(row['request'])['session_request']
                    if row['trigger']:
                        if canonical_json(trigger) != row['trigger']:
                            raise BrokerError('request', 'Assembly trigger already binds different content')
                    elif (plan.get('trigger', 'software') != 'software' or not status.get('armed') or
                          not status.get('ready') or not isinstance(trigger, dict) or
                          trigger.get('plan_sha256') != plan.get('plan_sha256')):
                        raise BrokerError('request', 'Assembly trigger requires the exact armed ready plan')
                    else:
                        db.execute('UPDATE native_assembly_sessions SET trigger=? WHERE session_id=?',
                                   (canonical_json(trigger), row['session_id']))
            return self._response(self._row(row['session_id']))

    def start(self, request):
        if self.factory is None:
            raise BrokerError('configuration', 'Native assembly requires a trusted startup binding')
        runtime = self.system.runtime
        with runtime.io_lock, self._mutex:
            if not runtime.lease or request.get('lease') != runtime.lease:
                raise BrokerError('ownership', 'Exact native device lease required')
            session_request = deepcopy(request.get('session_request'))
            target = request.get('target_hz')
            if (not isinstance(session_request, dict) or not isinstance(session_request.get('request_id'), str)
                    or not session_request['request_id'] or len(session_request['request_id']) > 200
                    or type(target) not in (int, float) or not math.isfinite(target) or target <= 0):
                raise BrokerError('request', 'Assembly requires an exact request ID, plan and finite target')
            body = dict(session_request=session_request, target_hz=target,
                        policy_revision=self.system.authority.revision, ff_profile_digest=runtime.ff_definition)
            serialized = canonical_json(body)
            session_id = 'assembly.' + digest([self.system.authority.system_id, session_request['request_id']])
            with self.journal.transaction() as db:
                old = db.execute('SELECT * FROM native_assembly_sessions WHERE session_id=?', (session_id,)).fetchone()
                if old:
                    if old['request'] != serialized or old['lease_digest'] != digest(runtime.lease):
                        raise BrokerError('request', 'Assembly request ID already binds different content or lease')
                    return self._response(dict(old))
            if self._parent_stopping or runtime.fault or not runtime.active or self.active():
                raise BrokerError('busy', 'Native assembly requires an active PI and no unresolved assembly')
            port = LocalNativePort(self.system, runtime.lease)
            from ldcon.jobs.native_ff_proxy import ProxiedFFSession
            # Validate trigger/ready syntax before committing any execution intent.
            ProxiedFFSession(port, None, session_request, self._queue(session_id))
            with self.journal.transaction() as db:
                db.execute('INSERT INTO native_assembly_sessions VALUES (?,?,?,?,?,?,NULL,NULL,0,0)',
                           (session_id, session_request['request_id'], serialized, digest(runtime.lease),
                            'running', canonical_json({'state': 'preparing', 'armed': False, 'ready': False})))
            thread = threading.Thread(target=self._run, args=(session_id, port),
                                      name='native-' + session_id[:28], daemon=True)
            self._threads[session_id] = thread
            thread.start()
            return self._response(self._row(session_id))

    def _cleanup_budget(self):
        profile = self.system.ff_profile or {}
        return (profile.get('unwind_steps', 0) * profile.get('unwind_interval_s', 0) +
                profile.get('missing_timeout_s', 0) + 2 * self.system.policy['max_sample_age_s'] + 5.)

    @contextmanager
    def parent_boundary(self, request):
        """A parent stop cannot release PI/bias while its AWG child is executing."""
        with self.system.runtime.io_lock, self._mutex:
            if not self.system.runtime.lease or request.get('lease') != self.system.runtime.lease:
                raise BrokerError('ownership', 'Exact native device lease required')
            if self._parent_stopping:
                raise BrokerError('busy', 'Parent cleanup is already in progress')
            self._parent_stopping = True
            with self.journal.transaction() as db:
                db.execute("UPDATE native_assembly_sessions SET cancelled=1 WHERE state='running'")
        try:
            deadline = time.monotonic() + self._cleanup_budget()
            for thread in list(self._threads.values()):
                thread.join(max(0., deadline-time.monotonic()))
            if self.active():
                raise BrokerError('busy', 'Assembly cleanup is unresolved; native and bias leases retained')
            yield
        finally:
            with self._mutex:
                self._parent_stopping = False

    def _queue(self, session_id):
        host = self
        class Queue:
            def write_status(self, request_id, status):
                host.system.authority._current()
                with host.journal.transaction() as db:
                    db.execute('UPDATE native_assembly_sessions SET status=? WHERE session_id=?',
                               (canonical_json(dict(status, unix_s=time.time())), session_id))
            def cancelled(self, request_id):
                return bool(host._row(session_id)['cancelled'])
            def finished(self, request_id):
                return bool(host._row(session_id)['finished'])
            def trigger_record(self, request_id):
                value = host._row(session_id)['trigger']
                return json.loads(value) if value else None
        return Queue()

    def _run(self, session_id, port):
        body = json.loads(self._row(session_id)['request'])
        queue = self._queue(session_id)
        try:
            self.system.authority._current()
            if queue.cancelled(None) or self.system.runtime.stop.is_set():
                result = dict(status='cancelled', errors=[], restored=True)
            else:
                from ldcon.jobs.native_ff_proxy import ProxiedFFSession, recentre_before_ff
                cancel = lambda: queue.cancelled(None) or self.system.runtime.stop.is_set()
                emit = self.system.runtime._report_assembly_event
                if body['session_request'].get('recentre'):
                    laser = deepcopy(getattr(self.factory, 'laser_snapshot', None))
                    if laser is None:
                        raise BrokerError('configuration', 'Recentre requires the trusted assembly definition')
                    recentred = recentre_before_ff(port, laser, body['target_hz'], clock=time.monotonic,
                                                   sleep=time.sleep, cancel=cancel, emit=emit)
                    if recentred['status'] == 'refused':
                        result = dict(status='rejected', errors=[recentred['reason']], restored=not port.uncertain_output)
                        with self.journal.transaction() as db:
                            db.execute('UPDATE native_assembly_sessions SET state=?,result=? WHERE session_id=?',
                                       ('rejected' if result['restored'] else 'review_required', canonical_json(result), session_id))
                        return
                awg = self.factory(self.system, deepcopy(body['session_request']))
                session = ProxiedFFSession(port, awg, body['session_request'], queue,
                                           cancel=self.system.runtime.stop.is_set, emit=emit)
                result = session.run(body['target_hz'])
            state = result['status'] if result.get('restored') else 'review_required'
        except BaseException as error:
            result = dict(status='review_required', restored=False, errors=[str(error)])
            state = 'uncertain'
        try:
            self.system.authority._current()
        except Exception:
            return  # A replacement owner retains the durable uncertain record.
        with self.journal.transaction() as db:
            db.execute('UPDATE native_assembly_sessions SET state=?,result=? WHERE session_id=?',
                       (state, canonical_json(result), session_id))

    def shutdown(self):
        with self.journal.transaction() as db:
            db.execute("UPDATE native_assembly_sessions SET cancelled=1 WHERE state='running'")
        # Keep the independent unwind supervisor running during bounded cleanup.
        deadline = time.monotonic() + self._cleanup_budget()
        while any(thread.is_alive() for thread in self._threads.values()) and time.monotonic() < deadline:
            self.system.poll()
            for thread in list(self._threads.values()):
                thread.join(.05)
        if any(thread.is_alive() for thread in self._threads.values()):
            self.system.instrument.guard.quarantine('Assembly shutdown deadline exceeded; review required')
