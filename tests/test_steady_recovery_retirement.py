"""Retired controls never silently survive admission or touch a live owner."""
from unittest.mock import Mock

import pytest

from ldcon.jobs import ecdl_steady
from ldcon.jobs.card_runtime import CardRuntime
from .test_recovery_reference_v2 import config

CARD=dict(id='steady',operation='servo.ecdl_steady_lock',uses=['lock'],parameters={})


@pytest.mark.parametrize('key',sorted(ecdl_steady.RETIRED_RECOVERY_PARAMETERS))
@pytest.mark.parametrize('value',[None,{}])
def test_retired_controls_reject_explicit_values_in_defaults_overrides_and_execution(key,value,tmp_path):
    assert key not in ecdl_steady.DEFAULTS
    with pytest.raises(ValueError,match='Legacy recovery parameters retired'):
        ecdl_steady.parameters({**CARD,'parameters':{key:value}},{})
    with pytest.raises(ValueError,match='Legacy recovery parameters retired'):
        ecdl_steady.parameters(CARD,{key:value})
    p=ecdl_steady.parameters(CARD,dict(target_frequency_hz=1000.,tolerance_hz=5.))
    lock=Mock()
    with pytest.raises(ValueError,match='Legacy recovery parameters retired'):
        ecdl_steady.run_steady_lock(lock,{**p,key:value},clock=Mock(),sleep=Mock(),cancel=Mock(),emit=Mock())
    assert not lock.mock_calls
    with pytest.raises(ValueError,match='Legacy recovery parameters retired'):
        CardRuntime(tmp_path,CARD,{**p,key:value},Mock(),lock=lock)
    assert not lock.mock_calls


def test_v2_runtime_has_target_updates_and_no_retired_controls(tmp_path):
    p=ecdl_steady.parameters(CARD,dict(target_frequency_hz=1000.,tolerance_hz=5.,recovery=config()))
    lock=Mock(runtime_owner_parameters=True)
    lock.parameters_status.return_value=dict(revision=1,parameters=dict(tec_gain=.1,tec_ff_paused=False,current_ff_enabled=True))
    runtime=CardRuntime(tmp_path,CARD,p,Mock(),lock=lock)
    try:
        assert {'target_frequency_hz','tolerance_hz'} <= runtime.specs.keys()
        assert not ecdl_steady.RETIRED_RECOVERY_PARAMETERS.intersection(runtime.specs)
        assert runtime.validate({**runtime.hot,'target_frequency_hz':1010.})['target_frequency_hz']==1010.
        lock.update_parameters.assert_not_called()
    finally:runtime.close()
