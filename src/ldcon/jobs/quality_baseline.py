"""Measured post-adjustment quality references; no control or owner operations.

Only a trusted worker calls ``accepted`` after a changed, owner-confirmed
adoption. Subsequent validated owner cycles supply the measurement. A pending
new adjustment hides the previous reference from current telemetry. Historical
evidence is retained as ``previous`` and never becomes fresh by being reread.
The separate reference export can display that evidence with its original dates
without changing the latest acquisition's availability or the stored record.
"""
import copy
import json
import math
import os
from pathlib import Path
import stat
import statistics
import tempfile
import time


METRICS = ('side_peak_height_max', 'side_peak_prominence_max',
           'main_fwhm_fraction', 'near_main_peak_prominence_max')
REQUIRED_METRICS = METRICS[:3]
MAX_BYTES = 128 * 1024
STATES = ('pending', 'available', 'unavailable')
# Dither pause (2026-10-27): the exact dither record of a reference measured while steady_lock.dither.paused was true.
DITHER_PAUSED = {'status': 'paused'}


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _identity(value):
    return isinstance(value, str) and bool(value) and len(value) <= 256 and not any(
        ord(character) < 32 for character in value)


def _context(row):
    """Small optical/owner identity; output changes are measured separately."""
    if not isinstance(row, dict):
        return None
    mode, target, owner = row.get('mode_id'), row.get('target_frequency_hz'), row.get('owner_context')
    if not _identity(mode) or not _number(target) or target <= 0:
        return None
    if owner is not None and (not isinstance(owner, dict) or not all(
            _identity(owner.get(key)) for key in ('system_id', 'generation', 'policy_revision'))):
        return None
    return dict(mode_id=mode, target_frequency_hz=target, owner_context=copy.deepcopy(owner))


def _path(path):
    path = Path(path)
    if (not path.is_absolute() or '..' in path.parts or
            path.parent.resolve() != path.parent or not path.parent.is_dir() or
            path.is_symlink()):
        raise ValueError('Baseline path must have an existing trusted non-symlink parent')
    return path


def _valid(record, *, previous=False):
    if not isinstance(record, dict) or record.get('schema_version') != 1:
        return False
    if any(not _identity(record.get(key)) for key in
           ('laser_id', 'family', 'action_id', 'controller_job_id')):
        return False
    if (record.get('state') not in STATES or type(record.get('available')) is not bool
            or record['available'] != (record['state'] == 'available')):
        return False
    accepted = record.get('accepted_unix_s')
    deadline = record.get('deadline_unix_s')
    if not _number(accepted) or accepted <= 0 or not _number(deadline) or deadline <= accepted:
        return False
    if type(record.get('require_dither')) is not bool:
        return False
    if type(record.get('minimum_samples')) is not int or not 5 <= record['minimum_samples'] <= 100:
        return False
    if any(not _number(record.get(key)) or record[key] <= 0 for key in
           ('max_source_age_s', 'max_sample_gap_s')):
        return False
    expected = record.get('expected_context')
    if expected is not None and _context(expected) != expected:
        return False
    if record['available']:
        start, end, recorded = (record.get(key) for key in
                                ('source_start_unix_s', 'source_end_unix_s', 'recorded_unix_s'))
        if (not all(_number(value) for value in (start, end, recorded)) or
                not accepted < start <= end <= recorded <= deadline):
            return False
        if (not _identity(record.get('mode_id')) or not _number(record.get('target_frequency_hz'))
                or record['target_frequency_hz'] <= 0 or
                type(record.get('sample_count')) is not int or
                record['sample_count'] < record['minimum_samples']):
            return False
        if expected is None or _context(record) != expected:
            return False
        metrics, outputs = record.get('metrics'), record.get('outputs')
        if (not isinstance(metrics, dict) or not set(REQUIRED_METRICS) <= set(metrics)
                or set(metrics) - set(METRICS) or
                any(not _number(value) or value < 0 for value in metrics.values()) or
                not isinstance(outputs, dict) or
                any(not _number(outputs.get(axis)) for axis in ('current', 'temperature', 'piezo'))):
            return False
        dither = record.get('dither')
        if dither is not None:
            if not isinstance(dither, dict):
                return False
            if dither.get('status') == 'paused':
                # Dither pause (2026-10-27): the reference was measured without the modulation; no fit is claimed.
                if dither != DITHER_PAUSED:
                    return False
            else:
                a, b = dither.get('source_start_unix_s'), dither.get('source_end_unix_s')
                if (dither.get('valid') is not True or dither.get('unit') != 'nm/V' or
                        not _number(dither.get('value')) or dither['value'] < 0 or
                        not _number(a) or not _number(b) or not accepted < a <= b <= recorded or
                        a < start or b > end):
                    return False
        if record['require_dither'] and dither is None:
            return False
    elif any(key in record for key in ('metrics', 'dither', 'recorded_unix_s',
                                       'source_start_unix_s', 'source_end_unix_s')):
        return False
    old = record.get('previous')
    if previous and old is not None:
        return False
    if old is not None and (not _valid(old, previous=True) or not old['available'] or
                            old['accepted_unix_s'] >= accepted or old['laser_id'] != record['laser_id']):
        return False
    return True


