"""One change/measurement transaction, reused by finite batches and optimizers.

The adapter owns all physical authority. This runner never retries a mutation,
rolls back an unknown write, or infers source time from its polling clock.
"""
import copy
import math
import time

from ldcon.core.contracts import DeviceError, MeasurementUnavailable
from ldcon.core.interface_cards import CardCancelled
from ldcon.jobs.measurement_statistics import numeric_summary
from ldcon.jobs.measurement_acquisition import collect, source_eligible

SCHEMA = 'ldcon.change-measure.v1'
MEASUREMENTS = ('wavelength', 'optical_quality', 'dither')
REQUIRED_PARAMETERS = frozenset({
    'changes', 'apply_mode', 'measurements', 'baseline_s', 'wait_s',
    'duration_s', 'history_wait_s', 'sample_interval_s', 'minimum_samples',
    'measurement_max_age_s', 'max_sample_gap_s', 'timeout_s',
})
OPTIONAL_PARAMETERS = frozenset({'target_frequency_hz'})


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def parameters(card, overrides):
    base = card.get('parameters', {})
    if (not isinstance(base, dict) or not isinstance(overrides, dict)
            or (set(base) | set(overrides)) - (REQUIRED_PARAMETERS | OPTIONAL_PARAMETERS)):
        raise ValueError('Unknown change/measurement parameters')
    # The caller resolves request > assignment > deck. Never invent operational
    # timings, counts, modes or a measurement selection while admitting a run.
    missing = REQUIRED_PARAMETERS - (set(base) | set(overrides))
    if missing:
        raise ValueError('Missing explicit change/measurement parameters: ' + ', '.join(sorted(missing)))
    # No frequency reference means no target-relative statistic; omitted switches
    # below mean disabled. These structural defaults do not initiate an action.
    p = copy.deepcopy({'target_frequency_hz': None, **base, **overrides})
    if p['apply_mode'] not in ('atomic', 'coordinated', 'ordered'):
        raise ValueError('Unknown application mode')
    if not isinstance(p['changes'], list) or len(p['changes']) > 32:
        raise ValueError('Expected at most 32 parameter changes')
    names = set()
    for item in p['changes']:
        if (not isinstance(item, dict) or set(item) not in ({'parameter', 'value'}, {'parameter', 'offset'})
                or not isinstance(item['parameter'], str) or not item['parameter']
                or item['parameter'] in names or not finite(item.get('value', item.get('offset')))):
            raise ValueError('Each change needs a unique parameter and exactly one finite value or offset')
        names.add(item['parameter'])
    flags = p['measurements']
    if not isinstance(flags, dict) or set(flags) - set(MEASUREMENTS) or any(type(v) is not bool for v in flags.values()):
        raise ValueError('Measurement switches must be booleans')
    p['measurements'] = {key: flags.get(key, False) for key in MEASUREMENTS}
    for name in ('baseline_s', 'wait_s', 'duration_s', 'history_wait_s'):
        if not finite(p[name]) or not 0 <= p[name] <= 86400:
            raise ValueError('Invalid ' + name)
    for name in ('sample_interval_s', 'measurement_max_age_s', 'max_sample_gap_s', 'timeout_s'):
        if not finite(p[name]) or not 0 < p[name] <= 86400:
            raise ValueError('Invalid ' + name)
    if p['baseline_s'] + p['wait_s'] + p['duration_s'] >= p['timeout_s']:
        raise ValueError('Deadline must leave time for application and measurements')
    if type(p['minimum_samples']) is not int or not 1 <= p['minimum_samples'] <= 100000:
        raise ValueError('Invalid minimum sample count')
    if p['target_frequency_hz'] is not None and (not finite(p['target_frequency_hz']) or p['target_frequency_hz'] <= 0):
        raise ValueError('Target frequency must be finite and positive')
    if any(p['measurements'].values()) and p['duration_s'] <= 0:
        raise ValueError('Selected measurements require a positive acquisition duration')
    return p


def batch_parameters(card, overrides):
    base = card.get('parameters', {})
    if not isinstance(base, dict) or not isinstance(overrides, dict) or (set(base) | set(overrides)) - {'steps', 'timeout_s'}:
        raise ValueError('Batch accepts only steps and timeout_s')
    missing = {'steps', 'timeout_s'} - (set(base) | set(overrides))
    if missing:
        raise ValueError('Missing explicit batch parameters: ' + ', '.join(sorted(missing)))
    p = copy.deepcopy({**base, **overrides})
    if not isinstance(p['steps'], list) or not 0 <= len(p['steps']) <= 1000:
        raise ValueError('Batch needs 1..1000 explicit measurement steps')
    if not finite(p['timeout_s']) or not 0 < p['timeout_s'] <= 86400:
        raise ValueError('Invalid batch deadline')
    p['steps'] = [parameters({'parameters': step}, {}) for step in p['steps']]
    return p


