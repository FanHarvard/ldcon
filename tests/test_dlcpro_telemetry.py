"""Independent monitoring must report fault/idle and suppress stale optics."""
from copy import deepcopy
import io
import json
from types import SimpleNamespace

import pytest

from ldcon.owners.dlcpro_telemetry import DLCReader, summarize
from ldcon.owners.owner_telemetry import collect, collector


def snapshot():
    return dict(client_claimed=True, snapshot=dict(observed_s=10, mode='idle',
        fault='control frequency deadline exceeded', pi_enabled=False,
        hardware=dict(emission=True, piezo=dict(setpoint=54.7, measured=54.8,
                                               calculated_output=None)),
        observation=dict(acquired_unix_s=100., frequency_hz=4.37e14,
            quality_valid=True, metrics=dict(side_peak_height_max=.1),
            quality=dict(fresh=True, quality_valid=True, single_mode=True))))


def test_faulted_idle_owner_keeps_new_independent_measurements():
    source=snapshot(); before=deepcopy(source)
    result=summarize(source, now=100.1, max_age_s=.2)
    assert result['owner_fault'] and not result['native_pi_enabled']
    assert result['frequency_valid'] and result['optical_valid']
    assert result['frequency_hz']==4.37e14 and result['single_mode']
    assert result['hardware']['piezo']['calculated_output'] is None
    assert source==before


@pytest.mark.parametrize('now', [99., 100.3])
def test_stale_or_future_frequency_is_not_republished_as_current(now):
    result=summarize(snapshot(), now=now, max_age_s=.2)
    assert not result['frequency_valid'] and not result['optical_valid']
    assert 'frequency_hz' not in result and 'single_mode' not in result
    assert 'metrics' not in result['observation']
    assert result['hardware']['piezo']['measured']==54.8


def test_quality_has_independent_freshness_gate():
    source=snapshot();source['snapshot']['observation']['quality']['fresh']=False
    result=summarize(source,now=100.1,max_age_s=.2)
    assert result['frequency_valid'] and not result['optical_valid']
    assert 'metrics' not in result['observation']


def test_collector_makes_only_scoped_inspect_request(monkeypatch):
    reader=DLCReader(dict(endpoint='http://127.0.0.1:1234',laser_id='test'))
    requests=[]
    def opened(request, timeout):
        requests.append(json.loads(request.data))
        return io.BytesIO(json.dumps(dict(ok=True,result=snapshot())).encode())
    reader.http=SimpleNamespace(open=opened)
    monkeypatch.setenv('LDCON_INSTRUMENT_TOKEN','test-only')
    rows=[];store=SimpleNamespace(record=lambda *args:rows.append(args))
    collect(reader,store,'run',1,.2,now=lambda:100.1,project=summarize)
    assert requests==[dict(action='inspect',laser_id='test')]
    assert rows[0][2]=='owner_sample' and rows[0][3]['owner_fault']


def test_failed_read_publishes_health_without_old_values():
    def failed():raise OSError('unavailable')
    rows=[];store=SimpleNamespace(record=lambda *args:rows.append(args))
    result=collect(SimpleNamespace(read=failed),store,'run',1,.2,project=summarize)
    assert not result['read_ok'] and 'hardware' not in result
    assert 'frequency_hz' not in result


def test_device_binding_constructs_no_hardware_connection():
    device=dict(type='dlcpro.laser.owner',connection=dict(endpoint='http://127.0.0.1:1234',laser_id='test'),
                control_settings=dict(policy=dict(max_sample_age_s=.2)))
    reader,project,operation,age=collector(dict(devices=dict(dlc=device)))
    assert isinstance(reader,DLCReader) and project is summarize
    assert operation=='monitor.dlcpro' and age==.2
    with pytest.raises(ValueError,match='One DLC'):
        collector(dict(devices=dict(first=device,second=device)))


def test_queries_prefer_monitor_and_block_stale_trace_on_failed_read():
    from ldcon.web.metric_query_profiles import dlc_trace_query
    expression=dlc_trace_query('hardware.piezo.measured',['steady-lock'])
    assert 'kind="owner_sample"' in expression and 'kind="trace"' in expression
    assert 'unless on(laser)' in expression and 'ldcon_read_ok' in expression
    assert 'tlast_over_time' in expression
