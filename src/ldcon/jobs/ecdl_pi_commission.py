"""Bounded native-PI commissioning, owned and evidenced by the rig owner.

Tests fixed-reference disable first, then the non-atomic actual-DAC handover.
Evidence records the observed transitions and configured correction bound; it
never asserts that separate firmware setting updates were atomic.
"""
import math
from ldcon.core.contracts import DeviceError,MeasurementUnavailable
from ldcon.core.interface_cards import CardCancelled
from ldcon.jobs.ecdl_steady import parameters as steady_parameters


def parameters(card,overrides):
    allowed={'target_frequency_hz','tolerance_hz','measurement_timeout_s'}
    if (set(card['parameters'])|set(overrides))-allowed:raise ValueError('Unknown PI commissioning parameter')
    p=steady_parameters(dict(parameters={}),{**card['parameters'],**overrides,'strategy':'hold'})
    return p


def proportional_counts(profile,target):
    """Worst-case proportional contribution (DAC counts) inside the commissioning error band, plus 2 counts."""
    c=299792458e9
    p_gain=profile['p_sample']/abs(profile['wavelength_nm_per_count'])
    max_error_hz=profile['capture_hz']+profile['commissioning']['step_hz']
    max_error_nm=max(abs(c/(target+max_error_hz)-c/target),abs(c/(target-max_error_hz)-c/target))
    return p_gain*max_error_nm+2.


def integral_schedule(profile,target):
    """Progressive integral bound (2026-10-22): the first verified stage keeps the WHOLE PI correction inside
    commissioning.max_correction_count (the policy's blast radius for an unverified PI: a wrong sign or gain can only
    move the PZT that far before the owner sees it); each later stage at most doubles the bound and is entered only
    after the previous one held the target cleanly; the last stage is the approved integral_limit_count."""
    c=profile['commissioning'];approved=float(profile.get('approved_integral_limit_count',profile['integral_limit_count']))
    first=min(approved,math.floor(c['max_correction_count']-proportional_counts(profile,target)))
    if first<1:
        # The proportional term alone exceeds the policy: keep one stage; Trial.add refuses on the first sample
        # ('Total PI correction bound exceeds commissioning policy'), exactly as before the progressive schedule.
        return [float(min(approved,c['max_correction_count']))]
    stages=[float(first)]
    while stages[-1]<approved:stages.append(min(approved,2*stages[-1]))
    return stages


class Trial:
    def __init__(self,profile,target,origin,clock):
        c=profile['commissioning']
        for key in ('hold_s','final_hold_s','step_hz','timeout_s','max_correction_count'):
            if type(c.get(key)) not in (int,float) or not math.isfinite(c[key]) or c[key]<=0:raise DeviceError('Invalid commissioning '+key)
        if c['step_hz']>=profile['capture_hz']/2:raise DeviceError('Commissioning frequency step exceeds capture margin')
        # No refusal for an approved bound above max_correction_count: the bound is raised progressively.
        self.schedule=integral_schedule(profile,target);self.bound_index=0;self.bound_events=[]
        self.p=profile;self.target=target;self.origin=dict(origin);self.clock=clock
        self.started=clock();self.stage=0;self.window=[];self.events=[];self.fixed_stopped=False
        self.current_held=False;self.held_dac=None;self.completed=False;self.max_observed_correction=0.
        self.hold_target=None;self.actual_hold_correction=None
        # Rule K (2026-10-27): single-mode / maintenance labels are recorded here, never a veto on the trial.
        self.quality=dict(single_mode_verified=True,degraded_samples=0,first=None,last=None)
    def duration(self):return self.p['commissioning']['final_hold_s'] if self.stage==5 else self.p['commissioning']['hold_s']
    def status(self):
        span=self.window[-1]['acquired_s']-self.window[0]['acquired_s'] if len(self.window)>1 else 0.
        ready=(span>=self.duration() and len(self.window)>=math.ceil(self.duration())+1)
        return dict(stage=self.stage,ready=ready,valid_samples=len(self.window),valid_span_s=span,
                    required_span_s=self.duration(),completed=self.completed,quality=dict(self.quality))
    def flag_quality(self,row):
        """Record a degraded sample (unverified single mode) and continue; the stage evidence carries the count."""
        q=self.quality;event=dict(stage=self.stage,acquired_s=row['acquired_s'],single_mode=row['single_mode'],
                                  maintenance_required=row.get('maintenance_required',False))
        q.update(single_mode_verified=False,degraded_samples=q['degraded_samples']+1,first=q['first'] or event,last=event)
    def add(self,row,tolerance):
        if self.clock()-self.started>self.p['commissioning']['timeout_s']:raise DeviceError('PI commissioning timed out')
        for axis in ('current','temperature'):
            if abs(row['outputs'][axis]-self.origin[axis])>1e-8:raise DeviceError('Slow output changed during PI commissioning')
        correction=abs(row['outputs']['piezo']-self.origin['piezo'])
        # The proportional term adds a bounded correction to the present (staged) integral cap.
        bound=self.integral_limit()+proportional_counts(self.p,self.target)
        if self.bound_index==0 and bound>self.p['commissioning']['max_correction_count']:raise DeviceError('Total PI correction bound exceeds commissioning policy')
        if correction>bound:raise DeviceError('Native DAC exceeds the configured PI correction bound')
        self.max_observed_correction=max(self.max_observed_correction,correction)
        if not row['single_mode'] or row.get('maintenance_required',False):self.flag_quality(row)
        expected=self.hold_target if self.stage in (4,5) else self.target+(self.p['commissioning']['step_hz'] if self.stage==1 else -self.p['commissioning']['step_hz'] if self.stage==2 else 0.)
        tracking_tolerance=min(tolerance,self.p['commissioning']['step_hz']*.2)
        if abs(row['frequency_hz']-expected)>tracking_tolerance:
            self.window=[];return
        if self.stage==5 and abs(row['outputs']['piezo']-self.held_dac)>1.:raise DeviceError('DAC drifted after native hold')
        stamp=row['acquired_s']
        if self.window and stamp<=self.window[-1]['acquired_s']:return
        if self.window and stamp-self.window[-1]['acquired_s']>1.:self.window=[]
        self.window.append(row)
    def integral_limit(self):return self.schedule[self.bound_index]
    def raise_bound(self):
        """Next stage after the present one held the target (status ready). Returns the new integral limit (counts)."""
        if self.stage!=0:raise DeviceError('The integral bound is raised only while holding the commissioning target')
        if self.bound_index>=len(self.schedule)-1:raise DeviceError('Integral bound already at the approved value')
        if not self.status()['ready']:raise MeasurementUnavailable('Present integral stage still requires valid stable measurements')
        self.bound_events.append(dict(limit_count=self.integral_limit(),max_observed_correction=self.max_observed_correction,**self.status()))
        self.window=[];self.bound_index+=1
        return self.integral_limit()
    def at_approved_bound(self):return self.bound_index==len(self.schedule)-1
    def advance(self):
        if not self.status()['ready']:raise MeasurementUnavailable('PI commissioning stage still requires valid stable measurements')
        self.events.append(dict(**self.status(),last=self.window[-1]))
        self.window=[]


