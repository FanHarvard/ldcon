"""Offline storage migration contracts; no owners, providers or hardware calls."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ldcon.core import state_layout as paths
from ldcon.agents import agent_workspace, agent_log_read
from ldcon.jobs import job_service
from ldcon.core.interface_schema import InterfaceRepository
from ldcon.core.laser_assets import LaserAssets, layout
from ldcon.jobs.laser_live import laser_file
from ldcon.web.web_runtime import WebRuntime


@pytest.fixture(params=[False, True], ids=['legacy', 'single-laser'])
def root(tmp_path, request):
    if request.param:
        (tmp_path/paths.MARKER).write_text(json.dumps(dict(schema=paths.SCHEMA, laser_id='laser')))
    return tmp_path


def test_assets_config_and_agent_locations_and_permissions(root):
    manifest=layout(root,'laser',True)
    assert agent_workspace.directory(root,'laser')==Path(manifest['agent'])
    assert Path(manifest['job_logs'])==paths.job_root(root,'laser')
    assets=LaserAssets(root,'laser')
    assets.write('reports/result.txt','Recorded evidence','')
    assert (Path(manifest['laser_root'])/'reports/result.txt').read_text()=='Recorded evidence'
    for name in ('config/lasers/laser.yaml','jobs/any/progress.json','agents/workspace/CLAUDE.md','../other/reports/x.txt'):
        with pytest.raises(ValueError):assets.write(name,'not allowed','')
    repo=InterfaceRepository(root)
    config=paths.config_file(root,'lasers','laser')
    config.write_text(json.dumps(dict(schema_version=1,id='laser',name='Fixture',allow_output=False,devices={},interfaces={})))
    assert repo.read('lasers','laser')['source_path']==manifest['laser_config']
    assert [v['id'] for v in repo.list('lasers')]==['laser']
    if paths.single_laser(root):
        assert Path(manifest['laser_root'])==root
        assert not (root/'laser-assets').exists()
        for call in (lambda:layout(root,'other'),lambda:repo.read('lasers','other')):
            with pytest.raises(ValueError,match='belong'):call()


def make_job(root,name='job1',laser='laser'):
    directory=paths.job_root(root,'laser')/name;directory.mkdir(parents=True)
    (directory/'job.json').write_text(json.dumps(dict(id=name,laser_id=laser,set_id=laser,laser={'id':laser},worker_backend='service')))
    (directory/'progress.json').write_text('{"status":"succeeded"}')
    return directory


def test_history_read_and_live_configuration_resolve_after_relocation(root):
    directory=make_job(root)
    assert laser_file(directory,'laser')==paths.config_file(root,'lasers','laser')
    with agent_log_read._job(root,'laser','job1') as descriptor:
        assert descriptor>=0
    runtime=SimpleNamespace(jobs=root/'jobs')
    assert WebRuntime.directory(runtime,'job1')==directory
    assert WebRuntime.job_metadata(runtime)==[directory/'job.json']
    (directory/'job.json').write_text('{"id":"job1","laser_id":"other"}')
    with pytest.raises(ValueError,match='different'): 
        with agent_log_read._job(root,'laser','job1'):pass


def test_durable_worker_launch_is_not_replayed(root,monkeypatch):
    directory=make_job(root)
    (directory/'launch.request').touch();calls=[]
    monkeypatch.setattr(job_service,'launch',lambda d:calls.append(d) or 'child')
    assert job_service.dispatch_pending(root)==['child']
    assert job_service.dispatch_pending(root)==[]
    assert calls==[directory]


def test_marker_does_not_follow_symlinks_or_accept_unknown_schema(tmp_path):
    path=tmp_path/paths.MARKER;path.write_text('{"schema":"unknown","laser_id":"laser"}')
    with pytest.raises(ValueError):paths.single_laser(tmp_path)
    path.unlink();path.symlink_to(tmp_path/'missing')
    with pytest.raises(OSError):paths.single_laser(tmp_path)


def test_shared_settings_and_approval_ledger_remain_outside_laser_root(tmp_path,monkeypatch):
    from ldcon.agents.agent_profiles import location
    from ldcon.core.agent_authority import approval_root
    root=tmp_path/'var/lasers/laser';root.mkdir(parents=True)
    (root/paths.MARKER).write_text(json.dumps(dict(schema=paths.SCHEMA,laser_id='laser')))
    for key in ('LDCON_AGENT_PROFILE_REGISTRY','LDCON_AGENT_APPROVALS_ROOT'):
        monkeypatch.delenv(key,raising=False)
    assert location(root)==tmp_path/'var/dashboard/agent-profiles.json'
    assert approval_root(root,'laser')==tmp_path/'var/agent-approvals/laser'
    assert paths.root_from_laser_file(root/'config/lasers/laser.yaml')==root
    assert not approval_root(root,'laser').is_relative_to(root)


def test_existing_gateway_identity_survives_directory_relocation(tmp_path):
    from ldcon.agents.resident_agent import AgentStore,public_agent_id
    meta=dict(set_id='laser',deck_id='deck');card='gateway'
    store=AgentStore(tmp_path);key=store.key(meta,card)
    store.session_context=dict(laser_id='laser',bridge_id='laser-existing-gateway',workspace='/previous/workspace')
    store.save(key,'claude','same-conversation','disconnected','old-job')
    (tmp_path/paths.MARKER).write_text(json.dumps(dict(schema=paths.SCHEMA,laser_id='laser')))
    assert public_agent_id(tmp_path,meta,card,'laser','claude')=='laser-existing-gateway'


def test_configuration_save_stages_from_selected_layout(root, monkeypatch):
    import threading
    from ldcon.core.configuration import save
    monkeypatch.delenv('LDCON_MAINTAINER_TOKEN',raising=False)
    repo=InterfaceRepository(root)
    path=paths.config_file(root,'lasers','laser')
    before=dict(schema_version=1,id='laser',name='Before',allow_output=False,devices={},interfaces={})
    path.write_text(json.dumps(before))
    runtime=SimpleNamespace(root=root,mutex=threading.RLock(),interface_repository=repo,
                            active_jobs=lambda:[],assert_admission_open=lambda:None)
    after=dict(before,name='After')
    result=save(runtime,'lasers','laser',json.dumps(after),repo.read('lasers','laser')['revision'],
                maintainer_reference='Offline layout regression')
    assert result['document']==after and json.loads(path.read_text())==after
    assert not (root/'lasers').exists() if paths.single_laser(root) else (root/'lasers').exists()
