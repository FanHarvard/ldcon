"""Hardware-free midpoint pilot using the real session, policy and core journal."""
import argparse
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace

from experiment_core.execution import Context, ManualDecisions
from experiment_core.ownership import Grant
from experiment_core.records import Journal

from ldcon.jobs.ecdl_window_center import WindowCenterSession
from ldcon.jobs.window_decisions import DeterministicWindowDecisions, WindowDecisions


class SimulatedLock:
    """Synthetic dimensionless axis. No transport, endpoint or live policy."""
    def __init__(self, *, one_sided=False, hop=False, stale_dither=False):
        self.t = 100.0
        self.current = .5
        self.pi_active = True
        self.uncertain_output = False
        self.one_sided, self.hop, self.stale_dither = one_sided, hop, stale_dither
        self.moves = []
        self.axis = SimpleNamespace(low=0., high=1., max_step=.02, resolution=.001,
                                    update_interval_s=.1, max_slew_per_s=1.)
        self.session = SimpleNamespace(device={"control_settings": {"steady_lock": {
            "entry_gate": {"mode_id": "branch", "branch_tolerance_hz": 1e6, "metrics_max": {}},
            "dither": {"window_s": .2, "frequency_hz": 10., "delivery_delay_s": 0.}}}})

    def clock(self):
        return self.t

    def wall_clock(self):
        return 100000. + self.t

    def recovery_ports(self):
        return {"current": SimpleNamespace(policy=self.axis)}

    def response_info(self):
        self.t += .01
        return {"outputs": {"current": self.current, "temperature": .5, "piezo": 100.},
                "native_pi_active": self.pi_active,
                "observation": {"frequency": {"frequency_hz": 1e12, "acquired_s": self.t,
                    "acquired_unix_s": self.wall_clock(), "quality": "valid"},
                    "mode_id": "other" if self.hop and self.current < .4 else "branch",
                    "single_mode": None, "metrics": {}, "maintenance_required": False}}

    def response_dither_info(self):
        outside = self.current < .3 or not self.one_sided and self.current > .7
        return {"dither_response": 2. if outside else .1, "dither_response_valid": True,
                "dither_source_unix_s": self.wall_clock(),
                "dither_source_start_unix_s": self.wall_clock() - (1000 if self.stale_dither else .1),
                "dither_sample_count": 20}

    def response_begin(self):
        pass

    def response_end(self):
        pass

    def response_current(self, value):
        assert self.pi_active and 0 <= value <= 1 and abs(value-self.current) <= .020001
        self.current = value
        self.moves.append(value)
        return {"applied": True, "value": value}


def run_midpoint(directory, *, mode="deterministic", one_sided=False, hop=False,
                 stale_dither=False, cancel_after=None, decision_provider=None):
    lock = SimulatedLock(one_sided=one_sided, hop=hop, stale_dither=stale_dither)
    journal = Journal(Path(directory) / "pilot.sqlite3")
    grant = Grant("synthetic-laser", "simulated-owner", "simulation-1", "parent-lease", "fixture-v1",
                  "midpoint", frozenset({"fixture/current"}))
    context = Context(grant, lock.t + 200, 100, clock=lock.clock)
    if decision_provider is not None:
        provider = decision_provider
    elif mode == "baseline":
        provider = None
    elif mode == "manual":
        provider = ManualDecisions([{"action": "accept", "parameters": {}} for _ in range(100)])
    elif mode == "agent":
        # Deterministic mock of the same resident port; no model login or network.
        class MockResident:
            def propose(self, measurement, capabilities, budget):
                return {"action": "accept", "parameters": {}}
        provider = MockResident()
    elif mode == "deterministic":
        provider = DeterministicWindowDecisions()
    else:
        raise ValueError("Unknown decision mode")
    events = []
    try:
        decisions = WindowDecisions(provider, journal, context) if mode != "baseline" else None
        session = WindowCenterSession({"request_id": "midpoint", "card_id": "window"}, {
            "step_fraction": .02, "max_span_fraction": .4, "max_steps": 100, "timeout_s": 180.,
            "settle_s": .1, "observables": ["dither_response"], "boundary_limits": {"dither_response": 1.},
            "require_single_mode": False,
        }, lock, {}, lambda kind, payload: events.append({"kind": kind, "payload": payload}),
            lambda value: None, {}, clock=lock.clock, wall_clock=lock.wall_clock, decisions=decisions)
        for _ in range(250):
            lock.t += 1.5
            session.poll(lock, cancel=cancel_after is not None and len(lock.moves) >= cancel_after)
            if session.status != "running" or session.needs_recovery:
                break
        return {"simulation": True, "decision_mode": mode, "status": session.status,
                "result": session.result, "parent_pi_active": lock.pi_active,
                "moves": lock.moves, "recovery_required": session.needs_recovery,
                "journal_state": journal.get("midpoint")["state"] if decisions is not None else None,
                "events": events}
    finally:
        journal.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("deterministic", "manual", "agent"), default="deterministic")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="ldcon-simulation-") as directory:
        result = run_midpoint(directory, mode=args.mode)
    result.pop("events")
    print(json.dumps(result, allow_nan=False))
    return 0 if result["status"] == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
