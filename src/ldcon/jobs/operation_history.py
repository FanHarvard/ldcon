"""Safe per-card operation index over the existing job evidence, never authority.

The complete chronological stream remains telemetry.sqlite3 / telemetry.events.jsonl.
This bounded manifest links it to the report without copying arbitrary prompts,
credentials, configuration documents or high-rate measurements into a new database.
"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import time

from ldcon.core.records import atomic_json, read_json

NAME = 'operation-record.json'
SCHEMA = 'ldcon.card-operation-record/1'
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,159}\Z')
_NUMBERS = frozenset(('frequency_hz', 'wavelength_nm', 'target_frequency_hz',
    'target_wavelength_nm', 'error_hz', 'tolerance_hz', 'dac_code', 'value',
    'current_a', 'current_estimate_a', 'temperature_c', 'duration_s', 'wait_s',
    'baseline_s', 'sample_interval_s', 'max_sample_age_s', 'measurement_max_age_s', 'max_sample_gap_s',
    'minimum_samples', 'timeout_s', 'expected_settle_s', 'evaluation_window_s',
    'check_interval_s', 'confirmation_s', 'max_step_hz', 'count', 'steps',
    'acquired_s', 'source_acquired_unix_s', 'acquired_unix_s'))
_OUTPUTS = frozenset(('current', 'temperature', 'piezo', 'current_a', 'temperature_c',
    'piezo_v', 'pzt_v', 'dac_code', 'kp_count_per_nm', 'ki_count_per_nm_s',
    'integral_limit_count', 'tec_gain', 'supervision_interval_s', 'tec_center_dac',
    'target_frequency_hz', 'current_per_temperature', 'gain_scale', 'maximum_step_hz'))
_BOOLEANS = frozenset(('locked', 'native_pi_active', 'pi_enabled', 'single_mode',
    'maintenance_required', 'target_ramp_active', 'control_ready_passed', 'applied'))
_STATES = frozenset(('running', 'succeeded', 'failed', 'cancelled', 'inconclusive',
    'unknown', 'uncertain', 'review_required', 'applied', 'not_applied', 'unchanged',
    'completed', 'completed_unsettled', 'measurement_incomplete', 'adopted', 'restored',
    'locked', 'tracking', 'mode_degraded', 'dither_degraded', 'acquiring', 'hold', 'idle', 'lock', 'scan'))
_EVIDENCE_FILES = ('summary.json', 'failure-detail.json', 'device-review.json',
    'measurement.json', 'target-response.json', 'operating-point.json',
    'lock-target-result.json', 'mode-recovery.json', 'recovery.json',
    'window-center.json', 'mode-map.json', 'axis-frequency.json', 'report.pdf')
_PROPOSALS = frozenset(('command_requested', 'change_measure_preflight',
    'target_response_recommendation', 'adaptive_command_requested'))


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def identifier(value):
    return value if isinstance(value, str) and _ID.fullmatch(value) else None


def clock_record(stamp):
    value = datetime.fromtimestamp(stamp, timezone.utc)
    local = value.astimezone()
    return dict(unix_s=stamp, utc=value.isoformat(), local=local.isoformat(),
                local_timezone=local.tzname())


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    allow_nan=False).encode()).hexdigest()


def _read_file(directory, name, *, limit, capture=False):
    """Hash and optionally capture the same bounded regular-file snapshot."""
    try:
        fd = os.open(Path(directory)/name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None, None
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            return None, None
        digest = hashlib.sha256()
        chunks = []
        count = 0
        while chunk := stream.read(65536):
            count += len(chunk)
            if count > limit:
                return dict(path=name, hash_status='grew_beyond_hash_budget'), None
            digest.update(chunk)
            if capture:
                chunks.append(chunk)
        after = os.fstat(stream.fileno())
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            return dict(path=name, hash_status='changed_during_read'), None
        return dict(path=name, sha256=digest.hexdigest(), bytes=after.st_size), b''.join(chunks) if capture else None


def _file_ref(directory, name, *, limit=64 * 1024 * 1024):
    """Only preselected local regular files, streamed without exposing contents."""
    return _read_file(directory, name, limit=limit)[0]


def _json_snapshot(directory, name):
    ref, raw = _read_file(directory, name, limit=1024 * 1024, capture=True)
    if raw is None:
        return ref, {}
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, ValueError):
        return ref, {}
    return ref, value if isinstance(value, dict) else {}


def safe_numbers(value, allowed):
    return {key: val for key, val in value.items() if key in allowed and finite(val)} if isinstance(value, dict) else {}


def safe_values(value):
    if not isinstance(value, dict):
        return {}
    result = safe_numbers(value, _NUMBERS)
    result.update({key: val for key, val in value.items() if key in _BOOLEANS and type(val) is bool})
    for key in ('state', 'status', 'physical_outcome'):
        if isinstance(value.get(key), str) and value[key] in _STATES:
            result[key] = value[key]
    for key in ('outputs', 'readback', 'checkpoint'):
        item = safe_numbers(value.get(key), _OUTPUTS)
        if item:
            result[key] = item
    return result


def conditions(invocation):
    p = invocation.get('parameters', {})
    result = safe_values(p)
    if isinstance(p, dict):
        if p.get('apply_mode') in ('atomic', 'coordinated', 'ordered'):
            result['apply_mode'] = p['apply_mode']
        selection = p.get('measurements')
        if isinstance(selection, dict):
            result['measurements'] = {k: v for k, v in selection.items()
                if k in ('wavelength', 'optical_quality', 'dither') and type(v) is bool}
        if isinstance(p.get('changes'), list):
            result['changes'] = [dict(parameter=row['parameter'], **safe_numbers(row, {'value', 'offset'}))
                for row in p['changes'][:128] if isinstance(row, dict)
                and isinstance(row.get('parameter'), str) and row['parameter'] in _OUTPUTS]
        if isinstance(p.get('offsets_hz'), list) and all(finite(v) for v in p['offsets_hz']):
            result['offsets_hz'] = p['offsets_hz'][:128]
    return result


def _source_manifest(package, prefix):
    # Search only ancestors of the executing code. A Python interpreter from a
    # different release must not assign its commit to PYTHONPATH override code.
    candidates = list(package.parents)
    prefix = Path(prefix).resolve()
    if package.is_relative_to(prefix):
        candidates += [p for p in prefix.parents if p not in candidates]
    for parent in candidates:
        ref, value = _json_snapshot(parent, 'SOURCE.json')
        if not ref or 'sha256' not in ref:
            continue
        commit = value.get('sha', value.get('commit', value.get('source_commit')))
        if isinstance(commit, str) and re.fullmatch('[a-fA-F0-9]{7,64}', commit):
            return dict(commit=commit, source_manifest=ref,
                        release_id=identifier(parent.name) if parent.parent.name == 'releases' else None)
    return None


def source_provenance(directory):
    """Observe the executing package, never ask a different checkout for its HEAD."""
    package = Path(__file__).resolve().parent.parent
    build_ref, build = _json_snapshot(directory, 'execution-provenance.json')
    commit = build.get('commit', build.get('source_commit'))
    if not isinstance(commit, str) or not re.fullmatch('[a-fA-F0-9]{7,64}', commit):
        commit = None
    commit_basis = 'frozen_execution_provenance' if commit else 'not_recorded'
    release = identifier(build.get('release_id'))
    release_basis = 'frozen_execution_provenance' if release else None
    source = _source_manifest(package, sys.prefix)
    if source is not None:
        commit, commit_basis = source['commit'], 'executing_source_manifest'
        if source['release_id']:
            release, release_basis = source['release_id'], 'executing_source_manifest'
    if release is None:
        for parent in package.parents:
            if parent.parent.name == 'releases' and identifier(parent.name):
                release, release_basis = parent.name, 'executing_package_location'
                break
    try:
        distribution = importlib.metadata.distribution('ldcon')
        version = distribution.version if Path(distribution.locate_file('ldcon')).resolve() == package else None
    except importlib.metadata.PackageNotFoundError:
        version = None
    sources = [ref for name in ('jobs/interface_worker.py', 'jobs/operation_history.py')
               if (ref := _file_ref(package, name))]
    return dict(release_id=release, release_basis=release_basis, commit=commit,
                commit_basis=commit_basis, source_manifest=source['source_manifest'] if source else None,
                package_version=identifier(version), executing_sources=sources,
                frozen_provenance=build_ref)


class OperationHistory:
    """Atomic report companion; the worker's existing journal fences execution."""
    def __init__(self, directory, frozen, *, wall_clock=time.time):
        self.directory, self.clock = Path(directory), wall_clock
        self.path = self.directory/NAME
        self.operation = frozen['card']['operation']
        self.parameters = frozen['invocation'].get('parameters', {})
        self.laser = frozen['laser']
        self.identity = dict(laser_id=identifier(frozen['laser']['id']),
            deck_id=identifier(frozen['deck']['id']), card_id=identifier(frozen['card']['id']),
            operation_id=identifier(self.operation), job_id=identifier(self.directory.name))
        self.record_id = _digest(self.identity)
        self.sequence = 0
        self.seen_lock = False
        with self._locked():
            old = read_json(self.path)
            if old is not None:
                if old.get('record_id') != self.record_id:
                    raise ValueError('Operation history identity differs from this job')
                self.seen_lock = bool(old.get('milestones'))
                return
            record = dict(schema_version=1, schema=SCHEMA, record_id=self.record_id,
                **self.identity, start=clock_record(self.clock()), end=None,
                lifecycle='running', outcome=None, conditions=conditions(frozen['invocation']),
                frozen_inputs=[ref for name in ('laser.yaml', 'interface-deck.yaml', 'invocation.json', 'job.json')
                    if (ref := _file_ref(self.directory, name))],
                provenance=source_provenance(self.directory), milestones=[],
                activity={category:dict(count=0, first=None, latest=None) for category in
                    ('proposals', 'card_reported_effects', 'owner_confirmed_applications', 'reported_adoptions')},
                unknown_effect_observed=False, recent_event_ids=[], last_event_elapsed_s=None, evidence=[],
                chronological_evidence=dict(path='telemetry.sqlite3', mirror='telemetry.events.jsonl',
                    complete_stream=True, manifest_is_bounded_index=True),
                classified_events=dict(proposals=sorted(_PROPOSALS),
                    card_reported_effects=['command_applied', 'calibration_move'],
                    owner_confirmed_applications=['change_measure_result with an applied receipt and readback'],
                    reported_adoptions=['quality_adjustment_accepted'],
                    unclassified_events_remain_in_original_stream=True),
                interpretation='Recorded observations, not a claim about current hardware state. '
                    'A successful procedure does not by itself prove any physical effect or a stopped PI.')
            atomic_json(self.path, record)

    @contextmanager
    def _locked(self):
        fd = os.open(self.directory/'.operation-record.lock',
                     os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o660)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError('Operation report lock is not a regular file')
            # Reporting never waits indefinitely on an unrelated process.
            # The execution journal already enforces one worker per job.
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            os.close(fd)

    def _lock_evidence(self, kind, payload, monotonic_s):
        """Only actual owner/sample events; status labels cannot create this milestone."""
        if self.seen_lock:
            return None
        row = payload
        if self.operation == 'servo.ecdl_steady_lock' and kind == 'ecdl_lock_cycle':
            qualified = (row.get('state') in ('locked', 'mode_degraded', 'dither_degraded')
                         and row.get('control_ready_passed') is True)
            target_ramping = row.get('target_ramp_active', False)
            basis = 'owner_verified_pi_cycle'
        elif self.operation == 'servo.native_current' and kind == 'trace':
            qualified = (row.get('locked') is True and row.get('native_pi_active') is True
                         and (row.get('native_config') or {}).get('pid_multiplier') == 1)
            target_ramping = row.get('target_ramp_active', False)
            basis = 'owner_verified_native_pi_and_current_optical_sample'
        elif self.operation == 'servo.dlcpro_frequency' and kind == 'trace':
            row = dict(payload.get('observation') or {}, outputs=payload.get('outputs', {}))
            f, target = row.get('frequency_hz'), payload.get('target_frequency_hz')
            row['error_hz'] = f-target if finite(f) and finite(target) else None
            row['target_frequency_hz'] = target
            qualified = payload.get('pi_enabled') is True and payload.get('mode') == 'lock' and not payload.get('fault')
            target_ramping, basis = False, 'owner_software_pi_and_current_optical_sample'
        else:
            return None
        frequency, error = row.get('frequency_hz'), row.get('error_hz')
        tolerance = self.parameters.get('tolerance_hz')
        stamp = row.get('source_acquired_unix_s', row.get('acquired_unix_s'))
        if stamp is None:
            stamp = row.get('acquired_s')
        if not (qualified and not target_ramping and finite(frequency) and frequency > 0
                and finite(error) and finite(tolerance) and tolerance > 0
                and abs(error) <= tolerance and finite(stamp)):
            return None
        explicit_stamp = row.get('source_acquired_unix_s', row.get('acquired_unix_s'))
        interface = {'servo.ecdl_steady_lock':'lock', 'servo.native_current':'native_current',
                     'servo.dlcpro_frequency':'dlc_lock'}[self.operation]
        device = self.laser.get('interfaces', {}).get(interface, {}).get('device')
        settings = self.laser.get('devices', {}).get(device, {}).get('control_settings', {})
        maximum_age = self.parameters.get('max_sample_age_s',
            settings.get('max_sample_age_s', settings.get('policy', {}).get('max_sample_age_s',
                settings.get('native_current', {}).get('max_sample_age_s'))))
        if not finite(maximum_age) or maximum_age <= 0:
            return None  # Unknown freshness criteria cannot establish an acquired lock.
        age = self.clock()-explicit_stamp if finite(explicit_stamp) else (
            monotonic_s-stamp if finite(monotonic_s) else None)
        if not finite(age) or not 0 <= age <= maximum_age:
            return None
        # ECDL/native workers emit this event only after their freshness gates.
        # DLC heartbeat repeats its last observation, so check its owner's clock too.
        if self.operation == 'servo.dlcpro_frequency':
            observed, acquired = payload.get('observed_s'), row.get('acquired_s')
            if not (finite(observed) and finite(acquired) and 0 <= observed-acquired <= maximum_age):
                return None
        return dict(kind='lock_acquired_from_operating_point', basis=basis,
                    evidence=safe_values(row), tolerance_hz=tolerance,
                    source_timestamp=stamp, observed_at=clock_record(self.clock()),
                    optical_quality_is_separate=True)

    def parameters_resolved(self, parameters):
        """Record normalized defaults without rewriting the exact frozen inputs."""
        self.parameters = deepcopy(parameters)
        with self._locked():
            record = read_json(self.path)
            if record.get('lifecycle') != 'running':
                return
            record['conditions'] = conditions({'parameters': parameters})
            record['conditions_basis'] = 'normalized_worker_parameters'
            atomic_json(self.path, record)

    def event(self, kind, payload, *, elapsed_s, run_id, monotonic_s=None):
        if not isinstance(payload, dict) or not finite(elapsed_s) or elapsed_s < 0:
            return
        self.sequence += 1
        milestone = self._lock_evidence(kind, payload, monotonic_s)
        category, value = None, safe_values(payload)
        if kind in _PROPOSALS:
            category = 'proposals'
        elif kind in ('command_applied', 'calibration_move'):
            category = 'card_reported_effects'
        elif kind == 'quality_adjustment_accepted':
            category = 'reported_adoptions'
        elif kind == 'change_measure_result':
            receipt = payload.get('application')
            if isinstance(receipt, dict) and receipt.get('status') == 'applied' and isinstance(receipt.get('readback'), dict):
                category = 'owner_confirmed_applications'
                value = safe_values(receipt)
        unknown = payload.get('physical_outcome') == 'unknown'
        if category is None and milestone is None and not unknown:
            return
        evidence = dict(path='telemetry.sqlite3', run_id=identifier(run_id), kind=identifier(kind),
                        elapsed_s=elapsed_s)
        event_id = _digest(dict(record_id=self.record_id, evidence=evidence, value=value))
        row = dict(id=event_id, observed_at=clock_record(self.clock()), values=value, evidence=evidence)
        with self._locked():
            record = read_json(self.path)
            if record.get('lifecycle') != 'running' or event_id in record['recent_event_ids']:
                return
            previous_elapsed = record.get('last_event_elapsed_s')
            if finite(previous_elapsed) and elapsed_s < previous_elapsed:
                return  # A late/replayed observation must not overwrite newer evidence.
            record['last_event_elapsed_s'] = elapsed_s
            record['recent_event_ids'] = (record['recent_event_ids']+[event_id])[-128:]
            if category:
                collection = record['activity'][category]
                collection['count'] += 1
                collection['first'] = collection['first'] or row
                collection['latest'] = row
            if milestone and not record['milestones']:
                record['milestones'].append(dict(id=_digest([self.record_id, milestone['kind']]),
                    source=evidence, **milestone))
                self.seen_lock = True
            record['unknown_effect_observed'] |= unknown
            atomic_json(self.path, record)

    def finish(self, summary):
        outcome = summary.get('operation_outcome') or {}
        state = outcome.get('execution_state')
        if state not in ('succeeded', 'failed', 'cancelled', 'inconclusive', 'uncertain'):
            state = 'unknown'
        failure = read_json(self.directory/'failure-detail.json', {})
        review = (self.directory/'device-review.json').exists()
        with self._locked():
            record = read_json(self.path)
            if record.get('lifecycle') != 'running':
                return
            unknown = record['unknown_effect_observed'] or review or state in ('unknown', 'uncertain')
            record.update(lifecycle='finished', end=clock_record(self.clock()),
                outcome=dict(execution_state='unknown' if unknown else state,
                    procedure_status=summary.get('status') if isinstance(summary.get('status'), str)
                        and summary['status'] in _STATES else 'unclassified',
                    physical_effects='unknown' if unknown else 'none' if outcome.get('effects') == 'none' else 'see_recorded_evidence',
                    physical_stop='unverified', review_required=unknown,
                    error_type=identifier(failure.get('error_type')) if isinstance(failure, dict) else None),
                evidence=[ref for name in _EVIDENCE_FILES if (ref := _file_ref(self.directory, name))])
            atomic_json(self.path, record)


def optional_history(directory, frozen):
    try:
        return OperationHistory(directory, frozen)
    except Exception as error:
        _diagnostic(directory, error)
        return None


def optional_call(history, method, *args, **kwargs):
    if history is not None:
        try:
            getattr(history, method)(*args, **kwargs)
        except Exception as error:
            _diagnostic(history.directory, error)


def _diagnostic(directory, error):
    # Never copy exception text (it may contain URLs or credentials), nor turn a
    # report-index failure into a failed physical operation.
    try:
        atomic_json(Path(directory)/'operation-record-error.json', dict(
            schema_version=1, error_type=type(error).__name__,
            message='Operation report index unavailable; inspect the original job evidence.'))
    except Exception:
        pass
