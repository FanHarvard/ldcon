"""Simulated sr422 rig for the locked-sweep tests: the REAL OffsetAWG / RigHardware / ECDLOwner code runs over
a SCPI emulator (BK 4054B semantics: BSWV leaves burst, BTWV CARR edits the carrier, ARWV applies the upload's
AMPL/OFST, MTRIG starts one MAN burst) and an esp32servocon firmware model (ADC -> target modulation + PZT FF)."""
import copy
from dataclasses import asdict
from pathlib import Path
import re

import yaml

from ldcon.contracts import FrequencySample
from ldcon.drivers.ecdl import OffsetAWG
from ldcon.ecdl_owner import RigHardware

C_NM = 299792458e9
LIVE_LASER = Path(__file__).resolve().parents[2] / 'jobs/tests/fixtures/live-20261022/lasers/sr422.yaml'
DRAFT_BLOCK = Path(__file__).resolve().parents[0] / 'locked_sweep_settings.yaml'


def laser_definition():
    laser = yaml.safe_load(LIVE_LASER.read_text())
    laser['devices']['rig']['control_settings']['locked_sweep'] = yaml.safe_load(DRAFT_BLOCK.read_text())['locked_sweep']
    laser['interfaces']['servo_modulation'] = dict(device='rig', capability='servo_modulation')
    return laser


def num(text):
    return float(re.sub(r'(HZ|VPP|V|S)$', '', str(text).upper()))


class Channel:
    def __init__(self, wave):
        self.wave = dict(wave)              # WVTP, FRQ, AMP, OFST
        self.arb = wave.get('NAME')
        self.burst = dict(STATE='OFF', TRSR='INT', GATE_NCYC='NCYC', TIME='1', STPS='0', DLAY='0', EDGE='RISE')
        self.fired_at = None


