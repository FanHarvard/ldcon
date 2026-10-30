"""Narrow host dispatch for an adopted resident current-card quality outcome.

Uses the existing configuration authorization/CAS and safe-boundary owner reload.
No model tools, physical writes, altered guards or automatic mutation retries.
"""
import copy
import time
from types import SimpleNamespace

import yaml

from experiment_core.records import atomic_json, digest, safe_id
from ldcon.core.records import read_json
from ldcon.agents.adjustment_quality import (FIELDS, QualityReview, dither_aggregate, dither_meets, finite,
    is_dither, thresholds)


def _covered(path, applied):
    """The owner reports a steady-profile block (control_settings.steady_lock.window) for a leaf it adopted."""
    return any(path == a or path.startswith(a+'.') for a in applied)


def _journal(runtime, job_id, request_id):
    return runtime.root/'agents'/'quality-applies'/(safe_id(job_id)+'-'+safe_id(request_id)+'.json')


def status(runtime, job_id, request_id):
    path = _journal(runtime, job_id, request_id)
    record = read_json(path, {})
    if not record:
        return dict(status='not_submitted', applied=False)
    saved = record.get('save_receipt') or {}
    controller = record['request']['controller_job_id']
    queued = (saved.get('live', {}).get('jobs', {}).get(controller) or {})
    result = read_json(runtime.directory(controller)/('laser-reload-result-'+safe_id(queued['request_id'])+'.json'), {}) if queued.get('request_id') else {}
    live = read_json(runtime.directory(controller)/'laser-live.json', {})
    paths = set(record.get('changes', {}))
    owner = (result.get('details') or {}).get('owner') or {}
    confirmed = bool(paths and result.get('status') == 'applied'
        and result.get('revision_after') == saved.get('revision') == live.get('revision')
        and all(_covered(p, result.get('applied', [])) for p in paths) and not result.get('deferred')
        and owner.get('revision') and owner.get('revision') == owner.get('saved_revision'))
    return dict(status='applied' if confirmed else 'saved_pending_owner' if saved else 'unknown_no_retry',
        applied=confirmed, saved=bool(saved), request_id=request_id,
        revision=saved.get('revision'), owner_effective_revision=owner.get('revision') if confirmed else None,
        save_receipt=saved, owner_receipt=result or None, live_status=live,
        reason=record.get('error') or ('Confirmed all changed paths at the owner' if confirmed else 'A saved/queued revision is not confirmed owner application'))


