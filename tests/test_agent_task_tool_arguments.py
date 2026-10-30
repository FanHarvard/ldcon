import math
from pathlib import Path

import pytest

from ldcon.agents.agent_tools import Toolbox
from ldcon.core.laser_assets import prepare_write_lock, LaserAssets


def box():
    result=object.__new__(Toolbox)
    result.context={'task_allowed_tools':['background_adjustment_step']}
    result._bridge_id='laser-test'
    return result


def test_real_toolbox_accepts_typed_pi_candidate_before_gateway(monkeypatch):
    monkeypatch.setattr('ldcon.agents.agent_background.call',lambda box,name,args:args)
    value=dict(kp_count_per_nm=-100.,ki_count_per_nm_s=-20000.,integral_limit_count=30.)
    result=box().call('background_adjustment_step',dict(action='begin',family='pi',request_id='test',pi_candidate=value))
    assert result['pi_candidate']==value


@pytest.mark.parametrize('value',[None,'{}',[],{},
    dict(kp_count_per_nm=True,ki_count_per_nm_s=-20000,integral_limit_count=30),
    dict(kp_count_per_nm=-100,ki_count_per_nm_s=-20000,integral_limit_count=math.inf),
    dict(kp_count_per_nm=-100,ki_count_per_nm_s=-20000,integral_limit_count=30,reset=True)])
def test_invalid_structured_candidate_never_reaches_gateway(monkeypatch,value):
    monkeypatch.setattr('ldcon.agents.agent_background.call',lambda *a:pytest.fail('Invalid candidate reached gateway'))
    with pytest.raises(ValueError):
        box().call('background_adjustment_step',dict(action='begin',family='pi',request_id='test',pi_candidate=value))


def test_asset_lock_preparation_preserves_writer_inode_and_cas(tmp_path):
    path=prepare_write_lock(tmp_path,'laser')
    inode=path.stat().st_ino
    assert prepare_write_lock(tmp_path,'laser').stat().st_ino==inode
    assets=LaserAssets(tmp_path,'laser')
    result=assets.write('reports/check.html','<p>Verified</p>','')
    assert assets.read('reports/check.html')['sha256']==result['sha256']
    assert path.stat().st_ino==inode


def test_asset_lock_symlink_and_hardlink_are_not_mountable(tmp_path):
    path=prepare_write_lock(tmp_path,'laser');path.unlink()
    other=tmp_path/'other';other.write_text('private')
    path.symlink_to(other)
    with pytest.raises(OSError):prepare_write_lock(tmp_path,'laser')
    path.unlink();path.hardlink_to(other)
    with pytest.raises(ValueError):prepare_write_lock(tmp_path,'laser')
    assert other.read_text()=='private'


@pytest.mark.parametrize('family,value',[('slow_loop',dict(tec_gain=.1,piezo_center_dac=30000.)),
                                        ('feedforward',dict(current_per_temperature=1.1))])
def test_parameter_candidates_reach_gateway_through_real_toolbox(monkeypatch,family,value):
    monkeypatch.setattr('ldcon.agents.agent_background.call',lambda box,name,args:args)
    result=box().call('background_adjustment_step',dict(action='begin',family=family,request_id='test',parameter_candidate=value))
    assert result['parameter_candidate']==value


@pytest.mark.parametrize('family,value',[('slow_loop',{}),('slow_loop',dict(tec_gain=True)),
    ('slow_loop',dict(current_per_temperature=1.)),('feedforward',dict(tec_gain=.1)),
    ('feedforward',dict(current_per_temperature=float('nan'))),('pi',dict(tec_gain=.1))])
def test_invalid_parameter_candidates_do_not_reach_gateway(monkeypatch,family,value):
    monkeypatch.setattr('ldcon.agents.agent_background.call',lambda *a:pytest.fail('Invalid candidate reached gateway'))
    with pytest.raises(ValueError):
        box().call('background_adjustment_step',dict(action='begin',family=family,request_id='test',parameter_candidate=value))
