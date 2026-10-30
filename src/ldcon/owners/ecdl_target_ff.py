"""Pure PZT target-feedforward proof; calibration and bounds belong to the laser."""
import math
import copy
import hashlib
import json
import uuid
from ldcon.core.contracts import DeviceError
from ldcon.core.device_conditions import finite

C_NM = 299792458e9
DEFAULT_TARGET_RAMP_STEP_HZ = 100e6


def study_policy(profile):
    """Pure target-change policy; no permission is borrowed from TEC/ADC FF."""
    scale = profile.get('target_feedforward_scale', 1.)
    step = profile.get('target_ramp_step_hz', DEFAULT_TARGET_RAMP_STEP_HZ)
    limits = profile.get('target_feedforward_scale_bounds', [scale, scale])
    if (any(type(x) not in (int, float) or not math.isfinite(x) or x <= 0
            for x in (scale, step)) or not isinstance(limits, list) or len(limits) != 2
            or any(type(x) not in (int, float) or not math.isfinite(x) or x <= 0 for x in limits)
            or not limits[0] <= scale <= limits[1]):
        raise ValueError('Invalid target feedforward scale, authorized bounds or target ramp step')
    return dict(scale=float(scale), step_hz=float(step), scale_bounds=list(limits))


class TargetFFStudy:
    """Lease/revision-scoped temporary choices, consumed only with their token.

    Expiration and restore change memory only. An already admitted target ramp
    owns its immutable settings and finishes under its existing output proofs.
    """
    def __init__(self, clock):
        self.clock = clock
        self.sequence = 0
        self.context = None
        self.session = None

    def status(self, lease, profile, policy_revision):
        defaults = study_policy(profile)
        context = (lease, str(policy_revision), json.dumps(defaults, sort_keys=True))
        if context != self.context:
            self.context = context
            self.session = None
            self.sequence += 1
        if self.session and self.clock() >= self.session['expires_at']:
            self.session = None
            self.sequence += 1
        identity = hashlib.sha256(repr(context).encode()).hexdigest()[:20]
        session = copy.deepcopy(self.session)
        return dict(revision=str(self.sequence)+':'+identity, active=session is not None,
                    defaults={k: defaults[k] for k in ('scale', 'step_hz')},
                    authorized_scale_bounds=defaults['scale_bounds'],
                    maximum_step_hz=defaults['step_hz'], session=session)

    def set(self, request, lease, profile, policy_revision):
        from ldcon.core.instrument_client import BrokerError
        status = self.status(lease, profile, policy_revision)
        if request.get('expected_revision') != status['revision']:
            raise BrokerError('configuration', 'Target FF study revision changed')
        if self.session and request.get('study_token') != self.session['study_token']:
            raise BrokerError('ownership', 'Exact target FF study token required')
        if self.session is None and request.get('study_token') is not None:
            raise BrokerError('ownership', 'Target FF study token expired or was superseded')
        scale, step, duration = (request.get(k) for k in ('scale', 'step_hz', 'duration_s'))
        if any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in (scale, step, duration)):
            raise BrokerError('range', 'Finite positive target FF scale, step and duration required')
        low, high = status['authorized_scale_bounds']
        if not low <= scale <= high:
            raise BrokerError('range', 'Target FF scale requires its own approved owner bounds')
        if step > status['maximum_step_hz'] or duration > 900:
            raise BrokerError('range', 'Target FF step exceeds owner ceiling or duration exceeds 900 seconds')
        self.session = dict(study_token=self.session['study_token'] if self.session else uuid.uuid4().hex,
                            scale=float(scale), step_hz=float(step), expires_at=self.clock()+duration)
        self.sequence += 1
        return self.status(lease, profile, policy_revision)

    def restore(self, request, lease, profile, policy_revision):
        from ldcon.core.instrument_client import BrokerError
        status = self.status(lease, profile, policy_revision)
        if request.get('expected_revision') != status['revision']:
            raise BrokerError('configuration', 'Target FF study revision changed')
        if not self.session or request.get('study_token') != self.session['study_token']:
            raise BrokerError('ownership', 'Exact active target FF study token required')
        self.session = None
        self.sequence += 1
        return self.status(lease, profile, policy_revision)

    def settings(self, token, lease, profile, policy_revision):
        from ldcon.core.instrument_client import BrokerError
        status = self.status(lease, profile, policy_revision)
        if token is None:
            # A foreground target must never inherit an experiment coefficient.
            if self.session:
                self.session = None
                self.sequence += 1
            return status['defaults']
        if not self.session or token != self.session['study_token']:
            raise BrokerError('ownership', 'Target FF study token expired or was superseded')
        return {k:self.session[k] for k in ('scale', 'step_hz')}


