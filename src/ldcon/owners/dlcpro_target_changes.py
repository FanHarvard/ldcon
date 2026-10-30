"""Attached target changes, advanced only by the leased DLC owner's tick.

Admission never claims a controller lease. Every physical step uses set_target;
only verified steps are saved. A lost/unknown write or interrupted operation is
never resumed. GET and cancel never drive an output or restore an old target.
"""
from copy import deepcopy
import json
import math
from pathlib import Path
import re
import time
import uuid

from ldcon.core.contracts import MeasurementUnavailable
from ldcon.core.interface_schema import digest
from ldcon.core.target_policy import laser_target, target_bounds
from ldcon.jobs.lock_target import DEFAULTS
from ldcon.owners.dlcpro_target_store import read_laser, save_target, target_file_guard, write_record

REQUEST_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}')
FINAL = {'settled', 'failed', 'cancelled'}


class TargetError(ValueError):
    def __init__(self, status, code, detail, **extra):
        super().__init__(detail)
        self.payload = dict(status=status, code=code, detail=detail, **extra)


def request_body(body):
    if not isinstance(body, dict) or set(body) - {'target_frequency_hz', 'request_id', 'timeout_s'}:
        raise TargetError(400, 'invalid_request', 'Expected target_frequency_hz, request_id and optional timeout_s')
    target, identity = body.get('target_frequency_hz'), body.get('request_id')
    timeout = body.get('timeout_s', DEFAULTS['timeout_s'])
    timeout = DEFAULTS['timeout_s'] if timeout is None else timeout
    if type(target) not in (int, float) or not math.isfinite(target) or target <= 0:
        raise TargetError(400, 'invalid_request', 'target_frequency_hz must be positive and finite')
    if not isinstance(identity, str) or not REQUEST_ID.fullmatch(identity):
        raise TargetError(400, 'invalid_request', 'Invalid request_id')
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 86400:
        raise TargetError(400, 'invalid_request', 'timeout_s must be in (0, 86400]')
    return dict(target_frequency_hz=float(target), request_id=identity, timeout_s=float(timeout))


