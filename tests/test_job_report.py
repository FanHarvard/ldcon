import json
from threading import RLock
from types import SimpleNamespace

from ldcon.web.job_report import evidence, render


def test_html_report_reads_evidence_not_json_dump_or_unavailable_artifact_links(tmp_path):
    job = dict(id='a'*32, status='succeeded', set_id='laser', set_name='<script>bad()</script>',
               latest=dict(frequency_hz=4e14), agent={'response': '<img src=x onerror=bad()>'})
    (tmp_path/'summary.json').write_text(json.dumps(dict(cards={'measure': {'status':'succeeded', 'samples':12}})))
    (tmp_path/'telemetry.events.jsonl').write_text('\n'.join(json.dumps(dict(kind='state', data={'state':str(i)})) for i in range(125)))
    (tmp_path/'mode-map.json').write_text('{"best_current": 1.2}')
    (tmp_path/'agent-instructions.md').write_text('# DFB individual notes')
    page = render(job, evidence(tmp_path))
    assert '<pre>' not in page and '<table>' in page
    assert '<script>' not in page and '<img' not in page
    assert '&lt;script&gt;' in page and '&lt;img' in page
    assert 'best_current' in page and 'artifacts/mode-map.json' in page
    assert 'artifacts/grafana-dashboard.json' not in page
    assert 'DFB individual notes' in page
    assert len(evidence(tmp_path)['events']) == 100
    assert '現在の実機状態とは区別' in page


def test_symlinks_and_oversized_records_are_not_followed(tmp_path):
    other = tmp_path/'outside'; other.write_text('PRIVATE')
    (tmp_path/'agent-instructions.md').symlink_to(other)
    (tmp_path/'summary.json').write_text('x' * 300000)
    assert evidence(tmp_path)['files'] == {}
    assert 'PRIVATE' not in render({'id':'a'}, evidence(tmp_path))


def test_archived_report_retains_readable_results_without_broken_downloads():
    job = dict(id='a', status='succeeded', archived=True, report_evidence=dict(
        events=[dict(kind='done')], artifacts=['mode-map.json'], files={'mode-map.json':{'current': 1}}))
    page = render(job)
    assert 'current' in page and 'done' in page
    assert 'href="artifacts/' not in page


def test_report_routes_regenerate_old_archive_and_history_filters_exact_set(tmp_path):
    import pytest
    pytest.importorskip('httpx')
    pytest.importorskip('fastapi')
    from fastapi.testclient import TestClient
    from ldcon.web.web_server import create_app
    app = create_app(token='test')
    live = tmp_path/'live'; live.mkdir()
    (live/'summary.json').write_text('{"result":"Readable result"}')
    archive = tmp_path/'archive-index'; archive.mkdir()
    (archive/'old.json').write_text(json.dumps(dict(id='old', set_id='laser2', status='succeeded', archived=True, summary={'result':'Old result'})))
    (archive/'old.html').write_text('<pre>OLD JSON PAGE</pre>')
    current = dict(id='current', set_id='laser', status='running')
    app.state.runtime = SimpleNamespace(root=tmp_path, mutex=RLock(), directory=lambda _:live,
                                      get_job=lambda _:current, list_jobs=lambda:[current])
    client = TestClient(app)
    result = client.get('/s/test/api/jobs/current/report')
    assert result.status_code == 200 and result.headers['content-type'].startswith('text/html')
    assert 'Readable result' in result.text
    old = client.get('/s/test/api/jobs/old/report')
    assert 'Old result' in old.text and 'OLD JSON PAGE' not in old.text
    rows = client.get('/s/test/api/history?set_id=laser').json()
    assert rows['total'] == 1 and rows['jobs'][0]['id'] == 'current'
    html = client.get('/s/test/').text
    assert 'assets/laser-workspace.js' in html and 'assets/configuration-editor.js' in html
    for name in ('laser-workspace.js', 'configuration-editor.js', 'workspace.css', 'i18n.js'):
        assert client.get('/s/test/assets/'+name).status_code == 200
