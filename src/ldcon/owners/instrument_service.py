"""Linux AWG owner process. Does not open any instrument until the first I/O.

python -m ldcon.owners.instrument_service --config examples/instruments/repump1092-linux0.json
Use --simulate --bind 127.0.0.1 for tests. Real mode is opt-in by running this CLI.
"""
import argparse
import base64
import binascii
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import re
import threading
import uuid

from ldcon.drivers.siglent import DummyTransport, VisaTransport, fields
from ldcon.drivers.cld1015 import Controller
from ldcon.drivers.cld1015_config import configuration
from ldcon.core.instrument_client import BrokerError
from ldcon.core.device_conditions import ConditionError

MAX_REQUEST_BYTES = 64 * 1024 * 1024
_COMMAND = re.compile(r"C([12]):(BSWV|ARWV|BTWV|OUTP|MDWV|SWWV|SRATE|WVDT)(.*)", re.ASCII)


class SimulatedInstrument:
    def __init__(self, config):
        self.channels = {ch: DummyTransport(dict(channel=ch, profile=config["profile"],
                         min_v=-1, max_v=1, load="HZ")) for ch in config["channels"]}
        self.closed = False

    def _channel(self, command):
        return self.channels[int(command[1:2])]

    def query(self, command):
        if command == "COUP?":
            return "COUP TRACE,OFF,FCOUP,OFF,PCOUP,OFF,ACOUP,OFF"
        if command in ("*IDN?", "*OPC?"):
            return next(iter(self.channels.values())).query(command)
        return self._channel(command).query(command)

    def write(self, command):
        self._channel(command).write(command)

    def write_raw(self, data):
        self._channel(data[:3].decode()).write_raw(data)

    def close(self):
        self.closed = True
        for io in self.channels.values():
            io.close()


class RigRoutedTransport:
    """Broker transport for an AWG channel whose physical instrument is also used by an ECDL rig in this process:
    every transaction runs inside ECDLOwner.shared_awg_session on the rig's one persistent session (rig lock held)."""
    def __init__(self, owner, awg, connection=None):
        self.owner, self.awg, self.connection = owner, awg, connection

    def run(self, operations):
        return self.owner.shared_awg_session(self.awg, list(operations), connection=self.connection)

    def query(self, command):
        return self.run([("query", command)])[0]

    def write(self, command):
        self.run([("write", command)])

    def write_raw(self, data):
        self.run([("write_raw", data)])

    def close(self):
        pass


class OwnerRelay:
    """Rule G compatibility: a broker entry whose physical instrument is served by a per-instrument owner.

    The lock worker of a running sr1092 job carries the frozen wiring ``endpoint :47130 / instrument
    repump_1092 / resource TCPIP::192.168.1.37::INSTR``; this relay keeps that contract alive without any
    session of its own: claim/channel_environment/transaction/release are forwarded to the owner, the owner's
    lease token is the broker's lease token, and the owner's fault state is authoritative (the broker does not
    latch a fault of its own for a relayed entry, so no instruments-service restart is needed to clear one)."""
    def __init__(self, config, registry):
        from ldcon.owners.instrument_identity import canonical
        relay = config['relay_to_owner']
        if not isinstance(relay, dict) or 'instrument' not in relay:
            raise ValueError('relay_to_owner must be {instrument, endpoint?}')
        self.instrument = canonical(relay['instrument'])
        self.static_endpoint = relay.get('endpoint')
        self.registry = registry
        self.link = None

    def _link(self):
        if self.link is None:
            from ldcon.owners.remote_instruments import OwnerLink
            endpoint = self.registry.resolve(self.instrument, static_endpoint=self.static_endpoint)
            self.link = OwnerLink(endpoint, self.instrument)
        return self.link

    def forward(self, action, **values):
        try:
            return self._link().rpc(action, **values)
        except BrokerError:
            raise
        except Exception as exc:
            self.link = None       # the next call resolves the endpoint again (the owner may have restarted)
            raise BrokerError('io', 'owner of %s unreachable: %s' % (self.instrument, exc)) from exc


class Instrument:
    def __init__(self, config):
        self.config = dict(config)
        self.lock = threading.RLock()
        self.leases = {}
        self.io = None
        self.fault = None
        self.relay = None
        self.shared_connection = None  # Host object; never selected by an RPC field.
        self.system_controller = None  # Trusted local CLD system delegate; never populated by RPC.


