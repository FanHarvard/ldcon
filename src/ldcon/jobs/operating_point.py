"""ONE generic "change the operating point" card (rule F, human decision 2026-10-22).

``tune.operating_point`` takes a list of moves::

    moves: [{axis, mode, target, settle_s?}]
      axis    a per-laser axis name (the mapping comes from the laser definition, see ``axis_map``), e.g.
              sr422: current / piezo / temperature;  sr1092: current (native DAC) / temperature (CLD TEC)
      mode    'locked_reference'  the reference the RUNNING lock holds for that axis (sr422: the TEC outer loop's DAC
                                  centre for piezo, the LDC current reference for current; sr1092: the native PI
                                  centre control_offset for current)
              'open_loop_output'  the held output while unlocked (PI off)
              'auto'              locked_reference when a lock runs, open_loop_output otherwise
      target  {value: x} absolute in axis units | {offset: dx} relative to the present value |
              {reference: 'window_center' | 'activation' | 'output' | 'tec_center', job_id?} (a stored result)

or a schedule (2026-10-22 PM, human request): exactly one of ``moves`` / ``schedule``::

    schedule: [{moves: [...], simultaneous: false, settle_s?, at_unix_s?, on_failure: hold|rollback_step|rollback_all}]

    plain ``moves`` = a one-step schedule (simultaneous false, on_failure hold).
    simultaneous  true: one bounded step per axis per tick (interleaved), so several outputs move together; every
                  axis keeps its own owner policy, readback verification and the optical check before each tick.
                  false: the moves of the step run one after another (as plain moves always did).
    at_unix_s     optional reservation: the step does not start before this wall-clock time (cancellable wait,
                  counted against timeout_s). Refused at validation when >60 s in the past or beyond timeout_s.
    on_failure    hold (default): stop where it is and report. rollback_step: move the step's axes back to their
                  pre-step values. rollback_all: undo every step so far, last step first, back to the pre-schedule
                  values. Rollbacks use the SAME bounded path (same adapter, owner limits, readback, optical check);
                  after an uncertain write nothing is written (hold). A cancel always holds (no rollback writes).
    Every step of the whole schedule is resolved (refusals, owner intervals) BEFORE any write. A relative
    ``offset`` in a later step is taken from the value the earlier steps planned for that axis (the planned
    target), else from the present readback; ``reference: output`` likewise.

Every write goes through the owners' existing bounds (absolute, activation-relative, max_step, slew, update interval,
readback verification, uncertain-write quarantine). Combinations that would fight a running loop are refused before
any write (an open-loop PZT/DAC output while the PI holds it; the TEC while the outer loop drives it; a locked
reference without a lock). Nothing is retried after an uncertain write.

Rule F: the procedure (``OperatingPointMove``) is one module. The standalone card runs it on its own open-loop owner
session when the laser is unlocked. When a lock runs, the standalone card files the request into the running lock
job's queue (``operating-point-requests/``) and the lock card runs the SAME procedure as its action under its own
lease at a cycle boundary (``lock_action``); the standalone card mirrors the outcome.

Rule L (latency, human decision 2026-10-26; "LDC の動作点変更に 10 秒かかる"): one axis operation is ONE RPC to the owner
where the owner offers the whole bounded move (``Adapter.can_move_direct`` / ``direct_move``: the native owner's
``recenter_ramp``; the ECDL DAC-centre reference ``set_center``); the host-side one-step loop stays only as the fallback
for owners without it. A per-step optical observation is taken only when ``observe_each_step`` is true (then the
branch-change abort applies); otherwise the move is judged by the owner's readback (value within the axis resolution)
and the optional final ``verify_samples`` wavemeter check keeps its meaning for locked moves. ``sample_interval_s`` is
the readback polling period (bounded by the owner's update interval), never a wait for a wavemeter sample. No host
slew/step caps are added (rule B): the owner's conditions bound every step inside the owner. The result carries
``timing`` (RPC/observation/readback counts and the first-RPC / last-readback / verify / total times).
"""
import copy
import json
import math
import time
import uuid
from pathlib import Path

from ldcon.core.contracts import DeviceError, MeasurementUnavailable
from ldcon.core.interface_cards import CardCancelled

C_NM = 299792458e9
MODES = ('locked_reference', 'open_loop_output', 'auto')
REFERENCES = ('window_center', 'activation', 'output', 'tec_center', 'integral_neutral')
# 'integral_neutral' (2026-10-22, sr1092): the PI centre at which the native PI needs no integral, i.e. the present
# control_offset + pid_output (the DAC the loop is producing now). Moving the centre there lets the integral relax.
# verify_samples None (rule L): 3 wavemeter samples after a locked move, 1 after an open-loop move (the readback is the
# truth there; the wavemeter verify only runs while locked). max_step_codes None: the host fallback loop steps 1 code,
# a direct owner move uses the owner's own step budget; an explicit value bounds both.
DEFAULTS = dict(moves=None, schedule=None, sample_interval_s=.5, measurement_timeout_s=60., timeout_s=900.,
                verify_samples=None, verify_tolerance_hz=None, max_step_codes=None, settle_s=None,
                approach_wait_s=0., approach_tolerance=None, observe_each_step=False)
HOST_STEP_CODES = 1          # the fallback loop's default step for count-unit axes (unchanged from before rule L)
QUEUE_DIR = 'operating-point-requests'
ON_FAILURE = ('hold', 'rollback_step', 'rollback_all')
MAX_STEPS = 16
RESERVATION_GRACE_S = 60.
STATUS_NAME = '.status.json'

# Default axis maps per lock-device type. The laser definition may override or restrict them under
# control_settings.operating_point.axes of the lock device (keys: port, locked, open_loop, unit).
#   locked:    'dac_center' | 'reference_move' | 'refused' ; open_loop: 'output' | 'refused'
AXIS_DEFAULTS = {
    'ecdl.rig.owner': {
        'current': dict(port='current', locked='reference_move', open_loop='output', unit='V'),
        'piezo': dict(port='piezo', locked='dac_center', open_loop='output', unit='count'),
        'temperature': dict(port='temperature', locked='refused', open_loop='output', unit='V',
                            locked_reason='the TEC outer loop drives the TEC while locked; move the DAC centre (piezo) instead'),
    },
    'esp32servocon.native_current.owner': {
        'current': dict(port='native_current', locked='dac_center', open_loop='output', unit='count'),
        'temperature': dict(port='temperature', locked='refused', open_loop='refused', unit='C',
                            locked_reason='the native bias lease freezes the CLD TEC set-point',
                            open_loop_reason='the CLD owner has temperature writes disabled'),
    },
}


def _finite(value, name, *, positive=False):
    if type(value) not in (int, float) or not math.isfinite(value) or (positive and value <= 0):
        raise ValueError(name + (' must be positive' if positive else ' must be finite'))
    return float(value)


# ------------------------------------------------------------------------------------------ parameters
def validate_move(move):
    if not isinstance(move, dict) or not {'axis', 'target'} <= set(move) or set(move) - {'axis', 'mode', 'target', 'settle_s'}:
        raise ValueError('A move is {axis, target, mode?, settle_s?}')
    if not isinstance(move['axis'], str) or not move['axis'].isidentifier():
        raise ValueError('move.axis must be an axis name')
    mode = move.get('mode', 'auto')
    if mode not in MODES:
        raise ValueError('move.mode must be one of %s' % (MODES,))
    t = move['target']
    if not isinstance(t, dict) or len([k for k in ('value', 'offset', 'reference') if k in t]) != 1:
        raise ValueError('move.target is exactly one of {value}, {offset}, {reference[, job_id]}')
    if 'value' in t:
        _finite(t['value'], 'target.value')
    elif 'offset' in t:
        if _finite(t['offset'], 'target.offset') == 0:
            raise ValueError('target.offset must be nonzero')
    else:
        if t['reference'] not in REFERENCES or set(t) - {'reference', 'job_id'}:
            raise ValueError('target.reference must be one of %s (+ optional job_id)' % (REFERENCES,))
        if 'job_id' in t and (not isinstance(t['job_id'], str) or not t['job_id']):
            raise ValueError('target.job_id must be a job id')
    if 'settle_s' in move:
        _finite(move['settle_s'], 'move.settle_s')
    return dict(move, mode=mode)


def _validate_moves(moves, where='moves'):
    if not isinstance(moves, list) or not 1 <= len(moves) <= 8:
        raise ValueError(where + ' must list 1..8 moves')
    return [validate_move(m) for m in moves]