class SCPI:
    """One physical AWG. ``clock`` drives playback."""
    def __init__(self, idn, clock, channels):
        self.idn, self.clock, self.ch = idn, clock, channels
        self.uploads = {}
        self.log = []
        self.fail_on = None
        self.coupling = 'COUP TRACE,OFF,FCOUP,OFF,PCOUP,OFF,ACOUP,OFF'
        self.mtrig_works = True          # set False to emulate a trigger that never starts the record
        # 2026-10-22 21:03Z sr422 (BK 4054B, TRSR EXT, untriggered): CH2 idled at ~0.735 V instead of the 1.0 V centre
        # (OFST 1 V, AMP 1.2 V -> -0.4417 of AMP/2). MAN-armed records idled at their first sample (the centre).
        # None = idle at the first sample for every source.
        self.ext_idle_fraction = None
        self.single_session = False      # SDG2042X (2026-10-22 13:24Z): a new LAN session breaks the previous one
        self.active_session = 0
        self.session_count = 0

    def open_session(self):
        self.session_count += 1
        self.active_session = self.session_count
        return self.session_count

    def check_session(self, sid):
        if self.single_session and sid != self.active_session:
            raise BrokenPipeError(32, 'Broken pipe')
        self.dropped = []                # lines after the first one of a multi-line socket write (suspected BK behaviour)

    # ---- command execution ------------------------------------------------------------------
    def write(self, line):
        self.log.append(line)
        if self.fail_on and self.fail_on in line:
            self.fail_on = None
            raise OSError('injected AWG write failure: ' + line)
        prefix, rest = line.split(':', 1)
        c = self.ch[int(prefix[1:])]
        key, _, value = rest.partition(' ')
        tokens = [t.strip() for t in value.split(',')] if value else []
        if key == 'BSWV':
            c.burst['STATE'] = 'OFF'               # BK: BSWV exits burst
            c.fired_at = None
            c.wave.update(dict(zip(tokens[::2], tokens[1::2])))
            if c.wave.get('WVTP') == 'DC':
                c.wave = dict(WVTP='DC', OFST=c.wave['OFST'])
        elif key == 'BTWV':
            if tokens and tokens[0] == 'MTRIG':
                if c.burst['STATE'] == 'ON' and c.burst['TRSR'] == 'MAN' and self.mtrig_works:
                    c.fired_at = self.clock()
                return
            if tokens and tokens[0] == 'CARR':
                c.wave.update(dict(zip(tokens[1::2], tokens[2::2])))
                return
            if c.wave.get('WVTP') == 'DC' and tokens[:2] == ['STATE', 'ON']:
                raise OSError('burst unavailable on DC')
            if c.fired_at is not None and 'FRQ' in c.wave:
                # v5.4: a completed burst stays idle when a burst setting (e.g. TIME,N) changes afterwards
                if self.clock() - c.fired_at >= int(float(c.burst.get('TIME', '1'))) / num(c.wave['FRQ']):
                    c.fired_at = None
            c.burst.update(dict(zip(tokens[::2], tokens[1::2])))
            if c.burst['STATE'] == 'OFF':
                c.fired_at = None
        elif key == 'ARWV':
            name = tokens[1]
            if name not in self.uploads:
                return                     # BK: an unknown name is acknowledged and ignored (R0 restore observation)
            c.arb = name
            c.wave['WVTP'] = 'ARB'
            up = self.uploads.get(name)
            if up:
                c.wave.update(AMP=up['AMPL'], OFST=up['OFST'])
        elif key == 'OUTP':
            pass
        else:
            raise OSError('unsupported ' + line)

    def write_raw(self, data):
        header, payload = data.split(b'WAVEDATA,', 1)
        text = header.decode()
        self.log.append(text + '<binary>')
        if self.fail_on and self.fail_on in text:
            self.fail_on = None
            raise OSError('injected upload failure')
        tokens = [t.strip() for t in text.split(' ', 1)[1].rstrip(',').split(',')]
        f = dict(zip(tokens[::2], tokens[1::2]))
        import numpy as np
        f['codes'] = np.frombuffer(payload, dtype='<i2').copy()
        if 'LENGTH' in f:
            f['codes'] = np.zeros_like(f['codes'])        # suspected BK mis-parse of a LENGTH header: flat record
        self.uploads[f['WVNM']] = f

    def query(self, line):
        self.log.append(line)
        if line == '*IDN?':
            return self.idn
        if line == '*OPC?':
            return '1'
        if line == 'COUP?':
            return self.coupling
        prefix, key = line[:-1].split(':')
        c = self.ch[int(prefix[1:])]
        if key == 'BSWV':
            if c.wave['WVTP'] == 'DC':
                return f'{prefix}:BSWV WVTP,DC,OFST,{c.wave["OFST"]}'
            return f'{prefix}:BSWV WVTP,{c.wave["WVTP"]},FRQ,{c.wave["FRQ"]},AMP,{c.wave["AMP"]},OFST,{c.wave["OFST"]}'
        if key == 'ARWV':
            return f'{prefix}:ARWV NAME,{c.arb}.bin' if c.arb else f'{prefix}:ARWV NAME,'
        if key == 'OUTP':
            return f'{prefix}:OUTP ON,LOAD,HZ,PLRT,NOR'
        if key == 'BTWV':
            return f'{prefix}:BTWV ' + ','.join(f'{k},{v}' for k, v in c.burst.items())
        if key in ('MDWV', 'SWWV'):
            return f'{prefix}:{key} STATE,OFF'
        raise OSError('unsupported query ' + line)

    # ---- physical output ------------------------------------------------------------------------
    def voltage(self, channel):
        c = self.ch[channel]
        offset = num(c.wave['OFST'])
        if c.wave['WVTP'] == 'DC':
            return offset
        amp = num(c.wave['AMP'])
        up = self.uploads.get(c.arb)
        if up is None:                                       # factory dither ARB: a slow sine of +-amp/2
            return offset
        codes = up['codes']
        period = 1 / num(c.wave['FRQ'])
        if c.burst['STATE'] == 'ON':
            if c.fired_at is None:
                if c.burst.get('TRSR') == 'EXT' and self.ext_idle_fraction is not None:
                    return offset + self.ext_idle_fraction * amp / 2
                return offset + codes[0] / 32767 * amp / 2
            t = self.clock() - c.fired_at
            cycles = int(float(str(c.burst.get('TIME', '1'))))          # v5.4: N-cycle burst (BTWV TIME,N)
            if t >= period * cycles:
                return offset + codes[-1] / 32767 * amp / 2
            t = t % period
            return offset + codes[min(len(codes) - 1, int(t / period * len(codes)))] / 32767 * amp / 2
        t = self.clock() % period
        return offset + codes[int(t / period * len(codes)) % len(codes)] / 32767 * amp / 2


