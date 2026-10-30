"""DHO900 read-only SCPI driver. No acquisition or readout setting writes."""

import math
import socket
import time


class RigolScope:
    def __init__(self, host, port, device_id, serial=None):
        self.host, self.port = host, port
        self.device_id, self.serial = device_id, serial
        self.connection = None
        self.identity = None
        self.deadline = 0

    def close(self):
        if self.connection:
            self.connection.close()
        self.connection = None
        self.identity = None

    def _exact(self, size):
        result = bytearray()
        while len(result) < size:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('SCPI read deadline exceeded')
            self.connection.settimeout(min(2, remaining))
            data = self.connection.recv(size - len(result))
            if not data:
                raise OSError('SCPI connection closed')
            result.extend(data)
        return bytes(result)

    def _line(self, first=b'', limit=4096):
        result = bytearray(first)
        while not result.endswith(b'\n'):
            if len(result) >= limit:
                raise ValueError('SCPI response too large')
            result.extend(self._exact(1))
        return bytes(result).rstrip(b'\r\n')

    def query(self, command, *, block=False):
        # Closed allowlist: no raw SCPI forwarding or separator injection.
        allowed = {'*IDN?', ':TRIG:STAT?', ':WAV:MODE?', ':WAV:SOUR?',
                   ':WAV:PRE?', ':WAV:DATA?'}
        allowed.update(f':CHAN{ch}:{key}?' for ch in range(1, 5)
                       for key in ('DISP', 'SCAL', 'OFFS', 'PROB'))
        if command not in allowed:
            raise ValueError('SCPI query is not allowed')
        self.deadline = min(self.cycle_deadline, time.monotonic() + 2)
        self.connection.settimeout(2)
        self.connection.sendall((command + '\n').encode('ascii'))
        first = self._exact(1)
        if first != b'#':
            return self._line(first, limit=65536 if block else 4096)
        if not block:
            raise ValueError('Unexpected binary SCPI reply')
        digits = self._exact(1)
        if digits not in (b'1', b'2', b'3', b'4', b'5', b'6', b'7', b'8', b'9'):
            raise ValueError('Invalid SCPI block header')
        size = int(self._exact(int(digits)))
        if not 1 <= size <= 65536:
            raise ValueError('SCPI block exceeds limit')
        data = self._exact(size)
        if self._line(limit=2):
            raise ValueError('Invalid SCPI block terminator')
        return data

    def text(self, command):
        return self.query(command).decode('ascii').strip()

    def number(self, command):
        value = float(self.text(command))
        if not math.isfinite(value) or abs(value) >= 1e30:
            raise ValueError('Invalid numeric SCPI reply')
        return value

    def read(self):
        self.cycle_deadline = time.monotonic() + 6
        if self.connection is None:
            self.connection = socket.create_connection((self.host, self.port), timeout=2)
            self.identity = self.text('*IDN?')
            fields = self.identity.split(',')
            if (len(fields) != 4 or fields[0].strip() != 'RIGOL TECHNOLOGIES'
                    or fields[1].strip() != 'DHO924'
                    or (self.serial and fields[2].strip() != self.serial)):
                raise ValueError('Scope identity mismatch')
        started = time.time()
        trigger = self.text(':TRIG:STAT?')
        channels = {}
        for ch in range(1, 5):
            display = self.text(f':CHAN{ch}:DISP?')
            if display not in ('0', '1', 'OFF', 'ON'):
                raise ValueError('Invalid channel display state')
            channels[str(ch)] = dict(enabled=display in ('1', 'ON'))
            if channels[str(ch)]['enabled']:
                channels[str(ch)].update(scale_v=self.number(f':CHAN{ch}:SCAL?'),
                                         offset_v=self.number(f':CHAN{ch}:OFFS?'),
                                         probe_ratio=self.number(f':CHAN{ch}:PROB?'))
        waveform = dict(available=False, reason='Readout mode is not NORM; settings preserved')
        if self.text(':WAV:MODE?') == 'NORM':
            source = self.text(':WAV:SOUR?')
            preamble = self.text(':WAV:PRE?')
            p = [float(value) for value in preamble.split(',')]
            if len(p) != 10 or not all(math.isfinite(v) for v in p):
                raise ValueError('Invalid waveform preamble')
            if p[0] in (0, 2) and p[1] == 0 and 1 <= p[2] <= 10000 and p[2].is_integer():
                raw = self.query(':WAV:DATA?', block=True)
                values = (list(raw) if p[0] == 0 else
                          [float(v) for v in raw.decode('ascii').rstrip(',').split(',')])
                if len(values) != int(p[2]) or not all(math.isfinite(v) for v in values):
                    raise ValueError('Waveform length differs from preamble')
                # Reject a front-panel/readout change across the transfer.
                if preamble != self.text(':WAV:PRE?') or source != self.text(':WAV:SOUR?'):
                    raise ValueError('Scope readout changed during transfer')
                waveform = dict(available=True, source=source, preamble=p,
                                time_s=[(i - p[6]) * p[4] + p[5] for i in range(len(values))],
                                voltage_v=([float(v) for v in values] if p[0] == 2 else
                                           [(v - p[8] - p[9]) * p[7] for v in values]))
            else:
                waveform = dict(available=False, source=source, preamble=p,
                                reason='Requires bounded BYTE or ASCII/NORM readout; settings preserved')
        return dict(identity=self.identity, read_started_unix_s=started,
                    read_finished_unix_s=time.time(), trigger_status=trigger,
                    acquisition_time_known=False, channels=channels, waveform=waveform)
