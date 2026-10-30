"""Opt-in typed SDG channel adapter; never opens a physical instrument."""
import copy

from ldcon.core.contracts import DeviceError, MeasurementUnavailable
from ldcon.core.instrument_client import BrokerError, BrokerTransport, validate_endpoint
from ldcon.owners.sdg_owner import PROFILE, policy_digest


class SDGPrewriteUnavailable(MeasurementUnavailable):
    """Authoritative rejection before the independent owner sent any mutation."""


def axis_policy(device, axis):
    absolute = device["absolute_conditions"]["axes"][axis]
    control = device["control_settings"]["axes"][axis]
    step = absolute.get('max_step')
    if axis == 'temperature' and step is None:
        from ldcon.owners.tec_rate import delivery_step
        step = delivery_step(absolute['max_slew_per_s'], control['update_interval_s'])
    return dict(bounds_v=absolute["bounds"], activation_range_v=absolute["activation_range"],
                max_step_v=step, max_slew_v_per_s=absolute["max_slew_per_s"],
                update_interval_s=control["update_interval_s"],
                readback_tolerance_v=control["readback_tolerance"], revision=control["revision"])


class RemoteOffsetAWG:
    """Lease exactly one channel, and read both channels from the owner cache."""
    _rpc = BrokerTransport._rpc

    def __init__(self, resource, policy, *, owner="ECDL rig"):
        remote = resource["owner"]
        if set(remote) != {"endpoint", "device_id", "identity"}:
            raise ValueError("SDG owner endpoint/device_id/identity must be explicit")
        self.endpoint = validate_endpoint(remote["endpoint"])
        self.device_id = remote["device_id"]
        self.channel = resource["channel"]
        self.timeout_s = 20.
        self.lease = None
        self.uncertain_output = False
        self.previous = None
        description = self._call("describe")
        if (description["host"] != resource["host"] or description["profile"] != PROFILE
                or description["identity"] != remote["identity"]):
            raise DeviceError("Independent SDG endpoint does not match the assigned physical resource")
        self.identity = ",".join(remote["identity"][key]
                                 for key in ("manufacturer", "model", "serial", "firmware"))
        # Claim failures are never retried; a lost reply can leave a retained lease.
        result = self._call("claim", channel=self.channel, identity=remote["identity"],
                            policy_digest=policy_digest(policy), owner=owner)
        self.lease = result["lease"]

    def _call(self, action, **values):
        return self._rpc(action, device_id=self.device_id, **values)

    def describe(self):
        return self.identity

    def read(self, channel):
        if type(channel) is not int or channel not in (1, 2):
            raise DeviceError("Unknown SDG channel")
        result = self._call("status")
        if result["fault"]:
            self.uncertain_output = True
            raise DeviceError("Independent SDG owner fault: " + result["fault"])
        if not result["fresh"]:
            raise MeasurementUnavailable("Independent SDG cache is stale")
        state = copy.deepcopy(result["sample"]["channels"][str(channel)])
        if channel == self.channel:
            self.previous = state["value"]
        return state

    def offset(self, channel, value):
        if channel != self.channel or self.lease is None or self.uncertain_output:
            raise DeviceError("SDG channel lease is unavailable or uncertain")
        if self.previous is None:
            raise SDGPrewriteUnavailable("Read a fresh SDG offset before proposing a move")
        self.uncertain_output = True
        try:
            result = self._call("offset", lease=self.lease, value=value, expected_v=self.previous)
        except BrokerError as exc:
            if exc.code == "measurement":
                # Only the owner guarantees these failures happened before write intent.
                self.uncertain_output = False
                raise SDGPrewriteUnavailable(str(exc)) from exc
            if exc.code in ("range", "configuration", "request", "action"):
                self.uncertain_output = False
            raise DeviceError(str(exc)) from exc
        except Exception as exc:
            raise DeviceError("Uncertain SDG RPC result; do not retry: " + str(exc)) from exc
        self.previous = result["value"]
        self.uncertain_output = False

    def close(self):
        if self.lease is not None and not self.uncertain_output:
            self._call("release", lease=self.lease)
            self.lease = None

    # ---- owner restart with a running native lock (design §5, 2026-10-27) ----------------------
    def detach(self):
        """Keep the channel lease for the next rig owner process; no RPC."""

    @classmethod
    def adopt(cls, resource, lease, *, owner="ECDL rig"):
        """Re-attach to the lease a previous rig owner retained; read-only (describe + cached status)."""
        self = cls.__new__(cls)
        remote = resource["owner"]
        self.endpoint = validate_endpoint(remote["endpoint"])
        self.device_id = remote["device_id"]
        self.channel = resource["channel"]
        self.timeout_s = 20.
        self.lease = str(lease)
        self.uncertain_output = False
        self.previous = None
        description = self._call("describe")
        if (description["host"] != resource["host"] or description["profile"] != PROFILE
                or description["identity"] != remote["identity"]):
            raise DeviceError("Independent SDG endpoint does not match the assigned physical resource")
        if not any(entry.get("channel") == self.channel and entry.get("owner") == owner for entry in description.get("leases", [])):
            raise DeviceError("Retained SDG lease for CH%d is not listed by its owner" % self.channel)
        self.identity = ",".join(remote["identity"][key] for key in ("manufacturer", "model", "serial", "firmware"))
        return self

