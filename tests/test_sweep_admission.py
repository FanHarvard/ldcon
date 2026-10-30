"""Canonical Web admission uses the same native record boundary as its worker."""
from types import SimpleNamespace

import pytest

from ldcon.jobs.modulation_sweep import validate_request
from ldcon.owners.native_ff_proof import duration_s, plan_parameters
from ldcon.jobs.native_ff_proxy import owner_plan, plan_from_card
from ldcon.web.sweep_api import shot_request


def route():
    return SimpleNamespace(kind="native", role="current_modulation",
                           defaults={"settle_fraction": .3, "acquire_fraction": .5})


def test_native_shot_preflight_preserves_approach_ramp_without_sending_host_annotations():
    levels = [float(value) for value in range(-600_000_000, 1, 10_000_000)]
    sequence = dict(levels=levels, dwell_s=.1, lead_s=.51, tail_s=.05)
    shot = shot_request(route(), sequence, "software", "bounded-ramp")
    requested = validate_request(shot)
    physical = plan_from_card({"edge_s": .01}, requested)
    assert physical["ramp_levels"] == {"lead": 5, "tail": 0, "step_hz": 100e6}
    proven = plan_parameters(owner_plan(physical))
    assert "ramp_levels" not in proven
    assert proven["levels"][5:] == levels
    assert duration_s(proven) == pytest.approx(.51 + len(levels) * .1 + .05)
    assert requested["lead_s"] == .51 and requested["tail_s"] == .05


def test_explicit_nonuniform_levels_keep_exact_offsets_under_native_divisor_normalization():
    sequence = dict(levels=[0., 30e6, 50e6], dwell_s=.1, lead_s=.05, tail_s=.05)
    shot = shot_request(route(), sequence, "software", "nonuniform-levels")
    requested = validate_request(shot)
    proven = plan_parameters(owner_plan(plan_from_card({"edge_s": .01}, requested)))
    assert requested["levels"] == proven["levels"] == sequence["levels"]
    assert proven["half_span_hz"] / proven["step_hz"] == 1
