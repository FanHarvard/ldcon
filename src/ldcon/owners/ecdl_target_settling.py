"""Read-only ECDL target completion: TEC must restore the active PZT center."""
import hashlib
import json
import math


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def center_dac(profile, policy, reference):
    return (reference if profile.get('tec_center_reference') == 'activation'
            else policy.low + profile['tec_center_fraction'] * (policy.high - policy.low))


def required(laser):
    binding = laser.get('interfaces', {}).get('lock', {})
    return laser.get('devices', {}).get(binding.get('device'), {}).get('type') == 'ecdl.rig.owner'


def centering_context(native):
    """No-I/O fixed study criterion, independent of own targets and TEC writes."""
    if native.planner is None:
        return dict(status='unavailable', reference_confirmed=False,
                    reason='No active owner planner supplies the PZT center')
    profile = native.p
    policies = native.planner.policies
    piezo, tec = policies['piezo'], policies['temperature']
    center = center_dac(profile, piezo, native.planner.tec_reference_dac)
    owner = native.o
    confirmed = bool(owner.lease and native.tec_reference_lease == owner.lease
                     and (profile.get('tec_center_reference') != 'activation'
                          or native.tec_reference_confirmed))
    tolerance = max(piezo.resolution, abs(profile['counts_per_temperature_unit'])
                    * tec.resolution / (2. * profile['tec_gain']))
    confirmation = max(profile['supervision_interval_s'], tec.update_interval_s)
    authority = getattr(owner, 'policy_authority', None)
    identity = dict(lease=owner.lease, base_profile=profile, center=center,
                    reference_confirmed=confirmed, tolerance=tolerance,
                    confirmation=confirmation, low=piezo.low, high=piezo.high,
                    resolution=piezo.resolution,
                    policy_revision=getattr(authority, 'revision', None))
    return dict(status='available' if confirmed else 'unavailable', center_dac=center,
                reference_confirmed=confirmed,
                reference_revision=hashlib.sha256(json.dumps(identity, sort_keys=True,
                    allow_nan=False).encode()).hexdigest(),
                resolution_count=piezo.resolution, tolerance_count=tolerance,
                confirmation_s=confirmation, low_dac=piezo.low, high_dac=piezo.high,
                supervision_interval_s=profile['supervision_interval_s'],
                basis='Frozen base-profile TEC deadband and confirmation; center is not a study variable')


def owner_evidence(native, state, plan):
    """Use the actual DAC readback, not the FF offset or the integral alone.

    The existing TEC planner stops proposing corrections below half a TEC
    output quantum. Express that same deadband in PZT counts. No physical
    command, gain, output limit or planning interval is changed here.
    """
    owner = native.o
    # The planner may use a narrower activation envelope than owner.policies().
    # Its effective midpoint and hot-updated reference are the control truth.
    # A temporary gain/cadence trial must not widen its own success band or
    # shorten the normal target-completion confirmation to manufacture success.
    overlay = getattr(native, 'thermal_study', None)
    profile = native.p if overlay is not None and overlay.active else native.planner.p
    policies = native.planner.policies
    piezo, tec = policies['piezo'], policies['temperature']
    center = center_dac(profile, piezo, native.planner.tec_reference_dac)
    confirmed = bool(owner.lease and native.tec_reference_lease == owner.lease
                     and (profile.get('tec_center_reference') != 'activation'
                          or native.tec_reference_confirmed))
    result = dict(status='unavailable', reference_confirmed=confirmed,
                  observed_unix_s=state.get('read_started_unix_s'),
                  pzt_dac=state['outputs'].get('piezo'), center_dac=center,
                  max_age_s=owner.device['control_settings']['max_sample_age_s'],
                  target_frequency_hz=native.current_target,
                  basis='TEC correction quantization and one outer-loop interval')
    if not confirmed or not finite(center):
        return dict(result, reason='TEC center reference is not confirmed for the current lease')
    # One DAC quantum also covers a midpoint halfway between integer counts.
    tolerance = max(piezo.resolution, abs(profile['counts_per_temperature_unit'])
                    * tec.resolution / (2. * profile['tec_gain']))
    confirmation = max(profile['supervision_interval_s'], tec.update_interval_s)
    if not finite(tolerance) or tolerance <= 0 or not finite(confirmation) or confirmation <= 0:
        return dict(result, reason='TEC resolution or confirmation interval is unavailable')
    # A center/gain/lease or TEC-write change invalidates accumulated evidence.
    identity = dict(lease=owner.lease, center=center, tolerance=tolerance,
                    confirmation=confirmation, tec_write=owner.last_write.get('temperature'),
                    gain=profile['tec_gain'], counts_per_temperature_unit=profile['counts_per_temperature_unit'],
                    target=native.current_target)
    result.update(tolerance_count=tolerance, confirmation_s=confirmation,
                  context_revision=hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest())
    if native.tec_ff_paused:
        return dict(result, status='paused', reason='TEC outer loop is paused')
    cfg = state['servo_config']
    # steady_read may write a target after taking its snapshot. That pre-write
    # DAC/frequency sample must never confirm the newly labelled target.
    if (cfg.get('pid_multiplier') != 1 or not finite(cfg.get('target')) or cfg['target'] <= 0
            or abs(299792458e9 / cfg['target'] - native.current_target) > 1.):
        return dict(result, reason='Awaiting a PI-on readback of the applied target')
    if not finite(result['pzt_dac']):
        return dict(result, reason='Actual PZT DAC readback is unavailable')
    result['error_count'] = result['pzt_dac'] - center
    if getattr(native.planner, 'ff_target', None) is not None:
        return dict(result, status='centering', reason='TEC current feedforward is pending')
    if any(action.get('axis') == 'temperature' for action in plan.get('actions', [])):
        return dict(result, status='centering', reason='A TEC correction is pending')
    if abs(result['error_count']) >= tolerance:
        return dict(result, status='centering', reason='PZT has not returned to the TEC center',
                    blocked=plan.get('blocked', []))
    return dict(result, status='centered', reason='PZT is within the TEC correction deadband')