class Socket:
    def __init__(self, scpi):
        self.scpi, self.replies = scpi, []
        self.sid = scpi.open_session()

    def sendall(self, data):
        self.scpi.check_session(self.sid)
        lines = [line for line in data.decode('ascii').split('\n') if line]
        if len(lines) > 1:
            self.scpi.dropped += lines[1:]               # suspected BK raw-socket behaviour: one command per write
            lines = lines[:1]
        for line in lines:
            if line.endswith('?'):
                self.replies.append(self.scpi.query(line) + '\n')
            else:
                self.scpi.write(line)

    def readline(self, limit):
        self.scpi.check_session(self.sid)
        return self.replies.pop(0).encode('ascii')

    def close(self):
        pass


def awg_over(scpi, host):
    awg = OffsetAWG.__new__(OffsetAWG)
    awg.host = host
    awg.socket = Socket(scpi)
    awg.stream = awg.socket
    awg.timeout = 5.
    awg.suspended = False

    def connect():
        s = Socket(scpi)
        s.makefile = lambda mode: s
        return s
    awg._connect = connect
    return awg


class Visa:
    """Stands in for drivers.siglent.VisaTransport: uploads reach the same emulator."""
    registry = {}

    def __init__(self, resource, timeout_s, backend=None):
        host = resource.split('::')[1]
        self.scpi = Visa.registry[host]
        self.sid = self.scpi.open_session()

    def write_raw(self, data):
        self.scpi.check_session(self.sid)
        self.scpi.write_raw(data)

    def write(self, command):
        self.scpi.check_session(self.sid)
        self.scpi.write(command)

    def query(self, command):
        self.scpi.check_session(self.sid)
        return self.scpi.query(command)

    def close(self):
        pass


class Firmware:
    """esp32servocon 2026.10.21-antiwindup model sufficient for the owner paths."""
    def __init__(self, clock, ch2_voltage):
        self.clock, self.ch2_voltage = clock, ch2_voltage
        self.cfg = dict(target=421.671066, control_offset=31542., pid_multiplier=0, multiplier_control=0, multiplier_target=0,
                        baseline_control=19589.25, baseline_target=19589.25, software_modulation_enable=0, valid_data_index=2,
                        system_min=421.6710363450719, system_max=421.6710956549322, mod_valid_min=1000, mod_valid_max=65535,
                        target_time_match=0, target_delay_us=0, pid_kp=0., pid_ki=1., pid_kd=0., pid_integral_min=-9.072e-05,
                        pid_integral_max=9.072e-05, log_force_full_data_on_new_system=0, log_forced_full_data_min_interval=0,
                        polling_interval=1)
        self.seq = 1000
        self.counts_per_v, self.intercept = 19500., 40.
        self.error_nm = 0.
        self.analog_path_counts_per_v = 0.
        self.pid_out = 254.
        self.pid_integral = 1e-6
        self.sets = []
        self.fail_on = None
        self.last_valid = None

    def raw(self):
        v = self.ch2_voltage()
        raw = int(round(max(0, min(65535, self.intercept + self.counts_per_v * v))))
        if self.cfg['mod_valid_min'] <= raw <= self.cfg['mod_valid_max']:
            self.last_valid = raw
            return raw
        return self.last_valid if self.last_valid is not None else raw

    def get(self, path):
        if path == 'config':
            return copy.deepcopy(self.cfg)
        if path == 'dac/limits':
            return dict(min_code=0, max_code=65535, saved=False)
        assert path == 'status'
        self.seq += 1
        raw = self.raw()
        t_eff = self.cfg['target'] + self.cfg['multiplier_target'] * (raw - self.cfg['baseline_target'])
        measured = t_eff + self.error_nm
        dac = (self.cfg['control_offset'] + self.cfg['multiplier_control'] * (raw - self.cfg['baseline_control'])
               + (self.pid_out if self.cfg['pid_multiplier'] else 0.) + self.analog_path_counts_per_v * (self.ch2_voltage() - 1.))
        return dict(firmware_version='2026.10.21-antiwindup', pid_time_based=True, last_udp_channel=3, last_udp_cycle=0,
                    last_udp_src='192.168.1.22', last_udp_quality=0, last_udp_seq=self.seq, last_udp_flags=5, system_src=1,
                    last_system_interval_us=42000, system_value=measured, pid_error=measured - t_eff, mod_raw=raw,
                    mod_invalid=0, dac_val=dac, pid_integral=self.pid_integral, pid_output=self.pid_out,
                    target_match_mod_raw=raw, target_match_age_us=12000, udp_out_of_range=0,
                    capabilities=['dac_limits_http_v1', 'modulation_atomic'])

    def set(self, values):
        self.sets.append(dict(values))
        if self.fail_on and self.fail_on in values:
            self.fail_on = None
            raise OSError('injected firmware write failure')
        for key, value in values.items():
            if key == 'pid_reset_state':
                continue
            self.cfg[key] = value
        return {}

    def require_write_support(self):
        pass

    def frequency(self):
        raw = self.raw()
        t_eff = self.cfg['target'] + self.cfg['multiplier_target'] * (raw - self.cfg['baseline_target'])
        return C_NM / (t_eff + self.error_nm)


