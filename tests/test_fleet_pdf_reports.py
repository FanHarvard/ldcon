"""Read-only PDF delivery must never call a laser API or reinterpret old HTML."""
import json
import pytest
from fastapi.testclient import TestClient
from ldcon.web.web_fleet import create_fleet_app, MAX_RESPONSE
from ldcon.web import dashboard_snapshot


@pytest.mark.parametrize('delivery_first', [False, True])
def test_pdf_listing_and_delivery_share_complete_bytes(tmp_path, delivery_first):
    root=tmp_path/'laser';folder=root/'jobs'/'map';folder.mkdir(parents=True)
    (folder/'job.json').write_text(json.dumps(dict(id='map',set_id='laser',cards=[dict(
        id='mode-map',operation='study.ecdl_mode_map')])))
    (folder/'progress.json').write_text(json.dumps(dict(status='succeeded')))
    pdf=b'%PDF-1.7\ncomplete report body\n%%EOF'
    path=folder/'report.pdf';path.write_bytes(pdf)
    registry=tmp_path/'services.json';registry.write_text(json.dumps(dict(services=[dict(
        id='laser',label='laser',origin='http://127.0.0.1:12345',state_file=str(root/'web-server.json'))])))
    def forbidden(*args,**kwargs):raise AssertionError('No control service access')
    app=create_fleet_app(registry,token='x'*32,transport=forbidden);client=TestClient(app)
    base=app.state.prefix+'/services/laser/api/'
    # Header readers must not poison either JSON metadata or the complete download.
    assert dashboard_snapshot.read(root,path,parser=lambda raw:raw[:5])==b'%PDF-'
    for _ in range(2):
        if delivery_first:assert client.get(base+'jobs/map/report.pdf').content==pdf
        assert client.get(base+'jobs?set_id=laser').json()['jobs'][0]['pdf_available'] is True
        assert client.get(base+'jobs/map/report.pdf').content==pdf
    path.unlink()
    assert client.get(base+'jobs?set_id=laser').json()['jobs'][0]['pdf_available'] is False
    assert client.get(base+'jobs/map/report.pdf').status_code==404


def test_pdf_routes_are_scoped_bounded_and_do_not_contact_services(tmp_path):
    def row(ident):
        root=tmp_path/ident;root.mkdir();(root/'jobs').mkdir()
        return dict(id=ident,label=ident,origin='http://127.0.0.1:12345',state_file=str(root/'web-server.json'))
    registry=tmp_path/'services.json';registry.write_text(json.dumps(dict(services=[row('a'),row('b')])))
    def forbidden(*args,**kwargs):raise AssertionError('PDF viewing cannot contact a laser service')
    app=create_fleet_app(registry,token='x'*32,transport=forbidden);client=TestClient(app)
    url=app.state.prefix+'/services/a/api/jobs/study/report.pdf'
    folder=tmp_path/'a/jobs/study';folder.mkdir();(folder/'report.html').write_text('historical only')
    assert client.get(url).status_code==404
    pdf=b'%PDF-1.7\nsynthetic binary\x80\xff\n%%EOF'
    path=folder/'report.pdf';path.write_bytes(pdf)
    response=client.get(url);assert response.status_code==200
    assert response.content==pdf and response.headers['content-type']=='application/pdf'
    assert client.get(url.replace('/services/a/','/services/b/')).status_code==404
    assert client.get(url+'?path=/etc/passwd').status_code==422
    assert client.get(url.replace('/services/a/','/services/missing/')).status_code==404
    assert client.post(url,headers={'X-LDCon-Client':'web'}).status_code==404
    path.write_text('<html>not PDF</html>');assert client.get(url).status_code==404
    with path.open('wb') as f:f.write(b'%PDF-');f.truncate(MAX_RESPONSE+1)
    assert client.get(url).status_code==404
    path.unlink();outside=tmp_path/'outside.pdf';outside.write_bytes(pdf);path.symlink_to(outside)
    assert client.get(url).status_code==404
    path.unlink()
    standard=tmp_path/'a/reports/study/build';standard.mkdir(parents=True);(standard/'report.pdf').write_bytes(pdf)
    assert client.get(url).content==pdf
    (standard/'report.pdf').unlink();standard.rmdir();standard.symlink_to(tmp_path)
    (tmp_path/'report.pdf').write_bytes(pdf);assert client.get(url).status_code==404
