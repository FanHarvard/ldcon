"""Proxy-to-real-owner protocol loop and honest full wavelength analysis."""
from copy import deepcopy
import pytest

from .test_dlcpro_offline import rig
from .test_dlcpro_awg_protocol import prepared
from .test_dlcpro_awg import request
from ldcon.jobs.dlcpro_awg_card import run, parameters, envelope
from ldcon.jobs.dlcpro_awg_analysis import analyse
from ldcon.core.operation_registry import admission_class, normalize_parameters


def card():
    return dict(id='frequency-staircase', operation='sweep.dlcpro_awg', uses=[],
        parameters=dict(shot_sweep=None,pzt_ff_gain_scale=1.,sample_interval_s=.025))


def test_registered_without_exclusive_lock_claim():
    c=card()
    assert admission_class({},c)=='proxy'
    assert normalize_parameters(c,{})['shot_sweep'] is None


def test_web_admits_attached_dlc_sweep_without_legacy_modulation_binding(monkeypatch,tmp_path):
    """BLACS /start must reach allocation, without claiming the parent slot."""
    from types import SimpleNamespace as NS
    from ldcon.web import web_runtime as web
    class ReachedAllocation(Exception):
        pass
    class Allocation:
        def __truediv__(self,key):return self
        def mkdir(self,**kw):raise ReachedAllocation()
    c=card()
    value=dict(id='set',laser_definition={'document':{'id':'laser'}},
        deck_definitions=[dict(id='deck',document={'cards':[c]},invocation_parameters={})])
    runtime=NS(root=tmp_path,assert_admission_open=lambda:None,
        assert_laser_policy_ready=lambda laser:None,job_headers=lambda **kw:[],
        processes={'lock':NS(poll=lambda:None)},max_concurrent_jobs=12,
        directory=lambda job:tmp_path/job)
    monkeypatch.setattr(web,'readiness',lambda *args:dict(runnable=True,parameters={'shot_sweep':request()}))
    monkeypatch.setattr(web,'read_json',lambda *args:dict(set_id='set',laser={'id':'laser'},
        cards=[{'card':'servo.dlcpro_frequency'}]))
    monkeypatch.setattr(web,'job_root',lambda *args:Allocation())
    with pytest.raises(ReachedAllocation):
        web.WebRuntime._start_interface_card(runtime,value,'deck','frequency-staircase',{})


def test_proxy_runs_with_real_attached_dispatch_and_no_parent_lease_change(rig,monkeypatch,tmp_path):
    o,p,_=prepared(rig,monkeypatch,tmp_path/'owner')
    laser=dict(allow_output=True,interfaces={'dlc_lock':{'device':'dlc'}},devices={'dlc':p.device})
    calls=[];pub=[];started=rig['clock']()
    class Session:
        def __init__(self,device):pass
        def rpc(self,action,**kw):
            calls.append(action)
            result = p.dispatch(dict(action=action,laser_id=o.laser_id,**kw))
            if action == 'awg_sweep_begin':
                assert o.awg_sweep.prepared.wait(2.)
            return result
    def sleep(dt):
        rig['clock'].sleep(dt);o.heartbeat()
        elapsed=rig['clock']()-started
        rig['driver'].values['io:fine-2:value-act']=.1 if .5<=elapsed<.9 else -.1 if 1.3<=elapsed<1.7 else 0.
        if o.awg_sweep.active():o.awg_sweep.tick()
    job=tmp_path/'job';job.mkdir()
    result=run(job,laser,card(),{'shot_sweep':request()},clock=rig['clock'],sleep=sleep,
        cancel=lambda:False,emit=lambda *a:None,publish=pub.append,session_factory=Session)
    assert result['status']=='succeeded' and pub[-1]['outcome']['final'] is True
    assert pub[-1]['outcome']['analysis']['state']=='unavailable'  # fake source has no data
    assert 'claim' not in calls and 'release' not in calls and 'stop' not in calls
    assert p.client=='existing-main-lease' and o.mode=='lock'
    assert sum(x=='awg_sweep_begin' for x in calls)==1


def test_analysis_keeps_raw_bad_quality_points_and_never_claims_unmeasured_settling():
    r=request();samples=[]
    for i in range(1,81):
        t=100+i*.025
        samples.append(dict(sequence=i,frequency_hz=3e14+i*1000,acquired_s=t,
            received_s=t+.01,acquired_unix_s=1000+t,acquisition_known=True,
            quality='source_quality_512',source_id='wsu'))
    result=dict(samples=samples,adc=[],origin_target_hz=3e14,
        trigger_bounds_s=[100,100.02],playback_completed=True,
        source_coverage={'complete':None,'samples':80})
    value=analyse(r,result,'job')
    assert len(value['points']['wavelength_nm'])==80
    assert all(x is not None for x in value['points']['wavelength_nm'])
    assert set(value['points']['settled_level_index'])=={-1}
    assert set(value['points']['in_transition'])=={None}
    assert value['source_rows']==samples
    assert value['quality']['coverage']==result['source_coverage']
    assert any(x['mean_wavelength_nm'] for x in value['levels'])
    assert 'Reconstructed' in value['timing']['receive_unix_time']


def test_analysis_missing_measurements_is_unavailable():
    with pytest.raises(ValueError,match='samples'):
        analyse(request(),{'samples':[],'trigger_bounds_s':[1,2]},'job')


def test_first_sweep_publishes_wavelengths_for_blacs(tmp_path):
    import json
    from ldcon.jobs.dlcpro_awg_analysis import save
    job = tmp_path/'first-job'
    job.mkdir()
    sample = dict(sequence=1,frequency_hz=3e14,acquired_s=100.5,
        received_s=100.51,acquired_unix_s=1100.5,acquisition_known=True,
        quality='valid',source_id='wsu')
    result=dict(samples=[sample],adc=[],origin_target_hz=3e14,
        trigger_bounds_s=[100,100.02],playback_completed=True,
        source_coverage={'complete':True,'samples':1})
    status=save(job,request(),result)
    assert status['state']=='ready'
    data=json.loads((job/'shot-analysis/result.json').read_text())
    assert data['points']['wavelength_nm']==pytest.approx([299792458e9/3e14])
    assert data['source_rows']==[sample]
