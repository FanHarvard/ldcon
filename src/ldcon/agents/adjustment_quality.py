"""Evidence-bound operational quality candidates; this module never saves settings.

An owner's measured result is authoritative about acquisition validation. Model
assessments remain labelled as assessments, and old classification thresholds and
raw results are preserved even when a more permissive candidate is proposed.

Revisable thresholds (2026-10-28): the optical ``single_mode_max`` scalar leaves
and the lock device's dither quality limit
``steady_lock.window.warning_response`` (the monitor ``dither_response``
condition and the dashboard quality line). The dither limit is judged against
the same repeated post-settle dither fits that the evidence rules already
require, aggregated as their mean (``dither_aggregate``; 2026-10-28, a single
32 s fit scatters by about 15-20 % window to window, and the owner trim guard
and the monitor's 300 s persistence aggregate too); the optical leaves keep the
maximum rule. ``window.edge_response`` is a window-search experiment boundary and is
never a quality threshold here; safety keys are never in scope.
"""
import copy
import json
import math
import statistics
import time

from experiment_core.records import atomic_json, digest
from ldcon.agents.agent_configuration import QUALITY_MAPS, QUALITY_METRICS, DITHER_QUALITY_LEAF

REVISION = 'unified-quality-2'
FIELDS = frozenset(('selected_current', 'measurement_ids', 'classification',
                    'margin_fraction', 'history_assessment', 'rationale',
                    'follow_up', 'changes'))
# Owner result states that are receipts of no output write (2026-10-28):
# ``stale_decision`` is returned before ``_start`` runs; ``prewrite_rejected`` is
# returned before any move or when the owner reports ``applied`` is not true.
# ``rejected`` is excluded because a late validation error can follow a move.
NO_WRITE_STATES = frozenset(('prewrite_rejected', 'stale_decision'))
HISTORY_FIELDS = frozenset(('evidence_refs', 'interval_unix_s', 'sample_coverage',
    'comparability', 'drift_and_interventions', 'practical_requirement',
    'unmeasured_alternatives'))


DITHER_SUFFIX = '.' + '.'.join(DITHER_QUALITY_LEAF)
DITHER_AGGREGATION = 'mean_of_valid_fits'
DITHER_RULE = 'mean of valid post-settle dither fits at the selected point <= limit*(1-margin_fraction)'


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def no_write(row):
    """An owner receipt that itself records no measurement and no applied move."""
    receipt = row.get('receipt')
    return (not any(k in row for k in ('optical_frames', 'target', 'adoption', 'restoration'))
            and (receipt is None or (isinstance(receipt, dict) and receipt.get('applied') is not True)))


def is_dither(path):
    return path.endswith(DITHER_SUFFIX)


def dither_aggregate(fits):
    """Mean rule for the dither quality limit: n, mean, max, min and sample std of valid fits."""
    values = [fit['value'] for fit in fits]
    return dict(aggregation=DITHER_AGGREGATION, rule=DITHER_RULE, n=len(values),
        mean=statistics.fmean(values), max=max(values), min=min(values),
        std=statistics.stdev(values) if len(values) > 1 else None,
        std_kind='sample standard deviation (n-1)', fits=copy.deepcopy(fits))


def dither_meets(aggregate, limit, margin):
    return aggregate['mean'] <= limit*(1-margin)


def threshold_sources(root, laser_id, paths, *, limit_bytes=4 << 20):
    """Newest audited configuration save of each path; explicit when none is recorded."""
    from ldcon.core.agent_authority import approval_root
    note = ('No audited configuration save in the scanned ledger set this value; it may predate the audit '
            'or have been edited outside the dashboard. The date is unknown, not inferred.')
    result = {path: dict(status='not_recorded', note=note) for path in paths}
    if root is None or not paths:
        return {path: dict(status='unavailable', note='No host ledger root in this context') for path in paths}
    try:
        with (approval_root(root, laser_id)/'audit.jsonl').open('rb') as stream:
            stream.seek(0, 2); size = stream.tell(); stream.seek(max(0, size-limit_bytes))
            raw = stream.read()
    except (OSError, ValueError) as error:
        return {path: dict(status='unavailable', note='Configuration audit unreadable: '+type(error).__name__) for path in paths}
    missing = set(paths)
    for line in reversed(raw.splitlines()):
        if not missing:
            break
        try:
            record = json.loads(line)
        except (UnicodeError, ValueError):
            continue
        if not isinstance(record, dict) or record.get('event') != 'configuration_saved':
            continue
        for row in record.get('changes') or []:
            if isinstance(row, dict) and row.get('path') in missing:
                stamp = record.get('unix_s')
                result[row['path']] = dict(status='recorded', unix_s=stamp,
                    utc=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(stamp)) if finite(stamp) else None,
                    actor=record.get('actor'), old=row.get('old'), new=row.get('new'),
                    authority=record.get('authority'), source='host configuration audit (configuration_saved)')
                missing.discard(row['path'])
    if size > limit_bytes:
        for path in missing:
            result[path]['note'] += ' Only the newest {} bytes of the audit were scanned.'.format(limit_bytes)
    return result