LDC_DEFAULTS = dict(v_per_hz=0., max_abs_v=.008, bounds_v_per_hz=[-2e-11, 2e-11], hz_per_v=None)


def ldc_policy(profile):
    """The LDC (current) target-feedforward term of the ECDL (target-ff design, human decision 4, 2026-10-27).

    ``steady_lock.ldc_target_feedforward: {v_per_hz, max_abs_v, bounds_v_per_hz, hz_per_v}``. ``v_per_hz`` (free) is
    the current deviation per Hz of requested shift, clamped to ``max_abs_v`` (safety: the clean-window half width)
    and admitted only inside ``bounds_v_per_hz`` (safety). ``hz_per_v`` (free, measured) converts the realised current
    move into the frequency share subtracted from the PZT term. Absent block or ``v_per_hz == 0``: the term is off."""
    block = profile.get('ldc_target_feedforward')
    if block is None:
        return dict(LDC_DEFAULTS, enabled=False)
    if not isinstance(block, dict) or set(block) - set(LDC_DEFAULTS):
        raise ValueError('ldc_target_feedforward accepts v_per_hz, max_abs_v, bounds_v_per_hz and hz_per_v only')
    value = {**LDC_DEFAULTS, **block}
    k = value['v_per_hz']
    limits = value['bounds_v_per_hz']
    if (type(k) not in (int, float) or not math.isfinite(k) or not isinstance(limits, list) or len(limits) != 2
            or any(type(x) not in (int, float) or not math.isfinite(x) for x in limits) or not limits[0] <= k <= limits[1]):
        raise ValueError('ldc_target_feedforward.v_per_hz must be finite and inside bounds_v_per_hz')
    cap = value['max_abs_v']
    if type(cap) not in (int, float) or not math.isfinite(cap) or cap <= 0:
        raise ValueError('ldc_target_feedforward.max_abs_v must be a positive voltage')
    sensitivity = value['hz_per_v']
    if k != 0 and (type(sensitivity) not in (int, float) or not math.isfinite(sensitivity) or sensitivity == 0):
        raise ValueError('ldc_target_feedforward.hz_per_v (finite, nonzero current->frequency sensitivity) is required when v_per_hz is nonzero')
    return dict(v_per_hz=float(k), max_abs_v=float(cap), bounds_v_per_hz=[float(limits[0]), float(limits[1])],
                hz_per_v=None if sensitivity is None else float(sensitivity), enabled=k != 0)


def ldc_desired(ldc, origin_current, delta_hz, resolution=0.):
    """The LDC current for a total requested shift ``delta_hz`` from the ramp origin: origin + clamp(k * delta)."""
    deviation = max(-ldc['max_abs_v'], min(ldc['max_abs_v'], ldc['v_per_hz'] * delta_hz))
    value = origin_current + deviation
    if resolution:
        value = round(value / resolution) * resolution
    return value


def ldc_share_hz(ldc, origin_current, actual_current):
    """The frequency the LDC term REALISED (actual current move x sensitivity): subtracted from the PZT share."""
    if not ldc.get('enabled') or ldc.get('hz_per_v') is None:
        return 0.
    return (actual_current - origin_current) * ldc['hz_per_v']