def safe_read(path, *, laser_id=None):
    """Return validated bounded regular JSON, or None; never refresh its dates."""
    try:
        path = _path(path)
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
                return None
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            return None
        record = json.loads(raw)
        if not _valid(record) or laser_id is not None and record['laser_id'] != laser_id:
            return None
        return record
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        return None


def export_metrics(record, now=None):
    """Historical-reference age is elapsed since recording, not live data age."""
    now = time.time() if now is None else now
    unavailable = {'quality_baseline_available': 0}
    if not _number(now) or not _valid(record) or not record['available']:
        return unavailable
    recorded = record['recorded_unix_s']
    if now < recorded:
        return unavailable
    result = dict(quality_baseline_available=1,
                  quality_baseline_recorded_unix_s=recorded,
                  quality_baseline_accepted_unix_s=record['accepted_unix_s'],
                  quality_baseline_source_start_unix_s=record['source_start_unix_s'],
                  quality_baseline_source_end_unix_s=record['source_end_unix_s'],
                  quality_baseline_age_s=now-recorded)
    result.update({'quality_baseline_'+key: value for key, value in record['metrics'].items()})
    dither = record.get('dither')
    if isinstance(dither, dict) and _number(dither.get('value')):
        result['quality_baseline_dither_response'] = dither['value']
    return result


def export_reference_metrics(record, now=None):
    """Display the last recorded reference independently of latest acquisition.

    Status codes: 0 absent/unreadable, 1 pending, 2 recorded, 3 unavailable.
    A stopped collector's expired pending window is unavailable, not still live.
    This is historical display metadata only; no context compatibility is claimed.
    """
    now = time.time() if now is None else now
    result = dict(quality_baseline_reference_available=0,
                  quality_baseline_reference_from_previous=0,
                  quality_baseline_acquisition_state_code=0)
    if not _number(now) or not _valid(record) or now < record['accepted_unix_s']:
        return result
    state = record['state']
    if state == 'pending' and now > record['deadline_unix_s']:
        state = 'unavailable'
    result['quality_baseline_acquisition_state_code'] = {
        'pending': 1, 'available': 2, 'unavailable': 3}[state]
    reference = record if record['available'] else record.get('previous')
    values = export_metrics(reference, now)
    if not values['quality_baseline_available']:
        return result
    result.update({key.replace('quality_baseline_', 'quality_baseline_reference_', 1): value
                   for key, value in values.items()})
    result['quality_baseline_reference_from_previous'] = int(not record['available'])
    return result


