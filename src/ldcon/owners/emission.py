"""Owner-side electrical emission transitions; no device access on import.

Electrical suppression and optical extinction are different observations. The
state is durable and fail-closed. Only the owner can execute an admitted recipe;
a caller supplies a desired state and request ID, never bounds or wiring.
"""
from copy import deepcopy
from contextlib import contextmanager
import hmac
import json
import math
import secrets
import time
import threading

from experiment_core.records import Journal, canonical_json, digest
from ldcon.core.instrument_client import BrokerError


def finite(value, name):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(name + ' must be a finite number')
    return float(value)


def interval(value, name):
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError(name + ' requires two bounds')
    lo, hi = (finite(v, name) for v in value)
    if not lo < hi:
        raise ValueError(name + ' bounds must increase')
    return lo, hi


def validate_policy(policy):
    """Drafts may be disabled; enabling requires every physical value explicitly."""
    if not isinstance(policy, dict) or type(policy.get('enabled')) is not bool:
        raise ValueError('Emission requires an explicit enabled flag')
    if policy['enabled'] is False:
        return deepcopy(policy)
    common = {'enabled', 'method', 'revision', 'current_bounds_a', 'off_current_max_a',
              'on_current_bounds_a', 'max_sample_age_s', 'verify_samples',
              'verify_timeout_s', 'sample_interval_s', 'current_uncertainty_a'}
    extras = {'modulation', 'monitor'} if policy.get('method') == 'current_modulation' else {'interlock_file'}
    if set(policy) != common | extras or policy.get('method') not in ('driver_enable', 'current_modulation'):
        raise ValueError('Incomplete or unknown emission policy fields')
    if not isinstance(policy['revision'], str) or not policy['revision']:
        raise ValueError('Emission commissioning revision is required')
    low, high = interval(policy['current_bounds_a'], 'current_bounds_a')
    on_low, on_high = interval(policy['on_current_bounds_a'], 'on_current_bounds_a')
    off = finite(policy['off_current_max_a'], 'off_current_max_a')
    uncertainty = finite(policy['current_uncertainty_a'], 'current_uncertainty_a')
    if low < 0 or uncertainty < 0 or not low <= off < on_low < on_high <= high:
        raise ValueError('Electrical OFF/ON evidence intervals must be distinct and within current bounds')
    for key in ('max_sample_age_s', 'verify_timeout_s', 'sample_interval_s'):
        if not 0 < finite(policy[key], key) <= 600:
            raise ValueError(key + ' must be in (0, 600] seconds')
    if type(policy['verify_samples']) is not int or not 1 <= policy['verify_samples'] <= 100:
        raise ValueError('verify_samples must be 1..100 distinct acquisitions')
    if policy['verify_timeout_s'] < policy['sample_interval_s'] * policy['verify_samples']:
        raise ValueError('Verification deadline cannot cover the requested samples')
    if policy['method'] == 'driver_enable':
        from pathlib import Path
        if not isinstance(policy['interlock_file'], str) or not Path(policy['interlock_file']).is_absolute():
            raise ValueError('A shared absolute owner interlock_file is required')
    else:
        m = policy['modulation']
        if not isinstance(m, dict) or set(m) != {'off_voltage_v', 'on_voltage_v', 'max_step_v', 'max_slew_v_per_s', 'update_interval_s', 'readback_tolerance_v'}:
            raise ValueError('Complete current-modulation trajectory is required')
        for key, value in m.items():
            n = finite(value, key)
            if key not in ('off_voltage_v', 'on_voltage_v') and n <= 0:
                raise ValueError(key + ' must be positive')
        validate_monitor(policy['monitor'])
    return deepcopy(policy)


def validate_monitor(config):
    required = {'endpoint', 'device_id', 'channel', 'calibration_revision',
                'amperes_per_volt', 'zero_voltage_v', 'uncertainty_a'}
    if not isinstance(config, dict) or set(config) != required:
        raise ValueError('A pinned calibrated current-monitor binding is required')
    from ldcon.core.instrument_client import validate_endpoint
    validate_endpoint(config['endpoint'])
    if any(not isinstance(config[k], str) or not config[k] for k in ('device_id', 'channel', 'calibration_revision')):
        raise ValueError('Current monitor identity, channel and calibration must be explicit')
    if finite(config['amperes_per_volt'], 'amperes_per_volt') == 0 or finite(config['uncertainty_a'], 'uncertainty_a') < 0:
        raise ValueError('Invalid current-monitor calibration')
    finite(config['zero_voltage_v'], 'zero_voltage_v')


