"""Owner-side long-lock policies, expressed in configured axis units."""
import math
import numpy as np
from ldcon.core.contracts import MeasurementUnavailable


WINDOW_QUALITY_FIELDS = frozenset(('warning_response', 'edge_response'))


def validate_window_quality(window):
    """Validate supplied quality thresholds even when the controller holds current."""
    if not isinstance(window, dict):
        raise ValueError('Window profile must be a mapping')
    for key in WINDOW_QUALITY_FIELDS & window.keys():
        value = window[key]
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError('Window quality threshold must be a positive finite number: ' + key)


def validate_profile(profile,strategy='hold',*,require_handover=True):
    required=('revision','native_handover_verified','firmware_version','native_input',
              'p_sample','g_sample','wavelength_nm_per_count','native_interval_bounds_s',
              'capture_hz','integral_limit_count','target_slew_hz_per_s','watchdog_s',
              'supervision_interval_s','current_span',
              'tec_center_fraction','counts_per_temperature_unit','tec_gain','tec_dt_cap_s')
    if not isinstance(profile,dict) or any(k not in profile for k in required):
        raise ValueError('Stationary ECDL device profile is incomplete')
    validate_window_quality(profile.get('window', {}))
    for key in required:
        if key in ('revision','firmware_version','native_input','native_handover_verified','native_interval_bounds_s'):continue
        value=profile[key]
        if type(value) not in (int,float) or not math.isfinite(value):raise ValueError('Invalid stationary '+key)
        if key!='wavelength_nm_per_count' and value<=0:raise ValueError('Nonpositive stationary '+key)
    if profile['wavelength_nm_per_count']>=0:raise ValueError('This native PI adapter requires negative wavelength/DAC slope')
    if type(profile.get('target_feedforward_enabled',False)) is not bool:
        raise ValueError('target_feedforward_enabled must be boolean')
    from ldcon.owners.ecdl_target_ff import study_policy,ldc_policy
    study_policy(profile)
    ldc_policy(profile)   # target-ff design (2026-10-27): the optional LDC target-FF term (default off)
    if not 0<profile['tec_center_fraction']<1:raise ValueError('Invalid stationary PZT center')
    dither=profile.get('dither')
    if dither is not None and not isinstance(dither,dict):raise ValueError('Dither profile must be a mapping')
    if isinstance(dither,dict) and type(dither.get('paused',False)) is not bool:raise ValueError('dither.paused must be boolean')
    bounds=profile['native_interval_bounds_s']
    if len(bounds)!=2 or not 0<bounds[0]<bounds[1]:raise ValueError('Invalid native input interval bounds')
    # The worker heartbeats each acquisition cycle; the owner's independent
    # watchdog must not be tied to the optional slow TEC-planning interval.
    # A long slow-loop interval never extends the watchdog deadline.
    retained=profile.get('retained_tec_reference_dac')
    if retained is not None and (type(retained) not in (int,float) or not math.isfinite(retained)):raise ValueError('Invalid retained TEC reference')
    gate=profile.get('entry_gate')
    if gate is not None:
        if not isinstance(gate,dict):raise ValueError('Entry gate must be a mapping')
        # Legacy mode_id and branch_tolerance_hz are accepted but have no admission authority.
        limits=gate.get('metrics_max')
        if not isinstance(limits,dict) or not limits:raise ValueError('Entry gate metrics required')
        for key,value in limits.items():
            if not isinstance(key,str) or type(value) not in (int,float) or not math.isfinite(value) or value<=0:raise ValueError('Invalid entry metric threshold')
    for section in ('entry_gate','current_safety'):
        configuration=profile.get(section)
        limits=configuration.get('metrics_max',{}) if isinstance(configuration,dict) else {}
        if isinstance(limits,dict) and set(limits)&{'A','B','C','F2'}:raise ValueError('Retired fixed-window/fringe-fill metric in '+section+'; configure position-free metrics')
    hot=profile.get('hot_control')
    if hot is not None:
        if not isinstance(hot,dict):raise ValueError('Invalid hot control policy')
        if 'dither_max' in hot and (type(hot['dither_max']) not in (int,float) or not math.isfinite(hot['dither_max']) or hot['dither_max']<=0):raise ValueError('Invalid hot control dither guard')
        # Legacy profiles may still pause TEC; local trim requires a complete
        # explicit policy at its own entry point and never borrows a default.
        if any(key in hot for key in ('max_trim_v','max_step_v','minimum_dwell_s')):
            from ldcon.owners.ecdl_hot_control import trim_policy
            trim_policy(profile)
        if type(hot.get('initial_tec_ff_paused',False)) is not bool:raise ValueError('Invalid initial TEC pause')
    follow=profile.get('tec_current_feedforward')
    if follow is not None:
        if not isinstance(follow,dict):raise ValueError('Invalid TEC current feedforward')
        for key in ('current_per_temperature',):
            if type(follow.get(key)) not in (int,float) or not math.isfinite(follow[key]):raise ValueError('Invalid TEC current feedforward '+key)
        # target-ff design (human decision 3, 2026-10-27): the coefficient is a free operating parameter; the measured
        # sr422 window drift is NEGATIVE (-0.07 V/V), so the sign is no longer prescribed here (0 disables the pair).
        if follow['current_per_temperature']==0:raise ValueError('Zero TEC current feedforward; remove the block to disable the pair')
    safety=profile.get('current_safety')
    if safety is not None:
        if not isinstance(safety,dict) or not isinstance(safety.get('metrics_max'),dict) or not safety['metrics_max']:raise ValueError('Current safety metrics required')
        # This historical key supplies quality thresholds to explicit trim cards only.
        # The steady controller never interprets decrement/direction/step as an action.
        for value in safety['metrics_max'].values():
            if type(value) not in (int,float) or not math.isfinite(value) or value<=0:raise ValueError('Invalid current quality threshold')
    if strategy!='hold':
        raise ValueError('Hidden current-window strategy is retired; select a separate adjustment card')
    if require_handover and profile['native_handover_verified'] is not True:
        raise ValueError('Native PI start/stop DAC handover has not been commissioned for this firmware')
    return profile


