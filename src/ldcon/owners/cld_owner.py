"""One pinned CLD1015 owner, with cached reads and exclusive native-bias leases.

The existing Hub/Controller implements all typed controller operations. No reset,
output enable, mode change or setpoint write occurs on startup or shutdown.
See doc/cld-owner-migration.html before a real handoff.
"""
import argparse
import copy
from contextlib import contextmanager
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import logging
import math
import os
from pathlib import Path
import re
import threading
import time

from ldcon.drivers.cld1015 import Controller, InvalidMeasurement, PhysicalLock, VisaIO
from ldcon.drivers.cld1015_config import configuration
from ldcon.core.instrument_client import BrokerError
from ldcon.owners.instrument_service import Hub


_READ_METHODS = {"read", "describe", "validate", "validate_sweep"}
_WRITE_METHODS = {"apply", "enable", "activate_current", "save_current_tuning"}
_BIAS_FIELDS = ("current_a", "temperature_c", "current_limit_a", "ld_enabled",
                "tec_enabled", "modulation_enabled", "ld_mode", "tec_mode")
_NUMERIC_FIELDS = ("current_a", "current_read_a", "temperature_c", "temperature_read_c",
                   "tec_current_a", "current_limit_a")


def _invalid_json_fields(value, path=""):
    """Report unusable cache fields without serializing arbitrary objects."""
    if value is None or type(value) in (bool, str, int):
        return []
    if type(value) is float:
        return [] if math.isfinite(value) else [path or "<root>"]
    if isinstance(value, dict):
        result = []
        for key, child in value.items():
            if not isinstance(key, str):
                result.append(path + ".<nonstring key>")
            else:
                result.extend(_invalid_json_fields(child, f"{path}.{key}" if path else key))
        return result
    if isinstance(value, (list, tuple)):
        return [field for index, child in enumerate(value)
                for field in _invalid_json_fields(child, f"{path}[{index}]")]
    return [path or "<root>"]


