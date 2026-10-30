"""WSU1 receiver compatible with esp32servocon's 34-byte system-value datagram.

A receiver owns its own destination; it never changes or forwards another
controller's route. Quality/aligned bits are metadata, not silent vetoes.
"""
from collections import deque
from dataclasses import asdict, dataclass
import binascii
import ipaddress
import math
import socket
import struct
import threading
import time

from ldcon.core.contracts import FrequencySample, MeasurementUnavailable

PACKET = struct.Struct('<4sBBBBBBHIqdH')
C_NM_HZ = 299792458e9


@dataclass(frozen=True)
class Datagram:
    flags: int
    index: int
    channel: int
    cycle: int
    quality: int
    sequence: int
    acquired_unix_ns: int
    value: float
    unit: str
    frequency_hz: float


def decode(raw):
    if len(raw) != PACKET.size:
        raise ValueError('WSU1 requires exactly 34 bytes')
    magic, version, flags, index, channel, cycle, reserved, quality, seq, stamp, value, crc = PACKET.unpack(raw)
    if magic != b'WSU1' or version != 1 or binascii.crc_hqx(raw[:32],0xffff) != crc:
        raise ValueError('WSU1 header/version/CRC mismatch')
    if reserved != 0 or flags & ~7 or not 1 <= channel <= 9 or cycle > 15:
        raise ValueError('Unsupported WSU1 metadata')
    if not math.isfinite(value) or value <= 0 or stamp <= 0:
        raise ValueError('Invalid WSU1 value/acquisition time')
    hz = C_NM_HZ/value if flags & 4 else value*1e12
    if not math.isfinite(hz) or hz <= 0:
        raise ValueError('Invalid WSU1 frequency conversion')
    return Datagram(flags,index,channel,cycle,quality,seq,stamp,value,'nm' if flags & 4 else 'THz',hz)


def validate_config(config):
    keys={'listen_host','listen_port','source_ip','valid_data_index','channel','cycle',
          'trust_source_clock','max_age_s','future_tolerance_s','buffer_samples'}
    if not isinstance(config,dict) or set(config)!=keys:
        raise ValueError('Explicit WSU1 route and timestamp policy required')
    for key in ('listen_host','source_ip'):
        address=ipaddress.ip_address(config[key])
        if address.version!=4 or address.is_unspecified or address.is_multicast:
            raise ValueError('WSU1 requires pinned local/source unicast IPv4 addresses')
    for key,lo,hi in [('listen_port',1024,65535),('valid_data_index',0,255),('channel',1,9),('cycle',0,15),('buffer_samples',16,65536)]:
        if type(config[key]) is not int or not lo<=config[key]<=hi:
            raise ValueError('Invalid WSU1 '+key)
    if config['trust_source_clock'] is not True:
        raise ValueError('Source clock alignment must be commissioned before UDP control')
    for key in ('max_age_s','future_tolerance_s'):
        v=config[key]
        if type(v) not in (int,float) or not math.isfinite(v) or not 0<=v<=60:
            raise ValueError('Invalid WSU1 '+key)
    if config['max_age_s']==0:raise ValueError('Positive WSU1 freshness required')
    return dict(config)


