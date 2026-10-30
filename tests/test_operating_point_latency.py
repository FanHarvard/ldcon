"""Rule L (2026-10-26/27): operating-point moves cost one RPC per axis where the owner offers the whole move, no optical
observation per step unless opted in, completion judged on the owner's readback, and the result reports the timing."""
import copy
import importlib.util
import math
from pathlib import Path
from types import SimpleNamespace

import pytest

from ldcon.jobs import operating_point as op
from ldcon.core.contracts import DeviceError, MeasurementUnavailable
from ldcon.core.interface_cards import CardCancelled
from ldcon.core.instrument_client import BrokerError

_spec = importlib.util.spec_from_file_location('native_fixture',
    (Path(__file__).resolve().parent / 'support') / 'jobs/tests/test_esp32_native_current.py')
_fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixture)

ECDL_LASER = dict(id='sr422', interfaces=dict(lock=dict(device='rig', capability='ecdl_stationary_lock'), observation=dict(device='rig')),
                  devices=dict(rig=dict(type='ecdl.rig.owner', connection={}, absolute_conditions={}, control_settings={})))
NATIVE_LASER = dict(id='sr1092', interfaces=dict(native_current=dict(device='native_esp', capability='native_current_lock')),
                    devices=dict(native_esp=dict(type='esp32servocon.native_current.owner', connection={}, absolute_conditions={}, control_settings={})))
F0 = 7.1e14


def axis_policy(**kw):
    base = dict(low=0., high=1., max_step=.002, max_slew_per_s=.02, update_interval_s=1., resolution=1e-4, unit='V')
    base.update(kw)
    return SimpleNamespace(**base)


class FakeAdapter(op.Adapter):
    """An open-loop owner that only offers one bounded step per RPC (the host fallback loop)."""
    locked = False

    def __init__(self, clock, *, modes=()):
        self.clock = clock
        self.out = dict(current=.5, piezo=100.)
        self.calls = []
        self.modes = list(modes)
        self.unavailable = False

    def policy(self, axis):
        return axis_policy() if axis == 'current' else axis_policy(low=0., high=1000., max_step=50., max_slew_per_s=500., resolution=1., unit='count')

    def readback(self, axis):
        self.calls.append(('readback', axis))
        return self.out[axis]

    def observe(self):
        self.calls.append(('observe',))
        if self.unavailable:
            raise MeasurementUnavailable('no wavemeter sample this cycle')
        mode = self.modes.pop(0) if len(self.modes) > 1 else (self.modes[0] if self.modes else 'A')
        return dict(frequency_hz=F0, single_mode=True, mode_id=mode, maintenance_required=False)

    def move_output(self, axis, value):
        self.calls.append(('move', axis, value))
        self.out[axis] = value

    def moves(self):
        return [c for c in self.calls if c[0] == 'move']

    def observations(self):
        return [c for c in self.calls if c[0] == 'observe']


class DirectAdapter(FakeAdapter):
    """An owner that moves the whole way inside ONE RPC, one bounded step per update interval, and reports progress."""
    def __init__(self, clock, **kw):
        super().__init__(clock, **kw)
        self.ramp = None
        self.cancelled = []

    def can_move_direct(self, axis, kind):
        return kind == 'output'

    def direct_move(self, axis, target, *, max_step_codes=None):
        self.calls.append(('direct_move', axis, target, max_step_codes))
        p = self.policy(axis)
        self.ramp = dict(axis=axis, start=self.out[axis], target=target, t0=self.clock(), step=p.max_step,
                         interval=p.update_interval_s, steps_planned=int(math.ceil(abs(target - self.out[axis]) / p.max_step - 1e-9)))
        return dict(steps_planned=self.ramp['steps_planned'])

    def _advance(self):
        r = self.ramp
        if r is None:
            return
        n = min(r['steps_planned'], int((self.clock() - r['t0']) / r['interval']))
        delta = max(-n * r['step'], min(n * r['step'], r['target'] - r['start']))
        self.out[r['axis']] = r['start'] + delta
        r['steps'] = n

    def readback(self, axis):
        self._advance()
        return super().readback(axis)

    def direct_progress(self, axis):
        self._advance()
        r = self.ramp
        return dict(done=r['steps'] >= r['steps_planned'], error=r.get('error'), steps=r['steps'])

    def direct_cancel(self, axis):
        self.cancelled.append(axis)


