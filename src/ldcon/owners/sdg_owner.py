"""One physical SDG owner: cached monitoring and bounded, leased DC offsets.

No service is started by import. See doc/sdg-owner-migration.html before use.
"""
import argparse
import copy
from contextlib import contextmanager
import fcntl
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import socket
import threading
import time
import uuid

from ldcon.core.contracts import DeviceError
from ldcon.drivers.ecdl import OffsetAWG
from ldcon.drivers.siglent import fields
from ldcon.core.instrument_client import BrokerError
from ldcon.owners.instrument_service import make_server


PROFILE = "siglent_sdg2000x_offset_v1"


def finite(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("Expected a finite number")
    return float(value)


def validate_config(config):
    config = copy.deepcopy(config)
    required = {"device_id", "host", "profile", "identity", "channels", "poll_s",
                "max_age_s", "io_timeout_s", "state_file", "bind", "port"}
    if set(config) != required or config["profile"] != PROFILE:
        raise ValueError("Incomplete or unknown SDG owner configuration/profile")
    if not isinstance(config["device_id"], str) or not config["device_id"].isidentifier():
        raise ValueError("device_id must be an identifier")
    if not isinstance(config["host"], str) or not config["host"]:
        raise ValueError("host is required")
    identity = config["identity"]
    if (set(identity) != {"manufacturer", "model", "serial", "firmware"}
            or identity["manufacturer"] != "Siglent Technologies"
            or identity["model"] != "SDG2042X"
            or identity["firmware"] != "2.01.01.39R7"
            or not isinstance(identity["serial"], str) or not identity["serial"]):
        raise ValueError("Expected an explicitly qualified SDG2042X identity/firmware")
    for key in ("poll_s", "max_age_s", "io_timeout_s"):
        if not 0 < finite(config[key]) <= 30:
            raise ValueError(key + " must be in (0, 30] seconds")
    if config["max_age_s"] < config["poll_s"]:
        raise ValueError("max_age_s must cover the polling interval")
    if (not isinstance(config["state_file"], str) or not config["state_file"]
            or not ipaddress.ip_address(config["bind"]).is_loopback
            or type(config["port"]) is not int or not 0 < config["port"] < 65536):
        raise ValueError("Persistent state, a loopback bind and a valid port are required")
    if set(config["channels"]) != {"1", "2"}:
        raise ValueError("Both physical channels must be represented")
    for policy in config["channels"].values():
        if set(policy) != {"writes_enabled", "policy"} or type(policy["writes_enabled"]) is not bool:
            raise ValueError("Invalid channel configuration")
        p = policy["policy"]
        if p is None and not policy["writes_enabled"]:
            continue
        expected = {"bounds_v", "activation_range_v", "max_step_v", "max_slew_v_per_s",
                    "update_interval_s", "readback_tolerance_v", "revision"}
        if not isinstance(p, dict) or set(p) != expected:
            raise ValueError("Enabled offsets require explicit channel safety policy")
        for key in ("bounds_v", "activation_range_v"):
            bounds = p[key]
            if not isinstance(bounds, list) or len(bounds) != 2 or finite(bounds[0]) >= finite(bounds[1]):
                raise ValueError("Invalid offset/activation bounds")
        if not p["activation_range_v"][0] <= 0 <= p["activation_range_v"][1]:
            raise ValueError("Activation-relative bounds must contain zero")
        for key in ("max_step_v", "max_slew_v_per_s", "update_interval_s", "readback_tolerance_v"):
            if finite(p[key]) <= 0:
                raise ValueError("Policy limits must be positive")
        if not isinstance(p["revision"], str) or not p["revision"]:
            raise ValueError("Policy revision is required")
    return config


def policy_digest(policy):
    return hashlib.sha256(json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class PhysicalLock:
    """Same resolved-IP lock as legacy RigHardware, acquired before connecting."""
    def __init__(self, host):
        self.address = socket.gethostbyname(host)
        fingerprint = hashlib.sha256(f"{self.address}:5025".encode()).hexdigest()[:24]
        self.handle = open(f"/tmp/ldcon-physical-{fingerprint}.lock", "a")
        try:
            fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self.handle.close()
            raise

    def close(self):
        self.handle.close()


class SDGOffsetDriver(OffsetAWG):
    """Existing offset transport, with a total operation deadline and evidence."""
    @contextmanager
    def deadline(self, timeout_s):
        self.until = time.monotonic() + timeout_s
        try:
            yield
        finally:
            self.until = None

    def query(self, command):
        until = getattr(self, "until", None)
        if until is None:
            raise RuntimeError("SDG queries require an operation deadline")
        def remaining():
            value = until - time.monotonic()
            if value <= 0:
                raise TimeoutError("SDG operation deadline exceeded")
            self.socket.settimeout(value)
        remaining()
        self.socket.sendall((command + "\n").encode("ascii"))
        data = bytearray()
        # Buffered readline can reset its socket timeout on each partial recv.
        # Check the absolute deadline for every fragment, including slow dribbles.
        while not data.endswith(b"\n"):
            remaining()
            fragment = self.socket.recv(min(4096, 65537 - len(data)))
            if not fragment:
                raise DeviceError("SDG disconnected during query")
            data.extend(fragment)
            if len(data) > 65536 or b"\n" in data[:-1]:
                raise DeviceError("Invalid or multiple SDG reply lines")
        remaining()
        result = data.decode("ascii").strip()
        if hasattr(self, "evidence"):
            self.evidence[command] = result
        return result

    def sample(self):
        self.evidence = {}
        coupling = fields(self.query("COUP?"))
        channels = {str(ch): self.read(ch) for ch in (1, 2)}
        raw = {}
        for ch in (1, 2):
            prefix = f"C{ch}:"
            raw[str(ch)] = {key[len(prefix):]: (fields(value) if key == prefix + "BSWV?" else value)
                            for key, value in self.evidence.items() if key.startswith(prefix)}
        return dict(channels=channels, coupling=coupling, raw=raw)


class SDGOwner:
    """Durable non-expiring channel leases; one serialized physical session."""
    def __init__(self, config, *, factory=SDGOffsetDriver, clock=time.monotonic,
                 lock_factory=PhysicalLock):
        self.config = validate_config(config)
        self.clock = clock
        self.factory = factory
        self.lock = threading.RLock()
        self.cache_lock = threading.RLock()
        self.cache = None
        self.cached_s = None
        self.fault = None
        self.observation_error = None
        self.retry_at = 0.
        self.retry_delay = self.config["poll_s"]
        self.leases = {}
        self.pending = None
        self.last_write = {}
        self.io = None
        self.physical = None
        self.closed = False
        self.state_file = Path(self.config["state_file"])
        self.definition = policy_digest({k: v for k, v in self.config.items()
                                         if k not in ("bind", "port", "state_file")})
        self.stop_event = threading.Event()
        self.poll_thread = None
        # Lock first: even loading the persistence file must not race another owner.
        self.physical = lock_factory(self.config["host"])
        try:
            if self.state_file.exists():
                record = json.loads(self.state_file.read_text())
                if record["definition"] != self.definition:
                    raise DeviceError("Saved SDG definition differs; offline review required")
                self.leases = record["leases"]
                self.pending = record["pending"]
                self.fault = record["fault"]
                if self.leases or self.pending:
                    self.fault = self.fault or "Previous owner exited with retained leases; offline review required"
            self._persist()
            # A quarantined restart serves evidence without reconnecting or taking over.
            if not self.fault:
                self.refresh()
        except BaseException:
            self.close()
            raise

    def _persist(self):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_file.with_name(self.state_file.name + ".tmp")
        record = dict(schema_version=1, definition=self.definition, leases=self.leases,
                      pending=self.pending, fault=self.fault)
        with temporary.open("w") as stream:
            json.dump(record, stream, allow_nan=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.state_file)
        directory = os.open(self.state_file.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def _latch(self, reason):
        self.fault = str(reason)
        try:
            self._persist()
        except OSError as exc:
            self.fault += "; state persistence failed: " + str(exc)
        return BrokerError("fault", self.fault)

    def _sample(self):
        with self.io.deadline(self.config["io_timeout_s"]):
            sample = self.io.sample()
        for ch in ("1", "2"):
            finite(sample["channels"][ch]["value"])
            finite(sample["channels"][ch]["amplitude_vpp"])
        return sample

    def _connect(self):
        self.io = self.factory(self.physical.address, timeout=self.config["io_timeout_s"])
        with self.io.deadline(self.config["io_timeout_s"]):
            actual = self.io.describe()
        expected = ",".join(self.config["identity"][key]
                            for key in ("manufacturer", "model", "serial", "firmware"))
        if actual != expected:
            raise self._latch("SDG physical identity/firmware does not match configuration")

    def _read_unavailable(self, reason):
        # Only used for pure queries before any write intent. Discard the stream
        # so a delayed reply cannot be mistaken for another channel's response.
        self.observation_error = str(reason)
        if self.io is not None:
            try:
                self.io.close()
            except OSError:
                pass
            self.io = None
        self.retry_at = self.clock() + self.retry_delay
        self.retry_delay = min(5., self.retry_delay * 2)

    def _publish(self, sample):
        with self.cache_lock:
            self.cache = copy.deepcopy(sample)
            self.cached_s = self.clock()
            self.observation_error = None
            self.retry_delay = self.config["poll_s"]

    def refresh(self):
        with self.lock:
            if self.fault or self.closed:
                return
            if self.io is None and self.clock() < self.retry_at:
                return
            try:
                if self.io is None:
                    self._connect()
                self._publish(self._sample())
            except BrokerError:
                raise
            except (OSError, DeviceError, ValueError, KeyError, TypeError, UnicodeError) as exc:
                self._read_unavailable(exc)

    def start_polling(self):
        if self.poll_thread is not None:
            raise RuntimeError("SDG polling already started")
        def poll():
            while not self.stop_event.wait(self.config["poll_s"]):
                try:
                    self.refresh()
                except BrokerError:
                    return
        self.poll_thread = threading.Thread(target=poll, name="sdg-owner-cache", daemon=True)
        self.poll_thread.start()

    def status(self):
        with self.cache_lock:
            age = None if self.cached_s is None else max(0., self.clock() - self.cached_s)
            return dict(device_id=self.config["device_id"], profile=PROFILE,
                        identity=copy.deepcopy(self.config["identity"]), fault=self.fault,
                        connected=self.io is not None and not self.closed,
                        read_ok=self.observation_error is None and self.cache is not None,
                        observation_error=self.observation_error,
                        fresh=not self.fault and self.observation_error is None and age is not None and age <= self.config["max_age_s"],
                        age_s=age, sample=copy.deepcopy(self.cache))

    def _healthy(self):
        if self.closed or self.fault:
            raise BrokerError("fault", self.fault or "SDG owner is closed")

    def _ready(self, sample, channel, policy):
        if any(sample["coupling"].get(key) != "OFF" for key in ("TRACE", "FCOUP", "PCOUP", "ACOUP")):
            raise BrokerError("range", "Independent channel offsets require all coupling/tracking OFF")
        state = sample["channels"][str(channel)]
        if (state["waveform"] != "DC" or not state["enabled"] or state["load"] != "HZ"
                or state["polarity"] != "NOR" or state["amplitude_vpp"] != 0
                or any(state[key] != "OFF" for key in ("burst", "modulation", "sweep"))):
            raise BrokerError("range", "Offset channel must already be enabled DC/HZ/NOR without modulation")
        if not policy["bounds_v"][0] <= finite(state["value"]) <= policy["bounds_v"][1]:
            raise BrokerError("range", "Present DC offset is outside configured bounds")

    @staticmethod
    def _unchanged(before, after, channel):
        expected = copy.deepcopy(before)
        # OFST is the sole permitted change; compare complete returned channel metadata.
        expected["channels"][str(channel)]["value"] = after["channels"][str(channel)]["value"]
        expected["raw"][str(channel)]["BSWV?"]["OFST"] = after["raw"][str(channel)]["BSWV?"]["OFST"]
        if expected != after:
            raise DeviceError("Offset changed another channel, output, waveform or coupling state")

    def dispatch(self, request):
        action = request.get("action")
        if request.get("device_id") != self.config["device_id"]:
            raise BrokerError("configuration", "SDG device identity does not match endpoint")
        if action == "status":
            return self.status()  # Cache only; API polling never increases hardware reads.
        with self.lock:
            if action == "describe":
                return dict(device_id=self.config["device_id"], host=self.config["host"],
                            profile=PROFILE, identity=copy.deepcopy(self.config["identity"]),
                            channels=copy.deepcopy(self.config["channels"]), fault=self.fault,
                            leases=[dict(channel=v["channel"], owner=v["owner"]) for v in self.leases.values()])
            self._healthy()
            if action == "claim":
                channel = request.get("channel")
                if type(channel) is not int or channel not in (1, 2):
                    raise BrokerError("channel", "Use CH1 or CH2")
                configured = self.config["channels"][str(channel)]
                policy = configured["policy"]
                if (not configured["writes_enabled"] or policy_digest(policy) != request.get("policy_digest")
                        or request.get("identity") != self.config["identity"]):
                    raise BrokerError("configuration", "Channel writes/policy/identity are not authorized")
                if any(v["channel"] == channel for v in self.leases.values()):
                    raise BrokerError("busy", "This channel already has a non-expiring lease")
                cached = self.status()
                if not cached["fresh"]:
                    raise BrokerError("measurement", "SDG cache is stale")
                self._ready(cached["sample"], channel, policy)
                token = uuid.uuid4().hex
                self.leases[token] = dict(channel=channel, owner=str(request.get("owner", "client"))[:200],
                                          activation_v=cached["sample"]["channels"][str(channel)]["value"])
                self.last_write[token] = self.clock()
                try:
                    self._persist()
                except OSError as exc:
                    raise self._latch("Lease persistence failed: " + str(exc)) from exc
                return dict(lease=token)
            token = request.get("lease")
            lease = self.leases.get(token)
            if lease is None:
                raise BrokerError("ownership", "An exact channel lease is required")
            if action == "release":
                # No output command or restoration is issued on release.
                del self.leases[token]
                try:
                    self._persist()
                except OSError as exc:
                    self.leases[token] = lease
                    raise self._latch("Release persistence failed: " + str(exc)) from exc
                self.last_write.pop(token, None)
                return True
            if action != "offset":
                raise BrokerError("action", "Only typed status/describe/claim/release/offset are supported")
            return self._offset(token, lease, request)

    def _offset(self, token, lease, request):
        channel = lease["channel"]
        policy = self.config["channels"][str(channel)]["policy"]
        value = finite(request["value"])
        expected = finite(request["expected_v"])
        lower = max(policy["bounds_v"][0], lease["activation_v"] + policy["activation_range_v"][0])
        upper = min(policy["bounds_v"][1], lease["activation_v"] + policy["activation_range_v"][1])
        if not lower <= value <= upper:
            raise BrokerError("range", "Absolute or activation-relative DC offset bound exceeded")
        if self.io is None:
            raise BrokerError("measurement", "SDG read connection is recovering; no write attempted")
        try:
            before = self._sample()
        except (OSError, DeviceError, ValueError, KeyError, TypeError, UnicodeError) as exc:
            self._read_unavailable(exc)
            raise BrokerError("measurement", "Prewrite SDG observation unavailable; no write attempted") from exc
        self._publish(before)
        self._ready(before, channel, policy)
        previous = before["channels"][str(channel)]["value"]
        if abs(previous - expected) > policy["readback_tolerance_v"]:
            raise BrokerError("measurement", "Prewrite offset differs from the caller's observation")
        elapsed = self.clock() - self.last_write[token]
        if elapsed + 1e-9 < policy["update_interval_s"]:
            raise BrokerError("measurement", "Channel update interval has not elapsed")
        permitted = min(policy["max_step_v"], policy["max_slew_v_per_s"] * min(elapsed, policy["update_interval_s"]))
        if abs(value - previous) > permitted + 1e-12:
            raise BrokerError("range", "Channel step/slew limit exceeded")
        self.pending = dict(channel=channel, lease=token, before_v=previous, value=value)
        try:
            self._persist()  # Durable intent precedes the first physical write.
        except OSError as exc:
            raise self._latch("Write-intent persistence failed: " + str(exc)) from exc
        try:
            with self.io.deadline(self.config["io_timeout_s"]):
                self.io.offset(channel, value)
                after = self.io.sample()
            actual = finite(after["channels"][str(channel)]["value"])
            if abs(actual - value) > policy["readback_tolerance_v"]:
                raise DeviceError("SDG offset verification mismatch")
            self._ready(after, channel, policy)
            self._unchanged(before, after, channel)
            self.last_write[token] = self.clock()
            self.pending = None
            self._persist()
            self._publish(after)
            return dict(value=actual, sample=copy.deepcopy(after))
        except Exception as exc:
            # Unknown execution or post-write evidence: retain every lease and stop writes.
            raise self._latch("Uncertain SDG output; review required: " + str(exc)) from exc

    def close(self):
        self.stop_event.set()
        if self.poll_thread is not None:
            self.poll_thread.join(timeout=self.config["io_timeout_s"] + 1)
        with self.lock:
            self.closed = True
            try:
                if self.io is not None:
                    self.io.close()  # Socket closure never changes channel outputs.
            finally:
                if self.physical is not None:
                    self.physical.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--validate-only", action="store_true", help="Validate JSON without locks, network or hardware")
    args = parser.parse_args()
    config = validate_config(json.loads(args.config.read_text()))
    if args.validate_only:
        print(json.dumps(dict(valid=True, device_id=config["device_id"], profile=PROFILE)))
        return
    owner = SDGOwner(config)
    try:
        server = make_server(owner, config["bind"], config["port"], token=os.environ.get("LDCON_INSTRUMENT_TOKEN"))
        owner.start_polling()
        try:
            server.serve_forever()
        finally:
            server.server_close()
    finally:
        owner.close()


if __name__ == "__main__":
    main()
