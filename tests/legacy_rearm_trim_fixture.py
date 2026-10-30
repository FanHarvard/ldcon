# Test-only archived re-arm trim; never packaged or dispatched.
"""Bounded open-loop fine trim toward the lock target before the native PZT PI is (re)armed.

Hardware finding 2026-10-27 (preparations/sr422-relock-20261027/LOG.md, 19:5x-20:4x): the tested mapped recovery
(prepare.ecdl_recovery artifact) restores the branch to within its ``tolerance_hz`` (1 GHz) of the reference, but the
owner's ``steady_arm`` requires the measured frequency within the acquisition tolerance (100 MHz capture band), so
every re-arm after a recovery -- and every lock start -- needed a manual open-loop PZT trim (~80 kHz/count measured).

This module is that trim as a card action (rule F): fresh observation -> bounded PZT step toward the target using the
artifact's ``piezo_hz_per_count`` (or the laser profile's ``steady_lock.wavelength_nm_per_count`` converted) ->
fresh observation -> branch-continuity check, at most ``max_steps`` steps and ``max_counts`` counts in total; when
the PZT destination would leave the owner interval the step goes to the TEC (``temperature`` axis, slope from the
axis policy) followed by ``tec_settle_s`` of thermal settling. All moves go through ``BoundedExperiment.move``
(owner limits: interval, max_step, slew, update interval; no host-side caps are added, rule B). A continuity
failure (the measured frequency differs from the predicted one by more than ``hop_threshold_hz``) is a branch loss:
``TrimBranchLost`` -- the caller runs the tested recovery again (bounded by ``mapped_recovery_max_attempts``).
"""
import math

from ldcon.core.contracts import DeviceError

# Steady-lock parameter block ``rearm_trim`` (card parameters; hot-changeable at the steady cycle boundary).
DEFAULTS = dict(enabled=True, max_counts=15000., max_steps=4, tec_settle_s=45.)


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def parameters(value):
    """Normalized ``rearm_trim`` block: None -> defaults, a partial mapping is completed with the defaults."""
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError('rearm_trim must be a mapping')
    if set(value) - DEFAULTS.keys():
        raise ValueError('Unknown rearm_trim parameter: ' + ', '.join(sorted(set(value) - DEFAULTS.keys())))
    p = {**DEFAULTS, **value}
    if type(p['enabled']) is not bool:
        raise ValueError('rearm_trim.enabled must be boolean')
    if not finite(p['max_counts']) or p['max_counts'] <= 0:
        raise ValueError('rearm_trim.max_counts must be finite and positive (PZT counts)')
    if type(p['max_steps']) is not int or not 1 <= p['max_steps'] <= 20:
        raise ValueError('rearm_trim.max_steps must be an integer 1..20')
    if not finite(p['tec_settle_s']) or not 0 <= p['tec_settle_s'] <= 600:
        raise ValueError('rearm_trim.tec_settle_s must be 0..600 seconds')
    return p


class TrimBranchLost(DeviceError):
    """The branch did not follow a trim step (measured vs predicted frequency beyond hop_threshold_hz)."""
    def __init__(self, message, record):
        super().__init__(message)
        self.record = record


