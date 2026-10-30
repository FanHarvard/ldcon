"""TEC/current feedforward branch alignment with the shared recovery v2 route.

The alignment target is explicit. A fresh stable window at each operating point
supplies recovery evidence; archived artifact frequencies never select a branch.
Intentional TEC/PZT changes are modeled for continuity. If they cause a hop,
alignment restores its own most recent measured context before executing the
same directed-current recovery used by standalone and stationary cards. Quality
is reported independently. Owner safety, freshness and uncertain-write checks
apply throughout; no live branch map is consulted.
"""
import copy
import math

from ldcon.core.contracts import DeviceError
from ldcon.jobs.ecdl_mode_map import BoundedExperiment, compact_row, finite
from ldcon.jobs.recovery_procedure import checked_recovery, execute, ReturnNotConfirmed, policy_from_artifact
from ldcon.jobs.recovery_reference import infer, observation
from ldcon.jobs.ecdl_recovery_plan import validate_reference


C_NM = 299792458e9
PZT_TRIM_DEFAULTS = dict(enabled=True, counts_per_hz=None, max_counts=15000.)
MAX_TEC_SETTLE_S = 3600.
STABLE_SAMPLES = 3

# tec_ff: signed TEC frequency slope (Hz per TEC volt); null = the temperature axis policy frequency_hz_per_unit.
# ldc_ff: explicit calibrated current per TEC volt; not a recovery-artifact default.
# hop_threshold_hz: null = the v2 artifact's configured hop threshold.
# pzt_trim.counts_per_hz: null = inverse of the current piezo-axis calibration.
DEFAULTS = dict(recovery_artifact=None, recovery_ref=None,
                target_frequency_hz=None, target_wavelength_nm=None,
                tolerance_hz=100e6, stable_s=10., hop_threshold_hz=None,
                tec_ff=None, ldc_ff=None, approach_gain=.5, tec_settle_s=45., max_tec_excursion_v=.3,
                pzt_trim=dict(PZT_TRIM_DEFAULTS), max_consecutive_recovery_failures=3,
                sample_interval_s=.5, max_sample_age_s=1., measurement_timeout_s=120., timeout_s=3600.)


def pzt_trim_parameters(value):
    if not isinstance(value, dict) or set(value) - PZT_TRIM_DEFAULTS.keys():
        raise ValueError('pzt_trim accepts ' + ', '.join(sorted(PZT_TRIM_DEFAULTS)))
    t = copy.deepcopy({**PZT_TRIM_DEFAULTS, **value})
    if type(t['enabled']) is not bool:
        raise ValueError('pzt_trim.enabled must be boolean')
    if t['counts_per_hz'] is not None and (not finite(t['counts_per_hz']) or not t['counts_per_hz']):
        raise ValueError('pzt_trim.counts_per_hz must be null (from the artifact) or a finite nonzero signed value')
    if not finite(t['max_counts']) or t['max_counts'] <= 0:
        raise ValueError('Positive pzt_trim.max_counts required')
    return t


