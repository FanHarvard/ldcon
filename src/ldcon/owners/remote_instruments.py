"""Clients of per-instrument owners (rule G). No process but the owner opens the instrument.

``RemoteAWG`` presents the rig's ``drivers.ecdl.OffsetAWG`` interface (read/offset/configure_dither/upload_arb/
arm_burst/.../fire) on top of the owner RPC: every SCPI exchange is a ``transaction`` on the lease of the channel
it addresses; multi-step readbacks are ONE transaction so no other lease holder can interleave. ARB uploads go
through the owner's single VXI-11 session (no transient VISA session per upload any more).

``RemoteServo`` presents ``drivers.ecdl.Servo`` (get/set) on top of an esp32servocon owner.

Both resolve the owner endpoint through ``device_registry.RegistryClient`` (registry -> published state ->
static endpoint), verify the owner's canonical identity against what the laser wiring expects, and hold one
lease per channel for their lifetime. An owner RPC failure raises ``DeviceError``/``OSError`` to the rig, which
faults (quarantine) exactly as for a direct I/O error: the lease is retained for review, nothing is retried.
"""
import base64
import json
import os
import socket
from urllib.request import Request, build_opener, ProxyHandler

from ldcon.core.contracts import DeviceError
from ldcon.drivers.ecdl import OffsetAWG, Servo
from ldcon.drivers.siglent import fields, number
from ldcon.core.instrument_client import BrokerError
from ldcon.owners.instrument_identity import canonical


class OwnerLink:
    """One owner endpoint; RPC with the broker reply shape."""
    def __init__(self, endpoint, instrument, timeout_s=30., post=None):
        self.endpoint, self.instrument, self.timeout_s = endpoint.rstrip('/'), canonical(instrument), timeout_s
        self.http = build_opener(ProxyHandler({}))
        self.post = post

    def rpc(self, action, **values):
        payload = dict(action=action, instrument=self.instrument, **values)
        if self.post is not None:
            return self.post(self.endpoint, payload, self.timeout_s)
        request = Request(self.endpoint + '/rpc', data=json.dumps(payload).encode(),
                          headers={'Content-Type': 'application/json'}, method='POST')
        token = os.environ.get('LDCON_INSTRUMENT_TOKEN')
        if token:
            request.add_header('Authorization', 'Bearer ' + token)
        with self.http.open(request, timeout=self.timeout_s) as response:
            reply = json.load(response)
        if reply.get('ok') is not True:
            raise BrokerError(reply.get('code', 'owner'), reply.get('error', 'owner request rejected'))
        return reply['result']


def resolve_link(spec, *, registry=None, post=None, resolver=socket.gethostbyname, expected_host=None):
    """``spec`` = {instrument, endpoint?, channels?/channel, observe?}; returns a verified ``OwnerLink``."""
    from ldcon.owners.device_registry import RegistryClient
    instrument = canonical(spec['instrument'])
    # Without a registry client the spec's static endpoint is the ONLY source (no published-state lookup: that
    # is how a test reached the live BK owner on 2026-10-22).
    client = registry if registry is not None else RegistryClient(None, state_dir=None)
    if registry is None and not spec.get('endpoint'):
        raise DeviceError('No registry and no static endpoint for %s' % instrument)
    endpoint = client.resolve(instrument, static_endpoint=spec.get('endpoint'))
    link = OwnerLink(endpoint, instrument, post=post)
    description = link.rpc('describe')
    if description.get('instrument') != instrument:
        raise DeviceError('Owner at %s serves %r, not %s' % (endpoint, description.get('instrument'), instrument))
    if description.get('fault'):
        raise DeviceError('Owner of %s requires review: %s' % (instrument, description['fault']))
    if not description.get('connected'):
        raise DeviceError('Owner of %s is not connected to its instrument' % instrument)
    if expected_host is not None:
        wanted = resolver(expected_host)
        if description.get('address') != wanted:
            raise DeviceError('Laser wiring names %s (%s) but the owner of %s holds %s' % (expected_host, wanted, instrument, description.get('address')))
    link.description = description
    return link


