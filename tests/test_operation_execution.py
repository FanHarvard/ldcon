import json
from types import SimpleNamespace

import pytest
import yaml

from experiment_core.errors import Conflict, UncertainWrite
from experiment_core.records import Journal
from ldcon.jobs import interface_worker
from ldcon.jobs.operation_execution import RunExecution, frozen_inputs


def job(directory):
    (directory / "laser.yaml").write_text(yaml.safe_dump({"id": "fixture", "name": "Fixture"}))
    (directory / "interface-deck.yaml").write_text(yaml.safe_dump({"id": "fixture", "cards": [
        {"id": "observe", "operation": "monitor.wavelength", "uses": ["wavelength"], "parameters": {}}]}))
    (directory / "invocation.json").write_text(json.dumps({"card_id": "observe", "parameters": {"duration_s": 1.}}))
    return directory


def test_finished_worker_reentry_reads_same_result_without_reopening_ports(tmp_path, monkeypatch):
    job(tmp_path)
    opened = []
    monkeypatch.setattr(interface_worker, "open_interfaces", lambda *args: opened.append(True) or {
        "wavelength": SimpleNamespace(close=lambda: None)})
    monkeypatch.setattr(interface_worker, "monitor_wavelength", lambda *args, **kwargs: {"status": "succeeded"})
    first = interface_worker.run(tmp_path)
    assert first["operation_outcome"]["execution_state"] == "succeeded"
    assert first["operation_outcome"]["physical_stop"]["state"] == "unverified"
    assert interface_worker.run(tmp_path) == first
    assert opened == [True]
    invocation = json.loads((tmp_path / "invocation.json").read_text())
    invocation["parameters"]["duration_s"] = 2.
    (tmp_path / "invocation.json").write_text(json.dumps(invocation))
    with pytest.raises(Conflict):
        interface_worker.run(tmp_path)
    assert opened == [True]


def test_unfinished_worker_intent_stays_unknown_and_is_never_replayed(tmp_path):
    job(tmp_path)
    with RunExecution(tmp_path, frozen_inputs(tmp_path)) as execution:
        execution.begin()
        execution.dispatching()
        request_id = execution.request_id
    with pytest.raises(UncertainWrite):
        with RunExecution(tmp_path, frozen_inputs(tmp_path)) as reopened:
            reopened.begin()
    journal = Journal(tmp_path / "operation.sqlite3")
    try:
        assert journal.get(request_id)["state"] == "running"
    finally:
        journal.close()


def test_unknown_output_cannot_be_reported_as_success_or_replayed(tmp_path, monkeypatch):
    job(tmp_path)
    monkeypatch.setattr(interface_worker, "open_interfaces", lambda *args: {"wavelength": SimpleNamespace(
        uncertain_output=True, review_details=lambda: {"lease": "retained"}, close=lambda: None)})
    monkeypatch.setattr(interface_worker, "monitor_wavelength", lambda *args, **kwargs: {"status": "succeeded"})
    summary = interface_worker.run(tmp_path)
    assert summary["status"] == "failed"
    assert summary["operation_outcome"]["execution_state"] == "uncertain"
    assert summary["operation_outcome"]["procedure_status"] == "succeeded"
    progress = json.loads((tmp_path / "progress.json").read_text())
    assert progress["status"] == "failed" and progress["operation_outcome"] == summary["operation_outcome"]
    with pytest.raises(UncertainWrite):
        interface_worker.run(tmp_path)


def test_prelaunch_cancel_does_not_open_or_claim_any_interface(tmp_path, monkeypatch):
    job(tmp_path)
    (tmp_path / "cancel.request").touch()
    def forbidden(*args):
        raise AssertionError("Cancelled operation must not open interfaces")
    monkeypatch.setattr(interface_worker, "open_interfaces", forbidden)
    summary = interface_worker.run(tmp_path)
    assert summary["status"] == "cancelled"
    assert summary["operation_outcome"]["effects"] == "none"


def test_mismatched_frozen_contract_refuses_before_worker_admission(tmp_path):
    job(tmp_path)
    (tmp_path / "job.json").write_text(json.dumps({"operation_contract": {
        "schema": "ldcon.operation/1", "operation_id": "servo.native_current", "version": "1"}}))
    with pytest.raises(Conflict, match="Frozen operation contract"):
        interface_worker.run(tmp_path)
    assert not (tmp_path / "operation.sqlite3").exists()