def validate_step(step, index=0):
    where = 'schedule[%d]' % index
    if not isinstance(step, dict) or 'moves' not in step or set(step) - {'moves', 'simultaneous', 'settle_s', 'at_unix_s', 'on_failure'}:
        raise ValueError(where + ' is {moves, simultaneous?, settle_s?, at_unix_s?, on_failure?}')
    step = dict(step)
    step['moves'] = _validate_moves(step['moves'], where + '.moves')
    axes = [m['axis'] for m in step['moves']]
    if len(set(axes)) != len(axes):
        raise ValueError(where + ': an axis may appear at most once per step')
    step.setdefault('simultaneous', False)
    if type(step['simultaneous']) is not bool:
        raise ValueError(where + '.simultaneous must be true or false')
    step.setdefault('settle_s', None)
    if step['settle_s'] is not None and _finite(step['settle_s'], where + '.settle_s') < 0:
        raise ValueError(where + '.settle_s must be >= 0')
    step.setdefault('at_unix_s', None)
    if step['at_unix_s'] is not None:
        _finite(step['at_unix_s'], where + '.at_unix_s', positive=True)
    step.setdefault('on_failure', 'hold')
    if step['on_failure'] not in ON_FAILURE:
        raise ValueError(where + '.on_failure must be one of %s' % (ON_FAILURE,))
    return step


def schedule_of(p):
    """The normalized schedule of validated parameters (plain moves = one sequential step that holds on failure)."""
    if p.get('schedule') is not None:
        return p['schedule']
    if p.get('moves') is not None:
        return [dict(moves=p['moves'], simultaneous=False, settle_s=None, at_unix_s=None, on_failure='hold')]
    return []


def all_moves(p):
    return [m for step in schedule_of(p) for m in step['moves']]


def validate(p, *, require_moves=True, now=None):
    p = copy.deepcopy(p)
    if set(p) - DEFAULTS.keys():
        raise ValueError('Unknown operating-point parameter: ' + ', '.join(sorted(set(p) - DEFAULTS.keys())))
    for key in DEFAULTS:
        p.setdefault(key, DEFAULTS[key])
    if p['moves'] is not None and p['schedule'] is not None:
        raise ValueError('Give exactly one of moves or schedule')
    if p['moves'] is not None:
        p['moves'] = _validate_moves(p['moves'])
    elif p['schedule'] is not None:
        if not isinstance(p['schedule'], list) or not 1 <= len(p['schedule']) <= MAX_STEPS:
            raise ValueError('schedule must list 1..%d steps' % MAX_STEPS)
        p['schedule'] = [validate_step(step, i) for i, step in enumerate(p['schedule'])]
    elif require_moves:
        raise ValueError('moves (or schedule) is required')
    for key in ('sample_interval_s', 'measurement_timeout_s', 'timeout_s'):
        _finite(p[key], key, positive=True)
    if not .1 <= p['sample_interval_s'] <= 5:
        raise ValueError('sample_interval_s must be in [0.1, 5]')
    if p['timeout_s'] > 86400:
        raise ValueError('Card runtime must be <=24 hours')
    if p['verify_samples'] is not None and (type(p['verify_samples']) is not int or not 0 <= p['verify_samples'] <= 50):
        raise ValueError('verify_samples must be 0..50 (or null: 3 locked, 1 open loop)')
    if p['verify_tolerance_hz'] is not None:
        _finite(p['verify_tolerance_hz'], 'verify_tolerance_hz', positive=True)
    if p['max_step_codes'] is not None and (type(p['max_step_codes']) is not int or not 1 <= p['max_step_codes'] <= 64):
        raise ValueError('max_step_codes must be 1..64 (the owner clamps it to its own budget) or null')
    if type(p['observe_each_step']) is not bool:
        raise ValueError('observe_each_step must be true or false')
    if p['settle_s'] is not None:
        _finite(p['settle_s'], 'settle_s')
    _finite(p['approach_wait_s'], 'approach_wait_s')
    if p['approach_tolerance'] is not None:
        _finite(p['approach_tolerance'], 'approach_tolerance', positive=True)
    reserved = [s['at_unix_s'] for s in (p['schedule'] or []) if s['at_unix_s'] is not None]
    if reserved:
        now = time.time() if now is None else now
        for at in reserved:
            if at < now - RESERVATION_GRACE_S:
                raise ValueError('schedule at_unix_s %.0f is in the past (now %.0f)' % (at, now))
            if at - now > p['timeout_s']:
                raise ValueError('schedule at_unix_s %.0f is beyond timeout_s from now' % at)
        if any(b < a for a, b in zip(reserved, reserved[1:])):
            raise ValueError('schedule reservations (at_unix_s) must not decrease from step to step')
    return p


def parameters(card, overrides):
    if not isinstance(card.get('parameters'), dict) or not isinstance(overrides, dict):
        raise ValueError('Operating-point parameters must be mappings')
    return validate({**card['parameters'], **overrides}, require_moves=False)


# ------------------------------------------------------------------------------------------ laser mapping
def lock_device(laser):
    """(binding name, device name, device) of the laser's lock device: 'lock' (ECDL) or 'native_current'."""
    for role in ('lock', 'native_current'):
        binding = laser['interfaces'].get(role)
        if binding:
            return role, binding['device'], laser['devices'][binding['device']]
    raise DeviceError('The laser has no lock device (ECDL rig or native current owner)')


def axis_map(laser):
    role, name, device = lock_device(laser)
    defaults = AXIS_DEFAULTS.get(device['type'])
    if defaults is None:
        raise DeviceError('No operating-point axis map for device type ' + device['type'])
    axes = copy.deepcopy(defaults)
    configured = (device.get('control_settings') or {}).get('operating_point', {}).get('axes') or {}
    if not isinstance(configured, dict):
        raise DeviceError('control_settings.operating_point.axes must be a mapping')
    for axis, entry in configured.items():
        if not isinstance(entry, dict) or set(entry) - {'port', 'locked', 'open_loop', 'unit', 'locked_reason', 'open_loop_reason', 'enabled'}:
            raise DeviceError('operating_point.axes.%s has unknown keys' % axis)
        if entry.get('enabled') is False:
            axes.pop(axis, None)
            continue
        base = axes.get(axis, {})
        merged = {**base, **{k: v for k, v in entry.items() if k != 'enabled'}}
        if merged.get('locked') not in ('dac_center', 'reference_move', 'refused') or merged.get('open_loop') not in ('output', 'refused'):
            raise DeviceError('operating_point.axes.%s: locked must be dac_center/reference_move/refused, open_loop output/refused' % axis)
        if not isinstance(merged.get('port'), str):
            raise DeviceError('operating_point.axes.%s needs a port' % axis)
        axes[axis] = merged
    return dict(role=role, device=name, type=device['type'], axes=axes)


def lock_operation(laser):
    return 'servo.ecdl_steady_lock' if lock_device(laser)[2]['type'] == 'ecdl.rig.owner' else 'servo.native_current'


