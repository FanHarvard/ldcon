"""A worker's follow-up trace must not erase reported degradation or a wait."""
import pytest

from ldcon.jobs.interface_worker import _controller_status_code


@pytest.mark.parametrize('state,locked,expected', [
    ('mode_degraded', True, 6),
    ('waiting_for_measurement', False, 7),
    ('failed', True, 5),
    ('locked', True, 3),
    ('tracking', False, 8),
    ('hold', False, 11),
    ('sweeping', False, 8),
])
def test_repeated_trace_keeps_the_explicit_state(state, locked, expected):
    # Real workers send a transition event followed by many measured traces.
    # Previously the first degraded trace overwrote 6 with the clean-lock 3.
    records = [('state', {'state': state}),
               ('trace', {'state': state, 'locked': locked}),
               ('trace', {'state': state, 'locked': locked})]
    assert [_controller_status_code(kind, row) for kind, row in records] == [expected]*3


def test_legacy_trace_and_unknown_state_behavior_is_preserved():
    assert _controller_status_code('trace', {'locked': True}) == 3
    assert _controller_status_code('trace', {'locked': False}) == 8
    assert _controller_status_code('trace', {}) == 8
    assert _controller_status_code('trace', {'state': 'future_state', 'locked': True}) == 3
    assert _controller_status_code('state', {'state': 'future_state'}) is None
    assert _controller_status_code('command_applied', {'state': 'locked'}) is None