class LockedFake(FakeAdapter):
    """A running lock: the current axis moves as a reference move (begin/step/end RPCs)."""
    locked = True

    def begin_reference_move(self, axis):
        self.calls.append(('begin', axis))

    def step_reference(self, axis, value):
        self.calls.append(('move', axis, value))
        self.out[axis] = value

    def end_reference_move(self, axis, final):
        self.calls.append(('end', axis, final))


def run(adapter, moves, laser=ECDL_LASER, now=None, cancel=None, **extra):
    now = now if now is not None else [0.]
    events = []
    p = op.validate(dict(moves=moves, **extra))
    proc = op.OperatingPointMove(adapter, p, op.axis_map(laser), clock=lambda: now[0], sleep=lambda s: now.__setitem__(0, now[0] + s),
                                 cancel=cancel or (lambda: False), emit=lambda k, v: events.append((k, v)), lock_target_hz=F0)
    return proc.run(), events


# ------------------------------------------------------------------------------------------ parameters
def test_rule_l_parameters_and_defaults():
    p = op.validate(dict(moves=[dict(axis='current', target=dict(offset=.01))]))
    assert p['observe_each_step'] is False and p['verify_samples'] is None and p['max_step_codes'] is None
    assert op.validate(dict(moves=p['moves'], verify_samples=0, max_step_codes=4, observe_each_step=True))['max_step_codes'] == 4
    for bad in (dict(observe_each_step='yes'), dict(verify_samples=-1), dict(max_step_codes=0), dict(max_step_codes=1.5)):
        with pytest.raises(ValueError):
            op.validate(dict(moves=p['moves'], **bad))


# ------------------------------------------------------------------------------------------ fallback loop
def test_a_five_step_fallback_move_observes_nothing_per_step_and_reports_timing():
    now = [0.]
    adapter = FakeAdapter(lambda: now[0])
    result, events = run(adapter, [dict(axis='current', target=dict(offset=.01))], now=now)
    assert result['status'] == 'succeeded' and adapter.out['current'] == pytest.approx(.51)
    assert len(adapter.moves()) == 5 and result['moves'][0]['steps'] == 5
    # before/after records only: no wavemeter observation between the steps (before rule L: one per step + 2)
    assert len(adapter.observations()) == 2
    assert result['observation_before']['mode_id'] == 'A' and result['observation_after']['mode_id'] == 'A'
    t = result['timing']
    assert set(t) >= {'rpc_count', 'observation_count', 'readback_count', 'first_rpc_s', 'last_readback_s', 'verify_s', 'total_s'}
    assert t['rpc_count'] == 5 and t['observation_count'] == 2 and t['readback_count'] >= 6
    assert t['first_rpc_s'] == 0. and t['verify_s'] is None
    # the owner cadence is waited only BETWEEN writes (4 x 1.04 s), nothing after the last one (before: 5 x 1.04 s)
    assert t['total_s'] == pytest.approx(4 * 1.04) and t['last_readback_s'] == pytest.approx(4 * 1.04)
    assert result['verify'] is None and result['verify_samples'] == 1 and result['observe_each_step'] is False


def test_an_unavailable_wavemeter_never_delays_an_unobserved_move():
    now = [0.]
    adapter = FakeAdapter(lambda: now[0])
    adapter.unavailable = True
    result, events = run(adapter, [dict(axis='current', target=dict(offset=.004))], now=now)
    assert result['status'] == 'succeeded' and result['observation_before'] is None and result['observation_after'] is None
    assert result['timing']['total_s'] == pytest.approx(1 * 1.04) and len(adapter.moves()) == 2
    rejected = [v for k, v in events if k == 'measurement_rejected']
    assert len(rejected) == 2 and all(v['waited'] is False for v in rejected)
    assert result['spectral_quality_passed'] is False


def test_the_branch_change_abort_fires_only_when_observing_each_step():
    # observations: before='A', step1='A', step2='B' (then 'B')
    now = [0.]
    adapter = FakeAdapter(lambda: now[0], modes=['A', 'A', 'B', 'B'])
    with pytest.raises(DeviceError, match='Optical branch changed'):
        run(adapter, [dict(axis='current', target=dict(offset=.01))], now=now, observe_each_step=True)
    assert len(adapter.moves()) == 1                      # held after the first step
    assert len(adapter.observations()) == 3
    now = [0.]
    adapter = FakeAdapter(lambda: now[0], modes=['A', 'B', 'B'])
    result, _ = run(adapter, [dict(axis='current', target=dict(offset=.01))], now=now)
    assert result['status'] == 'succeeded' and len(adapter.moves()) == 5
    assert result['observation_before']['mode_id'] == 'A' and result['observation_after']['mode_id'] == 'B'


