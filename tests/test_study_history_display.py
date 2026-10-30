"""Result browsing uses publications, never a controller API or owner."""
import json
from fastapi.testclient import TestClient
from ldcon.web.web_fleet import create_fleet_app


def test_study_index_filters_laser_preserves_renamed_operations_and_artifacts(tmp_path):
    root=tmp_path/'laser';(root/'jobs').mkdir(parents=True)
    registry=tmp_path/'services.json'
    registry.write_text(json.dumps({'services':[dict(id='laser',label='Laser',origin='http://127.0.0.1:9999',state_file=str(root/'web-server.json'))]}))
    for ident,laser in [('old','laser'),('other','other')]:
        d=root/'jobs'/ident;d.mkdir()
        (d/'job.json').write_text(json.dumps(dict(id=ident,set_id=laser,cards=[dict(id='target-response',card='legacy.response')],created_utc='2026-10-01')))
        (d/'progress.json').write_text('{"status":"succeeded"}')
        (d/'target-response.json').write_text('{"measurements":[]}')
    (root/'jobs/old/report.pdf').write_bytes(b'%PDF-fixture')
    def forbidden(*args):raise AssertionError('Read-only history contacted the laser API')
    app=create_fleet_app(registry,token='x'*32,transport=forbidden)
    with TestClient(app) as client:
        url=app.state.prefix+'/services/laser/api/jobs'
        rows=client.get(url+'?set_id=laser').json()['jobs']
        assert len(rows)==1 and rows[0]['id']=='old'
        assert rows[0]['available_artifacts']==['target-response.json']
        assert rows[0]['pdf_available'] is True
        assert client.get(url+'?set_id=laser&operation=measure.target_response').json()['jobs']==[]
        (root/'jobs/old/report.pdf').unlink()
        assert client.get(url+'?set_id=laser').json()['jobs'][0]['pdf_available'] is False
