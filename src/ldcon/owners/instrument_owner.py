"""One owner process per PHYSICAL instrument (rule G, 2026-10-22).

    python -m ldcon.owners.instrument_owner --config /opt/ldcon/var/instrument-owners/<model>-<serial>/config.json

Contract (card-system-design.md, rule G):
  identity   the owner is configured with the canonical identity (``<model>:<serial>``) and takes the identity
             lock (``instrument_identity.IdentityLock``) PLUS the manifest-pinned endpoint lock of the resolved
             address before it opens anything; the instrument's reported identity must equal the configured one
             or the owner latches a fault and serves no lease.
  session    exactly ONE connection to the instrument for the owner's lifetime; no per-operation reconnects.
             A lost connection is a fault (``reconnect: manual``): the owner stays up, publishes the fault and
             waits for an explicit ``reconnect`` RPC (or a restart); nothing is retried on its own.
  leases     per channel (AWG CH1/CH2, or channel 0 = the whole device), persisted; writes are accepted only on
             the leased channel; ``observe`` channels may be queried, never written; absolute limits and safety
             checks belong here (adapter), not in the registry.
  safe start the start is read-only: identity, then a readback of every channel, optionally compared with
             ``safe_start.expect``; no output is changed at start and nothing is written before a lease exists.
  publish    ``<state_dir>/owner-state.json`` (endpoint, pid, identity, leases, fault, readback) for the
             registry and for clients when the registry is down; ``<state_dir>/state.json`` is the durable lease
             record (a restart with retained leases is a fault that needs review, as for the other owners).
  faults     any I/O error during a transaction faults the owner and all its leases (clients see the resource
             unavailable and follow their own watchdog/fail-safe); nothing is retried.

RPC (same shape as the instrument service so ``instrument_client.BrokerTransport`` works unchanged):
  status | describe | claim{channel, observe[], owner, resource?, profile?} | release{lease}
  channel_environment{lease} | transaction{lease, operations[{op, data}]} | reconnect{} (read-only reconnect)
  instrument_settings{} (read-only state, no I/O) | check_instrument_settings{} (one check cycle now)
  enforce_instrument_settings{actor?} (probe-started owner -> enforce, once, without reopening the session)

Instrument-level settings (human request 2026-10-22: AWG reference clocks; card-system-design.md rule G):
  ``instrument_settings: {mode: enforce|probe, check_interval_s: 60, items: [{name, write, query, expect,
  reapply?, requires?}]}`` is the owner's explicit allowlist of GLOBAL commands (headers in ``SETTING_HEADERS``,
  today only ``ROSC``). Clients still cannot send any global SCPI (``AWGAdapter.validate`` is unchanged).
  enforce: after identity + safe start on every connect/reconnect, and every ``check_interval_s`` under the owner
           lock, each item is queried; a value that differs is re-applied with ONE command plus ``*OPC?`` and read
           back. Known-but-wrong readback = status flag (``ok: false``, audit ``instrument_setting_apply_failed``),
           channel work continues. Unknown readback AFTER a write = uncertain write = owner fault (rule G). An
           unparseable reply BEFORE a write is reported (``ok: null``) and nothing is written. ``requires`` gates
           a write on another owner's published, fresh ``ok`` setting (SDG EXT only while the BK 10 MHz out is ON).
  probe:   read-only (CLI ``--settings-probe`` or config ``mode: probe``): the queries are sent on the owner's own
           session, the raw replies are logged, audited and published (``instrument_settings_probe``); no write.
  Any I/O error in these queries/writes is an I/O error of the ONE session: owner + all leases fault (rule G).
"""
import argparse
import base64
import binascii
import copy
import hashlib
import json
import math
import logging
import os
from pathlib import Path
import re
import socket
import threading
import time
import uuid

from ldcon.core.contracts import DeviceError
from ldcon.core.instrument_client import BrokerError
from ldcon.owners.instrument_identity import IdentityLock, canonical, identity_from_idn

MAX_ASCII = 4096
_COMMAND = re.compile(r"C([12]):(BSWV|ARWV|BTWV|OUTP|MDWV|SWWV|SRATE|WVDT)(.*)", re.ASCII)
_ESP_READ_PATHS = ('/analog/config', '/config', '/status', '/dac/limits')


def finite(value, name):
    import math
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(name + ' must be a finite number')
    return float(value)


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o640)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, allow_nan=False, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


# ----------------------------------------------------------------------------------------------- adapters

