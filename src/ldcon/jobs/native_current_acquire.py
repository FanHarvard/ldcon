"""Finite PI-off DFB/DBR current acquisition using a measured external response."""
import math
import statistics

from ldcon.core.contracts import DeviceError, MeasurementUnavailable
from ldcon.owners.esp32_native_owner import C_NM, channel_metadata, current_for_code, number
from ldcon.core.interface_cards import _check_cancel

DEFAULTS = dict(target_frequency_hz=None, target_wavelength_nm=None, tolerance_hz=1e7,
                timeout_s=300., sample_interval_s=.5, max_sample_age_s=1.,
                settle_s=2., stable_s=5., average_samples=3, max_step_codes=3)


def parameters(card, overrides):
    if not isinstance(overrides, dict) or not isinstance(card['parameters'], dict):
        raise ValueError('Native acquisition parameters must be mappings')
    if (set(overrides) | set(card['parameters'])) - set(DEFAULTS):
        raise ValueError('Unknown native acquisition parameter')
    p = {**DEFAULTS, **card['parameters'], **overrides}
    for key, value in p.items():
        if value is None and key in ('target_frequency_hz', 'target_wavelength_nm'):
            continue
        number(value, key, positive=True)
    if p['target_frequency_hz'] is not None and p['target_wavelength_nm'] is not None:
        raise ValueError('Specify one frequency or wavelength target')
    if not .1 <= p['sample_interval_s'] <= 2 or p['timeout_s'] > 3600:
        raise ValueError('Native acquisition timing is outside supported bounds')
    if not 1 <= p['settle_s'] <= 60 or not 1 <= p['stable_s'] <= 60:
        raise ValueError('Native acquisition requires explicit settling and stable observation')
    if type(p['average_samples']) is not int or not 3 <= p['average_samples'] <= 100:
        raise ValueError('At least three independent samples per acquisition mean are required')
    if type(p['max_step_codes']) is not int or not 1 <= p['max_step_codes'] <= 4:
        raise ValueError('Native acquisition permits at most four codes per command')
    return p


def run_native_acquire(port, p, *, clock, sleep, cancel, emit, laser=None):
    policy = port.policy
    if not policy['calibration']['verified'] or not policy['calibration']['response_verified']:
        raise DeviceError('Verified measured external-current response is required')
    from ldcon.jobs.lock_target import resolve_target
    target = resolve_target(laser or {}, p)
    if target is None:
        raise DeviceError('Native acquisition target is unset (card parameter or laser.target)')
    c = policy['calibration']
    dac = channel_metadata(policy['dac'])
    per_code = dac['voltage_step_v']/(1 << (16-dac['bits']))
    slope = c['wavelength_nm_per_a']*c['current_a_per_v']*per_code
    from ldcon.owners.native_modulation_limits import budget_codes
    maximum = min(p['max_step_codes'], budget_codes(policy))
    if maximum < 1 or not slope:
        raise DeviceError('Policy cannot support a resolved external-current acquisition step')
    started = clock()
    prepared = False
    origin = None
    settle_until = started
    previous_stamp = None
    previous_acquired = None
    rows = []
    stable_since = None
    stable_means = []
    missing_since = None
    writes = 0
    emit('state', dict(state='acquiring', native_current=True, native_pi_active=False))
    while clock()-started < p['timeout_s']:
        _check_cancel(cancel)
        try:
            port.heartbeat()
            if not prepared:
                emit('native_prepared', port.mutate('acquire_prepare', target_wavelength_nm=C_NM/target))
                prepared = True
                settle_until = clock()+p['settle_s']
            state = port.read()
            if state.get('native_pi_active'):
                raise DeviceError('Finite operating-point acquisition requires native PI off')
            optical = state.get('optical') or {}
            acquired = optical.get('acquired_s')
            stamp = optical.get('acquired_unix_s', acquired)
            f = optical.get('frequency_hz')   # rule B 2026-10-22: finite current reading, no quality filter
            if (not isinstance(f, (int, float)) or not math.isfinite(f) or f <= 0
                    or acquired is None or not 0 <= clock()-acquired <= min(policy['max_sample_age_s'], p['max_sample_age_s'])):
                raise MeasurementUnavailable('Fresh independent optical data required')
            if previous_stamp is not None and stamp < previous_stamp:
                raise MeasurementUnavailable('Independent optical acquisition regressed')
            if previous_stamp is None or stamp > previous_stamp:
                if previous_acquired is not None and acquired-previous_acquired > min(policy['max_sample_age_s'], p['max_sample_age_s']):
                    rows = []; stable_since = None; stable_means = []
                previous_stamp, previous_acquired = stamp, acquired
                code = int(state['sample']['servo_config']['control_offset'])
                if origin is None:
                    origin = code
                error = optical['frequency_hz']-target
                emit('trace', dict(frequency_hz=optical['frequency_hz'], wavelength_nm=C_NM/optical['frequency_hz'],
                    error_hz=error, locked=False, native_pi_active=False, dac_code=code,
                    current_estimate_a=current_for_code(policy, code),
                    current_measured_a=(state.get('bias') or {}).get('sample', {}).get('current_read_a')))
                if acquired >= settle_until:
                    rows.append(optical['frequency_hz'])
                    if len(rows) >= p['average_samples']:
                        mean = statistics.mean(rows)
                        rows = []
                        if abs(mean-target) <= p['tolerance_hz']:
                            if stable_since is None:
                                stable_since = acquired
                            stable_means.append(mean)
                            if acquired-stable_since >= p['stable_s'] and len(stable_means) >= 3:
                                result = dict(status='succeeded', operating_point_reached=True, native_pi_active=False,
                                    target_frequency_hz=target, tolerance_hz=p['tolerance_hz'],
                                    final_frequency_hz=mean, stable_s=acquired-stable_since,
                                    stable_means_hz=stable_means, origin_dac_code=origin, dac_code=code, offset_writes=writes,
                                    response_evidence=c['evidence'], measured_wavelength_nm_per_code=slope,
                                    current_transfer_basis='Nominal external-modulation coefficient with configured uncertainty; total current read independently')
                                emit('operating_point_verified', result)
                                return result
                        else:
                            stable_since = None; stable_means = []
                            delta = round((C_NM/target-C_NM/mean)/slope)
                            step = max(-maximum, min(maximum, delta))
                            if step:
                                emit('command_requested', dict(interface='native_current', dac_code=code+step))
                                _check_cancel(cancel)
                                port.mutate('acquire_offset', code=code+step, expected_code=code)
                                writes += 1
                                settle_until = clock()+p['settle_s']
                                emit('command_applied', dict(interface='native_current', dac_code=code+step,
                                    current_estimate_a=current_for_code(policy, code+step)))
                            # A sub-code correction waits for fresh averaged
                            # observations until the same finite deadline.
            if missing_since is not None:
                emit('measurement_recovered', dict(duration_s=clock()-missing_since))
            missing_since = None
        except MeasurementUnavailable as exc:
            rows = []; stable_since = None; stable_means = []
            if missing_since is None:
                missing_since = clock()
                emit('measurement_wait', dict(reason=str(exc), native_pi_active=False))
            if clock()-missing_since >= policy['missing_timeout_s']:
                raise DeviceError('Native acquisition observation deadline exceeded') from exc
        end = clock()+p['sample_interval_s']
        while clock() < end:
            _check_cancel(cancel)
            sleep(max(0., min(.1, end-clock())))
    raise DeviceError('Native operating-point deadline exceeded; last verified fixed reference retained')
