"""Maintenance preparation and failure-order tests; never uses live adapters."""
import copy
import hashlib
import json
from pathlib import Path
import socket

import pytest

from ops.update_pi_handover import (PHASES, apply_edits, check_approval, execute,
                                   held_config, persistent_equal, relay_idle, sha, Host)
from ops.prepare_pi_handover import verify


@pytest.fixture(autouse=True)
def no_hardware(monkeypatch):
    def denied(*args,**kwargs):raise AssertionError('No network/device access in tests')
    monkeypatch.setattr(socket,'create_connection',denied)
    monkeypatch.setattr(socket.socket,'connect',denied)


class ModelHost:
    def __init__(self,fail=None):self.calls=[];self.fail=fail;self.closed=False
    def __getattr__(self,name):
        assert name in PHASES
        def phase():
            self.calls.append(name)
            if self.fail==name:raise RuntimeError('synthetic failure')
        return phase
    def close(self):self.closed=True


@pytest.mark.parametrize('phase',PHASES)
def test_every_uncertain_phase_stops_without_replay_or_restart(tmp_path,phase):
    host=ModelHost(phase);record=tmp_path/'run'
    with pytest.raises(RuntimeError):execute(host,record)
    assert host.calls==list(PHASES[:PHASES.index(phase)+1])
    assert host.closed
    assert json.loads((record/'FAILED.json').read_text())['phase']==phase
    second=ModelHost()
    with pytest.raises(FileExistsError):execute(second,record)
    assert second.calls==[]


def test_success_has_ordered_journal_no_card_restart(tmp_path):
    h=ModelHost();execute(h,tmp_path/'run')
    assert h.calls==list(PHASES) and h.closed
    assert len(list((tmp_path/'run').glob('*-done.json')))==len(PHASES)


def test_only_approved_leaves_and_revision_are_applied():
    original={'gain':2,'target':50,'nested':{'version':'old','other':9}}
    edits=[{'keys':['nested','version'],'before':'old','after':'new'}]
    result=apply_edits(original,edits)
    assert result=={'gain':2,'target':50,'nested':{'version':'new','other':9}}
    assert original['nested']['version']=='old'
    with pytest.raises(ValueError):apply_edits(result,edits)


def test_approval_bound_to_exact_bundle_and_time(tmp_path):
    bundle=tmp_path/'bundle';bundle.write_text('{}')
    a=dict(bundle_sha256=sha(bundle),expires_unix_s=1100,human_instruction='Approved exact Sr422 update',
           flash_sr422=True,stop_sr422_services=True,restore_saved_configuration=True,
           apply_config_differences=True,start_sr422_services=True,resume_cards=False)
    path=tmp_path/'approval';path.write_text(json.dumps(a));check_approval(path,bundle,1000)
    for key,value in [('flash_sr422',False),('expires_unix_s',900),('expires_unix_s',float('nan')),
                      ('bundle_sha256','bad'),('resume_cards',True),('human_instruction','')]:
        changed=dict(a);changed[key]=value;path.write_text(json.dumps(changed))
        with pytest.raises(ValueError):check_approval(path,bundle,1000)


def test_firmware_source_drift_blocks_reconstruction(tmp_path):
    p=tmp_path/'source';p.write_text('before');manifest={'source':sha(p)}
    verify(tmp_path,manifest);p.write_text('peer changed')
    with pytest.raises(ValueError):verify(tmp_path,manifest)


def sample_held():
    return {'fault':None,'lease_retained':False,'snapshot':{
        'servo_config':dict(pid_multiplier=0,pid_reset_state=0,multiplier_target=0,
                            multiplier_control=0,software_modulation_enable=0,control_offset=30000),
        'servo_status':{'dac_val':30000},'outputs':{'piezo':30000}}}


def test_held_requires_actual_pi_off_and_readback():
    h=sample_held();assert held_config(h)['control_offset']==30000
    for key in ('pid_multiplier','pid_reset_state','multiplier_target','multiplier_control','software_modulation_enable'):
        v=copy.deepcopy(h);v['snapshot']['servo_config'][key]=1
        with pytest.raises(ValueError):held_config(v)
    v=copy.deepcopy(h);v['snapshot']['servo_status']['dac_val']=30001
    with pytest.raises(ValueError):held_config(v)
    v=copy.deepcopy(h);v['lease_retained']=True
    with pytest.raises(ValueError):held_config(v)


def test_nvs_change_blocks_restore():
    old={'dac':{'scale':1},'storage_error':'ESP_OK'}
    persistent_equal(old,old,('dac',))
    with pytest.raises(ValueError):persistent_equal(old,dict(old,dac={'scale':2}),('dac',))


def test_idle_legacy_fault_is_recorded_not_mistaken_for_sr422_fault():
    status={'instruments':{'legacy':{'leases':[],'fault':'historical disconnected read timeout'}}}
    relay_idle(status)
    assert status['instruments']['legacy']['fault']
    status['instruments']['legacy']['leases']=[{'owner':'another client'}]
    with pytest.raises(ValueError,match='leases'):relay_idle(status)
    held=sample_held();held['fault']='Sr422 output unconfirmed'
    with pytest.raises(ValueError,match='clean'):held_config(held)


