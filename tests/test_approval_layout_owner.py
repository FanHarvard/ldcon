"""Owners and the Web configuration API must use the same host-only receipts."""
import hashlib
import json
from types import SimpleNamespace

import pytest

from ldcon.core.agent_authority import approval_root
from ldcon.owners.ecdl_policy_reload import _approved
from ldcon.core.instrument_client import BrokerError
from ldcon.core.state_layout import root_from_laser_file, SCHEMA


def test_single_laser_owner_reads_exact_host_decision(tmp_path, monkeypatch):
    monkeypatch.delenv('LDCON_AGENT_APPROVALS_ROOT', raising=False)
    root = tmp_path / 'var/lasers/sr422'
    file = root / 'config/lasers/sr422.yaml'
    file.parent.mkdir(parents=True)
    (root / 'laser-layout.json').write_text(json.dumps(dict(schema=SCHEMA, laser_id='sr422')))
    file.write_text('id: sr422\n')
    directory = tmp_path / 'var/agent-approvals/sr422/decisions'
    directory.mkdir(parents=True)
    assert approval_root(root_from_laser_file(file), 'sr422') == directory.parent
    rows = [dict(path='devices.rig.absolute_conditions.axes.temperature.max_step',
                 old=.0005, new=.001, **{'class': 'safety'})]
    identifier = 'a' * 32
    receipt = dict(id=identifier, laser_id='sr422', scope=dict(kind='lasers', name='sr422'),
                   authority='maintainer', revision_after=hashlib.sha256(file.read_bytes()).hexdigest(),
                   changes=rows)
    (directory / (identifier + '.json')).write_text(json.dumps(receipt))
    owner = SimpleNamespace(laser_file=file, laser={'id': 'sr422'})
    assert _approved(owner, {}, rows, dict(decision_id=identifier))['receipt_id'] == identifier
    with pytest.raises(BrokerError, match='exact approved change'):
        _approved(owner, {}, [{**rows[0], 'new': .002}], dict(decision_id=identifier))
    file.write_text('id: sr422\nchanged: true\n')
    with pytest.raises(BrokerError, match='currently saved laser revision'):
        _approved(owner, {}, rows, dict(decision_id=identifier))


def test_idle_adoption_requires_exact_top_level_target_bound_approval(tmp_path, monkeypatch):
    """Device-only diffs used to lose the system's target-bound authorization."""
    from ldcon.owners import ecdl_policy_reload as reload
    from ldcon.core.interface_schema import digest
    old = dict(id='sr422', target=dict(frequency_hz=10.,bounds_hz=[1.,20.]),devices={'rig':{'control_settings':{}}})
    new = dict(old, target=dict(frequency_hz=30.,bounds_hz=[21.,40.]))
    owner = SimpleNamespace(laser=old,device_key='rig',device=old['devices']['rig'],lease=None,
        steady=SimpleNamespace(armed=False),fault=None,laser_file=tmp_path/'laser.yaml',audit=lambda *a,**k:None)
    owner.laser_file.write_text('{}')
    decisions=tmp_path/'decisions';decisions.mkdir();(decisions/('a'*32+'.json')).write_text('{}')
    monkeypatch.setattr(reload,'saved',lambda _:new)
    monkeypatch.setattr('ldcon.core.agent_authority.approval_root',lambda *a:tmp_path)
    checked=[]
    def approved(owner,device,rows,request):
        assert any(r['path']=='target.bounds_hz' and r['old']==[1.,20.] and r['new']==[21.,40.] for r in rows)
        checked.append(request);return {'receipt_id':request['decision_id']}
    monkeypatch.setattr(reload,'_approved',approved)
    monkeypatch.setattr(reload,'prepare_limits',lambda *a:({},lambda:None))
    commits=[];monkeypatch.setattr(reload,'commit',lambda owner,candidate,receipt,*a:commits.append((candidate,receipt)))
    reload.adopt_saved(owner,digest(new['devices']['rig']))
    assert len(checked)==1 and commits[0][1]['receipt_id']=='a'*32
    (decisions/('a'*32+'.json')).unlink();commits.clear()
    with pytest.raises(BrokerError,match='exact trusted decision'):
        reload.adopt_saved(owner,digest(new['devices']['rig']))
    assert not commits
