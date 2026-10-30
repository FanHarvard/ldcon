"""Real branch-hop card with deterministic optics/owners; no hardware sockets."""
import copy
from dataclasses import replace

import pytest

from ldcon.jobs import ecdl_branch_hop as hop
from ldcon.core.contracts import FrequencySample, DeviceError
from ldcon.owners.ecdl_prelock import AxisPolicy, ModeObservation
from ldcon.core.interface_cards import CardCancelled, INTERFACE_CATALOG
from ldcon.core.interface_schema import parameters
from ldcon.core.operation_registry import admission_class

F = 7e14


class Rig:
    def __init__(self):
        self.now = 1000.
        self.outputs = dict(current=0., piezo=0., temperature=0.)
        self.commands = []
        self.seq = 0
        self.open = True
        self.stop = False
        self.fail_write = False
        self.missing = False
        self.frequency = lambda: F + (1e11 if self.outputs['current'] <= -.03 else 0.)
        policy = AxisPolicy('V', -1., 1., .02, 1., .1, .1, None, 'test', 'rev', .001)
        rig = self

        class Axis:
            uncertain_output = False
            def __init__(self, name):
                self.name, self.policy = name, policy
            def readback(self):return rig.outputs[self.name]
            def end_recovery(self):return True
            def move(self, value, duration_s):
                assert abs(value-rig.outputs[self.name]) <= self.policy.max_step + 1e-12
                rig.commands.append((self.name,value))
                if rig.fail_write:
                    self.uncertain_output = True
                    raise RuntimeError('Unknown write outcome')
                rig.outputs[self.name] = value

        class Optics:
            def read_mode(self):
                rig.now += .01;rig.seq += 1
                value = None if rig.missing else rig.frequency()
                return ModeObservation(FrequencySample(rig.seq,value,rig.now,rig.now,'valid','independent'),
                    False, 'unclassified', True, .4, True, {}, {})

        class Loop:
            def is_open_loop(self):return rig.open

        self.ports = {k:Axis(k) for k in self.outputs}
        self.ports.update(observation=Optics(),loop=Loop())
        self.records = []

    def sleep(self, dt):self.now += dt
    def run(self, **kwargs):
        p = dict(step_size=.01,max_excursion=.1,hop_threshold_hz=1e9,
                 confirmation_span_hz=1e8,confirm_s=.2,confirmation_timeout_s=2.,
                 dwell_s=.1,sample_interval_s=.1,measurement_timeout_s=.5,timeout_s=30.)
        return hop.run_branch_hop(self.ports,{**p,**kwargs},clock=lambda:self.now,sleep=self.sleep,
            cancel=lambda:self.stop,emit=lambda *a:None,publish=lambda row:self.records.append(copy.deepcopy(row)))


def test_hop_holds_first_confirmed_branch_no_restore_or_tec():
    r=Rig();result=r.run()
    assert result['status']=='succeeded' and result['hop_confirmed']
    assert r.commands==[('current',-.01),('current',-.02),('current',-.03)]
    assert r.outputs==dict(current=-.03,piezo=0.,temperature=0.)
    assert result['held_frequency_hz']==F+1e11
    assert not any(result[k] for k in ('restored','target_changed','lock_started','adjacency_verified','recovery_permitted'))
    assert result['artifact']['confirmation'] and result['output_hold_verified']


def test_scan_increase_and_piezo_are_explicit():
    r=Rig();r.frequency=lambda:F+(1e11 if r.outputs['piezo']>=.02 else 0)
    result=r.run(axis='piezo',direction='increase')
    assert result['hop_confirmed'] and r.commands==[('piezo',.01),('piezo',.02)]


def test_smooth_cumulative_drift_and_bad_quality_are_not_hops():
    r=Rig();r.frequency=lambda:F+r.outputs['current']*2e10
    result=r.run()
    assert result['status']=='inconclusive' and not result['hop_confirmed']
    assert len(r.commands)==10 and r.outputs['current']==pytest.approx(-.1)


def test_first_suspected_hop_does_not_continue_when_confirmation_fails():
    r=Rig();r.frequency=lambda:F+(1e11 if r.outputs['current']<=-.02 and r.seq%2 else 0)
    result=r.run()
    assert result['status']=='inconclusive'
    assert len(r.commands)==2 and r.outputs['current']==-.02
    assert not result['hop_confirmed']


def test_hop_on_common_prewrite_read_stops_without_next_command():
    r=Rig();r.frequency=lambda:F+(1e11 if r.seq>=5 else 0)
    result=r.run()
    assert result['hop_confirmed'] and r.commands==[]


def test_active_pi_and_missing_baseline_cannot_move():
    r=Rig();r.open=False
    with pytest.raises(DeviceError,match='Stop the lock'):r.run()
    assert not r.commands
    r.open=True;r.missing=True
    with pytest.raises(DeviceError,match='measurement deadline'):r.run()
    assert not r.commands and r.records[-1]['status']=='interrupted'


def test_uncertain_write_never_retries_or_restores():
    r=Rig();r.fail_write=True
    with pytest.raises(RuntimeError,match='Unknown write'):r.run()
    assert len(r.commands)==1
    assert r.records[-1]['output_hold_verified'] is False


def test_cancellation_holds_current_checkpoint_without_return():
    r=Rig()
    original=r.ports['current'].move
    def move(value,duration):original(value,duration);r.stop=True
    r.ports['current'].move=move
    with pytest.raises(CardCancelled):r.run()
    assert r.commands==[('current',-.01)] and r.records[-1]['status']=='cancelled'


def test_step_and_span_are_clamped_by_owner_and_stop_at_bound():
    r=Rig();r.frequency=lambda:F
    r.ports['current'].policy=replace(r.ports['current'].policy,low=-.025,max_step=.005)
    result=r.run()
    assert len(r.commands)==5 and r.outputs['current']==-.025
    assert result['effective_step']==.005 and result['status']=='inconclusive'


def test_registry_and_parameter_validation():
    card=dict(id='branch-hop',operation='tune.ecdl_branch_hop',parameters={},uses=['current','piezo','temperature','loop','observation'])
    assert parameters(card,{})['step_size'] is None
    assert admission_class({},card)=='exclusive'
    assert card['operation'] in INTERFACE_CATALOG
    with pytest.raises(ValueError,match='required'):hop.require_configured(parameters(card,{}))
    for bad in (dict(axis='temperature'),dict(direction='random'),dict(max_steps=True),dict(confirm_samples=1),dict(target_frequency_hz=F)):
        with pytest.raises(ValueError):parameters(card,bad)


def test_proxy_never_prepares_running_pi(monkeypatch):
    from ldcon.jobs.ecdl_devices import OwnerSession
    s=OwnerSession(dict(connection=dict(endpoint='http://127.0.0.1:1')))
    actions=[]
    def rpc(action,**kwargs):
        actions.append(action)
        assert action=='inspect'
        return dict(snapshot=dict(open_loop=False),fault=None,lease_retained=False)
    monkeypatch.setattr(s,'rpc',rpc)
    with pytest.raises(DeviceError,match='stop the lock'):s.connect(allow_prepare=False)
    assert actions==['inspect']
