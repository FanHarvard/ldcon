import pytest

from ldcon.jobs.simulation import run_midpoint


@pytest.mark.parametrize("mode", ["deterministic", "manual", "agent"])
def test_modes_use_same_measurements_owner_and_window_policy(tmp_path, mode):
    result = run_midpoint(tmp_path, mode=mode)
    assert result["status"] == "succeeded"
    assert result["parent_pi_active"] is True
    assert result["result"]["center"] == pytest.approx(.49)
    assert set(result["result"]["boundaries"]) == {"low", "high"}
    assert len(result["moves"]) == 43
    assert result["journal_state"] == "succeeded"


def test_dither_alone_can_bound_window_with_empty_optical_quality(tmp_path):
    result = run_midpoint(tmp_path)
    observations = [e["payload"] for e in result["events"] if e["kind"] == "window_center_observation"]
    assert observations
    assert all(o["observation"]["single_mode"] is None for o in observations)
    assert all(o["observation"]["metrics"] == {} for o in observations)
    assert all(o["dither_acquisition_status"] == "valid" for o in observations)
    assert result["status"] == "succeeded"


@pytest.mark.parametrize("scenario", ["one_sided", "hop", "stale_dither"])
def test_missing_or_different_branch_evidence_never_certifies_center(tmp_path, scenario):
    result = run_midpoint(tmp_path, **{scenario: True})
    assert result["result"].get("center") is None
    assert result["status"] != "succeeded"
    assert result["parent_pi_active"] is True
    if scenario == "stale_dither":
        assert result["moves"] == []


def test_search_only_cancel_preserves_parent_and_reports_return(tmp_path):
    result = run_midpoint(tmp_path, cancel_after=4)
    assert result["status"] == "cancelled"
    assert result["parent_pi_active"] is True
    assert result["result"]["return_outcome"]["status"] == "verified"
    assert result["journal_state"] == "cancelled"


def test_decision_adapter_matches_existing_executor_trajectory(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    baseline = run_midpoint(baseline_dir, mode="baseline")
    for mode in ("deterministic", "manual", "agent"):
        directory = tmp_path / mode
        directory.mkdir()
        result = run_midpoint(directory, mode=mode)
        assert result["moves"] == baseline["moves"]
        assert result["result"] == baseline["result"]
        assert result["events"] == baseline["events"]


def test_provider_disconnect_expires_only_search_without_writing(tmp_path):
    class Disconnected:
        def propose(self, *args):
            raise ConnectionError("resident disconnected")
    result = run_midpoint(tmp_path, decision_provider=Disconnected())
    assert result["status"] == "failed" and result["journal_state"] == "failed"
    assert result["parent_pi_active"] and result["moves"] == []


def test_existing_session_cannot_be_reattached_and_replayed(tmp_path):
    from experiment_core.errors import UncertainWrite
    run_midpoint(tmp_path)
    with pytest.raises(UncertainWrite, match="existing window session"):
        run_midpoint(tmp_path)

