"""Coordinated Sr422 firmware + ldcon rollout; default is an OFFLINE plan.

Execution needs a release-bound bundle, a separately recorded human approval,
an unused journal directory, and already-stopped cards. Never starts a card,
retries a write, erases NVS or rolls firmware back automatically.
"""
import argparse
import copy
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess
import sys
import time
import urllib.request
import zipfile

RIG='ldcon-prepared-instruments.service'
WEB='ldcon-prepared-sr422-web.service'
JOBS='ldcon-prepared-sr422-jobs.service'
UNITS=(WEB,JOBS,RIG)
ROOT=Path('/opt/ldcon/var/lasers')
CONFIG=ROOT/'sr422/config/lasers/sr422.yaml'
ASSIGNMENT=ROOT/'sr422/config/assignments/sr422.yaml'
PHASES=('preflight','quiesce','capture','flash','restore','configure','promote','start','verify')


def require(value,message):
    if not value:raise ValueError(message)


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path,value):
    with Path(path).open('x') as out:
        json.dump(value,out,indent=2,allow_nan=False);out.write('\n');out.flush();os.fsync(out.fileno())


def check_bundle(path):
    b=json.loads(path.read_text());base=path.parent
    require(b['schema_version']==1 and b['laser_id']=='sr422' and b['resume_cards'] is False,'Wrong rollout scope')
    require(b['device_host']=='192.168.1.9' and b['device_mac']=='ec:c9:ff:b8:bc:20','Wrong Sr422 identity')
    require(b['source_firmware_version']=='2026.10.23-telemetry-batch' and
            b['firmware_version']=='2026.10.28-pi-handover-candidate','Unexpected firmware pair')
    require(set(b['artifacts'])=={'app','mode_reset','partition_table'},'Application-only images required')
    for spec in b['artifacts'].values():
        p=(base/spec['path']).resolve()
        require(p.parent==base.resolve() and sha(p)==spec['sha256'],'Image hash/path mismatch')
    require(set(b['tool_hashes'])=={'update_pi_handover.py','promote.py'},'Missing pinned rollout tools')
    for name,digest in b['tool_hashes'].items():require(sha(base/name)==digest,'Rollout tool hash mismatch')
    require(sha(__file__)==b['tool_hashes']['update_pi_handover.py'],'Run the pinned updater')
    expected={str(CONFIG):{('devices','rig','control_settings','steady_lock','firmware_version')},
        str(ASSIGNMENT):{('decks',0,'parameters','agent-monitor','prompt'),
            ('decks',0,'parameters','agent-monitor','background','pid_parameters','strategy'),
            ('decks',0,'parameters','agent-adjust','prompt')}}
    require(len(b['configuration'])==2,'Exactly two configuration documents required')
    for item in b['configuration']:
        require(item['path'] in expected and {tuple(e['keys']) for e in item['edits']}==expected.pop(item['path']),
                'Unexpected configuration difference')
    require(not expected,'Missing configuration difference')
    require('ldcon/owners/pi_handover.py' in b['required_wheel_members'], 'Missing host protocol implementation')
    if b['release']:
        release=Path(b['release']).resolve()
        require(release.parent==Path('/opt/ldcon/releases'),'Use the standard release directory')
        source=json.loads((release/'SOURCE.json').read_text())
        require(re.fullmatch('[0-9a-f]{40}',source['sha']) and (release/'.venv/bin/python').is_file(),'Committed release required')
        members={}
        for name,digest in source['wheels'].items():
            require(Path(name).name==name,'Invalid wheel path')
            wheel=release/'dist'/name;require(sha(wheel)==digest,'Wheel hash mismatch')
            with zipfile.ZipFile(wheel) as z:
                for member in b['required_wheel_members']:
                    if member in z.namelist():members[member]=hashlib.sha256(z.read(member)).hexdigest()
        require(members==b['required_wheel_members'],'Release does not contain the reviewed host implementation')
        for member,digest in members.items():
            installed=list((release/'.venv/lib').glob('python*/site-packages/'+member))
            require(len(installed)==1 and sha(installed[0])==digest,'Installed release differs from its wheel')
    return b