def test_web_readiness_retries_reads_only_and_never_accepts_active_cards(tmp_path,monkeypatch):
    h=Host({},tmp_path/'bundle',tmp_path);calls=[]
    def api(*args):
        calls.append(args)
        if len(calls)==1:raise ConnectionRefusedError('listener starting')
        return {'jobs':[]}
    h.api=api;monkeypatch.setattr('ops.update_pi_handover.time.sleep',lambda _:None)
    h.wait_web_idle();assert len(calls)==2
    h.api=lambda *args:{'jobs':[{'status':'running'}]}
    with pytest.raises(ValueError,match='unexpected card'):h.wait_web_idle()


def test_flash_identity_and_partition_fail_before_write(tmp_path):
    h=Host({'device_mac':'ec:c9:ff:b8:bc:20','artifacts':{'partition_table':{'path':'part.bin'}}},tmp_path/'bundle.json',tmp_path)
    (tmp_path/'part.bin').write_bytes(b'correct')
    calls=[]
    def tool(op,args,**kwargs):
        calls.append(op)
        if op=='read_mac':return 'MAC: 11:22:33:44:55:66'
        raise AssertionError('Wrong MAC must stop before backup/write')
    h.tool=tool
    with pytest.raises(ValueError):h.flash()
    assert calls==['read_mac']
    calls.clear()
    def tool2(op,args,**kwargs):
        calls.append(op)
        if op=='read_mac':return 'MAC: ec:c9:ff:b8:bc:20'
        if op=='read_flash':Path(args[-1]).write_bytes(bytes(0x200000));return ''
        raise AssertionError('Bad partition must not write')
    h.tool=tool2
    with pytest.raises(ValueError):h.flash()
    assert calls==['read_mac','read_flash']


@pytest.mark.parametrize('peer_changed',[False,True])
def test_flash_writes_only_app_token_and_verifies_once(tmp_path,peer_changed):
    artifacts={}
    for name,data in [('partition_table',b'table'),('app',b'app'),('mode_reset',b'normal')]:
        p=tmp_path/(name+'.bin');p.write_bytes(data);artifacts[name]={'path':p.name,'sha256':sha(p)}
    h=Host({'device_mac':'ec:c9:ff:b8:bc:20','artifacts':artifacts,
            'source_app':{'bytes':4,'sha256':hashlib.sha256(bytes(4)).hexdigest()}},tmp_path/'bundle.json',tmp_path)
    calls=[]
    def tool(op,args,**kw):
        calls.append((op,args,kw))
        if op=='read_mac':return 'MAC: ec:c9:ff:b8:bc:20'
        if op=='read_flash':
            backup=bytearray(0x200000);backup[0x8000:0x8005]=b'table';Path(args[-1]).write_bytes(backup)
            if peer_changed:
                backup[0x10000]=1;Path(args[-1]).write_bytes(backup)
        return ''
    h.tool=tool
    if peer_changed:
        with pytest.raises(ValueError,match='do not overwrite peer changes'):h.flash()
        assert [x[0] for x in calls]==['read_mac','read_flash']
        return
    h.flash()
    assert [x[0] for x in calls]==['read_mac','read_flash','write_flash','verify_flash']
    assert '0x10000' in calls[2][1] and '0x110000' in calls[2][1]
    assert '0x8000' not in calls[2][1] and 'erase_flash' not in str(calls)
    assert calls[-1][2]['after']=='hard_reset'


@pytest.mark.parametrize('uncertain',[False,True])
def test_restore_writes_once_and_never_enables_pi(tmp_path,monkeypatch,uncertain):
    from ldcon.owners.pi_handover import FIELDS
    import websocket
    h=Host({'device_host':'192.168.1.9','firmware_version':'candidate'},tmp_path/'bundle.json',tmp_path)
    h.cfg={**sample_held()['snapshot']['servo_config'],
           'log_full_data_buffer_size':1024,'log_elapsed_buffer_size':128}
    current={k:0 for k in FIELDS};current.update(h.cfg,pi_handover_capability=1,
               pi_handover_boot_id='mock-boot',pi_handover_revision=1)
    current['control_offset']=0
    h.analog=dict(dac=1,adc=2,saved=True,mode='normal',storage_error='ESP_OK')
    h.limits=dict(min_code=0,max_code=65535,saved=True,persistent=True,min_voltage=0,max_voltage=10,storage_error='ESP_OK')
    h.inactive=lambda:None
    def read(route):
        if route=='/config':return dict(current)
        if route=='/status':return dict(firmware_version='candidate',dac_val=current['control_offset'])
        return h.analog if route=='/analog/config' else h.limits
    h.device=read;sent=[]
    class WS:
        def send(self,text):
            values=json.loads(text);sent.append(values);current.update(values)
            if current.get('pid_reset_state')==1:current['pid_reset_state']=0
        def recv(self):
            if uncertain:raise OSError('reply lost')
            return '{}'
        def close(self):pass
    monkeypatch.setattr(websocket,'create_connection',lambda *a,**k:WS())
    if uncertain:
        with pytest.raises(OSError):h.restore()
        assert len(sent)==1 and not (tmp_path/'restore-proof.json').exists()
    else:
        h.restore();assert len(sent)==3 and (tmp_path/'restore-proof.json').exists()
        assert all(current[k]==v for k,v in h.cfg.items())
    assert all(x.get('pid_multiplier',0)==0 for x in sent)