def trim_to_target(experiment, *, target_hz, tolerance_hz, hop_threshold_hz, piezo_hz_per_count, settings, emit,
                   stage):
    """Trim the open-loop laser toward ``target_hz`` until |f - target| <= tolerance_hz; returns the record.

    ``experiment`` is a ``BoundedExperiment`` on the lock's recovery ports (open loop, PI held). The record is also
    emitted as ``ecdl_rearm_trim``. Raises ``TrimBranchLost`` on a continuity failure (after the owner-limited move
    has been applied; nothing is undone -- the caller's recovery is the tested way back), ``DeviceError`` when
    neither axis can reach the target inside the owner interval.
    """
    p = parameters(settings)
    piezo = experiment.policies['piezo']
    temperature = experiment.policies['temperature']
    tec_slope = temperature.frequency_hz_per_unit
    row = experiment.read('re-arm trim observation (' + stage + ')', require_frequency=True)
    before = row['frequency_hz']
    record = dict(stage=stage, enabled=p['enabled'], before_hz=before, after_hz=before, target_hz=target_hz,
                  tolerance_hz=tolerance_hz, piezo_hz_per_count=piezo_hz_per_count,
                  temperature_hz_per_unit=tec_slope, steps=[], counts_moved=0., tec_moved=0.,
                  within_tolerance=abs(before - target_hz) <= tolerance_hz, reason=None,
                  settings={k: p[k] for k in ('max_counts', 'max_steps', 'tec_settle_s')},
                  outputs_before=dict(experiment.expected))
    if not p['enabled']:
        record['reason'] = 'disabled'
    elif record['within_tolerance']:
        record['reason'] = 'already within tolerance'
    elif not finite(piezo_hz_per_count) or piezo_hz_per_count == 0:
        record['reason'] = 'piezo_hz_per_count unavailable (artifact and laser profile)'
    budget = p['max_counts']
    try:
        while record['reason'] is None:
            error = target_hz - row['frequency_hz']
            if abs(error) <= tolerance_hz:
                record['within_tolerance'] = True
                record['reason'] = 'within tolerance'
                break
            if len(record['steps']) >= p['max_steps']:
                record['reason'] = 'max_steps reached'
                break
            if budget <= 0:
                record['reason'] = 'max_counts reached'
                break
            counts = error / piezo_hz_per_count
            counts = math.copysign(min(abs(counts), budget), counts)
            destination = experiment.expected['piezo'] + counts
            step = dict(index=len(record['steps']), before_hz=row['frequency_hz'], error_hz=error)
            if piezo.low <= destination <= piezo.high:
                axis, slope, delta = 'piezo', piezo_hz_per_count, counts
                step.update(axis='piezo', counts=counts, destination=destination)
                experiment.move('piezo', destination, 're-arm trim PZT step')
                applied = experiment.expected['piezo'] - (destination - counts)
                budget -= abs(applied)
                record['counts_moved'] += applied
                step['applied'] = applied
                delta = applied
            else:
                # The PZT would leave the owner interval: one TEC step instead (slope from the axis policy), then
                # the thermal settling wait before the next observation.
                if not finite(tec_slope) or tec_slope == 0:
                    raise DeviceError('Re-arm trim: the PZT destination %.6g leaves the owner interval [%.6g, %.6g] and '
                                      'the temperature axis has no calibrated frequency_hz_per_unit'
                                      % (destination, piezo.low, piezo.high))
                tec_delta = error / tec_slope
                tec_destination = experiment.expected['temperature'] + tec_delta
                if not temperature.low <= tec_destination <= temperature.high:
                    raise DeviceError('Re-arm trim: neither the PZT (%.6g) nor the TEC (%.6g) can reach the target '
                                      'inside the owner intervals' % (destination, tec_destination))
                axis, slope = 'temperature', tec_slope
                step.update(axis='temperature', destination=tec_destination, tec_delta=tec_delta,
                            pzt_destination_refused=destination, settle_s=p['tec_settle_s'])
                origin = experiment.expected['temperature']
                experiment.move('temperature', tec_destination, 're-arm trim TEC step')
                delta = experiment.expected['temperature'] - origin
                record['tec_moved'] += delta
                step['applied'] = delta
                if p['tec_settle_s']:
                    experiment.wait(p['tec_settle_s'])
            predicted = row['frequency_hz'] + slope * delta
            row = experiment.read('re-arm trim observation after step', require_frequency=True)
            step.update(after_hz=row['frequency_hz'], predicted_hz=predicted,
                        residual_hz=row['frequency_hz'] - predicted)
            record['steps'].append(step)
            record['after_hz'] = row['frequency_hz']
            if abs(row['frequency_hz'] - predicted) > hop_threshold_hz:
                record['reason'] = 'branch lost'
                raise TrimBranchLost('Re-arm trim: the branch did not follow the %s step (measured %.6g Hz, predicted '
                                     '%.6g Hz, hop threshold %.6g Hz)' % (axis, row['frequency_hz'], predicted, hop_threshold_hz),
                                     record)
    finally:
        record['after_hz'] = row['frequency_hz']
        record['within_tolerance'] = abs(record['after_hz'] - target_hz) <= tolerance_hz
        record['outputs_after'] = dict(experiment.expected)
        record['elapsed_s'] = experiment.clock() - experiment.started
        emit('ecdl_rearm_trim', dict(record))
    return record
