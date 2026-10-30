import copy
import importlib.util
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest
import yaml

from experiment_core.records import atomic_json, digest
from ldcon.agents import resident_quality_apply as module
from ldcon.agents.adjustment_quality import QualityReview
from ldcon.web.agent_task_api import admission_path
from ldcon.core.interface_schema import digest as configuration_digest

_spec = importlib.util.spec_from_file_location('quality_data', Path(__file__).with_name('test_adjustment_quality.py'))
q = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(q)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    root = tmp_path/'deployment'; directory = root/'jobs/task'; directory.mkdir(parents=True)
    controller_dir = root/'jobs/controller'; controller_dir.mkdir()
    host = tmp_path/'trusted'; host.mkdir(); monkeypatch.setenv('LDCON_ADAPTIVE_ROOT', str(host))
    before = q.document()
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
    review = QualityReview(directory, before)
    def measured(*args, **kw):
        row = q.measurement(*args, **kw)
        for frame in row['optical_frames']:
            frame['observation']['fringe_analysis']['classification_revision']=configuration_digest(before['devices']['rig']['control_settings']['optical'])
        return row
    for identifier, start in [('first', 800), ('second', 850)]:
        row = measured(identifier, start)
        atomic_json(results/(identifier+'.json'), row); review.observe(q.snapshot(row))
    candidate = q.outcome(); candidate['changes']={q.PATH:.35}
    review.record(candidate, q.snapshot(row))
    final = measured('final', 965, state='adopted'); final['recorded_unix_s']=1000.
    adopted = dict(token='owner-token', generation=2, context=dict(system_id='laser', generation='generation', policy_revision='policy', mode_id='same'), state='adopted')
    final['adoption'] = dict(last=adopted)
    latest = q.snapshot(final); latest['status']['state']='finished'; latest['grant_active']=True
    atomic_json(results/'final.json', final); review.observe(latest); review.finalize(latest)
    monkeypatch.setattr('ldcon.agents.agent_background.route', lambda *a:copy.deepcopy(latest))
    inspect = dict(background=dict(native_pi_active=True, epoch=None, reservations=[], last=adopted, generation=2),
        snapshot=dict(outputs=dict(current=.77, temperature=.5)),
        observation=dict(mode_id='same',frequency=dict(acquired_unix_s=1000.),metrics=dict(side_peak_height_max=.1)))
    owner_read = lambda device, action:dict(connected=True, fault=None,rig='rig') if action=='status' else copy.deepcopy(inspect)
    calls=[]
    def save(runtime, kind, laser, text, expected, **kw):
        calls.append((kind, laser, text, expected, kw)); config.update(document=yaml.safe_load(text), revision='new')
        return dict(revision='new', live=dict(jobs=dict(controller=dict(request_id='reload',status='queued'))))
    monkeypatch.setattr('ldcon.core.configuration.save', save)
    request = dict(request_id='quality-task', expected_revision='old', outcome_revision=digest(review.outcome),
                   controller_job_id='controller', adoption_id='final')
    return SimpleNamespace(runtime=runtime, request=request, owner_read=owner_read, inspect=inspect,
        latest=latest, config=config, calls=calls, jobs=jobs, directory=directory, controller_dir=controller_dir,
        results=results, review=review)


def apply(rig):
    return module.apply(rig.runtime, 'task', rig.request, owner_read=rig.owner_read, clock=lambda:1000.)


def test_scoped_cas_save_is_pending_until_matching_owner_paths_and_revision_confirmed(rig):
    result = apply(rig)
    assert result['status']=='saved_pending_owner' and result['applied'] is False
    assert len(rig.calls)==1 and rig.calls[0][3]=='old'
    assert rig.config['document']['devices']['rig']['control_settings']['optical']['single_mode_max']['side_peak_height_max']==.35
    assert apply(rig)['status']=='saved_pending_owner' and len(rig.calls)==1
    atomic_json(rig.controller_dir/'laser-live.json', dict(revision='new'))
    atomic_json(rig.controller_dir/'laser-reload-result-reload.json', dict(status='applied', revision_after='new',
        applied=[q.PATH], deferred=[], details=dict(owner=dict(revision='effective', saved_revision='effective'))))
    assert module.status(rig.runtime,'task','quality-task')['applied'] is True
    assert module.status(rig.runtime,'task','quality-task')['owner_effective_revision']=='effective'


@pytest.mark.parametrize('damage',['cas','grant','task','source','controller','foreground','generation','current','stale','history','raw'])
def test_changed_admission_or_evidence_blocks_before_save(rig, damage):
    if damage=='cas':rig.request['expected_revision']='another'
    elif damage=='grant':rig.latest['grant_active']=False
    elif damage=='task':rig.jobs['task']['status']='cancelled'
    elif damage=='source':rig.config['document']['name']='Changed source'
    elif damage=='controller':rig.jobs['controller']['laser']['id']='other'
    elif damage=='foreground':rig.inspect['background']['reservations']=['foreground']
    elif damage=='generation':rig.inspect['background']['generation']=3
    elif damage=='current':rig.inspect['snapshot']['outputs']['current']=.78
    elif damage=='stale':rig.latest['latest_result']['recorded_unix_s']=980.
    elif damage=='history':rig.request['outcome_revision']='changed'
    elif damage=='raw':
        row=q.measurement('first',800,peak=.6);atomic_json(rig.results/'first.json',row)
    with pytest.raises(ValueError):apply(rig)
    assert not rig.calls


def test_pending_foreground_file_and_other_active_device_operation_block(rig):
    (rig.controller_dir/'hot-control-request.json').write_text('{}')
    with pytest.raises(ValueError,match='Foreground'):apply(rig)
    (rig.controller_dir/'hot-control-request.json').unlink()
    rig.jobs['shot']=dict(id='shot',status='running',laser=dict(id='laser'),read_only=False)
    with pytest.raises(ValueError,match='pin quality'):apply(rig)
    assert not rig.calls


def test_uncertain_configuration_save_is_journaled_and_never_retried(rig, monkeypatch):
    def uncertain(*a, **kw):rig.calls.append('attempt');raise OSError('response lost after possible save')
    monkeypatch.setattr('ldcon.core.configuration.save', uncertain)
    with pytest.raises(OSError):apply(rig)
    result=apply(rig)
    assert result['status']=='unknown_no_retry' and result['applied'] is False
    assert rig.calls==['attempt']


def test_owner_receipt_for_partial_or_different_revision_is_never_applied(rig):
    apply(rig)
    atomic_json(rig.controller_dir/'laser-live.json',dict(revision='new'))
    for changed in [dict(applied=[]),dict(revision_after='wrong'),dict(deferred=[q.PATH]),
                    dict(details=dict(owner=dict(revision='different',saved_revision='effective')))]:
        atomic_json(rig.controller_dir/'laser-reload-result-reload.json',dict(
            dict(status='applied',revision_after='new',applied=[q.PATH],deferred=[],
                 details=dict(owner=dict(revision='effective',saved_revision='effective'))), **changed))
        assert module.status(rig.runtime,'task','quality-task')['applied'] is False


def test_fresh_quality_failure_blocks_even_with_good_recorded_adoption(rig):
    rig.inspect['observation']['metrics']['side_peak_height_max']=.4
    with pytest.raises(ValueError,match='Fresh owner quality'):apply(rig)
    assert not rig.calls


def test_permitted_tec_following_does_not_invalidate_fresh_verified_quality(rig):
    rig.inspect['snapshot']['outputs']['temperature']=.5001
    assert apply(rig)['status']=='saved_pending_owner'
    assert len(rig.calls)==1
