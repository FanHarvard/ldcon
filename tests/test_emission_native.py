"""Native PI cleanup and held-DAC proof before electrical ON; no sockets."""
import importlib.util
from pathlib import Path
import pytest
from ldcon.core.instrument_client import BrokerError

spec=importlib.util.spec_from_file_location('emission_native_fixture',(Path(__file__).resolve().parent / 'support')/'jobs/tests/test_esp32_native_current.py')
fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)
native_rig=fixture.rig


def test_native_quiescence_requires_real_pi_and_both_leases_released(native_rig):
    owner,servo,clock,bias,optical,gate,call,ready=native_rig
    ready();call('arm')
    emission=owner.system_owner.emission
    emission.store.reserve('off','off','synthetic')
    assert call('status')['emission']['phase']=='quiescing'
    with pytest.raises(BrokerError,match='inhibit'):
        call('retarget',target_wavelength_nm=1091.78)
    with pytest.raises(BrokerError,match='cleanup'):
        emission.adapter.quiet({})
    call('stop');call('release')
    assert owner.lease is None and bias.lease is None and servo.cfg['pid_multiplier']==0
    emission.adapter.quiet({})
    assert not owner.fault
    # ON validates the physical held-DAC envelope before allowing LD enable.
    p={'current_bounds_a':[0.,.1]}
    before={'tec_enabled':True,'current_setpoint_a':.05}
    emission.adapter.validate_transition(p,'on',before)
    with pytest.raises(BrokerError,match='bias differs'):
        emission.adapter.validate_transition(p,'on',{**before,'current_setpoint_a':.06})
    servo.cfg['control_offset']=65530
    with pytest.raises(BrokerError,match='current range|absolute limits'):
        emission.adapter.validate_transition(p,'on',before)