def test_observing_each_step_keeps_the_old_cost():
    now = [0.]
    adapter = FakeAdapter(lambda: now[0])
    result, _ = run(adapter, [dict(axis='current', target=dict(offset=.01))], now=now, observe_each_step=True)
    assert len(adapter.observations()) == 2 + 5 and result['timing']['observation_count'] == 7
    assert result['observe_each_step'] is True


# ------------------------------------------------------------------------------------------ direct owner move
def test_a_five_step_move_is_one_rpc_when_the_owner_offers_the_whole_move():
    now = [0.]
    adapter = DirectAdapter(lambda: now[0])
    result, events = run(adapter, [dict(axis='current', target=dict(offset=.01))], now=now)
    assert result['status'] == 'succeeded' and adapter.out['current'] == pytest.approx(.51)
    assert [c[0] for c in adapter.calls if c[0] in ('move', 'direct_move')] == ['direct_move']
    assert adapter.calls[adapter.calls.index(('direct_move', 'current', pytest.approx(.51), None))][3] is None   # owner budget
    row = result['moves'][0]
    assert row['direct'] is True and row['steps'] == 5 and row['final'] == pytest.approx(.51)
    t = result['timing']
    assert t['rpc_count'] == 1 and t['observation_count'] == 2
    # completion came from the owner's readback stream, polled at min(sample_interval_s, update_interval_s)
    assert t['readback_count'] >= 10 and 5. <= t['total_s'] <= 5.5 and t['last_readback_s'] <= t['total_s']
    assert [v for k, v in events if k == 'operating_point_direct'][0]['owner'] == dict(steps_planned=5)
    assert not adapter.cancelled


def test_an_explicit_max_step_codes_is_passed_to_the_owner_and_a_cancel_holds_the_ramp():
    now = [0.]
    adapter = DirectAdapter(lambda: now[0])
    result, _ = run(adapter, [dict(axis='current', target=dict(offset=.004))], now=now, max_step_codes=2)
    assert [c for c in adapter.calls if c[0] == 'direct_move'][0][3] == 2
    now = [0.]
    adapter = DirectAdapter(lambda: now[0])
    polls = []
    with pytest.raises(CardCancelled):
        run(adapter, [dict(axis='current', target=dict(offset=.01))], now=now,
            cancel=lambda: adapter.ramp is not None and (polls.append(1), len(polls) > 2)[1])
    assert adapter.cancelled == ['current'] and .5 < adapter.out['current'] < .51     # held where the owner was


def test_an_owner_error_during_the_direct_move_fails_the_card_without_a_host_replay():
    now = [0.]
    adapter = DirectAdapter(lambda: now[0])
    original = adapter.direct_progress

    def failing(axis):
        p = original(axis)
        if adapter.ramp['steps'] >= 2:
            adapter.ramp['error'] = 'Re-centring step exceeds max_step_codes'
        return original(axis)
    adapter.direct_progress = failing
    with pytest.raises(DeviceError, match='the owner ended the move'):
        run(adapter, [dict(axis='current', target=dict(offset=.01))], now=now)
    assert len([c for c in adapter.calls if c[0] == 'direct_move']) == 1 and adapter.cancelled == ['current']


# ------------------------------------------------------------------------------------------ verify defaults
def test_verify_samples_defaults_to_three_locked_and_one_open_loop():
    now = [0.]
    adapter = LockedFake(lambda: now[0])
    result, _ = run(adapter, [dict(axis='current', target=dict(offset=.004))], now=now)
    assert result['locked'] and result['verify_samples'] == 3 and result['verify']['samples'] == 3 and result['verify']['ok']
    assert result['timing']['verify_s'] == pytest.approx(3 * .5)
    assert [c[0] for c in adapter.calls if c[0] in ('begin', 'move', 'end')] == ['begin', 'move', 'move', 'end']
    assert result['timing']['rpc_count'] == 4                     # begin + 2 steps + end
    now = [0.]
    adapter = LockedFake(lambda: now[0])
    result, _ = run(adapter, [dict(axis='current', target=dict(offset=.004))], now=now, verify_samples=0)
    assert result['verify'] is None and result['verify_samples'] == 0
    now = [0.]
    adapter = FakeAdapter(lambda: now[0])
    result, _ = run(adapter, [dict(axis='current', target=dict(offset=.004))], now=now)
    assert not result['locked'] and result['verify_samples'] == 1 and result['verify'] is None


