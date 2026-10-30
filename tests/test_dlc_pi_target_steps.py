"""Synthetic owner/driver boundary for attached target-response programs."""
import pytest
from ldcon.core.interface_schema import digest
from ldcon.core.contracts import MeasurementUnavailable
from ldcon.drivers.dlcpro import UncertainDLCWrite
from ldcon.jobs.dlcpro_cards import Protocol
from .test_dlcpro_offline import rig


def begin(rig):
    o=rig['owner'];o.start_lock(3e14,rig['control'])
    protocol=Protocol(o,rig['device']);protocol.client='controller'
    def call(action,**kw):return protocol.dispatch(dict(action=action,laser_id=o.laser_id,**kw))
    r=call('pi_trial_begin',device_revision=digest(rig['device']),identity='response',
        target_revision=o.target_revision,policy_revision=digest(o.policy),watchdog_s=5,
        target_program=dict(offsets_hz=[1e6,0,-1e6,0],max_cycles=2))
    return o,call,r['token']


def step(o,call,token,index):
    return call('pi_trial_target_step',token=token,expected_revision=o.target_revision,index=index,cycle=0)


def test_verified_step_uses_ff_and_new_acquisition_boundary(rig):
    o,call,token=begin(rig);before=rig['driver'].state['piezo']
    r=step(o,call,token,0)
    assert r['delta_frequency_hz']==1e6
    assert r['output_after_v']==pytest.approx(before+.01)
    assert o.target==3e14+1e6 and o.last_acquisition==r['acquisition_fence_s']
    assert o.last_pi_write==r['confirmed_s']
    writes=len(rig['driver'].writes)
    with pytest.raises(ValueError,match='no replay'):step(o,call,token,0)
    assert len(rig['driver'].writes)==writes


def test_foreground_restoration_after_ff_preserves_new_output(rig):
    o,call,token=begin(rig)
    call('pi_trial_apply',token=token,expected_revision=o.pi_trial.revision,
         candidate=dict(kp=.7,ki_per_s=.2,integral_limit_v=.2))
    step(o,call,token,0);before=rig['driver'].state['piezo']
    o.pi_trial.preempt('foreground')
    assert o.base+o.control['kp']*o.applied_error_axis+o.integral==pytest.approx(before)
    assert o.target==3e14+1e6 and o.pi_trial.last['target_restored'] is False
    rig['clock'].sleep(1)
    o.set_target(3e14+2e6,o.target_revision)
    assert o.target==3e14+2e6
    with pytest.raises(ValueError):step(o,call,token,1)
    assert o.target==3e14+2e6


def test_prewrite_cadence_rejection_does_not_advance_program(rig):
    o,call,token=begin(rig);step(o,call,token,0)
    before=len(rig['driver'].writes);target=o.target
    with pytest.raises(MeasurementUnavailable):step(o,call,token,1)
    assert o.target==target and o.pi_trial.active['program_index']==1
    assert len(rig['driver'].writes)==before and not o.fault


def test_uncertain_ff_write_keeps_fault_and_never_replays(rig):
    o,call,token=begin(rig);rig['driver'].fail=True
    with pytest.raises(UncertainDLCWrite):step(o,call,token,0)
    assert o.fault and o.target==3e14
    before=len(rig['driver'].writes)
    with pytest.raises(Exception):step(o,call,token,0)
    assert len(rig['driver'].writes)==before


def test_receipt_and_coefficient_cleanup_need_no_fresh_optics(rig):
    o,call,token=begin(rig)
    call('pi_trial_apply',token=token,expected_revision=o.pi_trial.revision,
         candidate=dict(kp=.7,ki_per_s=.2,integral_limit_v=.2))
    rig['source'].missing=True
    r=call('pi_trial_receipt');assert r['trial']['active']['identity']=='response'
    r=call('pi_trial_finish',token=token,adopt=False)
    assert r['confirmed'] and r['target_restored']
    assert o.control['kp']==rig['control']['kp']


def test_new_capture_loss_during_console_read_rejects_ff(rig,monkeypatch):
    o,call,token=begin(rig);before=len(rig['driver'].writes)
    original=rig['instrument'].snapshot
    def moved(*args):
        result=original(*args);rig['source'].bias=2*o.control['capture_hz'];return result
    monkeypatch.setattr(rig['instrument'],'snapshot',moved)
    with pytest.raises(MeasurementUnavailable,match='left capture'):step(o,call,token,0)
    assert o.target==3e14 and len(rig['driver'].writes)==before
