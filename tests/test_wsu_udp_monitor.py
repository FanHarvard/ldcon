import binascii
import json
import pytest
from ldcon.drivers.wsu_udp import PACKET
from ldcon.owners.wsu_udp_monitor import Monitor,validate,save_status


def config():return dict(listen_host='127.0.0.1',listen_port=5558,source_ip='127.0.0.1',valid_data_index=4,channel=5,cycle=0,state_directory='/tmp/wsu-test')


def packet(seq=1,stamp=100.,channel=5,flags=4):
    raw=PACKET.pack(b'WSU1',1,flags,4,channel,0,0,0,seq,int(stamp*1e9),685.449,0)
    return raw[:-2]+binascii.crc_hqx(raw[:-2],0xffff).to_bytes(2,'little')


def test_monitor_valid_duplicate_and_new_sample():
    m=Monitor(config());r=m.feed(packet(),'127.0.0.1',100.01)
    assert r['packet']['unit']=='nm' and r['packet']['channel']==5
    assert m.feed(packet(flags=6),'127.0.0.1',100.013) is None
    assert m.feed(packet(2,100.05),'127.0.0.1',100.06)
    s=m.snapshot(101.);assert s['unique']==2 and s['duplicates']==1
    assert s['control_enabled'] is False and s['arrival_age_s']==pytest.approx(.94)


def test_monitor_bad_source_route_crc_and_reordering():
    m=Monitor(config())
    assert m.feed(packet(),'192.0.2.1',100) is None
    assert m.feed(packet(channel=4),'127.0.0.1',100) is None
    assert m.feed(packet()[:-1]+b'\0','127.0.0.1',100) is None
    assert m.feed(packet(2,102),'127.0.0.1',102)
    assert m.feed(packet(1,101),'127.0.0.1',103) is None
    assert m.rejected==3 and m.out_of_order==1
    assert m.latest['sequence']==2


def test_monitor_reports_clock_lead_without_claiming_control_freshness():
    m=Monitor(config());assert m.feed(packet(stamp=200),'127.0.0.1',100)
    assert m.snapshot(100)['source_age_s']==-100
    assert m.snapshot(100)['control_enabled'] is False


@pytest.mark.parametrize('key,value',[('listen_host','0.0.0.0'),('listen_port',5557.0),('channel',False),('state_directory','relative')])
def test_route_validation(key,value):
    c=config();c[key]=value
    with pytest.raises(ValueError):validate(c)


def test_atomic_status(tmp_path):
    p=tmp_path/'status.json';save_status(p,{'state':'listening'});save_status(p,{'state':'stopped'})
    assert json.loads(p.read_text())=={'state':'stopped'}
    assert not p.with_suffix('.tmp').exists()


def test_receive_only_service_loopback_and_stop(tmp_path):
    import os,socket,subprocess,sys,time
    from pathlib import Path
    root=(Path(__file__).resolve().parent / 'support')
    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as reserve:
        reserve.bind(('127.0.0.1',0));port=reserve.getsockname()[1]
    c=config();c.update(listen_port=port,state_directory=str(tmp_path/'state'))
    cfg=tmp_path/'config.json';cfg.write_text(json.dumps(c))
    env=dict(os.environ)  # Exercise the installed package in the subprocess.
    proc=subprocess.Popen([sys.executable,'-m','ldcon.owners.wsu_udp_monitor','--config',str(cfg)],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    status=tmp_path/'state/status.json'
    try:
        until=time.monotonic()+4
        while not status.exists() and time.monotonic()<until:time.sleep(.02)
        assert status.exists()
        with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sender:
            for seq in [1,1,2,2]:sender.sendto(packet(seq,100+seq),('127.0.0.1',port))
        until=time.monotonic()+4
        while time.monotonic()<until:
            s=json.loads(status.read_text())
            if s['received']>=4:break
            time.sleep(.05)
        assert s['unique']==2 and s['duplicates']==2
        assert s['control_enabled'] is False and s['state']=='listening'
    finally:
        proc.terminate()
        try:proc.communicate(timeout=4)
        except subprocess.TimeoutExpired:proc.kill();proc.communicate()
    assert proc.returncode==0
    assert json.loads(status.read_text())['state']=='stopped'
    assert len((tmp_path/'state/samples.jsonl').read_text().splitlines())==2
