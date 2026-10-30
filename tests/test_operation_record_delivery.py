"""Operation evidence is readable during continuous jobs and remains laser-scoped."""
import json
from types import SimpleNamespace
from fastapi.testclient import TestClient
from ldcon.web.web_server import create_app
from ldcon.web.web_fleet import create_fleet_app
from ldcon.agents import agent_log_read


def test_running_record_download_does_not_make_partial_measurements_final(tmp_path):
    app=create_app(token='test')
    app.state.runtime=SimpleNamespace(get_job=lambda _:dict(status='running'),directory=lambda _:tmp_path)
    record=dict(schema_version=1,outcome=dict(status='running'))
    (tmp_path/'operation-record.json').write_text(json.dumps(record))
    (tmp_path/'measurement.json').write_text('{}')
    client=TestClient(app);base='/s/test/api/jobs/job/artifacts/'
    assert client.get(base+'operation-record.json').json()==record
    assert client.get(base+'measurement.json').status_code==409
    (tmp_path/'operation-record.json').unlink()
    (tmp_path/'operation-record.json').symlink_to(tmp_path/'measurement.json')
    assert client.get(base+'operation-record.json').status_code==404


def test_fleet_forwards_only_named_evidence_routes(tmp_path):
    registry=tmp_path/'registry.json'
    registry.write_text(json.dumps(dict(services=[dict(id='laser',label='Laser',origin='http://127.0.0.1:12345',state_file=str(tmp_path/'state.json'))])))
    calls=[]
    def transport(service,method,route,query='',body=None):
        calls.append((service.id,method,route));return 200,'application/json',b'{"record":true}'
    app=create_fleet_app(registry,token='x'*32,transport=transport)
    client=TestClient(app);base=app.state.prefix+'/services/laser/api/jobs/job/artifacts/'
    for name in ('measurement.json','window-center.json','operation-record.json'):
        assert client.get(base+name).status_code==200
    assert client.get(base+'invocation.json').status_code==404
    assert client.post(base+'operation-record.json',headers={'X-LDCon-Client':'web'}).status_code==404
    assert len(calls)==3 and all(method=='GET' for _,method,_ in calls)


def test_agent_reads_record_without_cross_laser_or_arbitrary_file_access(tmp_path):
    (tmp_path/'laser-layout.json').write_text(json.dumps(dict(schema='ldcon.single-laser-layout.v1',laser_id='laser')))
    folder=tmp_path/'jobs'/'job';folder.mkdir(parents=True)
    (folder/'job.json').write_text(json.dumps(dict(id='job',laser_id='laser')))
    (folder/'operation-record.json').write_text(json.dumps(dict(schema_version=1,job_id='job')))
    result=agent_log_read.read(tmp_path,'laser',job_id='job',filename='operation-record.json')
    assert result['status']=='ok' and result['data']['job_id']=='job'
    import pytest
    with pytest.raises(ValueError,match='does not belong'):
        agent_log_read.read(tmp_path,'other',job_id='job',filename='operation-record.json')