class ChangeMeasure:
    """Adapter contract: preflight(changes, mode, measurements, planned=None),
    apply(plan, sample), read(measurements), check(). Preflight has no writes.
    apply returns an owner-confirmed receipt with status=applied and readback.
    read returns independently timestamped blocks for enabled measurements.
    """
    def __init__(self, adapter, p, *, clock=time.monotonic, wall_clock=time.time,
                 sleep=time.sleep, cancel=lambda: False, emit=lambda *_: None):
        self.adapter, self.p = adapter, parameters({'parameters': p}, {})
        self.clock, self.wall_clock, self.sleep, self.cancel, self.emit = clock, wall_clock, sleep, cancel, emit
        self.started = clock()
        self.rows = []
        self.last_sources = {}
        self.applied_unix_s = None
        self.result = dict(schema=SCHEMA, status='running', physical_outcome='not_applied',
                           parameters=copy.deepcopy(self.p), samples=self.rows, application=None, rejected_samples={})

    def check(self):
        if self.cancel():
            raise CardCancelled('Change/measurement cancelled; no implicit restoration')
        if self.clock() - self.started >= self.p['timeout_s']:
            raise DeviceError('Change/measurement deadline exceeded')
        self.adapter.check()

    def sample(self, phase):
        self.check()
        if not any(self.p['measurements'].values()):
            return
        try:
            data = self.adapter.read(self.p['measurements'])
        except MeasurementUnavailable as error:
            self.emit('change_measure_missing', dict(phase=phase, reason=str(error)))
            return
        row = dict(phase=phase, received_unix_s=self.wall_clock(), measurements={})
        for name, enabled in self.p['measurements'].items():
            if not enabled:
                continue
            block = data.get(name) or {}
            values=block.get('values') if isinstance(block,dict) else None
            if not isinstance(values,dict) or not values or not all(finite(v) for v in values.values()):
                reason='nonfinite_or_missing_values'
                rejected=self.result['rejected_samples'].setdefault(name,{})
                rejected[reason]=rejected.get(reason,0)+1
                continue
            stamp = block.get('acquired_unix_s')
            if (block.get('valid') is not True or not finite(stamp)
                    or not source_eligible(stamp,previous=self.last_sources.get(name),now=self.wall_clock(),
                        max_age_s=self.p['measurement_max_age_s'])):
                continue
            if phase == 'measurement' and stamp < self.result.get('measurement_started_unix_s', self.wall_clock()):
                continue
            if phase == 'measurement' and self.applied_unix_s is not None:
                start = block.get('source_start_unix_s', stamp)
                if not finite(start) or start <= self.applied_unix_s:
                    continue
            self.last_sources[name] = stamp
            row['measurements'][name] = copy.deepcopy(block)
        if row['measurements']:
            self.rows.append(row)
            self.emit('change_measure_sample', row)
            return True
        return False

    def acquire(self, phase, seconds):
        return collect(lambda:self.sample(phase),clock=self.clock,pause=self.sleep,
            check=self.check,sample_interval_s=self.p['sample_interval_s'],duration_s=seconds)

    def summarize(self):
        result = {}
        for name, enabled in self.p['measurements'].items():
            if not enabled:
                continue
            rows = [r['measurements'][name] for r in self.rows if r['phase'] == 'measurement' and name in r['measurements']]
            stamps = [r['acquired_unix_s'] for r in rows]
            gaps = [b-a for a,b in zip(stamps, stamps[1:])]
            if stamps:
                gaps += [stamps[0]-self.result['measurement_started_unix_s'],
                         self.result['measurement_ended_unix_s']-stamps[-1]]
            enough = len(rows) >= self.p['minimum_samples'] and max(gaps, default=0.) <= self.p['max_sample_gap_s']
            summary = dict(status='valid' if enough else 'insufficient', samples=len(rows),
                           max_sample_gap_s=max(gaps, default=None), statistics={})
            keys = set.intersection(*(set(r.get('values', {})) for r in rows)) if rows else set()
            for key in sorted(keys):
                values = [r['values'][key] for r in rows]
                if not all(finite(v) for v in values):
                    continue
                stats = numeric_summary(values, reference=self.p['target_frequency_hz'] if key == 'frequency_hz' else None)
                summary['statistics'][key] = stats
            if not summary['statistics']:
                summary['status'] = 'insufficient'
            result[name] = summary
        return result

    def run(self, plan=None):
        finalized = False
        try:
            self.check()
            plan = plan if plan is not None else self.adapter.preflight(self.p['changes'], self.p['apply_mode'], self.p['measurements'])
            self.result['plan'] = copy.deepcopy(plan)
            self.emit('change_measure_preflight', plan)
            self.result['baseline_started_unix_s']=self.wall_clock()
            self.acquire('baseline', self.p['baseline_s'])
            self.check()
            if self.p['changes']:
                self.result['physical_outcome'] = 'application_in_progress'
                plan['runtime_timeout_s'] = max(.001, self.p['timeout_s']-(self.clock()-self.started))
                receipt = self.adapter.apply(plan, lambda: self.sample('transition'))
                if not isinstance(receipt, dict) or receipt.get('status') != 'applied' or not isinstance(receipt.get('readback'), dict):
                    raise DeviceError('Owner-confirmed application receipt and readback required')
                self.result.update(application=copy.deepcopy(receipt), physical_outcome='applied')
            self.applied_unix_s = self.wall_clock()
            self.result['application_completed_unix_s'] = self.applied_unix_s
            self.acquire('wait', self.p['wait_s'])
            self.result['measurement_started_unix_s'] = self.wall_clock()
            self.acquire('measurement', self.p['duration_s'])
            self.result['measurement_ended_unix_s'] = self.wall_clock()
            if hasattr(self.adapter,'complete_measurement'):
                self.adapter.complete_measurement(self.result, check=self.check,
                    remaining_s=max(0.,self.p['timeout_s']-(self.clock()-self.started)))
            self.check()
            summary = self.summarize()
            self.result.update(summary=summary, status='succeeded' if all(v['status']=='valid' for v in summary.values()) else 'measurement_incomplete')
            finalized = True
            if hasattr(self.adapter,'finish'):
                try:
                    self.adapter.finish(self.result)
                except Exception as cleanup_error:
                    self.result['cleanup_error'] = str(cleanup_error)
                    self.result['physical_outcome'] = 'unknown'
                    raise
        except (Exception, KeyboardInterrupt) as error:
            self.result.update(status='cancelled' if isinstance(error, CardCancelled) else 'failed', error=str(error))
            if self.result['physical_outcome']=='application_in_progress':
                outcome='unknown'
                if hasattr(self.adapter,'physical_outcome_on_error'):
                    try:outcome=self.adapter.physical_outcome_on_error(error)
                    except Exception:pass
                self.result['physical_outcome']=outcome if outcome in ('not_applied','applied','unknown') else 'unknown'
            if not finalized and hasattr(self.adapter,'finish'):
                try:
                    self.adapter.finish(self.result)
                except Exception as cleanup_error:
                    self.result['cleanup_error'] = str(cleanup_error)
                    self.result['physical_outcome'] = 'unknown'
            self.result['elapsed_s'] = self.clock()-self.started
            error.measurement_result = self.result
            self.emit('change_measure_result', self.result)
            raise
        self.result['elapsed_s'] = self.clock()-self.started
        self.emit('change_measure_result', self.result)
        return self.result


