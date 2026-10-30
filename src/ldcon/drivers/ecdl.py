"""Owner-only esp32servocon and DC/offset SCPI transports; no automatic retry."""
import json
import socket
from urllib.request import ProxyHandler, build_opener

from ldcon.drivers.siglent import fields, number
from ldcon.core.contracts import DeviceError


class Servo:
    def __init__(self, host, timeout=5.):
        self.host=host; self.timeout=timeout
        self.http=build_opener(ProxyHandler({}))
        # 2026-10-23 (interim until the firmware exports it): report-only hook called with every /status mapping
        # this driver already reads (esp32_time_telemetry.TimeSyncExporter; rate-limited, never raises).
        self.status_observer=None

    def get(self, path):
        with self.http.open('http://'+self.host+'/'+path,timeout=self.timeout) as r:
            result=json.loads(r.read(1024*1024))
        if path.strip('/')=='status' and self.status_observer is not None and isinstance(result,dict):
            try:self.status_observer(result)
            except Exception:pass  # telemetry never affects the read or control path
        return result

    @staticmethod
    def require_write_support():
        import websocket
        if not callable(getattr(websocket,'create_connection',None)):
            raise DeviceError('Install websocket-client; the unrelated websocket package cannot control this servo')

    def set(self, values):
        self.require_write_support()
        import websocket
        ws=websocket.create_connection('ws://'+self.host+'/ws',timeout=self.timeout,
                                       http_no_proxy=[self.host])
        try:
            ws.send(json.dumps(values)); reply=json.loads(ws.recv())
            if 'error' in reply:raise DeviceError(str(reply['error']))
            return reply
        finally:ws.close()

    def close(self):pass


# Current-AWG profiles of the sr422 rig (2026-10-22 SDG swap, staging). The BK 4054B path is unchanged byte for byte;
# the SDG2000X differs in: (1) WVDT needs LENGTH,<payload bytes> (proven on the SDG .37 sr1092 path, drivers/siglent
# AWGChannel.upload); (2) a WVDT upload selects and PLAYS the record at once at the header FREQ/AMPL/OFST (burst state
# untouched); (3) every waveform-type change (DC<->ARB) drops the output to ~0 V for ~16 ms (.37 CH2, 16:39Z) -- so the
# LDC channel must never change type on an SDG while the LDC is on; (4) BSWV WVTP,DC exits burst, other BSWV writes do
# not; (5) DLAY,0 reads back a period-relative minimum (recorded only).
BK_PROFILE, SDG_PROFILE = 'bk4050b', 'siglent_sdg2000x'
PROFILE_MAX_POINTS = {BK_PROFILE: 32768, SDG_PROFILE: 8_000_000}
CURRENT_AWG_MODELS = {BK_PROFILE: r'4054B', SDG_PROFILE: r'SDG20\d\dX'}


def awg_profile(io):
    return getattr(io, 'profile', None) or BK_PROFILE


def is_sdg(io):
    return awg_profile(io) == SDG_PROFILE


def check_current_awg_identity(profile, identity):
    """The rig's current AWG must be the model of its configured profile (BK 4054B or SDG2000X)."""
    import re as _re
    if profile not in CURRENT_AWG_MODELS:
        raise DeviceError('Unsupported current AWG profile %r' % (profile,))
    if not _re.search(CURRENT_AWG_MODELS[profile], str(identity).upper().replace(' ', '')):
        raise DeviceError('Current AWG %r is not the %s model of its profile' % (identity, profile))
    return True


def max_arb_points(profile):
    return PROFILE_MAX_POINTS.get(profile, PROFILE_MAX_POINTS[BK_PROFILE])


def wvdt_header(channel, name, frequency_hz, amp_vpp, offset_v, payload_bytes, profile):
    """WVDT header. BK: no LENGTH (the proven dither/sweep upload, 2026-10-22 v3). SDG2000X: LENGTH,<bytes>."""
    length = 'LENGTH,%d,' % payload_bytes if profile == SDG_PROFILE else ''
    return (f'C{channel}:WVDT WVNM,{name},{length}FREQ,{frequency_hz:.12g},'
            f'AMPL,{amp_vpp:.12g},OFST,{offset_v:.12g},PHASE,0,WAVEDATA,').encode('ascii')