def apply(runtime, job_id, value, *, owner_read=None, clock=time.time):
    from ldcon.web.agent_task_api import admission_path
    from ldcon.agents.agent_background import route, control_root
    from ldcon.agents.agent_configuration import reviewed_quality_change, changes
    from ldcon.jobs.adaptive_window_session import AdaptiveExperimentManager
    from ldcon.core.interface_schema import load_mapping, digest as configuration_digest
    from ldcon.core import configuration
    if owner_read is None:
        from ldcon.agents.agent_owner_status import owner_read
    fields = {'request_id', 'expected_revision', 'outcome_revision', 'controller_job_id', 'adoption_id'}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError('Expected an exact adopted-point quality request with configuration CAS')
    for field in ('request_id', 'controller_job_id', 'adoption_id'):
        safe_id(value[field])
    path = _journal(runtime, job_id, value['request_id'])
    with runtime.mutex:
        previous = read_json(path, {})
        if previous:
            if previous.get('request') != value:
                raise ValueError('Quality request ID is bound to another candidate')
            return status(runtime, job_id, value['request_id'])
        directory = runtime.directory(safe_id(job_id))
        job = runtime.get_job(job_id)
        invocation = read_json(directory/'invocation.json', {})
        admission = read_json(admission_path(runtime.root, job_id), {})
        params = invocation.get('parameters') or {}
        frozen = load_mapping((directory/'laser.yaml').read_text())
        laser_id = frozen['id']
        if (job.get('status') != 'running' or job.get('laser', {}).get('id') != laser_id
                or (directory/'cancel.request').exists()
                or admission.get('task_id') != job_id or admission.get('laser_id') != laser_id
                or admission.get('state') not in ('direct', 'accepted')
                or admission.get('parameters') != params or params.get('instruction_profile') != 'adjustment'):
            raise ValueError('An active frozen resident adjustment task is required')
        if params.get('adoption_mode','auto_within_approved')!='auto_within_approved':
            raise ValueError('Task adoption policy requires review')
        if admission.get('profile') is not None:
            child=read_json(directory/'task-decision-session.json',{})
            if child.get('profile')!=admission['profile'] or child.get('report_author')!='task_model':
                raise ValueError('Verified delegated decision profile required')
        current = runtime.interface_repository.read('lasers', laser_id)
        if current['revision'] != value['expected_revision']:
            raise ValueError('Configuration changed; do not replay the quality candidate')
        before = current['document']
        outcome = read_json(directory/'adjustment-quality-outcome.json', {})
        if not outcome or digest(outcome) != value['outcome_revision']:
            raise ValueError('Recorded quality outcome changed')
        if outcome.get('old_thresholds') != thresholds(before) or digest(before) != outcome.get('source_snapshot_revision'):
            raise ValueError('Live definition differs from the measured task snapshot; reassess before saving')
        snapshot = route(runtime, job_id)
        final = snapshot.get('latest_result') or {}
        if (not snapshot.get('grant_active') or snapshot.get('controller_job_id') != value['controller_job_id']
                or snapshot.get('status', {}).get('state') != 'finished'
                or final.get('state') != 'adopted' or final.get('id') != value['adoption_id']
                or not finite(final.get('recorded_unix_s')) or not 0 <= clock()-final['recorded_unix_s'] <= 10):
            raise ValueError('Fresh final adoption from this granted controller is required')
        controller = runtime.get_job(value['controller_job_id'])
        if controller.get('laser', {}).get('id') != laser_id or controller.get('status') != 'running':
            raise ValueError('The adopted controller is no longer active for this laser')
        controller_dir = runtime.directory(value['controller_job_id'])
        if AdaptiveExperimentManager.foreground_ids(SimpleNamespace(job_directory=controller_dir)):
            raise ValueError('Foreground requests take priority over quality application')
        others = [j for j in runtime.active_jobs() if j.get('laser', {}).get('id') == laser_id
                  and j['id'] not in (job_id, controller['id']) and not j.get('read_only')]
        if others:
            raise ValueError('Another device operation may pin quality; application deferred')
        # Rebuild validation from protected owner results, not agent-writable copies.
        review = QualityReview(directory, frozen, persist=False)
        result_dir = control_root(runtime.root)/laser_id/controller['id']/'adaptive-experiments'/('task-'+job_id)/'adaptive-window'/'results'
        for identifier in outcome['verification']['measurement_ids']:
            row = read_json(result_dir/(safe_id(identifier)+'.json'), {})
            review.observe({**snapshot, 'latest_result':row})
        review.record({key:outcome[key] if key != 'measurement_ids' else outcome['verification']['measurement_ids'] for key in FIELDS}, snapshot={**snapshot, 'latest_result':review.measurements[outcome['verification']['measurement_ids'][-1]]['result']})
        review.observe(snapshot); review.finalize(snapshot)
        validated = review.outcome
        if not validated.get('post_adoption_verification', {}).get('metric_ranges'):
            raise ValueError('Final adopted-point verification is unavailable')
        ranges = validated['post_adoption_verification']['metric_ranges']
        if not any('.single_mode_max.' in key for key in validated['proposed_thresholds']):
            raise ValueError('A measured optical quality criterion is required')
        for key, limit in validated['proposed_thresholds'].items():
            if '.single_mode_max.' in key:
                metric = key.rsplit('.', 1)[-1]
                if metric not in ranges or ranges[metric]['max'] > limit*(1-validated['margin_fraction']):
                    raise ValueError('Repeated adopted quality does not meet the proposed criterion with margin')
        # A revised dither limit is judged against the MEAN of the repeated and
        # final adopted-point post-settle dither fits (at least two separate
        # windows plus the adoption window; 2026-10-28, a single 32 s fit
        # scatters by 15-20 %). The optical leaves above keep the max rule. An
        # unchanged dither limit is reported (comparisons), not a new refusal here.
        dither_changes = {k:v for k, v in validated['changes'].items() if is_dither(k)}
        adopted_dither = validated['post_adoption_verification'].get('dither_aggregate') or {}
        if dither_changes and adopted_dither.get('n', 0) < 3:
            raise ValueError('Two separate windows and the adoption window are required for a dither limit')
        for key, limit in dither_changes.items():
            if not dither_meets(adopted_dither, limit, validated['margin_fraction']):
                raise ValueError('Repeated adopted dither does not meet the proposed criterion with margin')
        patch = validated['changes']
        if not patch:
            return dict(status='unchanged', applied=False, saved=False)
        after = copy.deepcopy(before)
        for dotted, limit in patch.items():
            parts = dotted.split('.'); parent = after
            for part in parts[:-1]:parent = parent[part]
            parent[parts[-1]] = limit
        differences = changes(before, after)
        if not differences or any(not reviewed_quality_change(p, before, after) for p in differences):
            raise ValueError('Only existing optical classification or dither quality thresholds may change')
        lock_device = before.get('interfaces', {}).get('lock', {}).get('device')
        device = before.get('devices', {}).get(lock_device) or {}
        if device.get('type') != 'ecdl.rig.owner' or any(p[1] != lock_device for p in differences):
            raise ValueError('Quality application is scoped to this current controller owner')
        classifier_revision = configuration_digest(device['control_settings']['optical'])
        if any((frame['observation'].get('fringe_analysis') or {}).get('classification_revision') != classifier_revision
               for record in review.measurements.values() for frame in record['result']['optical_frames']):
            raise ValueError('Measured classifier revision does not match the quality baseline')
        state = owner_read(device, 'status')
        if (state.get('connected') is not True or state.get('fault')
                or state.get('rig') != device.get('connection', {}).get('instrument', lock_device)):
            raise ValueError('Owner is unavailable or faulted')
        inspected = owner_read(device, 'inspect')
        background = inspected.get('background') or {}
        adoption = (final.get('adoption') or {}).get('last') or {}
        context = adoption.get('context') or {}
        actual = (inspected.get('snapshot') or {}).get('outputs') or {}
        observation = inspected.get('observation') or {}
        stamp = (observation.get('frequency') or {}).get('acquired_unix_s')
        if (inspected.get('fault') or background.get('native_pi_active') is not True
                or background.get('epoch') is not None or background.get('reservations')
                or not adoption.get('token') or (background.get('last') or {}).get('token') != adoption['token']
                or background.get('generation') != adoption.get('generation')
                or (background.get('last') or {}).get('context') != context
                or context.get('system_id') != laser_id or observation.get('mode_id') != context.get('mode_id')
                or not finite(stamp) or not 0 <= clock()-stamp <= 1
                or not 0 <= clock()-final['recorded_unix_s'] <= 10
                or actual.get('current') != validated['selected_current']
                or not finite(actual.get('temperature'))):
            raise ValueError('Owner context, adopted point or foreground generation changed')
        for key, limit in validated['proposed_thresholds'].items():
            if '.single_mode_max.' in key:
                observed = observation.get('metrics', {}).get(key.rsplit('.', 1)[-1])
                if not finite(observed) or observed > limit*(1-validated['margin_fraction']):
                    raise ValueError('Fresh owner quality does not support the proposed criterion with margin')
        fresh_dither = inspected.get('dither') or {}
        fresh_aggregate = None
        if dither_changes:
            observed = fresh_dither.get('dither_response')
            source = fresh_dither.get('dither_source_unix_s')
            if (fresh_dither.get('dither_response_valid') is not True or not finite(observed)
                    or not finite(source) or not finite(final.get('post_settle_unix_s'))
                    or source <= final['post_settle_unix_s']):
                raise ValueError('Fresh owner dither does not support the proposed criterion with margin')
            # The fresh read joins the mean when it is a newer fit than every
            # adopted-point fit; the same fit (equal source end) is not counted twice.
            fits = list(adopted_dither['fits'])
            included = all(source > fit['source_end_unix_s'] for fit in fits)
            if included:
                fits.append(dict(id='owner-inspect', value=observed,
                    source_start_unix_s=fresh_dither.get('dither_source_start_unix_s'), source_end_unix_s=source))
            fresh_aggregate = dict(dither_aggregate(fits), fresh_fit_included=included,
                fresh_fit_note='newer owner fit, included in the mean' if included else
                    'same or older owner fit than the adopted-point fits; not counted twice')
            for key, limit in dither_changes.items():
                if not dither_meets(fresh_aggregate, limit, validated['margin_fraction']):
                    raise ValueError('Fresh owner dither does not support the proposed criterion with margin')
        if ((directory/'cancel.request').exists() or (controller_dir/'cancel.request').exists()
                or AdaptiveExperimentManager.foreground_ids(SimpleNamespace(job_directory=controller_dir))):
            raise ValueError('Stop/foreground request arrived before quality dispatch')
        # Durable intent precedes the one permitted configuration mutation.
        path.parent.mkdir(parents=True, exist_ok=True)
        record = dict(request=value, changes=patch, laser_id=laser_id,
            state='dispatching', created_unix_s=clock(), outcome=outcome, verified_outcome=validated,
            owner_verification=dict(background=background,outputs=actual,observation=observation,
                                    dither=fresh_dither if dither_changes else None,
                                    dither_aggregate=fresh_aggregate))
        atomic_json(path, record)
        try:
            saved = configuration.save(runtime, 'lasers', laser_id, yaml.safe_dump(after, sort_keys=False),
                value['expected_revision'], actor='resident-quality:'+job_id)
        except Exception as error:
            atomic_json(path, {**record, 'state':'unknown', 'error':str(error)})
            raise
        atomic_json(path, {**record, 'state':'saved', 'save_receipt':saved})
        return status(runtime, job_id, value['request_id'])