def monitor_sample(snapshot, config):
    """Typed current-monitor/v1 source; host transfer time cannot invent acquisition time.

    The producer may be an oscilloscope owner or a dedicated ADC owner. It must
    provide a genuine acquisition ID/time, clipping and channel provenance.
    Existing Rigol NORM snapshots do NOT satisfy this contract.
    """
    if (not isinstance(snapshot,dict) or snapshot.get('schema') != 'ldcon.current-monitor/1' or
            snapshot.get('device_id') != config['device_id'] or snapshot.get('channel') != config['channel'] or
            snapshot.get('acquisition_time_known') is not True or snapshot.get('valid') is not True or
            snapshot.get('clipped') is not False):
        raise BrokerError('measurement', 'Current monitor needs verified identity, channel and a fresh unclipped acquisition')
    voltage = finite(snapshot['voltage_v'], 'monitor voltage')
    return dict(current_a=(voltage-config['zero_voltage_v']) * config['amperes_per_volt'],
                acquired_unix_s=snapshot['acquired_unix_s'], acquisition_id=snapshot['acquisition_id'],
                uncertainty_a=config['uncertainty_a'], calibration_revision=config['calibration_revision'])


CLEANUP_ACTIONS = frozenset({'stop', 'release', 'steady_hold', 'steady_sweep_cancel', 'ff_end',
    'assembly_cancel', 'assembly_finish', 'steady_study_stop', 'steady_pid_finish',
    'background_restore', 'background_release', 'background_abandon', 'background_pid_finish',
    'pid_finish', 'locked_sweep_restore', 'locked_sweep_cleanup', 'response_end', 'restore_reviewed_dither'})


