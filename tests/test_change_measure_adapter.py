from types import SimpleNamespace

import pytest

from tests.measurement_fixtures import measurement_parameters, measurement_batch

from ldcon.core.contracts import DeviceError
from ldcon.jobs.operating_point import Adapter, OperatingPointQueue, lock_action
from ldcon.jobs.change_measure_adapter import OperatingPointMeasurementAdapter
from ldcon.jobs.change_measure import run


LASER=dict(id='test',interfaces={'lock':{'device':'rig'}},devices={'rig':{'type':'ecdl.rig.owner','control_settings':{}}})


class Clock:
    def __init__(self):self.t=0.
    def __call__(self):return self.t
    def sleep(self,t):self.t+=t


class Port(Adapter):
    def __init__(self):self.current=.5;self.calls=[]
    def policy(self,axis):return SimpleNamespace(low=0.,high=1.,resolution=.001,max_step=.01,max_slew_per_s=.1,update_interval_s=.1,unit='V')
    def readback(self,axis):return self.current
    def move_output(self,axis,value):self.calls.append((axis,value));self.current=value


def make():
    c=Clock();port=Port()
    a=OperatingPointMeasurementAdapter(port,LASER,clock=c,sleep=c.sleep,cancel=lambda:False,emit=lambda *_:None)
    return c,port,a


def test_real_axis_procedure_is_used_and_verifies_readback():
    c,p,a=make()
    result=run(a,measurement_parameters(changes=[{'parameter':'current','offset':.02}],measurements={},duration_s=0.),clock=c,sleep=c.sleep)
    assert result['application']['readback']['current']==pytest.approx(.52)
    assert len(p.calls)==2 and result['status']=='succeeded'


def test_relative_batch_checkpoint_is_checked_again_before_writing():
    c,p,a=make();plan=a.preflight([{'parameter':'current','value':.6}],'ordered',{})
    p.current=.7
    with pytest.raises(DeviceError,match='changed since preflight'):a.apply(plan,lambda:None)
    assert not p.calls


def test_refuses_unavailable_measurement_and_atomic_before_mutation():
    c,p,a=make()
    with pytest.raises(DeviceError):a.preflight([{'parameter':'current','value':.6}],'atomic',{})
    with pytest.raises(DeviceError):a.preflight([], 'ordered', {'dither':True})
    assert not p.calls


def test_lock_dispatch_runs_one_measurement_request_and_preserves_receipt(tmp_path):
    c,p,a=make();queue=OperatingPointQueue(tmp_path)
    rid='a'*32
    queue.submit(dict(request_id=rid,measurement_operation='measure.change',parameters=measurement_parameters(
        changes=[{'parameter':'current','value':.51}],measurements={},duration_s=0.)))
    result=lock_action(queue,lambda:p,LASER,clock=c,sleep=c.sleep,cancel=lambda:False,emit=lambda *_:None)
    assert result['status']=='succeeded' and queue.read_status(rid)['result']['application']['readback']['current']==.51
    assert lock_action(queue,lambda:p,LASER,clock=c,sleep=c.sleep,cancel=lambda:False,emit=lambda *_:None) is None
    assert len(p.calls)==1


def test_unknown_locked_write_preserves_result_and_does_not_continue_parent(tmp_path):
    from ldcon.core.records import read_json
    c,p,a=make();queue=OperatingPointQueue(tmp_path);rid='b'*32
    def uncertain(axis,value):
        p.calls.append((axis,value));raise OSError('lost application receipt')
    p.move_output=uncertain
    queue.submit(dict(request_id=rid,measurement_operation='measure.change',parameters=measurement_parameters(
        changes=[{'parameter':'current','value':.51}],measurements={},duration_s=0.)))
    with pytest.raises(DeviceError):
        lock_action(queue,lambda:p,LASER,clock=c,sleep=c.sleep,cancel=lambda:False,emit=lambda *_:None)
    assert queue.read_status(rid)['result']['physical_outcome']=='unknown'
    assert read_json(tmp_path/'device-review.json')['physical_outcome']=='unknown'
    assert len(p.calls)==1


def test_foreground_target_request_interrupts_measurement_before_any_write(tmp_path):
    from ldcon.jobs.change_measure_adapter import execute_locked
    from ldcon.core.interface_cards import CardCancelled
    c,p,a=make();(tmp_path/'laser-reload-request-other.json').write_text('{}')
    with pytest.raises(CardCancelled):
        execute_locked(dict(request_id='a'*32,measurement_operation='measure.change',parameters=measurement_parameters(
            changes=[{'parameter':'current','value':.51}],measurements={},duration_s=0.)),p,LASER,
            clock=c,sleep=c.sleep,cancel=lambda:False,emit=lambda *_:None,controller_directory=tmp_path)
    assert not p.calls