def run_pi_commission(lock,p,*,clock,sleep,cancel,emit):
    """One card/lease; all physical operations remain inside the owner."""
    if p['target_frequency_hz'] is None or p['tolerance_hz'] is None:raise DeviceError('Commissioning target and tolerance are required')
    active=False;waiting=None;last_stage=None
    def check():
        if cancel():raise CardCancelled('PI commissioning stopped')
    def wait_stage(held=False):
        nonlocal waiting,last_stage
        while True:
            check()
            if not held:lock.heartbeat()
            try:row=lock.commission_hold_sample() if held else lock.read_cycle()
            except MeasurementUnavailable as error:
                if waiting is None:waiting=clock()
                emit('measurement_rejected',dict(stage='PI commissioning',reason=str(error)))
                if clock()-waiting>p['measurement_timeout_s']:raise DeviceError('PI commissioning measurement timeout')
            else:
                waiting=None
                if row.get('state','locked')!='locked':raise DeviceError('Native lock lost during PI commissioning')
                emit('trace',dict(frequency_hz=row['frequency_hz'],wavelength_nm=299792458e9/row['frequency_hz'],
                    outputs=row['outputs'],single_mode=row['single_mode'],commission=row['commission']))
                stage=row['commission']['stage']
                if stage!=last_stage:emit('state',dict(state='pi_commissioning',stage=stage));last_stage=stage
                if row['commission']['ready']:return
            sleep(.2)
    def qualify():
        deadline=clock()+p['measurement_timeout_s']
        while True:
            check()
            if clock()>deadline:raise DeviceError('Initial-point qualification timed out before PI commissioning')
            try:ready=lock.check_commission_ready(p['target_frequency_hz'],p['tolerance_hz'])
            except MeasurementUnavailable:
                ready=dict(ready=False)
            emit('commission_qualification',ready)
            if ready['ready']:break
            sleep(.2)
    try:
        qualify()
        check();active=True
        lock.commission_arm(p['target_frequency_hz'],p['tolerance_hz'])
        wait_stage()
        # Progressive integral bound: each stage holds the target cleanly before the next (<=2x) bound is written.
        while True:
            check();stage=lock.commission_raise_bound();emit('commission_integral_stage',stage);wait_stage()
            if stage['at_approved']:break
        for _ in range(3):
            check();lock.commission_next();wait_stage()
        check();emit('commission_fixed_stop',lock.commission_fixed_stop())
        # Fresh measured-wavelength soft acquisition after the fixed-DAC stop.
        qualify()
        lock.commission_arm(p['target_frequency_hz'],p['tolerance_hz']);wait_stage()
        check();emit('commission_actual_hold',lock.commission_actual_hold());wait_stage(held=True)
        result=lock.commission_complete();emit('ecdl_pi_commission_verified',result)
        return dict(status='succeeded',commission=result,lock_enabled=False)
    finally:
        if active:emit('ecdl_lock_held',lock.hold('PI commissioning card exit'))