# ------------------------------------------------------------------------------------------ queue (rule F)
class OperatingPointQueue:
    """File queue under the lock job: pending/<id>.json -> <id>.status.json, <id>.cancel (at most once)."""
    def __init__(self, lock_directory):
        self.root = Path(lock_directory) / QUEUE_DIR
        self.pending = self.root / 'pending'
        self.pending.mkdir(parents=True, exist_ok=True)

    def submit(self, request):
        from ldcon.core.records import atomic_json
        path = self.pending / (request['request_id'] + '.json')
        if (path.exists() or self.status_path(request['request_id']).exists()
                or (self.root / 'claimed' / path.name).exists()
                or (self.root / (request['request_id'] + '.cancelled-request.json')).exists()):
            raise ValueError('Operating-point request already submitted')
        atomic_json(path, request)
        return path

    def receive(self):
        from ldcon.core.records import read_json
        # Only the consumer advertises this protocol. A newer proxy must not
        # rename a pending file underneath an older live consumer's directory
        # sort (which did not tolerate disappearance during stat).
        (self.root / 'atomic-claims-v1').touch(exist_ok=True)
        pending = []
        for path in self.pending.glob('*.json'):
            try:
                pending.append((path.stat().st_mtime, path))
            except FileNotFoundError:
                continue
        for _, path in sorted(pending):
            claimed = self.root / 'claimed' / path.name
            claimed.parent.mkdir(exist_ok=True)
            if claimed.exists():
                continue  # A previous claim is not permission to replay it.
            try:
                path.replace(claimed)
            except FileNotFoundError:
                continue  # Cancellation or another consumer won the atomic claim.
            value = read_json(claimed, None)
            if isinstance(value, dict) and value.get('request_id') == path.stem:
                return value
        return None

    def status_path(self, request_id):
        return self.root / (request_id + STATUS_NAME)

    def write_status(self, request_id, value):
        from ldcon.core.records import atomic_json
        atomic_json(self.status_path(request_id), dict(value, unix_s=time.time()))

    def read_status(self, request_id):
        from ldcon.core.records import read_json
        return read_json(self.status_path(request_id), None)

    def cancel(self, request_id):
        (self.root / (request_id + '.cancel')).touch()
        if self.read_status(request_id) is not None:
            return  # Preserve an admitted/running or terminal parent outcome.
        if not (self.root / 'atomic-claims-v1').is_file():
            from ldcon.jobs.operating_point_proxy import parent_ended
            try:
                ended = parent_ended(self.root.parent)
            except (OSError, ValueError, KeyError, TypeError):
                ended = False
            if not ended:
                return  # Legacy live/unknown consumers acknowledge .cancel themselves.
        pending = self.pending / (request_id + '.json')
        retired = self.root / (request_id + '.cancelled-request.json')
        if retired.exists():
            return
        try:
            pending.replace(retired)
        except FileNotFoundError:
            return  # The parent claimed it; await its own cooperative acknowledgement.
        # An old parent which read before the rename still sees .cancel before
        # executing lock_action. New parents claim via the same atomic rename.
        self.write_status(request_id, dict(status='cancelled', reason='Cancelled while pending; no owner action admitted',
                                          physical_outcome='not_executed'))

    def cancelled(self, request_id):
        return (self.root / (request_id + '.cancel')).exists()


# ------------------------------------------------------------------------------------------ adapters
class Adapter:
    """What the procedure needs from the laser. ``locked`` says which path is physically available."""
    locked = False

    def policy(self, axis):            # object with low, high, max_step, max_slew_per_s, update_interval_s, resolution
        raise NotImplementedError

    def readback(self, axis):
        raise NotImplementedError

    def observe(self):                 # dict(frequency_hz, single_mode, mode_id, maintenance_required)
        raise NotImplementedError

    def reference(self, name, axis, job_id=None):
        raise DeviceError('reference %r is not available on this laser' % name)

    def move_output(self, axis, value):           # open loop, one bounded step
        raise DeviceError('open-loop output moves are not available here')

    def set_center(self, axis, value):            # locked: the reference the loop drives toward
        raise DeviceError('locked references are not available here')

    def begin_reference_move(self, axis):
        pass

    def step_reference(self, axis, value):        # locked: one bounded step of the held output
        raise DeviceError('locked reference moves are not available here')

    def end_reference_move(self, axis, final):
        pass

    # ---- rule L: the owner moves the whole way inside ONE RPC (bounded by its own conditions); the host only polls
    # the owner's readback. Adapters that cannot offer it keep the stepped fallback above.
    def can_move_direct(self, axis, kind):
        return False

    def direct_move(self, axis, target, *, max_step_codes=None):     # -> dict(value, steps?, ...) after ONE RPC
        raise DeviceError('direct moves are not available here')

    def direct_progress(self, axis):   # -> None, or dict(done, error, steps) from the owner's readback stream
        return None

    def direct_cancel(self, axis):     # hold where it is (cancel/timeout); best effort, never a replay
        pass

    def tick(self):
        pass


def _step_toward(previous, target, policy, *, max_step=None):
    step = policy.max_step
    if max_step is not None:
        step = min(step, max_step)
    step = min(step, policy.max_slew_per_s * policy.update_interval_s)
    delta = max(-step, min(step, target - previous))
    value = previous + delta
    if policy.resolution:
        value = round(value / policy.resolution) * policy.resolution
    return value