class Hub:
    def __init__(self, instruments, *, simulate=False, factory=None, ecdl_rigs=None, sensors=None, retained_instrument_faults=None,
                 registry=None):
        from ldcon.drivers.staircase import PROFILES
        from ldcon.owners.device_registry import RegistryClient
        self.instruments = {}
        resources = set()
        # Rule G: how this process finds per-instrument owners (registry -> published state -> static endpoint).
        self.registry = registry if isinstance(registry, RegistryClient) else RegistryClient(
            (registry or {}).get('endpoint'), state_dir=(registry or {}).get('state_dir'))
        for name, config in instruments.items():
            if not name.isidentifier() or config["profile"] not in (*PROFILES, "cld1015"):
                raise ValueError("invalid instrument name/profile")
            config=dict(config)
            # Rule M step 3 (2026-10-27): the Hub no longer reads a laser yaml named by ``temperature_conditions_file``
            # (the indirect copy of the sr1092 CLD conditions). CLD operating conditions reach the CLD owner only as the
            # laser system owner's projection; a stale ``temperature_conditions_file`` key is ignored here.
            channels = config["channels"]
            allowed = (0,) if config["profile"] == "cld1015" else (1, 2)
            if not channels or any(ch not in allowed for ch in channels) or len(set(channels)) != len(channels):
                raise ValueError("instrument channels must be unique members of [1, 2]")
            resource = config["resource"].upper()
            if resource in resources:
                raise ValueError("each physical resource must have exactly one broker entry")
            resources.add(resource)
            self.instruments[name] = Instrument(config)
            if config.get('relay_to_owner') is not None:
                if config['profile'] == 'cld1015' or config.get('route_through_rig') or config.get('shared_with_rig'):
                    raise ValueError('relay_to_owner is for AWG entries and excludes route_through_rig/shared_with_rig')
                self.instruments[name].relay = OwnerRelay(config, self.registry)
        retained = retained_instrument_faults or {}
        if not isinstance(retained, dict):raise ValueError('Retained faults must be a mapping')
        for name, record in retained.items():
            if (name not in self.instruments or not isinstance(record,dict)
                    or set(record)!={'resource','fault'}
                    or record['resource']!=self.instruments[name].config['resource']
                    or not isinstance(record['fault'],str) or not record['fault']):
                raise ValueError('Retained fault identity mismatch')
        for name, record in retained.items():self.instruments[name].fault=record['fault']
        self.factory = factory or (SimulatedInstrument if simulate else self._visa)
        self.simulate = simulate
        self.ecdl_rigs={}
        self.sensor_definitions={};self.sensor_ports={};self.sensor_lock=threading.RLock()
        for name,entry in (sensors or {}).items():
            from ldcon.core.interface_schema import load_mapping,validate_laser
            laser=validate_laser(load_mapping(Path(entry['laser_file']).read_text()))
            device=laser['devices'][entry['device']]
            if device['type']!='fizeau.owner':raise ValueError('Optical owner requires a fizeau.owner definition')
            self.sensor_definitions[name]=device
        for name,entry in (ecdl_rigs or {}).items():
            if simulate:raise ValueError('Real ECDL rig definitions cannot be loaded in AWG simulation mode')
            from ldcon.owners.ecdl_owner import ECDLOwner
            from ldcon.core.interface_schema import load_mapping
            laser=load_mapping(Path(entry['laser_file']).read_text())
            if name!=laser['id']:raise ValueError('ECDL rig identity mismatch')
            self.ecdl_rigs[name]=ECDLOwner(laser,audit_path=entry['audit_file'],laser_file=entry['laser_file'],
                                           single_session_awgs=entry.get('single_session_awgs',()),
                                           instrument_owners=entry.get('instrument_owners'),registry=self.registry)
        self.shared_lock = threading.Lock()
        self.link_shared()

    def link_shared(self):
        """Shared physical AWG (2026-10-22): an instrument entry with ``shared_with_rig: <rig>`` (the sr1092
        modulation channel CH2 of the SDG whose CH1 is the sr422 TEC) and that rig's locked sweep exclude each
        other inside this one process: a claim is refused while the rig's sweep is in progress, and the rig's
        locked_sweep_prepare is refused while such an instrument holds a lease. One lock orders both checks."""
        for name, instrument in self.instruments.items():
            shared = instrument.config.get('shared_with_rig')
            if shared is not None and shared not in self.ecdl_rigs:
                raise ValueError('shared_with_rig names an unknown ECDL rig: %s' % shared)
            if shared is not None and not instrument.config.get('require_channel_environment'):
                raise ValueError('A shared AWG entry must set require_channel_environment: true')
        for name, instrument in self.instruments.items():
            route = instrument.config.get('route_through_rig')
            if route is not None:
                if (not isinstance(route, dict) or set(route) != {'rig', 'awg'} or route['rig'] != instrument.config.get('shared_with_rig')
                        or route['rig'] not in self.ecdl_rigs
                        or route['awg'] not in getattr(self.ecdl_rigs[route['rig']], 'single_session_awgs', ())):
                    raise ValueError('route_through_rig must be {rig, awg} for the shared_with_rig rig, and that rig must hold the AWG in single_session_awgs')
        for rig_name, owner in self.ecdl_rigs.items():
            def leases(rig=rig_name):
                return {n: [dict(l) for l in i.leases.values()] for n, i in self.instruments.items()
                        if i.config.get('shared_with_rig') == rig and i.leases}
            owner.shared_guard = (self.shared_lock, leases)

    def bind_shared_awg(self, name, authority, constraints, channels):
        """Host composition only: bind a sibling system to its exact channels.

        The ECDL physical connection verifies a real SystemPolicyAuthority and
        owns the one shared guard/session. No wire request can create a binding.
        """
        instrument = self.instruments[name]
        route = instrument.config.get('route_through_rig')
        if route is None or set(channels) != set(instrument.config['channels']):
            raise ValueError('A shared binding must name exactly the configured routed channels')
        with instrument.lock:
            if instrument.leases:
                raise ValueError('Bind shared channels before admitting broker leases')
            connection = self.ecdl_rigs[route['rig']].bind_shared_awg(
                route['awg'], authority, constraints, channels)
            instrument.shared_connection = connection
            if isinstance(instrument.io, RigRoutedTransport):
                instrument.io.connection = connection
            return connection

    def _open(self, instrument):
        route = instrument.config.get('route_through_rig')
        if route is not None:
            return RigRoutedTransport(self.ecdl_rigs[route['rig']], route['awg'], instrument.shared_connection)
        return self.factory(instrument.config)

    def _rig_sweep_busy(self, rig):
        owner = self.ecdl_rigs.get(rig)
        sweep = getattr(getattr(owner, 'steady', None), 'sweep', None)
        if sweep is None:
            return None
        from ldcon.owners.ecdl_locked_sweep import FINISHED
        return None if sweep.state in FINISHED else sweep.state

    @staticmethod
    def _visa(config):
        return VisaTransport(config["resource"], config.get("timeout_s", 10),
                             backend=config.get("visa_backend", "@py"))

    def dispatch(self, request):
        action = request["action"]
        if action=='sensor_read':
            from dataclasses import asdict
            from ldcon.core.interface_schema import digest
            from ldcon.drivers.fizeau import HttpFizeauWavemeter
            device=self.sensor_definitions.get(request.get('instrument'))
            if device is None or request.get('revision')!=digest(device):raise BrokerError('configuration','Optical sensor definition differs from owner')
            name=request['instrument']
            with self.sensor_lock:
                if name not in self.sensor_ports:
                    c=device['connection']['source']
                    self.sensor_ports[name]=HttpFizeauWavemeter(c['endpoint'],channel=c['channel'],cycle=c['cycle'],
                        trust_source_clock=c['trust_source_clock'],expected_laser=c['expected_laser'])
                return asdict(self.sensor_ports[name].read_frequency())
        if action=='ecdl':
            owner=self.ecdl_rigs.get(request.get('instrument'))
            if owner is None:raise BrokerError('configuration','Unknown ECDL rig')
            return owner.dispatch(request['request'])
        if action == "status":
            return self.status()
        name = request["instrument"]
        if name not in self.instruments:
            raise BrokerError("unknown_instrument", str(name))
        instrument = self.instruments[name]
        with instrument.lock:
            if instrument.relay is not None:
                return self._relayed(instrument, request)
            if action == "claim":
                ch = request["channel"]
                config = instrument.config
                if ch not in config["channels"]:
                    raise BrokerError("channel", "channel is not served by this broker")
                if request["resource"] != config["resource"] or request["profile"] != config["profile"]:
                    raise BrokerError("configuration", "client resource/profile does not match broker configuration")
                if any(lease["channel"] == ch for lease in instrument.leases.values()):
                    raise BrokerError("busy", f"{name} CH{ch} is already leased")
                shared = config.get("shared_with_rig")
                token = uuid.uuid4().hex
                if shared is not None:
                    with self.shared_lock:
                        busy = self._rig_sweep_busy(shared)
                        if busy is not None:
                            raise BrokerError("busy", f"{name} shares its AWG with rig {shared}, whose locked sweep is in progress ({busy})")
                        instrument.leases[token] = dict(channel=ch, owner=str(request.get("owner", "client"))[:200], environment_verified=False)
                else:
                    instrument.leases[token] = dict(channel=ch, owner=str(request.get("owner", "client"))[:200])
                return dict(lease=token, simulated=self.simulate)
            if action == "release":
                # Idempotent release permits a client to retry after a lost reply.
                released=instrument.leases.pop(request['lease'],None)
                if released and released['channel']==0 and isinstance(instrument.io,Controller):instrument.io.current_activation=None
                return released is not None
            if action == "controller":
                lease = instrument.leases.get(request["lease"])
                if lease is None or lease["channel"] != 0 or instrument.config["profile"] != "cld1015":
                    raise BrokerError("ownership", "controller requires its exclusive device lease")
                if instrument.fault:
                    raise BrokerError("fault", instrument.fault)
                method = request["method"]
                if method not in ("read", "validate", "apply", "enable", "validate_sweep", "describe", "activate_current", "save_current_tuning"):
                    raise BrokerError("method", "unsupported controller operation")
                if instrument.system_controller is not None:
                    from experiment_core.errors import AuthorizationRequired, StaleContext, Conflict
                    try:
                        return instrument.system_controller.dispatch(request)
                    except BrokerError:
                        raise
                    except (ValueError, ConditionError) as exc:
                        raise BrokerError('range', str(exc)) from exc
                    except (AuthorizationRequired, StaleContext, Conflict) as exc:
                        raise BrokerError('authorization', str(exc)) from exc
                if method in ('apply', 'enable', 'activate_current', 'save_current_tuning'):
                    raise BrokerError('authorization', 'CLD mutations require a configured laser system owner')
                if method=='describe':
                    from ldcon.core.device_conditions import policy_digest
                    policy=instrument.config.get('temperature_conditions')
                    return {'temperature_conditions':policy,'policy_digest':policy_digest(policy),
                            'temperature_limits_c':instrument.config.get('temperature_limits_c'),
                            'temperature_writes_enabled':False,
                            'current_conditions':instrument.config.get('current_conditions'),
                            'control_settings':instrument.config.get('control_settings',{}),
                            'current_writes_enabled':False}
                if method in ('apply','enable') and not instrument.config.get('temperature_writes_enabled',True):
                    raise BrokerError('range','Laser output writes are not enabled')
                try:
                    if instrument.io is None:
                        cfg = instrument.config
                        policy = configuration(resource=cfg["resource"], mock=self.simulate,
                            current_limits_a=cfg.get("current_limits_a"),
                            temperature_limits_c=cfg.get("temperature_limits_c"),
                            modulation_role=cfg.get("modulation_role"), timeout_s=cfg.get("timeout_s", 5),
                            temperature_conditions=cfg.get("temperature_conditions"),
                            temperature_writes_enabled=cfg.get("temperature_writes_enabled",True),
                            current_conditions=cfg.get('current_conditions'),
                            control_settings=cfg.get('control_settings',{}),
                            current_writes_enabled=cfg.get('current_writes_enabled',False))
                        controller = Controller(policy)
                        instrument.io = controller
                    controller = instrument.io
                    if controller.io is None:
                        controller.connect()
                    if method=='activate_current':
                        from ldcon.owners.current_owner import activate
                        return activate(controller,request['policy_digest'],request['settings_digest'])
                    if method=='save_current_tuning':
                        from ldcon.owners.current_owner import save_tuning
                        return save_tuning(instrument,request['expected_digest'],request['proposal'])
                    if method == "read": return controller.read()
                    if method == "validate": return controller.validate(request["values"])
                    if method == "apply": return controller.apply(request["values"], ramp_s=request.get("ramp_s", 0))
                    if method == "enable": return controller.enable(request["output"], request["enabled"])
                    return controller.validate_sweep(request["channels"], {}, request["resolved"])
                except (ValueError,ConditionError) as exc:
                    raise BrokerError("range", str(exc)) from exc
                except Exception as exc:
                    instrument.fault = str(exc)
                    raise BrokerError("io", str(exc)) from exc
            if action == "channel_environment":
                return self._channel_environment(instrument, request)
            if action != "transaction":
                raise BrokerError("action", "unsupported request")
            lease = instrument.leases.get(request["lease"])
            if lease is None:
                raise BrokerError("ownership", "no matching channel lease")
            if instrument.fault:
                raise BrokerError("fault", instrument.fault)
            if instrument.config["profile"] == "cld1015":
                raise BrokerError("operation", "use typed controller operations")
            operations = request["operations"]
            if not isinstance(operations, list) or not 1 <= len(operations) <= 64:
                raise BrokerError("operations", "transaction needs 1..64 operations")
            parsed = [self._validate(op, lease["channel"]) for op in operations]
            if any(op != 'query' for op, _ in parsed):
                route = instrument.config.get('route_through_rig')
                if route is None or instrument.shared_connection is None:
                    raise BrokerError('authorization', 'Legacy AWG broker is read-only without a trusted system projection; use the physical instrument owner')
            if (instrument.config.get("require_channel_environment") and not lease.get("environment_verified")
                    and any(op != "query" for op, _ in parsed)):
                # Mandatory, no attestation route: the other channel of this physical AWG is safety-critical.
                raise BrokerError("ownership", "verify channel_environment (identity, coupling/tracking OFF) on this lease before any write")
            try:
                if instrument.io is None:
                    instrument.io = self._open(instrument)
                if isinstance(instrument.io, RigRoutedTransport):
                    return instrument.io.run(parsed)            # one shared session, all operations, rig lock held
                results = []
                for op, data in parsed:
                    result = getattr(instrument.io, op)(data)
                    results.append(str(result).strip() if op == "query" else None)
                return results
            except Exception as exc:
                # A timed-out query can leave a stale reply in the VISA stream.
                # Never hand that stream to another channel or silently reconnect.
                instrument.fault = f"I/O failed; restore/review instrument before restarting broker: {exc}"
                raise BrokerError("io", instrument.fault) from exc

    def _relayed(self, instrument, request):
        """Forward the four lease actions of a relayed entry to its owner; same request/reply shapes."""
        action = request["action"]
        config = instrument.config
        relay = instrument.relay
        if action == "claim":
            ch = request["channel"]
            if ch not in config["channels"]:
                raise BrokerError("channel", "channel is not served by this broker")
            if request["resource"] != config["resource"] or request["profile"] != config["profile"]:
                raise BrokerError("configuration", "client resource/profile does not match broker configuration")
            result = relay.forward("claim", channel=ch, owner="relay:" + str(request.get("owner", "client"))[:150],
                                   resource=config.get("owner_resource", config["resource"]), profile=config.get("owner_profile", config["profile"]))
            instrument.leases[result["lease"]] = dict(channel=ch, owner=str(request.get("owner", "client"))[:200],
                                                      relayed_to=relay.instrument, environment_verified=False)
            return dict(lease=result["lease"], simulated=False, relayed_to=relay.instrument)
        if action == "release":
            # Keep the compatibility lease when the owner refuses release or
            # its response is unknown; no frontend state can clear that hold.
            ok = relay.forward("release", lease=request["lease"])
            released = instrument.leases.pop(request["lease"], None)
            return bool(released) or bool(ok)
        lease = instrument.leases.get(request.get("lease"))
        if lease is None:
            raise BrokerError("ownership", "no matching channel lease")
        if action == "channel_environment":
            result = relay.forward("channel_environment", lease=request["lease"])
            lease["environment_verified"] = bool(result.get("ok"))
            return result
        projection = {key: request[key] for key in ('principal', 'receipt', 'expected_revision', 'cleanup') if key in request}
        if action in ('projection_status', 'projection_install'):
            return relay.forward(action, lease=request['lease'], **projection)
        if action == "transaction":
            operations = request["operations"]
            if not isinstance(operations, list) or not 1 <= len(operations) <= 64:
                raise BrokerError("operations", "transaction needs 1..64 operations")
            for op in operations:              # the owner validates addressing; refuse malformed shapes early
                if not isinstance(op, dict) or op.get("op") not in ("query", "write", "write_raw"):
                    raise BrokerError("operation", "only query/write/write_raw are supported")
            return relay.forward("transaction", lease=request["lease"], operations=operations, **projection)
        raise BrokerError("action", "relayed entries support claim/release/channel_environment/projection_status/projection_install/transaction")

    COUPLING_KEYS = ("TRACE", "FCOUP", "PCOUP", "ACOUP")

    def _channel_environment(self, instrument, request):
        """Read-only *IDN? and COUP? for a leased AWG channel. Verifies every coupling/tracking field is OFF and,
        with ``expected_identity`` in the broker entry, the exact identity. Marks the lease verified for writes."""
        lease = instrument.leases.get(request.get("lease"))
        if lease is None or lease["channel"] not in (1, 2) or instrument.config["profile"] == "cld1015":
            raise BrokerError("ownership", "AWG environment requires an AWG channel lease")
        if instrument.fault:
            raise BrokerError("fault", instrument.fault)
        try:
            if instrument.io is None:
                instrument.io = self._open(instrument)
            if isinstance(instrument.io, RigRoutedTransport):
                identity, coupling = [str(v).strip() for v in instrument.io.run([("query", "*IDN?"), ("query", "COUP?")])]
            else:
                identity = str(instrument.io.query("*IDN?")).strip()
                coupling = str(instrument.io.query("COUP?")).strip()
        except Exception as exc:
            instrument.fault = "AWG environment read failed; review required: " + str(exc)
            raise BrokerError("io", instrument.fault) from exc
        text = coupling[5:] if coupling.upper().startswith("COUP ") else coupling
        values = fields(text)
        reasons = []
        expected = instrument.config.get("expected_identity")
        if expected is not None and identity != str(expected).strip():
            reasons.append("identity differs from the broker entry")
        missing = [k for k in self.COUPLING_KEYS if k not in values]
        if missing:
            reasons.append("coupling reply lacks " + ", ".join(missing))
        on = [k for k, v in values.items() if v.upper() == "ON"]
        if any(values.get(k, "OFF").upper() != "OFF" for k in self.COUPLING_KEYS) or on:
            reasons.append("coupling/tracking is not OFF: " + ", ".join(sorted(set(on))))
        ok = not reasons
        lease["environment_verified"] = ok
        return dict(identity=identity, coupling=coupling, coupling_fields=values, ok=ok, reasons=reasons,
                    channel=lease["channel"], resource=instrument.config["resource"])

    @staticmethod
    def _validate(operation, channel):
        op, data = operation["op"], operation["data"]
        if op not in ("query", "write", "write_raw"):
            raise BrokerError("operation", "only query/write/write_raw are supported")
        payload = None
        if op == "write_raw":
            try:
                payload = base64.b64decode(data, validate=True)
                header, body = payload.split(b"WAVEDATA,", 1)
                command = header.decode("ascii") + "WAVEDATA,"
            except (ValueError, UnicodeError, binascii.Error) as exc:
                raise BrokerError("binary", "invalid base64 WVDT upload") from exc
        else:
            command = data
        if not isinstance(command, str) or not command.isascii() or any(ord(c) < 32 for c in command) or ";" in command:
            raise BrokerError("command", "SCPI must be one ASCII command without separators")
        if op == "query" and command in ("*IDN?", "*OPC?"):
            return op, command
        match = _COMMAND.fullmatch(command)
        if match is None or int(match[1]) != channel:
            raise BrokerError("ownership", "command is not addressed to the leased channel")
        key, suffix = match[2], match[3]
        if op == "query":
            if suffix != "?" or key == "WVDT":
                raise BrokerError("command", "unsupported query")
        elif not suffix.startswith(" ") or "?" in suffix:
            raise BrokerError("command", "invalid channel write")
        if op == "write_raw":
            if key != "WVDT":
                raise BrokerError("binary", "binary operation must be WVDT")
            try:
                if int(fields(command)["LENGTH"]) != len(body):
                    raise ValueError("length mismatch")
            except (KeyError, ValueError) as exc:
                raise BrokerError("binary", "WVDT LENGTH does not match payload") from exc
        elif key == "WVDT":
            raise BrokerError("binary", "WVDT requires write_raw")
        return op, payload if payload is not None else command

    def status(self):
        result = {}
        for name, instrument in self.instruments.items():
            with instrument.lock:
                result[name] = dict(connected=(instrument.io.io is not None if isinstance(instrument.io, Controller) else instrument.io is not None), fault=instrument.fault,
                                    leases=list(instrument.leases.values()),
                                    write_authority=('typed_controller' if instrument.config['profile'] == 'cld1015'
                                        else 'physical_owner_revalidation' if instrument.relay is not None
                                        else 'trusted_shared_projection' if instrument.shared_connection is not None
                                        else 'read_only_unprojected'))
                if instrument.relay is not None:
                    result[name].update(relayed_to=instrument.relay.instrument, connected=instrument.relay.link is not None)
        return dict(simulated=self.simulate, instruments=result,
                    ecdl_rigs={name:owner.dispatch({'action':'status'}) for name,owner in self.ecdl_rigs.items()})

    def close(self, hold=True):
        for owner in self.ecdl_rigs.values():
            try:owner.close(hold=hold)
            except Exception:logging.exception('rig close failed; continuing to release the other leases')
        for instrument in self.instruments.values():
            with instrument.lock:
                if instrument.io is not None:
                    instrument.io.close()