def validate_config(value):
    config = copy.deepcopy(value)
    required = {"device_id", "instrument", "resource", "identity", "controller",
                "bind", "port", "poll_s", "max_age_s", "io_timeout_s",
                "read_unavailable_timeout_s", "state_file", "handoff_file", "writes_enabled"}
    if not required <= set(config) or set(config) - required - {'system_id','emission_interlock','projection_sources'}:
        raise ValueError("Incomplete or unknown CLD owner configuration")
    # Rule M step 3: the laser system owner installs the CH0 projection; one principal, channel 0 only.
    sources = config.get('projection_sources', {})
    if not isinstance(sources, dict) or len(sources) > 1:
        raise ValueError('projection_sources must be a host-configured mapping with at most one principal')
    for principal, source in sources.items():
        from experiment_core.records import safe_id
        safe_id(principal)
        if not isinstance(source, dict) or set(source) != {'system_id', 'key_env', 'channels'}:
            raise ValueError('Projection source requires system_id, key_env and channels')
        safe_id(source['system_id'])
        if not isinstance(source['key_env'], str) or not source['key_env']:
            raise ValueError('Projection source key_env must be an environment variable name')
        if source['channels'] != [0]:
            raise ValueError('A CLD projection source covers channel 0 only')
        if config.get('system_id') not in (None, source['system_id']):
            raise ValueError('Projection source system_id must equal the configured system_id')
    if 'emission_interlock' in config:
        v=config['emission_interlock']
        if not isinstance(v,dict) or set(v)!={'path','laser_id'} or not Path(v['path']).is_absolute() or v['laser_id']!=config.get('system_id'):
            raise ValueError('CLD emission interlock must pin its system ID and an absolute journal path')
    if 'system_id' in config:
        from experiment_core.records import safe_id
        safe_id(config['system_id'])
    for key in ("device_id", "instrument"):
        if not isinstance(config[key], str) or not config[key].isidentifier():
            raise ValueError(key + " must be an identifier")
    resource = config["resource"]
    # Pin the USB serial, vendor/product and interface. No discovery or aliases.
    match = re.fullmatch(r"USB[0-9]+::([0-9]+)::([0-9]+)::([A-Za-z0-9_-]+)::([0-9]+)::INSTR", resource)
    if not match or int(match[1]) != 0x1313:
        raise ValueError("Use the exact decimal, serial-pinned Thorlabs USB VISA resource")
    identity = config["identity"]
    if (set(identity) != {"manufacturer", "model", "serial"}
            or identity["manufacturer"].upper() != "THORLABS"
            or identity["model"].upper() != "CLD1015" or identity["serial"] != match[3]):
        raise ValueError("CLD identity must match the pinned USB serial")
    if (not ipaddress.ip_address(config["bind"]).is_loopback
            or type(config["port"]) is not int or not 0 < config["port"] < 65536
            or type(config["writes_enabled"]) is not bool):
        raise ValueError("Loopback bind, valid port and explicit writes_enabled required")
    for key in ("poll_s", "max_age_s", "io_timeout_s", "read_unavailable_timeout_s"):
        v = config[key]
        if type(v) not in (int, float) or not math.isfinite(v) or not 0 < v <= 120:
            raise ValueError(key + " must be finite in (0, 120] seconds")
    if config["max_age_s"] < config["poll_s"] or config["io_timeout_s"] > 30:
        raise ValueError("Cache age must cover polling; total I/O deadline must be <=30s")
    for key in ("state_file", "handoff_file"):
        if not isinstance(config[key], str) or not Path(config[key]).is_absolute():
            raise ValueError(key + " must be an absolute path")
    from ldcon.owners.cld_system_owner import LAYER1_CONTROLLER, LEGACY_CONTROLLER
    controller = config["controller"]
    if not isinstance(controller, dict) or set(controller) - LAYER1_CONTROLLER - LEGACY_CONTROLLER:
        raise ValueError("Controller definition may only contain the CLD physical facts (and ignored legacy policy copies)")
    if 'ld_enable_writes_enabled' in controller and type(controller['ld_enable_writes_enabled']) is not bool:
        raise ValueError('ld_enable_writes_enabled must be a boolean')
    rating = controller.get('current_limits_a')
    if rating is not None and (not isinstance(rating, list) or len(rating) != 2
                               or any(type(v) not in (int, float) or not math.isfinite(v) for v in rating) or not 0 <= rating[0] < rating[1]):
        raise ValueError('current_limits_a must be an increasing finite nonnegative pair (device rating)')
    return config


def split_controller(controller):
    """(layer-1 facts, legacy layer-2 copies) of a config.json ``controller`` block (rule M step 3)."""
    from ldcon.owners.cld_system_owner import LAYER1_CONTROLLER, LEGACY_CONTROLLER
    layer1 = {key: copy.deepcopy(value) for key, value in controller.items() if key in LAYER1_CONTROLLER}
    legacy = {key: copy.deepcopy(value) for key, value in controller.items()
              if key in LEGACY_CONTROLLER and key not in ('temperature_conditions_file', 'policy_device')}
    return layer1, legacy


def validate_handoff(path, resource):
    """Human-reviewed evidence gates first use; it cannot clear any prior fault."""
    record = json.loads(Path(path).read_text())
    if (record.get("schema_version") != 1 or record.get("resource") != resource
            or record.get("leases_empty") is not True
            or record.get("transport_closed") is not True
            or not record.get("reviewed_by") or not record.get("reviewed_at_utc")
            or not record.get("evidence")):
        raise ValueError("Reviewed idle, closed-transport handoff evidence is required")
    kind = record.get("disposition")
    if kind not in ("new_device", "removed_idle", "quarantined_read_failure"):
        raise ValueError("Unknown previous-owner disposition")
    if kind == "quarantined_read_failure" and (
            record.get("no_output_write_attempted") is not True
            or record.get("failure_stage") != "connect_identity_query"
            or record.get("previous_fault_retained") is not True):
        raise ValueError("A write/unknown fault cannot be bypassed by an independent owner")
    if kind != "new_device" and (not record.get("previous_endpoint") or not record.get("previous_instrument")):
        raise ValueError("Previous owner identity is required")
    return record