def test_native_temperature_readiness_does_not_claim_unsupported_axis():
    from ldcon.jobs.change_measure_adapter import readiness
    laser=dict(allow_output=True,interfaces={'native_current':{'device':'native'}},
        devices={'native':{'type':'esp32servocon.native_current.owner','control_settings':{}}})
    issues=readiness(laser,{'operation':'measure.change'},measurement_parameters(changes=[dict(parameter='temperature',value=20.)],
                     measurements={},apply_mode='ordered'))
    assert any('cannot change temperature' in value for value in issues)


def test_target_mixed_batch_and_unavailable_native_parameter_rejected_in_readiness():
    from ldcon.jobs.change_measure_adapter import readiness
    laser=dict(allow_output=True,interfaces={'native_current':{'device':'native'}},
        devices={'native':{'type':'esp32servocon.native_current.owner','control_settings':{}}})
    steps=[measurement_parameters(changes=[dict(parameter=name,value=1.)],measurements={},apply_mode='ordered')
           for name in ('target_frequency_hz','tec_gain')]
    issues=readiness(laser,{'operation':'measure.batch'},measurement_batch(steps))
    assert any('cannot share a batch' in value for value in issues)
    assert any('No same-owner transaction' in value for value in issues)


def test_measurement_incomplete_normalizes_to_terminal_inconclusive():
    from ldcon.jobs.interface_worker import normalize_card_result
    result=normalize_card_result(dict(status='measurement_incomplete',physical_outcome='applied'))
    assert result['status']=='inconclusive' and result['physical_outcome']=='applied'


def test_unlocked_ecdl_measurement_session_never_prepares_or_stops_pi(monkeypatch):
    from ldcon.jobs.change_measure_adapter import open_measurement_ports
    from ldcon.jobs import ecdl_devices
    calls=[]
    class Session:
        def __init__(self,device):self.policies={key:object() for key in ('current','temperature','piezo')}
        def connect(self,*,allow_prepare):
            calls.append(allow_prepare);return self
        def close(self):pass
        def snapshot(self):return {'open_loop':True}
    monkeypatch.setattr(ecdl_devices,'OwnerSession',Session)
    ports,movement,close=open_measurement_ports(LASER,{'id':'observe'}, {})
    assert calls==[False] and set(ports)=={'current','temperature','piezo','loop','observation'}
    close()


@pytest.mark.parametrize('missing', [
    'changes', 'apply_mode', 'measurements', 'baseline_s', 'wait_s',
    'duration_s', 'history_wait_s', 'sample_interval_s', 'minimum_samples',
    'measurement_max_age_s', 'max_sample_gap_s', 'timeout_s',
])
def test_missing_execution_value_rejects_before_owner_or_lock_lookup(tmp_path, monkeypatch, missing):
    from ldcon.jobs import change_measure_adapter as adapter
    def forbidden(*args, **kwargs):
        raise AssertionError('Incomplete measurement plan reached owner or lock lookup')
    monkeypatch.setattr(adapter, 'find_running_lock', forbidden)
    p = measurement_parameters()
    del p[missing]
    with pytest.raises(ValueError, match='Missing explicit change/measurement parameters: ' + missing):
        adapter.run_card(tmp_path, LASER, {'operation': 'measure.change'}, p,
                         clock=lambda: 0., sleep=forbidden, cancel=lambda: False,
                         emit=forbidden, open_ports=forbidden)


@pytest.mark.parametrize('case', ['batch_deadline', 'steps', 'second_step_duration'])
def test_incomplete_batch_rejects_before_first_owner_access(tmp_path, monkeypatch, case):
    from ldcon.jobs import change_measure_adapter as adapter
    def forbidden(*args, **kwargs):
        raise AssertionError('Incomplete batch reached owner or lock lookup')
    monkeypatch.setattr(adapter, 'find_running_lock', forbidden)
    p = measurement_batch([measurement_parameters(), measurement_parameters()])
    if case == 'second_step_duration':
        del p['steps'][1]['duration_s']
    else:
        del p['timeout_s' if case == 'batch_deadline' else 'steps']
    with pytest.raises(ValueError, match='Missing explicit'):
        adapter.run_card(tmp_path, LASER, {'operation': 'measure.batch'}, p,
                         clock=lambda: 0., sleep=forbidden, cancel=lambda: False,
                         emit=forbidden, open_ports=forbidden)
