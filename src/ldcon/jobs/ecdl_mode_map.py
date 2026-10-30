"""Directed, open-loop ECDL maps and bounded experiment primitives.

An optical branch is an observed continuous path, not a longitudinal mode
number. Device calibration/classification remains in the optical adapter.
"""
import copy
from dataclasses import asdict
import hashlib
import json
import math
import time

from ldcon.core.contracts import DeviceError, MeasurementUnavailable
from ldcon.core.interface_cards import CardCancelled


SCHEMA = 'ldcon.ecdl-mode-map.v1'
DEFAULTS = dict(target_frequency_hz=None, target_wavelength_nm=None, current_start_fraction=None,
                current_end_fraction=None, current_step_fraction=.01,
                conditioning_fractions=[], dwell_s=1., sample_interval_s=.5,
                max_sample_age_s=1., measurement_timeout_s=120., timeout_s=3600.,
                discontinuity_hz=None, capture_hz=None, min_branch_points=3,
                selected_branch_id=None, selection_author='automatic',
                ranking_weights=dict(distance=1., width=1., quality=1.))


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def artifact_hash(value):
    data = {k: v for k, v in value.items() if k != 'artifact_sha256'}
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(',', ':'),
                                    allow_nan=False).encode()).hexdigest()


def seal(value):
    value['artifact_sha256'] = artifact_hash(value)
    return value


def checked_artifact(value, schema):
    if not isinstance(value, dict) or value.get('schema') != schema:
        raise ValueError('Unexpected ECDL artifact schema')
    if value.get('artifact_sha256') != artifact_hash(value):
        raise ValueError('ECDL artifact hash mismatch')
    return copy.deepcopy(value)


def compact_row(row):
    """Events carry scalar evidence; complete profiles live in the artifact."""
    result = copy.deepcopy(row)
    for container in (result, result.get('observation', {})):
        analysis = container.get('fringe_analysis')
        if isinstance(analysis, dict):
            container['fringe_analysis'] = {key: value for key, value in analysis.items()
                                            if not isinstance(value, (list, tuple))}
    return result


def parameters(card, overrides):
    if not isinstance(card.get('parameters'), dict) or not isinstance(overrides, dict):
        raise ValueError('Mode map parameters must be mappings')
    p = copy.deepcopy({**DEFAULTS, **card['parameters'], **overrides})
    if set(p) - DEFAULTS.keys():
        raise ValueError('Unknown mode map parameter')
    nullable = {'target_frequency_hz', 'target_wavelength_nm', 'current_start_fraction', 'current_end_fraction',
                'discontinuity_hz', 'capture_hz'}
    for name in nullable:
        if p[name] is None:
            continue
        if not finite(p[name]) or p[name] < 0:
            raise ValueError('Invalid ' + name)
    for name in ('current_start_fraction', 'current_end_fraction'):
        if p[name] is not None and not 0 <= p[name] <= 1:
            raise ValueError('Current endpoint outside approved interval')
    for name in ('target_frequency_hz', 'target_wavelength_nm', 'discontinuity_hz', 'capture_hz'):
        if p[name] is not None and p[name] <= 0:
            raise ValueError('Positive ' + name + ' required')
    if p['target_wavelength_nm'] is not None:
        converted = 299792458e9 / p['target_wavelength_nm']
        if p['target_frequency_hz'] is not None and not math.isclose(
                p['target_frequency_hz'], converted, rel_tol=1e-12, abs_tol=1.):
            raise ValueError('Frequency and wavelength targets are inconsistent')
        p['target_frequency_hz'] = converted
    for name in ('current_step_fraction', 'dwell_s', 'sample_interval_s',
                 'max_sample_age_s', 'measurement_timeout_s', 'timeout_s'):
        if not finite(p[name]) or p[name] <= 0:
            raise ValueError('Positive ' + name + ' required')
    if p['current_step_fraction'] > .25 or p['sample_interval_s'] < .05 or p['timeout_s'] > 86400:
        raise ValueError('Invalid finite map budget')
    if type(p['min_branch_points']) is not int or not 2 <= p['min_branch_points'] <= 100:
        raise ValueError('Invalid branch point count')
    values = p['conditioning_fractions']
    if not isinstance(values, list) or len(values) > 100 or any(not finite(v) or not 0 <= v <= 1 for v in values):
        raise ValueError('Conditioning must use approved-axis fractions')
    weights = p['ranking_weights']
    if (not isinstance(weights, dict) or set(weights) != {'distance', 'width', 'quality'}
            or any(not finite(v) or v < 0 for v in weights.values()) or not any(weights.values())):
        raise ValueError('Invalid branch ranking weights')
    if p['selected_branch_id'] is not None and not isinstance(p['selected_branch_id'], str):
        raise ValueError('Branch selection must be an id')
    if p['selection_author'] not in ('automatic', 'human', 'agent'):
        raise ValueError('Unknown selection author')
    a, b = p['current_start_fraction'], p['current_end_fraction']
    if a is not None and b is not None:
        if a == b or math.ceil(abs(b - a) / p['current_step_fraction']) > 500:
            raise ValueError('Map must contain 1..500 directed intervals')
    return p


