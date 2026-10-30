"""2026-10-28: the dither quality limit (steady_lock.window.warning_response) is a revisable
threshold of the adjustment QualityReview under the same evidence rules as single_mode_max."""
import copy
import importlib.util
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest
import yaml

from experiment_core.records import atomic_json, digest
from ldcon.agents import resident_quality_apply as module
from ldcon.agents.adjustment_quality import QualityReview, thresholds
from ldcon.agents.agent_configuration import reviewed_quality_change, quality_change
from ldcon.agents.monitor_quality_binding import conditions
from ldcon.core.agent_authority import approval_root
from ldcon.core.interface_schema import digest as configuration_digest
from ldcon.web.agent_task_api import admission_path

_spec = importlib.util.spec_from_file_location('quality_data_dither', Path(__file__).with_name('test_adjustment_quality.py'))
q = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(q)

DPATH = 'devices.rig.control_settings.steady_lock.window.warning_response'
EDGE = 'devices.rig.control_settings.steady_lock.window.edge_response'


def document():
    doc = q.document()
    doc['devices']['rig']['control_settings']['steady_lock'] = dict(window=dict(warning_response=.0024, edge_response=.003))
    return doc


def measurement(identifier, start, *, dither=.007, **kw):
    row = q.measurement(identifier, start, **kw)
    row['dither'].update(dither_response=dither, dither_response_unit='nm / V')
    return row


def best_practical(changes):
    p = q.outcome(); p.update(classification='best_practical', changes=changes,
        rationale='On the 1.25 V branch the passive dither response is 0.005-0.010; the 0.0024 limit was set on the 0.8 V branch.')
    return p


@pytest.fixture
def review(tmp_path):
    review = QualityReview(tmp_path, document())
    review.observe(q.snapshot(measurement('first', 100, dither=.006)))
    latest = q.snapshot(measurement('second', 150, dither=.008))
    review.observe(latest)
    return review, latest


def test_dither_limit_is_a_revisable_threshold_and_edge_response_is_not():
    paths = thresholds(document())
    assert paths[DPATH] == .0024 and EDGE not in paths
    before = document(); after = copy.deepcopy(before)
    after['devices']['rig']['control_settings']['steady_lock']['window']['warning_response'] = .012
    assert reviewed_quality_change(tuple(DPATH.split('.')), before, after)
    assert not quality_change(tuple(DPATH.split('.')), before, after)   # laser_quality_update scope unchanged
    after['devices']['rig']['control_settings']['steady_lock']['window']['edge_response'] = .02
    assert not reviewed_quality_change(tuple(EDGE.split('.')), before, after)
    after['devices']['rig']['control_settings']['steady_lock']['window']['warning_response'] = 0
    assert not reviewed_quality_change(tuple(DPATH.split('.')), before, after)


def test_dither_threshold_accepted_with_two_windows_as_reclassification(review):
    review, latest = review
    result = review.record(best_practical({DPATH: .012}), latest)
    row = result['comparisons'][DPATH]
    assert row['observed_max'] == .008 and row['old_limit'] == .0024 and row['proposed_limit'] == .012
    assert row['meets_old'] is False and row['meets_proposed'] is True
    assert result['old_thresholds'][DPATH] == .0024 and result['proposed_thresholds'][DPATH] == .012
    assert result['reclassified_paths'] == [DPATH] and result['physical_improvement_claimed'] is False
    assert result['application_status'] == 'pending_point_adoption' and result['applied'] is False
    assert result['verification']['dither_range'] == [.006, .008]
    p = best_practical({DPATH: .012}); p['classification'] = 'restored_adequate'
    with pytest.raises(ValueError, match='relabel'):review.record(p, latest)


def test_dither_threshold_refused_without_two_separate_valid_windows(review):
    review, latest = review
    p = best_practical({DPATH: .012}); p['measurement_ids'] = ['second']
    with pytest.raises(ValueError, match='two distinct'):review.record(p, latest)
    review.measurements['first']['result']['dither']['dither_response_valid'] = False
    with pytest.raises(ValueError, match='dither window'):review.record(best_practical({DPATH: .012}), latest)
    assert review.outcome is None


@pytest.mark.parametrize('changes', [{EDGE: .02}, {DPATH: 0}, {DPATH: -1.}, {DPATH: float('inf')},
    {'devices.rig.control_settings.steady_lock.current_safety.max_error_hz': 1e9},
    {'devices.rig.control_settings.steady_lock.window.max_span': .1}])