class OperatingPointMove:
    """The procedure. ``adapter`` is the laser (locked or open loop); ``clock/sleep/cancel/emit`` as every card."""
    def __init__(self, adapter, p, axes, *, clock, sleep, cancel, emit, lock_target_hz=None, wall_clock=time.time):
        self.a, self.axes = adapter, axes
        self.p = validate(p, now=wall_clock())
        self.clock, self.sleep, self.cancel, self.emit = clock, sleep, cancel, emit
        self.wall_clock = wall_clock
        self.lock_target_hz = lock_target_hz
        self.started = clock()
        self.rows = []
        self.steps = []
        # rule L: per-step optical observation is opt-in; the verify default depends on the path (3 locked, 1 open loop)
        self.observing = bool(self.p['observe_each_step'])
        self.verify_samples = self.p['verify_samples'] if self.p['verify_samples'] is not None else (3 if adapter.locked else 1)
        self.timing = dict(rpc_count=0, observation_count=0, readback_count=0, first_rpc_s=None, last_readback_s=None,
                           verify_s=None, total_s=None)

    # ---- counted owner traffic (rule L: the human compares before/after by these) ----------------------------------
    def readback(self, axis):
        value = self.a.readback(axis)
        self.timing['readback_count'] += 1
        self.timing['last_readback_s'] = self.clock() - self.started
        return value

    def rpc(self, fn, *args, **kwargs):
        """One owner write/command RPC (refused ones count too: the owner was asked)."""
        self.timing['rpc_count'] += 1
        if self.timing['first_rpc_s'] is None:
            self.timing['first_rpc_s'] = self.clock() - self.started
        return fn(*args, **kwargs)

    def _count_owner_rpcs(self, result):
        """An adapter that loops inside (native ``set_center`` fallback) reports its own RPC count."""
        if isinstance(result, dict) and type(result.get('rpc_count')) is int and result['rpc_count'] >= 0:
            self.timing['rpc_count'] += result['rpc_count'] - 1      # rpc() counted the call itself as one

    def check(self):
        if self.cancel():
            raise CardCancelled('Operating-point move cancelled')
        if self.clock() - self.started > self.p['timeout_s']:
            raise DeviceError('Operating-point move timeout')
        self.a.tick()

    def wait(self, seconds):
        end = self.clock() + seconds
        while self.clock() < end:
            self.check()
            self.sleep(min(self.p['sample_interval_s'], max(0., end - self.clock())))

    def wait_for_claim_cadence(self, axes):
        """A new ECDL lease starts the owner's per-axis update clock.

        Wait before the first optical check and command, not after a rejected
        write. Count preparation time; unrelated slow axes do not delay current.
        The existing per-write waits still govern subsequent commands.
        """
        if not getattr(self.a, 'claim_cadence_wait', False):
            return
        due = self.started + max(self.a.policy(axis).update_interval_s for axis in axes)
        self.wait(max(0., due - self.clock()))

    def observe(self):
        began = self.clock()
        while True:
            self.check()
            try:
                self.timing['observation_count'] += 1
                return self.a.observe()
            except MeasurementUnavailable as error:
                if self.clock() - began > self.p['measurement_timeout_s']:
                    raise
                self.emit('measurement_rejected', dict(stage='operating point', reason=str(error)))
                self.wait(self.p['sample_interval_s'])

    @staticmethod
    def _finite(obs):
        value = obs.get('frequency_hz')
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise MeasurementUnavailable('A finite current-cycle frequency is required for an operating-point move')
        return obs

    def finite_observation(self):
        """A finite current-cycle reading (retried within measurement_timeout_s); no branch tracking."""
        return self._finite(self.observe())

    def control_observation(self):
        """Track the observed branch; poor spectral quality alone cannot veto a correction. The branch-change abort
        applies only when the move observes each step (rule L); otherwise this is a finite observation."""
        obs = self.finite_observation()
        if not self.observing:
            return obs
        mode = obs.get('mode_id')
        previous = getattr(self, '_control_mode', None)
        if previous is not None and mode != previous:
            raise DeviceError('Optical branch changed during operating-point move; output held')
        if mode is not None:
            self._control_mode = mode
        return obs

    def record_observation(self):
        """One attempt for the record (observation_before/after) when the move does not observe each step: never a
        wait for the wavemeter (rule L), None when nothing finite is available right now."""
        self.check()
        try:
            self.timing['observation_count'] += 1
            return self._finite(self.a.observe())
        except MeasurementUnavailable as error:
            self.emit('measurement_rejected', dict(stage='operating point record', reason=str(error), waited=False))
            return None

    def observation(self):
        """The before/after observation of the move: tracked and retried when observing each step, else recorded."""
        return self.control_observation() if self.observing else self.record_observation()

    # ---- transient owner measurement refusals (2026-10-22 live: "Fresh, aligned optical mode observation is
    # required" on a PZT move right after a recovery). The owner refused BEFORE any write (code 'measurement' ->
    # MeasurementUnavailable, uncertain_output cleared), so the client waits and retries the same bounded step;
    # consecutive refusals on one axis longer than measurement_timeout_s end the move. Never after an uncertain write.
    def refused(self, axis, error):
        if self._uncertain(error):
            raise error
        refusals = self.__dict__.setdefault('_refusals', {})
        first = refusals.setdefault(axis, self.clock())
        if self.clock() - first > self.p['measurement_timeout_s']:
            raise DeviceError('%s: the owner refused the move for %.0f s (no fresh optical observation): %s'
                              % (axis, self.clock() - first, error)) from error
        self.emit('measurement_rejected', dict(stage='operating point move', axis=axis, reason=str(error), retry=True))

    def accepted(self, axis):
        self.__dict__.setdefault('_refusals', {}).pop(axis, None)

    def retrying(self, axis, fn, *args, **kwargs):
        """Call one owner request, retrying owner measurement refusals (bounded, see ``refused``)."""
        while True:
            self.check()
            try:
                result = self.rpc(fn, *args, **kwargs)
            except MeasurementUnavailable as error:
                self.refused(axis, error)
                self.wait(max(.2, self.p['sample_interval_s']))
                continue
            self.accepted(axis)
            return result

    # ---- planning -------------------------------------------------------------------------------
    def resolve(self, move, base=None):
        """``base``: the value earlier schedule steps planned for this axis (offsets/output resolve against it)."""
        axis = move['axis']
        entry = self.axes['axes'].get(axis)
        if entry is None:
            raise DeviceError('Axis %r is not an operating-point axis of this laser (%s)' % (axis, ', '.join(sorted(self.axes['axes']))))
        mode = move['mode']
        if mode == 'auto':
            mode = 'locked_reference' if self.a.locked else 'open_loop_output'
        if mode == 'locked_reference':
            if not self.a.locked:
                raise DeviceError('%s: a locked reference needs a running lock (the laser is unlocked)' % axis)
            kind = entry['locked']
            if kind == 'refused':
                raise DeviceError('%s: locked move refused: %s' % (axis, entry.get('locked_reason', 'not allowed by the laser definition')))
        else:
            if self.a.locked:
                raise DeviceError('%s: an open-loop output move while the lock holds the outputs would fight the loop; use locked_reference' % axis)
            kind = entry['open_loop']
            if kind == 'refused':
                raise DeviceError('%s: open-loop move refused: %s' % (axis, entry.get('open_loop_reason', 'not allowed by the laser definition')))
        present = self.a.readback(axis)
        origin = present if base is None else base
        t = move['target']
        if 'value' in t:
            target = float(t['value'])
        elif 'offset' in t:
            target = origin + float(t['offset'])
        elif t['reference'] == 'output' and base is not None:
            target = float(base)
        else:
            target = float(self.a.reference(t['reference'], axis, t.get('job_id')))
        policy = self.a.policy(axis)
        if not policy.low <= target <= policy.high:
            raise DeviceError('%s: target %.6g leaves the owner interval [%.6g, %.6g]' % (axis, target, policy.low, policy.high))
        return dict(axis=axis, mode=mode, kind=kind, unit=entry.get('unit'), present=present, target=target,
                    settle_s=move.get('settle_s', self.p['settle_s']))

    # ---- execution --------------------------------------------------------------------------------
    def run_move(self, plan):
        axis, kind, target = plan['axis'], plan['kind'], plan['target']
        policy = self.a.policy(axis)
        steps = 0
        if self.a.can_move_direct(axis, kind):
            return self.run_direct(plan, policy)
        if kind == 'dac_center':
            # The reference the outer loop drives toward (sr422 TEC loop / sr1092 PI centre). Bounded by the owner.
            # ECDL: ONE RPC (the TEC outer loop approaches). Native without recenter_ramp: the adapter's stepped loop.
            result = self.retrying(axis, self.a.set_center, axis, target)
            self._count_owner_rpcs(result)
            steps = int(result.get('steps', 1)) if isinstance(result, dict) else 1
            final = self.readback(axis)
            applied = result.get('value', target) if isinstance(result, dict) else target
            wait = self.p['approach_wait_s']
            actual = getattr(self.a, 'actual', None)
            if wait > 0 and actual is not None and isinstance(result, dict) and result.get('approach_tracked'):
                # optional: wait (bounded) while the outer loop brings the ACTUAL output toward the new centre
                tol = self.p['approach_tolerance'] or max(policy.resolution, 1e-9)
                end = self.clock() + wait
                while self.clock() < end and abs(actual(axis) - target) > tol:
                    self.wait(self.readback_period(policy))
                final = dict(reference=self.readback(axis), actual=actual(axis), approached=abs(actual(axis) - target) <= tol)
            return dict(applied=applied, final=final, steps=steps)
        # Fallback (owners without a direct move): output (open loop) or reference_move (locked), bounded steps on the
        # owner's cadence. Rule L: no optical observation per step unless observe_each_step; the cadence wait precedes
        # the NEXT write (nothing is waited after the last one); each write is judged by the owner's readback.
        mover = self.a.move_output if kind == 'output' else self.a.step_reference
        if kind == 'reference_move':
            self.retrying(axis, self.a.begin_reference_move, axis)
        previous = None   # 2026-10-27: a readback failure before the first write must not mask itself with UnboundLocalError in finally
        try:
            previous = self.readback(axis)
            last_write = None
            while abs(previous - target) > max(policy.resolution / 2, 1e-12):
                self.check()
                # an explicit max_step_codes bounds count-unit steps; otherwise the owner's own budget (policy.max_step)
                value = _step_toward(previous, target, policy, max_step=self.p['max_step_codes'] if policy.unit == 'count' else None)
                if abs(value - previous) < 1e-12:
                    break
                if last_write is None:
                    self.wait_for_claim_cadence([axis])
                else:
                    self.wait(max(0., last_write + policy.update_interval_s * 1.04 - self.clock()))
                if self.observing:
                    self.control_observation()
                try:
                    self.rpc(mover, axis, value)
                except MeasurementUnavailable as error:
                    self.refused(axis, error)
                    self.wait(max(.2, policy.update_interval_s))
                    continue
                self.accepted(axis)
                last_write = self.clock()
                steps += 1
                self.emit('operating_point_step', dict(axis=axis, kind=kind, value=value, step=steps))
                previous = self.readback(axis)
                if abs(previous - value) > max(policy.resolution, 1e-9) * 1.5:
                    # the owner's readback stream may lag one cadence; one more read before calling it a mismatch
                    self.wait(policy.update_interval_s * 1.04)
                    previous = self.readback(axis)
                    if abs(previous - value) > max(policy.resolution, 1e-9) * 1.5:
                        raise DeviceError('%s: readback %.6g differs from the applied %.6g' % (axis, previous, value))
        finally:
            if kind == 'reference_move':
                try:
                    self.rpc(self.a.end_reference_move, axis, previous if previous is not None else self.readback(axis))
                except Exception as error:   # the original error (if any) propagates; the end failure is recorded
                    self.emit('operating_point_end_reference_failed', dict(axis=axis, error=str(error)))
        return dict(applied=target, final=previous, steps=steps)

    def readback_period(self, policy):
        """sample_interval_s is only the owner-readback polling period, bounded by the owner's update interval."""
        return max(.01, min(self.p['sample_interval_s'], policy.update_interval_s))

    def run_direct(self, plan, policy):
        """Rule L: ONE RPC starts the whole bounded move inside the owner; completion is judged on the owner's readback
        stream (value within the axis resolution), never by a host-side wait. Cancel/timeout hold where it is."""
        axis, kind, target = plan['axis'], plan['kind'], plan['target']
        tol = max(policy.resolution / 2, 1e-12)
        result = self.retrying(axis, self.a.direct_move, axis, target, max_step_codes=self.p['max_step_codes'])
        self.emit('operating_point_direct', dict(axis=axis, kind=kind, target=target,
                                                 owner=result if isinstance(result, dict) else None))
        progress = None
        try:
            value = self.readback(axis)
            while abs(value - target) > tol:
                progress = self.a.direct_progress(axis)
                if progress is not None:
                    if progress.get('error'):
                        raise DeviceError('%s: the owner ended the move: %s' % (axis, progress['error']))
                    if progress.get('done'):
                        value = self.readback(axis)
                        if abs(value - target) > tol:
                            raise DeviceError('%s: readback %.6g differs from the target %.6g after the owner finished the move'
                                              % (axis, value, target))
                        break
                self.wait(self.readback_period(policy))
                value = self.readback(axis)
        except BaseException:
            self.a.direct_cancel(axis)      # hold (no rollback write, no replay)
            raise
        progress = self.a.direct_progress(axis) or progress or {}
        steps = progress.get('steps')
        if steps is None:
            steps = result.get('steps', 1) if isinstance(result, dict) else 1
        return dict(applied=target, final=value, steps=int(steps), direct=True)

    # ---- simultaneous steps ---------------------------------------------------------------------------
    def run_simultaneous(self, plans):
        """One bounded step per axis per tick (interleaved), every axis through its own owner policy; the optical
        check precedes every tick only when observing each step (rule L); every applied value is read back after
        the tick's wait. (The host loop stays here: several owners' steps are interleaved by the host.)"""
        states = []
        try:
            for plan in plans:
                axis, kind = plan['axis'], plan['kind']
                if kind == 'reference_move':
                    self.retrying(axis, self.a.begin_reference_move, axis)
                states.append(dict(plan=plan, policy=self.a.policy(axis), previous=self.readback(axis), steps=0, done=False, final=None))
            tick = 0
            while True:
                self.check()
                for st in states:
                    if not st['done'] and abs(st['previous'] - st['plan']['target']) <= max(st['policy'].resolution / 2, 1e-12):
                        st['done'] = True
                active = [st for st in states if not st['done']]
                if not active:
                    break
                self.wait_for_claim_cadence([st['plan']['axis'] for st in active])
                if self.observing:
                    self.control_observation()
                tick += 1
                moved = []
                for st in active:
                    plan, policy = st['plan'], st['policy']
                    axis, kind, target = plan['axis'], plan['kind'], plan['target']
                    if kind == 'dac_center' and not hasattr(self.a, 'step_center'):
                        # one reference write (sr422 TEC-loop DAC centre): the whole move is one tick
                        result = self.retrying(axis, self.a.set_center, axis, target)
                        self._count_owner_rpcs(result)
                        st['steps'] += 1
                        st['done'] = True
                        st['previous'] = self.readback(axis)
                        st['applied'] = result.get('value', target) if isinstance(result, dict) else target
                        self.emit('operating_point_step', dict(axis=axis, kind=kind, value=st['applied'], step=st['steps'], tick=tick))
                        continue
                    value = _step_toward(st['previous'], target, policy)
                    if abs(value - st['previous']) < 1e-12:
                        st['done'] = True
                        continue
                    mover = dict(output=self.a.move_output, reference_move=self.a.step_reference,
                                 dac_center=getattr(self.a, 'step_center', None))[kind]
                    try:
                        self.rpc(mover, axis, value)
                    except MeasurementUnavailable as error:
                        self.refused(axis, error)                 # bounded; this axis skips the tick, the others move
                        continue
                    self.accepted(axis)
                    st['steps'] += 1
                    st['applied'] = value
                    moved.append(st)
                    self.emit('operating_point_step', dict(axis=axis, kind=kind, value=value, step=st['steps'], tick=tick))
                if moved:
                    self.wait(max(st['policy'].update_interval_s for st in moved) * 1.04)
                elif any(not st['done'] for st in states):
                    self.wait(max(.2, max(st['policy'].update_interval_s for st in states)))
                for st in moved:
                    st['previous'] = self.readback(st['plan']['axis'])
                    if abs(st['previous'] - st['applied']) > max(st['policy'].resolution, 1e-9) * 1.5:
                        raise DeviceError('%s: readback %.6g differs from the applied %.6g' % (st['plan']['axis'], st['previous'], st['applied']))
        finally:
            for st in states:
                if st['plan']['kind'] == 'reference_move':
                    self.rpc(self.a.end_reference_move, st['plan']['axis'], st['previous'])
        return [dict(st['plan'], applied=st['plan']['target'], final=st['previous'], steps=st['steps']) for st in states]

    # ---- schedule -------------------------------------------------------------------------------------
    def plan_schedule(self):
        """Resolve every move of every step before any write (all refusals happen here)."""
        planned, expected = [], {}
        for index, step in enumerate(schedule_of(self.p)):
            plans = []
            for move in step['moves']:
                plan = self.resolve(move, base=expected.get(move['axis']))
                expected[move['axis']] = plan['target']
                plans.append(plan)
            planned.append(dict(index=index, step=step, plans=plans, pre=None))
        return planned

    def _uncertain(self, error):
        for holder in (getattr(error, 'session', None), error, self.a, getattr(self.a, 'lock', None), getattr(self.a, 'port', None)):
            if holder is not None and getattr(holder, 'uncertain_output', False) is True:
                return True
        ports = getattr(self.a, 'ports', None)
        if isinstance(ports, dict) and any(getattr(port, 'uncertain_output', False) is True for port in ports.values()):
            return True
        return False

    def _fail(self, message, error):
        failure = DeviceError(message)
        failure.operating_point_steps = copy.deepcopy(self.steps)
        session = getattr(error, 'session', None)
        if session is not None:
            failure.session = session
        return failure

    def _step_failed(self, planned, index, row, error):
        step = planned[index]['step']
        policy = step['on_failure']
        if policy == 'hold' or self._uncertain(error):
            row['status'] = 'held'
            if policy != 'hold':
                row['rollback'] = dict(ok=False, skipped=True, reason='uncertain write: nothing is written after it (hold)')
            self.steps.append(row)
            self.emit('operating_point_step_finished', row)
            raise self._fail('Operating-point schedule step %d failed and holds: %s' % (index + 1, error), error) from error
        order = [index] if policy == 'rollback_step' else list(range(index, -1, -1))
        done = []
        row['rollback'] = dict(policy=policy, ok=False, moves=done)
        try:
            for j in order:
                for plan in reversed(planned[j]['plans']):
                    if planned[j]['pre'] is None or plan['axis'] not in planned[j]['pre']:
                        continue
                    back = dict(plan, target=planned[j]['pre'][plan['axis']], settle_s=None)
                    self.emit('operating_point_rollback', dict(step=j + 1, axis=back['axis'], target=back['target'], policy=policy))
                    outcome = self.run_move(back)
                    done.append(dict(step=j + 1, axis=back['axis'], target=back['target'], **outcome))
        except CardCancelled:
            row['status'] = 'held'
            self.steps.append(row)
            raise
        except Exception as rollback_error:
            row['status'] = 'held'
            row['rollback'].update(error=str(rollback_error))
            self.steps.append(row)
            self.emit('operating_point_step_finished', row)
            raise self._fail('Operating-point schedule step %d failed (%s) and its %s failed too (%s); held'
                             % (index + 1, error, policy, rollback_error), rollback_error) from rollback_error
        row['status'] = 'rolled_back'
        row['rollback']['ok'] = True
        self.steps.append(row)
        self.emit('operating_point_step_finished', row)
        raise self._fail('Operating-point schedule step %d failed (%s); %s done' % (index + 1, error, policy), error) from error

    def run(self):
        planned = self.plan_schedule()                      # every refusal happens before any write
        self.emit('operating_point_plan', dict(moves=[p for s in planned for p in s['plans']], locked=self.a.locked,
                                               schedule=[dict(step=s['index'] + 1, simultaneous=s['step']['simultaneous'],
                                                              at_unix_s=s['step']['at_unix_s'], on_failure=s['step']['on_failure'],
                                                              moves=s['plans']) for s in planned]))
        start = self.observation()
        for entry in planned:
            index, step = entry['index'], entry['step']
            self.check()
            late_s = None
            if step['at_unix_s'] is not None:
                remaining = step['at_unix_s'] - self.wall_clock()
                if remaining > 0:
                    self.emit('operating_point_reservation', dict(step=index + 1, at_unix_s=step['at_unix_s'], wait_s=remaining))
                    self.wait(remaining)
                else:
                    late_s = -remaining
            entry['pre'] = {plan['axis']: self.readback(plan['axis']) for plan in entry['plans']}
            row = dict(step=index + 1, simultaneous=step['simultaneous'], on_failure=step['on_failure'],
                       at_unix_s=step['at_unix_s'], started_unix_s=self.wall_clock(), late_s=late_s, pre=dict(entry['pre']), moves=[])
            self.emit('operating_point_step_started', dict(step=index + 1, simultaneous=step['simultaneous'], pre=row['pre']))
            try:
                if step['simultaneous'] and len(entry['plans']) > 1:
                    rows = self.run_simultaneous(entry['plans'])
                    row['moves'] = rows
                    settle = max([r['settle_s'] or 0. for r in rows])
                    if settle:
                        self.wait(settle)
                else:
                    for plan in entry['plans']:
                        self.check()
                        move_row = dict(plan, **self.run_move(plan))
                        row['moves'].append(move_row)
                        if plan['settle_s']:
                            self.wait(plan['settle_s'])
                        self.emit('operating_point_move', move_row)
            except CardCancelled:
                row.update(status='held', error='cancelled')
                self.steps.append(row)
                self.emit('operating_point_step_finished', row)
                raise
            except Exception as error:
                row.update(status='failed', error=str(error))
                self.rows.extend(row['moves'])
                self._step_failed(planned, index, row, error)
            self.rows.extend(row['moves'])
            if step['simultaneous'] and len(entry['plans']) > 1:
                for move_row in row['moves']:
                    self.emit('operating_point_move', move_row)
            if step['settle_s']:
                self.wait(step['settle_s'])
            row['status'] = 'succeeded'
            self.steps.append(row)
            self.emit('operating_point_step_finished', row)
        verify = None
        if self.verify_samples and self.a.locked:
            # the optional wavemeter check after a LOCKED move keeps its meaning (rule L); the lock's own readback
            # stream (response_info / native status) is what is sampled, at sample_interval_s
            began = self.clock()
            tol = self.p['verify_tolerance_hz']
            samples = []
            for _ in range(self.verify_samples):
                obs = self.control_observation()
                samples.append(obs)
                self.wait(self.p['sample_interval_s'])
            target = self.lock_target_hz
            errors = [abs(o['frequency_hz'] - target) for o in samples if target and o.get('frequency_hz')]
            quality = all(o.get('single_mode') is True and not o.get('maintenance_required') for o in samples)
            arrived = None
            if tol is not None and target:
                arrived = bool(len(errors)==len(samples) and max(errors) <= tol)
            ok = arrived is not False
            verify = dict(samples=len(samples), max_abs_error_hz=max(errors) if errors else None,
                          single_mode=quality, spectral_quality_passed=quality, frequency_arrived=arrived, ok=ok)
            self.timing['verify_s'] = self.clock() - began
            if not ok:
                raise DeviceError('Operating point moved but the lock did not verify afterwards: %s' % verify)
        final = self.observation()
        self.timing['total_s'] = self.clock() - self.started
        row = lambda o: None if o is None else {k: o.get(k) for k in ('frequency_hz', 'single_mode', 'mode_id')}
        return dict(status='succeeded', movement_applied=True, locked=self.a.locked, moves=self.rows, schedule=self.steps, verify=verify,
                    verify_samples=self.verify_samples, observe_each_step=self.observing,
                    spectral_quality_passed=bool(final is not None and final.get('single_mode') is True and not final.get('maintenance_required')),
                    observation_before=row(start), observation_after=row(final), timing=dict(self.timing))


