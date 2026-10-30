"""Explicit, non-autostart DLC owner host; not installed/enabled by this change.

Usage is intentionally gated by --allow-hardware and commissioned definitions.
The same Instrument object serializes all channels of the physical chassis.
"""
import argparse
from dataclasses import asdict
import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import secrets
import threading
import time

from ldcon.core.contracts import MeasurementUnavailable
from ldcon.core.interface_schema import load_mapping
from ldcon.jobs.dlcpro_cards import Protocol
from ldcon.drivers.dlcpro import Console, DLCPro, UncertainDLCWrite
from ldcon.owners.dlcpro_owner import Instrument, LaserOwner, policy_from_device
from ldcon.drivers.fizeau import HttpFizeauWavemeter
from ldcon.owners.ecdl_optical import classify
from ldcon.owners.ecdl_prelock import ModeObservation


class OpticalSource:
    def __init__(self, wavemeter, quality):
        self.wavemeter, self.quality = wavemeter, quality

    def read_mode(self):
        frame = self.wavemeter.read_frame()
        result = classify(frame,self.quality)
        # Keep acquisition identity, raw position-free analysis and all metrics.
        return ModeObservation(frame.frequency, result['single_mode'], result['mode_id'],
            result['quality_valid'],result['impurity'],result['maintenance_required'],
            result.get('metrics') or {},result.get('fringe_analysis') or {})


def make_observation(device):
    """Construct adapters without opening sockets or querying a wavemeter."""
    settings=device['control_settings']
    optical=settings['optical_source']
    result=OpticalSource(HttpFizeauWavemeter(**optical),settings['optical'])
    source=settings.get('frequency_source')
    if source is None:return result  # Existing explicit HTTP-only definition.
    if set(source)!={'type','udp','quality_interval_s','quality_max_age_s'} or source['type']!='wsu.udp':
        raise ValueError('Unsupported DLC control frequency source')
    from ldcon.drivers.wsu_udp import WSUReceiver
    from ldcon.owners.dlcpro_observation import DatagramOpticalSource
    receiver=WSUReceiver(source['udp'])
    if (receiver.config['channel'],receiver.config['cycle'])!=(optical['channel'],optical['cycle']):
        raise ValueError('UDP frequency and optical pattern routes must name the same channel/cycle')
    return DatagramOpticalSource(receiver,result,quality_interval_s=source['quality_interval_s'],
        quality_max_age_s=source['quality_max_age_s'])