def check_approval(path,bundle_path,now):
    a=json.loads(path.read_text())
    require(a.get('bundle_sha256')==sha(bundle_path),'Approval belongs to a different bundle')
    for key in ('flash_sr422','stop_sr422_services','restore_saved_configuration',
                'apply_config_differences','start_sr422_services'):
        require(a.get(key) is True,'Missing explicit approval: '+key)
    require(a.get('resume_cards') is False,'This updater never starts cards')
    expiry=a.get('expires_unix_s')
    require(type(expiry) in (int,float) and math.isfinite(expiry) and now<expiry<=now+21600,'Approval expired or too broad')
    require(isinstance(a.get('human_instruction'),str) and a['human_instruction'].strip(),'Record the actual human instruction')


def apply_edits(document,edits):
    result=copy.deepcopy(document)
    for edit in edits:
        target=result
        for key in edit['keys'][:-1]:target=target[key]
        key=edit['keys'][-1]
        require(target[key]==edit['before'],'Configuration leaf changed; review a new bundle')
        target[key]=edit['after']
    return result


def held_config(held):
    require(held.get('fault') is None and held.get('lease_retained') is False,'Owner must be clean and unleased')
    snapshot=held['snapshot'];cfg=snapshot['servo_config']
    for k in ('pid_multiplier','pid_reset_state','multiplier_target','multiplier_control','software_modulation_enable'):
        require(cfg[k]==0,'PI/modulation must already be held: '+k)
    require(snapshot['servo_status']['dac_val']==cfg['control_offset']==snapshot['outputs']['piezo'],'Held DAC mismatch')
    return cfg


def persistent_equal(before,after,keys):
    require(after.get('storage_error')=='ESP_OK','Persistent storage error')
    require(all(before[k]==after[k] for k in keys),'Analog calibration / DAC limits changed')


def relay_idle(status):
    # This status enumerates compatibility connections, not the Sr422 rig.
    # An unused, disconnected legacy Sr1092 connection may retain a read fault.
    # Do not clear it or let it mask the independent Sr422 inspect/fault checks.
    require(not status.get('simulated'),'Refuse simulated production owner')
    require(all(not x.get('leases') for x in status['instruments'].values()),
            'Shared broker still holds leases')


def execute(host,record):
    record.mkdir(parents=True,exist_ok=False)
    try:
        for phase in PHASES:
            save(record/(phase+'-intent.json'),dict(phase=phase,unix_s=time.time()))
            getattr(host,phase)()
            save(record/(phase+'-done.json'),dict(phase=phase,unix_s=time.time()))
    except BaseException as error:
        save(record/'FAILED.json',dict(phase=phase,error_type=type(error).__name__,
             action='Inspect evidence; no replay, rollback or card restart was attempted'))
        raise
    finally:host.close()


