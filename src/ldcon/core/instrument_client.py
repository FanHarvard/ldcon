"""Reusable SCPI transport for sweep and manual labscript device workers."""
import base64
import json
import os
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


class BrokerError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(f"AWG broker {code}: {message}")
        self.code = code


def validate_endpoint(endpoint):
    parsed = urlsplit(endpoint)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in ("", "/")):
        raise ValueError("broker endpoint must be http(s)://host:port without credentials/path")
    return endpoint.rstrip("/")


class BrokerTransport:
    """Lease one channel until close(); no connection to the AWG in this process.

    The broker keeps a lease if the client disappears. There is no timeout-based
    takeover of an armed waveform. Retain this object when restoration fails.
    Construct this only when the device needs the hardware, not in worker.init().
    """
    def __init__(self, config, timeout_s=10, *, owner="labscript"):
        broker = config["broker"]
        self.endpoint = validate_endpoint(broker["endpoint"])
        self.instrument = broker["instrument"]
        self.channel = int(config["channel"])
        self.timeout_s = float(timeout_s)
        self.lease = None
        self.projection = None
        self.projection_spec = broker.get('projection', {})
        result = self._rpc("claim", instrument=self.instrument, channel=self.channel,
                           resource=config["resource"], profile=config["profile"], owner=owner)
        self.lease = result["lease"]
        self.simulated = result.get("simulated", False)

    def _rpc(self, action, **values):
        request = Request(self.endpoint + "/rpc", method="POST",
                          data=json.dumps(dict(action=action, **values)).encode(),
                          headers={"Content-Type": "application/json"})
        token = os.environ.get("LDCON_INSTRUMENT_TOKEN") or os.environ.get("AWG_BROKER_TOKEN")
        if token:
            request.add_header("Authorization", "Bearer " + token)
        # Mutations are never retried: a timeout may occur after execution.
        with urlopen(request, timeout=self.timeout_s) as response:
            result = json.load(response)
        if not result.get("ok"):
            raise BrokerError(result.get("code", "error"), result.get("error", "unknown error"))
        return result["result"]

    def transaction(self, operations):
        """Execute a batch without another channel interleaving its commands."""
        if self.lease is None:
            raise BrokerError("closed", "transport has no channel lease")
        if self.projection is not None and any(op.get('op') not in ('get', 'query') for op in operations):
            return self.projection.transaction(self.channel, operations)
        return self._rpc("transaction", instrument=self.instrument, lease=self.lease,
                         operations=operations)

    def configure_projection(self, authority, constraints):
        from ldcon.owners.projection_transport import ProjectionClient
        transport = self
        class Link:
            def rpc(self, action, **values):
                return transport._rpc(action, instrument=transport.instrument, **values)
        if self.projection is None:
            self.projection = ProjectionClient(authority, Link(), self.projection_spec,
                                               {self.channel: self.lease}, constraints)
        else:
            if self.projection.authority is not authority:
                raise BrokerError('projection', 'System authority changed')
            self.projection.configure(constraints)

    def channel_environment(self):
        """Read identity/coupling under the existing owner's physical I/O lock."""
        if self.lease is None:
            raise BrokerError("closed", "transport has no channel lease")
        return self._rpc("channel_environment", instrument=self.instrument, lease=self.lease)

    def query(self, command):
        return self.transaction([dict(op="query", data=command)])[0]

    def write(self, command):
        self.transaction([dict(op="write", data=command)])

    def command(self, command):
        result = self.transaction([dict(op="write", data=command), dict(op="query", data="*OPC?")])
        if result[-1].strip() != "1":
            raise BrokerError("opc", "AWG did not acknowledge command completion")

    def write_raw(self, data):
        self.transaction([dict(op="write_raw", data=base64.b64encode(data).decode("ascii"))])

    def write_raw_and_wait(self, data):
        result = self.transaction([dict(op="write_raw", data=base64.b64encode(data).decode("ascii")),
                                   dict(op="query", data="*OPC?")])
        if result[-1].strip() != "1":
            raise BrokerError("opc", "AWG did not acknowledge waveform upload")

    def close(self):
        if self.lease is not None:
            self._rpc("release", instrument=self.instrument, lease=self.lease)
            self.lease = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class RemoteCLD1015:
    """Typed remote controller. The owning Linux service performs every USB I/O."""
    def __init__(self, config):
        self.config = config
        self.transport = None

    @property
    def io(self):
        return self.transport

    @property
    def simulated(self):
        return self.transport is not None and self.transport.simulated

    def connect(self):
        if self.transport is None:
            proxy = dict(resource=self.config.get("resource") or "auto", channel=0, profile="cld1015",
                         broker=dict(endpoint=self.config["endpoint"], instrument=self.config["instrument"]))
            self.transport = BrokerTransport(proxy, self.config["timeout_s"], owner="labscript CLD1015")
        try:
            return self.read()
        except Exception:
            self.close()
            raise

    def _call(self, method, **values):
        if self.transport is None:
            raise RuntimeError("Connect to ldcon controller first")
        t = self.transport
        return t._rpc("controller", instrument=t.instrument, lease=t.lease, method=method, **values)

    def read(self):
        return self._call("read")

    def describe(self):
        return self._call("describe")

    def validate(self, request):
        return self._call("validate", values=request)

    def apply(self, request, *, ramp_s=0):
        from ldcon.drivers.cld1015_config import check
        for key, value in request.items():
            bounds = self.config["current_limits_a" if key == "current_a" else "temperature_limits_c"]
            check(value, bounds, key)
        if self.transport is None:
            raise RuntimeError("Connect to ldcon controller first")
        previous_timeout = self.transport.timeout_s
        self.transport.timeout_s = max(previous_timeout, float(ramp_s) + previous_timeout)
        try:
            return self._call("apply", values=request, ramp_s=ramp_s)
        finally:
            self.transport.timeout_s = previous_timeout

    def enable(self, output, enabled):
        return self._call("enable", output=output, enabled=enabled)

    def validate_sweep(self, channels, waves, resolved):
        return self._call("validate_sweep", channels=[dict(role=c["role"]) for c in channels], resolved=resolved)

    def save_current_tuning(self, expected_digest, proposal, *, approval_token=None):
        """Pass the host-issued exact approval through without manufacturing authority."""
        values = dict(expected_digest=expected_digest, proposal=proposal)
        if approval_token is not None:
            values['approval_token'] = approval_token
        return self._call('save_current_tuning', **values)

    def close(self):
        if self.transport is not None:
            self.transport.close()
            self.transport = None
