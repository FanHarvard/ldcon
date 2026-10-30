"""Owner-only temporary TEC gain/cadence overlay for target-response studies.

``native.p`` remains the canonical runtime profile. Only the existing planner's
profile view is overlaid; its physical origin, FF state and center are retained.
The existing TargetFFStudy token and deadline are the single session authority.

All methods must run under the physical owner's lock. The owner must sync before
reads/plans/adjustments, from its independent watchdog, and after FF policy
status/set/restore. Clear before foreground changes, sweep, hold and recovery.
Read the effective gain/cadence from ``native.planner.p`` while this is active.
An invalidated pending adjustment must be deferred without replay, not dispatched
after ``sync`` returns True. This module never reads or writes hardware, restores
a historical target/output, or grants permission to exceed physical bounds.
"""

import copy
import hashlib
import json
import math

from ldcon.core.adjustment_registry import owner_policy
from ldcon.owners.ecdl_steady_policy import validate_profile
from ldcon.core.instrument_client import BrokerError


FIELDS = frozenset(('tec_gain', 'supervision_interval_s'))


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


class ThermalStudyOverlay:
    """A scoped parameter view, with no physical checkpoint-return authority."""

    def __init__(self, native):
        self.native = native
        self._record = None
        self._planner = None
        self._effective = None
        self.last = None

    @property
    def active(self):
        return self._record is not None

    @property
    def token(self):
        return self._record['session_key'][0] if self.active else None

    def _context(self):
        n = self.native
        return dict(
            lease=n.o.lease, policy_revision=n.o.policy_authority.revision,
            base_profile=copy.deepcopy(n.p), strategy=n.strategy,
            tec_ff_paused=n.tec_ff_paused,
            reference=dict(lease=n.tec_reference_lease, dac=n.tec_reference_dac,
                           confirmed=n.tec_reference_confirmed,
                           pending=n.tec_reference_pending),
            planner_reference=n.planner.tec_reference_dac,
            planner_origin=copy.deepcopy(n.planner.origin),
            parameter_overrides=copy.deepcopy(n.parameter_overrides),
            authorized_policy=owner_policy(n.o.device, 'slow_loop'))

    @staticmethod
    def _revision(context):
        return hashlib.sha256(json.dumps(context, sort_keys=True, allow_nan=False,
                                        separators=(',', ':')).encode()).hexdigest()

    @staticmethod
    def _session_key(session):
        if not isinstance(session, dict):
            return None
        token, expiry = session.get('study_token'), session.get('expires_at')
        if not isinstance(token, str) or not token or not _number(expiry):
            return None
        # Scale/step changes also require an explicit new overlay transaction.
        return tuple(session.get(key) for key in
                     ('study_token', 'expires_at', 'scale', 'step_hz'))

    def _busy(self):
        n = self.native
        sweep = n.sweep
        return bool(n.commissioning or n.response_control or n.o.recovery is not None
                    or n.study and n.study.status == 'running'
                    or n.pid_tuning and not n.pid_tuning.closed
                    or n.background.epoch is not None or n.background.reservations
                    or sweep is not None and sweep.state not in n._sweep_finished())

    def validate(self, candidate, *, outputs=None):
        """Preflight before changing the FF session; no mutation on rejection."""
        n = self.native
        if (not n.o.lease or n.o.fault or not n.armed or not n.active
                or n.planner is None or n.strategy != 'hold' or n.tec_ff_paused
                or n.current_target != n.target or self._busy()):
            raise BrokerError('ownership', 'Thermal study needs an idle unpaused hold controller')
        if (n.p.get('tec_center_reference') == 'activation'
                and (n.tec_reference_lease != n.o.lease or not n.tec_reference_confirmed
                     or n.tec_reference_pending)):
            raise BrokerError('ownership', 'Thermal study needs the confirmed current PZT center')
        if not isinstance(candidate, dict) or not candidate or set(candidate) - FIELDS:
            raise BrokerError('configuration', 'Only TEC gain and supervision interval may be studied')
        if any(not _number(value) or value <= 0 for value in candidate.values()):
            raise BrokerError('range', 'Thermal study parameters must be finite positive numbers')
        try:
            policy = owner_policy(n.o.device, 'slow_loop')
            values = {key: candidate.get(key, n.p[key]) for key in FIELDS}
            for key, value in values.items():
                bounds = policy['bounds'].get(key)
                if (bounds is None or not bounds[0] <= value <= bounds[1]
                        or not bounds[0] <= n.p[key] <= bounds[1]):
                    raise ValueError('Candidate and baseline must be inside approved ' + key + ' bounds')
            override = n.parameter_overrides.get('tec_gain')
            if override is not None and values['tec_gain'] > override:
                raise ValueError('Thermal study exceeds the explicit invocation gain ceiling')
            profile = {**copy.deepcopy(n.p), **values}
            validate_profile(profile, n.strategy)
            context = self._context()
            revision = self._revision(context)
            if outputs is None or any(not _number(outputs.get(axis)) for axis in ('current', 'temperature')):
                raise ValueError('Verified current/TEC readbacks are required for the study envelope')
            anchor = (self._record['output_anchor'] if self.active
                      else {axis: outputs[axis] for axis in ('current', 'temperature')})
            physical_bounds = {}
            for axis in ('current', 'temperature'):
                limits = n.planner.policies[axis]
                span = policy['max_excursion'][axis]
                physical_bounds[axis] = [max(limits.low, anchor[axis] - span),
                                         min(limits.high, anchor[axis] + span)]
                if not physical_bounds[axis][0] - 1e-12 <= outputs[axis] <= physical_bounds[axis][1] + 1e-12:
                    raise ValueError('Actual output is outside the admitted study ' + axis + ' excursion')
        except (KeyError, TypeError, ValueError) as error:
            raise BrokerError('configuration', str(error)) from error
        if self.active and revision != self._record['context_revision']:
            raise BrokerError('configuration', 'Thermal study base context changed; old scope is revoked')
        return dict(parameters=values, baseline={key: n.p[key] for key in FIELDS},
                    authorized_bounds={key: copy.deepcopy(policy['bounds'][key]) for key in FIELDS},
                    output_anchor=copy.deepcopy(anchor), physical_bounds=physical_bounds,
                    context_revision=revision)

    def apply(self, session, candidate, *, outputs):
        """Bind a preflighted candidate to the just-confirmed FF session."""
        checked = self.validate(candidate, outputs=outputs)
        key = self._session_key(session)
        now = self.native.o.clock()
        if key is None or not now < session['expires_at'] <= now + 900:
            raise BrokerError('ownership', 'A live bounded target-response session is required')
        if self.active and self._record['session_key'][0] != key[0]:
            raise BrokerError('ownership', 'Another target-response token owns the thermal overlay')
        n = self.native
        effective = {**copy.deepcopy(n.p), **checked['parameters']}
        record = dict(checked, session_key=key, expires_at=session['expires_at'])
        # Validate all data before touching even the owner-generated proposal queue.
        n.discard_proposals('target-response thermal parameters changed')
        self._record = record
        self._planner = n.planner
        self._effective = effective
        n.planner.p = effective
        n.last_supervision = now
        n.o.audit('target_response_thermal_applied', parameters=checked['parameters'],
                  context_revision=checked['context_revision'], hardware_write=False)
        return self.status()

    def permit_output(self, axis, value, *, outputs=None):
        """The approved excursion is an extra ceiling, never a hardware proof."""
        if not self.active:
            return True
        bounds = self._record['physical_bounds']
        if axis not in bounds or not _number(value):
            return False
        if outputs is not None and any(not _number(outputs.get(name))
                or not low - 1e-12 <= outputs[name] <= high + 1e-12
                for name, (low, high) in bounds.items()):
            return False
        low, high = bounds[axis]
        return low - 1e-12 <= value <= high + 1e-12

    def sync(self, session):
        """Revoke expired/lost scope. True means pending proposals were discarded."""
        if not self.active:
            return False
        n = self.native
        reason = None
        if n.o.fault:
            reason = 'owner_fault'
        elif not n.armed or not n.active:
            reason = 'controller_stopped'
        elif n.o.clock() >= self._record['expires_at']:
            reason = 'expired'
        elif self._session_key(session) != self._record['session_key']:
            reason = 'study_ended_or_superseded'
        elif n.planner is not self._planner:
            reason = 'planner_replaced'
        elif n.planner.p is not self._effective:
            reason = 'profile_view_replaced'
        elif self._busy():
            reason = 'foreground_operation'
        else:
            try:
                if self._revision(self._context()) != self._record['context_revision']:
                    reason = 'scope_changed'
                elif self._effective != {**n.p, **self._record['parameters']}:
                    reason = 'effective_profile_changed'
            except (KeyError, TypeError, ValueError, AttributeError):
                reason = 'scope_unavailable'
        if reason is not None:
            return self.clear(reason)
        return False

    def clear(self, reason):
        """Restore the current base view, never historical settings or outputs."""
        if not self.active:
            return False
        n = self.native
        record = self._record
        if n.planner is not None:
            n.planner.p = n.p
        n.discard_proposals('target-response thermal policy ended: ' + str(reason))
        n.last_supervision = n.o.clock()
        self.last = dict(reason=str(reason), context_revision=record['context_revision'],
                         parameters_restored=True, physical_outputs_restored=False,
                         target_restored=False, hardware_write=False)
        self._record = self._planner = self._effective = None
        n.o.audit('target_response_thermal_restored', **self.last)
        return True

    def status(self):
        """Report only; callers sync against TargetFFStudy before publication."""
        n = self.native
        base = getattr(n, 'p', None) or n.o.device['control_settings'].get('steady_lock', {})
        profile = n.planner.p if n.planner is not None else base
        result = dict(active=self.active, parameters={key: profile[key] for key in FIELDS if key in profile},
                      baseline={key: base[key] for key in FIELDS if key in base}, last=copy.deepcopy(self.last),
                      runtime_only=True, physical_checkpoint_return=False)
        try:
            if set(result['baseline']) != FIELDS:
                raise ValueError('Stationary thermal profile is unavailable')
            policy = owner_policy(n.o.device, 'slow_loop')
            bounds = {key: copy.deepcopy(policy['bounds'][key]) for key in FIELDS}
            ceiling = n.parameter_overrides.get('tec_gain')
            if ceiling is not None:
                bounds['tec_gain'][1] = min(bounds['tec_gain'][1], ceiling)
            result.update(supported=True, authorized_bounds=bounds)
        except (KeyError, TypeError, ValueError) as error:
            result.update(supported=False, authorized_bounds={}, reason=str(error))
        if self.active:
            result.update(expires_at=self._record['expires_at'],
                          context_revision=self._record['context_revision'],
                          output_anchor=copy.deepcopy(self._record['output_anchor']),
                          physical_bounds=copy.deepcopy(self._record['physical_bounds']))
        return result