class Host:
    def __init__(self,bundle,path,record):
        self.b=bundle;self.base=path.parent;self.record=record;self.endpoint_lock=None
        self.original_manifest=os.environ.get('LDCON_PHYSICAL_LOCK_MANIFEST')
        self.http=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.env=dict(os.environ,XDG_RUNTIME_DIR='/run/user/'+str(os.getuid()),
                      DBUS_SESSION_BUS_ADDRESS='unix:path=/run/user/'+str(os.getuid())+'/bus')

    def command(self,args,name=None,timeout=90):
        result=subprocess.run(args,env=self.env,capture_output=True,text=True,timeout=timeout)
        if name:(self.record/(name+'.log')).write_text(result.stdout+result.stderr)
        require(result.returncode==0,'Command failed; inspect journal'+(' '+name if name else ''))
        return result.stdout

    def unit(self,unit):
        raw=self.command(['systemctl','--user','show',unit,'-p','ActiveState,MainPID,ExecStart'])
        return dict(line.split('=',1) for line in raw.splitlines() if '=' in line)

    def inactive(self):
        for unit in UNITS:
            state=self.unit(unit)
            require(state.get('ActiveState')=='inactive' and state.get('MainPID')=='0','Service not cleanly inactive: '+unit)

    def request(self,url,body=None,headers=None):
        req=urllib.request.Request(url,data=None if body is None else json.dumps(body).encode(),
            headers=headers or {'Content-Type':'application/json'})
        with self.http.open(req,timeout=15) as response:return json.load(response)

    def api(self,laser,suffix):
        server=json.loads((ROOT/laser/'web-server.json').read_text())
        return self.request('http://127.0.0.1:%d%s/api/%s'%(server['port'],server['path'].rstrip('/'),suffix),
                            headers={'X-LDCon-Client':'web'})

    def rpc(self,**body):
        value=self.request('http://192.168.1.5:47130/rpc',body)
        require(value.get('ok') is True,'Owner RPC rejected');return value['result']

    def device(self,route):return self.request('http://'+self.b['device_host']+route)

    def preflight(self):
        require(pwd.getpwuid(os.getuid()).pw_name=='FanLabAdmin','Run under FanLabAdmin user service manager')
        require(str(Path(sys.executable).absolute())==str(Path(self.b['release'])/'.venv/bin/python'),
                'Run the updater with the matched release interpreter')
        require(Path(self.b['esptool_python']).is_file(),'ESP-IDF esptool environment missing')
        require(self.b['serial_port'].startswith('/dev/serial/by-path/') and stat.S_ISCHR(Path(self.b['serial_port']).stat().st_mode),'Stable serial device path required')
        # This compatibility broker serves both lasers. Do not stop the peer:
        # refuse this rollout while any card could use the shared relay.
        for laser in ('sr422','sr1092'):
            require(not self.api(laser,'jobs?active_only=true')['jobs'],laser+' still has active cards; normal stop is separate')
        self.before_units={u:self.unit(u) for u in UNITS}
        require(all(s['ActiveState']=='active' for s in self.before_units.values()),'Expected active service inventory changed')
        save(self.record/'units-before.json',self.before_units)
        # Inherit only the exclusion-manifest path from the actual rig process.
        # Never log its environment (it also contains credentials).
        entries=Path('/proc/'+self.before_units[RIG]['MainPID']+'/environ').read_bytes().split(b'\0')
        key=b'LDCON_PHYSICAL_LOCK_MANIFEST='
        paths=[e[len(key):].decode() for e in entries if e.startswith(key)]
        require(len(paths)==1 and Path(paths[0]).is_file(),'Rig exclusion manifest unavailable')
        self.lock_manifest=paths[0]

        status=self.rpc(action='status')
        relay_idle(status)
        save(self.record/'compatibility-status-before.json',status)
        self.held=self.rpc(action='ecdl',instrument='sr422',request={'action':'inspect'})
        self.cfg=held_config(self.held)
        require(self.held['snapshot']['servo_status']['firmware_version']==self.b['source_firmware_version'],'Source firmware mismatch')
        save(self.record/'held.json',self.held)
        import yaml
        self.configs=[]
        for item in self.b['configuration']:
            path=Path(item['path']);raw=path.read_bytes();updated=apply_edits(yaml.safe_load(raw),item['edits'])
            self.configs.append((path,raw,updated))
        # Check import before stopping services; no endpoint lock acquired yet.
        from ldcon.owners.migration_physical_lock import acquire_endpoint
        self.acquire_endpoint=acquire_endpoint

    def quiesce(self):
        for unit in (WEB,JOBS):self.command(['systemctl','--user','stop',unit])
        # Read frozen worker records after closing admission, not the stopped Web API.
        for laser in ('sr422','sr1092'):
            for progress in (ROOT/laser/'jobs').glob('*/progress.json'):
                require(json.loads(progress.read_text()).get('status') not in ('running','queued','starting'),
                        'A card appeared during maintenance admission closure')
        fresh=self.rpc(action='ecdl',instrument='sr422',request={'action':'inspect'})
        require(held_config(fresh)==self.cfg and fresh['snapshot']['outputs']==self.held['snapshot']['outputs'],'Held state changed')
        status=self.rpc(action='status')
        relay_idle(status)
        self.command(['systemctl','--user','stop',RIG]);self.inactive()
        os.environ['LDCON_PHYSICAL_LOCK_MANIFEST']=self.lock_manifest
        self.endpoint_lock=self.acquire_endpoint(self.b['device_host'],80)

    def capture(self):
        self.inactive()
        require(self.device('/config')==self.cfg,'Configuration changed after owner shutdown')
        status=self.device('/status')
        require(status['firmware_version']==self.b['source_firmware_version'] and status['dac_val']==self.cfg['control_offset'],'Firmware/held output changed')
        require(status.get('step_test_state')!='running','Step test active')
        self.analog=self.device('/analog/config');self.limits=self.device('/dac/limits')
        require(self.analog.get('mode')=='normal' and self.analog.get('storage_error')=='ESP_OK','Analog mode/storage not normal')
        require(self.limits.get('persistent') is True and self.limits.get('storage_error')=='ESP_OK','DAC limits not persistent')
        save(self.record/'device-before.json',dict(config=self.cfg,status=status,analog=self.analog,limits=self.limits))

    def tool(self,operation,args,after='no_reset',before='no_reset'):
        self.inactive()
        return self.command([self.b['esptool_python'],'-m','esptool','--chip','esp32','--port',self.b['serial_port'],
             '--before',before,'--after',after,operation,*args],name='esptool-'+operation,timeout=240)

    def flash(self):
        text=self.tool('read_mac',[],before='default_reset')
        macs=re.findall(r'(?i)(?:MAC|Base MAC):\s*([0-9a-f:]{17})',text)
        require(self.b['device_mac'] in [m.lower() for m in macs],'Physical ESP32 MAC mismatch')
        backup=self.record/'flash-before.bin';self.tool('read_flash',['0','0x200000',str(backup)])
        raw=backup.read_bytes();require(len(raw)==0x200000,'Incomplete flash backup')
        part=(self.base/self.b['artifacts']['partition_table']['path']).read_bytes()
        require(raw[0x8000:0x8000+len(part)]==part,'Partition table mismatch; app-only flashing refused')
        old=self.b['source_app']
        require(type(old['bytes']) is int and 0<old['bytes']<=0x100000,'Invalid source image size')
        require(hashlib.sha256(raw[0x10000:0x10000+old['bytes']]).hexdigest()==old['sha256'],
                'Installed firmware differs from the reviewed baseline; do not overwrite peer changes')
        save(self.record/'flash-before.sha256.json',dict(sha256=sha(backup)))
        for spec in self.b['artifacts'].values():require(sha(self.base/spec['path'])==spec['sha256'],'Image changed before flashing')
        images=['0x10000',str(self.base/self.b['artifacts']['app']['path']),
                '0x110000',str(self.base/self.b['artifacts']['mode_reset']['path'])]
        self.tool('write_flash',['--flash_mode','dio','--flash_size','2MB','--flash_freq','40m',*images])
        self.tool('verify_flash',images,after='hard_reset')

    def wait_config(self,expected):
        deadline=time.monotonic()+15
        while True:
            value=self.device('/config')
            require(value.get('pid_multiplier')==0,'PI unexpectedly enabled')
            if all(value.get(k)==v for k,v in expected.items()):return value
            require(time.monotonic()<deadline,'Config readback mismatch; never resend')
            time.sleep(.2)

    def restore(self):
        self.inactive()
        deadline=time.monotonic()+60
        while True:
            try:status=self.device('/status');break
            except OSError:
                require(time.monotonic()<deadline,'Firmware did not return');time.sleep(.5)
        require(status.get('firmware_version')==self.b['firmware_version'],'Wrong firmware after programming')
        cfg=self.device('/config')
        from ldcon.owners.pi_handover import capability
        capability(cfg)
        require(cfg['pid_multiplier']==0,'Firmware did not boot PI-disabled')
        require(self.device('/analog/config').get('mode')=='normal','Unexpected analog mode after reboot')
        persistent_equal(self.analog,self.device('/analog/config'),('dac','adc','saved'))
        persistent_equal(self.limits,self.device('/dac/limits'),('min_code','max_code','saved','persistent','min_voltage','max_voltage'))
        def once(values,name):
            self.inactive();save(self.record/(name+'-intent.json'),values)
            import websocket
            ws=websocket.create_connection('ws://'+self.b['device_host']+'/ws',timeout=15,http_proxy_host=None,http_no_proxy=[self.b['device_host']])
            try:ws.send(json.dumps(values));reply=json.loads(ws.recv())
            finally:ws.close()
            save(self.record/(name+'-reply.json'),reply)
            require(isinstance(reply,dict) and 'error' not in reply,'Uncertain configuration write; no resend')
            expected=dict(values)
            if expected.get('pid_reset_state')==1:expected['pid_reset_state']=0
            self.wait_config(expected)
        once(dict(control_offset=self.cfg['control_offset'],multiplier_control=0,multiplier_target=0,
                  software_modulation_enable=0,pid_multiplier=0),'restore-held')
        sizes={k:self.cfg[k] for k in ('log_full_data_buffer_size','log_elapsed_buffer_size')}
        if any(cfg.get(k)!=v for k,v in sizes.items()):once(sizes,'restore-buffer-sizes')
        once(self.cfg,'restore-config');once(dict(pid_reset_state=1,pid_multiplier=0),'reset-integral')
        self.wait_config(self.cfg)
        require(self.device('/status')['dac_val']==self.cfg['control_offset'],'DAC restoration unverified')
        save(self.record/'restore-proof.json',dict(pi_enabled=False,firmware=self.b['firmware_version'],configuration_verified=True))

    def configure(self):
        self.inactive()
        import yaml
        # Recheck both documents before the first replacement, preserving other leaves.
        for path,raw,_ in self.configs:require(path.read_bytes()==raw,'Configuration changed during rollout')
        for path,raw,updated in self.configs:
            save(self.record/(path.parent.name+'-backup.json'),dict(original_yaml=raw.decode(),sha256=hashlib.sha256(raw).hexdigest()))
            tmp=path.with_name(path.name+'.pi-handover-new')
            with tmp.open('x') as out:
                out.write(yaml.safe_dump(updated,sort_keys=False));out.flush();os.fsync(out.fileno())
            os.chmod(tmp,stat.S_IMODE(path.stat().st_mode))
            require(path.read_bytes()==raw,'Configuration changed before replacement');os.replace(tmp,path)
            require(yaml.safe_load(path.read_text())==updated,'Saved configuration mismatch')

    def promote(self):
        self.inactive()
        for unit in UNITS:
            self.command([sys.executable,str(self.base/'promote.py'),unit,self.b['release'],'--consolidate'],name='promote-'+unit)

    def start(self):
        self.close()  # Let the normal owner acquire the hardware endpoint.
        self.command(['systemctl','--user','start',RIG])
        deadline=time.monotonic()+60
        while True:
            try:held=self.rpc(action='ecdl',instrument='sr422',request={'action':'inspect'});break
            except (OSError,ValueError):
                require(time.monotonic()<deadline,'New owner did not become inspectable');time.sleep(.5)
        held_config(held)
        require(held['snapshot']['outputs']==self.held['snapshot']['outputs'],'Held outputs changed across update')
        require(held['snapshot']['servo_status']['firmware_version']==self.b['firmware_version'],'Owner sees a different firmware')
        save(self.record/'owner-after.json',held)
        for unit in (JOBS,WEB):self.command(['systemctl','--user','start',unit])

    def wait_web_idle(self):
        # systemd Type=simple becomes active before the HTTP listener is ready.
        # Repeat only this read, never service starts or configuration writes.
        deadline=time.monotonic()+30
        while True:
            try:
                jobs=self.api('sr422','jobs?active_only=true')['jobs']
                break
            except OSError:
                require(time.monotonic()<deadline,'Web did not become readable after startup')
                time.sleep(.2)
        require(not jobs,'An unexpected card started')

    def verify(self):
        for unit in UNITS:
            state=self.unit(unit)
            require(state['ActiveState']=='active' and state['MainPID']!='0','Service is not active')
            args=Path('/proc/'+state['MainPID']+'/cmdline').read_bytes().split(b'\0')
            require(str(Path(self.b['release'])/'.venv/bin/python').encode() in args,'Running interpreter mismatch')
        self.wait_web_idle()
        save(self.record/'completed.json',dict(firmware=self.b['firmware_version'],release=self.b['release'],
             pi_enabled=False,cards_started=False,live_bumpless_trial_performed=False))

    def close(self):
        if self.endpoint_lock is not None:
            self.endpoint_lock.close();self.endpoint_lock=None
        if self.original_manifest is None:os.environ.pop('LDCON_PHYSICAL_LOCK_MANIFEST',None)
        else:os.environ['LDCON_PHYSICAL_LOCK_MANIFEST']=self.original_manifest


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('bundle',type=Path);parser.add_argument('--execute',action='store_true')
    parser.add_argument('--approval',type=Path);parser.add_argument('--record',type=Path)
    args=parser.parse_args();path=args.bundle.resolve();b=check_bundle(path)
    if not args.execute:
        print(json.dumps(dict(mode='offline-plan',hardware_operations=False,phases=PHASES,
            release_bound=bool(b['release']),ready_to_execute=False,
            pending='Explicit flash approval, stopped cards, live preflight; release binding if absent'),indent=2));return
    require(b['release'] is not None and args.approval is not None and args.record is not None,'Release, approval and new journal required')
    check_approval(args.approval,path,time.time());os.umask(0o077)
    lock=Path('/tmp/ldcon-pi-handover-'+str(os.getuid())+'.lock')
    fd=os.open(lock,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        execute(Host(b,path,args.record),args.record)
    finally:os.close(fd)


if __name__=='__main__':main()
