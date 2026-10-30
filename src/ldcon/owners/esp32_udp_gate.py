"""Per-device WSU1 quality/freshness gate before native firmware can integrate.

The ESP firmware does not reject quality flags. Only this owner forwards this
device's route; the upstream source must not retain a parallel direct route.
"""
import binascii
from collections import deque
import ipaddress
import math
import socket
import struct
import threading
import time

PACKET = struct.Struct('<4sBBBBBBHIqdH')


def validate_config(config, route):
    if not isinstance(config, dict) or set(config) != {'listen_host', 'listen_port', 'source_ip', 'max_age_s', 'future_tolerance_s'}:
        raise ValueError('An explicit per-device UDP quality gate is required')
    local = ipaddress.ip_address(config['listen_host'])
    source = ipaddress.ip_address(config['source_ip'])
    if local.version != 4 or source.version != 4 or local.is_unspecified or source.is_unspecified:
        raise ValueError('UDP gate must pin local and source IPv4 addresses')
    if str(local) != route['src']:
        raise ValueError('Native route source must be the independent UDP gate address')
    if type(config['listen_port']) is not int or not 1024 <= config['listen_port'] <= 65535:
        raise ValueError('Invalid dedicated UDP gate listen port')
    for key in ('max_age_s', 'future_tolerance_s'):
        value = config[key]
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError('UDP age/future tolerance must be finite within 0..1 seconds')
    if config['max_age_s'] <= 0:
        raise ValueError('UDP freshness deadline must be positive')
    return config


class UdpQualityGate:
    def __init__(self, config, route, destination, *, utc=time.time, clock=time.monotonic):
        self.config = validate_config(config, route)
        self.route = route
        self.destination = (destination, route['udp_port'])
        self.utc, self.clock = utc, clock
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.thread = None
        self.socket = None
        self.last_seq = None
        self.recent = deque(maxlen=2048)
        self.accepted = 0
        self.rejected = 0
        self.error = None

    def validate(self, raw, source):
        if source != self.config['source_ip']:
            raise ValueError('Unexpected UDP upstream source')
        if len(raw) != PACKET.size:
            raise ValueError('Wrong WSU1 packet size')
        magic, version, flags, index, channel, cycle, reserved, quality, seq, stamp, value, crc = PACKET.unpack(raw)
        if magic != b'WSU1' or version != 1 or binascii.crc_hqx(raw[:32], 0xffff) != crc:
            raise ValueError('Invalid WSU1 header or CRC')
        if reserved != 0 or flags & ~7 or flags & 5 != 5 or quality != 0:
            raise ValueError('Wavelength input is not aligned and quality-valid')
        if (index, channel, cycle) != tuple(self.route[k] for k in ('valid_data_index', 'channel', 'cycle')):
            raise ValueError('Wrong WSU1 index/channel/cycle')
        if not math.isfinite(value) or value <= 0:
            raise ValueError('Invalid wavelength')
        age = self.utc() - stamp / 1e9
        if not -self.config['future_tolerance_s'] <= age <= self.config['max_age_s']:
            raise ValueError('Stale or future wavelength acquisition')
        if self.last_seq is not None:
            delta = (seq - self.last_seq) & 0xffffffff
            if not 0 < delta < 0x80000000:
                raise ValueError('Duplicate or regressed WSU1 sequence')
        return seq, value, stamp

    def forward(self, raw, source, send):
        with self.lock:
            try:
                seq, value, stamp = self.validate(raw, source)
                # Check freshness immediately before the sole send, never queue
                # or retry an old datagram after communication recovers.
                send(raw, self.destination)
            except (ValueError, OSError) as exc:
                self.rejected += 1
                self.error = str(exc)
                return False
            self.last_seq = seq
            self.accepted += 1
            self.recent.append((seq, value, stamp / 1e9, self.clock()))
            self.error = None
            return True

    def contains(self, seq, value):
        with self.lock:
            return any(item[0] == seq and item[1] == value
                       and -self.config['future_tolerance_s'] <= self.utc()-item[2] <= self.config['max_age_s']
                       for item in reversed(self.recent))

    def snapshot(self):
        with self.lock:
            last = self.recent[-1] if self.recent else None
            return dict(accepted=self.accepted, rejected=self.rejected, error=self.error,
                        last_seq=self.last_seq, last_forward_age_s=None if last is None else self.clock()-last[3],
                        fresh=last is not None and 0 <= self.clock()-last[3] <= self.config['max_age_s'],
                        upstream=self.config['source_ip'], destination=list(self.destination))

    def start(self):
        if self.thread is not None:
            raise RuntimeError('UDP quality gate already started')
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.socket.bind((self.config['listen_host'], self.config['listen_port']))
            self.socket.settimeout(.2)
        except BaseException:
            self.socket.close()
            raise
        def run():
            while not self.stop.is_set():
                try:
                    raw, source = self.socket.recvfrom(2048)
                except socket.timeout:
                    continue
                except OSError as exc:
                    self.error = str(exc)
                    return
                self.forward(raw, source[0], self.socket.sendto)
        self.thread = threading.Thread(target=run, name='native-udp-quality-gate', daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=1)
        if self.socket is not None:
            self.socket.close()