def arb_name_matches(actual,requested):
    # BK stores named ARBs as files and reports the optional .bin suffix.
    def normalized(value):
        return value.removesuffix('.bin') if isinstance(value,str) else None
    return normalized(actual) is not None and normalized(actual)==normalized(requested)


class OffsetAWG:
    """One connection per physical AWG; callers supply only configured channels."""
    profile = BK_PROFILE
    def __init__(self, host, timeout=5.):
        self.host=host
        self.socket=socket.create_connection((host,5025),timeout=timeout)
        self.stream=self.socket.makefile('rb')

    def _send(self, command):
        self.socket.sendall((command+'\n').encode('ascii'))

    def query(self, command):
        self._send(command)
        data=self.stream.readline(65537)
        if not data.endswith(b'\n') or len(data)>65536:raise DeviceError('Invalid AWG reply')
        return data.decode('ascii').strip()

    def describe(self):return self.query('*IDN?')

    def read(self, channel):
        wave=fields(self.query(f'C{channel}:BSWV?'))
        output=self.query(f'C{channel}:OUTP?').split(' ',1)[-1].replace(' ','').split(',')
        options=dict(zip(output[1::2],output[2::2]))
        burst=fields(self.query(f'C{channel}:BTWV?'))
        modulation=fields(self.query(f'C{channel}:MDWV?'))
        sweep=fields(self.query(f'C{channel}:SWWV?'))
        return dict(value=number(wave['OFST']),waveform=wave['WVTP'],
                    frequency_hz=number(wave['FRQ']) if 'FRQ' in wave else None,
                    arb_name=fields(self.query(f'C{channel}:ARWV?')).get('NAME') if wave['WVTP']=='ARB' else None,
                    amplitude_vpp=0. if wave['WVTP']=='DC' else number(wave['AMP']),
                    enabled=output[0]=='ON',load=options.get('LOAD'),polarity=options.get('PLRT','NOR'),
                    burst=burst.get('STATE'),modulation=modulation.get('STATE'),sweep=sweep.get('STATE'))

    def offset(self, channel, value):
        # OFST only: preserve waveform, amplitude, channel output and other channel.
        self._send(f'C{channel}:BSWV OFST,{value:.8f}V')
        if self.query('*OPC?')!='1':raise DeviceError('AWG did not acknowledge offset')

    def configure_dither(self,channel,frequency,vpp,previous,programming=None,*,paused=False,keep_bank=False):
        """Upload a named dither ARB using EOI; never change output or AMP.

        Default: the inactive bank (ldcd0 <-> ldcd1) is written and then selected. ``keep_bank`` (and every
        ``paused`` write) overwrites the SELECTED bank in place so the readback name/frequency stay those of the
        verified programming (dither pause 2026-10-27). BK raw 5025 is not binary-safe. The VISA INSTR connection is
        opened inside the same owner process, serialized with its existing SCPI socket.
        """
        from ldcon.drivers.siglent import VisaTransport
        metadata,payload=dither_payload(channel,frequency,vpp,previous,programming,paused=paused,keep_bank=keep_bank)
        header=wvdt_header(channel,metadata["name"],frequency,previous["amplitude_vpp"],previous["value"],len(payload),self.profile)
        io=VisaTransport(f'TCPIP::{self.host}::INSTR',10.,backend='@py')
        try:
            io.write_raw(header+payload)
            if io.query('*OPC?')!='1':raise DeviceError('Inactive dither ARB upload was not acknowledged')
        finally:io.close()
        # Same nominal amplitude/offset in the selected bank; range unchanged.
        for command in (f'C{channel}:ARWV NAME,{metadata["name"]}',f'C{channel}:BSWV FRQ,{frequency:.12g}'):
            self._send(command)
            if self.query('*OPC?')!='1':raise DeviceError('Dither selection was not acknowledged')
        return metadata

    # ---- locked staircase sweep (owner-only; see ecdl_locked_sweep) ----------------------------
    # BK 4054B: any BSWV write exits burst mode, so carrier writes while a burst is armed use
    # BTWV CARR,... (the reviewed sequence of drivers/siglent.AWGChannel.arm). Every write is
    # followed by *OPC?; no write is ever retried here.
    def ack(self,command,what):
        self._send(command)
        if self.query('*OPC?')!='1':raise DeviceError(what+' was not acknowledged')

    def burst_readback(self,channel):
        return fields(self.query(f'C{channel}:BTWV?'))

    def wave_readback(self,channel):
        wave=fields(self.query(f'C{channel}:BSWV?'))
        name=fields(self.query(f'C{channel}:ARWV?')).get('NAME')
        return wave,name

    def upload_arb(self,channel,name,codes,frequency_hz,amp_vpp,offset_v):
        """Store a named ARB (signed 16-bit codes) with EOI over VISA; selection is a separate step.
        The upload's AMPL/OFST are what a later ARWV selection applies (ECDLcontrol sweep.py)."""
        from ldcon.drivers.siglent import VisaTransport
        import numpy as np
        codes=np.asarray(codes,dtype='<i2')
        if not 2<=codes.size<=max_arb_points(self.profile):
            raise DeviceError('ARB length must be 2..%d points on %s' % (max_arb_points(self.profile), self.profile))
        payload=codes.tobytes()
        # BK: same header as the proven dither upload (no LENGTH; 2026-10-22 R0: a LENGTH header is the leading suspect
        # for a record that never moved the ADC). SDG2000X: LENGTH,<bytes> as on the proven .37 path.
        header=wvdt_header(channel,name,frequency_hz,amp_vpp,offset_v,len(payload),self.profile)
        io=VisaTransport(f'TCPIP::{self.host}::INSTR',10.,backend='@py')
        try:
            io.write_raw(header+payload)
            if io.query('*OPC?')!='1':raise DeviceError('ARB upload was not acknowledged')
        finally:io.close()
        return dict(name=name,points=int(codes.size))

    def arm_burst(self,channel,source):
        """N-cycle (1) burst on the present carrier, start phase 0, triggered by MAN (software) or EXT."""
        if source not in ('MAN','EXT'):raise DeviceError('Burst source must be MAN or EXT')
        for command in ('BTWV STATE,ON',f'BTWV TRSR,{source}','BTWV GATE_NCYC,NCYC','BTWV TIME,1',
                        'BTWV STPS,0','BTWV DLAY,0')+(('BTWV EDGE,RISE',) if source=='EXT' else ()):
            self.ack(f'C{channel}:{command}','Burst setting '+command)

    def set_burst_cycles(self,channel,cycles):
        """v5.4 (continuous triangle): N-cycle count of an ARMED burst (BTWV TIME,N), acknowledged and read back.
        The AWG itself then bounds the run to N records. A readback other than N is a DeviceError (caller restores)."""
        if type(cycles) is not int or not 1<=cycles<=1000000:raise DeviceError('Burst cycle count must be an integer 1..1000000')
        self.ack(f'C{channel}:BTWV TIME,{cycles}','Burst cycle count')
        back=self.burst_readback(channel)
        try:got=int(float(str(back.get('TIME')).upper().rstrip('CYCLES').strip()))
        except (TypeError,ValueError):got=None
        if got!=cycles or back.get('STATE')!='ON':
            raise DeviceError('Burst cycle count readback mismatch (want TIME,%d): %s' % (cycles,json.dumps(back)))
        return back

    def select_burst_arb(self,channel,name,frequency_hz,amp_vpp,offset_v):
        """Select a stored ARB as the carrier of an armed burst (burst stays armed and idle)."""
        self.ack(f'C{channel}:BTWV CARR,WVTP,ARB','Burst carrier type')
        self.ack(f'C{channel}:ARWV NAME,{name}','ARB selection')
        self.ack(f'C{channel}:BTWV CARR,FRQ,{frequency_hz:.12g},AMP,{amp_vpp:.12g},OFST,{offset_v:.8f}','Burst carrier settings')

    def select_continuous_arb(self,channel,name,frequency_hz,amp_vpp,offset_v):
        """Continuous ARB: carrier type first at the given (small) amplitude, then the name, then the values
        (BSWV also leaves burst mode). Same order as ECDLcontrol sweep.py start()."""
        self.ack(f'C{channel}:BSWV WVTP,ARB,FRQ,{frequency_hz:.12g},AMP,{amp_vpp:.12g},OFST,{offset_v:.8f}','ARB carrier')
        self.ack(f'C{channel}:ARWV NAME,{name}','ARB selection')
        self.ack(f'C{channel}:BSWV FRQ,{frequency_hz:.12g},AMP,{amp_vpp:.12g},OFST,{offset_v:.8f}','ARB settings')

    def set_dc(self,channel,offset_v):
        # DC type and offset only (BSWV also leaves burst mode); output switch untouched.
        self.ack(f'C{channel}:BSWV WVTP,DC,OFST,{offset_v:.8f}V','DC level')

    def burst_off(self,channel):
        self.ack(f'C{channel}:BTWV STATE,OFF','Burst disarm')

    def fire(self,channels):
        """Software trigger of MAN-armed bursts: one socket write carrying both MTRIG commands (a few ms
        apart on the instrument), then one *OPC?, then the burst readback of each channel."""
        # One command per write, each acknowledged (every proven BK path does this; 2026-10-22 R0 sent both
        # MTRIG lines in one write and the second channel never played).
        for ch in channels:
            self.ack(f'C{ch}:BTWV MTRIG',f'CH{ch} manual trigger')
        readback={f'CH{ch}':self.burst_readback(ch) for ch in channels}
        return dict(command='; '.join(f'C{ch}:BTWV MTRIG' for ch in channels),readback=readback)

    def fire_one(self,channel):
        self.ack(f'C{channel}:BTWV MTRIG',f'CH{channel} manual trigger')

    def close(self):
        self.stream.close(); self.socket.close()



