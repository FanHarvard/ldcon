"""The DLC router preserves the Sr target queue on schema-valid test copies."""
import json
from pathlib import Path
import shutil

import pytest
import yaml

from ldcon.web.dlcpro_target_api import TargetApi
from ldcon.web.web_runtime import WebRuntime
from .test_dlcpro_offline import no_network


@pytest.fixture
def ordinary(tmp_path, monkeypatch):
    source = (Path(__file__).resolve().parent / 'support') / 'web/tests/fixtures/live-20261022'
    root = tmp_path / 'state'
    shutil.copytree(source, root)
    # The archived recovery recipe was retired after these fixtures were saved.
    # This temporary, unexecuted fixture has no recovery operation; target
    # policy, physical limits, lock and target card definitions are unchanged.
    path = root / 'assignments/sr422.yaml'
    assignment = yaml.safe_load(path.read_text())
    assignment['decks'][0]['parameters']['steady-lock']['recovery'] = None
    path.write_text(yaml.safe_dump(assignment))
    monkeypatch.setenv('LDCON_JOB_LAUNCHER', 'service')
    runtime = WebRuntime(root)
    try:
        yield TargetApi(runtime, clock=lambda: 100.), root
    finally:
        runtime.close()


def make_lock(api, root, laser, state):
    target = api.ordinary._laser(laser)['target']['frequency_hz']
    directory = root / 'jobs' / laser / ('a' * 32)
    directory.mkdir(parents=True)
    operation = 'servo.ecdl_steady_lock' if laser == 'sr422' else 'servo.native_current'
    metadata = dict(id='a' * 32, set_id=laser, deck_id='deck', laser=dict(id=laser, name=laser, kind='interface-laser'),
                    created_utc='2026-10-22T00:00:00+00:00', read_only=False, engine='interface-cards',
                    cards=[dict(id='lock', card=operation, params={'target_frequency_hz': None})])
    (directory / 'job.json').write_text(json.dumps(metadata))
    (directory / 'progress.json').write_text(json.dumps(dict(status='running', controller_state=state)))
    (directory / 'laser-live.json').write_text('{"supported":true}')
    (directory / 'lock-target.json').write_text(json.dumps(dict(state='recovering', updated_unix_s=100.)))
    return target, directory


@pytest.mark.parametrize('laser', ['sr422', 'sr1092'])
@pytest.mark.parametrize('state', ['tracking', 'mode_degraded', 'waiting_for_measurement'])
def test_sr_queued_request_dispatches_once_after_fresh_lock_evidence(ordinary, laser, state):
    api, root = ordinary
    target, lock = make_lock(api, root, laser, state)
    assert api._api(laser) is api.ordinary
    body = dict(target_frequency_hz=target + 1e7, request_id='sr-shot-001')
    status, change = api.start_change(laser, body)
    assert status == 202 and change['state'] == 'queued'
    assert api.change_by_request(laser, body['request_id'])['change_id'] == change['change_id']
    job = root / 'jobs' / laser / change['change_id']
    assert not job.exists()
    (lock / 'lock-target.json').write_text(json.dumps(dict(state='locked', updated_unix_s=100., ramp_active=False)))
    api.tick()
    assert api.change_state(laser, change['change_id'])['state'] == 'accepted'
    assert (job / 'invocation.json').is_file()
    api.tick()
    assert len(list((root / 'jobs' / laser).glob('*/invocation.json'))) == 1


@pytest.mark.parametrize('laser', ['sr422', 'sr1092'])
def test_sr_queued_cancel_and_unknown_dispatch_keep_original_semantics(ordinary, laser, monkeypatch):
    from ldcon.web.lock_target_api import LockTargetApi
    api, root = ordinary
    target, lock = make_lock(api, root, laser, 'recovering')
    body = dict(target_frequency_hz=target + 1e7, request_id='sr-shot-001')
    _, first = api.start_change(laser, body)
    assert api.cancel(laser, first['change_id'])[1]['state'] == 'cancelled'
    _, second = api.start_change(laser, dict(body, request_id='sr-shot-002'))
    calls = []

    def lost(*args, **kwargs):
        calls.append((args, kwargs))
        raise RuntimeError('Dispatch outcome unknown')

    monkeypatch.setattr(LockTargetApi, 'start_change', lost)
    api.tick()
    api.tick()
    assert len(calls) == 1
    assert api.change_state(laser, second['change_id'])['state'] == 'unknown'
    assert api.cancel(laser, second['change_id'])[1]['state'] == 'unknown'
    assert not list((root / 'jobs' / laser).glob('*/invocation.json'))