# ------------------------------------------------------------------------------------------ ECDL adapters
def _mode_row(obs):
    f = obs.frequency.frequency_hz if hasattr(obs, 'frequency') else None
    return dict(frequency_hz=f, single_mode=getattr(obs, 'single_mode', None), mode_id=getattr(obs, 'mode_id', None),
                maintenance_required=getattr(obs, 'maintenance_required', False))


class ECDLOpenAdapter(Adapter):
    """Unlocked ECDL: the open-loop owner session of open_ecdl_interfaces (AxisProxy moves)."""
    locked = False
    claim_cadence_wait = True

    def __init__(self, ports, laser):
        self.ports, self.laser = ports, laser
        if not ports['loop'].is_open_loop():
            raise DeviceError('Open-loop operating-point moves require an open loop (a lock holds the rig)')

    def policy(self, axis):
        return self.ports[axis].policy

    def readback(self, axis):
        return self.ports[axis].readback()

    def observe(self):
        return _mode_row(self.ports['observation'].read_mode())

    def move_output(self, axis, value):
        self.ports[axis].move(value, self.ports[axis].policy.update_interval_s)

    def reference(self, name, axis, job_id=None):
        if name == 'output':
            return self.readback(axis)
        raise DeviceError('reference %r needs a running lock (window_center/activation/tec_center are lock state)' % name)