class AWGAdapter:
    """Siglent SDG2000X / BK 4054B over ONE session (VXI-11 by default; raw 5025 is ASCII-only)."""
    kind = 'awg'
    PROFILES = ('siglent_sdg2000x', 'bk4050b')

    def __init__(self, config):
        self.config = config
        if config.get('profile') not in self.PROFILES:
            raise ValueError('AWG profile must be one of %s' % (self.PROFILES,))
        if config.get('transport', 'vxi11') not in ('vxi11', 'socket'):
            raise ValueError('AWG transport must be vxi11 or socket')
        for ch in config['channels']:
            if ch not in ('1', '2'):
                raise ValueError('AWG channels are "1" and "2"')
        self.io = None

    def open(self, address, timeout_s, factory=None):
        if factory is not None:
            self.io = factory(address, timeout_s)
        elif self.config.get('transport', 'vxi11') == 'vxi11':
            from ldcon.drivers.siglent import VisaTransport
            self.io = VisaTransport('TCPIP::%s::INSTR' % address, max(timeout_s, 10.), backend=self.config.get('visa_backend', '@py'))
        else:
            from ldcon.drivers.ecdl import OffsetAWG
            self.io = OffsetAWG(address, timeout=timeout_s)
        return self.io

    def identity(self):
        return str(self.io.query('*IDN?')).strip()

    def readback(self, channel):
        """Read-only channel state in the rig's ``OffsetAWG.read`` shape (no writes)."""
        from ldcon.drivers.siglent import fields, number
        ch = int(channel)
        wave = fields(self.io.query('C%d:BSWV?' % ch))
        output = str(self.io.query('C%d:OUTP?' % ch)).split(' ', 1)[-1].replace(' ', '').split(',')
        options = dict(zip(output[1::2], output[2::2]))
        burst = fields(self.io.query('C%d:BTWV?' % ch))
        modulation = fields(self.io.query('C%d:MDWV?' % ch))
        sweep = fields(self.io.query('C%d:SWWV?' % ch))
        return dict(value=number(wave['OFST']), waveform=wave['WVTP'],
                    frequency_hz=number(wave['FRQ']) if 'FRQ' in wave else None,
                    arb_name=fields(self.io.query('C%d:ARWV?' % ch)).get('NAME') if wave['WVTP'] == 'ARB' else None,
                    amplitude_vpp=0. if wave['WVTP'] == 'DC' else number(wave['AMP']),
                    enabled=output[0] == 'ON', load=options.get('LOAD'), polarity=options.get('PLRT', 'NOR'),
                    burst=burst.get('STATE'), modulation=modulation.get('STATE'), sweep=sweep.get('STATE'))

    def environment(self):
        from ldcon.drivers.siglent import fields
        identity = self.identity()
        coupling = str(self.io.query('COUP?')).strip()
        text = coupling[5:] if coupling.upper().startswith('COUP ') else coupling
        values = fields(text)
        reasons = []
        missing = [k for k in ('TRACE', 'FCOUP', 'PCOUP', 'ACOUP') if k not in values]
        if missing:
            reasons.append('coupling reply lacks ' + ', '.join(missing))
        on = [k for k, v in values.items() if v.upper() == 'ON']
        if any(values.get(k, 'OFF').upper() != 'OFF' for k in ('TRACE', 'FCOUP', 'PCOUP', 'ACOUP')) or on:
            reasons.append('coupling/tracking is not OFF: ' + ', '.join(sorted(set(on))))
        return dict(identity=identity, coupling=coupling, coupling_fields=values, reasons=reasons)

    def validate(self, operation, lease):
        """Return ``(op, payload)``; refuse anything not addressed to the leased channel (queries may address
        the lease's observe channels)."""
        op, data = operation.get('op'), operation.get('data')
        if op not in ('query', 'write', 'write_raw'):
            raise BrokerError('operation', 'only query/write/write_raw are supported')
        payload = None
        if op == 'write_raw':
            try:
                payload = base64.b64decode(data, validate=True)
                header, body = payload.split(b'WAVEDATA,', 1)
                command = header.decode('ascii') + 'WAVEDATA,'
            except (ValueError, UnicodeError, binascii.Error) as exc:
                raise BrokerError('binary', 'invalid base64 WVDT upload') from exc
            if len(payload) > 64 * 1024 * 1024:
                raise BrokerError('binary', 'WVDT upload exceeds 64 MiB')
        else:
            command = data
        if (not isinstance(command, str) or not command.isascii() or len(command) > MAX_ASCII
                or any(ord(c) < 32 for c in command) or ';' in command):
            raise BrokerError('command', 'SCPI must be one ASCII command without separators')
        if op == 'query' and command in ('*IDN?', '*OPC?'):
            return op, command
        match = _COMMAND.fullmatch(command)
        if match is None:
            raise BrokerError('command', 'unsupported SCPI command')
        channel = int(match[1])
        key, suffix = match[2], match[3]
        if op == 'query':
            if suffix != '?' or key == 'WVDT':
                raise BrokerError('command', 'unsupported query')
            if channel != lease['channel'] and channel not in lease.get('observe', []):
                raise BrokerError('ownership', 'query is not addressed to the leased or observed channel')
            return op, command
        if channel != lease['channel']:
            raise BrokerError('ownership', 'command is not addressed to the leased channel')
        if not suffix.startswith(' ') or '?' in suffix:
            raise BrokerError('command', 'invalid channel write')
        if op == 'write_raw':
            if key != 'WVDT':
                raise BrokerError('binary', 'binary operation must be WVDT')
            from ldcon.drivers.siglent import fields
            declared = fields(command.rstrip(',')).get('LENGTH')      # BK uploads carry no LENGTH (v3 finding)
            if declared is not None and int(declared) != len(body):
                raise BrokerError('binary', 'WVDT LENGTH does not match payload')
            if self.config.get('transport', 'vxi11') != 'vxi11':
                raise BrokerError('binary', 'WVDT needs the VXI-11 (EOI) session; this owner uses the raw socket')
        elif key == 'WVDT':
            raise BrokerError('binary', 'WVDT requires write_raw')
        return op, payload if payload is not None else command

    def execute(self, op, data):
        if op == 'query':
            return str(self.io.query(data)).strip()
        if op == 'write':
            self.io.write(data) if hasattr(self.io, 'write') else self.io._send(data)
            return None
        self.io.write_raw(data)
        return None

    def close(self):
        if self.io is not None:
            try:
                self.io.close()
            finally:
                self.io = None


class Esp32Adapter:
    """esp32servocon board: read-only HTTP GET on an allowlist, writes over its websocket; channel 0 = device."""
    kind = 'esp32servocon'

    def __init__(self, config):
        self.config = config
        if list(config['channels']) != ['0']:
            raise ValueError('esp32servocon owners have exactly channel "0"')
        self.io = None

    def open(self, address, timeout_s, factory=None):
        if factory is not None:
            self.io = factory(address, timeout_s)
        else:
            from ldcon.drivers.ecdl import Servo
            self.io = Servo('%s:%d' % (address, int(self.config.get('device_port', 80))), timeout=timeout_s)
        return self.io

    def identity(self):
        status = self.io.get('status')
        expected = self.config.get('expected_firmware')
        if expected is not None and status.get('firmware_version') != expected:
            raise DeviceError('ESP32 firmware %r differs from the expected %r' % (status.get('firmware_version'), expected))
        return 'esp32servocon,%s,%s' % (self.config['host'], status.get('firmware_version'))

    def readback(self, channel):
        return dict(status=self.io.get('status'), config=self.io.get('config'))

    def environment(self):
        return dict(identity=self.identity(), coupling=None, coupling_fields={}, reasons=[])

    def validate(self, operation, lease):
        op, data = operation.get('op'), operation.get('data')
        if op == 'get':
            path = data if isinstance(data, str) and data.startswith('/') else '/' + str(data)
            if path not in _ESP_READ_PATHS:
                raise BrokerError('command', 'device read path is not allowed')
            return op, path.lstrip('/')
        if op == 'set':
            if not isinstance(data, dict) or not data or any(not isinstance(k, str) for k in data):
                raise BrokerError('command', 'set needs a non-empty object of settings')
            return op, dict(data)
        raise BrokerError('operation', 'only get/set are supported')

    def execute(self, op, data):
        if op == 'get':
            return self.io.get(data)
        return self.io.set(data)

    def close(self):
        if self.io is not None:
            try:
                self.io.close()
            finally:
                self.io = None


ADAPTERS = {AWGAdapter.kind: AWGAdapter, Esp32Adapter.kind: Esp32Adapter}