def runtime_specs(p):
    """Only explicit safe-boundary changes; physical discovery path is frozen."""
    from ldcon.jobs.runtime_parameters import ParameterSpec
    specs = {
        'dwell_s': ParameterSpec(boundaries=('between_points',),
            description='次の測定点の待機時間。機器の整定時間より短くならない'),
        'sample_interval_s': ParameterSpec(boundaries=('between_points',),
            maximum=p['sample_interval_s'], description='観測再試行間隔。開始時の間隔を超えて延ばさない'),
        'measurement_timeout_s': ParameterSpec(boundaries=('between_points',),
            maximum=p['measurement_timeout_s'], description='欠測待機期限を短縮'),
        'ranking_weights': ParameterSpec(boundaries=('before_selection',),
            description='保存した点の近さ・幅・品質の順位付け重み'),
        'selected_branch_id': ParameterSpec(boundaries=('before_selection',), group='branch_choice',
            description='保存した適格枝から選択'),
        'selection_author': ParameterSpec(boundaries=('before_selection',), group='branch_choice',
            description='人間・エージェント・自動の選択者を同時記録'),
    }
    for name in ('target_frequency_hz', 'target_wavelength_nm', 'capture_hz', 'discontinuity_hz'):
        specs[name] = ParameterSpec(boundaries=('before_selection',), group='map_selection_context',
            description='取得済みの全点を新しい目標・候補距離・枝分割条件で再評価し、取得時設定とは別に保存')
    return specs