class _GuardedIO:
    """Reuse VISA I/O, applying a total transaction deadline and pinned identity."""
    def __init__(self, raw, owner):
        self.raw, self.owner = raw, owner

    def _deadline(self):
        remaining = self.owner.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("CLD transaction deadline exceeded")
        if getattr(self.raw, "inst", None) is not None:
            self.raw.inst.timeout = max(1, int(remaining * 1000))

    def query(self, command):
        self._deadline()
        result = self.raw.query(command)
        self._deadline()
        if command == "*IDN?":
            parts = [part.strip() for part in result.split(",")]
            identity = self.owner.config["identity"]
            if (len(parts) != 4 or parts[0].upper() != identity["manufacturer"].upper()
                    or parts[1].upper() != identity["model"].upper()
                    or parts[2] != identity["serial"]):
                raise ValueError("CLD physical identity differs from pinned serial/model")
        return result

    def write(self, command):
        self._deadline()
        def terminal():
            self.owner.write_attempted = True
            self.raw.write(command)
            self._deadline()
            if self.raw.query('*OPC?').strip() != '1':
                raise RuntimeError('CLD command completion is unknown')
            error = self.raw.query('SYST:ERR?')
            if int(error.split(',', 1)[0]) != 0:
                raise RuntimeError('CLD command failed: ' + error)
            self._deadline()
            return {'completed': True}
        return self.owner.physical_writer.write(command, terminal)

    def close(self):
        self.raw.close()


def _transport_error(exc):
    if isinstance(exc, OSError):
        return True
    try:
        from pyvisa.errors import VisaIOError
    except ImportError:
        return False
    return isinstance(exc, VisaIOError)