def run(adapter, p, **arguments):
    return ChangeMeasure(adapter, p, **arguments).run()


def run_batch(adapter, p, *, clock=time.monotonic, cancel=lambda: False, emit=lambda *_: None, **arguments):
    p = batch_parameters({'parameters': p}, {})
    if not p['steps']:
        raise ValueError('Batch execution requires at least one step')
    started = clock()
    result = dict(schema='ldcon.change-measure-batch.v1', status='running', steps=[])
    # Validate every step's capability/ranges before the first mutation. Relative
    # values resolve from the previous planned target and are then fixed.
    plans, planned = [], None
    def interrupted():
        return cancel() or clock()-started >= p['timeout_s']
    try:
        for step in p['steps']:
            if interrupted():
                raise CardCancelled('Batch cancelled or deadline reached during preflight')
            plan = adapter.preflight(step['changes'], step['apply_mode'], step['measurements'], planned=planned)
            plans.append(plan)
            planned = plan.get('planned', planned)
        for index, (step, plan) in enumerate(zip(p['steps'], plans)):
            remaining = p['timeout_s']-(clock()-started)
            if interrupted() or remaining <= step['baseline_s']+step['wait_s']+step['duration_s']:
                raise CardCancelled('Batch cancelled or remaining deadline cannot cover next measurement')
            step = dict(step, timeout_s=min(step['timeout_s'], remaining))
            runner = ChangeMeasure(adapter, step, clock=clock, cancel=interrupted, emit=emit, **arguments)
            try:
                row = runner.run(plan)
            except (Exception, KeyboardInterrupt):
                result['steps'].append(copy.deepcopy(runner.result))
                raise
            result['steps'].append(row)
            emit('change_measure_batch_step', dict(index=index, result=row))
            if row['status'] != 'succeeded':
                result['status'] = 'measurement_incomplete'
                break
        else:
            result['status'] = 'succeeded'
    except (Exception, KeyboardInterrupt) as error:
        result.update(status='cancelled' if isinstance(error, CardCancelled) else 'failed', error=str(error))
        result['elapsed_s']=clock()-started
        result['physical_outcome']='unknown' if any(s.get('physical_outcome')=='unknown' for s in result['steps']) else ('applied' if any(s.get('physical_outcome')=='applied' for s in result['steps']) else 'not_applied')
        error.measurement_result=result
        emit('change_measure_batch_result', result)
        raise
    result['elapsed_s'] = clock()-started
    result['physical_outcome']='applied' if any(s.get('physical_outcome')=='applied' for s in result['steps']) else 'not_applied'
    emit('change_measure_batch_result', result)
    return result