# ----------------------------------------------------------------------------------------------- instrument settings
# Code-level allowlist of GLOBAL (non channel-prefixed) setting commands an owner config may name. A config cannot
# add a header; it can only choose items with these headers. Clients never send these (validate() refuses them).
ROSC_SOURCES = {'INT': 'INT', 'INTERNAL': 'INT', 'EXT': 'EXT', 'EXTERNAL': 'EXT'}
ROSC_SWITCHES = {'ON': 'ON', 'OFF': 'OFF', '1': 'ON', '0': 'OFF'}


def parse_rosc(reply):
    """Tolerant parser for ``ROSC?`` replies (and for the arguments of a ``ROSC`` write).

    Accepts case/space variants with or without the header echo: ``ROSC INT``, ``INT``, ``rosc ext``,
    ``ROSC INT,10MOUT,ON``, ``ROSC 10MOUT,ON``, ``EXT, 10MOUT, OFF``, ``INTERNAL``. Returns
    ``({'source': INT|EXT|None, '10MOUT': ON|OFF|None}, unknown_tokens)``; raises ``ValueError`` when neither field is
    recognised or a field is contradictory (never guesses)."""
    text = str(reply).strip().upper()
    tokens = [t for t in re.split(r'[\s,]+', text) if t]
    if tokens and tokens[0].lstrip(':') == 'ROSC':
        tokens = tokens[1:]
    fields, unknown, i = {'source': None, '10MOUT': None}, [], 0
    while i < len(tokens):
        token = tokens[i]
        if token in ROSC_SOURCES:
            value = ROSC_SOURCES[token]
            if fields['source'] not in (None, value):
                raise ValueError('contradictory ROSC source in %r' % reply)
            fields['source'] = value
        elif token == '10MOUT':
            if i + 1 >= len(tokens) or tokens[i + 1] not in ROSC_SWITCHES:
                raise ValueError('10MOUT without ON/OFF in %r' % reply)
            fields['10MOUT'] = ROSC_SWITCHES[tokens[i + 1]]
            i += 1
        else:
            unknown.append(token)
        i += 1
    if fields['source'] is None and fields['10MOUT'] is None:
        raise ValueError('unrecognised ROSC reply %r' % reply)
    return fields, unknown


SETTING_HEADERS = {'ROSC': dict(parse=parse_rosc, fields={'source': ('INT', 'EXT'), '10MOUT': ('ON', 'OFF')})}
SETTINGS_MODES = ('enforce', 'probe')
SAFE_START_KEYS = {'waveform', 'value', 'tolerance_v', 'value_range', 'enabled', 'load', 'polarity', 'burst', 'modulation',
                   'sweep', 'arb_name', 'amplitude_vpp', 'frequency_hz'}
DEFAULT_SETTINGS_INTERVAL_S = 60.
_SETTING_NAME = re.compile(r'[a-z][a-z0-9_]{0,47}', re.ASCII)
_SETTING_WRITE = re.compile(r'([A-Z]+) ([A-Z0-9]+(?:,[A-Z0-9]+)*)', re.ASCII)
_SETTING_QUERY = re.compile(r'([A-Z]+)\?(?: ([A-Z0-9]+(?:,[A-Z0-9]+)*))?', re.ASCII)


def validate_instrument_settings(block, kind):
    """The owner's allowlist of instrument-level settings; refuses anything that is not explicit and consistent."""
    if kind != 'awg':
        raise ValueError('instrument_settings are supported on AWG owners only')
    if not isinstance(block, dict) or not {'items'} <= set(block) <= {'items', 'mode', 'check_interval_s'}:
        raise ValueError('instrument_settings must be {items, mode?, check_interval_s?}')
    if block.get('mode', 'enforce') not in SETTINGS_MODES:
        raise ValueError('instrument_settings.mode must be one of %s' % (SETTINGS_MODES,))
    if not 10 <= finite(block.get('check_interval_s', DEFAULT_SETTINGS_INTERVAL_S), 'check_interval_s') <= 3600:
        raise ValueError('instrument_settings.check_interval_s must be in [10, 3600]')
    items = block['items']
    if not isinstance(items, list) or not 1 <= len(items) <= 8:
        raise ValueError('instrument_settings.items must list 1..8 settings')
    names = set()
    for item in items:
        if not isinstance(item, dict) or not {'name', 'write', 'query', 'expect'} <= set(item) <= {'name', 'write', 'query', 'expect', 'reapply', 'requires', 'description'}:
            raise ValueError('each instrument setting is {name, write, query, expect, reapply?, requires?, description?}')
        name = item['name']
        if not isinstance(name, str) or not _SETTING_NAME.fullmatch(name) or name in names:
            raise ValueError('instrument setting name %r must be unique snake_case' % (name,))
        names.add(name)
        write = _SETTING_WRITE.fullmatch(item['write']) if isinstance(item['write'], str) else None
        query = _SETTING_QUERY.fullmatch(item['query']) if isinstance(item['query'], str) else None
        if write is None or query is None:
            raise ValueError('%s: write must be "<HEADER> <ARGS>" and query "<HEADER>?" (one ASCII command, upper case, no separators)' % name)
        header = write[1]
        if header not in SETTING_HEADERS or query[1] != header:
            raise ValueError('%s: only the global setting headers %s are allowed, write and query on the same header' % (name, sorted(SETTING_HEADERS)))
        spec = SETTING_HEADERS[header]
        expect = item['expect']
        if not isinstance(expect, dict) or not expect or any(k not in spec['fields'] or v not in spec['fields'][k] for k, v in expect.items()):
            raise ValueError('%s: expect must map %s fields to their allowed values' % (name, header))
        try:
            written, unknown = spec['parse'](write[2])
        except ValueError as exc:
            raise ValueError('%s: write arguments not understood: %s' % (name, exc)) from exc
        if unknown or {k: v for k, v in written.items() if v is not None} != expect:
            raise ValueError('%s: the write %r does not set exactly the expected value %r' % (name, item['write'], expect))
        if type(item.get('reapply', True)) is not bool:
            raise ValueError('%s: reapply must be a bool' % name)
        requires = item.get('requires')
        if requires is not None:
            if (not isinstance(requires, dict) or not {'state_file', 'setting'} <= set(requires) <= {'state_file', 'setting', 'max_age_s', 'instrument'}
                    or not isinstance(requires['state_file'], str) or not os.path.isabs(requires['state_file'])
                    or not isinstance(requires['setting'], str) or not _SETTING_NAME.fullmatch(requires['setting'])):
                raise ValueError('%s: requires is {state_file (absolute owner-state.json), setting, max_age_s?, instrument?}' % name)
            if not 1 <= finite(requires.get('max_age_s', 180.), 'max_age_s') <= 86400:
                raise ValueError('%s: requires.max_age_s must be in [1, 86400]' % name)
    return block