class ECDLLockedAdapter(Adapter):
    """Running sr422 lock: the SteadyLockProxy under its own lease (rule F action)."""
    locked = True

    def __init__(self, lock, laser, *, directory=None, heartbeat=None):
        self.lock, self.laser, self.directory, self.heartbeat = lock, laser, Path(directory) if directory else None, heartbeat
        self.ports = lock.recovery_ports()
        self.last_info = None

    def tick(self):
        if self.heartbeat:
            self.heartbeat()

    def policy(self, axis):
        return self.ports[axis].policy

    def info(self):
        self.last_info = self.lock.response_info()
        return self.last_info

    def readback(self, axis):
        if axis == 'piezo':
            # the DAC centre target of the TEC outer loop (what this card changes), not the actual DAC
            info = self.info()
            centre = (info.get('tec_context') or {}).get('tec_center_reference_dac')
            return float(centre) if centre is not None else float(info['outputs']['piezo'])
        return float(self.info()['outputs'][axis])

    def actual(self, axis):
        return float(self.info()['outputs'][axis])

    def observe(self):
        o = self.info()['observation']
        f = o['frequency']
        value = f.get('frequency_hz')
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise MeasurementUnavailable('Finite optical sample required')
        return dict(frequency_hz=f['frequency_hz'], single_mode=o.get('single_mode'), mode_id=o.get('mode_id'),
                    maintenance_required=o.get('maintenance_required', False))

    def reference(self, name, axis, job_id=None):
        info = self.info()
        if name == 'output':
            return float(info['outputs'][axis])
        if name == 'activation':
            value = (info.get('activation') or {}).get(axis)
            if value is None:
                raise DeviceError('activation reference is not reported by this owner')
            return float(value)
        if name == 'tec_center':
            value = (info.get('tec_context') or {}).get('tec_center_reference_dac')
            if value is None:
                raise DeviceError('the DAC centre reference is not reported by this owner')
            return float(value)
        if name == 'window_center':
            return window_center_value(self.directory, job_id, axis)
        raise DeviceError('unknown reference ' + name)

    def set_center(self, axis, value):
        if axis != 'piezo':
            raise DeviceError('%s has no locked DAC-centre reference' % axis)
        result = self.lock.set_reference('piezo_center', value)
        return dict(value=result.get('value', value), steps=1, approach_tracked=True)

    def begin_reference_move(self, axis):
        if axis != 'current':
            raise DeviceError('%s has no locked reference move' % axis)
        self.lock.response_begin()

    def step_reference(self, axis, value):
        result = self.lock.response_current(value)
        if isinstance(result, dict) and result.get('applied') is False:
            raise MeasurementUnavailable('owner held the step: ' + str(result.get('reason')))

    def end_reference_move(self, axis, final):
        try:
            if final is not None and not self.lock.uncertain_output:
                self.lock.set_reference('current', final)
        finally:
            self.lock.response_end()


def window_center_value(directory, job_id, axis):
    """The last verified midpoint of a tune.ecdl_window_center run: online-status.json of that steady job."""
    from ldcon.core.records import read_json
    if axis != 'current':
        raise DeviceError('window_center is a current-axis midpoint')
    if directory is None:
        raise DeviceError('window_center needs the lock job directory')
    directory = Path(directory)
    candidates = [directory] if job_id in (None, directory.name) else [directory.parent / job_id]
    for d in candidates:
        status = read_json(d / 'online-status.json', {})
        if status.get('done') and status.get('center') is not None and status.get('status') in (None, 'succeeded'):
            return float(status['center'])
    raise DeviceError('no verified window-centre midpoint in %s' % ', '.join(str(c) for c in candidates))