class VisaAWG(OffsetAWG):
    """The same AWG interface over ONE persistent VISA (VXI-11) session instead of the raw 5025 socket.

    2026-10-22: the SDG2042X broke when a second LAN session (the broker's VXI-11) was opened next to the rig's raw
    socket, and its LAN stack later hung. For an AWG shared with another owner's channel, the rig opens exactly one
    session for its lifetime; the broker's routed transactions use the same session under the rig lock
    (ECDLOwner.shared_awg_session). VXI-11 is binary-safe, so WVDT uploads need no second session either."""
    def __init__(self, host, timeout=5.):
        from ldcon.drivers.siglent import VisaTransport
        self.host=host;self.timeout=timeout
        self.io=VisaTransport(f'TCPIP::{host}::INSTR',max(timeout,10.),backend='@py')

    def _send(self, command):
        self.io.write(command)

    def query(self, command):
        reply=str(self.io.query(command))
        if len(reply)>65536:raise DeviceError('Invalid AWG reply')
        return reply.strip()

    def write_raw(self, data):
        self.io.write_raw(data)

    def configure_dither(self,*args,**kwargs):
        raise DeviceError('ARB programming on a single-session AWG must use its one session (not implemented for the TEC AWG)')

    def upload_arb(self,*args,**kwargs):
        raise DeviceError('ARB programming on a single-session AWG must use its one session (not implemented for the TEC AWG)')

    def close(self):
        self.io.close()