class EmissionStore:
    """Shared owner interlock and immutable request receipts; callers never see tokens."""
    def __init__(self, path, laser_id):
        self.journal = Journal(path)
        self.laser_id = laser_id
        self._cleanup_context = threading.local()
        with self.journal.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS emission_state (laser TEXT PRIMARY KEY, document TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS emission_requests (laser TEXT, request_id TEXT, fingerprint TEXT, document TEXT, PRIMARY KEY(laser,request_id))')
            db.execute('INSERT OR IGNORE INTO emission_state VALUES (?,?)', (laser_id, canonical_json(dict(
                phase='unmanaged', inhibited=False, desired=None, request_id=None, optical_state='unknown'))))

    def private(self):
        with self.journal._mutex:
            return json.loads(self.journal._db.execute('SELECT document FROM emission_state WHERE laser=?', (self.laser_id,)).fetchone()[0])

    def status(self):
        return {k: v for k, v in self.private().items() if k != 'permit'}

    def update(self, *, expected_phase=None, require_not_cancelled=False, **values):
        with self.journal.transaction() as db:
            state = json.loads(db.execute('SELECT document FROM emission_state WHERE laser=?', (self.laser_id,)).fetchone()[0])
            if expected_phase is not None and state['phase'] != expected_phase:
                raise BrokerError('busy', 'Emission state changed before dispatch; inspect without replay')
            if require_not_cancelled and state.get('cancel_requested'):
                raise BrokerError('cancelled', 'Emission was cancelled; inhibit retained')
            state.update(values, updated_unix_s=time.time())
            db.execute('UPDATE emission_state SET document=? WHERE laser=?', (canonical_json(state), self.laser_id))
            if state.get('request_id'):
                db.execute('UPDATE emission_requests SET document=? WHERE laser=? AND request_id=?',
                           (canonical_json(state), self.laser_id, state['request_id']))
        return self.status()

    def reserve(self, request_id, desired, revision):
        if desired not in ('on', 'off') or not isinstance(request_id, str) or not 1 <= len(request_id) <= 128 or not request_id.isascii() or not all(c.isalnum() or c in '-_' for c in request_id):
            raise ValueError('A bounded request ID and on/off state are required')
        fingerprint = digest([desired, revision])
        with self.journal.transaction() as db:
            old = db.execute('SELECT fingerprint,document FROM emission_requests WHERE laser=? AND request_id=?', (self.laser_id, request_id)).fetchone()
            if old:
                if old[0] != fingerprint:
                    raise BrokerError('configuration', 'Emission request ID already binds different content')
                return {k:v for k,v in json.loads(old[1]).items() if k != 'permit'}
            state = json.loads(db.execute('SELECT document FROM emission_state WHERE laser=?', (self.laser_id,)).fetchone()[0])
            if state['phase'] in ('quiescing', 'executing', 'changing', 'verifying', 'unknown'):
                raise BrokerError('busy', 'Inspect the existing emission transition; it cannot be replayed or replaced')
            state = dict(phase='quiescing', inhibited=True, desired=desired, request_id=request_id,
                         policy_revision=revision, permit=secrets.token_hex(32), optical_state='unknown',
                         created_unix_s=time.time(), updated_unix_s=time.time())
            db.execute('UPDATE emission_state SET document=? WHERE laser=?', (canonical_json(state), self.laser_id))
            db.execute('INSERT INTO emission_requests VALUES (?,?,?,?)', (self.laser_id, request_id, fingerprint, canonical_json(state)))
        return self.status()

    def authorize_ld(self, permit, enabled):
        state = self.private()
        return (type(enabled) is bool and state['phase'] == 'changing' and
                (state['desired'] == 'on') == enabled and isinstance(permit, str) and
                hmac.compare_digest(permit, state.get('permit', '')))

    def require_control(self, action):
        state = self.private()
        if not state['inhibited']:
            return
        # Cleanup is allowed only before the electrical transition. Once OFF,
        # no stale old-session restore may relight the laser.
        reads = {'status', 'describe', 'inspect', 'read', 'observe', 'heartbeat',
                 'steady_heartbeat', 'steady_read', 'ff_status', 'assembly_status', 'policy_update_status',
                 'background_status','response_info','response_dither_info','steady_parameters_status',
                 'locked_sweep_status','ff_owner_state','ff_heartbeat','pid_state','pid_check','steady_study_status'}
        if action in reads or state['phase'] == 'quiescing' and (
                action in CLEANUP_ACTIONS or getattr(self._cleanup_context,'depth',0)):
            return
        raise BrokerError('configuration', 'Intentional emission inhibit: ' + state['phase'] + '; only the explicit emission transition may enable output')

    @contextmanager
    def cleanup_scope(self, action):
        # Only owner code can establish this thread-local scope. A caller cannot
        # mark a generic movement as cleanup. Existing cleanup validates its
        # checkpoint and may make nested owner calls under the same mutex.
        active = action in CLEANUP_ACTIONS and self.status()['phase']=='quiescing'
        depth = getattr(self._cleanup_context,'depth',0)
        if active:
            self._cleanup_context.depth=depth+1
        try:
            yield
        finally:
            self._cleanup_context.depth=depth

    def close(self):
        self.journal.close()


class EmissionController:
    """Host holds the owner mutex; adapter owns physical proofs and exact writes."""
    def __init__(self, store, policy, adapter, *, clock=time.monotonic, wall=time.time, sleep=time.sleep):
        self.store, self.policy, self.adapter = store, policy, adapter
        self.clock, self.wall, self.sleep = clock, wall, sleep
        self.thread = None
        self.stopping = threading.Event()
        if store.status()['phase'] in ('quiescing', 'executing', 'changing', 'verifying'):
            store.update(phase='unknown', reason='Owner restarted during emission transition; no command replayed')

    def config(self):
        p = validate_policy(self.policy() or {'enabled': False})
        if not p['enabled']:
            raise BrokerError('configuration', 'Emission is not commissioned/enabled for this laser')
        return p

    def reserve(self, request):
        self.check_running()
        p = self.config()
        if request.get('policy_revision') != digest(p):
            raise BrokerError('configuration', 'Emission policy changed; refresh capabilities')
        self.adapter.preflight(p)  # No cancellation or physical mutation on a missing binding.
        return self.store.reserve(request['request_id'], request['state'], digest(p))

    def execute(self, request):
        self.check_running()
        state=self.store.status()
        if request.get('request_id') != state['request_id']:
            raise BrokerError('ownership', 'Only the reserved emission request may execute')
        if state['phase'] != 'quiescing':
            return state
        p=self.config()
        if digest(p) != state['policy_revision']:
            raise BrokerError('configuration', 'Emission policy changed during quiescence')
        self.adapter.preflight(p)
        self.adapter.quiet(p)
        self.store.update(phase='executing',expected_phase='quiescing',require_not_cancelled=True)
        def work():
            try:
                with self.adapter.mutex:
                    self._execute(request)
            except BaseException as error:
                self.store.update(phase='unknown', inhibited=True, reason=str(error), electrical_verified=False)
        self.thread=threading.Thread(target=work, name='laser-emission', daemon=True)
        try:
            self.thread.start()
        except BaseException:
            self.store.update(phase='unknown', inhibited=True, reason='Emission executor could not start')
            raise
        return self.store.status()

    def _execute(self, request):
        state = self.store.status()
        if request.get('request_id') != state['request_id']:
            raise BrokerError('ownership', 'Only the reserved emission request may execute')
        if state['phase'] != 'executing':
            return state  # Includes unknown: never retry an uncertain physical effect.
        p = self.config()
        if digest(p) != state['policy_revision']:
            raise BrokerError('configuration', 'Emission policy changed during quiescence')
        self.adapter.quiet(p)  # Owner-confirmed PI/FF/lease cleanup; terminal job is insufficient.
        self.adapter.open(p)
        try:
            self.check_cancelled()
            before = self.adapter.snapshot(p)
            self.check_current(before['current'], p)
            self.adapter.validate_transition(p, state['desired'], before)
            self.store.update(phase='changing', expected_phase='executing', require_not_cancelled=True, before=before)
            write_finished = self.adapter.apply(p, state['desired'], self.store.private()['permit'])
            self.store.update(phase='verifying', write_finished_unix_s=write_finished)
            deadline = self.clock() + p['verify_timeout_s']
            ids, evidence = set(), []
            last_stamp = write_finished
            while self.clock() < deadline:
                self.check_cancelled()
                sample = self.adapter.snapshot(p)
                self.adapter.verify_preserved(before, sample, state['desired'])
                current = sample['current']
                self.check_current(current, p)
                stamp = finite(current['acquired_unix_s'], 'current acquisition time')
                identity = str(current['acquisition_id'])
                if stamp > last_stamp and identity not in ids:
                    last_stamp = stamp; ids.add(identity)
                    uncertainty = max(p['current_uncertainty_a'], current.get('uncertainty_a', 0.))
                    value = current['current_a']
                    low, high = (p['current_bounds_a'][0], p['off_current_max_a']) if state['desired'] == 'off' else p['on_current_bounds_a']
                    if low <= max(0., value-uncertainty) and 0 <= value+uncertainty <= high:
                        evidence.append(sample)
                    else:
                        evidence.clear()
                    if len(evidence) >= p['verify_samples']:
                        phase = 'output_disabled' if state['desired']=='off' and p['method']=='driver_enable' else 'current_suppressed' if state['desired']=='off' else 'enabled_unlocked'
                        self.adapter.close()
                        return self.store.update(phase=phase, require_not_cancelled=True, inhibited=state['desired']=='off', evidence=evidence,
                                                 electrical_verified=True, verified_at_unix_s=self.wall(), optical_state='unknown', reason=None)
                self.sleep(p['sample_interval_s'])
            raise BrokerError('measurement', 'Fresh post-command current verification did not complete before its deadline')
        except BaseException as error:
            self.store.update(phase='unknown', inhibited=True, reason=str(error), electrical_verified=False)
            # Do not restore ON or release uncertain physical ownership.
            raise

    def check_running(self):
        if self.stopping.is_set() or getattr(self.adapter,'stopping',False):
            raise BrokerError('cancelled','Owner is shutting down; no emission command admitted')

    def shutdown(self, timeout_s=8.):
        self.stopping.set()
        state=self.store.status()
        if state['phase'] in ('quiescing','executing','changing','verifying'):
            self.store.update(cancel_requested=True)
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout_s)
        if self.store.status()['phase'] in ('quiescing','executing','changing','verifying'):
            self.store.update(phase='unknown',inhibited=True,electrical_verified=False,
                              reason='Owner shutdown interrupted emission transition; no command replayed')
        return self.thread is None or not self.thread.is_alive()

    def check_cancelled(self):
        self.check_running()
        if self.store.status().get('cancel_requested'):
            raise BrokerError('cancelled', 'Emission cancelled; actual electrical state needs inspection; no automatic ON')

    def check_current(self, sample, p):
        current = finite(sample['current_a'], 'measured current')
        stamp = finite(sample['acquired_unix_s'], 'current acquisition time')
        if not isinstance(sample.get('acquisition_id'), (str, int)) or isinstance(sample.get('acquisition_id'), bool):
            raise BrokerError('measurement', 'A distinct current acquisition identity is required')
        if not 0 <= self.wall()-stamp <= p['max_sample_age_s']:
            raise BrokerError('measurement', 'Measured current is stale or from a future clock')
        low, high = p['current_bounds_a']
        uncertainty = max(p['current_uncertainty_a'], finite(sample.get('uncertainty_a',0.), 'current uncertainty'))
        if not low <= max(0., current-uncertainty) <= current+uncertainty <= high:
            raise BrokerError('range', 'Measured current including uncertainty exceeds emission policy')

    def dispatch(self, request):
        action = request['action']
        if action == 'emission_status':
            raw = self.policy() or {'enabled': False}
            state=self.store.status()
            stamp=state.get('verified_at_unix_s')
            return dict(state, configured=raw.get('enabled') is True, policy_revision=digest(raw),
                        verification_age_s=None if stamp is None else self.wall()-stamp,
                        observation_scope='last_transition; not continuous optical extinction verification')
        if action == 'emission_reserve':
            return self.reserve(request)
        if action == 'emission_execute':
            return self.execute(request)
        if action == 'emission_cancel':
            state = self.store.status()
            if request.get('request_id') != state['request_id']:
                raise BrokerError('ownership', 'Cancellation belongs to another emission request')
            if state['phase'] == 'quiescing':
                return self.store.update(phase='cancelled', inhibited=True, cancel_requested=True, reason='Cancelled before output transition; no automatic ON')
            if state['phase'] in ('executing','changing','verifying'):
                return self.store.update(cancel_requested=True)
            return state
        raise BrokerError('request', 'Unknown emission operation')