class BoundedExperiment:
    """Fresh observations, immutable device policies and explicitly safe writes."""

    def __init__(self, ports, p, *, clock, sleep, cancel, emit, runtime=None):
        self.ports = ports
        self.p = p
        self.clock, self.sleep, self.cancel, self.emit = clock, sleep, cancel, emit
        self.runtime = runtime
        self.started = clock()
        self.started_unix_s = time.time()
        self.axes = {name: ports[name] for name in ('current', 'piezo', 'temperature')}
        self.policies = {name: axis.policy for name, axis in self.axes.items()}
        for policy in self.policies.values():
            policy.validate()
        self.expected = {name: axis.readback() for name, axis in self.axes.items()}
        self.last_write = {name: self.started for name in self.axes}
        self.last_stamp = None
        self.commands = []
        self.require_frequency = True
        self.observation_observer = None
        self.classification_revision = None

    def check(self):
        if self.cancel():
            raise CardCancelled('ECDL experiment cancelled; outputs held')
        if self.clock() - self.started >= self.p['timeout_s']:
            raise DeviceError('ECDL experiment deadline; outputs held')
        if self.ports['loop'].is_open_loop() is not True:
            raise DeviceError('ECDL experiment requires open loop')
        for name, axis in self.axes.items():
            if axis.policy != self.policies[name]:
                raise DeviceError('Axis calibration or safety policy changed: ' + name)
            if getattr(axis, 'uncertain_output', False):
                raise DeviceError('Uncertain axis output: ' + name)

    def wait(self, duration):
        end = self.clock() + duration
        while self.clock() < end:
            self.check()
            self.sleep(max(0., min(.5, end - self.clock())))
        self.check()

    def between_points(self):
        if self.runtime is not None:
            self.runtime.poll(boundary='between_points')
        self.check()

    def read(self, stage, *, require_frequency=None):
        began = self.clock()
        frequency_required = self.require_frequency if require_frequency is None else require_frequency
        while True:
            self.check()
            try:
                source = self.ports['observation']
                reader = getattr(source, 'read_mode_with_patterns', source.read_mode)
                observation = reader()
                sample = observation.frequency
                # v5.1-hostcheck (human decision 2026-10-22): wavemeter quality/status/aligned labels are informational; the source clock is used for age only when trusted+aligned.
                source_clock = sample.acquisition_known and sample.acquired_unix_s is not None
                stamp = sample.acquired_unix_s if source_clock else sample.acquired_s
                now = time.time() if source_clock else self.clock()
                if (sample.unit != 'Hz'
                        or not finite(stamp) or not 0 <= now - stamp <= self.p['max_sample_age_s']
                        or self.last_stamp is not None and stamp <= self.last_stamp):
                    raise MeasurementUnavailable('Fresh frequency and optical pattern required')
                frequency_valid = finite(sample.frequency_hz) and sample.frequency_hz > 0
                if frequency_required and not frequency_valid:
                    raise MeasurementUnavailable('Fresh valid optical frequency required')
                classification = (observation.fringe_analysis or {}).get('classification_revision')
                if classification is not None:
                    if self.classification_revision is None:
                        self.classification_revision = classification
                    elif classification != self.classification_revision:
                        raise DeviceError('Optical classification configuration changed during experiment')
                outputs = {name: axis.readback() for name, axis in self.axes.items()}
                for name, value in outputs.items():
                    if not finite(value) or abs(value - self.expected[name]) > max(self.policies[name].resolution, 1e-8):
                        raise DeviceError('Axis changed outside experiment: ' + name)
                self.check()
                now = time.time() if sample.acquired_unix_s is not None else self.clock()
                if not 0 <= now - stamp <= self.p['max_sample_age_s']:
                    raise MeasurementUnavailable('Optical observation expired during axis readback')
                self.last_stamp = stamp
                row = dict(stage=stage, outputs=outputs,
                           frequency_hz=sample.frequency_hz if frequency_valid else None,
                           wavelength_nm=299792458e9 / sample.frequency_hz if frequency_valid else None,
                           observation=asdict(observation), acquired_s=stamp,
                           acquired_unix_s=stamp if source_clock else None,
                           acquisition_clock='unix' if source_clock else 'monotonic',
                           recorded_unix_s=time.time(),
                           quality=sample.quality, single_mode=observation.single_mode,
                           mode_id=observation.mode_id, metrics=copy.deepcopy(observation.metrics),
                           fringe_analysis=copy.deepcopy(observation.fringe_analysis or {}),
                           settings_revision=getattr(self.runtime, 'revision', 0))
                if isinstance(row['observation'].get('fringe_analysis'), dict):
                    row['observation']['fringe_analysis'].pop('raw_patterns', None)
                # Keep full raw frames at map/trial points; incidental gating
                # reads emit compact evidence rather than duplicating pixels.
                self.emit('ecdl_map_observation', compact_row(row))
                if self.observation_observer is not None:
                    self.observation_observer(row)
                return row
            except (MeasurementUnavailable, OSError) as error:
                if any(getattr(a, 'uncertain_output', False) for a in self.axes.values()):
                    raise
                if self.clock() - began >= self.p['measurement_timeout_s']:
                    raise DeviceError('ECDL experiment measurement deadline; outputs held') from error
                self.emit('measurement_rejected', dict(stage=stage, reason=str(error)))
                self.wait(self.p['sample_interval_s'])

    def move(self, name, target, stage, *, settle_s=None):
        """Bounded owner steps to ``target``. ``settle_s`` replaces the axis policy settle wait after each
        write (a card's explicit pacing choice, e.g. tune.ecdl_operating_point approach_settle_s); the owner's
        update interval, step and slew limits are never relaxed here."""
        axis, policy = self.axes[name], self.policies[name]
        if policy.resolution:
            target = round(target / policy.resolution) * policy.resolution
        if not finite(target) or not policy.low <= target <= policy.high:
            raise DeviceError('Experiment destination outside approved interval')
        deferred = None
        while abs(self.expected[name] - target) > max(policy.resolution / 2, 1e-10):
            self.check()
            self.wait(max(0., self.last_write[name] + policy.update_interval_s - self.clock()))
            self.read('before ' + stage, require_frequency=True if name != 'current' else None)
            maximum = min(policy.max_step, policy.max_slew_per_s * policy.update_interval_s)
            delta = max(-maximum, min(maximum, target - self.expected[name]))
            if policy.resolution:
                delta = math.copysign(math.floor((abs(delta) + 1e-12) / policy.resolution) * policy.resolution, delta)
            if not delta:
                raise DeviceError('Experiment step is below axis resolution')
            value = self.expected[name] + delta
            self.check()
            try:
                axis.move(value, policy.update_interval_s)
            except MeasurementUnavailable as error:
                # Only an explicit owner-confirmed prewrite failure may defer.
                if getattr(axis, 'uncertain_output', None) is not False:
                    raise
                if deferred is None:
                    deferred = self.clock()
                if self.clock() - deferred >= self.p['measurement_timeout_s']:
                    raise DeviceError('ECDL experiment command deferral deadline') from error
                self.emit('command_deferred', dict(interface=name, reason=str(error)))
                self.wait(self.p['sample_interval_s'])
                continue
            deferred = None
            self.expected[name] = value
            self.last_write[name] = self.clock()
            command = dict(interface=name, value=value, elapsed_s=self.clock() - self.started,
                           stage=stage, update_interval_s=policy.update_interval_s)
            self.commands.append(command)
            self.emit('command_applied', command)
            self.wait(policy.settle_s if settle_s is None else settle_s)

    def current_recovery(self, active):
        method = getattr(self.axes['current'], 'begin_recovery' if active else 'end_recovery', None)
        if method is not None:
            began = self.clock()
            while True:
                self.check()
                try:
                    method()
                    return
                except MeasurementUnavailable as error:
                    if getattr(self.axes['current'], 'uncertain_output', None) is not False:
                        raise
                    if self.clock() - began >= self.p['measurement_timeout_s']:
                        raise DeviceError('Current recovery state transition measurement deadline') from error
                    self.emit('command_deferred', dict(interface='current',
                        stage='begin recovery' if active else 'end recovery', reason=str(error)))
                    self.wait(self.p['sample_interval_s'])