def make_server(hub, bind="127.0.0.1", port=0, token=None):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            try:
                if self.path != "/rpc":
                    raise BrokerError("path", "use /rpc")
                if token and not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
                    raise BrokerError("authorization", "invalid broker token")
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_REQUEST_BYTES:
                    raise BrokerError("size", "request is empty or exceeds 64 MiB")
                self.connection.settimeout(30)
                result = hub.dispatch(json.loads(self.rfile.read(length)))
                reply = dict(ok=True, result=result)
            except BrokerError as exc:
                reply = dict(ok=False, code=exc.code, error=str(exc))
            except (KeyError, ValueError, TypeError) as exc:
                reply = dict(ok=False, code="request", error=str(exc))
            except Exception:
                logging.exception("Unhandled ldcon instrument service request")
                reply = dict(ok=False, code="server", error="internal server error; inspect broker log")
            payload = json.dumps(reply).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                logging.warning("Client lost reply; command may have executed; leases retained")

        def log_message(self, fmt, *args):
            logging.debug(fmt, *args)

    return ThreadingHTTPServer((bind, port), Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--bind")
    parser.add_argument("--port", type=int)
    parser.add_argument("--simulate", action="store_true")
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    logging.basicConfig(level=logging.INFO)
    # One broker process per Linux user. A second copy cannot open another VISA
    # session even if started accidentally with a different listening port.
    import fcntl
    from ldcon.owners.migration_physical_lock import acquire_broker
    with acquire_broker() as lock:
        hub = Hub(config["instruments"], simulate=args.simulate,ecdl_rigs=config.get('ecdl_rigs'),sensors=config.get('sensors'),retained_instrument_faults=config.get('retained_instrument_faults'),
                  registry=config.get('registry'))
        server = make_server(hub, args.bind or config["bind"],
                             args.port if args.port is not None else config["port"],
                             token=os.environ.get("LDCON_INSTRUMENT_TOKEN") or os.environ.get("AWG_BROKER_TOKEN"))
        logging.info("ldcon instrument service %s; simulated=%s; instruments unopened", server.server_address, args.simulate)
        terminated = install_termination(server)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
            # SIGTERM (systemctl stop/restart): release this process's own owner leases ('ldcon rig ...' on the BK/SDG
            # owners) without any hardware write. 2026-10-22 18:2xZ: without a handler the process died on SIGTERM,
            # finally never ran, and the rig's leases stayed held, so the relock failed "CH1 is already leased".
            hub.close(hold=not terminated.is_set())
            logging.info("ldcon instrument service stopped; owner leases released (terminated=%s)", terminated.is_set())


def install_termination(server):
    """SIGTERM -> stop serve_forever from a helper thread (shutdown() must not run in the serving thread), so the
    finally block of main() runs and releases the owner leases. Returns the event that records the signal."""
    import signal
    import threading
    terminated = threading.Event()

    def handler(signum, frame):
        terminated.set()
        threading.Thread(target=server.shutdown, name='instrument-service-shutdown', daemon=True).start()
    signal.signal(signal.SIGTERM, handler)
    return terminated


if __name__ == "__main__":
    main()