# ----------------------------------------------------------------------------------------------- owner core

def validate_config(config):
    config = copy.deepcopy(config)
    required = {'instrument', 'kind', 'host', 'channels', 'bind', 'port', 'state_dir', 'lock_dir'}
    optional = {'profile', 'transport', 'visa_backend', 'resource', 'expected_identity', 'expected_firmware',
                'device_port', 'timeout_s', 'endpoint_lock', 'reconnect', 'safe_start', 'description', 'registry',
                'capabilities', 'projection_sources', 'instrument_settings'}
    if not isinstance(config, dict) or not required <= set(config) <= required | optional:
        raise ValueError('Owner configuration keys: required %s, optional %s' % (sorted(required), sorted(optional)))
    canonical(config['instrument'])
    if config['kind'] not in ADAPTERS:
        raise ValueError('Unknown owner kind %r' % config['kind'])
    if not isinstance(config['host'], str) or not config['host']:
        raise ValueError('host is required')
    import ipaddress
    if not ipaddress.ip_address(config['bind']).is_loopback:
        raise ValueError('Owners bind to loopback only (clients are local: rig, relay, native owners)')
    if type(config['port']) is not int or not 0 < config['port'] < 65536:
        raise ValueError('Invalid port')
    if not isinstance(config['channels'], dict) or not config['channels']:
        raise ValueError('channels must be a non-empty mapping')
    for ch, policy in config['channels'].items():
        if not isinstance(policy, dict) or type(policy.get('writes_enabled')) is not bool:
            raise ValueError('channel %r needs writes_enabled (bool)' % ch)
        if policy.get('require_channel_environment') not in (None, True, False):
            raise ValueError('require_channel_environment must be a bool')
    if config.get('reconnect', 'manual') != 'manual':
        raise ValueError('Only reconnect: manual is supported (no automatic session re-establishment)')
    if 'timeout_s' in config and not 0 < finite(config['timeout_s'], 'timeout_s') <= 60:
        raise ValueError('timeout_s must be in (0, 60]')
    lock = config.get('endpoint_lock')
    if lock is not None and (not isinstance(lock, dict) or type(lock.get('port')) is not int or 'kinds' in lock and not isinstance(lock['kinds'], list)):
        raise ValueError('endpoint_lock must be {port, kinds?}')
    if config['kind'] == 'awg' and not config.get('expected_identity'):
        raise ValueError('AWG owners pin expected_identity (*IDN?)')
    allowed = {'awg': {'1', '2'}, 'esp32servocon': {'0'}}[config['kind']]
    if not set(config['channels']) <= allowed:
        raise ValueError('%s channels must be within %s' % (config['kind'], sorted(allowed)))
    from experiment_core.records import safe_id
    sources = config.get('projection_sources', {})
    if not isinstance(sources, dict):
        raise ValueError('projection_sources must be a host-configured mapping')
    occupied = set()
    for principal, source in sources.items():
        safe_id(principal)
        if not isinstance(source, dict) or set(source) != {'system_id', 'key_env', 'channels'}:
            raise ValueError('Projection source requires system_id, key_env and channels')
        safe_id(source['system_id'])
        if not isinstance(source['key_env'], str) or not source['key_env']:
            raise ValueError('Projection source requires a signing key environment name')
        if (not isinstance(source['channels'], list) or not source['channels']
                or any(type(ch) is not int or str(ch) not in config['channels'] for ch in source['channels'])
                or occupied.intersection(source['channels'])):
            raise ValueError('Projection source channel scopes must be configured and disjoint')
        occupied.update(source['channels'])
    caps = config.get('capabilities')
    if caps is not None:
        keys = {'voltage_bounds_v', 'max_vpp', 'max_points'} if config['kind'] == 'awg' else {'dac_bounds'}
        if not isinstance(caps, dict) or set(caps) != keys:
            raise ValueError('Physical capabilities require ' + str(sorted(keys)))
        bounds = caps['voltage_bounds_v' if config['kind'] == 'awg' else 'dac_bounds']
        if not isinstance(bounds, list) or len(bounds) != 2 or finite(bounds[0], 'capability lower') >= finite(bounds[1], 'capability upper'):
            raise ValueError('Physical capability bounds are invalid')
        if config['kind'] == 'awg' and (finite(caps['max_vpp'], 'max_vpp') <= 0
                or type(caps['max_points']) is not int or caps['max_points'] < 2):
            raise ValueError('Invalid physical AWG capabilities')
    safe = config.get('safe_start') or {}
    if not isinstance(safe, dict):
        raise ValueError('safe_start must be a mapping')
    for ch, expect in (safe.get('expect') or {}).items():
        if ch not in config['channels'] or not isinstance(expect, dict):
            raise ValueError('safe_start.expect keys must be configured channels')
        # 2026-10-22 (SDG swap / TEC moves): only readback fields and the value rules are accepted, so a misspelt key
        # can no longer latch every start. ``value_range: [lo, hi]`` accepts a level that legitimately moves (the sr422
        # TEC centring, the LDC current window) inside an approved envelope; it excludes an exact ``value``.
        unknown = set(expect) - SAFE_START_KEYS
        if unknown:
            raise ValueError('safe_start.expect.%s: unknown keys %s (allowed %s)' % (ch, sorted(unknown), sorted(SAFE_START_KEYS)))
        if 'value_range' in expect:
            rng = expect['value_range']
            if ('value' in expect or 'tolerance_v' in expect or not isinstance(rng, list) or len(rng) != 2
                    or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in rng) or not rng[0] < rng[1]):
                raise ValueError('safe_start.expect.%s.value_range must be [lo, hi] (finite, lo < hi) without value/tolerance_v' % ch)
    if config.get('instrument_settings') is not None:
        validate_instrument_settings(config['instrument_settings'], config['kind'])
    return config