def handler(protocols, token):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Never log bearer credentials or request payloads.

        def do_POST(self):
            if self.path!='/rpc' or not secrets.compare_digest(self.headers.get('Authorization',''), 'Bearer '+token):
                self.send_error(403)
                return
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=262144:raise ValueError('Invalid request length')
                request=json.loads(self.rfile.read(length))
                result=protocols[request['laser_id']].dispatch(request)
                reply=dict(ok=True,result=result)
            except MeasurementUnavailable as error:
                reply=dict(ok=False,code='measurement',error=str(error))
            except PermissionError as error:
                reply=dict(ok=False,code='ownership',error=str(error))
            except (ValueError, KeyError, TypeError) as error:
                reply=dict(ok=False,code='configuration',error=str(error))
            except Exception:
                reply=dict(ok=False,code='owner',error='DLC owner fault; inspect audit before retry')
            content=json.dumps(reply,allow_nan=False).encode()
            self.send_response(200)
            self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(content)))
            self.end_headers()
            self.wfile.write(content)
    return Handler


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True,type=Path)
    parser.add_argument('--allow-hardware',action='store_true')
    parser.add_argument('--check-config',action='store_true',help='Offline commissioning inventory; no connections or service changes')
    args=parser.parse_args()
    config=json.loads(args.config.read_text())
    lasers=[load_mapping((args.config.parent / path).read_text()) for path in config['laser_files']]
    from ldcon.owners.dlcpro_deployment import commissioning_issues
    issues=commissioning_issues(config,lasers)
    if args.check_config:
        print(json.dumps(dict(ready=not issues,issues=issues,hardware_operations=False),indent=2))
        return 2 if issues else 0
    if not args.allow_hardware:
        parser.error('Hardware access is disabled; offline tests require no service')
    if issues:raise ValueError('DLC commissioning incomplete: '+'; '.join(issues))
    if not ipaddress.ip_address(config['listen_host']).is_loopback:
        raise ValueError('DLC owner service must bind loopback; use existing authenticated routing')
    token=os.environ.get('LDCON_INSTRUMENT_TOKEN','')
    if len(token)<32:raise ValueError('Owner bearer token required')
    for laser in lasers:
        device=laser['devices']['dlc']
        if not laser['allow_output'] or device['control_settings'].get('commissioned') is not True:
            raise ValueError('Laser remains an offline/uncommissioned definition')
    state=Path(config['state_directory'])
    state.mkdir(parents=True,exist_ok=True)
    lock=(state/'chassis.lock').open('a')
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    marker=state/'inspection-required.json'
    if marker.exists():raise ValueError('Previous owner exit needs explicit inspection; marker retained')
    audit_lock=threading.Lock()
    def audit(event):
        with audit_lock, (state/'audit.jsonl').open('a') as stream:
            stream.write(json.dumps(dict(unix_s=time.time(),**event),allow_nan=False)+'\n')
            stream.flush()
            os.fsync(stream.fileno())
    console=Console.connect(config['host'],timeout_s=config['socket_timeout_s'])
    driver=DLCPro(console)
    try:
        driver.verify_identity(config['expected_serial_number'], config.get('expected_firmware_version'))
        driver.verify_compact_write_support()
    except BaseException:
        console.close()
        raise
    instrument=Instrument(driver,config['hardware'],audit=audit)
    protocols={}
    for laser, laser_path in zip(lasers, config['laser_files']):
        device=laser['devices']['dlc']
        connection=device['connection']
        channel=connection['channel']
        if driver.read(f'laser{channel}:scan:enabled') is not False:
            raise ValueError('Existing DLC scan requires inspection; not stopped automatically')
        source=make_observation(device)
        owner=LaserOwner(instrument,channel,laser['id'],policy_from_device(device),source,
                         audit=audit,target_policy=laser.get('target'))
        protocols[laser['id']]=Protocol(owner,device)
        from ldcon.owners.dlcpro_target_changes import attach as attach_target_changes
        attach_target_changes(protocols[laser['id']], args.config.parent / laser_path,
                              state / 'target-changes' / laser['id'])
        from ldcon.owners.dlcpro_awg_protocol import attach as attach_awg_sweep
        attach_awg_sweep(protocols[laser['id']], state / 'awg-sweeps' / laser['id'])
    sources=[p.owner.observation for p in protocols.values() if hasattr(p.owner.observation,'start')]
    from ldcon.owners.dlcpro_readback import HardwareReadback
    def readback_fault(channel, reason):
        for protocol in protocols.values():
            owner = protocol.owner
            if owner.channel == channel:
                with owner.mutex:
                    if not owner.fault:
                        owner.fault = reason
                        audit(dict(event='owner_fault', laser_id=owner.laser_id, reason=reason))
    readback = HardwareReadback(driver, [p.owner.channel for p in protocols.values()], fault=readback_fault)
    try:
        for source in sources:source.start()
        readback.start()
        instrument.readback = readback
        for protocol in protocols.values():
            channel = protocol.owner.channel
            hardware, receipt = readback.read(channel)
            if not receipt['fresh']:
                raise ValueError('Initial DLC hardware readback unavailable')
            instrument.confirmed_outputs[channel] = {axis:hardware[axis]['setpoint'] for axis in DLCPro.AXES}
    except BaseException:
        for source in sources:source.close()
        readback.close()
        console.close()
        raise
    # A crash never silently re-adopts unknown writes on restart. Operator review
    # is required before manually removing this marker; no automatic cleanup.
    marker.write_text(json.dumps(dict(started_unix_s=time.time(),reason='owner active or unclean exit'))+'\n')
    stopped=threading.Event()
    def control_loop():
        while not stopped.wait(config['tick_s']):
            for protocol in protocols.values():
                try:protocol.owner.tick()
                except Exception:audit(dict(event='tick_fault',laser_id=protocol.owner.laser_id))
    thread=threading.Thread(target=control_loop,daemon=True)
    thread.start()
    server=ThreadingHTTPServer((config['listen_host'],config['listen_port']),handler(protocols,token))
    try:
        server.serve_forever()
    finally:
        stopped.set()
        thread.join(timeout=config['socket_timeout_s']*10)
        server.server_close()
        for source in sources:source.close()
        readback.close()
        console.close()
        # No hidden output writes on process exit. Keep inspection marker.


if __name__=='__main__':
    raise SystemExit(main())