class FakeRig(RigHardware):
    def __init__(self, device, clock, single_session=()):
        self.device, self.clock = device, clock
        self.bk = SCPI('BK,4054B,51524H117,1.01.01.33R8', clock, {
            1: Channel(dict(WVTP='ARB', FRQ='0.25HZ', AMP='0.002V', OFST='0.9185V', NAME='ldcd0')),
            2: Channel(dict(WVTP='DC', OFST='1V'))})
        self.sdg = SCPI('Siglent Technologies,SDG2042X,SDG2XFBCA00839,2.01.01.39R7', clock, {
            1: Channel(dict(WVTP='DC', OFST='0.1411V')), 2: Channel(dict(WVTP='DC', OFST='0V'))})
        self.sdg.single_session = True
        import numpy as np
        self.bk.uploads['ldcd0'] = dict(WVNM='ldcd0', AMPL='0.002', OFST='0.6949', codes=np.zeros(8192, dtype='<i2'))
        Visa.registry.update({'192.168.1.21': self.bk, '192.168.1.37': self.sdg})
        self.firmware = Firmware(clock, lambda: self.bk.voltage(2))
        from ldcon.drivers.ecdl import VisaAWG
        self.connections = dict(piezo=self.firmware, current_awg=awg_over(self.bk, '192.168.1.21'),
                                tec_awg=VisaAWG('192.168.1.37') if 'tec_awg' in single_session else awg_over(self.sdg, '192.168.1.37'))
        self.identities = {k: io.describe() for k, io in self.connections.items() if k != 'piezo'}
        self.locks = []
        self.single_mode = True
        self.side_peak = .1
        self.observation_error = None
        self.seq = 0

    def observation(self):
        if self.observation_error:
            from ldcon.contracts import DeviceError
            raise DeviceError(self.observation_error)
        self.seq += 1
        f = self.firmware.frequency()
        sample = FrequencySample(self.seq, f, self.clock(), self.clock(), 'valid', 'test', acquired_unix_s=1000. + self.clock())
        return dict(frequency=asdict(sample), single_mode=self.single_mode, mode_id='desired', quality_valid=True,
                    maintenance_required=False,
                    metrics=dict(side_peak_height_max=self.side_peak, side_peak_prominence_max=.05, main_fwhm_fraction=.1))

    def close(self):
        pass