def dither_programming_policy(value):
    """Validate an explicit subset of this BK adapter's qualified envelope."""
    import math
    fields={'channel','frequency_bounds_hz','max_vpp','nominal_amplitude_vpp'}
    if not isinstance(value,dict) or set(value)!=fields:
        raise DeviceError('Explicit device dither programming policy is required')
    bounds=value['frequency_bounds_hz']
    if (not isinstance(bounds,list) or len(bounds)!=2
            or any(type(v) not in (int,float) or not math.isfinite(v) for v in bounds)
            or not .1<=bounds[0]<=bounds[1]<=4.):
        raise DeviceError('Dither frequency policy exceeds qualified 0.1..4 Hz envelope')
    if (type(value['channel']) is not int or value['channel']!=1
            or type(value['max_vpp']) not in (int,float) or not math.isfinite(value['max_vpp'])
            or not 0<value['max_vpp']<=.002
            or type(value['nominal_amplitude_vpp']) not in (int,float)
            or value['nominal_amplitude_vpp']!=.002):
        raise DeviceError('Dither policy exceeds qualified CH1 / 2 mVpp carrier envelope')
    return value


DITHER_BANKS=('ldcd0','ldcd1')


def dither_payload(channel,frequency,vpp,previous,programming=None,*,paused=False,keep_bank=False):
    """8192-point sine ARB (signed 16-bit codes) scaled as vpp / nominal carrier, or -- ``paused`` -- the same record
    with ALL-ZERO codes (``programmed_vpp`` 0.0). ``vpp == 0`` is refused unless ``paused=True`` is explicit: a
    pause is a deliberate removal of the modulation, never an accidental zero. A paused write always keeps the
    selected bank (``keep_bank``): the readback name/frequency remain those of the verified programming."""
    import hashlib,math
    import numpy as np
    policy=dither_programming_policy(programming)
    if type(paused) is not bool or type(keep_bank) is not bool:raise DeviceError('paused/keep_bank must be boolean')
    if channel!=policy['channel'] or previous['waveform']!='ARB' or not previous['enabled']:
        raise DeviceError('Dither programming requires the existing enabled CH1 ARB')
    amplitude=previous['amplitude_vpp']
    lo,hi=policy['frequency_bounds_hz']
    if type(vpp) not in (int,float) or not math.isfinite(vpp):raise DeviceError('Requested dither amplitude must be finite')
    if paused:
        if vpp!=0:raise DeviceError('A paused dither is programmed with vpp 0 (all-zero codes), not %r'%(vpp,))
        keep_bank=True
    elif vpp<=0:
        raise DeviceError('Requested dither amplitude must be positive (vpp 0 only with an explicit paused=True)')
    if not (lo<=frequency<=hi and vpp<=policy['max_vpp'] and vpp<=amplitude):raise DeviceError('Requested dither is outside the device envelope')
    if amplitude!=policy['nominal_amplitude_vpp']:raise DeviceError('Dither programming requires the qualified unchanged carrier range')
    if keep_bank:
        name=next((bank for bank in DITHER_BANKS if arb_name_matches(previous['arb_name'],bank)),None)
        if name is None:raise DeviceError('In-place dither programming requires the selected bank to be one of %s, not %r'%(DITHER_BANKS,previous.get('arb_name')))
    else:
        name='ldcd1' if arb_name_matches(previous['arb_name'],'ldcd0') else 'ldcd0'
    points=8192
    if paused:codes=np.zeros(points,dtype='<i2')
    else:codes=np.rint(vpp/amplitude*32767*np.sin(2*np.pi*np.arange(points)/points)).astype('<i2')
    payload=codes.tobytes();effective=float((codes.max()-codes.min())/65534*amplitude)
    return dict(name=name,points=points,frequency_hz=frequency,requested_vpp=vpp,
        programmed_vpp=effective,nominal_amplitude_vpp=amplitude,offset_v=previous['value'],paused=paused,
        payload_sha256=hashlib.sha256(payload).hexdigest(),amplitude_basis='signed 16-bit ARB codes and unchanged nominal carrier; no independent voltage measurement'),payload