class RemoteAWG(OffsetAWG):
    def __init__(self, spec, *, registry=None, post=None, owner='ldcon rig', expected_host=None, resolver=socket.gethostbyname):
        self.link = resolve_link(spec, registry=registry, post=post, resolver=resolver, expected_host=expected_host)
        self.host = self.link.description.get('host')
        self.spec = dict(spec)
        # 2026-10-22 SDG swap: the AWG profile comes from the laser wiring (instruments config) and must equal the
        # owner's own profile (the owner refuses a claim with a different profile).
        self.profile = spec.get('profile') or self.link.description.get('profile') or 'bk4050b'
        self.leases = {}
        self.projection = None
        self.uncertain = False
        channels = spec.get('channels') or [spec['channel']]
        observe = [int(c) for c in spec.get('observe', [])]
        try:
            for ch in channels:
                result = self.link.rpc('claim', channel=int(ch), observe=[c for c in observe if c != int(ch)],
                                       owner=owner, resource=spec.get('resource'), profile=spec.get('profile'))
                self.leases[int(ch)] = result['lease']
            self.identity = self.link.description.get('identity')
        except BaseException:
            self.close()
            raise

    # ---- transport ---------------------------------------------------------------------------
    @staticmethod
    def _channel_of(command):
        if command in ('*IDN?', '*OPC?'):
            return None
        if command.startswith('C') and ':' in command:
            return int(command[1:command.index(':')])
        raise DeviceError('Unsupported command for a remote AWG: ' + command)

    def _lease_for(self, channel):
        if channel is None:
            return next(iter(self.leases.values()))
        if channel in self.leases:
            return self.leases[channel]
        # read-only observation of a reserved channel runs on any lease that observes it
        for ch, lease in self.leases.items():
            if channel in [int(c) for c in self.spec.get('observe', [])]:
                return lease
        raise DeviceError('No lease covers channel %d' % channel)

    def transaction(self, channel, operations):
        lease = self._lease_for(channel)
        try:
            if any(op.get('op') not in ('query', 'get') for op in operations):
                if self.projection is None:
                    raise DeviceError('A laser system projection is required before AWG writes')
                return self.projection.transaction(channel, operations)
            return self.link.rpc('transaction', lease=lease, operations=operations)
        except BrokerError as exc:
            if exc.code in ('fault', 'io'):
                raise OSError(str(exc)) from exc       # the rig treats this as an I/O fault (quarantine)
            raise DeviceError(str(exc)) from exc

    def configure_projection(self, authority, constraints, resources=None):
        from ldcon.owners.projection_transport import ProjectionClient
        selected = dict(self.leases)
        if resources is not None:
            selected = {ch: lease for ch, lease in selected.items() if 'CH' + str(ch) in resources}
        if not selected:
            raise DeviceError('No AWG channel is covered by the system projection')
        if self.projection is None:
            self.projection = ProjectionClient(authority, self.link, self.spec.get('projection', {}), selected, constraints)
        else:
            if self.projection.authority is not authority or self.projection.leases != selected:
                raise DeviceError('Remote projection authority or resource scope changed')
            self.projection.configure(constraints)

    def _send(self, command):
        self.transaction(self._channel_of(command), [dict(op='write', data=command)])

    def query(self, command):
        return str(self.transaction(self._channel_of(command), [dict(op='query', data=command)])[0]).strip()

    def write_raw(self, data):
        header = data.split(b'WAVEDATA,', 1)[0].decode('ascii')
        self.transaction(self._channel_of(header), [dict(op='write_raw', data=base64.b64encode(data).decode('ascii'))])

    def ack(self, command, what):
        result = self.transaction(self._channel_of(command), [dict(op='write', data=command), dict(op='query', data='*OPC?')])
        if str(result[-1]).strip() != '1':
            raise DeviceError(what + ' was not acknowledged')

    def offset(self, channel, value):
        self.ack('C%d:BSWV OFST,%.8fV' % (channel, value), 'AWG offset')

    def describe(self):
        return self.query('*IDN?')

    def read(self, channel):
        keys = ('BSWV', 'OUTP', 'BTWV', 'MDWV', 'SWWV', 'ARWV')
        replies = self.transaction(int(channel), [dict(op='query', data='C%d:%s?' % (channel, k)) for k in keys])
        wave = fields(replies[0])
        output = str(replies[1]).split(' ', 1)[-1].replace(' ', '').split(',')
        options = dict(zip(output[1::2], output[2::2]))
        burst, modulation, sweep = fields(replies[2]), fields(replies[3]), fields(replies[4])
        return dict(value=number(wave['OFST']), waveform=wave['WVTP'],
                    frequency_hz=number(wave['FRQ']) if 'FRQ' in wave else None,
                    arb_name=fields(replies[5]).get('NAME') if wave['WVTP'] == 'ARB' else None,
                    amplitude_vpp=0. if wave['WVTP'] == 'DC' else number(wave['AMP']),
                    enabled=output[0] == 'ON', load=options.get('LOAD'), polarity=options.get('PLRT', 'NOR'),
                    burst=burst.get('STATE'), modulation=modulation.get('STATE'), sweep=sweep.get('STATE'))

    def configure_dither(self, channel, frequency, vpp, previous, programming=None, *, paused=False, keep_bank=False):
        """Same payload and command sequence as OffsetAWG.configure_dither, on the owner's one session."""
        from ldcon.drivers.ecdl import dither_payload, wvdt_header
        metadata, payload = dither_payload(channel, frequency, vpp, previous, programming, paused=paused, keep_bank=keep_bank)
        header = wvdt_header(channel, metadata['name'], frequency, previous['amplitude_vpp'], previous['value'], len(payload), self.profile)
        result = self.transaction(channel, [dict(op='write_raw', data=base64.b64encode(header + payload).decode('ascii')),
                                            dict(op='query', data='*OPC?')])
        if str(result[-1]).strip() != '1':
            raise DeviceError('Inactive dither ARB upload was not acknowledged')
        for command in ('C%d:ARWV NAME,%s' % (channel, metadata['name']), 'C%d:BSWV FRQ,%.12g' % (channel, frequency)):
            self.ack(command, 'Dither selection')
        return metadata

    def upload_arb(self, channel, name, codes, frequency_hz, amp_vpp, offset_v):
        import numpy as np
        from ldcon.drivers.ecdl import max_arb_points, wvdt_header
        codes = np.asarray(codes, dtype='<i2')
        if not 2 <= codes.size <= max_arb_points(self.profile):
            raise DeviceError('ARB length must be 2..%d points on %s' % (max_arb_points(self.profile), self.profile))
        header = wvdt_header(channel, name, frequency_hz, amp_vpp, offset_v, codes.size * 2, self.profile)
        result = self.transaction(channel, [dict(op='write_raw', data=base64.b64encode(header + codes.tobytes()).decode('ascii')),
                                            dict(op='query', data='*OPC?')])
        if str(result[-1]).strip() != '1':
            raise DeviceError('ARB upload was not acknowledged')
        return dict(name=name, points=int(codes.size))

    def channel_environment(self, channel):
        return self.link.rpc('channel_environment', lease=self.leases[int(channel)])

    def close(self):
        for ch, lease in list(self.leases.items()):
            try:
                self.link.rpc('release', lease=lease)
            except Exception:
                self.uncertain = True
            else:
                self.leases.pop(ch, None)

    # ---- owner restart with a running native lock (design §5, 2026-10-27) ----------------------
    def detach(self):
        """Drop this process's transport WITHOUT releasing the channel leases: the next rig owner adopts them
        (``adopt``). No RPC, no instrument write."""
        self.link = None

    @classmethod
    def adopt(cls, spec, leases, *, registry=None, post=None, owner='ldcon rig', expected_host=None,
              resolver=socket.gethostbyname):
        """Re-attach to channel leases a previous rig owner process retained at its deliberate shutdown.

        Read-only: the owner is resolved and verified as for a claim, then every retained lease is checked with ONE
        read-only query on its channel (an 'ownership' refusal means the lease is no longer held). Nothing is
        claimed, written or released; a lease that is gone raises DeviceError (the rig refuses adoption)."""
        self = cls.__new__(cls)
        self.link = resolve_link(spec, registry=registry, post=post, resolver=resolver, expected_host=expected_host)
        self.host = self.link.description.get('host')
        self.spec = dict(spec)
        self.profile = spec.get('profile') or self.link.description.get('profile') or 'bk4050b'
        self.leases = {int(ch): str(token) for ch, token in dict(leases).items()}
        self.projection = None
        self.uncertain = False
        if not self.leases:
            raise DeviceError('No retained channel lease to adopt on ' + str(spec.get('instrument')))
        for ch, lease in self.leases.items():
            try:
                self.link.rpc('transaction', lease=lease, operations=[dict(op='query', data='C%d:OUTP?' % ch)])
            except BrokerError as exc:
                raise DeviceError('Retained lease for CH%d of %s is not honoured by its owner (%s): %s'
                                  % (ch, self.link.instrument, exc.code, exc)) from exc
        self.identity = self.link.description.get('identity')
        return self