def headroom(profile, state, origin, bounds):
    """Report-only PZT room for the next target step at the present PI centre (target-ff design, 2026-10-27).

    The same envelope terms as ``plan`` (proportional over the acceptance band, integral limits, the held DAC) at the
    CURRENT offset are compared with the admitted window; the remaining counts become Hz through the calibrated slope
    and the FF scale. Returns ``{up_hz, down_hz, up_counts, down_counts, bounds_count, envelope_count}``."""
    cfg = state['servo_config']
    slope = finite(origin['wavelength_nm_per_count'])
    target_nm = finite(cfg['target'])
    scale = finite(origin.get('target_feedforward_scale', 1.))
    offset = finite(cfg['control_offset'])
    band = finite(profile['capture_hz'])
    target_hz = C_NM / target_nm
    system = [C_NM/(target_hz+band), C_NM/(target_hz-band)]
    proportional = sorted(finite(cfg['pid_kp'])*(x-target_nm) for x in system)
    integral = sorted(finite(cfg['pid_ki'])*finite(cfg[k]) for k in ('pid_integral_min','pid_integral_max'))
    held = finite(state['outputs']['piezo'])
    low_edge = min(offset+proportional[0]+integral[0], held)
    high_edge = max(offset+proportional[1]+integral[1], held)
    lower, upper = bounds
    up_counts = max(0., upper - high_edge)
    down_counts = max(0., low_edge - lower)
    # frequency per count: f = c/lambda, d f = -c/lambda^2 d lambda, d lambda = slope d count (slope < 0 -> up)
    hz_per_count = -C_NM / target_nm ** 2 * slope / max(scale, 1e-9)
    up_hz, down_hz = up_counts * abs(hz_per_count), down_counts * abs(hz_per_count)
    if hz_per_count < 0:
        up_hz, down_hz = down_hz, up_hz
    return dict(up_hz=up_hz, down_hz=down_hz, up_counts=up_counts, down_counts=down_counts,
                bounds_count=list(bounds), envelope_count=[low_edge, high_edge], hz_per_count=hz_per_count)


def plan(profile, state, origin, target_hz, bounds, *, ldc_share_hz=0.):
    cfg = state['servo_config']
    slope = finite(origin['wavelength_nm_per_count'])
    if slope >= 0:
        raise DeviceError('Target feedforward requires the commissioned negative wavelength/DAC slope')
    if any(cfg.get(k) != 0 for k in ('multiplier_control','multiplier_target','software_modulation_enable','pid_kd')):
        raise DeviceError('Target feedforward requires the unmodulated commissioned PI configuration')
    target_nm = C_NM/finite(target_hz)
    scale = finite(origin.get('target_feedforward_scale', 1.))
    if scale <= 0:
        raise DeviceError('Target feedforward scale must be positive')
    # The PZT carries the requested shift minus the share the LDC term realised (target-ff design, decision 4).
    pzt_nm = C_NM/finite(target_hz - finite(ldc_share_hz))
    offset = round(origin['offset_count']+scale*(pzt_nm-origin['target_nm'])/slope)
    band = finite(profile['capture_hz'])
    if target_hz <= band:
        raise DeviceError('Target acceptance interval must remain positive')
    system = [C_NM/(target_hz+band), C_NM/(target_hz-band)]
    proportional = sorted(finite(cfg['pid_kp'])*(x-target_nm) for x in system)
    integral = sorted(finite(cfg['pid_ki'])*finite(cfg[k]) for k in ('pid_integral_min','pid_integral_max'))
    envelope = [offset+proportional[0]+integral[0], offset+proportional[1]+integral[1]]
    delta = offset-finite(cfg['control_offset'])
    p_jump = abs(finite(cfg['pid_kp'])*(target_nm-finite(cfg['target'])))
    # Cover the held DAC and either order of target/offset updates. Do not rely
    # on multi-key firmware updates being atomic or on hardware clipping.
    transient = [state['outputs']['piezo']+min(0.,delta)-p_jump,
                 state['outputs']['piezo']+max(0.,delta)+p_jump]
    lower, upper = bounds
    if not all(lower <= x <= upper for x in [offset,*envelope,*transient]):
        raise DeviceError('PZT target feedforward leaves the existing absolute/activation/firmware envelope')
    return dict(values=dict(target=target_nm,system_min=system[0],system_max=system[1],control_offset=offset),
                target_feedforward_scale=scale, ldc_share_hz=float(ldc_share_hz),
                delta_count=delta, configuration_step_bound_count=abs(delta)+p_jump,
                envelope_count=envelope, transient_count=transient, bounds_count=list(bounds))
