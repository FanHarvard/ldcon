"""Optional decision boundary around the existing bounded window executor.

Trusted hosts attach a child Context from the parent lock. The existing policy
proposes safe points, the provider may accept/repeat/finish/cancel, and the same
owner path performs the move. Providers never receive the lock or its grant.
"""
import copy
import hashlib

from experiment_core.errors import PrewriteRefusal, UncertainWrite
from experiment_core.execution import Executor, Operation, Registry, decide

from ldcon.core.contracts import DeviceError, MeasurementUnavailable
from ldcon.core.instrument_client import BrokerError


class DeterministicWindowDecisions:
    def propose(self, measurement, capabilities, context):
        return {"action": "accept", "parameters": {}}


class WindowDecisions:
    def __init__(self, provider, journal, context):
        self.provider = provider
        self.journal = journal
        self.context = context
        intent={"kind": "window_decisions", "lease_id": context.grant.lease_id,
                "generation": context.grant.generation, "revision": context.grant.policy_revision}
        if not journal.admit(context.grant.request_id, context.grant.system_id, intent):
            raise UncertainWrite("An existing window session must be inspected before attaching a new provider")
        journal.transition(context.grant.request_id, 0, "running", {"kind": "window_attached"})
        self.ordinal = 0
        self._move = None
        self._session = None
        self._lock = None
        self.registry = Registry()
        self.registry.register(Operation(
            "ldcon.window.current", "1",
            {"type": "object", "properties": {"value": {"type": "number"}},
             "required": ["value"], "additionalProperties": False},
            {"type": "object", "properties": {"applied": {"type": "boolean"}, "value": {"type": "number"}},
             "required": ["applied"]},
            context.grant.resources,
            lambda owner, child, parameters: owner._apply(parameters["value"]),
            lambda owner, child: {"confirmed": True, "disposition": "delegated_to_window_cleanup"} ))

    def choose(self, action, row):
        empty = {"type": "object", "additionalProperties": False}
        result = decide(self.provider, {"point": row, "proposed": action},
                        {key: empty for key in ("accept", "repeat", "finish", "cancel")}, self.context)
        if result.get("state") == "waiting_for_decision":
            return "repeat"
        return result["action"]

    def finish(self, result):
        """Persist the existing session's verified outcome, including cleanup."""
        record = self.journal.get(self.context.grant.request_id)
        status = result["status"]
        state = {"unknown": "uncertain", "inconclusive": "failed"}.get(status, status)
        if record["state"] != "running":
            if record["result"] != result:
                raise UncertainWrite("Window outcome changed after finalization")
            return
        self.journal.transition(record["id"], record["sequence"], state,
                                {"kind": "window_finished", "status": status}, copy.deepcopy(result))

    def move(self, session, lock, move, value):
        self._session, self._lock, self._move = session, lock, move
        self.ordinal += 1
        request_id = hashlib.sha256(self.context.grant.request_id.encode()).hexdigest()[:32] + ".point." + str(self.ordinal)
        child = self.context.child(request_id, self.context.grant.resources,
                                   steps=1, deadline=self.context.deadline)
        result = Executor(self.registry, self.journal, self).execute(
            child, "ldcon.window.current", "1", {"value": value})
        if result["state"] == "uncertain":
            raise DeviceError("Window move outcome requires reconciliation; automatic replay forbidden")
        if result["state"] != "succeeded":
            raise MeasurementUnavailable(result["result"]["error"]["message"])
        return result["result"]["value"]

    def validate(self, grant, operation, parameters):
        self.context.check()
        if self._session.write_unknown or self._lock.uncertain_output:
            raise UncertainWrite("Parent lock output is uncertain")
        # The existing physical owner revalidates its authenticated lease,
        # policy, current output and exact step in response_current().

    def validate_cancel(self, grant, operation):
        # Cancellation is performed by WindowCenterSession's existing scoped
        # owner cleanup. It must not be dispatched as a new child move.
        raise PrewriteRefusal("Cancel through the attached WindowCenterSession")

    def _apply(self, value):
        try:
            return copy.deepcopy(self._move(self._lock, value))
        except (MeasurementUnavailable, BrokerError) as error:
            if self._session.write_unknown or self._lock.uncertain_output:
                raise UncertainWrite(str(error)) from error
            if isinstance(error, MeasurementUnavailable) or error.code == "range":
                raise PrewriteRefusal(str(error)) from error
            raise

    def quarantine(self, grant):
        self._session.write_unknown = True