def parameters(card, overrides):
    if not isinstance(card.get('parameters'), dict) or not isinstance(overrides, dict):
        raise ValueError('Operating-point parameters must be mappings')
    p = copy.deepcopy({**DEFAULTS, **card['parameters'], **overrides})
    if set(p) - DEFAULTS.keys():
        raise ValueError('Unknown operating-point parameter: ' + ', '.join(sorted(set(p) - DEFAULTS.keys())))
    if p['recovery_artifact'] is not None and not isinstance(p['recovery_artifact'], dict):
        raise ValueError('recovery_artifact must be the prepare.ecdl_recovery artifact mapping')
    validate_reference(p['recovery_ref'])
    for name in ('target_frequency_hz', 'target_wavelength_nm', 'hop_threshold_hz'):
        if p[name] is not None and (not finite(p[name]) or p[name] <= 0):
            raise ValueError('Positive ' + name + ' required')
    if p['target_wavelength_nm'] is not None:
        converted = C_NM / p['target_wavelength_nm']
        if p['target_frequency_hz'] is not None and not math.isclose(p['target_frequency_hz'], converted, rel_tol=1e-12, abs_tol=1.):
            raise ValueError('Frequency and wavelength targets are inconsistent')
        p['target_frequency_hz'] = converted
    for name in ('tolerance_hz', 'stable_s', 'max_tec_excursion_v', 'sample_interval_s', 'max_sample_age_s',
                 'measurement_timeout_s', 'timeout_s'):
        if not finite(p[name]) or p[name] <= 0:
            raise ValueError('Positive ' + name + ' required')
    for name in ('tec_ff', 'ldc_ff'):
        if p[name] is not None and not finite(p[name]):
            raise ValueError(name + ' must be null (default from the axis policy) or finite')
    if p['tec_ff'] is not None and not p['tec_ff']:
        raise ValueError('tec_ff is a signed nonzero slope (Hz per TEC volt)')
    if not finite(p['approach_gain']) or not 0 < p['approach_gain'] <= 1:
        raise ValueError('approach_gain must be within (0, 1]')
    if not finite(p['tec_settle_s']) or not 0 <= p['tec_settle_s'] <= MAX_TEC_SETTLE_S:
        raise ValueError('tec_settle_s must be 0..%g s' % MAX_TEC_SETTLE_S)
    p['pzt_trim'] = pzt_trim_parameters(p['pzt_trim'])
    if type(p['max_consecutive_recovery_failures']) is not int or not 1 <= p['max_consecutive_recovery_failures'] <= 100:
        raise ValueError('max_consecutive_recovery_failures must be an integer 1..100')
    if p['timeout_s'] > 86400 or p['stable_s'] >= p['timeout_s'] or p['tec_settle_s'] >= p['timeout_s']:
        raise ValueError('Invalid alignment deadline')
    if (p['sample_interval_s'] < .05
            or not p['sample_interval_s'] < p['measurement_timeout_s'] <= p['timeout_s']
            or p['max_sample_age_s'] > p['measurement_timeout_s']):
        raise ValueError('Invalid observation interval, freshness or measurement deadline')
    return p


def target(p):
    return p['target_frequency_hz'] if p['target_frequency_hz'] is not None else (
        C_NM / p['target_wavelength_nm'] if p['target_wavelength_nm'] else None)


def coefficients(p, artifact, policies):
    """The signed slopes of the approach, with their sources (parameter, axis policy or artifact)."""
    tp, cp = policies['temperature'], policies['current']
    tec_ff = p['tec_ff'] if p['tec_ff'] is not None else tp.frequency_hz_per_unit
    if not finite(tec_ff) or not tec_ff:
        raise ValueError('tec_ff is required (temperature.frequency_hz_per_unit is null): a calibrated signed TEC slope')
    ldc_ff = p['ldc_ff']
    if not finite(ldc_ff):
        raise ValueError('ldc_ff is required; keep alignment calibration separate from recovery evidence')
    hop = p['hop_threshold_hz'] if p['hop_threshold_hz'] is not None else artifact['parameters']['hop_threshold_hz']
    if not finite(hop) or hop <= 0:
        raise ValueError('hop_threshold_hz is required (the recovery artifact carries none)')
    piezo_hz_per_count = policies['piezo'].frequency_hz_per_unit
    counts_per_hz = p['pzt_trim']['counts_per_hz']
    if counts_per_hz is None and finite(piezo_hz_per_count) and piezo_hz_per_count:
        counts_per_hz = 1. / piezo_hz_per_count
    if p['pzt_trim']['enabled'] and counts_per_hz is None:
        raise ValueError('pzt_trim.counts_per_hz is required; the piezo axis has no frequency calibration')
    return dict(tec_ff_hz_per_v=tec_ff, tec_ff_source='parameter' if p['tec_ff'] is not None else 'temperature.frequency_hz_per_unit',
                ldc_ff_per_v=ldc_ff, ldc_ff_source='parameter',
                hop_threshold_hz=hop, hop_threshold_source='parameter' if p['hop_threshold_hz'] is not None else 'recovery parameters',
                counts_per_hz=counts_per_hz,
                counts_per_hz_source=('parameter' if p['pzt_trim']['counts_per_hz'] is not None
                                      else 'piezo.frequency_hz_per_unit' if counts_per_hz is not None else None),
                piezo_hz_per_count=(1. / counts_per_hz if counts_per_hz else piezo_hz_per_count if finite(piezo_hz_per_count) else None),
                current_hz_per_unit=cp.frequency_hz_per_unit if finite(cp.frequency_hz_per_unit) else None)