class RemoteServo(Servo):
    """esp32servocon through its owner: ``get(path)`` / ``set(values)`` on the device lease."""
    def __init__(self, spec, *, registry=None, post=None, owner='ldcon rig', expected_host=None, resolver=socket.gethostbyname):
        self.link = resolve_link(spec, registry=registry, post=post, resolver=resolver, expected_host=expected_host)
        self.host = self.link.description.get('host')
        self.spec = dict(spec)
        self.projection = None
        self.timeout = 5.
        self.lease = None
        self.lease = self.link.rpc('claim', channel=0, owner=owner)['lease']

    def _transaction(self, operations):
        try:
            if any(op.get('op') not in ('get', 'query') for op in operations):
                if self.projection is None:
                    raise DeviceError('A laser system projection is required before servo writes')
                return self.projection.transaction(0, operations)
            return self.link.rpc('transaction', lease=self.lease, operations=operations)
        except BrokerError as exc:
            if exc.code in ('fault', 'io'):
                raise OSError(str(exc)) from exc
            raise DeviceError(str(exc)) from exc

    def configure_projection(self, authority, constraints, resources=None):
        from ldcon.owners.projection_transport import ProjectionClient
        if resources is not None and list(resources) != ['servo']:
            raise DeviceError('Servo projection scope must be the whole servo')
        if self.projection is None:
            self.projection = ProjectionClient(authority, self.link, self.spec.get('projection', {}), {0: self.lease}, constraints)
        else:
            if self.projection.authority is not authority:
                raise DeviceError('Remote servo authority changed')
            self.projection.configure(constraints)

    def get(self, path):
        return self._transaction([dict(op='get', data='/' + path.lstrip('/'))])[0]

    @staticmethod
    def require_write_support():
        return None

    def set(self, values):
        return self._transaction([dict(op='set', data=dict(values))])[0]

    def close(self):
        if self.lease is not None:
            self.link.rpc('release', lease=self.lease)
            self.lease = None

    def detach(self):
        """Drop the transport without releasing the device lease (owner restart with a running lock, design §5)."""
        self.link = None

    @classmethod
    def adopt(cls, spec, lease, *, registry=None, post=None, owner='ldcon rig', expected_host=None,
              resolver=socket.gethostbyname):
        """Re-attach to the device lease a previous rig owner retained; verified with one read-only status get."""
        self = cls.__new__(cls)
        self.link = resolve_link(spec, registry=registry, post=post, resolver=resolver, expected_host=expected_host)
        self.host = self.link.description.get('host')
        self.spec = dict(spec)
        self.projection = None
        self.timeout = 5.
        self.lease = str(lease)
        try:
            self.link.rpc('transaction', lease=self.lease, operations=[dict(op='get', data='/status')])
        except BrokerError as exc:
            raise DeviceError('Retained servo lease of %s is not honoured by its owner (%s): %s'
                              % (self.link.instrument, exc.code, exc)) from exc
        return self