# ------------------------------------------------------------------------------------------ native adapter
class FakePort:
    """NativeCurrentPort stand-in over a tiny firmware model; ``capabilities`` decides whether the owner ramps."""
    def __init__(self, active, capabilities=()):
        self.policy = _fixture.policy()
        self.active = active
        self.capabilities = list(capabilities)
        self.offset = 32768
        self.calls = []
        self.ramp = None

    def read(self):
        if self.ramp and self.ramp['active']:
            step = max(-self.ramp['step'], min(self.ramp['step'], self.ramp['target_code'] - self.offset))
            self.offset += step
            self.ramp['steps'] += 1
            self.ramp['active'] = self.offset != self.ramp['target_code']
        return dict(sample=dict(servo_config=dict(control_offset=self.offset)), optical=dict(frequency_hz=F0),
                    activation=dict(dac_code=32700), recentre=copy.deepcopy(self.ramp))

    def heartbeat(self):
        self.calls.append('heartbeat')

    def mutate(self, action, **values):
        self.calls.append((action, values))
        if action == 'recenter_ramp':
            assert self.active and 'recenter_ramp' in self.capabilities and values['expected_code'] == self.offset
            budget = int(op._NativePolicy(self.policy).max_step)
            step = values.get('max_step_codes', budget)
            assert 1 <= step <= budget
            self.ramp = dict(active=True, target_code=values['target_code'], step=step, steps=0, error=None,
                             steps_planned=math.ceil(abs(values['target_code'] - self.offset) / step))
            return dict(self.ramp)
        if action == 'recenter_ramp_stop':
            self.ramp['active'] = False
            return self.ramp
        assert values['expected_code'] == self.offset
        if action == 'recenter':
            assert self.active and abs(values['code'] - self.offset) <= 1
        elif action == 'probe_offset':
            assert not self.active
        else:
            raise AssertionError(action)
        self.offset = values['code']
        return dict(code=self.offset)


def test_native_adapter_uses_the_owner_ramp_when_offered_and_the_stepped_loop_otherwise():
    port = FakePort(active=True, capabilities=['recenter', 'recenter_ramp'])
    adapter = op.NativeAdapter(port, NATIVE_LASER, heartbeat=port.heartbeat, sleep=lambda s: None)
    assert adapter.can_move_direct('current', 'dac_center')
    result, _ = run(adapter, [dict(axis='current', target=dict(reference='activation'))], laser=NATIVE_LASER, verify_samples=0)
    assert port.offset == 32700 and result['moves'][0]['direct'] and result['moves'][0]['kind'] == 'dac_center'
    rpcs = [c for c in port.calls if isinstance(c, tuple)]
    assert [a for a, v in rpcs] == ['recenter_ramp'] and 'max_step_codes' not in rpcs[0][1]   # the owner's own budget
    assert result['timing']['rpc_count'] == 1 and result['moves'][0]['steps'] == 17 and port.ramp['steps'] == 17
    # the same move on an owner without recenter_ramp: the fallback loop, one 'recenter' RPC per code (as before)
    port = FakePort(active=True, capabilities=['recenter'])
    adapter = op.NativeAdapter(port, NATIVE_LASER, heartbeat=port.heartbeat, sleep=lambda s: None)
    assert not adapter.can_move_direct('current', 'dac_center')
    result, _ = run(adapter, [dict(axis='current', target=dict(reference='activation'))], laser=NATIVE_LASER, verify_samples=0)
    assert port.offset == 32700 and result['moves'][0]['steps'] == 68 and 'direct' not in result['moves'][0]
    assert all(a == 'recenter' for a, v in [c for c in port.calls if isinstance(c, tuple)])
    assert result['timing']['rpc_count'] == 68
    # an explicit card max_step_codes reaches the owner ramp
    port = FakePort(active=True, capabilities=['recenter_ramp'])
    adapter = op.NativeAdapter(port, NATIVE_LASER, heartbeat=port.heartbeat, sleep=lambda s: None)
    result, _ = run(adapter, [dict(axis='current', target=dict(offset=-8.))], laser=NATIVE_LASER, verify_samples=0, max_step_codes=2)
    assert [c for c in port.calls if isinstance(c, tuple)][0][1]['max_step_codes'] == 2 and result['moves'][0]['steps'] == 4


