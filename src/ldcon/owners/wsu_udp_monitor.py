"""Receive-only WSU1 monitor. No DLC connection, output API or control loop."""
import argparse
from dataclasses import asdict
import ipaddress
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal
import socket
import time

from ldcon.drivers.wsu_udp import PACKET, decode


def validate(config):
    if set(config) != {'listen_host','listen_port','source_ip','valid_data_index','channel','cycle','state_directory'}:
        raise ValueError('Explicit receive-only route and state_directory required')
    for key in ('listen_host','source_ip'):
        addr=ipaddress.ip_address(config[key])
        if addr.version != 4 or addr.is_unspecified or addr.is_multicast:
            raise ValueError('Pinned unicast IPv4 addresses required')
    for key,lo,hi in [('listen_port',1024,65535),('valid_data_index',0,255),('channel',1,9),('cycle',0,15)]:
        if type(config[key]) is not int or not lo<=config[key]<=hi:raise ValueError('Invalid '+key)
    if not Path(config['state_directory']).is_absolute():raise ValueError('Absolute state directory required')
    return dict(config)


class Monitor:
    def __init__(self, config):
        self.config=validate(config)
        self.started=time.time();self.received=0;self.valid=0;self.unique=0
        self.duplicates=0;self.out_of_order=0;self.rejected=0
        self.latest=None;self.received_at=None;self.last_error=None
        self.previous=None

    def feed(self, raw, source, received_at):
        self.received+=1
        try:
            if source!=self.config['source_ip']:raise ValueError('Wrong source IP')
            packet=decode(raw)
            if (packet.index,packet.channel,packet.cycle)!=tuple(self.config[k] for k in ('valid_data_index','channel','cycle')):
                raise ValueError('Wrong index/channel/cycle')
        except (ValueError,TypeError,OverflowError) as error:
            self.rejected+=1;self.last_error=str(error);return None
        self.valid+=1
        identity=(packet.sequence,packet.acquired_unix_ns)
        if identity==self.previous:
            self.duplicates+=1;return None
        if self.previous and packet.acquired_unix_ns<=self.previous[1]:
            self.out_of_order+=1;return None
        self.previous=identity;self.unique+=1;self.last_error=None
        self.latest=asdict(packet);self.received_at=received_at
        return dict(received_unix_s=received_at,source_ip=source,
                    source_age_at_receive_s=received_at-packet.acquired_unix_ns/1e9,
                    packet=self.latest)

    def snapshot(self, now):
        return dict(schema_version=1,mode='receive_only',control_enabled=False,
                    started_unix_s=self.started,updated_unix_s=now,
                    route={k:v for k,v in self.config.items() if k!='state_directory'},
                    received=self.received,valid=self.valid,unique=self.unique,
                    duplicates=self.duplicates,out_of_order=self.out_of_order,rejected=self.rejected,
                    last_error=self.last_error,last_received_unix_s=self.received_at,
                    arrival_age_s=None if self.received_at is None else now-self.received_at,
                    source_age_s=None if self.latest is None else now-self.latest['acquired_unix_ns']/1e9,
                    latest=self.latest,
                    clock_policy='Arrival and source ages reported; no clock trust or control authority granted')


def save_status(path, value):
    temporary=path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value,allow_nan=False,indent=2)+'\n')
    os.replace(temporary,path)


def run(config):
    monitor=Monitor(config);directory=Path(config['state_directory'])
    # Exclusive binding: later owner activation requires stopping this monitor first.
    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock:
        sock.bind((config['listen_host'],config['listen_port']));sock.settimeout(.2)
        directory.mkdir(parents=True,exist_ok=True)
        log=RotatingFileHandler(directory/'samples.jsonl',maxBytes=10*1024*1024,backupCount=3)
        log.setFormatter(logging.Formatter('%(message)s'))
        stopped=False
        def stop(signum,frame):
            nonlocal stopped
            stopped=True
        signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
        due=0.
        try:
            while not stopped:
                try:
                    raw,source=sock.recvfrom(PACKET.size+1)
                    item=monitor.feed(raw,source[0],time.time())
                    if item is not None:log.handle(logging.makeLogRecord({'msg':json.dumps(item,allow_nan=False)}))
                except socket.timeout:pass
                if time.monotonic()>=due:
                    save_status(directory/'status.json',dict(monitor.snapshot(time.time()),state='listening'))
                    due=time.monotonic()+1.
        finally:
            log.close()
            save_status(directory/'status.json',dict(monitor.snapshot(time.time()),state='stopped'))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,required=True)
    args=parser.parse_args();run(validate(json.loads(args.config.read_text())))


if __name__=='__main__':main()
