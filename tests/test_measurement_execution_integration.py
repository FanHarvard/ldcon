"""Durable measure-card worker/report integration, without ROOT or hardware."""
import copy
import json
from pathlib import Path
import socket

import pytest

from tests.measurement_fixtures import measurement_parameters, measurement_batch
import yaml
from ldcon.core.contracts import DeviceError
from ldcon.core.records import read_json
from ldcon.core.operation_registry import spec,normalize_parameters,validate_card,admission_class
from ldcon.jobs import measurement_execution,measurement_report,interface_worker


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*a,**k):raise AssertionError('No network or physical device in this test')
    monkeypatch.setattr(socket.socket,'connect',forbidden)
    monkeypatch.setattr(socket,'create_connection',forbidden)
    monkeypatch.delenv('LDCON_TELEGRAF_ENDPOINT',raising=False)


def result(status='succeeded'):
    return dict(schema='ldcon.change-measure.v1',status=status,physical_outcome='applied',
        samples=[dict(phase='measurement',measurements={'wavelength':dict(valid=True,acquired_unix_s=100.,values={'wavelength_nm':685.45})})],
        application=dict(status='applied',readback={'current':.1}),summary={})


def report_stub(calls):
    def write(directory,record):
        directory=Path(directory)
        # Evidence has already been durably recorded before presentation begins.
        assert read_json(directory/'measurement.json',{})['physical_outcome']==record['physical_outcome']
        calls.append(copy.deepcopy(record))
        pdf=directory/'report'/'report.pdf';pdf.parent.mkdir(exist_ok=True)
        pdf.write_bytes(b'%PDF-1.7\n% offline test report\n')
        return pdf
    return write


def test_wrapper_persists_owner_receipt_before_report(tmp_path,monkeypatch):
    expected=result();reports=[]
    monkeypatch.setattr(measurement_execution,'run_measurement',lambda *a,**k:copy.deepcopy(expected))
    monkeypatch.setattr(measurement_report,'write_report',report_stub(reports))
    summary=measurement_execution.run_card(tmp_path,{}, {},{},emit=lambda *a:None)
    assert summary['status']=='succeeded' and summary['physical_outcome']=='applied'
    assert summary['report']==dict(status='created',path='report.pdf')
    artifact=read_json(tmp_path/'measurement.json',{})
    assert artifact['application']==expected['application'] and len(artifact['samples'])==1
    assert len(reports)==1 and reports[0]['status']=='succeeded'
    assert (tmp_path/'report.pdf').read_bytes()==(tmp_path/'report'/'report.pdf').read_bytes()


def test_original_failure_survives_partial_pdf_generation(tmp_path,monkeypatch):
    failure=DeviceError('owner-confirmed handoff failed')
    partial=result('failed');reports=[]
    def fail(*args,emit,**kwargs):
        emit('change_measure_result',partial)
        raise failure
    monkeypatch.setattr(measurement_execution,'run_measurement',fail)
    monkeypatch.setattr(measurement_report,'write_report',report_stub(reports))
    with pytest.raises(DeviceError) as captured:
        measurement_execution.run_card(tmp_path,{}, {},{},emit=lambda *a:None)
    assert captured.value is failure
    assert read_json(tmp_path/'measurement.json',{})['status']=='failed'
    assert (tmp_path/'report.pdf').exists() and reports[0]['samples']==partial['samples']


@pytest.mark.parametrize('physical_failure',[False,True])
def test_report_failure_is_not_a_new_physical_failure(tmp_path,monkeypatch,physical_failure):
    error=DeviceError('original physical error')
    expected=result('failed' if physical_failure else 'succeeded')
    def execute(*args,emit,**kwargs):
        if physical_failure:emit('change_measure_result',expected);raise error
        return expected
    def report(*args):raise RuntimeError('Unavailable presentation toolchain')
    monkeypatch.setattr(measurement_execution,'run_measurement',execute)
    monkeypatch.setattr(measurement_report,'write_report',report)
    if physical_failure:
        with pytest.raises(DeviceError) as caught:measurement_execution.run_card(tmp_path,{}, {},{},emit=lambda *a:None)
        assert caught.value is error
    else:
        r=measurement_execution.run_card(tmp_path,{}, {},{},emit=lambda *a:None)
        assert r['status']=='succeeded' and r['physical_outcome']=='applied'
    record=read_json(tmp_path/'measurement.json',{})
    assert record['status']==expected['status'] and record['application']['status']=='applied'
    assert record['report']==dict(status='unavailable',reason='RuntimeError')


def card(operation):
    p=measurement_parameters(changes=[],measurements={},duration_s=0.)
    return dict(id='measure',operation=operation,uses=[],parameters=p if operation=='measure.change' else measurement_batch([p]))


def dlc_laser(allow=True):
    return dict(id='fixture',name='Fixture',allow_output=allow,
        devices={'dlc':dict(type='dlcpro.laser.owner',control_settings=dict(commissioned=True,policy={'axes':{'current':{},'temperature':{},'piezo':{}}}))},
        interfaces={'dlc_lock':dict(device='dlc',capability='dlcpro_frequency_control')})


@pytest.mark.parametrize('operation',['measure.change','measure.batch'])
def test_public_catalog_validates_and_dlc_stays_exclusive(operation):
    c=card(operation);laser=dlc_laser();deck={'cards':[c]}
    p=normalize_parameters(c,{})
    assert spec(operation).operation_id==operation
    assert validate_card(laser,deck,c)['runnable']
    active=[dict(status='running',cards=[dict(operation='servo.dlcpro_frequency')])]
    assert admission_class(laser,c,active)=='exclusive'
    assert admission_class(laser,c,[])=='exclusive'
    denied=copy.deepcopy(c)
    step=denied['parameters'] if operation=='measure.change' else denied['parameters']['steps'][0]
    step['changes']=[dict(parameter='current',value=100.)]
    assert not validate_card(dlc_laser(False),{'cards':[denied]},denied)['runnable']
    assert p.get('steps') is not None if operation=='measure.batch' else p['changes']==[]


@pytest.mark.parametrize('operation',['measure.change','measure.batch'])
def test_real_worker_dispatches_measurement_wrapper_once(operation,tmp_path,monkeypatch):
    c=card(operation)
    (tmp_path/'laser.yaml').write_text(yaml.safe_dump(dlc_laser()))
    (tmp_path/'interface-deck.yaml').write_text(yaml.safe_dump({'id':'fixture','cards':[c]}))
    (tmp_path/'invocation.json').write_text(json.dumps({'card_id':c['id'],'parameters':c['parameters']}))
    import ldcon.agents.operating_mode_client as mode
    monkeypatch.setattr(mode,'observation',lambda _:dict(mode='characterization',revision='mode:1'))
    opened=[]
    monkeypatch.setattr(interface_worker,'open_interfaces',lambda laser,selected:opened.append(selected['operation']) or {})
    calls=[];reports=[]
    def run(directory,laser,selected,p,**kwargs):
        calls.append((selected['operation'],copy.deepcopy(p)))
        r=result()
        if operation=='measure.batch':r['steps']=[result()]
        return r
    monkeypatch.setattr(measurement_execution,'run_measurement',run)
    monkeypatch.setattr(measurement_report,'write_report',report_stub(reports))
    first=interface_worker.run(tmp_path)
    assert first['status']=='succeeded',first
    assert calls[0][0]==operation and len(calls)==1 and opened==[operation]
    assert read_json(tmp_path/'measurement.json',{})['report']['status']=='created'
    assert interface_worker.run(tmp_path)==first and len(calls)==1
