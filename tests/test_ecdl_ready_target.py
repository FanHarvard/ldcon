"""Readiness uses the intended target; no firmware command in a preflight.

Branch distance and mode labels are records, not admission criteria (live
policy 2026-10-26). The selected target shows in the readiness diagnostic.
"""
from types import SimpleNamespace
import pytest
from ldcon.owners.ecdl_steady_owner import NativeLockOwner
from ldcon.core.contracts import DeviceError

F=711073730000000.

def owner(saved=True):
    state=dict(open_loop=True,servo_config={'target':299792458e9/(F-110e9)})
    obs=dict(frequency={'frequency_hz':F},mode_id='desired',single_mode=False)
    hardware=SimpleNamespace(snapshot=lambda:state)
    o=SimpleNamespace(clock=lambda:0.,laser={'target':{'frequency_hz':F}} if saved else {},lease=None,
                      device={'control_settings':{}},policy_authority=SimpleNamespace(revision=None),
                      hw=lambda:hardware,observation=lambda:obs,policies=lambda:{},validate_state=lambda *a:None)
    n=NativeLockOwner(o)
    n.profile=lambda _:dict(entry_gate={'mode_id':'desired','branch_tolerance_hz':700e6},revision='test')
    n.native=lambda *a,**kw:None
    return n,state,obs

def test_saved_target_is_used_without_changing_firmware():
    n,state,obs=owner();old=state['servo_config'].copy()
    assert n.dispatch({'action':'steady_ready','strategy':'hold'})['ready']
    obs['frequency']['frequency_hz']=None
    with pytest.raises(DeviceError,match='no valid frequency/target .measured None THz, target 711.073730000'):
        n.dispatch({'action':'steady_ready','strategy':'hold'})
    assert state['servo_config']==old

def test_explicit_target_overrides_saved_and_branch_labels_do_not_refuse():
    n,state,obs=owner();old=state['servo_config'].copy()
    assert n.dispatch({'action':'steady_ready','strategy':'hold','target_frequency_hz':F-110e9})['ready']
    obs['mode_id']='unclassified'
    assert n.dispatch({'action':'steady_ready','strategy':'hold'})['ready']
    obs['frequency']['frequency_hz']=None
    with pytest.raises(DeviceError,match='target 710.963730000 THz, mode unclassified'):
        n.dispatch({'action':'steady_ready','strategy':'hold','target_frequency_hz':F-110e9})
    assert state['servo_config']==old

def test_old_definitions_without_target_keep_legacy_firmware_reference():
    n,_,obs=owner(False)
    obs['frequency']['frequency_hz']=None
    with pytest.raises(DeviceError,match='target 710.963730000'):
        n.dispatch({'action':'steady_ready','strategy':'hold'})