class Completion:
    """Confirm sustained centering using distinct fresh owner observations."""
    def __init__(self):
        self.first = self.last = self.context = None
        self.samples = 0

    def reset(self):
        self.first = self.last = self.context = None
        self.samples = 0

    def poll(self, state, *, accepted_unix_s, now, at_target):
        evidence = state.get('tec_centering')
        if not isinstance(evidence, dict):
            self.reset()
            return dict(status='waiting', reason='Owner/worker TEC centering evidence is unavailable')
        result = dict(evidence, status='waiting', confirmed_s=0., samples=0)
        stamp = evidence.get('observed_unix_s')
        valid = all(finite(evidence.get(key)) for key in (
            'observed_unix_s', 'max_age_s', 'pzt_dac', 'center_dac', 'tolerance_count',
            'confirmation_s', 'target_frequency_hz'))
        valid = (valid and evidence['max_age_s'] > 0 and evidence['tolerance_count'] > 0
                 and evidence['confirmation_s'] > 0 and evidence.get('context_revision')
                 and evidence.get('reference_confirmed') is True)
        if not valid:
            self.reset()
            return dict(result, reason=evidence.get('reason', 'Incomplete TEC centering evidence'))
        if not accepted_unix_s <= stamp <= now or now - stamp > evidence['max_age_s']:
            self.reset()
            return dict(result, reason='Awaiting fresh post-request PZT readback')
        if (not at_target or abs(evidence['target_frequency_hz'] - state['ramped_target_hz']) > 1e-3
                or evidence.get('status') != 'centered'
                or abs(evidence['pzt_dac'] - evidence['center_dac']) >= evidence['tolerance_count']):
            self.reset()
            return dict(result, reason=evidence.get('reason', 'PZT is not centered at the applied target'))
        context = (evidence['context_revision'], evidence['center_dac'], evidence['tolerance_count'],
                   evidence['confirmation_s'], evidence['target_frequency_hz'])
        if (context != self.context or self.last is not None
                and (stamp < self.last or stamp - self.last > evidence['max_age_s'])):
            self.reset()
        if self.first is None:
            self.first = stamp
            self.context = context
        if self.last is None or stamp > self.last:
            self.samples += 1
            self.last = stamp
        duration = self.last - self.first
        settled = self.samples >= 2 and duration >= evidence['confirmation_s']
        return dict(result, status='settled' if settled else 'confirming', confirmed_s=duration,
                    samples=self.samples, reason='TEC center confirmed' if settled else 'Confirming PZT remains centered')