def validate_dither_profile(profile):
    """Require a commissioned passive fit source without a traversal algorithm."""
    validate_profile(profile,'hold')
    d=profile.get('dither',{})
    if d.get('verified') is not True:
        raise ValueError('Effective existing dither and telemetry have not been verified')
    for key in ('frequency_hz','effective_amplitude_peak','window_s','minimum_samples'):
        if type(d.get(key)) not in (int,float) or not math.isfinite(d[key]) or d[key]<=0:
            raise ValueError('Dither profile missing '+key)
    if not d.get('telemetry_endpoint') or not d.get('selector'):
        raise ValueError('Dither telemetry source is unset')
    return d


def control_ready(observation, profile, target, *, mode_id=None, tolerance_hz=None):
    """Valid frequency plus any explicit capture tolerance; branch maps are descriptive.

    The owner has already checked current-cycle input, hardware state and lease.
    Capture and output-envelope checks still apply separately before PI enable.
    """
    frequency = observation.get('frequency', {}).get('frequency_hz')
    if (type(frequency) not in (int, float) or not math.isfinite(frequency) or frequency <= 0
            or type(target) not in (int, float) or not math.isfinite(target) or target <= 0):
        return False
    # Branch labels and the former broad (700 MHz) preflight range are records,
    # not lock admission criteria. Only an explicitly requested capture/recovery
    # tolerance applies here; PI arm separately enforces profile.capture_hz.
    return tolerance_hz is None or (
        type(tolerance_hz) in (int, float) and math.isfinite(tolerance_hz)
        and tolerance_hz > 0 and abs(frequency - target) <= tolerance_hz)


def entry_quality(observation,profile,target):
    """Configured acquisition gate, separate from post-lock fine classification."""
    gate=profile.get('entry_gate')
    if gate is None:return bool(observation['single_mode'] and not observation['maintenance_required'])
    # v5.1-hostcheck: quality_valid is an informational label, not part of the gate (human decision 2026-10-22).
    if not control_ready(observation,profile,target):return False
    metrics=observation.get('metrics',{})
    return all(type(metrics.get(k)) in (int,float) and math.isfinite(metrics[k]) and metrics[k]<limit for k,limit in gate['metrics_max'].items())


def adjustment_ready(observation, profile, target):
    """Permit quality correction without an implicit classification veto.

    Explicit quality criteria for this adjustment remain independently enforced. A poor quality
    label is the reason to assess correction, not a physical safety violation.
    """
    return control_ready(observation, profile, target) and (
        entry_quality(observation, profile, target) if profile.get('entry_gate') is not None else True)