def branch_summary(points, p, current_span):
    """Segment observed paths and rank contiguous qualified windows.

    Source single_mode is archived, but only phase-free calibrated patterns are
    eligible for automatic choice. Unknown classifiers remain reviewable data.
    """
    groups, group, previous = [], [], None
    for index, point in enumerate(points):
        if not finite(point['frequency_hz']):
            if group:
                groups.append(group)
            group, previous = [], None
            continue
        if previous is not None and abs(point['frequency_hz'] - previous['frequency_hz']) > p['discontinuity_hz']:
            groups.append(group)
            group = []
        group.append(index)
        previous = point
    if group:
        groups.append(group)
    branches = []
    for number, indices in enumerate(groups):
        clean_runs, clean = [], []
        for index in indices:
            point = points[index]
            analysis = point['fringe_analysis']
            m = point['metrics']
            metrics = ('side_peak_prominence_max', 'side_peak_height_max', 'main_fwhm_fraction',
                       'near_main_peak_prominence_max')
            valid = (point['single_mode'] and not point['observation']['maintenance_required']
                     and analysis.get('qualified') is True and analysis.get('period_qualified') is True
                     and bool(analysis.get('raw_patterns'))
                     and str(analysis.get('revision', '')).startswith('full-period-free-position-')
                     and all(finite(m.get(k)) for k in metrics))
            if valid:
                clean.append(index)
            elif clean:
                clean_runs.append(clean)
                clean = []
        if clean:
            clean_runs.append(clean)
        windows = []
        for run in clean_runs:
            if len(run) < p['min_branch_points']:
                continue
            low, high = sorted((points[run[0]]['current'], points[run[-1]]['current']))
            mid = (low + high) / 2
            reference = min(run, key=lambda i: abs(points[i]['current'] - mid))
            distance = min(abs(points[i]['frequency_hz'] - p['target_frequency_hz']) for i in run)
            quality = sum(sum(points[i]['metrics'][key] for key in metrics) for i in run) / len(run)
            weights = p['ranking_weights']
            score = (weights['distance'] * distance / p['capture_hz']
                     - weights['width'] * (high - low) / current_span + weights['quality'] * quality)
            windows.append(dict(point_indices=run, current_low=low, current_high=high,
                                sampled_width=high - low, reference_index=reference,
                                target_distance_hz=distance, quality_penalty=quality, score=score,
                                boundaries_verified=False))
        best = min(windows, key=lambda w: w['score']) if windows else None
        branches.append(dict(id='branch-' + str(number + 1), point_indices=indices,
                             frequency_bounds_hz=[min(points[i]['frequency_hz'] for i in indices), max(points[i]['frequency_hz'] for i in indices)],
                             current_bounds=[min(points[i]['current'] for i in indices), max(points[i]['current'] for i in indices)],
                             observed_mode_labels=sorted({points[i]['mode_id'] for i in indices if points[i]['mode_id']}),
                             qualified_windows=windows, best_window=best,
                             eligible=bool(best and best['target_distance_hz'] <= p['capture_hz']),
                             hop_cause='unknown', physical_mode_identity_verified=False))
    ranked = sorted((b for b in branches if b['eligible']), key=lambda b: b['best_window']['score'])
    return branches, [b['id'] for b in ranked]