class TargetChanges:
    def __init__(self, protocol, laser_file, state_directory, *, wall_clock=time.time):
        self.protocol, self.owner = protocol, protocol.owner
        self.laser_file = Path(laser_file)
        self.root = Path(state_directory)
        self.root.mkdir(parents=True, exist_ok=True)
        self.wall_clock = wall_clock
        self.records = {}
        for path in self.root.glob('*.json'):
            record = json.loads(path.read_text())
            if record['laser'] != self.owner.laser_id or path.stem != record['change_id']:
                raise ValueError('DLC target receipt identity mismatch')
            self.records[record['change_id']] = record
            if record['state'] not in FINAL:
                self._finish(record, 'failed', 'Owner restarted; target operation interrupted and never resumed',
                             review_required=record['state'] == 'applying')

    def _save(self, record):
        record['updated_unix_s'] = self.wall_clock()
        try:
            write_record(self.root / (record['change_id'] + '.json'), record)
        except OSError as error:
            # A durable receipt failure is not a transient optical read failure.
            raise RuntimeError('Cannot persist target receipt; inspect before another step') from error

    def _finish(self, record, state, message, **extra):
        record.update(state=state, message=message, **extra)
        self._save(record)
        return self.view(record)

    def active(self):
        return next((r for r in self.records.values() if r['state'] not in FINAL or r.get('review_required')), None)

    def _laser(self):
        laser, rev = read_laser(self.laser_file)
        interface = laser.get('interfaces', {}).get('dlc_lock', {})
        device = laser.get('devices', {}).get(interface.get('device'), {})
        if laser.get('id') != self.owner.laser_id or digest(device) != digest(self.protocol.device):
            raise TargetError(409, 'configuration_changed', 'Laser definition differs from the attached owner')
        if laser.get('allow_output') is not True or device.get('control_settings', {}).get('commissioned') is not True:
            raise TargetError(409, 'not_configured', 'DLC output/commissioning is not enabled')
        return laser, rev

    def _available(self):
        if self.owner.fault:
            raise TargetError(409, 'owner_fault', 'Owner requires inspection')
        if not self.protocol.client or self.owner.mode != 'lock':
            raise TargetError(409, 'busy', 'A running DLC controller lease is required', retry_after_s=1)
        sweep = getattr(self.owner, 'awg_sweep', None)
        if sweep is not None and sweep.active():
            raise TargetError(409, 'busy', 'An AWG sweep is armed or active', retry_after_s=1)
        if self.owner.clock() - self.owner.last_client > self.owner.policy['client_timeout_s']:
            raise TargetError(409, 'busy', 'Controller heartbeat is stale', retry_after_s=1)

    def view(self, record):
        keys = ('laser', 'request_id', 'change_id', 'target_frequency_hz', 'before_hz',
                'state', 'message', 'updated_unix_s', 'frequency_hz', 'error_hz',
                'ramped_target_hz', 'saved_target_hz', 'applied_steps', 'review_required',
                'verify', 'admitted_bounds_hz')
        result = {key: deepcopy(record[key]) for key in keys if key in record}
        result.update(final=record['state'] in FINAL,
                      applied_to_lock=record['state'] == 'settled',
                      ramp_active=record['state'] not in FINAL and self.owner.target != record['target_frequency_hz'])
        return result

    def find(self, change_id=None, request_id=None):
        record = self.records.get(change_id) if change_id else next(
            (r for r in self.records.values() if r['request_id'] == request_id), None)
        if record is None:
            raise TargetError(404, 'not_found', 'Unknown target request')
        return record

    def start(self, body):
        body = request_body(body)
        previous = next((r for r in self.records.values() if r['request_id'] == body['request_id']), None)
        if previous is not None:
            if previous['body'] != body:
                raise TargetError(409, 'request_conflict', 'request_id identifies a different immutable request')
            return dict(status=200, change=self.view(previous))
        self._available()
        if self.active() is not None:
            raise TargetError(409, 'busy', 'Earlier target change is active or requires inspection', retry_after_s=1)
        with target_file_guard(self.laser_file):
            laser, rev = self._laser()
            block, bounds = laser_target(laser), target_bounds(laser)
            if block is None or bounds is None or block.get('max_change_hz') is None:
                raise TargetError(409, 'not_configured', 'Target bounds and maximum step must be configured')
            target = body['target_frequency_hz']
            if not bounds[0] <= target <= bounds[1]:
                raise TargetError(422, 'out_of_bounds', 'Target is outside the frozen admission range', bounds_hz=bounds)
            now = self.owner.clock()
            record = dict(**body, body=body, change_id=uuid.uuid4().hex, laser=self.owner.laser_id,
                          state='accepted', message='Accepted; waiting for fresh optical settlement',
                          before_hz=self.owner.target, ramped_target_hz=self.owner.target,
                          saved_target_hz=block['frequency_hz'], config_revision=rev,
                          admitted_bounds_hz=bounds, step_hz=min(block['max_change_hz'], self.owner.control['capture_hz']),
                          tolerance_hz=self.owner.control['tolerance_hz'],
                          controller_lease=self.protocol.client, target_revision=self.owner.target_revision,
                          created_s=now, deadline_s=now + body['timeout_s'], created_unix_s=self.wall_clock(),
                          acquisition_fence_s=now, samples=[], applied_steps=0)
            self._save(record)  # Receipt exists before any PI preemption or physical write.
            self.records[record['change_id']] = record
        return dict(status=202, change=self.view(record))

    def cancel(self, change_id):
        record = self.find(change_id=change_id)
        if record['state'] not in FINAL:
            self._finish(record, 'cancelled', 'Cancelled; last confirmed target retained, no rollback')
        return dict(status=200, change=self.view(record))

    def get_target(self):
        laser, _ = self._laser()
        block = laser_target(laser)
        change = self.active()
        sample = None
        try:
            sample = self.owner.read_control()
        except (MeasurementUnavailable, OSError):
            pass
        locked = bool(sample and self.owner.mode == 'lock' and not self.owner.fault and
                      abs(sample.frequency_hz - self.owner.target) <= self.owner.control['tolerance_hz'])
        return dict(laser=self.owner.laser_id, frequency_hz=block['frequency_hz'],
                    wavelength_nm=299792458e9 / block['frequency_hz'], bounds_hz=target_bounds(laser),
                    max_change_hz=min(block['max_change_hz'], self.owner.control['capture_hz'])
                    if self.owner.mode == 'lock' else block['max_change_hz'],
                    locked=locked, follows_laser_target=True, queue_supported=False,
                    lock_state=self.owner.mode, lock_job=None,
                    measured_frequency_hz=sample.frequency_hz if sample else None,
                    ramped_target_hz=self.owner.target,
                    change=self.view(change) if change else None)

    def tick(self):
        """Call under owner.mutex before its ordinary PI tick; never from GET."""
        record = self.active()
        if record is None or record['state'] in FINAL:
            return
        try:
            self._available()
            if (self.protocol.client != record['controller_lease'] or
                    self.owner.target_revision != record['target_revision']):
                self._finish(record, 'failed', 'Controller or target changed; newer target retained')
                return
            if self.owner.clock() >= record['deadline_s']:
                self._finish(record, 'failed', 'Target settlement timed out; last confirmed target retained')
                return
            # UDP read_control reads the validated receiver cache. Check that
            # cache before any file lock, YAML parse, hash or durable receipt.
            sample = self.owner.read_control()
            if sample.acquired_s <= record['acquisition_fence_s']:
                return
            if record['state'] in ('accepted', 'ramping'):
                if (sample.acquired_s <= record.get('last_step_attempt_s', float('-inf')) or
                        self.owner.clock() - self.owner.last_pi_write <
                        self.owner.policy['axes']['piezo']['update_interval_s']):
                    return
                if abs(sample.frequency_hz - self.owner.target) > self.owner.control['capture_hz']:
                    raise MeasurementUnavailable('Current frequency is outside capture before target step')
                # A configuration editor must not block the primary PI cycle.
                with target_file_guard(self.laser_file, blocking=False):
                    if not self._configuration_current(record):
                        return
                    record['last_step_attempt_s'] = sample.acquired_s
                    self._step(record)
                return
            last = record.get('last_verification_acquisition_s')
            if last is not None and sample.acquired_s - last < DEFAULTS['sample_interval_s']:
                return
            self._verify(record, sample)
        except (MeasurementUnavailable, OSError) as error:
            # Only fresh read failures are safe waits. Physical writes/save are
            # handled by _step before reaching here.
            record.update(samples=[], message='Waiting for fresh optical frequency: ' + str(error))
        except TargetError as error:
            self._finish(record, 'failed', str(error))
        except Exception as error:
            self._finish(record, 'failed', 'Target operation failed: ' + str(error), review_required=True)

    def _configuration_current(self, record):
        """Under the file guard, at mutation/final-confirmation boundaries only."""
        _, rev = self._laser()
        if rev == record['config_revision']:
            return True
        self._finish(record, 'failed', 'Newer laser configuration retained; sequence stopped')
        return False

    def _step(self, record):
        o = self.owner
        current, target = o.target, record['target_frequency_hz']
        cap = min(record['step_hz'], o.control['capture_hz'])
        step = current + math.copysign(min(abs(target - current), cap), target - current)
        if abs(step - current) > cap:
            step = math.nextafter(step, current)
        if current != target and step == current:
            raise TargetError(422, 'step_too_small', 'Configured step is below frequency representation spacing')
        if not record['admitted_bounds_hz'][0] <= step <= record['admitted_bounds_hz'][1]:
            raise TargetError(422, 'out_of_bounds', 'Next step left the frozen admission range')
        if current != target:
            record['state'] = 'applying'
            self._save(record)  # Crash from here onward requires inspection, never replay.
            last_client = o.last_client
            try:
                o.set_target(step, record['target_revision'])
            except MeasurementUnavailable:
                if o.fault:
                    self._finish(record, 'failed', 'Uncertain target write; owner fault retained', review_required=True)
                    return
                record['state'] = 'ramping'
                self._save(record)
                raise
            except Exception as error:
                self._finish(record, 'failed', 'Target write outcome requires inspection: ' + str(error), review_required=True)
                return
            finally:
                o.last_client = last_client  # Attached API cannot renew the main controller lease.
            record.update(ramped_target_hz=o.target, target_revision=o.target_revision,
                          applied_steps=record['applied_steps'] + 1)
        else:
            o.pi_trial.preempt('foreground_target')
        try:
            record['config_revision'] = save_target(self.laser_file, record['config_revision'], o.target)
            record['saved_target_hz'] = o.target
        except Exception as error:
            self._finish(record, 'failed', 'Applied target but configuration save unresolved: ' + str(error), review_required=True)
            return
        record.update(state='verifying', samples=[], acquisition_fence_s=o.clock(),
                      message='Waiting for fresh optical settlement at the confirmed target')
        record.pop('last_verification_acquisition_s', None)
        self._save(record)

    def _verify(self, record, sample):
        samples = record['samples']
        record['last_verification_acquisition_s'] = sample.acquired_s
        if samples and sample.acquired_s - samples[-1]['acquired_s'] > self.owner.policy['max_sample_age_s'] + DEFAULTS['sample_interval_s']:
            samples.clear()
        error = sample.frequency_hz - self.owner.target
        record.update(frequency_hz=sample.frequency_hz,
                      error_hz=sample.frequency_hz - record['target_frequency_hz'],
                      updated_unix_s=self.wall_clock())
        tolerance = min(record['tolerance_hz'], self.owner.control['tolerance_hz'])
        if abs(error) > tolerance:
            samples.clear()
            return
        samples.append(dict(frequency_hz=sample.frequency_hz, acquired_s=sample.acquired_s,
                            sequence=sample.sequence, error_hz=error))
        if len(samples) < DEFAULTS['verify_samples']:
            return
        if self.owner.target != record['target_frequency_hz']:
            record.update(state='ramping', samples=[], acquisition_fence_s=sample.acquired_s)
            self._save(record)
            return
        with target_file_guard(self.laser_file, blocking=False):
            if not self._configuration_current(record):
                return
            record['verify'] = dict(tolerance_hz=tolerance, samples=deepcopy(samples),
                                    sample_interval_s=DEFAULTS['sample_interval_s'],
                                    acquisition_fence_s=record['acquisition_fence_s'])
            self._finish(record, 'settled', 'Confirmed saved target and fresh optical settlement')


def attach(protocol, laser_file, state_directory):
    protocol.owner.target_changes = TargetChanges(protocol, laser_file, state_directory)


def tick(owner):
    manager = getattr(owner, 'target_changes', None)
    if manager is not None:
        manager.tick()


def dispatch(protocol, action, request):
    manager = getattr(protocol.owner, 'target_changes', None)
    try:
        if manager is None:
            raise TargetError(409, 'not_configured', 'DLC target operation has no trusted laser file/store')
        if action == 'target_change_target':
            return manager.get_target()
        if action == 'target_change_start':
            return manager.start(request['body'])
        if action == 'target_change_read':
            return manager.view(manager.find(change_id=request.get('change_id'), request_id=request.get('request_id')))
        if action == 'target_change_cancel':
            return manager.cancel(request['change_id'])
        raise TargetError(400, 'invalid_request', 'Unknown DLC target action')
    except TargetError as error:
        return dict(error=error.payload)