# ------------------------------------------------------------------------------------------ owner: recenter_ramp
@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.delenv('LDCON_BACKGROUND_LASER_ID', raising=False)
    result = _fixture.rig.__wrapped__(tmp_path)
    result[0].system_owner.assemblies.factory = SimpleNamespace(laser_snapshot=dict(
        id='sr1092', interfaces=dict(native_current=dict(device='native')),
        devices=dict(native=dict(type='esp32servocon.native_current.owner',
                                connection=dict(device_id=result[0].driver.device_id)))))
    return result


def armed(rig):
    owner, servo, clock, bias, optical, gate, call, ready = rig
    ready()
    call('arm')
    clock.advance(.3)
    owner.poll()
    return int(servo.cfg['control_offset'])


def polls(rig, n=40):
    owner, servo, clock, bias, optical, gate, call, ready = rig
    for _ in range(n):
        clock.advance(.3)
        call('heartbeat')
        owner.poll()
        if not owner.recentre['active']:
            return
    raise AssertionError('ramp did not finish')


def test_owner_recenter_ramp_moves_the_whole_way_in_one_rpc_at_its_own_cadence(rig):
    owner, servo, clock, bias, optical, gate, call, ready = rig
    old = armed(rig)
    assert 'recenter_ramp' in call('describe')['capabilities']
    start = len(servo.writes)
    started = call('recenter_ramp', target_code=old + 20, expected_code=old)
    assert started['active'] and started['steps_planned'] == 5 and started['max_step_codes'] == 4   # the owner budget
    assert len(servo.writes) == start                                   # nothing written by the request itself
    # meanwhile an outside step, a target ramp and a second ramp are refused (busy), nothing written
    for action, kwargs in (('recenter', dict(code=old + 1, expected_code=old, max_step_codes=1)),
                           ('retarget', dict(target_frequency_hz=C_NM_of(servo) + 1e6)),
                           ('recenter_ramp', dict(target_code=old + 2, expected_code=old))):
        with pytest.raises(BrokerError, match='busy|in progress'):
            call(action, **kwargs)
    polls(rig)
    r = owner.recentre
    assert not r['active'] and r['error'] is None and r['steps'] == 5 and r['current_code'] == old + 20
    assert int(servo.cfg['control_offset']) == old + 20
    writes = [w['control_offset'] for w in servo.writes[start:] if 'control_offset' in w]
    assert writes == [old + 4, old + 8, old + 12, old + 16, old + 20]
    assert call('status')['recentre']['steps'] == 5 and not owner.fault
    assert [e for e in owner.input_events if e['kind'] == 'recentre_ramp_ended'][-1]['error'] is None


def C_NM_of(servo):
    return _fixture.C_NM / servo.cfg['target']


def test_owner_recenter_ramp_refuses_before_any_write_and_can_be_stopped(rig):
    owner, servo, clock, bias, optical, gate, call, ready = rig
    old = armed(rig)
    start = len(servo.writes)
    with pytest.raises(BrokerError, match='bound|envelope|range|window'):
        call('recenter_ramp', target_code=old + 2000, expected_code=old)     # leaves the activation window
    with pytest.raises(BrokerError, match='changed since caller observation'):
        call('recenter_ramp', target_code=old + 8, expected_code=old + 1)
    with pytest.raises(BrokerError, match='max_step_codes'):
        call('recenter_ramp', target_code=old + 8, expected_code=old, max_step_codes=5)
    assert len(servo.writes) == start and (owner.recentre is None or not owner.recentre['active'])
    # a 2-code ramp, stopped after its first step: holds where it is, no further write
    call('recenter_ramp', target_code=old + 8, expected_code=old, max_step_codes=2)
    clock.advance(.3); call('heartbeat'); owner.poll()
    assert owner.recentre['steps'] == 1 and int(servo.cfg['control_offset']) == old + 2
    stopped = call('recenter_ramp_stop', reason='card cancelled')
    assert not stopped['active'] and stopped['stopped'] == 'card cancelled'
    written = len(servo.writes)
    for _ in range(3):
        clock.advance(.3); call('heartbeat'); owner.poll()
    assert len(servo.writes) == written and int(servo.cfg['control_offset']) == old + 2
    # a native stop ends a running ramp too
    call('recenter_ramp', target_code=old + 6, expected_code=old + 2, max_step_codes=2)
    assert owner.recentre['active']
    call('stop')
    assert not owner.recentre['active'] and owner.recentre['stopped'] == 'native stop'