class QualityBaseline:
    """Collect a new reference after a trusted, successful changed adoption.

    The callback must not be invoked for a no-op, failed or restored trial.
    While other work owns the physical context the caller must withhold rows or
    mark ``quality_baseline_blocked``. All I/O failures are diagnostic results,
    never exceptions that can alter the surrounding control lifecycle.
    """
    def __init__(self, path, *, laser_id, controller_job_id, require_dither=True,
                 deadline_s=120., sample_count=5, max_source_age_s=3.,
                 max_sample_gap_s=3., clock=time.time):
        if not _identity(laser_id) or not _identity(controller_job_id):
            raise ValueError('Baseline laser and controller identities are required')
        if (type(require_dither) is not bool or type(sample_count) is not int or
                not 5 <= sample_count <= 100 or any(not _number(value) or value <= 0
                for value in (deadline_s, max_source_age_s, max_sample_gap_s))):
            raise ValueError('Invalid baseline observation policy')
        self.path = Path(path)
        self.laser_id, self.controller_job_id = laser_id, controller_job_id
        self.require_dither, self.deadline_s, self.sample_count = require_dither, deadline_s, sample_count
        self.max_source_age_s, self.max_sample_gap_s, self.clock = max_source_age_s, max_sample_gap_s, clock
        self.record = safe_read(path, laser_id=laser_id)
        self.rows = []
        self.last_source = self.last_native = None
        self.last_error = None
        self.last_context = None
        self.last_context_source = None

    def _save(self):
        temporary = None
        try:
            path = _path(self.path)
            disk = safe_read(path, laser_id=self.laser_id)
            if disk is not None and disk['accepted_unix_s'] > self.record['accepted_unix_s']:
                raise ValueError('A newer accepted adjustment owns the baseline publication')
            raw = json.dumps(self.record, allow_nan=False, separators=(',', ':')).encode()
            if not _valid(self.record) or len(raw) > MAX_BYTES:
                raise ValueError('Invalid or oversized quality baseline record')
            descriptor, temporary = tempfile.mkstemp(prefix='.'+path.name+'.', dir=path.parent)
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            # A symlink installed while preparing the temporary file is refused.
            _path(path)
            os.replace(temporary, path)
            self.last_error = None
            return {'persisted': True}
        except (OSError, ValueError, TypeError, UnicodeError) as error:
            self.last_error = type(error).__name__+': '+str(error)[:240]
            return {'persisted': False, 'reason': self.last_error}
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def accepted(self, family, action_id, accepted_time, controller_job_id=None, *, expected_context=None):
        job = controller_job_id or self.controller_job_id
        now = self.clock()
        if (not all(_identity(value) for value in (family, action_id, job)) or
                not _number(accepted_time) or accepted_time <= 0 or
                not _number(now) or accepted_time > now):
            return {'state': 'rejected', 'persisted': False, 'reason': 'Invalid accepted-adjustment identity or time'}
        old = self.record
        # Include the on-disk record so a delayed callback cannot overwrite a
        # newer controller's adjustment. A sole lease holder is still required.
        disk = safe_read(self.path, laser_id=self.laser_id)
        if disk is not None and (old is None or disk['accepted_unix_s'] > old['accepted_unix_s']):
            old = self.record = disk
        if old is not None:
            if (old['family'], old['action_id'], old['controller_job_id']) == (family, action_id, job):
                return {'state': old['state'], 'duplicate': True,
                        **(self._save() if self.last_error else {'persisted': True})}
            if accepted_time <= old['accepted_unix_s']:
                return {'state': 'rejected', 'persisted': False, 'reason': 'Older adjustment cannot replace the current baseline'}
        previous = copy.deepcopy(old if old and old['available'] else (old or {}).get('previous'))
        if previous is not None:
            previous.pop('previous', None)
        if expected_context is not None:
            expected_context = _context(expected_context)
            if (expected_context is None or expected_context['owner_context'] is not None and
                    expected_context['owner_context']['system_id'] != self.laser_id):
                return {'state': 'rejected', 'persisted': False, 'reason': 'Invalid expected optical context'}
        elif (self.last_context_source is not None and
              0 <= accepted_time-self.last_context_source <= self.max_source_age_s):
            expected_context = copy.deepcopy(self.last_context)
        self.record = dict(schema_version=1, laser_id=self.laser_id, family=family,
            action_id=action_id, controller_job_id=job, accepted_unix_s=accepted_time,
            deadline_unix_s=accepted_time+self.deadline_s, state='pending', available=False,
            require_dither=self.require_dither, minimum_samples=self.sample_count,
            max_source_age_s=self.max_source_age_s, max_sample_gap_s=self.max_sample_gap_s,
            expected_context=expected_context,
            unavailable_reason='Awaiting post-adjustment measurement', previous=previous)
        self.rows = []
        self.last_source = self.last_native = None
        return {'state': 'pending', **self._save()}

    def _waiting(self, reason, *, reset=False):
        if reset:
            self.rows = []
        return {'state': 'pending', 'samples': len(self.rows), 'reason': reason,
                'persisted': self.last_error is None}

    def observe(self, row, now=None):
        now = self.clock() if now is None else now
        context = _context(row)
        source = row.get('source_acquired_unix_s') if isinstance(row, dict) else None
        if (context is not None and _number(now) and _number(source) and
                0 <= now-source <= self.max_source_age_s and
                row.get('state') == 'locked' and row.get('control_ready_passed') is True and
                row.get('native_input_stale') is False and row.get('target_ramp_active') is False and
                type(row.get('native_input_seq')) is int and row['native_input_seq'] >= 0 and
                (context['owner_context'] is None or context['owner_context']['system_id'] == self.laser_id) and
                (self.last_context_source is None or source > self.last_context_source)):
            self.last_context, self.last_context_source = context, source
        record = self.record
        if record is None or record['state'] != 'pending':
            if record is not None and self.last_error:
                return {'state': record['state'], 'changed': False, **self._save()}
            return {'state': record['state'] if record else 'absent', 'changed': False}
        if record['controller_job_id'] != self.controller_job_id:
            return self._waiting('Pending adjustment belongs to another controller', reset=True)
        if not _number(now) or now < record['accepted_unix_s']:
            return self._waiting('Invalid observation time', reset=True)
        if now > record['deadline_unix_s']:
            record.update(state='unavailable', unavailable_reason='Post-adjustment observation deadline exceeded')
            self.rows = []
            return {'state': 'unavailable', **self._save()}
        if not isinstance(row, dict):
            return self._waiting('Validated owner cycle required', reset=True)
        if record.get('expected_context') is None:
            return self._waiting('No trusted pre-acceptance optical context was recorded', reset=True)
        if (row.get('state') != 'locked' or row.get('control_ready_passed') is not True or
                row.get('native_input_stale') is not False or row.get('target_ramp_active') is not False or
                row.get('fringe_period_qualified') is False or
                row.get('quality_baseline_blocked') or row.get('parameter_adjustment') or row.get('pid_tuning')):
            return self._waiting('Fresh idle owner control context required', reset=True)
        source = row.get('source_acquired_unix_s')
        native = row.get('native_input_seq')
        if (not _number(source) or not record['accepted_unix_s'] < source <= now or
                now-source > record['max_source_age_s'] or type(native) is not int or native < 0):
            return self._waiting('Fresh post-acceptance source acquisition required', reset=True)
        if self.last_source is not None and (source <= self.last_source or native <= self.last_native):
            return self._waiting('Distinct advancing optical and native acquisitions required')
        self.last_source, self.last_native = source, native
        if context is not None and context != record['expected_context']:
            record.update(state='unavailable', unavailable_reason='Owner, optical branch or target changed after adoption')
            self.rows = []
            return {'state': 'unavailable', **self._save()}
        mode, target = row.get('mode_id'), row.get('target_frequency_hz')
        metrics, outputs = row.get('metrics'), row.get('outputs')
        if (not _identity(mode) or not _number(target) or target <= 0 or
                not isinstance(metrics, dict) or any(not _number(metrics.get(key)) or metrics[key] < 0
                    for key in REQUIRED_METRICS) or not isinstance(outputs, dict) or
                any(not _number(outputs.get(axis)) for axis in ('current', 'temperature', 'piezo'))):
            return self._waiting('Finite optical metrics, outputs, branch and target required', reset=True)
        context = row.get('owner_context')
        if context is not None and (not isinstance(context, dict) or not all(
                _identity(context.get(key)) for key in ('system_id', 'generation', 'policy_revision'))):
            return self._waiting('Invalid owner context', reset=True)
        if context is not None and context['system_id'] != self.laser_id:
            return self._waiting('Owner belongs to a different laser', reset=True)
        item = dict(source=source, native=native, mode_id=mode, target_frequency_hz=target,
                    owner_context=copy.deepcopy(context),
                    metrics={key: metrics[key] for key in METRICS
                             if _number(metrics.get(key)) and metrics[key] >= 0},
                    outputs={axis: outputs[axis] for axis in ('current', 'temperature', 'piezo')})
        if self.rows:
            last = self.rows[-1]
            if (any(last[key] != item[key] for key in ('mode_id', 'target_frequency_hz', 'owner_context')) or
                    source-last['source'] > record['max_sample_gap_s']):
                self.rows = []
        self.rows.append(item)
        self.rows = self.rows[-record['minimum_samples']:]
        if len(self.rows) < record['minimum_samples']:
            return self._waiting('Collecting post-adjustment optical window')
        dither = None
        start, end = row.get('dither_source_start_unix_s'), row.get('dither_source_unix_s')
        value, unit = row.get('dither_response'), row.get('dither_response_unit')
        if row.get('dither_analysis_status') == 'paused' or row.get('dither_paused') is True:
            # Dither pause (2026-10-27): no modulation, so no fit is required; the record says so explicitly.
            dither = dict(DITHER_PAUSED)
        elif (row.get('dither_response_valid') is True and _number(value) and value >= 0 and
                isinstance(unit, str) and ''.join(unit.split()) == 'nm/V' and
                _number(start) and _number(end) and record['accepted_unix_s'] < start <= end <= now):
            # The owner validates fit freshness and quality. Preserve its
            # original source window rather than stamping it as newly sampled.
            dither = dict(value=value, unit='nm/V', valid=True,
                          source_start_unix_s=start, source_end_unix_s=end)
        if record['require_dither'] and dither is None:
            return self._waiting('Wholly post-acceptance valid nm/V dither window required')
        fit = dither if dither is not None and 'value' in dither else None
        measured = {key: statistics.median(item['metrics'][key] for item in self.rows)
                    for key in METRICS if all(key in item['metrics'] for item in self.rows)}
        first, last = self.rows[0]['source'], self.rows[-1]['source']
        record.update(state='available', available=True, recorded_unix_s=now,
            source_start_unix_s=min(first, fit['source_start_unix_s']) if fit else first,
            source_end_unix_s=max(last, fit['source_end_unix_s']) if fit else last,
            optical_source_start_unix_s=first, optical_source_end_unix_s=last,
            sample_count=len(self.rows), aggregation='median of distinct post-adjustment optical acquisitions',
            metrics=measured, dither=dither, mode_id=mode, target_frequency_hz=target,
            owner_context=copy.deepcopy(context), outputs=copy.deepcopy(item['outputs']),
            output_ranges={axis: [min(item['outputs'][axis] for item in self.rows),
                                  max(item['outputs'][axis] for item in self.rows)]
                           for axis in ('current', 'temperature', 'piezo')})
        record.pop('unavailable_reason', None)
        return {'state': 'available', **self._save()}
