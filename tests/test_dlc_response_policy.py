from pathlib import Path
import json
import pytest
from ldcon.agents.agent_bundle import build
from ldcon.jobs.response_comparison import improved
from .test_dlcpro_offline import rig,advance


def test_all_agent_profiles_receive_shared_tradeoff_policy(tmp_path):
    for profile in ('adjustment','research','diagnosis'):
        directory=tmp_path/profile;directory.mkdir()
        bundle=build(directory,profile,context={'laser_id':'test'},evidence={})
        assert 'Common response and PI optimization policy' in bundle['instructions']
        assert 'Do not require every metric' in bundle['instructions']
        assert 'both the benefit and the cost' in bundle['instructions']
        assert 'only the frozen card' in bundle['instructions']


def test_no_hard_rms_gate_replaces_model_tradeoff():
    before=dict(mse=100.,mse_se=1.,response_loss=10.,peak_hz=25.)
    trial=dict(mse=121.,mse_se=1.,response_loss=5.,peak_hz=26.)
    assert improved(before,trial,before)
    assert not improved(before,{**trial,'response_loss':float('nan')},before)


def test_configured_missing_window_holds_and_never_integrates_gap(rig):
    o=rig['owner'];o.policy['measurement_timeout_s']=15.
    o.start_lock(3e14,rig['control']);advance(rig,1)
    count=len(rig['driver'].writes);integral=o.integral
    rig['source'].missing=True;advance(rig,6.3)
    assert o.mode=='lock' and not o.fault
    assert len(rig['driver'].writes)==count and o.integral==integral
    rig['source'].missing=False;advance(rig,.1)
    assert o.mode=='lock' and not o.fault
    assert o.integral==pytest.approx(integral)
    rig['source'].missing=True;advance(rig,15.2)
    assert o.mode=='idle' and o.fault=='control frequency deadline exceeded'
