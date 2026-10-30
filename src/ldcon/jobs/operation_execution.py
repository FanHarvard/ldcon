"""Worker-side operation records, independent of presentation lifetimes.

These records establish execution identity, never hardware write authority.
Instrument owners still validate every physical action and its projection.
"""
from copy import deepcopy
import hashlib
from pathlib import Path

from experiment_core.errors import Conflict, UncertainWrite
from experiment_core.records import Journal, digest

from ldcon.core.interface_schema import OPERATION_ALIASES, load_mapping
from ldcon.core.records import read_json


def frozen_inputs(directory):
    directory = Path(directory)
    laser_text = (directory / "laser.yaml").read_text()
    deck_text = (directory / "interface-deck.yaml").read_text()
    laser, deck = load_mapping(laser_text), load_mapping(deck_text)
    for card in deck["cards"]:
        card["operation"] = OPERATION_ALIASES.get(card["operation"], card["operation"])
    invocation = read_json(directory / "invocation.json")
    if not isinstance(invocation, dict):
        raise ValueError("A frozen invocation is required")
    card = next((value for value in deck["cards"] if value["id"] == invocation["card_id"]), None)
    if card is None:
        raise ValueError("Invocation does not identify a registered card in this deck")
    return dict(laser=laser, deck=deck, card=card, invocation=invocation,
                laser_file_sha256=hashlib.sha256(laser_text.encode()).hexdigest(),
                deck_file_sha256=hashlib.sha256(deck_text.encode()).hexdigest())


class RunExecution:
    """One durable execution per frozen job, including direct library callers."""

    def __init__(self, directory, frozen):
        from ldcon.core.operation_registry import spec
        self.directory = Path(directory)
        self.frozen = deepcopy(frozen)
        self.operation = spec(frozen["card"]["operation"])
        self.operation_id = frozen["card"]["operation"]
        self.version = self.operation.version
        self.meta = read_json(self.directory / "job.json", {})
        self.job_id = self.meta.get("id", self.directory.name)
        self.request_id = "run." + digest(self.job_id)
        contract = self.meta.get("operation_contract")
        if contract is not None and (contract.get("schema") != "ldcon.operation/1" or
                                     contract.get("operation_id") != self.operation_id or
                                     contract.get("version") != self.version):
            raise Conflict("Frozen operation contract differs from the registered worker operation")
        self.intent = {"operation_id": self.operation_id, "version": self.version,
                       "job_id": self.job_id, "invocation": frozen["invocation"],
                       "laser_file_sha256": frozen["laser_file_sha256"],
                       "deck_file_sha256": frozen["deck_file_sha256"]}
        self.journal = Journal(self.directory / "operation.sqlite3")
        self.guard = None
        self.fresh = False
        self.may_have_effects = False
        self.interrupted = False

    def __enter__(self):
        self.guard = self.journal.guard("worker", self.request_id)
        try:
            self.guard.__enter__()
        except BaseException:
            self.journal.close()
            raise
        return self

    def begin(self):
        fresh = self.journal.admit(self.request_id, self.frozen["laser"]["id"], self.intent)
        self.fresh = fresh
        record = self.journal.get(self.request_id)
        if not fresh:
            if record["state"] in ("succeeded", "failed", "cancelled", "inconclusive"):
                return deepcopy(record["result"]["summary"])
            raise UncertainWrite("Worker intent already exists; inspect owners and the original run, never replay")
        self.journal.transition(self.request_id, 0, "running", {"kind": "worker_started"})
        return None

    def dispatching(self):
        # Opening an interface may claim resources or perform bounded preparation.
        self.may_have_effects = True

    def finish(self, summary, *, review=None):
        summary = deepcopy(summary)
        unknown = bool(review) or self.interrupted or summary["status"] in ("unknown", "review_required")
        state = "uncertain" if unknown else {
            "succeeded": "succeeded", "completed": "succeeded", "cancelled": "cancelled",
            "inconclusive": "inconclusive", "completed_unsettled": "inconclusive",
        }.get(summary["status"], "failed")
        outcome = dict(schema="ldcon.operation-outcome/1", request_id=self.request_id,
                       job_id=self.job_id, laser_id=self.frozen["laser"]["id"],
                       operation_id=self.operation_id, version=self.version,
                       execution_state=state, procedure_status=summary["status"],
                       effects="unknown" if unknown else "reported" if self.may_have_effects else "none",
                       physical_stop={"state": "unverified", "reason": "Fresh independent owner evidence required"},
                       configured_policy_revision=self.frozen["laser_file_sha256"],
                       effective_policy_revision=None)
        if review:
            outcome["review"] = deepcopy(review)
        if unknown:
            summary["status"] = "failed"
        summary["operation_outcome"] = outcome
        record = self.journal.get(self.request_id)
        if record["state"] != "running":
            raise Conflict("Only the admitted running worker can publish its outcome")
        self.journal.transition(self.request_id, record["sequence"], state,
                                {"kind": "worker_finished", "outcome": outcome}, {"summary": summary})
        return summary

    def __exit__(self, kind, error, traceback):
        try:
            if error is not None and self.fresh:
                try:
                    record = self.journal.get(self.request_id)
                except KeyError:
                    record = None
                if record is not None and record["state"] == "running":
                    self.interrupted = self.may_have_effects
                    self.finish({"status": "failed", "message": type(error).__name__ + ": " + str(error)})
        finally:
            self.guard.__exit__(kind, error, traceback)
            self.journal.close()