def selected_reference(artifact, branch_id=None):
    mapping = checked_artifact(artifact, SCHEMA)
    if mapping.get('status', 'complete') != 'complete':
        raise ValueError('An incomplete branch map cannot supply a recovery reference')
    selected = branch_id or mapping['selection']['branch_id']
    branch = next((b for b in mapping['branches'] if b['id'] == selected), None)
    if branch is None or not branch['eligible'] or not branch['best_window']:
        raise ValueError('A qualified mapped branch must be selected')
    return mapping, branch, mapping['points'][branch['best_window']['reference_index']]


def run_mode_map(ports, p, *, publish=None, context=None, **arguments):
    validated = parameters(dict(parameters=p), {})
    p.clear()
    p.update(validated)
    if any(p[k] is None for k in ('target_frequency_hz', 'current_start_fraction',
                                  'current_end_fraction', 'discontinuity_hz', 'capture_hz')):
        raise ValueError('Map target, endpoints, discontinuity and capture range required')
    experiment = BoundedExperiment(ports, p, **arguments)
    experiment.require_frequency = False
    if experiment.runtime is not None:
        experiment.runtime.poll(boundary='before_scan')
    acquisition_parameters = copy.deepcopy(p)
    policy = experiment.policies['current']
    start = policy.low + p['current_start_fraction'] * policy.span
    end = policy.low + p['current_end_fraction'] * policy.span
    count = math.ceil(abs(end - start) / (p['current_step_fraction'] * policy.span))
    path = [start + (end - start) * i / count for i in range(count + 1)]
    if policy.resolution and any(abs(b - a) < policy.resolution for a, b in zip(path, path[1:])):
        raise ValueError('Map step below axis resolution')
    initial = experiment.read('initial map context')
    origin = copy.deepcopy(experiment.expected)
    points = []
    def snapshot(status, **extra):
        artifact = seal(dict(schema=SCHEMA, status=status, context=copy.deepcopy(context or {}),
            started_unix_s=experiment.started_unix_s, recorded_unix_s=time.time(),
            elapsed_s=experiment.clock()-experiment.started,
            lock_admission=False, branch_identity='observed continuous path; not physical mode proof',
            parameters=copy.deepcopy(p), acquisition_parameters=acquisition_parameters, initial=initial,
            policies={name: asdict(value) for name, value in experiment.policies.items()},
            held_context={name: origin[name] for name in ('temperature', 'piezo')},
            conditioning_fractions=copy.deepcopy(p['conditioning_fractions']),
            path=path, commands=copy.deepcopy(experiment.commands), points=copy.deepcopy(points),
            final_outputs=dict(experiment.expected),
            settings_revision=getattr(experiment.runtime, 'revision', 0), **extra))
        if publish is not None:
            publish(artifact)
        return artifact

    snapshot('incomplete')
    experiment.current_recovery(True)
    # A failed or uncertain command intentionally skips cleanup writes. The
    # owner/worker retains responsibility for quarantine and lease handling.
    for fraction in p['conditioning_fractions']:
        experiment.move('current', policy.low + fraction * policy.span, 'conditioning')
    experiment.move('current', start, 'preparation')
    for index, target in enumerate(path):
        experiment.between_points()
        experiment.move('current', target, 'directed map')
        dwell = max(p['dwell_s'], policy.settle_s)
        experiment.wait(dwell)
        row = experiment.read('directed map point')
        row.update(index=index, current=row['outputs']['current'],
                   direction='up' if end > start else 'down', dwell_s=dwell)
        points.append(row)
        experiment.emit('ecdl_mode_map_point', compact_row(row))
        snapshot('incomplete')
    if finite(points[-1]['frequency_hz']):
        experiment.current_recovery(False)
    if experiment.runtime is not None:
        experiment.runtime.poll(boundary='before_selection')
    branches, ranked = branch_summary(points, p, policy.span)
    from ldcon.jobs.fringe_sweep_reference import analyze_sweep
    relative_context = analyze_sweep(points, discontinuity_hz=p['discontinuity_hz'])
    selected = p['selected_branch_id'] or (ranked[0] if ranked else None)
    # Rule K (2026-10-27): an explicit selection that is absent or not optically qualified is recorded as
    # unqualified and the complete map is still published; plan.ecdl_recovery (selected_reference) refuses it.
    qualified = selected is None or selected in ranked
    unqualified = None if qualified else dict(branch_id=selected, reason=(
        'not optically qualified' if any(b['id'] == selected for b in branches) else 'absent from the map'))
    artifact = snapshot('complete', branches=branches, ranked_branch_ids=ranked,
        relative_pattern_context=relative_context,
        selection=dict(branch_id=selected, author=p['selection_author'], qualified=qualified, unqualified=unqualified,
                       qualification='source-qualified phase-free pattern; not physical mode proof'),
        raw_pattern_available=all(bool(x['fringe_analysis'].get('raw_patterns')) for x in points),
        hop_cause='unknown', universality_verified=False)
    experiment.emit('ecdl_mode_map_artifact', dict(schema=SCHEMA,
        artifact_sha256=artifact['artifact_sha256'], points=len(points), branches=len(branches),
        selected_branch_id=selected, selection_qualified=qualified, unqualified=unqualified,
        raw_pattern_available=artifact['raw_pattern_available'], settings_revision=artifact['settings_revision']))
    return dict(status='succeeded', selected_branch_id=selected, selection_required=not qualified or selected is None,
                selection_qualified=qualified, unqualified=unqualified,
                lock_enabled=False, artifact=artifact, outputs=dict(experiment.expected))
