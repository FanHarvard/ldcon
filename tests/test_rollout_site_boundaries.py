"""Site integration: clock preservation and non-disclosure of signing material."""
import json
import pytest
from ldcon.agents.agent_sandbox import command
from ldcon.owners.instrument_owner import validate_config, definition_digest, parse_rosc


def test_private_key_mount_is_masked_after_broad_home_binding(tmp_path,monkeypatch):
    private=tmp_path/'keys';private.mkdir()
    monkeypatch.setenv('LDCON_PRIVATE_ROOTS',json.dumps([str(private)]))
    monkeypatch.setenv('HOME',str(tmp_path))   # a real ~/.local/state/ldcon/gateway-access would add its own tmpfs after ours
    argv=command(['agent'],[tmp_path])
    mask=argv.index(str(private));bind=argv.index('--bind')
    assert mask>bind and argv[mask-1]=='--tmpfs' and argv[mask+1]=='--'
    assert '--unshare-pid' in argv


def test_private_root_cannot_be_missing_or_global(tmp_path,monkeypatch):
    for path in ('/','/opt',str(tmp_path/'absent')):
        monkeypatch.setenv('LDCON_PRIVATE_ROOTS',json.dumps([path]))
        with pytest.raises(ValueError,match='Private root'):
            command(['agent'],[tmp_path])


def test_monitor_retains_all_migrated_lock_domains(monkeypatch):
    from ldcon.owners.monitor_owner import ResourceLock
    from ldcon.owners import migration_physical_lock
    calls=[]
    class Held:
        def close(self):calls.append('closed')
    def acquire(host,port,kinds):
        calls.append((host,port,kinds));return Held()
    monkeypatch.setattr(migration_physical_lock,'acquire_endpoint',acquire)
    lock=ResourceLock('192.0.2.7',80);lock.close()
    assert calls==[('192.0.2.7',80,('physical','device')),'closed']
    ResourceLock('192.0.2.8',5555,('device',)).close()
    assert calls[-2:]==[('192.0.2.8',5555,('device',)),'closed']


def test_agent_registration_uses_explicit_public_binding(tmp_path,monkeypatch):
    from ldcon.agents.resident_agent import register_bridge
    from ldcon.agents import agent_tools
    monkeypatch.setenv('LDCON_AGENTBRIDGE_PUBLIC',str(tmp_path))
    class Box:
        def __init__(self,context):assert context=='test-context'
        def call(self,name,args):assert name=='bridge_register';return '{"id":"laser-test"}'
    monkeypatch.setattr(agent_tools,'Toolbox',Box)
    assert register_bridge(dict(tools_context='test-context',bridge_id='laser-test'))['workspace']==str(tmp_path)


def test_clock_settings_and_projection_bindings_coexist(tmp_path):
    cfg=dict(instrument='sdg2042x:SYNTHETIC',kind='awg',host='offline.invalid',profile='siglent_sdg2000x',
             expected_identity='Siglent Technologies,SDG2042X,SYNTHETIC,1',
             channels={'1':{'writes_enabled':True}},bind='127.0.0.1',port=1,
             state_dir=str(tmp_path/'state'),lock_dir=str(tmp_path/'locks'),
             capabilities=dict(voltage_bounds_v=[-10,10],max_vpp=20,max_points=8_000_000),
             projection_sources={'laser':dict(system_id='laser',key_env='OFFLINE_KEY',channels=[1])})
    before=definition_digest(validate_config(cfg))
    cfg['instrument_settings']=dict(mode='enforce',check_interval_s=60,items=[
        dict(name='reference_clock_source',write='ROSC EXT',query='ROSC?',expect={'source':'EXT'})])
    assert definition_digest(validate_config(cfg))==before
    assert parse_rosc('ROSC EXT')[0]['source']=='EXT'
    cfg['instrument_settings']['items'][0]['write']='C1:BSWV OFST,1'
    with pytest.raises(ValueError):validate_config(cfg)