def definition_digest(config):
    # The LEASE definition: instrument_settings (like safe_start) are not part of it, so adding/changing them never
    # turns a saved state.json into "Saved owner definition differs; offline review required".
    keys = {k: v for k, v in config.items() if k not in ('bind', 'port', 'state_dir', 'lock_dir', 'description', 'registry', 'safe_start',
                                                         'instrument_settings')}
    return hashlib.sha256(json.dumps(keys, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class InstrumentOwner:
    def __init__(self, config, *, io_factory=None, clock=time.monotonic, wall_clock=time.time,
                 identity_lock=IdentityLock, endpoint_lock=None, resolver=socket.gethostbyname, unit=None,
                 settings_probe=False):
        self.config = validate_config(config)
        self.instrument = self.config['instrument']
        self.adapter = ADAPTERS[self.config['kind']](self.config)
        self.clock, self.wall_clock = clock, wall_clock
        block = self.config.get('instrument_settings') or {}
        self.setting_items = list(block.get('items') or [])
        self.settings_mode = 'probe' if settings_probe else block.get('mode', 'enforce')
        if settings_probe and self.config['kind'] != 'awg':
            raise ValueError('--settings-probe is for AWG owners')
        self.settings_interval_s = float(block.get('check_interval_s', DEFAULT_SETTINGS_INTERVAL_S))
        self.settings = {item['name']: dict(expected=dict(item['expect']), actual=None, ok=None, raw=None, error=None,
                                            last_checked_unix_s=None, last_applied_unix_s=None, applies=0,
                                            mismatches=0, write=item['write'], query=item['query'])
                         for item in self.setting_items}
        self.settings_probe = None
        self.settings_stop = threading.Event()
        self.settings_thread = None
        self.lock = threading.RLock()
        self.leases = {}
        self.fault = None
        self.connected = False
        self.identity = None
        self.readback = {}
        self.startup_check = None
        self.closed = False
        self.unit = unit
        self.io_factory = io_factory
        self.audit_entries = 0
        self.state_dir = Path(self.config['state_dir'])
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.state_dir / 'state.json'
        self.published_file = self.state_dir / 'owner-state.json'
        self.audit_file = self.state_dir / 'audit.jsonl'
        self.definition = definition_digest(self.config)
        self.endpoint = 'http://%s:%d' % (self.config['bind'], self.config['port'])
        self.address = resolver(self.config['host'])
        self.locks = []
        self.projections = None
        try:
            # Exclusion first: nothing is opened before both layers are held.
            self.locks.append(identity_lock(self.instrument, self.config['lock_dir']))
            lock = self.config.get('endpoint_lock')
            if lock is not None:
                if endpoint_lock is None:
                    from ldcon.owners.migration_physical_lock import acquire_endpoint
                    endpoint_lock = acquire_endpoint
                self.locks.append(endpoint_lock(self.address, lock['port'], tuple(lock.get('kinds', ['physical']))))
            from ldcon.owners.instrument_projection import InstrumentProjection
            self.projections = InstrumentProjection(self)
            if self.state_file.exists():
                record = json.loads(self.state_file.read_text())
                if record.get('definition') != self.definition:
                    raise DeviceError('Saved owner definition differs; offline review required')
                self.leases = record.get('leases') or {}
                self.fault = record.get('fault')
                if self.leases:
                    self.fault = self.fault or 'Previous owner exited with retained leases; offline review required'
            self._persist()
            if not self.fault:
                # Read-only: identity + readback; no output change. A refused connection, an identity mismatch or a
                # safe-start mismatch is a LATCHED fault: the owner stays up (locks held, no lease served, state
                # published) so the registry and the operator can see it and `reconnect` after review.
                self._try_connect()
            self.publish()
        except BaseException:
            self.close()
            raise

    def _try_connect(self):
        try:
            self._connect()
        except BrokerError:
            pass                      # already latched by _connect
        except Exception as exc:
            self.connected = False
            self._latch('connect failed; review, then reconnect explicitly: %s' % exc)

    # ---- durable state -----------------------------------------------------------------------
    def _persist(self):
        atomic_json(self.state_file, dict(schema_version=1, definition=self.definition, leases=self.leases, fault=self.fault))

    def audit(self, kind, **values):
        fd = os.open(self.audit_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o640)
        with os.fdopen(fd, 'a') as f:
            f.write(json.dumps(dict(kind=kind, unix_s=self.wall_clock(), **values), allow_nan=False, default=str) + '\n')
        self.audit_entries += 1

    def publish(self):
        """What the registry (and clients, when the registry is down) read."""
        atomic_json(self.published_file, self.status())

    def _latch(self, reason):
        self.fault = str(reason)[:1000]
        try:
            self._persist()
        except OSError as exc:
            self.fault += '; state persistence failed: ' + str(exc)
        self.audit('fault', fault=self.fault)
        self.publish()
        return BrokerError('fault', self.fault)

    # ---- the ONE session -----------------------------------------------------------------------
    def _connect(self):
        timeout = float(self.config.get('timeout_s', 10.))
        self.adapter.open(self.address, timeout, self.io_factory)
        observed = self.adapter.identity()
        expected = self.config.get('expected_identity')
        if expected is not None and observed != str(expected).strip():
            raise self._latch('Instrument identity %r differs from the configured %r; no lease is served' % (observed, expected))
        if self.config['kind'] == 'awg':
            canonical_id, _ = identity_from_idn(observed)
            if canonical_id != self.instrument:
                raise self._latch('Instrument reports %s but this owner is %s' % (canonical_id, self.instrument))
        self.identity = observed
        self.readback = {ch: self.adapter.readback(ch) for ch in self.config['channels']}
        self.connected = True
        self.startup_check = self._safe_start_check()
        self.audit('connected', identity=observed, readback=self.readback, startup_check=self.startup_check)
        if self.startup_check and not self.startup_check['ok']:
            raise self._latch('Safe-start readback differs from expectation: ' + '; '.join(self.startup_check['reasons']))
        # Instrument-level settings revert at every power cycle: (re)apply them on EVERY connect, after identity and
        # the read-only safe start, before any lease can exist. The channel readback above is taken first, so the
        # published readback is the one the safe-start check compared.
        self._settings_cycle('connect')

    # ---- instrument-level settings (global allowlisted commands; see the module docstring) ---------------------
    def settings_enabled(self):
        return bool(self.setting_items) or self.settings_mode == 'probe'

    def _settings_query(self, query, replies):
        if query not in replies:
            try:
                replies[query] = str(self.adapter.execute('query', query)).strip()
            except Exception as exc:
                raise self._io_failed('instrument setting query %s failed: %s' % (query, exc)) from exc
        return replies[query]

    @staticmethod
    def _settings_parse(item, raw):
        """-> (actual fields restricted to the expected keys, error). ``actual`` is None when unknown."""
        spec = SETTING_HEADERS[item['query'].split('?', 1)[0]]
        try:
            fields, _ = spec['parse'](raw)
        except ValueError as exc:
            return None, 'reply not understood: %s' % exc
        actual = {k: fields.get(k) for k in item['expect']}
        missing = [k for k, v in actual.items() if v is None]
        if missing:
            return None, 'reply %r carries no %s field' % (raw, ', '.join(missing))
        return actual, None

    def _settings_dependency(self, item):
        """None when the item's ``requires`` holds (another owner's published, fresh, ok setting), else the reason."""
        requires = item.get('requires')
        if not requires:
            return None
        try:
            state = json.loads(Path(requires['state_file']).read_text())
        except (OSError, ValueError) as exc:
            return 'required owner state %s unreadable: %s' % (requires['state_file'], exc)
        if requires.get('instrument') not in (None, state.get('instrument')):
            return 'required owner state is for %r, not %r' % (state.get('instrument'), requires['instrument'])
        row = (state.get('instrument_settings') or {}).get(requires['setting']) or {}
        if not state.get('connected') or state.get('fault'):
            return '%s owner is not connected/healthy' % state.get('instrument')
        checked = row.get('last_checked_unix_s')
        if row.get('ok') is not True or checked is None:
            return '%s %s is not confirmed ok (ok=%r)' % (state.get('instrument'), requires['setting'], row.get('ok'))
        age = self.wall_clock() - float(checked)
        if age > float(requires.get('max_age_s', 180.)):
            return '%s %s confirmation is %.0f s old' % (state.get('instrument'), requires['setting'], age)
        return None

    def _settings_cycle(self, reason):
        """One check (and, in enforce mode, re-apply) of every configured setting. Caller holds ``self.lock`` and the
        session is connected. I/O errors fault the owner (raised as the latched BrokerError)."""
        if not self.settings_enabled():
            return
        now = self.wall_clock()
        replies = {}
        if self.settings_mode == 'probe':
            queries = list(dict.fromkeys(item['query'] for item in self.setting_items)) or ['ROSC?']
            self.settings_probe = dict(reason=reason, unix_s=now, replies=[dict(query=q, raw=self._settings_query(q, replies)) for q in queries])
            logging.info('instrument settings PROBE (read-only) %s: %s', self.instrument,
                         '; '.join('%s -> %r' % (r['query'], r['raw']) for r in self.settings_probe['replies']))
            self.audit('instrument_settings_probe', reason=reason, replies=self.settings_probe['replies'])
        for item in self.setting_items:
            row = self.settings[item['name']]
            previous = (row['ok'], row['actual'])
            raw = self._settings_query(item['query'], replies)
            actual, error = self._settings_parse(item, raw)
            row.update(raw=raw, actual=actual, last_checked_unix_s=now)
            if actual is None:
                # Unknown BEFORE any write: report and write nothing (no uncertain write has happened).
                changed = row['error'] != error
                row.update(ok=None, error=error)
                if changed:
                    self.audit('instrument_setting_unknown', name=item['name'], raw=raw, error=error, reason=reason)
                continue
            if actual == row['expected']:
                row.update(ok=True, error=None)
                continue
            row['mismatches'] += 1
            if previous != (False, actual):         # audit a new/changed mismatch once, not every cycle
                self.audit('instrument_setting_mismatch', name=item['name'], expected=row['expected'], actual=actual,
                           raw=raw, reason=reason, mode=self.settings_mode)
            if self.settings_mode == 'probe':
                row.update(ok=False, error='differs; probe mode (read-only): not applied')
                continue
            if reason == 'periodic' and not item.get('reapply', True):
                row.update(ok=False, error='differs; periodic re-apply disabled for this setting')
                continue
            blocked = self._settings_dependency(item)
            if blocked:
                if row['error'] != 'not applied: ' + blocked:
                    self.audit('instrument_setting_waiting', name=item['name'], reason=reason, blocked=blocked)
                row.update(ok=False, error='not applied: ' + blocked)
                continue
            # ONE command, *OPC?, a fresh readback. A transport error here is an I/O error of the session.
            try:
                self.adapter.execute('write', item['write'])
                opc = str(self.adapter.execute('query', '*OPC?')).strip()
            except Exception as exc:
                raise self._io_failed('instrument setting write %r failed: %s' % (item['write'], exc)) from exc
            replies.pop(item['query'], None)
            after_raw = self._settings_query(item['query'], replies)
            after, after_error = self._settings_parse(item, after_raw)
            row.update(raw=after_raw, actual=after, last_applied_unix_s=now, applies=row['applies'] + 1)
            if after is None:
                row.update(ok=None, error='readback unknown after write: ' + after_error)
                raise self._latch('Uncertain instrument setting write %r (%s): readback %r unknown; review before reconnect'
                                  % (item['write'], item['name'], after_raw))
            if after == row['expected']:
                row.update(ok=True, error=None)
                self.audit('instrument_setting_applied', name=item['name'], write=item['write'], before=actual,
                           after=after, raw=after_raw, opc=opc, reason=reason)
            else:
                # Known but still wrong: a status flag, NOT a fault; channel work continues; retried next cycle.
                row.update(ok=False, error='re-apply failed: readback %r after %r' % (after_raw, item['write']))
                self.audit('instrument_setting_apply_failed', name=item['name'], write=item['write'], before=actual,
                           after=after, raw=after_raw, opc=opc, reason=reason)

    def settings_status(self):
        rows = copy.deepcopy(self.settings)
        oks = [r['ok'] for r in rows.values()]
        summary = None if not rows else (False if False in oks else (True if all(o is True for o in oks) else None))
        return dict(instrument_settings=rows, instrument_settings_ok=summary,
                    instrument_settings_mode=self.settings_mode if self.settings_enabled() else None,
                    instrument_settings_interval_s=self.settings_interval_s if self.settings_enabled() else None,
                    instrument_settings_probe=copy.deepcopy(self.settings_probe))

    def check_instrument_settings(self, reason='periodic'):
        """One cycle under the owner lock (the periodic supervisor and the ``check_instrument_settings`` RPC)."""
        with self.lock:
            if self.closed or self.fault or not self.connected or not self.settings_enabled():
                return self.settings_status()
            try:
                self._settings_cycle(reason)
            except BrokerError:
                pass                 # latched (fault published by _latch)
            self.publish()
            return self.settings_status()

    def enforce_after_probe(self, actor=None):
        """Second phase of the first run WITHOUT a second session: an owner started with ``--settings-probe`` whose
        config says ``mode: enforce`` switches to enforce (audited) and runs one cycle now. Not persistent: a restart
        follows the command line again (the deploy removes ``--settings-probe`` from the unit afterwards)."""
        with self.lock:
            configured = (self.config.get('instrument_settings') or {}).get('mode', 'enforce')
            if not self.setting_items or configured != 'enforce' or self.settings_mode != 'probe':
                raise BrokerError('configuration', 'only an owner started in probe mode with configured enforce settings can switch (mode %r, configured %r)'
                                  % (self.settings_mode, configured if self.setting_items else None))
            if self.fault or not self.connected or self.closed:
                raise BrokerError('fault', self.fault or 'owner session is not connected')
            self.settings_mode = 'enforce'
            self.audit('instrument_settings_enforced', actor=str(actor or 'rpc')[:200], probe=self.settings_probe)
        return self.check_instrument_settings('enforce_after_probe')

    def start_settings_supervision(self):
        """Background check every ``check_interval_s`` (production ``main``; tests call the check directly)."""
        if not self.settings_enabled() or self.settings_thread is not None:
            return None

        def loop():
            while not self.settings_stop.wait(self.settings_interval_s):
                try:
                    self.check_instrument_settings('periodic')
                except Exception:
                    logging.exception('instrument settings check failed')
        self.settings_thread = threading.Thread(target=loop, name='instrument-settings', daemon=True)
        self.settings_thread.start()
        return self.settings_thread

    def _safe_start_check(self):
        expect = (self.config.get('safe_start') or {}).get('expect') or {}
        if not expect:
            return None
        reasons = []
        for ch, wanted in expect.items():
            actual = self.readback.get(ch) or {}
            for key, value in wanted.items():
                if key == 'tolerance_v':
                    continue
                if key == 'value_range':
                    lo, hi = float(value[0]), float(value[1])
                    v = actual.get('value')
                    if not isinstance(v, (int, float)) or not lo <= v <= hi:
                        reasons.append('CH%s value %r outside the safe-start range [%g, %g]' % (ch, v, lo, hi))
                    continue
                if key == 'value':
                    tolerance = float(wanted.get('tolerance_v', 0.))
                    if not isinstance(actual.get('value'), (int, float)) or abs(actual['value'] - float(value)) > tolerance:
                        reasons.append('CH%s value %r differs from %r (+-%g)' % (ch, actual.get('value'), value, tolerance))
                elif actual.get(key) != value:
                    reasons.append('CH%s %s %r differs from %r' % (ch, key, actual.get(key), value))
        return dict(ok=not reasons, reasons=reasons, expect=expect)

    def _io_failed(self, exc):
        # The session state is unknown for every lease: fault everything, retry nothing, keep the session object
        # (it is not reopened; a reconnect is an explicit, read-only RPC).
        self.connected = False
        return self._latch('I/O failed; all leases faulted, review before reconnect: %s' % exc)

    # ---- RPC -----------------------------------------------------------------------------------
    def status(self):
        with self.lock:
            return dict(schema_version=1, instrument=self.instrument, kind=self.config['kind'], unit=self.unit,
                        endpoint=self.endpoint, address=self.address, host=self.config['host'], pid=os.getpid(),
                        identity=self.identity, expected_identity=self.config.get('expected_identity'),
                        connected=self.connected and not self.closed, fault=self.fault, closed=self.closed,
                        channels={ch: dict(writes_enabled=p['writes_enabled'], role=p.get('role'))
                                  for ch, p in self.config['channels'].items()},
                        leases=[dict(lease=t, **{k: v for k, v in l.items()}) for t, l in self.leases.items()],
                        readback=copy.deepcopy(self.readback), startup_check=self.startup_check,
                        definition=self.definition, updated_unix_s=self.wall_clock(), **self.settings_status())

    def describe(self):
        return dict(instrument=self.instrument, kind=self.config['kind'], profile=self.config.get('profile'),
                    resource=self.config.get('resource'), host=self.config['host'], address=self.address,
                    identity=self.identity, expected_identity=self.config.get('expected_identity'),
                    channels=copy.deepcopy(self.config['channels']), endpoint=self.endpoint, fault=self.fault,
                    connected=self.connected, leases=[dict(channel=l['channel'], owner=l['owner']) for l in self.leases.values()],
                    definition=self.definition,
                    instrument_settings=dict(mode=self.settings_mode if self.settings_enabled() else None,
                                             check_interval_s=self.settings_interval_s,
                                             items=copy.deepcopy(self.setting_items)))

    def dispatch(self, request):
        action = request.get('action')
        if request.get('instrument') not in (None, self.instrument):
            raise BrokerError('configuration', 'request names another instrument (%r)' % request.get('instrument'))
        if action == 'status':
            return self.status()
        if action == 'instrument_settings':
            with self.lock:
                return self.settings_status()                 # read-only, no instrument I/O
        if action == 'check_instrument_settings':
            if self.fault:
                raise BrokerError('fault', self.fault)
            return self.check_instrument_settings('rpc')
        if action == 'enforce_instrument_settings':
            return self.enforce_after_probe(request.get('actor'))
        with self.lock:
            if action == 'describe':
                return self.describe()
            if action == 'reconnect':
                return self._reconnect()
            if self.closed:
                raise BrokerError('fault', 'owner is closed')
            if action == 'claim':
                return self._claim(request)
            if action == 'release':
                if self.fault or self.projections.guard.inspect()['fault']:
                    raise BrokerError('fault', 'Uncertain physical effects retain every lease for explicit offline reconciliation')
                released = self.leases.pop(request.get('lease'), None)
                if released is not None:
                    self._persist()
                    self.audit('release', channel=released['channel'], owner=released['owner'])
                    self.publish()
                return released is not None     # idempotent
            lease = self.leases.get(request.get('lease'))
            if lease is None:
                raise BrokerError('ownership', 'no matching channel lease')
            if action in ('projection_status', 'projection_install'):
                if action == 'projection_install' and (self.fault or not self.connected):
                    raise BrokerError('fault', 'A faulted or disconnected owner cannot admit new projections or cleanup recipes')
                try:
                    method = self.projections.status if action == 'projection_status' else self.projections.install
                    return method(request, lease)
                except (ValueError, RuntimeError) as exc:
                    raise BrokerError('projection', str(exc)) from exc
            cleanup = action == 'transaction' and request.get('cleanup') is True
            if self.fault and not cleanup:
                raise BrokerError('fault', self.fault)
            if not self.connected and not cleanup:
                raise BrokerError('fault', 'owner session is not connected')
            if action == 'channel_environment':
                return self._environment(lease)
            if action == 'transaction':
                return self._transaction(lease, request)
            raise BrokerError('action', 'unsupported request')

    def _claim(self, request):
        if self.fault:
            raise BrokerError('fault', self.fault)
        if not self.connected:
            raise BrokerError('fault', 'owner session is not connected; no lease is served')
        channel = request.get('channel')
        if type(channel) is not int or str(channel) not in self.config['channels']:
            raise BrokerError('channel', 'channel is not served by this owner')
        policy = self.config['channels'][str(channel)]
        for key in ('resource', 'profile'):
            if request.get(key) is not None and self.config.get(key) is not None and request[key] != self.config[key]:
                raise BrokerError('configuration', 'client %s does not match the owner (%r != %r)' % (key, request[key], self.config[key]))
        if any(l['channel'] == channel for l in self.leases.values()):
            raise BrokerError('busy', '%s CH%d is already leased' % (self.instrument, channel))
        observe = request.get('observe') or []
        if (not isinstance(observe, list) or any(type(c) is not int or str(c) not in self.config['channels'] or c == channel for c in observe)):
            raise BrokerError('channel', 'observe must list other configured channels')
        token = uuid.uuid4().hex
        self.leases[token] = dict(channel=channel, observe=sorted(observe), owner=str(request.get('owner', 'client'))[:200],
                                  environment_verified=not policy.get('require_channel_environment', False),
                                  claimed_unix_s=self.wall_clock())
        try:
            self._persist()
        except OSError as exc:
            self.leases.pop(token, None)
            raise self._latch('Lease persistence failed: ' + str(exc)) from exc
        self.audit('claim', channel=channel, observe=observe, owner=self.leases[token]['owner'])
        self.publish()
        return dict(lease=token, simulated=False, instrument=self.instrument, identity=self.identity,
                    writes_enabled=policy['writes_enabled'])

    def _environment(self, lease):
        try:
            result = self.adapter.environment()
        except Exception as exc:
            raise self._io_failed('environment read failed: %s' % exc) from exc
        reasons = list(result['reasons'])
        expected = self.config.get('expected_identity')
        if expected is not None and result['identity'] != str(expected).strip():
            reasons.append('identity differs from the owner configuration')
        ok = not reasons
        lease['environment_verified'] = ok
        self._persist()
        self.publish()
        return dict(result, ok=ok, reasons=reasons, channel=lease['channel'], resource=self.config.get('resource'),
                    instrument=self.instrument)

    def _transaction(self, lease, request):
        operations = request.get('operations')
        if not isinstance(operations, list) or not 1 <= len(operations) <= 64:
            raise BrokerError('operations', 'transaction needs 1..64 operations')
        parsed = [self.adapter.validate(op, lease) for op in operations]
        policy = self.config['channels'][str(lease['channel'])]
        writes = [op for op, _ in parsed if op not in ('query', 'get')]
        if writes and not policy['writes_enabled']:
            raise BrokerError('range', 'writes are not enabled on %s CH%d' % (self.instrument, lease['channel']))
        if writes and policy.get('require_channel_environment') and not lease.get('environment_verified'):
            raise BrokerError('ownership', 'verify channel_environment (identity, coupling/tracking OFF) on this lease before any write')
        def writer():
            results = []
            try:
                for op, data in parsed:
                    results.append(self.adapter.execute(op, data))
            except Exception as exc:
                raise self._io_failed(exc) from exc
            return results
        if writes:
            try:
                results = self.projections.execute(lease, request, writer)
            except BrokerError:
                raise
            except (ValueError, RuntimeError) as exc:
                raise BrokerError('projection', str(exc)) from exc
        else:
            results = writer()
        if writes:
            self.audit('transaction', channel=lease['channel'], owner=lease['owner'], operations=[op for op, _ in parsed])
        return results

    def _reconnect(self):
        """Explicit, read-only re-establishment of the one session (a human decision, never automatic)."""
        if self.closed:
            raise BrokerError('fault', 'owner is closed')
        if self.leases:
            raise BrokerError('busy', 'release or review every lease before reconnecting')
        try:
            self.adapter.close()
        except Exception:
            pass
        self.connected = False
        self.fault = None
        self._persist()
        self._try_connect()
        self.publish()
        if self.fault:
            raise BrokerError('fault', self.fault)
        return self.status()

    def close(self):
        self.settings_stop.set()
        with self.lock:
            self.closed = True
            try:
                self.adapter.close()     # closing the session never changes outputs
            except Exception:
                pass
            try:
                if self.published_file.parent.exists():
                    self.publish()
            except Exception:
                pass
            for handle in reversed(self.locks):
                try:
                    handle.close()
                except Exception:
                    pass
            self.locks = []


def main():
    from ldcon.owners.instrument_service import make_server
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--validate-only', action='store_true', help='Validate the configuration without locks, DNS, network or hardware')
    parser.add_argument('--settings-probe', action='store_true',
                        help='FIRST RUN: instrument settings are READ-ONLY (only their queries, default ROSC?, on the '
                             "owner's own session at connect and every check); raw replies are logged, audited and "
                             'published as instrument_settings_probe; nothing is written')
    args = parser.parse_args()
    config = validate_config(json.loads(args.config.read_text()))
    if args.validate_only:
        block = config.get('instrument_settings') or {}
        print(json.dumps(dict(valid=True, instrument=config['instrument'], kind=config['kind'], endpoint='http://%s:%d' % (config['bind'], config['port']),
                              definition=definition_digest(config),
                              instrument_settings=dict(mode='probe' if args.settings_probe else block.get('mode', 'enforce') if block else None,
                                                       items=[(i['name'], i['write'], i['query'], i['expect']) for i in block.get('items', [])]))))
        return
    logging.basicConfig(level=logging.INFO)
    from ldcon.owners.instrument_identity import unit_name
    owner = InstrumentOwner(config, unit=unit_name(config['instrument']), settings_probe=args.settings_probe)
    if owner.settings_probe:
        print(json.dumps(dict(instrument_settings_probe=owner.settings_probe, instrument=owner.instrument)), flush=True)
    owner.start_settings_supervision()
    try:
        server = make_server(owner, config['bind'], config['port'], token=os.environ.get('LDCON_INSTRUMENT_TOKEN'))
        logging.info('ldcon instrument owner %s at %s; connected=%s fault=%s', owner.instrument, owner.endpoint, owner.connected, owner.fault)
        import signal
        signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
    finally:
        owner.close()


if __name__ == '__main__':
    main()