# ------------------------------------------------------------------------------------------ native adapters
class _NativePolicy:
    def __init__(self, policy, *, unit='count', open_loop=True):
        from ldcon.owners.native_modulation_limits import budget_codes
        self.unit = unit
        self.low, self.high = 0., 65535.
        self.max_step = float(max(1, budget_codes(policy)))
        self.update_interval_s = policy['update_interval_s']
        self.max_slew_per_s = self.max_step / self.update_interval_s
        self.resolution = 1.


class NativeAdapter(Adapter):
    """sr1092: the native current owner port (NativeCurrentPort). Locked = PI active (recenter), else probe_offset."""
    def __init__(self, port, laser, *, heartbeat=None, max_step_codes=HOST_STEP_CODES, sleep=time.sleep):
        self.port, self.laser, self.heartbeat, self.sleep = port, laser, heartbeat, sleep
        self.locked = bool(getattr(port, 'active', False))
        self.max_step_codes = max_step_codes          # the host-loop step (fallback set_center / step_center ticks)
        self._policy = _NativePolicy(port.policy)
        if self.locked:
            self._policy.max_step = float(min(self._policy.max_step, max_step_codes))

    # ---- rule L: the owner's recenter_ramp (v5.9) moves the PI centre the whole way in ONE RPC, one bounded step
    # (<= the owner's offset budget) per update interval inside the owner; the host polls the status readback.
    def can_move_direct(self, axis, kind):
        return kind == 'dac_center' and self.locked and 'recenter_ramp' in (getattr(self.port, 'capabilities', None) or ())

    def direct_move(self, axis, target, *, max_step_codes=None):
        self.policy(axis)
        previous = int(round(self.readback(axis)))
        request = dict(target_code=int(round(target)), expected_code=previous)
        if max_step_codes is not None:
            request['max_step_codes'] = int(max_step_codes)     # explicit card value; else the owner's own budget
        result = self.port.mutate('recenter_ramp', **request)
        return dict(value=int(round(target)), steps=result.get('steps_planned') if isinstance(result, dict) else None,
                    rpc_count=1, approach_tracked=False, owner=result if isinstance(result, dict) else None)

    def direct_progress(self, axis):
        r = self.state().get('recentre')
        if not isinstance(r, dict):
            return dict(done=True, error='the owner reports no re-centring ramp (owner restarted?)', steps=None)
        return dict(done=not r.get('active'), error=r.get('error'), steps=r.get('steps'))

    def direct_cancel(self, axis):
        try:
            self.port.mutate('recenter_ramp_stop', reason='operating-point move cancelled or timed out')
        except Exception:
            pass        # best effort: the owner stops the ramp itself at the next poll if its lease/heartbeat ends

    def tick(self):
        if self.heartbeat:
            self.heartbeat()

    def policy(self, axis):
        if axis != 'current':
            raise DeviceError('%s is not served by the native owner' % axis)
        return self._policy

    def state(self):
        return self.port.read()

    def readback(self, axis):
        self.policy(axis)
        return float(self.state()['sample']['servo_config']['control_offset'])

    def observe(self):
        s = self.state()
        o = s.get('optical') or {}
        f = o.get('frequency_hz')
        # rule B (2026-10-22): a finite current reading; no wavemeter quality filter
        if not isinstance(f, (int, float)) or not math.isfinite(f) or f <= 0:
            raise MeasurementUnavailable('native optical cache invalid')
        return dict(frequency_hz=o['frequency_hz'], single_mode=None, mode_id=None, maintenance_required=False)

    def reference(self, name, axis, job_id=None):
        self.policy(axis)
        if name == 'output':
            return self.readback(axis)
        if name == 'activation':
            s = self.state()
            code = (s.get('activation') or {}).get('dac_code') if isinstance(s.get('activation'), dict) else s.get('activation_dac_code')
            if code is None:
                raise DeviceError('activation reference is not reported by this owner')
            return float(code)
        if name == 'integral_neutral':
            sample = self.state()['sample']
            return float(sample['servo_config']['control_offset']) + float(sample['servo_status']['pid_output'])
        raise DeviceError('reference %r is not available on the native owner' % name)

    def integral_fraction(self):
        """|pid_integral| / the firmware's raw integral limit (the owner's FF start/abort fractions use the same)."""
        sample = self.state()['sample']
        cfg = sample['servo_config']
        limit = max(abs(float(cfg['pid_integral_min'])), abs(float(cfg['pid_integral_max'])))
        return abs(float(sample['servo_status']['pid_integral'])) / limit if limit > 0 else math.inf

    def move_output(self, axis, value):
        previous = int(round(self.readback(axis)))
        self.port.mutate('probe_offset', code=int(round(value)), expected_code=previous)

    def _step_budget(self):
        # v4.17: a step larger than 1 code must be announced to the owner (its 'recenter' default is 1 code); the owner
        # checks the approved external-modulation budget and re-proves the complete output envelope.
        step = int(self._policy.max_step)
        return dict(max_step_codes=step) if self.locked and step > 1 else {}

    def step_center(self, axis, value):
        """One bounded recenter write (simultaneous schedule ticks); the owner re-checks the envelope."""
        self.policy(axis)
        previous = int(round(self.readback(axis)))
        code = int(round(value))
        if abs(code - previous) > int(self._policy.max_step):
            raise DeviceError('recenter step %d -> %d exceeds %d codes' % (previous, code, int(self._policy.max_step)))
        self.port.mutate('recenter' if self.locked else 'probe_offset', code=code, expected_code=previous,
                         **self._step_budget())

    def set_center(self, axis, value):
        # Fallback for owners without recenter_ramp: one bounded 'recenter' RPC per step (the owner re-checks the
        # envelope and the PI absorbs the shift), one step per update interval, host-side loop.
        self.policy(axis)
        target = int(round(value))
        steps = 0
        previous = int(round(self.readback(axis)))
        while previous != target:
            self.tick()
            step = max(-int(self._policy.max_step), min(int(self._policy.max_step), target - previous))
            self.port.mutate('recenter', code=previous + step, expected_code=previous, **self._step_budget())
            steps += 1
            previous = int(round(self.readback(axis)))
            if previous != target:
                self.sleep(self._policy.update_interval_s * 1.04)
        return dict(value=previous, steps=steps, rpc_count=steps, approach_tracked=False)


# ------------------------------------------------------------------------------------------ lock-job action
def lock_action(queue, make_adapter, laser, *, clock, sleep, cancel, emit, lock_target_hz=None, busy=False):
    """Called by the lock card at a cycle boundary: run at most one queued request as its own action."""
    (queue.root / 'change-measure-v1').touch(exist_ok=True)
    request = queue.receive()
    if request is None:
        return None
    rid = request['request_id']
    if busy:
        queue.write_status(rid, dict(status='rejected', reason='Another controller operation is active'))
        return dict(request_id=rid, status='rejected')
    if queue.cancelled(rid):
        queue.write_status(rid, dict(status='cancelled', reason='cancelled before start'))
        return dict(request_id=rid, status='cancelled')
    try:
        if request.get('measurement_operation') in ('measure.change', 'measure.batch'):
            from ldcon.jobs.change_measure_adapter import execute_locked
            queue.write_status(rid, dict(status='running'))
            result = execute_locked(request, make_adapter(), laser, clock=clock, sleep=sleep,
                cancel=lambda: cancel() or queue.cancelled(rid), emit=emit, controller_directory=queue.root.parent)
            queue.write_status(rid, dict(status=result['status'], result=result))
            return dict(request_id=rid, status=result['status'], result=result)
        p = validate(request['parameters'])
        if any(m['mode'] == 'open_loop_output' for m in all_moves(p)):
            raise DeviceError('open-loop output moves cannot run inside a lock')
        queue.write_status(rid, dict(status='running'))
        adapter = make_adapter()
        procedure = OperatingPointMove(adapter, p, axis_map(laser), clock=clock, sleep=sleep,
                                       cancel=lambda: cancel() or queue.cancelled(rid), emit=emit, lock_target_hz=lock_target_hz)
        result = procedure.run()
        queue.write_status(rid, dict(status='succeeded', result=result))
        return dict(request_id=rid, status='succeeded', result=result)
    except CardCancelled as error:
        result=getattr(error,'measurement_result',None)
        queue.write_status(rid, dict(status='cancelled', reason=str(error),result=result))
        if (result or {}).get('physical_outcome')=='unknown':
            from ldcon.core.records import atomic_json
            atomic_json(queue.root.parent/'device-review.json',dict(source='change_measure',
                request_id=rid,physical_outcome='unknown',reason='Cancellation lacks a confirmed hold receipt'))
            raise DeviceError('Measurement cancellation left an uncertain application; inspect owner') from error
        return dict(request_id=rid, status='cancelled')
    except Exception as error:
        result=getattr(error,'measurement_result',None)
        queue.write_status(rid, dict(status='failed', reason=str(error),result=result))
        if (result or {}).get('physical_outcome')=='unknown':
            from ldcon.core.records import atomic_json
            atomic_json(queue.root.parent/'device-review.json',dict(source='change_measure',
                request_id=rid,physical_outcome='unknown',reason='Application or cleanup lacks a confirmed receipt'))
            raise
        if getattr(getattr(error, 'session', None), 'uncertain_output', False):
            raise
        return dict(request_id=rid, status='failed', reason=str(error))


