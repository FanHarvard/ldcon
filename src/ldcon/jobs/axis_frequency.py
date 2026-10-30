"""Shared bounded-axis optical calibration, independent of actuator units/vendor.

A proposed calibration never installs itself. Both directions are measured;
quality evidence and context are retained even when a result is inconclusive.
"""
from dataclasses import asdict
import math
from pathlib import Path
import statistics

from ldcon.core.contracts import DeviceError, MeasurementUnavailable
from ldcon.core.interface_cards import CardCancelled
from ldcon.core.records import atomic_json
from ldcon.jobs.measurement_acquisition import collect, source_eligible


KEYS = {'offsets', 'repeats', 'samples_per_point', 'settle_s', 'sample_interval_s',
        'max_sample_age_s', 'timeout_s', 'point_timeout_s', 'max_discontinuity_hz',
        'max_fit_residual_hz', 'max_hysteresis_hz', 'min_signal_hz', 'require_single_mode',
        'quality_limits', 'restore_origin', 'write_report'}


def parameters(card, overrides):
    p = {**card['parameters'], **overrides}
    if set(p) != KEYS:
        raise ValueError('Axis calibration parameters missing/unknown: '+str(sorted(set(p)^KEYS)))
    for key in ('repeats', 'samples_per_point'):
        if type(p[key]) is not int or p[key] < 1:
            raise ValueError(key+' must be a positive integer')
    for key in ('require_single_mode', 'restore_origin', 'write_report'):
        if type(p[key]) is not bool:
            raise ValueError(key+' must be boolean')
    if not isinstance(p['quality_limits'], dict):
        raise ValueError('Quality limits must be a metric mapping')
    offsets = p['offsets']
    if not isinstance(offsets, list) or len(offsets) < 3 or any(type(x) not in (int,float) or not math.isfinite(x) for x in offsets):
        raise ValueError('At least three finite offsets are required')
    if offsets != sorted(set(offsets)) or not offsets[0] < 0 < offsets[-1] or 0 not in offsets:
        raise ValueError('Offsets must be increasing, unique and include negative, zero and positive')
    for key in KEYS - {'offsets','repeats','samples_per_point','quality_limits','require_single_mode','restore_origin','write_report'}:
        if type(p[key]) not in (int,float) or not math.isfinite(p[key]) or p[key] <= 0:
            raise ValueError('Expected positive '+key)
    if p['point_timeout_s'] > p['timeout_s']:
        raise ValueError('Point timeout exceeds study deadline')
    if any(type(v) not in (int,float) or not math.isfinite(v) for v in p['quality_limits'].values()):
        raise ValueError('Nonfinite quality limit')
    return p


def fit(points, p):
    """Offset-referenced regression avoids subtracting optical-scale squares."""
    if len(points) < 3:
        return dict(valid=False, reason='insufficient points')
    x0 = statistics.mean(x['axis_value'] for x in points)
    f0 = points[0]['frequency_mean_hz']
    xs = [x['axis_value']-x0 for x in points]
    ys = [x['frequency_mean_hz']-f0 for x in points]
    mean = statistics.mean(ys)
    denom = sum(x*x for x in xs)
    if denom == 0:
        return dict(valid=False, reason='no actuator span')
    slope = sum(x*(y-mean) for x,y in zip(xs,ys))/denom
    residuals = [y-mean-slope*x for x,y in zip(xs,ys)]
    peak_residual = max(abs(v) for v in residuals)
    grouped = {}
    for point in points:
        grouped.setdefault(point['axis_value'], []).append(point['frequency_mean_hz'])
    hysteresis = max(max(v)-min(v) for v in grouped.values())
    signal = abs(slope)*(max(xs)-min(xs))
    reasons = []
    if signal < p['min_signal_hz']:reasons.append('insufficient optical signal')
    if peak_residual > p['max_fit_residual_hz']:reasons.append('nonlinearity/residual limit')
    if hysteresis > p['max_hysteresis_hz']:reasons.append('hysteresis or repeat drift limit')
    directional = {}
    for direction in ('up','down'):
        subset = [x for x in points if x['direction']==direction]
        dx = [x['axis_value'] for x in subset]
        dy = [x['frequency_mean_hz']-f0 for x in subset]
        mx, my = statistics.mean(dx), statistics.mean(dy)
        d = sum((x-mx)**2 for x in dx)
        directional[direction] = sum((x-mx)*(y-my) for x,y in zip(dx,dy))/d if d else None
    return dict(valid=not reasons, reasons=reasons, frequency_hz_per_unit=slope,
                axis_reference=x0, frequency_reference_hz=f0+mean,
                max_residual_hz=peak_residual, hysteresis_or_drift_hz=hysteresis,
                directional_hz_per_unit=directional, signal_hz=signal,
                residuals_hz=residuals, adopted=False)