def dither_response(times,dac,config,slope):
    """Passive constant + drift + sine + cosine fit; no waveform writes."""
    try:t=np.asarray(times,float);y=np.asarray(dac,float)
    except (ValueError,TypeError) as error:
        raise MeasurementUnavailable('Invalid dither samples') from error
    if len(t)<max(50,config['minimum_samples']) or len(t)!=len(y):raise MeasurementUnavailable('Insufficient dither samples')
    if not np.isfinite(t).all() or not np.isfinite(y).all() or not np.all(np.diff(t)>0):raise MeasurementUnavailable('Invalid dither acquisition times')
    x=t-t[0];frequency=config['frequency_hz']
    if x[-1]<max(8.,3/frequency) or np.max(np.diff(t))>1/(4*frequency):raise MeasurementUnavailable('Dither window is short or has gaps')
    phase=2*np.pi*frequency*x
    matrix=np.column_stack((np.ones_like(x),x,np.sin(phase),np.cos(phase)))
    if np.linalg.matrix_rank(matrix)<4 or np.linalg.cond(matrix)>1e6:raise MeasurementUnavailable('Unresolved dither fit')
    coeff=np.linalg.lstsq(matrix,y,rcond=None)[0]
    # Response is wavelength-nm / configured current-axis unit. Device edge
    # thresholds must use this same unit, never AWG nominal AMP as calibration.
    return float(np.hypot(coeff[2],coeff[3])*abs(slope)/config['effective_amplitude_peak'])


class SteadyPlanner:
    def __init__(self,profile,policies,origin,strategy,*,tec_reference_dac=None):
        if strategy!='hold':raise ValueError('Hidden current-window strategy is retired')
        self.p=profile;self.policies=policies;self.origin=origin;self.strategy=strategy
        self.tec_reference_dac=origin['piezo'] if tec_reference_dac is None else tec_reference_dac
        self.ff_temperature=origin['temperature'];self.ff_target=None;self.ff_remainder=0.
    def rebase_ff(self,outputs):
        # Pause/resume or explicit trim is a new incremental origin, no catch-up.
        self.ff_temperature=outputs['temperature'];self.ff_target=None;self.ff_remainder=0.
    def ack_current(self,outputs,reason):
        if reason!='TEC current feedforward':
            self.rebase_ff(outputs)
        elif self.ff_target is not None and abs(outputs['current']-self.ff_target)<=max(self.policies['current'].resolution/2,1e-12):
            self.ff_target=None
    def plan(self,now,elapsed,state,metrics,response=None):
        p=self.p;current=state['outputs']['current'];tec=state['outputs']['temperature'];dac=state['outputs']['piezo']
        cp=self.policies['current'];low=max(cp.low,self.origin['current']-p['current_span']);high=min(cp.high,self.origin['current']+p['current_span'])
        # Spectral quality is diagnostic, not a veto on PZT headroom recovery.
        # Finish any verified TEC compensation before planning another TEC step.
        thermal_ready=state.get('tec_control_ready',not state.get('optical_maintenance_required',True))
        follow=p.get('tec_current_feedforward')
        if follow and not state.get('tec_ff_paused') and thermal_ready:
            if self.ff_target is None:
                raw=current+follow['current_per_temperature']*(tec-self.ff_temperature)+self.ff_remainder
                desired=round(raw/cp.resolution)*cp.resolution if cp.resolution else raw
                self.ff_remainder=raw-desired;self.ff_temperature=tec
                if abs(desired-current)>=max(cp.resolution/2,1e-12):self.ff_target=desired
            desired=current if self.ff_target is None else self.ff_target
            if not low<=desired<=high:
                return dict(actions=[],blocked=['TEC current feedforward outside approved current span'],needs_attention=True)
            step=min(cp.max_step,cp.max_slew_per_s*min(elapsed,cp.update_interval_s))
            if abs(desired-current)>=max(cp.resolution/2,1e-12):
                value=current+max(-step,min(step,desired-current))
                return dict(actions=[dict(axis='current',value=value,reason='TEC current feedforward')],blocked=[])
        if not state.get('tec_ff_paused') and thermal_ready:
            pp=self.policies['piezo'];tp=self.policies['temperature']
            from ldcon.owners.ecdl_target_settling import center_dac
            center=center_dac(p,pp,self.tec_reference_dac)
            delta=p['tec_gain']*(dac-center)/p['counts_per_temperature_unit']
            # Plan a destination; the owner delivers it in slew-derived slices.
            # Legacy profiles retain their explicit step cap via the default.
            step=tp.max_slew_per_s*min(elapsed,p['tec_dt_cap_s'])
            if not state.get('tec_rate_only',False):step=min(tp.max_step,step)
            value=tec+max(-step,min(step,delta))
            if tp.low<=value<=tp.high and abs(value-tec)>=tp.resolution/2:
                return dict(actions=[dict(axis='temperature',value=value,reason='PZT slow centering')],
                            blocked=[],dither_response=response)
        return dict(actions=[],blocked=[],dither_response=response)