def thresholds(document):
    result = {}
    devices = document.get('devices', {})
    lock_device = document.get('interfaces', {}).get('lock', {}).get('device')
    if lock_device:
        devices = {lock_device:devices.get(lock_device, {})}
    for device, value in devices.items():
        optical = value.get('control_settings', {}).get('optical', {})
        for category in QUALITY_MAPS:
            for metric, limit in optical.get(category, {}).items():
                if metric in QUALITY_METRICS and finite(limit) and limit >= 0:
                    result[f'devices.{device}.control_settings.optical.{category}.{metric}'] = limit
        node = value
        for key in DITHER_QUALITY_LEAF:
            node = node.get(key, {}) if isinstance(node, dict) else None
        if finite(node) and node > 0:
            result[f'devices.{device}{DITHER_SUFFIX}'] = node
    return result


class QualityReview:
    def __init__(self, directory, document, *, persist=True, root=None):
        self.directory = directory
        self.persist = persist
        self.root = root
        self.laser_id = document.get('id')
        self.old = thresholds(document)
        self._sources = None
        self.source_revision = digest(document)
        self.measurements = {}
        self.no_write_rows = []
        self.outcome = None

    def save(self, name, value):
        if self.persist:
            atomic_json(self.directory/name, value)

    def observe(self, snapshot):
        row = snapshot.get('latest_result') or {}
        if row.get('state') in NO_WRITE_STATES and row.get('id') and no_write(row):
            if row not in self.no_write_rows:
                self.no_write_rows.append(copy.deepcopy(row))
            return
        if row.get('state') not in ('measured', 'adopted') or not row.get('id'):
            return
        record = dict(result=copy.deepcopy(row), controller_job_id=snapshot.get('controller_job_id'),
                      segment=snapshot.get('status', {}).get('segment', 0))
        previous = self.measurements.get(row['id'])
        if previous is not None:
            if previous['result'] != record['result'] or previous['controller_job_id'] != record['controller_job_id']:
                raise ValueError('Owner measurement identity changed; quality evidence is inconsistent')
            return
        self.measurements[row['id']] = record
        self.save('adjustment-quality-evidence.json', self.measurements)

    def context(self):
        from ldcon.jobs.quality_baseline import safe_read
        baseline = safe_read(self.directory.parent.parent/'quality-baseline.json') if self.directory.parent.name == 'jobs' else {}
        return dict(policy_revision=REVISION,
            last_accepted_adjustment_baseline=baseline,
            phase='reviewed_candidate' if self.outcome else 'restore_existing_quality_then_explore',
            supported_action='record_quality_outcome', application='host_save_after_verified_adoption',
            old_thresholds=self.old, source_snapshot_revision=self.source_revision,
            source_scope='task laser.yaml snapshot; not a live configuration CAS revision',
            measurement_ids=list(self.measurements), outcome=self.outcome,
            revisable_thresholds=sorted(self.old),
            revisable_scope='optical single_mode_max scalar leaves and the lock device steady_lock.window.warning_response '
                '(dither quality limit); window.edge_response (window-search boundary) and safety keys are outside this review',
            dither_quality=self.dither_context(),
            required_verification='Two separate owner-accepted post-settle windows at the selected current, in the current segment; adoption measures again.',
            historical_context='adjustment-input-1.json recorded_context and supplied knowledge; cite evidence and coverage, mark missing history explicitly')

    def dither_context(self):
        """Current dither limit, its recorded source/date and the dither fits measured in this task."""
        paths = [p for p in self.old if is_dither(p)]
        if not paths:
            return dict(status='not_configured', note='No positive steady_lock.window.warning_response on the lock device')
        if self._sources is None:
            self._sources = threshold_sources(self.root, self.laser_id, sorted(self.old)) if self.laser_id else {}
        path = paths[0]; limit = self.old[path]
        rows, unit = [], None
        for identifier, record in self.measurements.items():
            row = record['result']; fit = row.get('dither') or {}
            unit = unit or fit.get('dither_response_unit')
            if fit.get('dither_response_valid') is True and finite(fit.get('dither_response')):
                rows.append(dict(id=identifier, target=row.get('target'), segment=record['segment'],
                    value=fit['dither_response'], source_start_unix_s=fit.get('dither_source_start_unix_s'),
                    source_end_unix_s=fit.get('dither_source_unix_s')))
        values = sorted(r['value'] for r in rows)
        distribution = dict(count=len(values),
            source='valid post-settle dither fits of owner-accepted results in this task (all points, all segments)')
        if values:
            distribution.update(min=values[0], median=statistics.median(values), max=values[-1],
                above_current_limit=sum(v > limit for v in values), samples=rows[-20:])
        return dict(path=path, current_limit=limit, unit=unit or 'owner dither_response_unit (nm per current-axis unit)',
            source=self._sources.get(path, dict(status='unavailable')), recent_measured=distribution,
            monitor_binding='agent.monitor follow_quality_thresholds dither_response condition and the dashboard quality line read this saved value',
            excluded='window.edge_response is the window-search experiment boundary, not this quality limit',
            telemetry='full series: VictoriaMetrics ldcon_dither_response / ldcon_dither_warning_threshold (kind=owner_sample)')

    def _verification(self, identifiers, current, snapshot):
        if (not isinstance(identifiers, list) or len(identifiers) < 2 or
                any(not isinstance(i, str) for i in identifiers) or
                len(set(identifiers)) != len(identifiers)):
            raise ValueError('Quality review requires at least two distinct measured result IDs')
        if not finite(current):
            raise ValueError('A finite selected current is required')
        latest = snapshot.get('latest_result') or {}
        if latest.get('id') not in identifiers:
            raise ValueError('Repeat the selected point with current owner evidence before quality review')
        expected = (snapshot.get('controller_job_id'), snapshot.get('status', {}).get('segment', 0))
        windows, contexts, samples, dither_fits, temperatures = [], set(), [], [], []
        for identifier in identifiers:
            record = self.measurements.get(identifier)
            if not record or (record['controller_job_id'], record['segment']) != expected:
                raise ValueError('Quality measurements must belong to the current controller and segment')
            row = record['result']
            if not finite(row.get('target')) or abs(row['target']-current) > 1e-9:
                raise ValueError('Repeat measurements must be at the selected current')
            frames, settled = row.get('optical_frames'), row.get('post_settle_unix_s')
            if not isinstance(frames, list) or len(frames) < 5 or not finite(settled):
                raise ValueError('Complete owner post-settle measurement frames are required')
            times = []
            for frame in frames:
                observation = frame.get('observation') or {}
                timestamp = (observation.get('frequency') or {}).get('acquired_unix_s')
                owner = frame.get('owner_context') or {}
                temperature = (frame.get('outputs') or {}).get('temperature')
                if (not finite(timestamp) or timestamp <= settled or
                        frame.get('native_pi_active') is not True or
                        not observation.get('mode_id') or not finite(temperature) or
                        not all(owner.get(k) for k in ('system_id', 'generation', 'policy_revision'))):
                    raise ValueError('Verified owner, branch and post-settle source times are required')
                times.append(timestamp)
                contexts.add(digest([owner, observation['mode_id'], frame.get('target_frequency_hz')]))
                temperatures.append(temperature)
                samples.append(observation.get('metrics') or {})
            if times != sorted(set(times)):
                raise ValueError('Repeated source acquisitions do not verify stability')
            fit = row.get('dither') or {}
            start, end = fit.get('dither_source_start_unix_s'), fit.get('dither_source_unix_s')
            if (fit.get('dither_response_valid') is not True or not finite(fit.get('dither_response'))
                    or not finite(start) or not finite(end) or not settled < start <= end):
                raise ValueError('A valid wholly post-settle dither window is required')
            dither_fits.append(dict(id=identifier, value=fit['dither_response'],
                source_start_unix_s=start, source_end_unix_s=end))
            windows.append((min(times[0], start), max(times[-1], end)))
        windows.sort()
        if len(contexts) != 1 or any(a[1] >= b[0] for a, b in zip(windows, windows[1:])):
            raise ValueError('Separate same-context windows are required; overlapping or changed-branch evidence is invalid')
        metrics = {}
        for metric in QUALITY_METRICS:
            values = [sample.get(metric) for sample in samples]
            if all(finite(v) for v in values):
                metrics[metric] = dict(min=min(values), max=max(values))
        return dict(measurement_ids=identifiers, source_windows_unix_s=windows,
            metric_ranges=metrics, dither_range=[min(f['value'] for f in dither_fits), max(f['value'] for f in dither_fits)],
            dither_aggregate=dither_aggregate(dither_fits),
            temperature_range=[min(temperatures), max(temperatures)],
            observation='repeated same-context samples; no long-term stability or physical single-mode guarantee')

    def record(self, parameters, snapshot):
        if set(parameters) != FIELDS:
            raise ValueError('Quality outcome fields are incomplete or unsupported')
        classification = parameters['classification']
        if classification not in ('adequate', 'restored_adequate', 'best_practical'):
            raise ValueError('Quality classification must preserve an unmet historical criterion')
        margin = parameters['margin_fraction']
        if not finite(margin) or not 0 < margin < 1:
            raise ValueError('Specify a positive practical margin fraction below one')
        for key in ('rationale', 'follow_up'):
            if not isinstance(parameters[key], str) or not parameters[key].strip():
                raise ValueError('Quality rationale and follow-up are required')
        history = parameters['history_assessment']
        if not isinstance(history, dict) or set(history) != HISTORY_FIELDS:
            raise ValueError('History interval, coverage, comparability and practical requirement are required')
        refs, interval = history['evidence_refs'], history['interval_unix_s']
        if (not isinstance(refs, list) or not refs or any(not isinstance(r, str) or not r.strip() for r in refs)
                or not isinstance(interval, list) or len(interval) != 2
                or not all(finite(t) for t in interval) or interval[0] >= interval[1]
                or any(not isinstance(history[k], str) or not history[k].strip()
                       for k in HISTORY_FIELDS-{'evidence_refs', 'interval_unix_s'})):
            raise ValueError('History evidence and its limitations must be explicit')
        changes = parameters['changes']
        if (not isinstance(changes, dict) or len(changes) > 8 or
                any(k not in self.old or not finite(v) or v < 0 or (is_dither(k) and v <= 0)
                    for k, v in changes.items())):
            raise ValueError('Only existing recognized optical classification or dither quality scalar candidates are supported')
        verification = self._verification(parameters['measurement_ids'], parameters['selected_current'], snapshot)
        new = {**self.old, **changes}
        comparisons = {}
        for path, limit in self.old.items():
            if is_dither(path):
                # The dither limit is compared with the mean of the valid fits
                # (max and std are reported, not compared).
                aggregate = verification['dither_aggregate']
                observed = aggregate['mean']
                comparisons[path] = dict(observed_max=aggregate['max'], observed_mean=observed,
                    aggregation=DITHER_AGGREGATION, rule=DITHER_RULE, n=aggregate['n'],
                    mean=aggregate['mean'], max=aggregate['max'], std=aggregate['std'])
            else:
                values = verification['metric_ranges'].get(path.rsplit('.', 1)[-1])
                observed = values['max'] if values else None
                comparisons[path] = dict(observed_max=observed, aggregation='max_of_frames')
            comparisons[path].update(old_limit=limit, proposed_limit=new[path],
                meets_old=None if observed is None else observed <= limit,
                meets_old_with_margin=None if observed is None else observed <= limit*(1-margin),
                meets_proposed=None if observed is None else observed <= new[path])
        primary = [row for path, row in comparisons.items() if '.single_mode_max.' in path]
        adequate = bool(primary) and all(row['meets_old_with_margin'] is True for row in primary)
        # A changed threshold whose old value is unmet is a reclassification,
        # never a restored or physically improved quality.
        reclassified = sorted(p for p in changes if comparisons[p]['meets_old_with_margin'] is not True)
        if classification in ('adequate', 'restored_adequate') and (not adequate or reclassified):
            raise ValueError('Existing quality with margin is unverified or unmet; do not relabel degradation as restored')
        result = dict(schema_version=1, policy_revision=REVISION,
            source_snapshot_revision=self.source_revision, old_criterion_revision=digest(self.old),
            proposed_criterion_revision=digest(new), old_thresholds=self.old, proposed_thresholds=new,
            changes=changes, classification=classification, selected_current=parameters['selected_current'],
            historical_quality_with_margin=adequate, comparisons=comparisons, verification=verification,
            reclassified_paths=reclassified, physical_improvement_claimed=False,
            reclassification_note='A revised limit changes the operating criterion only; old values and raw evidence are kept.',
            margin_fraction=margin, history_assessment=copy.deepcopy(history),
            history_validation='agent assessment; cited external history has not been independently verified by this host',
            rationale=parameters['rationale'], follow_up=parameters['follow_up'],
            decision_revision=snapshot.get('status', {}).get('decision_revision'),
            no_write_rows_at_record=len(self.no_write_rows),
            application_status='pending_point_adoption' if changes else 'thresholds_unchanged',
            applied=False, physical_single_mode_guarantee=False,
            pending_reason='Host applies requested changes only after verified point adoption and fresh scoped CAS/admission checks; saved/queued is not owner-effective application. Unknown writes are not retried.')
        self.outcome = result
        self.save('adjustment-quality-outcome.json', result)
        return result

    def require_adoption(self, value, snapshot):
        """The recorded outcome must still describe the owner evidence being adopted.

        The owner advances ``decision_revision`` as ``digest([previous, row])`` for
        every command result row (``AdaptiveWindowSession._result``) and by other
        digests for revocation, interruption, termination and replan. An outcome
        stays current only if the present revision is reproduced exactly from the
        recorded one by chaining observed no-write receipts (``NO_WRITE_STATES``),
        so any measured/adopted/other row or session event in between refuses.
        """
        if not self.outcome or self.outcome['selected_current'] != value:
            raise ValueError('Record a current quality outcome after repeated measurements before adoption')
        current = snapshot.get('status', {}).get('decision_revision')
        revision = self.outcome['decision_revision']
        if revision == current:
            return
        chained = []
        for row in self.no_write_rows[self.outcome.get('no_write_rows_at_record', 0):]:
            revision = digest([revision, row])
            chained.append(row)
            if isinstance(current, str) and revision == current:
                self.outcome['revision_advanced_by_no_write'] = [dict(id=r['id'], state=r['state'],
                    action=(r.get('command') or {}).get('action'), reason=r.get('reason')) for r in chained]
                self.save('adjustment-quality-outcome.json', self.outcome)
                return
        raise ValueError('Owner evidence changed after the quality outcome (a new measured, adopted or '
                         'session result, not only a no-write rejection); record a current quality outcome '
                         'after repeated measurements before adoption')

    def finalize(self, snapshot):
        """Keep physical adoption and the final point's observed quality separate."""
        row = snapshot.get('latest_result') or {}
        if not self.outcome or row.get('state') != 'adopted':
            return
        self.outcome['adoption_result_id'] = row.get('id')
        try:
            verification = self._verification(
                self.outcome['verification']['measurement_ids']+[row['id']],
                self.outcome['selected_current'], snapshot)
            primary = [(path, limit) for path, limit in self.old.items() if '.single_mode_max.' in path]
            ranges = verification['metric_ranges']
            self.outcome['post_adoption_meets_old_with_margin'] = bool(primary) and all(
                path.rsplit('.', 1)[-1] in ranges and
                ranges[path.rsplit('.', 1)[-1]]['max'] <= limit*(1-self.outcome['margin_fraction'])
                for path, limit in primary)
            dither = [limit for path, limit in self.old.items() if is_dither(path)]
            if dither:
                self.outcome['post_adoption_dither_meets_old_with_margin'] = dither_meets(
                    verification['dither_aggregate'], dither[0], self.outcome['margin_fraction'])
            self.outcome['post_adoption_verification'] = verification
        except (ValueError, KeyError) as error:
            self.outcome['post_adoption_meets_old_with_margin'] = None
            self.outcome['post_adoption_verification'] = dict(status='unverified', reason=str(error))
        self.save('adjustment-quality-outcome.json', self.outcome)