class WSUReceiver:
    def __init__(self,config,*,clock=time.monotonic,utc=time.time):
        self.config=validate_config(config);self.clock=clock;self.utc=utc
        self.lock=threading.Lock();self.stop=threading.Event();self.thread=None;self.socket=None
        self.samples=deque(maxlen=config['buffer_samples']);self.latest_packet=None
        self.last_seq=None;self.sequence_stamp=None;self.accepted=0;self.rejected=0;self.overwritten=0
        self.rebases=0;self.error=None

    def feed(self,raw,source):
        with self.lock:
            try:
                if source!=self.config['source_ip']:raise ValueError('Unexpected WSU1 source')
                packet=decode(raw)
                if (packet.index,packet.channel,packet.cycle)!=tuple(self.config[k] for k in ('valid_data_index','channel','cycle')):
                    raise ValueError('Wrong WSU1 index/channel/cycle')
                received=self.clock();age=self.utc()-packet.acquired_unix_ns/1e9
                if not -self.config['future_tolerance_s']<=age<=self.config['max_age_s']:
                    raise ValueError('Stale/future WSU1 acquisition')
                if self.sequence_stamp is not None and packet.acquired_unix_ns<=self.sequence_stamp:
                    raise ValueError('Duplicate/reordered WSU1 acquisition')
                if self.last_seq is not None:
                    delta=(packet.sequence-self.last_seq)&0xffffffff
                    if delta==0:raise ValueError('Duplicate WSU1 sequence')
                    if delta>=0x80000000:
                        # A newer source timestamp permits rebaselining after a
                        # sender restart, but never integrates this first packet.
                        self.last_seq=packet.sequence;self.sequence_stamp=packet.acquired_unix_ns
                        self.rebases+=1
                        raise ValueError('WSU1 sequence rebased; await next acquisition')
                self.last_seq=packet.sequence;self.sequence_stamp=packet.acquired_unix_ns
                # Small source-clock lead is retained in raw timestamp metadata,
                # but cannot produce a future monotonic control acquisition.
                acquired=received-max(0.,age)
                sample=FrequencySample(packet.sequence,packet.frequency_hz,acquired,received,
                    'valid' if packet.quality==0 else 'source_quality_'+str(packet.quality),
                    'wsu-udp:%d:%d:%d'%(packet.index,packet.channel,packet.cycle),
                    acquired_unix_s=packet.acquired_unix_ns/1e9)
                if self.samples and sample.acquired_s<=self.samples[-1].acquired_s:
                    raise ValueError('Non-increasing mapped acquisition time')
                if len(self.samples)==self.samples.maxlen:self.overwritten+=1
                self.samples.append(sample);self.latest_packet=packet;self.accepted+=1;self.error=None
                return True
            except (ValueError,TypeError,OverflowError) as error:
                self.rejected+=1;self.error=str(error)
                return False

    def read_frequency(self):
        with self.lock:
            if not self.samples or not 0<=self.clock()-self.samples[-1].acquired_s<=self.config['max_age_s']:
                raise MeasurementUnavailable('No fresh WSU1 frequency')
            return self.samples[-1]

    def samples_after(self,acquired_s):
        with self.lock:
            return [x for x in self.samples if acquired_s is None or x.acquired_s>acquired_s]

    def snapshot(self):
        with self.lock:
            last=self.samples[-1] if self.samples else None
            age=None if last is None else self.clock()-last.acquired_s
            return dict(transport='WSU1/UDP',accepted=self.accepted,rejected=self.rejected,
                ring_overwritten=self.overwritten,sequence_rebases=self.rebases,error=self.error,
                fresh=age is not None and 0<=age<=self.config['max_age_s'],age_s=age,
                packet=None if self.latest_packet is None else asdict(self.latest_packet),
                quality_policy='source quality/aligned are reported; no quality-label veto')

    def start(self):
        if self.thread is not None:raise RuntimeError('WSU1 receiver already started')
        sock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
        try:
            # No SO_REUSEPORT: another laser/controller's listener must not be stolen.
            sock.bind((self.config['listen_host'],self.config['listen_port']));sock.settimeout(.2)
        except BaseException:
            sock.close();raise
        self.socket=sock
        def run():
            while not self.stop.is_set():
                try:raw,address=sock.recvfrom(PACKET.size+1)
                except socket.timeout:continue
                except OSError as error:
                    if not self.stop.is_set():
                        with self.lock:self.error=type(error).__name__
                    break
                self.feed(raw,address[0])
        self.thread=threading.Thread(target=run,name='wsu-udp-receiver',daemon=True);self.thread.start()
        return self

    def close(self):
        self.stop.set()
        if self.socket is not None:self.socket.close()
        if self.thread is not None:self.thread.join(timeout=1)