def run_operating_point(ports, p, **arguments):
    normalized = parameters(dict(parameters=p), {})
    p.clear()
    p.update(normalized)
    if p['recovery_artifact'] is None:
        raise ValueError('A v2 prepare.ecdl_recovery artifact of this laser is required '
                         '(recovery_ref resolved into recovery_artifact)' + (
                             '' if p['recovery_ref'] is None else '; the reference was not resolved'))
    if target(p) is None:
        raise ValueError('Target frequency/wavelength is required (card parameter or laser.target)')
    artifact = checked_recovery(p['recovery_artifact'])
    recovery_p = policy_from_artifact(artifact)
    experiment = BoundedExperiment(ports, p, **arguments)
    policies = experiment.policies
    tp, cp, pp = policies['temperature'], policies['current'], policies['piezo']
    k = coefficients(p, artifact, policies)
    slopes = dict(temperature=k['tec_ff_hz_per_v'], current=k['current_hz_per_unit'] or 0., piezo=k['piezo_hz_per_count'] or 0.)
    runtime = arguments.get('runtime')
    revision = runtime.revision if runtime else None
    counts = dict(observations=0, iterations=0, tec_commands=0, current_commands=0, piezo_commands=0, hops=0,
                  recoveries=0, recovery_failures=0, recovery_s=0., settle_s=0.)
    experiment.observation_observer = lambda row: counts.__setitem__('observations', counts['observations'] + 1)
    start = dict(experiment.expected)
    tec_low = max(tp.low, start['temperature'] - p['max_tec_excursion_v'])
    tec_high = min(tp.high, start['temperature'] + p['max_tec_excursion_v'])
    trace = []
    consecutive = 0
    previous = None
    stable_history = []
    reference = None
    epoch = str(target(p))

    def annotate(row):
        row['optical_quality'] = dict(single_mode=row.get('single_mode'), metrics=copy.deepcopy(row.get('metrics', {})),
                                     metric_limits_met=None, maintenance_required=False)
        row['error_hz'] = target(p) - row['frequency_hz']
        trace.append(row)
        experiment.emit('ecdl_operating_point_sample', compact_row(row))

    def _recover(row):
        """The tested recovery (rule F: the same ``execute`` as the lock and the recover card) after an observed hop.
        Consecutive failures are counted; cancellation, deadline, uncertain outputs and policy changes are never retried."""
        nonlocal previous, consecutive, reference, stable_history
        if reference is None or reference['operation_epoch'] != str(target(p)):
            raise DeviceError('No fresh stable alignment reference in the current target epoch; outputs held')
        if experiment.clock()-reference['end_s'] > recovery_p['reference']['lookback_s']:
            raise DeviceError('Stable alignment reference expired; outputs held')
        counts['hops'] += 1
        experiment.emit('ecdl_alignment_hop', dict(stage=row['stage'], continuity=row['continuity'], frequency_hz=row['frequency_hz'],
            outputs=dict(experiment.expected), hops=counts['hops'], consecutive_failures=consecutive))
        while True:
            began = experiment.clock()
            try:
                experiment.check()
                # Restore the alignment's own last measured context, never an archived map's context.
                for axis in ('temperature', 'piezo'):
                    experiment.move(axis, reference['outputs'][axis], 'restore recent alignment context')
                experiment.wait(p['tec_settle_s'])
                response = execute(experiment, recovery_p, reference)
                restored = observation(response['final'], experiment.clock())
                restored['recovery'] = dict(reference=response['reference'], selection=response['selection'],
                    algorithm=response['algorithm'], measured_frequency_hz=restored['frequency_hz'],
                    predicted_frequency_hz=reference['frequency_hz'])
            except ReturnNotConfirmed as error:
                experiment.require_frequency = True
                counts['recovery_failures'] += 1
                counts['recovery_s'] += experiment.clock() - began
                consecutive += 1
                experiment.emit('ecdl_alignment_recovery_failed', dict(attempt=consecutive,
                    limit=p['max_consecutive_recovery_failures'], reason=str(error), outputs=dict(experiment.expected),
                    elapsed_s=experiment.clock() - began))
                experiment.check()
                if consecutive >= p['max_consecutive_recovery_failures']:
                    raise DeviceError('%d consecutive recovery failures (last: %s); outputs held'
                                      % (consecutive, error)) from error
                continue
            counts['recoveries'] += 1
            counts['recovery_s'] += experiment.clock() - began
            consecutive = 0
            restored['continuity'] = dict(recovered=True, predicted_frequency_hz=restored['recovery']['predicted_frequency_hz'],
                                          measured_frequency_hz=restored['frequency_hz'])
            annotate(restored)
            experiment.emit('ecdl_alignment_recovered', dict(recovery=restored['recovery'], frequency_hz=restored['frequency_hz'],
                error_hz=restored['error_hz'], outputs=dict(experiment.expected), recoveries=counts['recoveries'],
                elapsed_s=experiment.clock() - began))
            previous = restored
            stable_history = []
            reference = None
            return restored

    def recover(row):
        # Hot target changes supersede an in-progress recovery before another write.
        original_cancel = experiment.cancel
        recovery_target = target(p)
        experiment.cancel = lambda: original_cancel() or target(p) != recovery_target
        try:
            return _recover(row)
        finally:
            experiment.cancel = original_cancel

    def observe(stage):
        """One fresh observation with the branch-continuity check against the previous accepted row."""
        nonlocal previous
        row = observation(experiment.read(stage), experiment.clock())
        if previous is None:previous = dict(row, stage='fresh alignment observation')
        predicted = sum(slopes[name] * (row['outputs'][name] - previous['outputs'][name]) for name in policies)
        measured = row['frequency_hz'] - previous['frequency_hz']
        row['continuity'] = dict(reference_stage=previous['stage'], predicted_change_hz=predicted, measured_change_hz=measured,
                                 residual_hz=abs(measured - predicted), hop_threshold_hz=k['hop_threshold_hz'],
                                 hop=abs(measured - predicted) > k['hop_threshold_hz'])
        annotate(row)
        if row['continuity']['hop']:
            return recover(row)
        previous = row
        stable_history.append({**row, 'operation_epoch':str(target(p))})
        return row

    def stable_checkpoint(row):
        """Measure, rather than assume, the return reference before an intentional step."""
        nonlocal reference, stable_history, epoch
        if epoch != str(target(p)):
            epoch = str(target(p))
            reference = None
            stable_history = []
        while True:
            try:
                reference = infer(stable_history, experiment.clock(), epoch, recovery_p['reference'])
                return row
            except ValueError:
                experiment.wait(p['sample_interval_s'])
                row = observe('alignment reference stability')

    def approach(row):
        """One iteration: bounded TEC move, LDC feed-forward in the same iteration, thermal settling."""
        row = stable_checkpoint(row)
        stable_history.clear()
        error = row['error_hz']
        before = dict(experiment.expected)
        wanted = p['approach_gain'] * error / k['tec_ff_hz_per_v']
        destination = max(tec_low, min(tec_high, before['temperature'] + wanted))
        if tp.resolution:
            destination = round(destination / tp.resolution) * tp.resolution
        if abs(destination - before['temperature']) <= max(tp.resolution / 2, 1e-10):
            raise DeviceError('TEC approach reached the excursion budget (%.4g V from the start) or the axis resolution with '
                              'error %.4g Hz; outputs held' % (p['max_tec_excursion_v'], error))
        n = len(experiment.commands)
        experiment.move('temperature', destination, 'TEC approach', settle_s=0.)
        delivered = experiment.expected['temperature'] - before['temperature']
        tec_commands = len(experiment.commands) - n
        current_destination = before['current'] + k['ldc_ff_per_v'] * delivered
        limited = not cp.low <= current_destination <= cp.high
        current_destination = max(cp.low, min(cp.high, current_destination))
        n = len(experiment.commands)
        if abs(current_destination - before['current']) > max(cp.resolution / 2, 1e-10):
            experiment.move('current', current_destination, 'LDC feed-forward', settle_s=0.)
        current_commands = len(experiment.commands) - n
        counts['iterations'] += 1
        counts['tec_commands'] += tec_commands
        counts['current_commands'] += current_commands
        step = dict(iteration=counts['iterations'], error_hz=error, requested_delta_v=wanted, delivered_delta_v=delivered,
                    tec_before=before['temperature'], tec_after=experiment.expected['temperature'],
                    current_before=before['current'], current_after=experiment.expected['current'],
                    current_delta=experiment.expected['current'] - before['current'], current_limited=limited,
                    tec_commands=tec_commands, current_commands=current_commands,
                    predicted_change_hz=(slopes['temperature'] * delivered
                                         + slopes['current'] * (experiment.expected['current'] - before['current'])),
                    settle_s=p['tec_settle_s'], optical_quality=row['optical_quality'])
        experiment.emit('ecdl_alignment_step', step)
        experiment.wait(p['tec_settle_s'])
        counts['settle_s'] += p['tec_settle_s']

    def trim(row):
        """One bounded PZT trim of the residual at the end (so the arm capture window is met with margin)."""
        row = stable_checkpoint(row)
        stable_history.clear()
        t = p['pzt_trim']
        before = experiment.expected['piezo']
        wanted = row['error_hz'] * k['counts_per_hz']
        delta = max(-t['max_counts'], min(t['max_counts'], wanted))
        destination = max(pp.low, min(pp.high, before + delta))
        if pp.resolution:
            destination = round(destination / pp.resolution) * pp.resolution
        record = dict(enabled=True, performed=False, residual_hz=row['error_hz'], requested_counts=wanted,
                      max_counts=t['max_counts'], counts_per_hz=k['counts_per_hz'], before=before)
        if abs(destination - before) < max(pp.resolution, 1e-10):
            record['reason'] = 'residual below one PZT count'
            experiment.emit('ecdl_alignment_pzt_trim', record)
            return record, row
        n = len(experiment.commands)
        experiment.move('piezo', destination, 'PZT trim')
        counts['piezo_commands'] += len(experiment.commands) - n
        after = observe('after PZT trim')
        record.update(performed=True, counts=experiment.expected['piezo'] - before, after=experiment.expected['piezo'],
                      predicted_change_hz=slopes['piezo'] * (experiment.expected['piezo'] - before),
                      frequency_hz=after['frequency_hz'], error_after_hz=after['error_hz'], continuity=after['continuity'])
        experiment.emit('ecdl_alignment_pzt_trim', record)
        return record, after

    def headroom():
        value = experiment.expected['piezo']
        hz = k['piezo_hz_per_count']
        return dict(counts_low=value - pp.low, counts_high=pp.high - value,
                    hz_low=(value - pp.low) * abs(hz) if hz else None, hz_high=(pp.high - value) * abs(hz) if hz else None)

    def timing():
        return dict(tec_distance_v=abs(experiment.expected['temperature'] - start['temperature']),
                    tec_start=start['temperature'], tec_end=experiment.expected['temperature'],
                    iterations=counts['iterations'], tec_commands=counts['tec_commands'],
                    current_commands=counts['current_commands'], piezo_commands=counts['piezo_commands'],
                    command_count=len(experiment.commands), observation_count=counts['observations'],
                    hops=counts['hops'], recoveries=counts['recoveries'], recovery_failures=counts['recovery_failures'],
                    recovery_s=counts['recovery_s'], settle_s=counts['settle_s'],
                    total_s=experiment.clock() - experiment.started)

    row = observe('alignment start')  # The present branch, not archived artifact coordinates.
    required = row['error_hz'] / k['tec_ff_hz_per_v']
    if not tec_low <= start['temperature'] + required <= tec_high:
        raise DeviceError('Target needs a TEC move of %.4g V, beyond max_tec_excursion_v %.4g V from the start (%.4g V) '
                          'or the owner interval; no approach write' % (required, p['max_tec_excursion_v'], start['temperature']))
    experiment.emit('ecdl_alignment_start', dict(target_frequency_hz=target(p), frequency_hz=row['frequency_hz'],
        error_hz=row['error_hz'], start_outputs=start, tec_range=[tec_low, tec_high], required_tec_delta_v=required,
        coefficients=k, recovery_sha256=artifact['artifact_sha256'], recovery_algorithm=artifact['algorithm']))
    stable = None
    stable_count = 0
    previous_stamp = None
    trimmed = None
    while True:
        experiment.between_points()
        if runtime and runtime.revision != revision:
            stable = None
            stable_count = 0
            previous_stamp = None
            revision = runtime.revision
            row['error_hz'] = target(p) - row['frequency_hz']
            reference = None
            stable_history.clear()
            previous = None
            epoch = str(target(p))
        if abs(row['error_hz']) <= p['tolerance_hz']:
            if previous_stamp is not None and row['acquired_s'] - previous_stamp > max(3 * p['sample_interval_s'], 1.):
                stable = None
                stable_count = 0
                experiment.emit('ecdl_acquisition_window_reset', dict(
                    reason='Observation gap; reacquire continuous stable window', gap_s=row['acquired_s'] - previous_stamp))
            previous_stamp = row['acquired_s']
            if stable is None:
                stable = row['acquired_s']
            stable_count += 1
            if stable_count >= STABLE_SAMPLES and row['acquired_s'] - stable >= p['stable_s']:
                if p['pzt_trim']['enabled'] and trimmed is None:
                    trimmed, row = trim(row)
                    if trimmed['performed']:
                        stable = None
                        stable_count = 0
                        previous_stamp = None
                        continue
                break
            experiment.wait(p['sample_interval_s'])
            row = observe('stability')
            continue
        stable = None
        stable_count = 0
        previous_stamp = None
        approach(row)
        row = observe('after TEC settle')
    quality = row['optical_quality']
    result = dict(status='succeeded', lock_enabled=False, recovery_algorithm=artifact['algorithm'],
                  recovery_sha256=artifact['artifact_sha256'],
                  outputs=dict(experiment.expected), start_outputs=start, target_frequency_hz=target(p),
                  final_frequency_hz=row['frequency_hz'], final_error_hz=row['error_hz'],
                  iterations=counts['iterations'], hops=counts['hops'], recoveries=counts['recoveries'],
                  recovery_failures=counts['recovery_failures'], consecutive_recovery_failures=consecutive,
                  pzt_trim=trimmed if trimmed is not None else dict(enabled=False, performed=False),
                  pzt_headroom=headroom(), coefficients=k, optical_quality=quality, single_mode=row.get('single_mode'),
                  spectral_quality_passed=None,
                  physical_mode_identity_verified=False, trace=trace, timing=timing())
    experiment.emit('ecdl_operating_point', dict(result, trace=len(trace)))
    return result