# ------------------------------------------------------------------------------------------ standalone card
def find_running_lock(directory, laser):
    # Most siblings are historical. Read their small progress headers first;
    # load full frozen metadata only for plausible active lock controllers.
    from ldcon.core.records import read_json
    from ldcon.jobs.native_ff_proxy import lock_report_locked
    from ldcon.jobs.job_service import JobProcess
    operation = lock_operation(laser)
    matches = []
    for path in Path(directory).parent.glob('*/progress.json'):
        if path.parent == Path(directory):
            continue
        progress = read_json(path, {})
        if progress.get('status') not in ('running', 'acquiring', 'finishing'):
            continue
        if progress.get('controller_state') != 'locked' and not lock_report_locked(path.parent):
            continue
        meta = read_json(path.parent / 'job.json', {})
        if ((meta.get('laser') or {}).get('id') != laser['id'] or not any(
                c.get('operation', c.get('card')) == operation for c in meta.get('cards', []))):
            continue
        if (path.parent / 'worker-process.json').exists() and JobProcess(path.parent).poll() is not None:
            continue
        matches.append((path.parent, meta, progress))
    if len(matches) > 1:
        raise DeviceError('Multiple active lock jobs; inspect ownership before moving an operating point')
    return matches[0] if matches else None


def run_operating_point(directory, laser, card, p, *, clock, sleep, cancel, emit, publish=None, open_ports=None):
    """Standalone card: proxy into the running lock (locked_reference) or move the held outputs (open loop)."""
    p = validate(p)
    publish = publish or (lambda value: None)
    found = find_running_lock(directory, laser)
    if found is not None:
        lock_directory, meta, progress = found
        for move in all_moves(p):
            if move['mode'] == 'open_loop_output':
                raise DeviceError('%s: the lock %s holds the outputs; open-loop moves are refused (use locked_reference)' % (move['axis'], meta.get('id', '')[:8]))
        queue = OperatingPointQueue(lock_directory)
        locked = lambda moves: [dict(m, mode='locked_reference') for m in moves]
        # Rule L parameters at their defaults are not sent (a lock job of an earlier release validates the request
        # with its own defaults and would refuse unknown keys); None values re-default on the lock side too.
        base = {k: v for k, v in p.items() if v is not None and not (k == 'observe_each_step' and v is False)}
        if p['schedule'] is not None:
            parameters = {**base, 'schedule': [dict(step, moves=locked(step['moves'])) for step in p['schedule']]}
        else:
            parameters = {**base, 'moves': locked(p['moves'])}
        request = dict(request_id=uuid.uuid4().hex, card_id=card['id'], job_id=Path(directory).name,
                       parameters=parameters, submitted_unix_s=time.time())
        queue.submit(request)
        emit('operating_point_submitted', dict(request_id=request['request_id'], lock_job=meta.get('id')))
        publish(dict(request_id=request['request_id'], lock_job=meta.get('id'), status='submitted'))
        started = clock()
        last_wait_publication = None
        last_wait_at = None
        last_status_publication = None
        while True:
            if cancel():
                queue.cancel(request['request_id'])
            status = queue.read_status(request['request_id'])
            if status is not None:
                if status != last_status_publication:
                    publish(dict(status, request_id=request['request_id'], lock_job=meta.get('id')))
                    last_status_publication = status
                if status.get('status') in ('succeeded', 'failed', 'cancelled', 'rejected'):
                    if status['status'] == 'succeeded':
                        result = dict(status.get('result') or {})
                        result.update(status='succeeded', proxied=True, lock_job=meta.get('id'), request_id=request['request_id'])
                        # Output readback and optical convergence are distinct.
                        # The shared learned consumer only observes; it never
                        # changes or restores the already-applied point.
                        if p['verify_samples'] != 0:
                            from ldcon.jobs.settling_profile import verify_move
                            from ldcon.jobs.lock_target import resolve_target
                            learned=verify_move(directory,laser,resolve_target(laser,{}),time.time(),
                                clock=clock,sleep=sleep,cancel=cancel,
                                timeout_s=max(.1,p['timeout_s']-(clock()-started)))
                            if learned:result['verify']=learned
                        return result
                    if status['status'] == 'cancelled':
                        raise CardCancelled('Operating-point request cancelled: ' + str(status.get('reason')))
                    raise DeviceError('Operating-point request %s: %s' % (status['status'], status.get('reason')))
            if clock() - started > p['timeout_s']:
                queue.cancel(request['request_id'])
                raise DeviceError('Operating-point request timed out in the lock job')
            from ldcon.jobs.operating_point_proxy import parent_ended, waiting_state
            try:
                ended = parent_ended(lock_directory)
            except (OSError, ValueError, KeyError, TypeError):
                ended = False  # Unknown process identity never means the parent exited.
            if ended:
                queue.cancel(request['request_id'])
                from ldcon.core.records import atomic_json
                terminal = queue.read_status(request['request_id'])
                atomic_json(Path(directory) / 'operating-point-parent-ended.json', dict(
                    request_id=request['request_id'], lock_job=meta.get('id'), parent_ended=True,
                    queue_status=terminal, prior_queue_status=status, hardware_write=False,
                    physical_outcome=(terminal or {}).get('physical_outcome', 'unknown_prior_effects'),
                    note='Parent exited; no historical command is replayed and no output is restored'))
                if cancel():
                    raise CardCancelled('Operating-point parent exited; proxy cancelled without replay')
                raise DeviceError('Operating-point parent exited before a verified request result; inspect parent and queue evidence')
            if status is None:
                waiting = waiting_state(lock_directory, request['submitted_unix_s'])
                signature = (waiting['parent_status'], waiting['parent_controller_state'])
                if signature != last_wait_publication or last_wait_at is None or clock() - last_wait_at >= 1.:
                    publish(dict(waiting, request_id=request['request_id'], lock_job=meta.get('id'), status='submitted'))
                    last_wait_publication = signature
                    last_wait_at = clock()
            # Queue polling has no physical read or write. Device cadence and
            # the procedure's settling/verification intervals remain unchanged.
            sleep(min(.1, p['sample_interval_s']))
    # Unlocked: the card owns an open-loop session itself.
    for move in all_moves(p):
        if move['mode'] == 'locked_reference':
            raise DeviceError('%s: a locked reference needs a running lock (the laser is unlocked); use open_loop_output or auto' % move['axis'])
    role, name, device = lock_device(laser)
    if open_ports is None:
        open_ports = open_unlocked_ports
    ports, adapter, close = open_ports(laser, card, p)
    try:
        return OperatingPointMove(adapter, p, axis_map(laser), clock=clock, sleep=sleep, cancel=cancel, emit=emit).run()
    finally:
        close()


def open_unlocked_ports(laser, card, p):
    role, name, device = lock_device(laser)
    if device['type'] == 'ecdl.rig.owner':
        from ldcon.jobs.ecdl_devices import open_ecdl_interfaces
        from ldcon.core.interface_schema import ECDL_OPEN_PORTS
        ports = open_ecdl_interfaces(laser, dict(card, uses=list(ECDL_OPEN_PORTS)))
        adapter = ECDLOpenAdapter(ports, laser)
        def close():
            for port in ports.values():
                if hasattr(port, 'close'):
                    port.close()
        return ports, adapter, close
    from ldcon.owners.esp32_native_client import NativeCurrentPort
    port = NativeCurrentPort(device).connect()
    adapter = NativeAdapter(port, laser, max_step_codes=p['max_step_codes'] or HOST_STEP_CODES, heartbeat=port.heartbeat)
    if adapter.locked:
        port.close()
        raise DeviceError('The native PI is active without a lock job; refusing open-loop moves')
    return dict(native_current=port), adapter, port.close