def run(ports, p, directory, *, clock, sleep, cancel, emit):
    p = parameters({'parameters':p}, {})
    axis, observation, loop = (ports[k] for k in ('axis','observation','loop'))
    policy = axis.policy
    origin = axis.readback()
    values = [origin+v for v in p['offsets']]
    if any(not policy.low <= value <= policy.high for value in values):
        raise ValueError('Calibration scan outside approved axis bounds')
    if not loop.is_open_loop():
        raise DeviceError('Calibration requires PI/scan and competing outer loops held')
    started = clock()
    result = dict(schema_version=1, operation='identify.axis_frequency', status='running',
                  parameters=p, origin=origin, axis=asdict(policy), points=[], raw=[], adopted=False,
                  context=ports.get('context', lambda: {})(), clock='monotonic acquisition seconds',
                  calibration_kind='combined operational response; see recorded FF context')
    directory = Path(directory)
    directory.mkdir(parents=True,exist_ok=True)
    artifact = directory/'axis-frequency-calibration.json'
    last_stamp = None
    last_frequency = None
    restored = False
    atomic_json(artifact,result)  # Prove durable evidence storage before any move.

    def check():
        if cancel():raise CardCancelled('Axis calibration cancelled; current output held')
        if clock()-started >= p['timeout_s']:raise DeviceError('Axis calibration deadline')
        if not loop.is_open_loop() or axis.policy != policy:
            raise DeviceError('Axis calibration context changed')

    def pause(duration):
        until = clock()+duration
        while clock() < until:
            check()
            sleep(min(p['sample_interval_s'], until-clock()))

    try:
        for repeat in range(p['repeats']):
            for direction, targets in (('up', values), ('down', list(reversed(values)))):
                for value in targets:
                    check()
                    axis.move(value, policy.update_interval_s)
                    moved_at = clock()
                    actual = axis.readback()
                    if abs(actual-value) > max(policy.resolution, 1e-12):
                        raise DeviceError('Calibration axis readback mismatch')
                    emit('calibration_move', dict(value=actual, unit=policy.unit, direction=direction))
                    pause(max(p['settle_s'],policy.settle_s))
                    samples = []
                    def acquire_sample():
                        nonlocal last_stamp,last_frequency
                        try:
                            o = observation.read_mode()
                        except (MeasurementUnavailable, OSError):
                            return False
                        sample = o.frequency
                        raw = dict(repeat=repeat, direction=direction, axis_value=actual, **asdict(o))
                        raw['accepted'] = False
                        result['raw'].append(raw)
                        good = (sample.acquisition_known and sample.unit=='Hz' and sample.frequency_hz is not None and
                                math.isfinite(sample.frequency_hz) and sample.frequency_hz>0 and
                                source_eligible(sample.acquired_s,previous=last_stamp,now=clock(),
                                    max_age_s=p['max_sample_age_s'],after=moved_at) and o.quality_valid)
                        if good:
                            last_stamp = sample.acquired_s
                            if p['require_single_mode'] and not o.single_mode:
                                raise DeviceError('Optical single-mode requirement lost')
                            if any(k not in o.metrics or not math.isfinite(o.metrics[k]) or o.metrics[k]>v for k,v in p['quality_limits'].items()):
                                raise DeviceError('Calibration optical quality requirement lost')
                            if last_frequency is not None and abs(sample.frequency_hz-last_frequency)>p['max_discontinuity_hz']:
                                raise DeviceError('Possible mode discontinuity; calibration invalid')
                            last_frequency = sample.frequency_hz
                            raw['accepted'] = True
                            samples.append(sample.frequency_hz)
                        atomic_json(artifact,result)
                        return good
                    collect(acquire_sample,clock=clock,pause=pause,check=check,
                        sample_interval_s=p['sample_interval_s'],minimum_samples=p['samples_per_point'],
                        timeout_s=p['point_timeout_s'])
                    point = dict(axis_value=actual, direction=direction, repeat=repeat,
                                 frequency_mean_hz=statistics.mean(samples),
                                 frequency_stddev_hz=statistics.pstdev(samples), count=len(samples),
                                 moved_s=moved_at, end_s=clock())
                    result['points'].append(point)
                    emit('calibration_point',point)
                    atomic_json(artifact,result)
        result['fit'] = fit(result['points'],p)
        if p['restore_origin']:
            check()
            axis.move(origin,policy.update_interval_s)
            restored = abs(axis.readback()-origin) <= max(policy.resolution,1e-12)
            if not restored:raise DeviceError('Origin restoration not verified')
        result['status'] = 'succeeded' if result['fit']['valid'] else 'inconclusive'
        result['message'] = 'Calibration proposal only; owner adoption requires explicit review'
        return result
    except BaseException as error:
        result.update(status='cancelled' if isinstance(error,CardCancelled) else 'failed', reason=str(error))
        raise
    finally:
        result.update(elapsed_s=clock()-started, origin_restored=restored)
        atomic_json(artifact,result)
        if p['write_report']:
            from ldcon.jobs.axis_frequency_report import write_report
            try:
                write_report(directory,result)
                result['pdf_report'] = 'report/report.pdf'
            except Exception as error:
                # A plotting installation failure does not erase physical results.
                result['report_error'] = type(error).__name__+': '+str(error)
            atomic_json(artifact,result)


def open_ports(laser, card):
    from ldcon.jobs.ecdl_devices import OwnerSession, AxisProxy, ModeProxy, LoopProxy
    binding = laser['interfaces']['axis']
    device = laser['devices'][binding['device']]
    if not laser['allow_output']:
        raise DeviceError('Laser output writes disabled')
    if any(laser['interfaces'][key]['device']!=binding['device'] for key in ('observation','loop')):
        raise ValueError('Calibration axis, observation and loop must share one authority')
    if device['type']=='dlcpro.laser.owner':
        if device['control_settings'].get('commissioned') is not True:
            raise DeviceError('DLC commissioning incomplete')
        from ldcon.jobs.dlcpro_cards import Session
        session=Session(device).connect()
    elif device['type']=='ecdl.rig.owner':
        session=OwnerSession(device).connect(allow_prepare=False)
    else:
        raise ValueError('No calibration owner adapter')
    return dict(axis=AxisProxy(session,binding['axis']), observation=ModeProxy(session), loop=LoopProxy(session),
                context=lambda: dict(snapshot=session.snapshot(), device_revision=session.device,
                                     ff_mode='as_configured; calibration does not toggle feedforward'))