class CLDOwner:
    """Shared code, one physical resource, non-expiring durable exclusive lease."""
    def __init__(self, config, *, io_factory=None, clock=time.monotonic,
                 wall_clock=time.time, lock_factory=PhysicalLock):
        self.config = validate_config(config)
        self.layer1, self.legacy_copies = split_controller(self.config.pop('controller'))
        self.write_binding_enabled = bool(self.config.get('system_id') and self.config['writes_enabled'])
        self.emission_interlock = None
        if self.config.get('emission_interlock'):
            from ldcon.owners.emission import EmissionStore
            v=self.config['emission_interlock']
            self.emission_interlock=EmissionStore(v['path'],v['laser_id'])
        self.clock, self.wall_clock = clock, wall_clock
        self.lock, self.cache_lock = threading.RLock(), threading.RLock()
        self.stop_event, self.poll_thread = threading.Event(), None
        self.closed, self.pending = False, None
        self.cache, self.cached_s, self.observed_at = None, None, None
        self.observation_error, self.missing_since = None, None
        self.retry_at, self.retry_delay = 0., self.config["poll_s"]
        self.physical, self.deadline = None, None
        self.write_attempted = False
        self.state_file = Path(self.config["state_file"])
        self.audit_file = self.state_file.with_name('audit.jsonl')
        self.handoff = validate_handoff(self.config["handoff_file"], self.config["resource"])
        instrument_config = dict(profile="cld1015", channels=[0],
                                 resource=self.config["resource"], timeout_s=self.config["io_timeout_s"])
        self.hub = Hub({self.config["instrument"]: instrument_config})
        self.instrument = self.hub.instruments[self.config["instrument"]]
        policy = configuration(resource=self.config["resource"], timeout_s=self.config["io_timeout_s"])
        self.factory = io_factory or (lambda: VisaIO(self.config["resource"], self.config["io_timeout_s"],
                                                    physical_lock=self.physical))
        self.instrument.io = Controller(policy, factory=lambda: _GuardedIO(self.factory(), self))
        self.snapshot = {}
        try:
            # Same lock as the legacy broker, retained through pure-read reconnects.
            self.physical = lock_factory(self.config["resource"])
            self._initialize_system(self.layer1)
            for kind, values in self.system_owner.notes:
                self.audit(kind, **values)
            if self.legacy_copies:
                # Rule M step 3: operating conditions come from the system owner's projection; the copies an older
                # config.json still carries are ignored once a projection is installed, and serve as the pre-step-3
                # fallback until then (ops/trim_owner_config.py removes them).
                if self.system_owner.authority.projected:
                    self.audit('layer1_copy_ignored', keys=sorted(self.legacy_copies),
                               note='CLD operating conditions are the laser system projection; run ops/trim_owner_config.py')
                else:
                    self.audit('layer1_copy_fallback', keys=sorted(self.legacy_copies),
                               note='no system projection installed yet; the config copies apply until one arrives')
            if self.state_file.exists():
                if self.state_file.is_symlink():
                    raise ValueError("Refuse symlink owner state")
                record = json.loads(self.state_file.read_text())
                if record["definition"] != self.definition:
                    if record["leases"] or record["pending"] or record["fault"]:
                        raise ValueError("Saved CLD definition differs; offline review required")
                    # No retained lease, intent or fault: the definition is now the physical facts only (step 3).
                    self.audit('definition_changed_at_start', before=record["definition"], after=self.definition)
                self.instrument.leases = record["leases"]
                self.pending, self.instrument.fault = record["pending"], record["fault"]
                if self.pending or self.instrument.leases:
                    self.instrument.fault = self.instrument.fault or "Previous owner exited with retained lease or intent; review required"
            self._persist()
            self._snapshot()
        except BaseException:
            self.close()
            raise

    def _initialize_system(self, layer1):
        from experiment_core.records import Journal
        from ldcon.owners.cld_instrument_owner import CLDInstrumentOwner
        from ldcon.owners.cld_system_owner import CLDLaserSystemOwner
        self.policy_journal = Journal(str(self.state_file) + '.system.sqlite3')
        self.physical_writer = CLDInstrumentOwner(self, self.policy_journal)
        self.system_owner = CLDLaserSystemOwner(self, layer1,
            self.config.get('system_id') or self.config['device_id'] + '.read_only', self.policy_journal,
            legacy=self.legacy_copies)
        self.instrument.system_controller = self.system_owner

    def audit(self, kind, **values):
        """Append-only host record beside the state file (no secrets, no laser policy)."""
        record = dict(kind=kind, unix_s=self.wall_clock(), **values)
        logging.warning('cld owner %s: %s', kind, json.dumps(values, sort_keys=True, default=str))
        try:
            self.audit_file.parent.mkdir(parents=True, exist_ok=True)
            with self.audit_file.open('a') as stream:
                stream.write(json.dumps(record, sort_keys=True, default=str) + '\n')
        except OSError as exc:
            logging.warning('cld owner audit write failed: %s', exc)

    @property
    def definition(self):
        # Physical facts only (rule M step 3): resource, identity, instrument name, write binding, layer-1 controller.
        # The laser policy is a projection with its own revision and never changes this definition.
        definition = dict(resource=self.config["resource"], identity=self.config["identity"],
                          instrument=self.config["instrument"], writes_enabled=self.write_binding_enabled,
                          layer1=self.layer1)
        return hashlib.sha256(json.dumps(definition, sort_keys=True).encode()).hexdigest()

    def _persist(self):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        if self.state_file.is_symlink():
            raise ValueError("Refuse symlink owner state")
        temporary = self.state_file.with_name(self.state_file.name + ".tmp")
        record = dict(schema_version=1, definition=self.definition, leases=self.instrument.leases,
                      pending=self.pending, fault=self.instrument.fault, handoff=self.handoff)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(record, stream, allow_nan=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.state_file)
        directory = os.open(self.state_file.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def _projection_revision(self):
        from experiment_core.errors import StaleContext
        try:
            return self.physical_writer.guard.inspect(self.system_owner.binding)['projection_revision']
        except StaleContext:
            return None         # a later owner on the same journal replaced this generation (closing owner)

    def _snapshot(self):
        with self.cache_lock:
            self.snapshot = dict(device_id=self.config["device_id"], instrument=self.config["instrument"],
                                 resource=self.config["resource"], fault=self.instrument.fault,
                                 connected=self.instrument.io.io is not None,
                                 leases=[{k: v for k, v in lease.items() if k != "baseline"}
                                         for lease in self.instrument.leases.values()],
                                 bias_held=any(x.get("kind") == "bias" for x in self.instrument.leases.values()),
                                 observation_error=self.observation_error, sample=copy.deepcopy(self.cache),
                                 observed_at=self.observed_at, max_age_s=self.config["max_age_s"],
                                 writes_enabled=self.write_binding_enabled,
                                 projected=self.system_owner.authority.projected,
                                 policy_revision=self.system_owner.authority.source_revision,
                                 projection_revision=self._projection_revision())

    def status(self):
        # GET never waits for USB or schedules extra I/O.
        with self.cache_lock:
            result = copy.deepcopy(self.snapshot)
            age = None if self.cached_s is None else max(0., self.clock() - self.cached_s)
        if self.emission_interlock is not None:
            result['emission_interlock']={**self.config['emission_interlock'],
                'ld_enable_writes_enabled':self.system_owner.authority.snapshot().get('ld_enable_writes_enabled',False),
                'state':self.emission_interlock.status()}
        result["age_s"] = age
        result["fresh"] = bool(age is not None and age <= self.config["max_age_s"]
                               and not result.get("fault") and not result.get("observation_error") and not self.closed)
        invalid = _invalid_json_fields(result)
        if invalid:
            # This should be unreachable after _publish validation. Never expose
            # a malformed cache as fresh or drop the HTTP connection on encoding.
            return dict(device_id=self.config["device_id"], instrument=self.config["instrument"],
                        resource=self.config["resource"], fresh=False, sample=None, age_s=None,
                        max_age_s=self.config["max_age_s"], observed_at=None,
                        fault="Invalid owner cache; review required", bias_held=result.get("bias_held") is True,
                        observation_error="Invalid cached fields: " + ", ".join(invalid),
                        invalid_fields=invalid)
        return result

    def _latch(self, message):
        self.instrument.fault = str(message)
        if self.write_attempted:
            self.physical_writer.guard.quarantine(message)
        try:
            self._persist()
        except (OSError, ValueError) as exc:
            self.instrument.fault += "; persistence failed: " + str(exc)
        self._snapshot()
        return BrokerError("fault", self.instrument.fault)

    @contextmanager
    def _operation(self, seconds=None):
        self.write_attempted = False
        self.deadline = time.monotonic() + (seconds or self.config["io_timeout_s"])
        try:
            yield
        finally:
            self.deadline = None

    def _publish(self, sample, started, observed_at):
        invalid = _invalid_json_fields(sample, "sample")
        for field in _NUMERIC_FIELDS:
            if not isinstance(sample, dict) or type(sample.get(field)) not in (int, float):
                invalid.append("sample." + field)
        if type(started) not in (int, float) or not math.isfinite(started):
            invalid.append("cached_s")
        if type(observed_at) not in (int, float) or not math.isfinite(observed_at):
            invalid.append("observed_at")
        if invalid:
            raise InvalidMeasurement("Invalid CLD cache fields: " + ", ".join(sorted(set(invalid))))
        for lease in self.instrument.leases.values():
            if lease.get("kind") == "bias" and any(sample[key] != lease["baseline"][key] for key in _BIAS_FIELDS):
                raise self._latch("CLD held bias, limits, modes or enables changed externally")
        with self.cache_lock:
            self.cache, self.cached_s, self.observed_at = copy.deepcopy(sample), started, observed_at
            self.observation_error, self.missing_since = None, None
            self.retry_delay = self.config["poll_s"]
            self._snapshot()

    def _unavailable(self, exc):
        # Only pure reads get this path. Close the timed-out stream, retain the
        # physical flock, and discard the entire partial sample before retrying.
        self.observation_error = str(exc)
        if self.missing_since is None:
            self.missing_since = self.clock()
        try:
            self.instrument.io.close()
        except Exception as close_error:
            raise self._latch("Read-session closure uncertain: " + str(close_error)) from close_error
        self.retry_at = self.clock() + self.retry_delay
        self.retry_delay = min(5., self.retry_delay * 2)
        if self.clock() - self.missing_since >= self.config["read_unavailable_timeout_s"]:
            raise self._latch("CLD continuous read unavailability deadline exceeded: " + str(exc)) from exc
        self._snapshot()

    def refresh(self):
        with self.lock:
            if self.closed or self.instrument.fault:
                return
            if (self.missing_since is not None
                    and self.clock() - self.missing_since >= self.config["read_unavailable_timeout_s"]):
                raise self._latch("CLD continuous read unavailability deadline exceeded")
            if self.clock() < self.retry_at:
                return
            started, observed_at = self.clock(), self.wall_clock()
            try:
                with self._operation():
                    controller = self.instrument.io
                    sample = controller.read() if controller.io is not None else controller.connect()
                self._publish(sample, started, observed_at)
            except BrokerError:
                raise
            except Exception as exc:
                if isinstance(exc, InvalidMeasurement) or _transport_error(exc):
                    self._unavailable(exc)
                else:
                    raise self._latch("CLD read/identity integrity failure: " + str(exc)) from exc

    def start_polling(self):
        if self.poll_thread is not None:
            raise RuntimeError("CLD polling is already running")
        def run():
            while not self.stop_event.is_set():
                try:
                    self.refresh()
                except BrokerError:
                    pass  # Fault is durable and visible; no further USB I/O.
                if self.stop_event.wait(self.config["poll_s"]):
                    break
        self.poll_thread = threading.Thread(target=run, daemon=True, name="cld-owner-poll")
        self.poll_thread.start()

    def _ready(self):
        status = self.status()
        if status["fault"]:
            raise BrokerError("fault", status["fault"])
        if not status["fresh"]:
            raise BrokerError("measurement", "Fresh cached CLD readback unavailable")
        return status

    def dispatch(self, request):
        action = request["action"]
        if action == "status":
            return self.status()
        if request.get("instrument") != self.config["instrument"]:
            raise BrokerError("unknown_instrument", "Wrong physical CLD owner")
        with self.lock:
            if self.closed:
                raise BrokerError("closed", "CLD owner is closed")
            if action in ("projection_status", "projection_install"):
                # Rule M step 3: the laser system owner installs the CH0 operating conditions (signed receipt).
                if action == "projection_install" and self.instrument.fault:
                    raise BrokerError("fault", "A faulted owner cannot admit new projections")
                from experiment_core.errors import AuthorizationRequired, Conflict, StaleContext, UncertainWrite
                try:
                    method = self.system_owner.projection_status if action == "projection_status" else self.system_owner.projection_install
                    result = method(request)
                except (AuthorizationRequired, Conflict, StaleContext, UncertainWrite, ValueError, KeyError, TypeError) as exc:
                    raise BrokerError("projection", str(exc)) from exc
                if action == "projection_install":
                    self.audit('projection_installed', principal=request.get('principal'),
                               projection_revision=result.get('projection_revision'),
                               policy_revision=(result.get('projection') or {}).get('policy_revision'))
                    if self.legacy_copies and self.system_owner.authority.legacy is not None:
                        self.system_owner.authority.legacy = None       # the projection supersedes the copies for good
                        self.audit('layer1_copy_ignored', keys=sorted(self.legacy_copies),
                                   note='a system projection is installed; run ops/trim_owner_config.py')
                    self._snapshot()
                return result
            if action == "bias_status":
                lease = self.instrument.leases.get(request.get("lease"))
                if not lease or lease.get("kind") != "bias":
                    raise BrokerError("ownership", "No matching native bias lease")
                return self.status()
            if self.instrument.fault:
                raise BrokerError("fault", self.instrument.fault)
            if action in ("claim", "bias_claim"):
                if action == "bias_claim":
                    if self.emission_interlock is not None:
                        self.emission_interlock.require_control('bias_claim')
                    status = self._ready()
                    sample = status["sample"]
                    if (not all(sample[key] is True for key in ("ld_enabled", "tec_enabled", "modulation_enabled"))
                            or sample["ld_mode"].upper() not in ("CURR", "CURRENT")
                            or sample["tec_mode"].upper() not in ("TEMP", "TEMPERATURE")):
                        raise BrokerError("range", "Native bias requires LD/TEC/modulation enabled and current/temperature modes")
                    for field, policy in (("current_a", "current_limits_a"), ("current_read_a", "current_limits_a"),
                                          ("temperature_c", "temperature_limits_c"), ("temperature_read_c", "temperature_limits_c")):
                        bounds = self.system_owner.authority.snapshot().get(policy)
                        if bounds is None or not bounds[0] <= sample[field] <= bounds[1]:
                            raise BrokerError("range", "CLD bias requires explicit, satisfied current/temperature bounds")
                    if not 0 <= sample["current_read_a"] <= sample["current_limit_a"]:
                        raise BrokerError("range", "Measured current exceeds CLD current limit")
                    request = dict(request, action="claim", channel=0, profile="cld1015")
                result = self.hub.dispatch(request)
                entry = self.instrument.leases[result["lease"]]
                entry["kind"] = "bias" if action == "bias_claim" else "controller"
                if action == "bias_claim":
                    entry["baseline"] = {key: sample[key] for key in _BIAS_FIELDS}
                try:
                    self._persist()
                except Exception as exc:
                    raise self._latch("CLD lease persistence failed: " + str(exc)) from exc
                self._snapshot()
                return dict(self.status(), **result) if action == "bias_claim" else result
            lease = self.instrument.leases.get(request.get("lease"))
            if action in ("release", "bias_release"):
                if not lease:
                    return False
                if lease.get("kind") == "bias":
                    if action != "bias_release" or request.get("native_stopped") is not True:
                        raise BrokerError("ownership", "Explicit native stop confirmation required before bias release")
                elif action == "bias_release":
                    raise BrokerError("ownership", "Not a native bias lease")
                result = self.hub.dispatch(dict(request, action="release"))
                try:
                    if lease.get('kind') == 'controller':
                        self.system_owner.release()
                    self._persist()
                except Exception as exc:
                    # Retain the original token after an uncommitted release.
                    self.instrument.leases[request["lease"]] = lease
                    raise self._latch("CLD release persistence failed: " + str(exc)) from exc
                self._snapshot()
                return result
            if action != "controller" or not lease or lease.get("kind") != "controller":
                raise BrokerError("ownership", "Typed operation requires exclusive controller lease")
            method = request.get("method")
            if method not in _READ_METHODS | _WRITE_METHODS:
                raise BrokerError("method", "Unsupported CLD operation")
            if method == "read":
                return self._ready()["sample"]
            if method in _WRITE_METHODS and not self.write_binding_enabled:
                raise BrokerError("range", "This independent owner is read-only")
            if method == "describe":
                result = self.hub.dispatch(request)
                if not self.write_binding_enabled:
                    result.update(current_writes_enabled=False, temperature_writes_enabled=False)
                return result
            self._ready()
            mutating = method in _WRITE_METHODS
            if mutating:
                self.pending = dict(method=method, lease=request["lease"], requested_at=self.wall_clock())
                try:
                    self._persist()
                except Exception as exc:
                    raise self._latch("CLD write-intent persistence failed: " + str(exc)) from exc
            started, observed_at = self.clock(), self.wall_clock()
            try:
                ramp_s = float(request.get("ramp_s", 0))
                if not math.isfinite(ramp_s) or not 0 <= ramp_s <= 3600:
                    raise BrokerError("range", "Invalid ramp duration")
                with self._operation(self.config["io_timeout_s"] + ramp_s):
                    result = self.hub.dispatch(request)
                if isinstance(result, dict) and all(k in result for k in _BIAS_FIELDS):
                    self._publish(result, started, observed_at)
                self.pending = None
                self._persist()
                self._snapshot()
                return result
            except BrokerError as exc:
                # Existing Hub marks non-validation failures fatal. Never erase
                # that fault or retry a potentially sent mutation.
                if self.write_attempted or self.instrument.fault or exc.code not in ("range", "measurement", "authorization"):
                    raise self._latch("CLD operation failed; review required: " + str(exc)) from exc
                self.pending = None
                self._persist()
                raise
            except Exception as exc:
                raise self._latch("CLD operation result uncertain: " + str(exc)) from exc

    def close(self):
        self.stop_event.set()
        if self.poll_thread is not None:
            self.poll_thread.join(timeout=self.config["io_timeout_s"] + 1)
        with self.lock:
            self.closed = True
            try:
                self.hub.close()  # USB session close only; no output commands.
            finally:
                if self.physical is not None:
                    self.physical.close()
                    self.physical = None
                if hasattr(self, 'system_owner'):
                    self._snapshot()


def make_server(owner, bind="127.0.0.1", port=0, token=None):
    class Handler(BaseHTTPRequestHandler):
        def reply(self, value, status=200):
            try:
                payload = json.dumps(value, allow_nan=False).encode()
            except (ValueError, TypeError, OverflowError):
                logging.exception("CLD owner response contains invalid JSON evidence")
                status = 500
                payload = json.dumps(dict(ok=False, code="server", fresh=False, sample=None,
                    error="Invalid owner response; inspect stderr", invalid_fields=_invalid_json_fields(value))).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass  # Lost mutation replies do not release or replay leases.

        def authorized(self):
            return not token or hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token)

        def do_GET(self):
            if not self.authorized():
                self.reply({"error": "unauthorized"}, 403)
            elif self.path != "/status":
                self.reply({"error": "use /status"}, 404)
            else:
                try:
                    self.reply(owner.status())
                except Exception:
                    logging.exception("CLD owner status request failed")
                    self.reply(dict(ok=False, code="server", fresh=False, sample=None,
                                    error="Owner status failure; inspect stderr"), 500)

        def do_POST(self):
            try:
                if not self.authorized():
                    raise BrokerError("authorization", "Invalid owner token")
                if self.path != "/rpc":
                    raise BrokerError("path", "Use /rpc")
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 65536:
                    raise BrokerError("size", "Request must be 1..65536 bytes")
                self.connection.settimeout(10)
                result = owner.dispatch(json.loads(self.rfile.read(length)))
                self.reply(dict(ok=True, result=result))
            except BrokerError as exc:
                self.reply(dict(ok=False, code=exc.code, error=str(exc)))
            except (ValueError, TypeError, KeyError) as exc:
                self.reply(dict(ok=False, code="request", error=str(exc)))
            except Exception:
                self.reply(dict(ok=False, code="server", error="Owner failure; inspect persisted state"))

        def log_message(self, fmt, *args):
            pass

    return ThreadingHTTPServer((bind, port), Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--validate-only", action="store_true",
                        help="Validate config syntax without locks or device I/O")
    args = parser.parse_args()
    config = validate_config(json.loads(args.config.read_text()))
    if args.validate_only:
        print(json.dumps(dict(valid=True, device_id=config["device_id"], resource=config["resource"])))
        return
    owner = CLDOwner(config)
    try:
        server = make_server(owner, config["bind"], config["port"], os.environ.get("LDCON_INSTRUMENT_TOKEN"))
        try:
            owner.start_polling()
            server.serve_forever()
        finally:
            server.server_close()
    finally:
        owner.close()


if __name__ == "__main__":
    main()
