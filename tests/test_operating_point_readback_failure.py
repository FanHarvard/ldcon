"""2026-10-27 22:28 (sr422 lock 8961e623): a locked reference move whose first readback raised reported
"cannot access local variable 'previous'" instead of the real error. The real error must propagate."""
import pytest
from ldcon.core.contracts import DeviceError
from ldcon.jobs import operating_point


def test_readback_failure_before_first_write_propagates(monkeypatch):
    src = open(operating_point.__file__).read()
    assert "previous = None" in src and "operating_point_end_reference_failed" in src
    # the finally block no longer references an unbound name
    tail = src.split("mover = self.a.move_output if kind == 'output' else self.a.step_reference")[1].split('def readback_period')[0]
    assert 'previous if previous is not None else self.readback(axis)' in tail