def test_edge_response_and_safety_keys_are_refused(review, changes):
    review, latest = review
    with pytest.raises(ValueError, match='recognized'):review.record(best_practical(changes), latest)


def test_context_shows_dither_limit_source_date_and_measured_distribution(tmp_path, review):
    review, latest = review
    root = tmp_path/'deployment'
    ledger = approval_root(root, 'laser'); ledger.mkdir(parents=True)
    rows = [dict(unix_s=1792627200., event='configuration_saved', actor='dashboard', authority='free',
                 changes=[dict(path=DPATH, old=.003, new=.0024)]),
            dict(unix_s=1792627300., event='configuration_saved', actor='dashboard', authority='free',
                 changes=[dict(path='devices.rig.control_settings.steady_lock.window.ttl_s', old=1., new=2.)])]
    (ledger/'audit.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows))
    review.root = root; review.laser_id = 'laser'
    context = review.context()
    assert DPATH in context['revisable_thresholds'] and EDGE not in context['revisable_thresholds']
    dither = context['dither_quality']
    assert dither['current_limit'] == .0024 and dither['unit'] == 'nm / V'
    assert dither['source']['status'] == 'recorded' and dither['source']['utc'].startswith('2026-10-22')
    assert dither['source']['old'] == .003 and dither['source']['new'] == .0024
    measured = dither['recent_measured']
    assert measured['count'] == 2 and measured['min'] == .006 and measured['max'] == .008
    assert measured['above_current_limit'] == 2
    (ledger/'audit.jsonl').write_text('')
    fresh = QualityReview(tmp_path, document(), root=root)
    assert fresh.context()['dither_quality']['source']['status'] == 'not_recorded'


@pytest.fixture
def rig(tmp_path, monkeypatch):
    root = tmp_path/'deployment'; directory = root/'jobs/task'; directory.mkdir(parents=True)
    controller_dir = root/'jobs/controller'; controller_dir.mkdir()
    host = tmp_path/'trusted'; host.mkdir(); monkeypatch.setenv('LDCON_ADAPTIVE_ROOT', str(host))
    before = document()
    before['devices']['rig'].update(type='ecdl.rig.owner', connection=dict(endpoint='http://owner.invalid', instrument='rig'))
    before['interfaces'] = dict(lock=dict(device='rig'))
    (directory/'laser.yaml').write_text(yaml.safe_dump(before))
    params = dict(instruction_profile='adjustment')
    atomic_json(directory/'invocation.json', dict(parameters=params))
    admission_path(root, 'task').parent.mkdir(parents=True)
    atomic_json(admission_path(root, 'task'), dict(state='direct', task_id='task', laser_id='laser', profile=None, parameters=params))
    jobs = {i:dict(id=i, status='running', laser=dict(id='laser'), read_only=i=='task') for i in ('task', 'controller')}
    config = dict(document=before, revision='old')
    runtime = SimpleNamespace(root=root, mutex=threading.RLock(), directory=lambda i:root/'jobs'/i,
        get_job=lambda i:jobs[i], active_jobs=lambda:list(jobs.values()),
        interface_repository=SimpleNamespace(read=lambda *a:copy.deepcopy(config)))
    results = host/'laser/controller/adaptive-experiments/task-task/adaptive-window/results'; results.mkdir(parents=True)
    state = SimpleNamespace(before=before, directory=directory, results=results, controller_dir=controller_dir,
                            runtime=runtime, config=config, jobs=jobs, calls=[])

    def measured(*args, **kw):
        row = measurement(*args, **kw)
        for frame in row['optical_frames']:
            frame['observation']['fringe_analysis']['classification_revision'] = configuration_digest(before['devices']['rig']['control_settings']['optical'])
        return row

    def prepare(changes, *, margin=.1, fits=(('first', 800, .006), ('second', 850, .008)), final_dither=.007,
                classification='best_practical'):
        review = QualityReview(directory, before)
        for identifier, start, value in fits:
            row = measured(identifier, start, dither=value)
            atomic_json(results/(identifier+'.json'), row); review.observe(q.snapshot(row))
        candidate = best_practical(changes); candidate['margin_fraction'] = margin
        candidate['classification'] = classification
        candidate['measurement_ids'] = [f[0] for f in fits]
        review.record(candidate, q.snapshot(row))
        final = measured('final', 965, state='adopted', dither=final_dither); final['recorded_unix_s'] = 1000.
        adopted = dict(token='owner-token', generation=2, context=dict(system_id='laser', generation='generation', policy_revision='policy', mode_id='same'), state='adopted')
        final['adoption'] = dict(last=adopted)
        latest = q.snapshot(final); latest['status']['state'] = 'finished'; latest['grant_active'] = True
        atomic_json(results/'final.json', final); review.observe(latest); review.finalize(latest)
        monkeypatch.setattr('ldcon.agents.agent_background.route', lambda *a:copy.deepcopy(latest))
        state.inspect = dict(background=dict(native_pi_active=True, epoch=None, reservations=[], last=adopted, generation=2),
            snapshot=dict(outputs=dict(current=.77, temperature=.5)),
            observation=dict(mode_id='same', frequency=dict(acquired_unix_s=1000.), metrics=dict(side_peak_height_max=.1)),
            dither=dict(dither_response=.007, dither_response_valid=True, dither_source_unix_s=999.))
        state.review = review
        state.request = dict(request_id='quality-task', expected_revision='old', outcome_revision=digest(review.outcome),
                             controller_job_id='controller', adoption_id='final')
        return state

    def save(runtime, kind, laser, text, expected, **kw):
        state.calls.append((kind, laser, text, expected, kw)); config.update(document=yaml.safe_load(text), revision='new')
        return dict(revision='new', live=dict(jobs=dict(controller=dict(request_id='reload', status='queued'))))
    monkeypatch.setattr('ldcon.core.configuration.save', save)
    state.prepare = prepare
    return state


def apply(rig):
    owner_read = lambda device, action:dict(connected=True, fault=None, rig='rig') if action=='status' else copy.deepcopy(rig.inspect)
    return module.apply(rig.runtime, 'task', rig.request, owner_read=owner_read, clock=lambda:1000.)


def test_host_applies_dither_limit_through_scoped_cas_save_and_owner_block_receipt(rig):
    rig.prepare({DPATH: .012})
    result = apply(rig)
    assert result['status'] == 'saved_pending_owner' and len(rig.calls) == 1
    window = rig.config['document']['devices']['rig']['control_settings']['steady_lock']['window']
    assert window == dict(warning_response=.012, edge_response=.003)
    # The rig owner reports the steady-profile block it adopted, not the leaf.
    atomic_json(rig.controller_dir/'laser-live.json', dict(revision='new'))
    atomic_json(rig.controller_dir/'laser-reload-result-reload.json', dict(status='applied', revision_after='new',
        applied=['devices.rig.control_settings.steady_lock.window'], deferred=[],
        details=dict(owner=dict(revision='effective', saved_revision='effective'))))
    assert module.status(rig.runtime, 'task', 'quality-task')['applied'] is True
    atomic_json(rig.controller_dir/'laser-reload-result-reload.json', dict(status='applied', revision_after='new',
        applied=['devices.rig.control_settings.steady_lock.window_s'], deferred=[],
        details=dict(owner=dict(revision='effective', saved_revision='effective'))))
    assert module.status(rig.runtime, 'task', 'quality-task')['applied'] is False


def test_dither_limit_without_margin_or_with_stale_fresh_dither_is_not_saved(rig):
    rig.prepare({DPATH: .0075})   # mean(.006, .008, .007) = .007 > .0075*(1-.1)
    with pytest.raises(ValueError, match='adopted dither'):apply(rig)
    assert not rig.calls
    rig.prepare({DPATH: .012})
    rig.inspect['dither'].update(dither_source_unix_s=900.)   # before the adopted point settled
    with pytest.raises(ValueError, match='Fresh owner dither'):apply(rig)
    rig.inspect['dither'].update(dither_source_unix_s=999., dither_response_valid=False)
    with pytest.raises(ValueError, match='Fresh owner dither'):apply(rig)
    # A newer fresh fit joins the mean: mean(.006, .008, .007, .03) = .01275 > .012*(1-.1).
    rig.inspect['dither'].update(dither_response_valid=True, dither_response=.03)
    with pytest.raises(ValueError, match='Fresh owner dither'):apply(rig)
    assert not rig.calls


def test_monitor_binding_follows_the_revised_dither_value(rig):
    rules = [dict(id='dither-high', metric='dither_response', above=.003, clear_below=.0024),
             dict(id='quality-height', metric='fringe_metrics.side_peak_height_max', above=.5, clear_below=.45)]
    assert conditions(rules, rig.config['document'])[0]['above'] == .0024
    rig.prepare({DPATH: .012})
    apply(rig)
    bound = conditions(rules, rig.config['document'])
    assert bound[0]['above'] == .012 and bound[0]['clear_below'] == pytest.approx(.012*.0024/.003)
    assert bound[1]['above'] == .3
