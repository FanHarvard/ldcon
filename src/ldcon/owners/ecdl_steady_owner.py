"""Typed native-PI lifecycle inside the sole ECDL instrument owner.

Hardware commissioning is explicit: multi-key firmware updates are not assumed
atomic. A matching verified handover profile is required before any PI write.
"""
from dataclasses import replace
import copy
import hashlib
import json
import math
import threading
import time
from urllib.parse import urlencode,urlsplit
from urllib.request import build_opener,ProxyHandler
from ldcon.core.contracts import DeviceError,MeasurementUnavailable
from ldcon.drivers.ecdl import arb_name_matches
from ldcon.core.device_conditions import finite
from ldcon.owners.ecdl_steady_policy import validate_profile,validate_dither_profile,SteadyPlanner,dither_response,entry_quality,control_ready,adjustment_ready

C_NM=299792458e9
ACTIONS={'steady_target_ff_study_status','steady_target_ff_study_set','steady_target_ff_study_restore',
         'steady_parameters','steady_parameters_status','steady_hot_control','steady_ready','steady_arm','steady_read','steady_heartbeat','steady_hold','steady_adjust',
         'background_status','background_reserve','background_release','background_begin',
         'background_current','background_restore','background_adopt','background_abandon',
         'background_pid_target','background_pid_finish','background_parameters_target',
         'steady_set_reference','steady_set_target',
         'response_info','response_dither_info','response_begin','response_end','response_current','response_dither',
         'steady_study_start','steady_study_status','steady_study_stop',
         'steady_pid_begin','steady_pid_target','steady_pid_save','steady_pid_finish',
         'commission_ready','commission_arm','commission_next','commission_fixed_stop','commission_raise_bound',
         'commission_actual_hold','commission_hold_sample','commission_complete','configure_dither','load_reviewed_dither',
         'locked_sweep_prepare','locked_sweep_status','locked_sweep_trigger','locked_sweep_restore','locked_sweep_calibrate',
         'locked_sweep_cleanup','restore_reviewed_dither'}
# Allowed while a locked sweep owns the rig (everything else is refused until it is restored).
# Stable marker of the one-sample sequence-regression refusal (matched by the jobs worker; never an outage sample).
SEQUENCE_REGRESSED='Native input sequence regressed'
SWEEP_ALLOWED={'steady_parameters_status','steady_target_ff_study_status','steady_target_ff_study_restore',
               'steady_heartbeat','steady_hold','steady_read','response_info','response_dither_info'}
# Dither pause (human request 2026-10-27): the reason every report carries while steady_lock.dither.paused is true.
DITHER_PAUSED_REASON='Dither paused (steady_lock.dither.paused); no passive dither fit'


class NativeLockOwner:
    def __init__(self,owner):
        self.o=owner;self.active=False;self.armed=False;self.planner=None
        self.deadline=None;self.stop_event=threading.Event();self.watchdog=None
        self.last_seq=None;self.last_seq_at=None;self.pending=[];self.proposed=[]
        self.native_input_report=None;self.native_interval_samples=[]  # report-only native input view (v5.1-hostcheck)
        self.native_sequence_regressions=0;self.native_sequence_last_regression=None
        self.study=None;self.pid_tuning=None
        self.response_control=False
        from experiment_core.background import BackgroundCoordinator
        self.background=BackgroundCoordinator(owner.clock)
        self.trial=None;self.commissioning=False
        self.qualification=[];self.qualification_context=None
        self.arm_reference=None
        self.programmed_dither=None
        self.sweep=None;self.sweep_calibration=None;self.dither_blackout_until=None  # ecdl_locked_sweep
        self.ch2_rehearsal=None  # v5.4: cached passed CH2 rehearsal {key, unix_s} (once per owner process per verified config)
        self.tec_ff_paused=False
        self.tec_reference_lease=None;self.tec_reference_dac=None
        self.tec_reference_confirmed=False;self.tec_reference_pending=False
        self.parameter_sequence=0
        self.supervision_sequence=0
        self.parameter_lease=None
        self.parameter_overrides={}
        self.target_ff=None
        from ldcon.owners.ecdl_target_ff import TargetFFStudy
        self.target_ff_study=TargetFFStudy(owner.clock)
        from ldcon.owners.ecdl_thermal_study import ThermalStudyOverlay
        self.thermal_study=ThermalStudyOverlay(self)
        self._thermal_proposals_revoked=False
        self.target_move_policy=None
        self.dither_report=dict(dither_response=None,dither_response_valid=False,
                                dither_analysis_unix_s=None,dither_source_unix_s=None,
                                dither_analysis_status='not_analyzed')
        # The servo configuration this owner last read or wrote while armed (design §5: the next owner process
        # verifies the CONTROLLED keys against it, read-only, before adopting the running PI).
        self.expected_servo=None
    # Watchdog thread switch (tests drive watch_tick() themselves with a fake clock).
    watch_enabled=True
    # Servo configuration keys the owner controls while the native PI runs (compared at adoption; the telemetry
    # log keys are restored by their own journals and are not part of the PI contract).
    CONTROLLED_SERVO=('target','control_offset','pid_multiplier','multiplier_control','multiplier_target',
                      'baseline_control','baseline_target','software_modulation_enable','valid_data_index',
                      'system_min','system_max','pid_kp','pid_ki','pid_kd','pid_integral_min','pid_integral_max')
    DURABLE=('strategy','target','current_target','tolerance','interval','arm_reference','commissioning','tec_ff_paused',
             'tec_reference_lease','tec_reference_dac','tec_reference_confirmed','tec_reference_pending',
             'parameter_sequence','parameter_lease','parameter_overrides','parameter_gain_ceiling',
             'parameter_tolerance_ceiling','parameter_ff_config','target_ff','target_move_policy','programmed_dither',
             'expected_servo','supervision_sequence')

    def in_flight(self):
        """Owner operations whose state lives only in this process: a lock with one of these at shutdown is not
        adopted by the next owner (quarantine with the reason), exactly like the native owner's FF session."""
        reasons=[]
        if self.commissioning:reasons.append('PI commissioning')
        if self.sweep is not None and self.sweep.state not in self._sweep_finished():reasons.append('locked sweep (%s)'%self.sweep.state)
        if self.pid_tuning and not getattr(self.pid_tuning,'closed',False):reasons.append('native PI tuning')
        if self.study and self.study.status=='running':reasons.append('locked current study')
        if self.background.epoch is not None:reasons.append('background epoch (%s)'%self.background.epoch.get('family'))
        if self.response_control:reasons.append('response study')
        if getattr(self.thermal_study,'active',False):reasons.append('thermal study')
        return reasons

    def durable(self):
        """JSON context of the armed lock for the durable owner state (None when no native lock is armed)."""
        if not self.armed:return None
        record={key:copy.deepcopy(getattr(self,key,None)) for key in self.DURABLE}
        record.update(profile=copy.deepcopy(getattr(self,'p',None)),active=self.active,in_flight=self.in_flight(),
                      target_ramp_active=getattr(self,'current_target',None)!=getattr(self,'target',None),
                      deadline_remaining_s=None if self.deadline is None else self.deadline-self.o.clock())
        return record

    def restore(self,record):
        """Owner restart: put the persisted lock context back in memory (armed/active, no planner yet, no
        proposals). The hardware is NOT touched here; ``adopt_verify``/``adopt`` follow after a read-only snapshot."""
        for key in self.DURABLE:
            setattr(self,key,copy.deepcopy(record.get(key)))
        self.p=copy.deepcopy(record.get('profile') or {})
        self.commissioning=bool(record.get('commissioning'))
        self.parameter_overrides=dict(self.parameter_overrides or {})
        self.parameter_sequence=int(self.parameter_sequence or 0);self.supervision_sequence=int(self.supervision_sequence or 0)
        self.armed=True;self.active=True;self.planner=None;self.pending=[];self.proposed=[]
        self.deadline=None;self.stop_event=threading.Event()
        self.last_seq=None;self.last_seq_at=None
        self.dither_unavailable('Owner restart: adoption pending',status='not_analyzed')

    def adopt_verify(self,state):
        """READ-ONLY verification of a persisted active lock against the firmware (design §5): firmware version,
        ``pid_multiplier == 1`` (named first), the CONTROLLED configuration equal to what this lease last read/wrote,
        the native input route (``native``) and the verified dither waveform. Raises DeviceError with the reason."""
        cfg=state['servo_config'];status=state['servo_status']
        if not self.p:raise DeviceError('No persisted steady-lock profile to verify against')
        if status.get('firmware_version')!=self.p.get('firmware_version'):
            raise DeviceError('Firmware identity changed: %r (verified profile %r)'%(status.get('firmware_version'),self.p.get('firmware_version')))
        if cfg.get('pid_multiplier')!=1:
            raise DeviceError('Native PI ownership lost: pid_multiplier=%r (expected 1)'%(cfg.get('pid_multiplier'),))
        differing=[]
        for key in self.CONTROLLED_SERVO:
            if not isinstance(self.expected_servo,dict) or key not in self.expected_servo or key not in cfg:continue
            expected=self.expected_servo[key];actual=cfg[key]
            if isinstance(expected,(int,float)) and not isinstance(expected,bool) and isinstance(actual,(int,float)) and not isinstance(actual,bool):
                same=math.isclose(actual,expected,rel_tol=1e-8,abs_tol=1e-9)
            else:same=actual==expected
            if not same:differing.append('%s=%r (expected %r)'%(key,actual,expected))
        if differing:
            raise DeviceError('Native controlled configuration changed outside this lease: '+', '.join(differing))
        # Route/version/time-based/modulation checks of the running firmware; no wavemeter reading is needed.
        self.native(state,dict(frequency=dict(frequency_hz=None)),self.p,locked=True)
        if not self.commissioning:self.check_dither(state,self.p)

    def adopt(self,state,policies):
        """Resume supervision of the verified running lock with the SAME lease: planner from the current outputs
        (as a runtime parameter update does), a full ``watchdog_s`` heartbeat grace from now, watchdog restarted."""
        o=self.o;now=o.clock()
        effective={}
        for name,p in policies.items():
            bounds=o.device['absolute_conditions']['axes'][name]['activation_range']
            half=0 if name=='piezo' else state[name]['amplitude_vpp']/2
            effective[name]=replace(p,low=max(p.low,o.activation[name]+bounds[0])+half,
                                   high=min(p.high,o.activation[name]+bounds[1])-half)
        self.planner=SteadyPlanner(self.p,effective,state['outputs'],self.strategy,
                                   tec_reference_dac=None if self.commissioning else self.tec_reference_dac)
        self.expected_servo=copy.deepcopy(state['servo_config'])
        self.armed=True;self.active=True;self.pending=[];self.proposed=[]
        self.last_target=now;self.last_supervision=now
        self.deadline=now+self.p['watchdog_s'];self.stop_event=threading.Event()
        self.dither_unavailable('Owner restart adoption',status='not_analyzed')
        self.start_watch()

    def start_watch(self):
        if not self.watch_enabled:return
        if self.watchdog is not None and self.watchdog.is_alive():return
        self.watchdog=threading.Thread(target=self._watch,daemon=True);self.watchdog.start()

    def note_servo_config(self,cfg):
        if self.armed and isinstance(cfg,dict):self.expected_servo=copy.deepcopy(cfg)

    def persist(self):
        """Durable owner state after a change of the armed lock's context (never raises into the control path)."""
        persist=getattr(self.o,'persist_state',None)
        if persist is None:return
        try:persist()
        except (OSError,ValueError,TypeError) as error:self.o.audit('state_persist_failed',error=str(error))
    def _sweep_finished(self):
        from ldcon.owners.ecdl_locked_sweep import FINISHED
        return FINISHED
    def target_ramp_step(self):
        """Desired internal target chunk; output proof may reduce the 100 MHz default."""
        from ldcon.owners.ecdl_target_ff import study_policy
        return (self.target_move_policy or study_policy(self.p))['step_hz']
    def target_ff_study_context(self):
        profile=getattr(self,'p',None) or self.o.device['control_settings'].get('steady_lock',{})
        return self.o.lease,profile,self.o.policy_authority.revision
    def sync_target_response(self):
        """Expire one shared study scope before it can produce another output."""
        status=self.target_ff_study.status(*self.target_ff_study_context())
        old_token=self.thermal_study.token
        pending=bool(self.pending or self.proposed)
        changed=self.thermal_study.sync(status['session'])
        if changed:
            self._thermal_proposals_revoked |= pending
            # Never revoke a newer session while retiring an older overlay.
            if status['session'] and status['session']['study_token']==old_token:
                self.target_ff_study.restore(dict(expected_revision=status['revision'],
                    study_token=old_token),*self.target_ff_study_context())
        return changed
    def clear_target_response(self,reason):
        """Foreground preemption restores parameters only, never old outputs."""
        pending=bool(self.pending or self.proposed)
        changed=self.thermal_study.clear(reason)
        self._thermal_proposals_revoked |= changed and pending
        self.target_ff_study.settings(None,*self.target_ff_study_context())
        return changed
    def target_response_thermal_status(self):
        from ldcon.owners.ecdl_target_settling import centering_context
        return dict(self.thermal_study.status(),centering=centering_context(self))
    def require_thermal_output(self,axis,value,outputs):
        if not self.thermal_study.permit_output(axis,value,outputs=outputs):
            self.clear_target_response('approved thermal study excursion exceeded')
            raise MeasurementUnavailable('Thermal study excursion exceeded; parameters restored without output rewind')
    def target_ff_study_status(self):
        self.sync_target_response()
        result=self.target_ff_study.status(*self.target_ff_study_context())
        result['thermal']=self.target_response_thermal_status()
        move=self.target_ff
        if move is not None:
            fields=('steps','started_unix_s','first_write_started_unix_s','first_write_verified_unix_s',
                    'last_write_started_unix_s','last_write_verified_unix_s','minimum_actual_step_hz',
                    'maximum_actual_step_hz','target_feedforward_scale','target_ramp_step_hz')
            result['last_move']={k:move[k] for k in fields if type(move.get(k)) in (int,float) and math.isfinite(move[k])}
            result['last_move'].update(target_frequency_hz=self.target,ramped_target_hz=self.current_target,
                                       target_finished=self.current_target==self.target)
        else:result['last_move']=None
        return result
    def target_ff_study_dispatch(self,request):
        from ldcon.core.instrument_client import BrokerError
        action=request['action']
        if action=='steady_target_ff_study_status':return self.target_ff_study_status()
        if action=='steady_target_ff_study_restore':
            self.target_ff_study.restore(request,*self.target_ff_study_context())
            return self.target_ff_study_status()
        if (not self.armed or self.commissioning or self.current_target!=self.target
                or self.parameter_status()['busy']):
            raise BrokerError('ownership','Target FF study requires an armed idle controller')
        if not self.p.get('target_feedforward_enabled',False) and request.get('scale')!=1:
            raise BrokerError('configuration','Target feedforward is not enabled')
        thermal=request.get('thermal')
        if thermal is None and self.thermal_study.active:
            raise BrokerError('configuration','Explicit thermal candidate required until this study is restored')
        state=None
        if thermal is not None:
            state,obs=self.read_feedback();self.o.validate_state(state,self.o.policies())
            self.native(state,obs,self.p,locked=True)
            if not control_ready(obs,self.p,self.current_target):
                raise MeasurementUnavailable('Thermal study needs fresh current frequency context')
            self.thermal_study.validate(thermal,outputs=state['outputs'])
        result=self.target_ff_study.set(request,*self.target_ff_study_context())
        # The token and its overlay are one lock-held memory transaction. Do not
        # call sync between these operations: the same token may be renewed.
        if thermal is not None:
            pending=bool(self.pending or self.proposed)
            self.thermal_study.apply(result['session'],thermal,outputs=state['outputs'])
            self._thermal_proposals_revoked |= pending
        self.o.audit('target_ff_study_policy',revision=result['revision'],session=result['session'],hardware_write=False)
        return self.target_ff_study_status()
    def target_ff_bounds(self):
        policy=self.o.policies()['piezo']
        relative=self.o.device['absolute_conditions']['axes']['piezo']['activation_range']
        limits=self.o.hw().connections['piezo'].get('dac/limits')
        if not isinstance(limits,dict):raise MeasurementUnavailable('PZT DAC limits are unavailable')
        return (max(policy.low,self.o.activation['piezo']+relative[0],finite(limits['min_code'])),
                min(policy.high,self.o.activation['piezo']+relative[1],finite(limits['max_code'])))
    def prepare_target_ff(self,state,target,settings=None):
        if not self.p.get('target_feedforward_enabled',False) or self.commissioning:return None
        from ldcon.owners.ecdl_target_ff import plan,study_policy,ldc_policy,headroom
        settings=settings or study_policy(self.p)
        cfg=state['servo_config']
        if not math.isclose(cfg['target'],C_NM/self.current_target,rel_tol=0,abs_tol=1e-9):
            raise MeasurementUnavailable('Firmware target differs from the owner target before feedforward')
        ldc=ldc_policy(self.p)
        origin=dict(target_nm=cfg['target'],offset_count=cfg['control_offset'],
                    wavelength_nm_per_count=self.p['wavelength_nm_per_count'],
                    target_feedforward_scale=settings['scale'],target_ramp_step_hz=settings['step_hz'],
                    expected_offset=cfg['control_offset'],steps=0,started_unix_s=time.time(),
                    # LDC target-FF term (decision 4, 2026-10-27): the ramp origin current and the realised share
                    origin_current=state['outputs']['current'],origin_target_hz=self.current_target,
                    ldc=dict(enabled=ldc['enabled'],v_per_hz=ldc['v_per_hz'],max_abs_v=ldc['max_abs_v'],hz_per_v=ldc['hz_per_v']),
                    ldc_applied_v=0.,ldc_share_hz=0.,ldc_writes=0,ldc_skipped=None)
        bounds=self.target_ff_bounds()
        proof=plan(self.p,state,origin,target,bounds)
        origin['final_offset_count']=proof['values']['control_offset']
        origin['pzt_headroom_hz']=headroom(self.p,state,origin,bounds)
        return origin
    def pzt_headroom(self,state):
        """Report-only room (Hz, up/down) for the next target step at the present PI centre; None when FF is off."""
        if getattr(self,'p',None) is None or not self.p.get('target_feedforward_enabled',False):return None
        from ldcon.owners.ecdl_target_ff import headroom,study_policy
        try:
            settings=self.target_move_policy or study_policy(self.p)
            origin=dict(wavelength_nm_per_count=self.p['wavelength_nm_per_count'],target_feedforward_scale=settings['scale'])
            return headroom(self.p,state,origin,self.target_ff_bounds())
        except (MeasurementUnavailable,DeviceError,ValueError,KeyError,TypeError,ZeroDivisionError):
            return None
    def ldc_feedforward(self,state,next_target):
        """The LDC term of one ramp step (decision 4, 2026-10-27): move the current to origin + clamp(k x total shift)
        BEFORE the PZT/target write, through the owner's verified locked_move under the current axis conditions
        (bounds, activation window, current_span, step/slew, update interval, runtime grant, thermal-study excursion).
        A pre-write refusal skips the LDC move for this step (reported); the PZT then carries the whole remainder.
        Returns (realised share in Hz, current state after the move)."""
        origin=self.target_ff;ldc=origin.get('ldc') or {}
        from ldcon.owners.ecdl_target_ff import ldc_desired,ldc_share_hz
        if not ldc.get('enabled'):return 0.,state
        cp=self.o.policies()['current']
        desired=ldc_desired(ldc,origin['origin_current'],next_target-origin['origin_target_hz'],cp.resolution)
        actual=state['outputs']['current']
        if abs(desired-actual)<max(cp.resolution/2,1e-12):
            origin['ldc_share_hz']=ldc_share_hz(ldc,origin['origin_current'],actual)
            return origin['ldc_share_hz'],state
        from ldcon.owners.ecdl_tec_pair import check_current
        try:
            self.require_thermal_output('current',desired,state['outputs'])
            if check_current(self,state,desired):
                self.adjusting=True
                try:result=self.o._dispatch(dict(action='locked_move',lease=self.o.lease,axis='current',value=desired))
                finally:self.adjusting=False
                self.planner.ack_current(result['outputs'],'LDC target feedforward')
                state=dict(state,outputs=result['outputs'])
                origin['ldc_writes']+=1
                origin['ldc_skipped']=None
                self.o.audit('ldc_target_feedforward_applied',desired=desired,previous=actual,current=result['outputs']['current'],
                             v_per_hz=ldc['v_per_hz'],shift_hz=next_target-origin['origin_target_hz'])
        except (MeasurementUnavailable,DeviceError) as refusal:
            # A known pre-write refusal (interval, step/slew, envelope, grant, excursion) defers the LDC share to the
            # PZT; an uncertain write has latched the owner fault and propagates (never replayed).
            if self.o.fault:raise
            origin['ldc_skipped']=str(refusal)
            self.o.audit('ldc_target_feedforward_deferred',reason=str(refusal),desired=desired,actual=actual)
        origin['ldc_applied_v']=state['outputs']['current']-origin['origin_current']
        origin['ldc_share_hz']=ldc_share_hz(ldc,origin['origin_current'],state['outputs']['current'])
        return origin['ldc_share_hz'],state
    def ramp_target(self,state,elapsed):
        """Target and calibrated offset in one verified request; PI memory stays intact."""
        if self.o.fault:raise DeviceError('Owner fault: target write is not replayed')
        limit=min(self.p['target_slew_hz_per_s']*max(0.,elapsed),self.target_ramp_step())
        if self.target_ff is None:
            # Initial PI-only acquisition and disabled-FF operation have no
            # compensating offset jump. Keep their existing capture-sized ramp.
            limit=min(limit,self.p['capture_hz']/2)
        remaining=self.target-self.current_target
        room=None
        if self.target_ff is not None:
            # target-ff design (2026-10-27): the per-step cap is min(saved cap, PZT headroom in the step's direction).
            # The headroom is REPORTED (pzt_headroom_hz); it bounds the step so the ramp proceeds as the background
            # TEC centering returns room, instead of refusing or halving blindly at the envelope edge.
            from ldcon.owners.ecdl_target_ff import headroom
            room=headroom(self.p,state,self.target_ff,self.target_ff_bounds())
            self.target_ff['pzt_headroom_hz']=room
            available=room['up_hz'] if remaining>0 else room['down_hz']
            limit=min(limit,available)
        change=max(-limit,min(limit,remaining))
        if not change:return False
        next_target=self.current_target+change
        proof=None
        if self.target_ff is not None:
            from ldcon.owners.ecdl_target_ff import plan
            origin=self.target_ff;cfg=state['servo_config']
            if (not math.isclose(cfg['target'],C_NM/self.current_target,rel_tol=0,abs_tol=1e-9)
                    or cfg['control_offset']!=origin['expected_offset']):
                raise DeviceError('Target/offset changed outside the active feedforward request')
            pp=self.o.policies()['piezo'];age=self.o.clock()-self.o.last_write['piezo']
            if age+1e-9<pp.update_interval_s:return False
            budget=min(pp.max_step,pp.max_slew_per_s*min(age,pp.update_interval_s))
            bounds=self.target_ff_bounds()
            # LDC term first (decision 4): its realised share is subtracted from the PZT term of the same step.
            share,state=self.ldc_feedforward(state,next_target)
            for _ in range(40):
                proof=plan(self.p,state,origin,next_target,bounds,ldc_share_hz=share)
                if proof['configuration_step_bound_count']<=budget+1e-9:break
                change*=.5;next_target=self.current_target+change
            else:raise MeasurementUnavailable('PZT feedforward cannot meet the existing step/slew budget')
            if next_target==self.current_target:return False
            values=proof['values']
        else:
            values=dict(target=C_NM/next_target)
            if not self.commissioning:
                band=self.p['capture_hz']
                values.update(system_min=C_NM/(next_target+band),system_max=C_NM/(next_target-band))
        began=time.time()
        actual_step=abs(next_target-self.current_target)
        self.write(values,'bounded target and PZT feedforward' if proof else 'bounded lock target ramp')
        # Advance only after readback succeeds; unknown writes retain the fault.
        self.current_target=next_target
        if proof:
            self.o.last_write['piezo']=self.o.clock()
            origin.update(expected_offset=values['control_offset'],steps=origin['steps']+1,
                          last_write_started_unix_s=began,last_write_verified_unix_s=time.time())
            origin.setdefault('first_write_started_unix_s',began)
            origin.setdefault('first_write_verified_unix_s',origin['last_write_verified_unix_s'])
            origin['minimum_actual_step_hz']=min(origin.get('minimum_actual_step_hz',actual_step),actual_step)
            origin['maximum_actual_step_hz']=max(origin.get('maximum_actual_step_hz',actual_step),actual_step)
            self.o.audit('target_feedforward_applied',target_hz=next_target,proof=proof,integral_reset=False)
        return True
    def target_bounds(self):
        """Admit from the last accepted target, never from an archival branch map."""
        from ldcon.core.target_policy import target_bounds
        return target_bounds(getattr(self.o,'laser',None) or {}, reference_hz=self.target)
    def tec_context_fields(self):
        current=self.tec_reference_lease==self.o.lease and self.o.lease is not None
        return dict(tec_ff_paused=self.tec_ff_paused,steady_strategy=getattr(self,'strategy',None),tec_center_reference_dac=self.tec_reference_dac if current else None,
                    tec_center_reference_confirmed=bool(current and self.tec_reference_confirmed),
                    target_response_thermal=self.target_response_thermal_status())

    def parameter_status(self):
        values=dict(target_frequency_hz=self.target,tolerance_hz=self.tolerance,
                    strategy=self.strategy,tec_ff_paused=self.tec_ff_paused,
                    current_ff_enabled=bool(self.p.get('tec_current_feedforward')),
                    tec_gain=self.p['tec_gain'])
        identity=json.dumps(dict(lease=self.o.lease,parameters=values),sort_keys=True,separators=(',',':'))
        revision=str(self.parameter_sequence)+':'+hashlib.sha256(identity.encode()).hexdigest()[:20]
        busy=bool(self.sweep is not None and self.sweep.state not in self._sweep_finished()
                  or self.commissioning or self.response_control or self.o.recovery is not None
                  or self.study and self.study.status=='running'
                  or self.pid_tuning and not self.pid_tuning.closed or self.background.epoch is not None)
        return dict(revision=revision,parameters=values,busy=busy,native_pi_active=bool(self.armed),
                    ceilings=dict(tec_gain=self.parameter_gain_ceiling,tolerance_hz=self.parameter_tolerance_ceiling),
                    configured_current_feedforward=bool(self.parameter_ff_config))


    def update_parameters(self,request):
        """One lease-scoped memory transaction; hardware ramp stays in read_cycle."""
        from ldcon.core.instrument_client import BrokerError
        allowed={'target_frequency_hz','tolerance_hz','strategy','tec_ff_paused','current_ff_enabled','tec_gain'}
        patch=request.get('patch')
        status=self.parameter_status()
        if request.get('expected_revision')!=status['revision']:
            raise BrokerError('configuration','Stationary parameter revision changed')
        if not isinstance(patch,dict) or not patch or set(patch)-allowed:
            raise BrokerError('configuration','Unknown stationary runtime parameter')
        if status['busy']:
            raise BrokerError('ownership','Another native study/recovery owns the controller')
        if not self.armed:
            raise BrokerError('ownership','Native stationary lock is not armed')
        if set(patch)&{'target_frequency_hz','tolerance_hz'} and not {'target_frequency_hz','tolerance_hz'}<=set(patch):
            raise BrokerError('configuration','Target and tolerance form one transaction')
        candidate={**status['parameters'],**patch}
        for key in ('target_frequency_hz','tolerance_hz','tec_gain'):
            value=candidate[key]
            if type(value) not in (int,float) or not math.isfinite(value) or value<=0:
                raise BrokerError('configuration','Invalid stationary '+key)
        if candidate['strategy']!='hold':
            raise BrokerError('configuration','Hidden current-window traversal is retired; use a separate window-center/adjustment card')
        if any(type(candidate[k]) is not bool for k in ('tec_ff_paused','current_ff_enabled')):
            raise BrokerError('configuration','Stationary enable/pause parameters must be boolean')
        if ('tec_gain' in patch and candidate['tec_gain']>self.parameter_gain_ceiling) or candidate['tolerance_hz']>self.parameter_tolerance_ceiling:
            raise BrokerError('range','Runtime parameter exceeds its invocation ceiling')
        if candidate['current_ff_enabled'] and not self.parameter_ff_config:
            raise BrokerError('configuration','Current feedforward requires an existing calibrated coefficient')
        profile=copy.deepcopy(self.p)
        profile['tec_gain']=candidate['tec_gain']
        if candidate['current_ff_enabled']:
            profile['tec_current_feedforward']=copy.deepcopy(self.parameter_ff_config)
        else:
            profile.pop('tec_current_feedforward',None)
        validate_profile(profile,candidate['strategy'])
        state,obs=self.read_feedback()
        self.o.validate_state(state,self.o.policies())
        self.native(state,obs,self.p,locked=True)
        if not control_ready(obs,self.p,self.current_target):
            raise MeasurementUnavailable('Runtime update requires fresh current frequency/branch context')
        target=candidate['target_frequency_hz'];tolerance=candidate['tolerance_hz']
        if 'target_frequency_hz' in patch:
            cfg=state['servo_config']
            # Firmware capture limits are unchanged. A runtime target may move
            # only inside the already commissioned and programmed interval.
            low=C_NM/finite(cfg['system_max']);high=C_NM/finite(cfg['system_min'])
            if (not low+tolerance<=target<=high-tolerance
                    or abs(target-obs['frequency']['frequency_hz'])>=self.p['capture_hz']):
                raise BrokerError('range','Requested target leaves existing native capture interval')
        planner=SteadyPlanner(profile,self.planner.policies,state['outputs'],candidate['strategy'],
                              tec_reference_dac=self.tec_reference_dac)
        settings=(self.target_ff_study.settings(None,*self.target_ff_study_context())
                  if 'target_frequency_hz' in patch else self.target_move_policy)
        ff=self.prepare_target_ff(state,target,settings) if 'target_frequency_hz' in patch else self.target_ff
        self.o.audit('stationary_parameters_requested',old_revision=status['revision'],patch=patch,
                     outputs=state['outputs'],native_pi_unchanged=True)
        self.discard_proposals('atomic stationary parameter update')
        self.p=profile;self.planner=planner;self.strategy=candidate['strategy']
        self.tec_ff_paused=candidate['tec_ff_paused']
        self.target_ff=ff
        self.target_move_policy=settings
        self.target=target;self.tolerance=tolerance
        # Reset the ramp/supervision clocks, not native PI memory. No elapsed
        # waiting time or stale FF remainder becomes an immediate command.
        self.last_target=self.o.clock();self.last_supervision=self.o.clock()
        self.parameter_overrides.update({key:candidate[key] for key in patch})
        self.parameter_sequence+=1
        result=self.parameter_status()
        self.o.audit('stationary_parameters_applied',revision=result['revision'],parameters=candidate,
                     native_pi_unchanged=True)
        self.persist()
        return result


    # target-ff design (2026-10-27): the slew, the LDC target-FF term and the TEC->current FF coefficient are adopted
    # live like the FF scale / step (deferred while a target ramp or an FF study is in flight).
    TARGET_FF_PROFILE=frozenset(('target_feedforward_scale','target_feedforward_scale_bounds','target_ramp_step_hz',
                                 'target_slew_hz_per_s','ldc_target_feedforward'))
    HOT_PROFILE=frozenset(('current_safety','window','online_pid','entry_gate','supervision_interval_s','tec_dt_cap_s',
                           'tec_current_feedforward','dither')) | TARGET_FF_PROFILE
    # Of the dither block only the pause switch is live (agent_authority LIVE_RULES); the rest is snapshot/forbidden.
    DITHER_PAUSE_KEY='dither.paused'

    def prepare_profile_reload(self,new_steady,keys,*,policies=None):
        """Adopt reviewed steady-profile keys live; the rest wait for the next arm.

        Memory-only, except ``dither.paused``: when armed, the CH1 dither bank is rewritten in place (all-zero
        codes / the reviewed sine) HERE, before the policy transaction, through the proven configure_dither path
        with readback; an uncertain write faults the owner (no retry) and the profile is not adopted.
        ``online_pid`` waits for an active tuning ramp. Window quality metadata changes are memory-only
        and preserve the planner's physical origin and all unrelated control state.
        """
        from ldcon.owners.ecdl_steady_policy import validate_profile,validate_window_quality
        from ldcon.core.interface_schema import digest
        tops={k.split('.')[0] for k in keys}
        rejected={k:'applies at the next arm (snapshot)' for k in tops-self.HOT_PROFILE}
        hot=tops&self.HOT_PROFILE
        pause_requested=self.DITHER_PAUSE_KEY in keys or 'dither' in keys
        if 'dither' in hot:
            hot.discard('dither')
            for key in keys:
                if key.startswith('dither.') and key!=self.DITHER_PAUSE_KEY:rejected[key]='applies at the next arm (snapshot)'
            if pause_requested:hot.add(self.DITHER_PAUSE_KEY)
        def take(target,key):
            if key==self.DITHER_PAUSE_KEY:
                target.setdefault('dither',{})['paused']=(new_steady.get('dither') or {}).get('paused',False)
                return
            if key in new_steady:target[key]=copy.deepcopy(new_steady[key])
            else:target.pop(key,None)
        if not hot:return dict(applied=[],deferred=[],rejected=rejected),lambda:None
        if 'window' in hot:validate_window_quality(new_steady.get('window',{}))
        if hot&self.TARGET_FF_PROFILE:
            from ldcon.owners.ecdl_target_ff import study_policy,ldc_policy
            study_policy(new_steady);ldc_policy(new_steady)
        if self.DITHER_PAUSE_KEY in hot and type((new_steady.get('dither') or {}).get('paused',False)) is not bool:
            raise ValueError('dither.paused must be boolean')
        deferred=[]
        if not self.armed or getattr(self,'p',None) is None:
            return dict(applied=sorted(hot),deferred=[],rejected=rejected),lambda:None
        if 'online_pid' in hot and self.pid_tuning and not getattr(self.pid_tuning,'closed',False):
            deferred.append('online_pid');hot.discard('online_pid')
        target_fields=hot&self.TARGET_FF_PROFILE
        if target_fields and (self.current_target!=self.target or self.parameter_status()['busy']
                or self.target_ff_study.status(*self.target_ff_study_context())['active']):
            deferred.extend(sorted(target_fields));hot-=target_fields
        if self.DITHER_PAUSE_KEY in hot and self.dither_busy():
            deferred.append(self.DITHER_PAUSE_KEY);hot.discard(self.DITHER_PAUSE_KEY)
        if not hot:return dict(applied=[],deferred=deferred,rejected=rejected),lambda:None
        profile=copy.deepcopy(self.p)
        for key in hot:take(profile,key)
        if 'tec_current_feedforward' in hot:
            # The runtime switch current_ff_enabled (update_parameters) keeps its say: a disabled pair stays out of the
            # live profile and the new coefficient becomes the configured block it would re-enable.
            self.parameter_ff_config=copy.deepcopy(new_steady.get('tec_current_feedforward'))
            if self.parameter_overrides.get('current_ff_enabled') is False:profile.pop('tec_current_feedforward',None)
        validate_profile(profile,self.strategy)
        if self.DITHER_PAUSE_KEY in hot:
            wanted=profile['dither']['paused']
            if wanted!=self.dither_hardware_paused():
                # Hardware last of the preparation (after every validation), first of the adoption: readback-verified,
                # faults on uncertainty; the profile follows in apply(). Reports use dither_off(), so a transaction
                # that fails after this write still reports 'paused' truthfully until the next adoption reconciles.
                self.program_dither(wanted,'settings adoption',profile)
        def apply():
            self.p.clear();self.p.update(profile)
            if self.planner is not None and getattr(self.planner,'p',None) is not self.p:
                self.planner.p=self.p
            self.parameter_sequence=getattr(self,'parameter_sequence',0)+1
            self.o.audit('stationary_profile_reloaded',keys=sorted(hot),deferred=deferred,profile_revision=digest(self.p))
        return dict(applied=sorted(hot),deferred=deferred,rejected=rejected),apply

    def reload_profile(self,new_steady,keys):
        # The real system owner adopts the device document and profile together
        # through its configuration transaction. Legacy lightweight test owners
        # retain this in-memory adapter entry point.
        if hasattr(self.o,'policy_authority'):
            raise DeviceError('Profile changes require the system policy adoption transaction')
        result,apply=self.prepare_profile_reload(new_steady,keys)
        target=self.o.device['control_settings'].setdefault('steady_lock',{})
        for key in result['applied']:
            if key==self.DITHER_PAUSE_KEY:target.setdefault('dither',{})['paused']=(new_steady.get('dither') or {}).get('paused',False)
            elif key in new_steady:target[key]=copy.deepcopy(new_steady[key])
            else:target.pop(key,None)
        apply()
        return result

    def commission_profile(self,target=None):
        # A separate typed experiment, not an override accepted by steady_arm. The PI starts at the FIRST stage of
        # the progressive integral schedule (ecdl_pi_commission.integral_schedule); commission_raise_bound steps it
        # up to the approved integral_limit_count after verified holds.
        p=validate_profile(self.o.device['control_settings'].get('steady_lock'),'hold',require_handover=False)
        from ldcon.jobs.ecdl_pi_commission import integral_schedule
        if type(target) not in (int,float) or not math.isfinite(target) or target<=0:
            raise DeviceError('Explicit commissioning target frequency is required')
        schedule=integral_schedule(p,target)
        p=copy.deepcopy(p);p['approved_integral_limit_count']=p['integral_limit_count'];p['integral_limit_count']=schedule[0]
        return p
    def dither_profile(self):
        # Dither programming does not depend on PI commissioning (2026-10-22).
        return validate_profile(self.o.device['control_settings'].get('steady_lock'),'hold',require_handover=False)
    def profile(self,strategy):
        if strategy!='hold':raise DeviceError('Hidden current-window traversal is retired; use a separate window-center/adjustment card')
        p=validate_profile(self.o.device['control_settings'].get('steady_lock'),strategy)
        if not isinstance(p.get('handover_evidence'),str) or not p['handover_evidence'].strip():
            raise DeviceError('Native handover commissioning evidence is missing')
        return p
    # ---- dither pause (human request 2026-10-27; design §1 M example, card-system-design.md) -----------------------
    def dither_paused(self,profile=None):
        """The profile switch steady_lock.dither.paused (the live profile while armed, else the device document)."""
        p=profile if profile is not None else (getattr(self,'p',None) or self.o.device.get('control_settings',{}).get('steady_lock',{}))
        return (p.get('dither') or {}).get('paused') is True
    def dither_hardware_paused(self):
        """What this owner last programmed into the selected dither bank (durable with the armed lock). None/absent
        means the bank holds the reviewed sine (every other path leaves it so)."""
        record=self.programmed_dither
        return isinstance(record,dict) and record.get('paused') is True
    def dither_off(self,profile=None):
        """No modulation is (or will at this boundary be) on CH1: the profile switch or the programmed zero bank."""
        return self.dither_paused(profile) or self.dither_hardware_paused()
    def dither_busy(self):
        """Operations that own CH1 or consume the dither fit: the pause switch waits for the next boundary."""
        from ldcon.owners.ecdl_locked_sweep import active as sweep_active
        if self.commissioning:return True   # the PI commissioning trial runs on the verified waveform
        if sweep_active(self) or self.response_control or self.background.epoch is not None or getattr(self.o,'recovery',None) is not None:return True
        if self.study and self.study.status=='running':return True
        return False
    def program_dither(self,paused,reason,profile):
        """Rewrite the SELECTED CH1 dither bank in place: all-zero codes (``paused``) or exactly the reviewed sine
        payload (resume), same name/frequency/carrier range/offset, through configure_dither (the path that produced
        the reviewed evidence) with readback. Works armed (between heartbeats, under the owner lock) and unarmed
        (lock start / hold). A readback mismatch or any I/O doubt latches 'Uncertain dither configuration' (lease
        retained, no retry). Records dither_paused / dither_resumed (before/after readbacks, lease) and replaces
        ``programmed_dither``."""
        from ldcon.owners.ecdl_locked_sweep import reviewed_dither_policy
        from ldcon.core.records import atomic_json
        o=self.o;settings=profile['dither'];programming=settings.get('programming')
        if not isinstance(programming,dict):raise DeviceError('Verified dither programming evidence is missing')
        from ldcon.owners.ecdl_locked_sweep import active as sweep_active
        if sweep_active(self):raise DeviceError('A locked sweep owns CH1; dither pause/resume waits for its restoration')
        hw=o.hw();state=hw.snapshot();o.validate_state(state,o.policies())
        self.check_dither(state,profile)   # name / frequency / nominal carrier of the verified bank, in every state
        channel=o.device['connection']['resources']['current_awg']['channel']
        kind='dither_paused' if paused else 'dither_resumed'
        # Pre-write proof (restore_reviewed_dither pattern): the in-place payload targets the reviewed bank and, on
        # resume, is byte-identical to the reviewed sine. A refusal here writes nothing and latches nothing.
        from ldcon.drivers.ecdl import dither_payload
        planned,_=dither_payload(channel,finite(programming['frequency_hz']),0. if paused else finite(programming['requested_vpp']),
                                 state['current'],reviewed_dither_policy(programming,channel),paused=paused,keep_bank=True)
        if planned['name']!=programming['name'] or (not paused and planned['payload_sha256']!=programming['payload_sha256']):
            raise DeviceError('In-place dither payload differs from the reviewed programming (CH1 on %r)'%(state['current'].get('arb_name'),))
        reserved=hw.reserved_readbacks()
        o.audit(kind+'_requested',reason=reason,lease=o.lease,armed=self.armed,before=state['current'],outputs=state['outputs'],
                reserved=reserved,programming=programming)
        try:
            programmed=hw.connections['current_awg'].configure_dither(channel,finite(programming['frequency_hz']),
                0. if paused else finite(programming['requested_vpp']),state['current'],
                reviewed_dither_policy(programming,channel),paused=paused,keep_bank=True)
            after=hw.snapshot();o.validate_state(after,o.policies())
            o.audit('dither_readback',requested=programmed,after=after['current'],lease=o.lease)
            if programmed['name']!=programming['name']:raise DeviceError('Programmed bank %r is not the reviewed %r'%(programmed['name'],programming['name']))
            if paused:
                if programmed.get('paused') is not True or programmed['programmed_vpp']!=0.:raise DeviceError('Paused payload is not all-zero')
            elif programmed['payload_sha256']!=programming['payload_sha256']:
                raise DeviceError('Re-programmed dither differs from the reviewed programming')
            if not arb_name_matches(after['current']['arb_name'],programmed['name']) or abs(after['current']['frequency_hz']-programming['frequency_hz'])>1e-6:
                raise DeviceError('Selected dither readback mismatch: name=%r expected=%r frequency=%r expected_frequency=%r'
                                  %(after['current']['arb_name'],programmed['name'],after['current']['frequency_hz'],programming['frequency_hz']))
            for key in state['current']:
                if key not in ('arb_name','frequency_hz') and after['current'][key]!=state['current'][key]:
                    raise DeviceError('Dither programming changed current output configuration: '+key)
            # The PZT moves while the PI runs; the held axes and the loop state must not.
            if (after['temperature']!=state['temperature'] or after['open_loop']!=state['open_loop']
                    or after['outputs']['current']!=state['outputs']['current'] or after['outputs']['temperature']!=state['outputs']['temperature']):
                raise DeviceError('Dither programming changed held rig state')
            if hw.reserved_readbacks()!=reserved:raise DeviceError('Reserved channel state changed during dither programming')
        except Exception as error:
            o.fault='Uncertain dither configuration: '+str(error);o.persist_fault();raise
        path=o.audit_path.parent/(kind.replace('_','-')+'-'+str(o.lease)+'.json')
        result=dict(settings=settings,programmed=programmed,before=state,after=after,reserved=reserved,
                    paused=paused,reason=reason,lease=o.lease,armed=self.armed,evidence=str(path))
        self.programmed_dither=result
        atomic_json(path,result)
        o.audit(kind,reason=reason,lease=o.lease,armed=self.armed,programmed=programmed,before=state['current'],
                after=after['current'],outputs=after['outputs'],evidence=str(path))
        self.dither_unavailable(DITHER_PAUSED_REASON if paused else 'Dither resumed; awaiting a new fit',
                                status='paused' if paused else 'not_analyzed')
        if self.armed:self.persist()
        return result
    def check_dither(self,state,p):
        expected=p['dither'].get('programming')
        actual=state['current']
        if not isinstance(expected,dict):raise DeviceError('Verified dither programming evidence is missing')
        if actual.get('waveform')!='ARB' or not arb_name_matches(actual.get('arb_name'),expected['name']):
            raise DeviceError('Current waveform differs from verified dither')
        for field,expected_value in (('frequency_hz',expected['frequency_hz']),('amplitude_vpp',expected['nominal_amplitude_vpp'])):
            if not math.isclose(finite(actual.get(field)),finite(expected_value),rel_tol=1e-6,abs_tol=1e-9):
                raise DeviceError('Current '+field+' differs from verified dither')
    def native(self,state,observation,p,locked):
        cfg=state['servo_config'];status=state['servo_status'];route=p['native_input']
        if status.get('firmware_version')!=p['firmware_version']:raise DeviceError('Native firmware differs from verified handover profile')
        if status.get('pid_time_based') is not True:raise DeviceError('Native time-based PI is required')
        for key in ('multiplier_control','multiplier_target','software_modulation_enable'):
            if cfg.get(key)!=0:raise DeviceError('Unexpected native feedforward or modulation')
        if cfg.get('valid_data_index')!=route['valid_data_index']:raise DeviceError('Native wavelength field differs from commissioned input')
        for key in ('channel','cycle','src'):
            if status.get('last_udp_'+key)!=route[key]:raise DeviceError('Native wavelength route mismatch: '+key)
        # v5.1-hostcheck (human decision 2026-10-22): the firmware consumes the wavemeter UDP records itself and
        # accepts index/seq/value band only. The host does not re-validate that data afterwards: UDP quality/flags,
        # staleness, interval and native-vs-optical difference are REPORTED (native_input_fields), never refused.
        if status.get('system_src')!=1:raise MeasurementUnavailable('Native input must be UDP wavelength data')
        now=self.o.clock();seq=status.get('last_udp_seq')
        if not isinstance(seq,int) or isinstance(seq,bool):raise MeasurementUnavailable('Native input sequence is unavailable')
        if self.last_seq is not None and seq<self.last_seq:
            previous=self.last_seq
            # Refuse ONLY this sample for host decisions and re-baseline (a restarted source does not lock the owner
            # out). Reported as an audit event and status fields. The worker recognises SEQUENCE_REGRESSED and never
            # counts it toward its measurement-outage timer.
            self.last_seq=seq;self.last_seq_at=now
            self.native_sequence_regressions+=1
            self.native_sequence_last_regression=dict(previous_seq=previous,seq=seq,unix_s=time.time())
            self.o.audit('native_input_sequence_regressed',previous_seq=previous,seq=seq,count=self.native_sequence_regressions)
            raise MeasurementUnavailable(SEQUENCE_REGRESSED+' (%d -> %d)'%(previous,seq))
        if self.last_seq is None:self.last_seq=seq;self.last_seq_at=None   # not yet seen to advance
        elif seq!=self.last_seq:self.last_seq=seq;self.last_seq_at=now
        age=None if self.last_seq_at is None else now-self.last_seq_at
        stale=age is None or age>self.o.device['control_settings']['max_sample_age_s']
        raw_interval=status.get('last_system_interval_us')
        interval=raw_interval*1e-6 if type(raw_interval) in (int,float) and math.isfinite(raw_interval) else None
        if interval is not None:self.native_interval_samples=(self.native_interval_samples+[interval])[-15:]
        samples=sorted(self.native_interval_samples)
        difference=None
        try:
            native_hz=C_NM/finite(status.get('system_value'))
            optical_hz=observation['frequency']['frequency_hz']
            if type(optical_hz) in (int,float) and math.isfinite(optical_hz):difference=native_hz-optical_hz
        except (ValueError,TypeError,KeyError,ZeroDivisionError):pass
        previous_stale=(self.native_input_report or {}).get('native_input_stale')
        self.native_input_report=dict(native_input_stale=stale,native_input_age_s=age,native_input_seq=seq,
            native_input_quality=status.get('last_udp_quality'),native_input_flags=status.get('last_udp_flags'),
            native_interval_last_s=interval,native_interval_median_s=samples[len(samples)//2] if samples else None,
            native_optical_difference_hz=difference)
        if previous_stale is not stale:
            self.o.audit('native_input_stale' if stale else 'native_input_fresh',report_only=True,**self.native_input_report)
        if locked and cfg.get('pid_multiplier')!=1:raise DeviceError('Native PI is no longer enabled')
        return interval
    def native_input_fields(self):
        """Report-only host view of the native wavelength input (never a refusal; human decision 2026-10-22)."""
        return dict(self.native_input_report or dict(native_input_stale=None,native_input_age_s=None),
                    native_sequence_regressions=self.native_sequence_regressions,
                    native_sequence_last_regression=self.native_sequence_last_regression)
    def gain_interval(self,interval,p):
        """The measured interval used to DERIVE PI gains at arm (not input validation): must be finite and in bounds."""
        if interval is None or not p['native_interval_bounds_s'][0]<=interval<=p['native_interval_bounds_s'][1]:
            raise DeviceError('Measured native interval for PI gain derivation is outside the profile bounds; set native_interval_reference_s')
        return interval
    def write(self,values,reason):
        o=self.o;o.audit('native_pi_requested',reason=reason,values=values)
        try:
            o.hw().connections['piezo'].set(values)
            actual=o.hw().connections['piezo'].get('config')
            for key,value in values.items():
                if key=='pid_reset_state':continue # action flag, not persistent configuration
                if isinstance(value,(int,float)):
                    if not math.isclose(actual[key],value,rel_tol=1e-8,abs_tol=1e-9):raise DeviceError('Native PI configuration did not apply: '+key)
                elif actual[key]!=value:raise DeviceError('Native PI configuration mismatch: '+key)
        except Exception as error:
            o.fault='Uncertain native PI write: '+str(error);raise
        o.audit('native_pi_applied',reason=reason,values=values)
        self.note_servo_config(actual)
        if self.active:self.persist()
    def hold(self,reason):
        o=self.o
        self.clear_target_response('lock hold: '+reason)
        self.discard_proposals('lock hold: '+reason)
        if o.fault:
            if self.armed and self.arm_reference is not None:return self.fault_disable(reason)
            raise DeviceError('Uncertain native state: lease retained for review')
        if not self.armed:return dict(held=True,already_open=True)
        from ldcon.owners.ecdl_locked_sweep import active as sweep_active
        if sweep_active(self):
            # Restore the AWGs and firmware first (no settle wait), then the usual verified handover.
            o.audit('locked_sweep_hold_restore',reason=reason,state=self.sweep.state)
            self.sweep.abort_reason=self.sweep.abort_reason or 'hold: '+reason
            try:self.sweep.restore_now('hold: '+reason)
            except Exception:
                if self.armed and self.arm_reference is not None:return self.fault_disable(reason)
                raise
            if self.sweep.state=='settling':
                self.sweep.state='restored_unverified'
                self.sweep.restore_result.update(restored=False,reason='native lock held; no settle measurement')
        state=o.hw().snapshot();dac=state['outputs']['piezo']
        o.audit('native_hold_requested',reason=reason,dac=dac)
        try:
            # This hardware handover is gated by device-specific commissioning.
            # Same-message settings alone do not establish atomic firmware updates.
            o.hw().prepare(dac);after=o.hw().snapshot()
            tolerance=o.device['control_settings']['axes']['piezo']['readback_tolerance']
            if not after['open_loop'] or abs(after['outputs']['piezo']-dac)>tolerance:
                raise DeviceError('Native hold handover mismatch')
        except Exception as error:
            o.fault='Uncertain native hold: '+str(error);raise
        self.active=False;self.armed=False;self.deadline=None;self.stop_event.set()
        if self.pid_tuning:self.pid_tuning.closed=True
        if self.study and self.study.status=='running':
            self.study.status='failed';self.study.reason='Stationary lock ended; held actual outputs without blind current return'
        o.audit('native_hold_applied',reason=reason,outputs=after['outputs'])
        self.persist()
        if self.dither_hardware_paused():
            # Invariant: an unarmed rig holds the reviewed dither sine (locked-sweep prepare, claim cleanup and
            # restore_reviewed_dither assume it). The pause lives only with the armed lock; the next arm re-pauses.
            self.program_dither(False,'lock hold: '+reason,self.p)
        return dict(held=True,outputs=after['outputs'])
    def fault_disable(self,reason):
        """A new fail-safe disable, never a retry of an uncertain enable."""
        o=self.o;hw=o.hardware
        if hw is None:raise DeviceError('No existing native transport for fail-safe disable')
        o.audit('commission_fault_disable_requested',reason=reason,fault=o.fault)
        try:
            hw.connections['piezo'].set(dict(pid_multiplier=0))
            state=hw.snapshot()
            if not state['open_loop'] or abs(state['outputs']['piezo']-self.arm_reference['piezo'])>1.:
                raise DeviceError('Fail-safe fixed-DAC disable could not be verified')
        except Exception as error:
            o.fault+='; fail-safe disable uncertain: '+str(error)
            raise DeviceError(o.fault) from error
        self.active=False;self.armed=False;self.stop_event.set()
        o.persist_fault()
        self.persist()
        o.audit('commission_fault_disable_applied',outputs=state['outputs'],fault=o.fault)
        return dict(held=True,outputs=state['outputs'],requires_review=True,lease_retained=True)
    def _watch(self):
        while not self.stop_event.wait(.5):
            if self.watch_tick() is False:return

    def watch_tick(self):
        """One watchdog pass (the thread calls it every 0.5 s). Returns False when the thread must end: the heartbeat
        deadline held the lock, or a pending owner-restart adoption was refused."""
        with self.o.lock:
            adoption=getattr(self.o,'adoption',None)
            if adoption is not None and adoption.get('state')=='pending':
                self.o.adopt_tick()
                return adoption.get('state')!='refused'
            self.sync_target_response()
            if self.armed and self.deadline is not None and self.o.clock()>self.deadline:
                try:self.hold('supervisor heartbeat expired')
                except Exception as error:self.o.audit('native_watchdog_fault',error=str(error))
                return False
            from ldcon.owners.ecdl_background_pid import poll_measurement_grant
            if self.armed:poll_measurement_grant(self)
            from ldcon.owners.ecdl_background_parameters import active as parameter_epoch,grant as parameter_grant,tick as parameter_tick
            if self.armed and parameter_epoch(self):
                e=self.background.epoch
                if e['state']=='active':
                    try:parameter_grant(self,e)
                    except (ValueError,KeyError,TypeError):self.background.returning('grant_revoked')
                    if self.o.clock()>=e['expires_at']:self.background.returning('experiment_timeout')
                if e['state']=='returning':
                    try:
                        state,obs=self.read_feedback();self.o.validate_state(state,self.o.policies())
                        self.native(state,obs,self.p,locked=True);parameter_tick(self,state,obs)
                    except MeasurementUnavailable:
                        if self.o.clock()>e.get('return_deadline',math.inf):self.background.block('Checkpoint restoration deadline exceeded')
                    except Exception as error:self.background.block(str(error))
            return True
    def background_context(self, obs):
        authority=self.o.policy_authority
        return dict(system_id=authority.system_id,generation=authority.generation,
                    policy_revision=authority.revision,lease=hashlib.sha256(self.o.lease.encode()).hexdigest(),
                    target=self.target,mode_id=obs.get('mode_id'))

    def reserve_background(self,identifiers):
        status=self.background.reserve(identifiers)
        if self.background.epoch and self.background.epoch.get('family')=='pi':
            from ldcon.owners.ecdl_background_pid import request_return,restore_atomic
            if self.background.epoch.get('completion_mode')=='verified_handover':
                restore_atomic(self,'foreground')
            else:request_return(self)
        self.discard_proposals('foreground reservation')
        return self.background.status()

    def background_dispatch(self, request):
        """Current/PI epochs; all writes use existing bounded owner paths.

        Never restore instantaneous PI DAC or integral state. Parameter epochs
        also prove the return of actual current/TEC outputs.
        """
        from experiment_core.errors import OperationError
        from ldcon.core.instrument_client import BrokerError
        o=self.o;bg=self.background;action=request['action']
        if action=='background_status':
            from ldcon.core.adjustment_registry import capabilities
            extra=capabilities('servo.ecdl_steady_lock',o.device)
            from ldcon.owners.ecdl_background_parameters import values
            extra['actual_parameters']=values(self) if self.armed else None
            return dict(bg.status(),owner_fault=o.fault,native_pi_active=self.active,idle_foreground_fencing=True,**extra,
                capabilities=dict(current_epochs=dict(supported=True,version=1,
                modified_axes=['current'],native_pi_preserved=True,explicit_return_budget=True),
                pi=dict(supported=(self.expected_servo or {}).get('pi_handover_capability')==1,
                        version=3,strategy='validated_candidates',runtime_only=True,
                        completion_mode='verified_handover',optical_settle_required_for_handoff=False,
                        requires_firmware='pi_handover v1',
                        save_settings=False,raw_integral_bounds_fixed=False,
                        candidate_fields=['kp_count_per_nm','ki_count_per_nm_s','integral_limit_count']),
                **extra['parameter_adjustments']))
        if action=='background_reserve':
            return self.reserve_background(request['request_ids'])
        if action=='background_release':return bg.release(request['request_ids'])
        if action=='background_abandon':
            # Safety stop/recovery never moves back to a historical checkpoint.
            if bg.epoch:
                bg.block(request.get('reason','parent safety/recovery action'))
                self.response_control=False;self.discard_proposals('background abandoned for safety')
                if bg.epoch.get('family')=='pi' and self.pid_tuning:self.pid_tuning.closed=True
            return bg.status()
        if o.fault:raise DeviceError('Owner fault prevents background restoration: '+str(o.fault))
        if action=='background_restore':
            if bg.epoch is None or request.get('token')!=bg.epoch['token']:
                raise BrokerError('configuration','Background epoch is absent or replaced')
            if bg.epoch.get('completion_mode')=='verified_handover':
                from ldcon.owners.ecdl_background_pid import restore_atomic
                return restore_atomic(self,request.get('reason','epoch completed'))
            bg.returning(request.get('reason','epoch completed'))
            if bg.epoch.get('family')=='pi':
                from ldcon.owners.ecdl_background_pid import request_return
                request_return(self)
            if bg.epoch and o.clock()>bg.epoch.get('return_deadline',math.inf):
                return bg.block('Checkpoint restoration deadline exceeded')
        try:
            state,obs=self.read_feedback();o.validate_state(state,o.policies())
            self.native(state,obs,self.p,locked=True)
            context=self.background_context(obs)
            policy=o.policies()['current'];tolerance=max(policy.resolution/2,1e-12)
            if action=='background_begin':
                if (not self.active or self.commissioning or self.response_control or o.recovery is not None
                        or self.current_target!=self.target or self.study and self.study.status=='running'
                        or self.pid_tuning and not self.pid_tuning.closed
                        or self.sweep is not None and self.sweep.state not in self._sweep_finished()):
                    raise BrokerError('configuration','Another owner operation excludes background work')
                interval=finite(request['poll_interval_s'])
                if not 0<interval<=5:raise ValueError('Invalid background poll interval')
                identity=request['identity']
                from ldcon.core.adjustment_registry import PARAMETER_FAMILIES
                identity_keys={'monitor_id','generation','controller_job_id','grant_revision'} if request.get('family') in PARAMETER_FAMILIES else {'monitor_id','generation'}
                if isinstance(identity,dict) and 'measurement_job_id' in identity:
                    identity_keys={'monitor_id','generation','controller_job_id','grant_revision','measurement_job_id'}
                    from ldcon.owners.background_grants import read_grant
                    read_grant(o.laser['id'],identity,lease=o.lease)
                if (not isinstance(identity,dict) or set(identity)!=identity_keys
                        or not all(isinstance(v,str) and v for v in identity.values())):
                    raise ValueError('Monitor identity and generation are required')
                family=request.get('family','current')
                if family in PARAMETER_FAMILIES:
                    from ldcon.owners.ecdl_background_parameters import begin
                    return begin(self,request,context,state,obs)
                if family=='pi':
                    from ldcon.owners.ecdl_background_pid import begin
                    return begin(self,request,context,identity,interval)
                if family!='current':raise ValueError('Unsupported background adjustment family')
                validate_dither_profile(self.p)
                span=finite(request['half_span_v'])
                if span<=0:raise ValueError('Invalid background span')
                checkpoint=dict(current=state['outputs']['current'],temperature=state['outputs']['temperature'])
                epoch=bg.begin(request['expected_generation'],context,checkpoint,request['handoff_timeout_s'],identity)
                bg.epoch.update(low=max(policy.low,checkpoint['current']-span),
                                high=min(policy.high,checkpoint['current']+span),poll_interval_s=interval)
                o.audit('background_checkpoint',**bg.epoch)
                self.response_control=True;self.discard_proposals('background measurement epoch')
                dc=self.p['dither']
                dwell=policy.settle_s+max(dc['window_s'],8.,3/dc['frequency_hz']+1.)+dc.get('delivery_delay_s',3.)
                return dict(started=True,minimum_dwell_s=dwell,epoch=bg.status()['epoch'])
            token=request['token']
            from ldcon.owners.ecdl_background_parameters import active as parameter_epoch,dispatch as parameter_dispatch
            if parameter_epoch(self):return parameter_dispatch(self,request,context,state,obs)
            if bg.epoch and bg.epoch.get('family')=='pi':
                from ldcon.owners.ecdl_background_pid import dispatch
                return dispatch(self,request,context,state)
            epoch=bg.validate(token,context,restoring=action=='background_restore')
            tp=o.policies()['temperature']
            if abs(state['outputs']['temperature']-epoch['checkpoint']['temperature'])>max(tp.resolution/2,1e-12):
                bg.block('Temperature changed during the background epoch')
                if action=='background_restore':return bg.status()
                raise BrokerError('configuration','Background checkpoint context changed; no current effect was applied')
            if action=='background_current':
                target=finite(request['value'])
                if not epoch['low']<=target<=epoch['high']:
                    raise BrokerError('range','Background current target exceeds checkpoint envelope')
                step=min(policy.max_step,policy.max_slew_per_s*policy.update_interval_s)
                if policy.resolution:step=math.floor((step+1e-12)/policy.resolution)*policy.resolution
                if step<=0:raise BrokerError('range','Current policy has no returnable step')
                steps=math.ceil(max(0.,abs(target-epoch['checkpoint']['current'])-tolerance)/step)
                return_bound=(steps+1)*max(policy.update_interval_s,epoch['poll_interval_s'])
                if return_bound>epoch['timeout_s']:
                    raise BrokerError('range','Current trial cannot meet the admitted handoff deadline')
            elif action in ('background_restore','background_adopt'):
                target=epoch['checkpoint']['current']
                if action=='background_adopt' or abs(state['outputs']['current']-target)<=tolerance:
                    # read_feedback above is fresh readback; no full dither wait.
                    self.response_control=False;self.discard_proposals('background response cleanup')
                    receipt=dict(confirmed=True,response_ended=True,current=state['outputs']['current'],
                                 native_pi_active=self.active,modified_axes=['current'])
                    status=bg.complete(token,context,receipt,adopted=action=='background_adopt')
                    bg.release([r for r in bg.reservations if r.startswith('direct:')])
                    o.audit('background_completed',**status['last'])
                    return bg.status()
            else:raise ValueError('Unknown background operation')
            proposal=dict(axis='current',value=target,reason='background checkpoint return' if action=='background_restore' else 'background current trial')
            self.proposed=[proposal]
            try:receipt=self.adjust(dict(proposal=proposal))
            except MeasurementUnavailable:
                self.discard_proposals('background prewrite refusal')
                raise
            except Exception as error:
                bg.block('Uncertain background effect: '+str(error))
                raise
            return dict(bg.status(),**receipt)
        except (OperationError,ValueError) as error:
            # These are owner-confirmed prewrite rejections. A blocked return
            # remains visible while native PI continues; no fabricated handoff.
            if action=='background_restore' and bg.epoch:
                bg.block(str(error));return bg.status()
            raise BrokerError('configuration',str(error)) from error

    def dispatch(self,request):
        o=self.o;action=request['action']
        self.sync_target_response()
        foreground={'steady_parameters','steady_hot_control','steady_set_reference',
            'steady_pid_begin','steady_study_start','response_begin','response_current','response_end',
            'locked_sweep_prepare','locked_sweep_calibrate','configure_dither','load_reviewed_dither',
            'steady_arm','commission_arm','background_begin','background_reserve','background_abandon'}
        if action in foreground or action=='steady_set_target' and not request.get('target_ff_study_token'):
            self.clear_target_response('foreground '+action)
        if action.startswith('background_'):return self.background_dispatch(request)
        conflicting={'steady_target_ff_study_set','steady_parameters','steady_hot_control','steady_set_reference','steady_set_target',
                     'steady_pid_begin','steady_study_start','response_begin','response_current','response_end',
                     'locked_sweep_prepare','locked_sweep_calibrate','configure_dither','load_reviewed_dither'}
        if self.background.epoch and action in conflicting:
            from ldcon.core.instrument_client import BrokerError
            self.reserve_background(['direct:'+action])
            if self.background.epoch:
                raise BrokerError('configuration','Background checkpoint restoration is pending; foreground operation has not run')
        if action in conflicting:
            # Even between background segments, a direct foreground request
            # invalidates old plans. Parent hold/recovery/arm and regular slow
            # proposals are deliberately outside this explicit action set.
            self.background.generation+=1
        if action.startswith('locked_sweep_') or action=='restore_reviewed_dither':
            from ldcon.owners.ecdl_locked_sweep import dispatch as sweep_dispatch
            return sweep_dispatch(self,request)
        from ldcon.owners.ecdl_locked_sweep import active as sweep_active
        if action=='steady_adjust' and sweep_active(self) and getattr(self.sweep,'continuous',False):
            # v5.4: TEC following during a playing continuous triangle (owner TEC proposal only; refused otherwise)
            return self.sweep.tec_adjust(request)
        if sweep_active(self) and action not in SWEEP_ALLOWED:
            raise DeviceError('A locked sweep owns the rig until it is restored')
        if action=='steady_read' and sweep_active(self):
            return self.sweep.steady_row()
        if action.startswith('steady_target_ff_study_'):
            return self.target_ff_study_dispatch(request)
        if action=='response_dither_info':
            if self.dither_off():
                # Not a measurement failure: consumers (hot-control trims, window centre) read the 'paused' status.
                self.dither_unavailable(DITHER_PAUSED_REASON,status='paused')
                return self.dither_fields()
            self.passive_dither()
            return self.dither_fields()
        if action=='response_info':
            state,obs=self.read_feedback(spectral_only=o.recovery is not None)
            o.validate_state(state,o.policies())
            raw=state['servo_status'].get('pid_integral');cfg=state['servo_config']
            bound=max(abs(cfg.get('pid_integral_min',0)),abs(cfg.get('pid_integral_max',0)))
            fraction=abs(raw)/bound if type(raw) in (int,float) and math.isfinite(raw) and bound>0 else None
            if request.get('include_patterns') is True:
                # This is the frame used by read_feedback above, never another
                # HTTP fetch or a folded/averaged substitute for the raw pixels.
                from dataclasses import asdict
                from ldcon.core.interface_schema import digest
                frame=getattr(o.hw(),'last_optical_frame',None)
                if frame is None or asdict(frame.frequency)!=obs['frequency']:
                    raise MeasurementUnavailable('Raw optical frame does not match this observation')
                optical=o.device['control_settings']['optical']
                obs={**obs,'fringe_analysis':{**(obs.get('fringe_analysis') or {}),
                    'raw_patterns':[asdict(pattern) for pattern in frame.patterns],
                    'channel':frame.channel,'classification_revision':digest(optical),
                    'classification_method':optical.get('metric_method','unconfigured'),
                    'classification_enabled':optical.get('classification_enabled',False)}}
            authority=o.policy_authority
            return dict(owner_context=dict(system_id=authority.system_id,generation=authority.generation,
                            policy_revision=authority.revision),
                        background=self.background.status(),
                        outputs=state['outputs'],observation=obs,open_loop=state['open_loop'],
                        native_pi_active=bool(self.active and not state['open_loop']),target_frequency_hz=self.target if self.active else None,
                        ramped_target_hz=self.current_target if self.active else None,
                        target_feedforward_enabled=bool(getattr(self,'p',{}).get('target_feedforward_enabled',False)),
                        target_feedforward=copy.deepcopy(self.target_ff),
                        target_ff_study=self.target_ff_study_status(),servo_config=copy.deepcopy(cfg),
                        servo_status=state['servo_status'],native_integral_fraction=fraction,
                        tec_context=self.tec_context_fields(),activation=dict(o.activation) if o.activation else None)
        if action=='load_reviewed_dither':
            from pathlib import Path
            path=Path(request['evidence']).resolve()
            if path.parent!=o.audit_path.parent.resolve() or not path.name.startswith('dither-reviewed-') or path.suffix!='.json':
                raise DeviceError('Only owner-generated reviewed dither evidence may be loaded')
            proof=json.loads(path.read_text());programmed=proof['programmed']
            if not proof.get('reviewed_fault') or self.armed or o.recovery is not None:
                raise DeviceError('Unarmed, reviewed dither evidence is required')
            state=o.hw().snapshot();o.validate_state(state,o.policies());current=state['current']
            if (not state['open_loop'] or not arb_name_matches(current['arb_name'],programmed['name'])
                    or abs(current['frequency_hz']-programmed['frequency_hz'])>1e-6
                    or abs(current['value']-programmed['offset_v'])>1e-8):
                raise DeviceError('Actual dither differs from reviewed evidence')
            if any(current[k]!=v for k,v in proof['after']['current'].items() if k not in ('arb_name','frequency_hz')):
                raise DeviceError('Reviewed current output configuration changed')
            if o.hw().reserved_readbacks()!=proof['reserved']:raise DeviceError('Reserved channels changed after review')
            self.programmed_dither=proof
            o.audit('reviewed_dither_loaded',evidence=str(path),outputs=state['outputs'])
            return dict(loaded=True,evidence=str(path))
        if action=='configure_dither':
            if self.armed or o.recovery is not None:raise DeviceError('Dither configuration requires an open, idle rig')
            p=self.dither_profile();state=o.hw().snapshot();obs=o.observation();o.validate_state(state,o.policies())
            if self.dither_paused(p):raise DeviceError('Dither commissioning programs the verified waveform; clear steady_lock.dither.paused first')
            self.native(state,obs,p,locked=False)
            if not state['open_loop']:raise DeviceError('Dither programming requires an open-loop servo')
            ready=self.qualify(state,obs,finite(request['target_frequency_hz']),finite(request['tolerance_hz']),p)
            if not ready['ready']:raise MeasurementUnavailable('Dither programming requires owner-qualified initial point')
            if not obs['single_mode'] or obs['maintenance_required']:raise MeasurementUnavailable('Dither programming requires a clean single mode')
            reserved=o.hw().reserved_readbacks();o.audit('dither_requested',settings=p['dither'],outputs=state['outputs'],reserved=reserved)
            try:
                programmed=o.hw().configure_dither(p['dither'],state['current']);after=o.hw().snapshot()
                o.audit('dither_readback',requested=programmed,after=after)
                o.validate_state(after,o.policies())
                if not arb_name_matches(after['current']['arb_name'],programmed['name']) or abs(after['current']['frequency_hz']-p['dither']['frequency_hz'])>1e-6:
                    raise DeviceError('Selected dither readback mismatch: name='+repr(after['current']['arb_name'])+' expected='+repr(programmed['name'])+' frequency='+repr(after['current']['frequency_hz'])+' expected_frequency='+repr(p['dither']['frequency_hz']))
                for key in state['current']:
                    if key not in ('arb_name','frequency_hz') and after['current'][key]!=state['current'][key]:raise DeviceError('Dither programming changed current output configuration: '+key)
                if after['outputs']!=state['outputs'] or after['temperature']!=state['temperature'] or not after['open_loop']:
                    raise DeviceError('Dither programming changed held rig state')
                if o.hw().reserved_readbacks()!=reserved:raise DeviceError('Reserved channel state changed during dither programming')
            except Exception as error:
                o.fault='Uncertain dither configuration: '+str(error);raise
            self.qualification=[];self.qualification_context=None
            path=o.audit_path.parent/('dither-'+o.lease+'.json')
            from ldcon.core.records import atomic_json
            result=dict(settings=p['dither'],programmed=programmed,before=state,after=after,reserved=reserved,evidence=str(path))
            self.programmed_dither=result
            atomic_json(path,result);o.audit('dither_applied',**result);return result
        if action in ('steady_ready','commission_ready'):
            p=self.commission_profile(request.get('target_frequency_hz')) if action=='commission_ready' else self.profile(request['strategy'])
            state=o.hw().snapshot();obs=o.observation()
            o.validate_state(state,o.policies());self.native(state,obs,p,locked=False)
            if not state['open_loop']:raise DeviceError('Stationary acquisition requires a verified open-loop initial point')
            # An idle target edit changes laser.target only. The firmware still
            # carries the previous branch's target until steady_arm; never use
            # that historical value to reject the requested acquisition.
            configured=(getattr(o,'laser',{}) or {}).get('target') or {}
            target=(finite(request['target_frequency_hz']) if 'target_frequency_hz' in request else
                    finite(configured['frequency_hz']) if 'frequency_hz' in configured else
                    C_NM/finite(state['servo_config']['target']))
            ready = entry_quality(obs,p,target) if action=='commission_ready' else control_ready(obs,p,target)
            if not ready:
                detail=('Initial point has no valid frequency/target' if action=='steady_ready' else
                        'Initial point spectrum fails commissioning gate')
                measured=obs['frequency']['frequency_hz']
                raise DeviceError(detail+' (measured %s THz, target %.9f THz, mode %s, expected %s)' %
                                  (None if measured is None else '%.9f'%(measured/1e12),target/1e12,obs.get('mode_id'),
                                   (p.get('entry_gate') or {}).get('mode_id')))
            if action=='commission_ready':
                target=finite(request['target_frequency_hz']);tolerance=finite(request['tolerance_hz'])
                return self.qualify(state,obs,target,tolerance,p)
            return dict(ready=True,revision=p['revision'])
        if action in ('steady_arm','commission_arm'):
            if self.armed or o.recovery is not None:raise DeviceError('Native lock or recovery is already active')
            self.commissioning=action=='commission_arm'
            if self.parameter_lease!=o.lease:
                self.parameter_lease=o.lease;self.parameter_overrides={}
            self.strategy='hold' if self.commissioning else self.parameter_overrides.get('strategy',request['strategy'])
            self.p=copy.deepcopy(self.commission_profile(request.get('target_frequency_hz')) if self.commissioning else self.profile(self.strategy))
            self.parameter_gain_ceiling=self.p['tec_gain']
            self.parameter_ff_config=copy.deepcopy(self.p.get('tec_current_feedforward'))
            if not self.commissioning:
                if 'tec_gain' in self.parameter_overrides:self.p['tec_gain']=self.parameter_overrides['tec_gain']
                if self.parameter_overrides.get('current_ff_enabled') is False:self.p.pop('tec_current_feedforward',None)
            state=o.hw().snapshot();obs=o.observation();o.validate_state(state,o.policies())
            if not self.commissioning:
                self.check_dither(state,self.p)
                if self.dither_paused()!=self.dither_hardware_paused():
                    # A lock started with dither.paused already true programs the zero payload from the start
                    # (open loop, before any PI write); an unpaused profile over a paused bank resumes it.
                    self.program_dither(self.dither_paused(),'lock start',self.p)
                    state=o.hw().snapshot();obs=o.observation();o.validate_state(state,o.policies())
            interval=self.native(state,obs,self.p,locked=False)
            target=finite(request['target_frequency_hz']);tolerance=finite(request['tolerance_hz'])
            if target<=0 or tolerance<=0:raise DeviceError('Invalid stationary target')
            if self.commissioning:
                if not self.programmed_dither:raise DeviceError('Requested dither has not been prepared by this owner')
                if not arb_name_matches(state['current'].get('arb_name'),self.programmed_dither['programmed']['name']):
                    raise DeviceError('Prepared dither bank has changed')
                qualification=self.qualify(state,obs,target,tolerance,self.p)
                if not qualification['ready']:raise MeasurementUnavailable('Initial point requires an owner-qualified current reading')
                # Gain derivation uses the median of the recently observed native intervals (report history).
                interval=self.gain_interval(self.native_input_fields().get('native_interval_median_s'),self.p)
                if self.trial and self.trial.fixed_stopped and not self.trial.current_held:
                    step=self.p['commissioning']['step_hz']
                    measured_now=obs['frequency']['frequency_hz']
                    target=measured_now+math.copysign(step,target-measured_now)
                    self.trial.hold_target=target
                self.qualification=[]
            elif self.p.get('native_interval_reference_s') is not None:
                interval=finite(self.p['native_interval_reference_s'])
                if not self.p['native_interval_bounds_s'][0]<=interval<=self.p['native_interval_bounds_s'][1]:raise DeviceError('Verified native interval is outside the profile bounds')
            else:interval=self.gain_interval(interval,self.p)
            ready = entry_quality(obs,self.p,target) if self.commissioning else control_ready(obs,self.p,target)
            if not state['open_loop'] or not ready:
                raise DeviceError('A verified open-loop point with a valid frequency and target is required')
            measured=obs['frequency']['frequency_hz']
            if abs(target-measured)>min(tolerance,self.p['capture_hz']):raise DeviceError('Initial point is outside stationary handover tolerance')
            slope=abs(self.p['wavelength_nm_per_count']);ki=self.p['g_sample']/(interval*slope)
            wavelength=C_NM/measured;target_nm=C_NM/target;band=self.p['capture_hz']
            values=dict(target=wavelength,control_offset=state['outputs']['piezo'],
                multiplier_control=0,multiplier_target=0,software_modulation_enable=0,
                system_min=C_NM/(target+band),system_max=C_NM/(target-band),
                pid_kp=self.p['p_sample']/slope,pid_ki=ki,pid_kd=0,
                pid_integral_min=-self.p['integral_limit_count']/ki,
                pid_integral_max=self.p['integral_limit_count']/ki,
                log_force_full_data_on_new_system=1,log_forced_full_data_min_interval=0)
            missing=set(values)-set(state['servo_config'])
            if missing:raise DeviceError('Native firmware configuration keys unavailable: '+', '.join(sorted(missing)))
            servo=o.hw().connections['piezo']
            if hasattr(servo,'require_write_support'):servo.require_write_support()
            self.armed=True # unknown arm must retain lease, never silently release
            self.arm_reference=dict(state['outputs'])
            self.deadline=o.clock()+self.p['watchdog_s'];self.stop_event=threading.Event()
            self.start_watch()
            self.write(values,'soft acquisition at measured wavelength')
            self.write(dict(pid_reset_state=1),'reset PI state before enabling')
            self.write(dict(pid_multiplier=1),'enable native PI')
            self.target_ff=None  # Initial measured-wavelength acquisition retains its existing PI-only ramp.
            self.target_move_policy=None
            self.target=target;self.current_target=measured;self.interval=interval;self.last_target=o.clock()
            self.response_control=False
            self.dither_unavailable('New native lock acquisition',status='not_analyzed')
            self.tolerance=tolerance;self.active=True;self.last_supervision=o.clock();self.pending=[];self.proposed=[]
            self.parameter_tolerance_ceiling=tolerance
            self.parameter_sequence+=1
            effective={}
            for name,p in o.policies().items():
                bounds=o.device['absolute_conditions']['axes'][name]['activation_range']
                half=0 if name=='piezo' else state[name]['amplitude_vpp']/2
                effective[name]=replace(p,low=max(p.low,o.activation[name]+bounds[0])+half,
                                       high=min(p.high,o.activation[name]+bounds[1])-half)
            if not self.commissioning:
                # A new lease starts a new operating-point context. Automatic
                # hop recovery re-arms within this lease and retains its target.
                new_context=self.tec_reference_lease!=o.lease
                if new_context:
                    self.tec_ff_paused=self.p.get('hot_control',{}).get('initial_tec_ff_paused',False)
                    self.tec_reference_lease=o.lease
                    retained=self.p.get('retained_tec_reference_dac')
                    self.tec_reference_dac=state['outputs']['piezo'] if retained is None else finite(retained)
                    self.tec_reference_confirmed=retained is not None
                self.tec_reference_pending=new_context and not self.tec_reference_confirmed
            self.planner=SteadyPlanner(self.p,effective,state['outputs'],self.strategy,
                tec_reference_dac=None if self.commissioning else self.tec_reference_dac)
            if self.commissioning:
                from ldcon.jobs.ecdl_pi_commission import Trial
                if self.trial and self.trial.fixed_stopped and not self.trial.current_held:
                    self.trial.stage=4;self.trial.window=[]
                else:self.trial=Trial(self.p,target,state['outputs'],o.clock)
            o.audit('native_lock_started',target_hz=target,native_interval_s=interval)
            if self.background.epoch:
                # Parent recovery/new lock has independently qualified and
                # armed this state. Old background tokens remain invalid; this
                # is not reported as a checkpoint restoration.
                self.background.last=dict(self.background.epoch,state='superseded_by_parent_arm',completed_at=o.clock())
                self.background.epoch=None
            self.persist()   # design §5: the armed lock is durable from this moment
            return dict(locked=True,target_frequency_hz=target)
        if action.startswith('commission_'):
            return self.commission_action(request)
        if action=='steady_hold':return self.hold(str(request.get('reason','requested')))
        if not self.active:raise DeviceError('Native stationary lock is not active')
        if action=='steady_parameters_status':return self.parameter_status()
        if action=='steady_parameters':return self.update_parameters(request)
        if action=='steady_hot_control':
            from ldcon.owners.ecdl_hot_control import command
            c=command(request['command'])
            if c['action']=='set_current_trim':raise DeviceError('Trim must run through the same-worker inbox')
            if self.response_control or self.study and self.study.status=='running' or self.pid_tuning and not self.pid_tuning.closed:
                raise DeviceError('Another controller operation is active')
            state,obs=self.read_feedback();self.native(state,obs,self.p,locked=True)
            self.discard_proposals('explicit hot control')
            if c['action'] in ('pause_tec_ff','resume_tec_ff'):
                self.tec_ff_paused=c['action']=='pause_tec_ff'
                self.planner.rebase_ff(state['outputs'])
            else:
                name=c['strategy'];self.strategy='hold'
                p=copy.deepcopy(self.p);validate_profile(p,self.strategy)
                if name=='center':p['tec_center_reference']='range_midpoint'
                self.p=p
                self.planner=SteadyPlanner(p,self.planner.policies,state['outputs'],self.strategy,tec_reference_dac=self.tec_reference_dac)
                self.parameter_overrides['strategy']=self.strategy
            self.parameter_sequence+=1
            o.audit('stationary_hot_control',command=c,outputs=state['outputs'])
            self.persist()
            return dict(strategy=self.strategy,tec_ff_paused=self.tec_ff_paused,native_pi_active=True)
        if action=='steady_set_reference':
            # Rule F generic operating-point card (2026-10-22): the references this running lock holds.
            #   piezo_center: the DAC centre the TEC outer loop drives toward (the approach itself keeps the existing
            #                 TEC max_step/slew/dt-cap bounds of the planner and locked_move);
            #   current:      re-anchor the planner's current origin/span after a bounded response_current move.
            axis=request.get('axis');value=finite(request['value'])
            if self.commissioning:raise DeviceError('No reference change during commissioning')
            if self.sweep is not None and self.sweep.state not in self._sweep_finished():raise DeviceError('No reference change during a locked sweep')
            if self.pid_tuning and not self.pid_tuning.closed or self.study and self.study.status=='running':
                raise DeviceError('Another controller operation is active')
            state,obs=self.read_feedback();self.native(state,obs,self.p,locked=True)
            if axis=='piezo_center':
                if self.response_control:raise DeviceError('Finish the response study before moving the DAC centre')
                if self.p.get('tec_center_reference')!='activation':
                    raise DeviceError("The DAC centre is the range midpoint under strategy 'center'; set strategy hold first")
                pp=self.planner.policies['piezo']
                if not pp.low<=value<=pp.high:raise DeviceError('DAC centre %.1f leaves the approved PZT interval [%.1f, %.1f]'%(value,pp.low,pp.high))
                previous=self.tec_reference_dac
                self.tec_reference_dac=value;self.tec_reference_confirmed=True;self.tec_reference_pending=False
                self.planner.tec_reference_dac=value
                self.discard_proposals('DAC centre reference moved')
                o.audit('tec_context_reference_set',dac=value,previous=previous,source='operating-point card',outputs=state['outputs'])
                self.persist()
                return dict(axis=axis,value=value,previous=previous,actual_dac=state['outputs']['piezo'])
            if axis=='current':
                cp=self.planner.policies['current'];actual=state['outputs']['current']
                if abs(actual-value)>max(cp.resolution,1e-9)*1.5:
                    raise DeviceError('The current reference must equal the held current (%.6g); move it first with response_current'%actual)
                previous=self.planner.origin['current']
                self.planner.origin['current']=value;self.planner.rebase_ff(state['outputs'])
                self.discard_proposals('current reference moved')
                o.audit('current_reference_set',value=value,previous=previous,outputs=state['outputs'])
                self.persist()
                return dict(axis=axis,value=value,previous=previous)
            raise DeviceError('Unknown locked reference axis '+str(axis)+' (piezo_center or current)')
        if action=='steady_set_target':
            # ONE target (2026-10-22): the owner ramps the firmware target (and its acceptance window) to the new value
            # at target_slew_hz_per_s, at most target_ramp_step_hz per read; steady_read judges the lock against the
            # ramped value so a legitimate ramp is never 'lost' or a 'hop'.
            target=finite(request['target_frequency_hz'])
            if target<=0:raise DeviceError('Invalid lock target')
            if self.commissioning:raise DeviceError('No target change during commissioning')
            if self.sweep is not None and self.sweep.state not in self._sweep_finished():raise DeviceError('No target change during a locked sweep')
            if self.pid_tuning and not self.pid_tuning.closed or self.response_control or self.study and self.study.status=='running':
                raise DeviceError('Another controller operation is active')
            if self.current_target!=self.target:raise DeviceError('A lock target ramp is in progress')
            bounds=self.target_bounds()
            if bounds is None:raise DeviceError('No target change range is configured (laser.target.allowed_change_hz)')
            if not bounds[0]<=target<=bounds[1]:
                raise DeviceError('Target %.6f THz is outside the configured bounds [%.6f, %.6f] THz'%(target/1e12,bounds[0]/1e12,bounds[1]/1e12))
            settings=self.target_ff_study.settings(request.get('target_ff_study_token'),*self.target_ff_study_context())
            ff=None
            if self.p.get('target_feedforward_enabled',False):
                state,obs=self.read_feedback();o.validate_state(state,o.policies())
                self.native(state,obs,self.p,locked=True)
                ff=self.prepare_target_ff(state,target,settings)
            self.discard_proposals('new target supersedes pending TEC/current planning')
            previous=self.target;self.target=target;self.target_ff=ff
            self.target_move_policy=settings
            o.audit('lock_target_set',target_hz=target,previous_hz=previous,ramped_from_hz=self.current_target,
                    target_slew_hz_per_s=self.p['target_slew_hz_per_s'],target_ramp_step_hz=self.target_ramp_step())
            self.persist()
            return dict(target_frequency_hz=target,previous_hz=previous,ramped_target_hz=self.current_target,
                        target_slew_hz_per_s=self.p['target_slew_hz_per_s'],target_ramp_step_hz=self.target_ramp_step(),
                        target_ramp_active=self.current_target!=self.target,target_feedforward=copy.deepcopy(self.target_ff))
        if action=='response_begin':
            if (self.study and self.study.status=='running') or (self.pid_tuning and not self.pid_tuning.closed):
                raise DeviceError('Another native study owns the current axis')
            self.response_control=True;self.discard_proposals('explicit response study')
            validate_dither_profile(self.p)
            dc=self.p['dither']
            minimum=o.policies()['current'].settle_s+max(dc['window_s'],8.,3/dc['frequency_hz']+1.)+dc.get('delivery_delay_s',3.)
            return dict(started=True,minimum_dwell_s=minimum)
        if action=='response_end':
            self.response_control=False;self.discard_proposals('explicit response study ended')
            o.audit('stationary_response_control_ended',native_pi_active=self.active)
            return dict(ended=True,native_pi_active=self.active)
        if action=='response_dither':return self.passive_dither()
        if action=='response_current':
            if not self.response_control:raise DeviceError('Explicit response study is required')
            proposal=dict(axis='current',value=finite(request['value']),reason='response study current step')
            self.proposed=[proposal]
            return self.adjust(dict(proposal=proposal))
        if action=='steady_pid_begin':
            if self.response_control:raise DeviceError('Explicit current response study is active')
            from ldcon.jobs.online_pid import parameters
            from ldcon.owners.ecdl_native_pid import NativePIDTuning
            if self.pid_tuning and not self.pid_tuning.closed:raise DeviceError('Native PI tuning already active')
            self.pid_tuning=NativePIDTuning(self,parameters(dict(parameters={}),request['parameters']))
            return self.pid_tuning.status()
        if action.startswith('steady_pid_'):
            trial=self.pid_tuning
            if not trial or trial.closed:raise DeviceError('No active native PI trial')
            if action=='steady_pid_target':return trial.target(request['pid'])
            if action=='steady_pid_save':return trial.save(request['pid'])
            if action=='steady_pid_finish':return trial.finish(request['status'])
            raise DeviceError('Unsupported native PI operation')
        if action=='steady_study_start':
            if self.response_control:raise DeviceError('Explicit current response study is active')
            if self.pid_tuning and not self.pid_tuning.closed:raise DeviceError('Native PI tuning is active')
            from ldcon.owners.ecdl_locked_study import parameters,LockedCurrentStudy
            if self.study and self.study.status=='running':raise DeviceError('Locked current study is already running')
            validate_dither_profile(self.p) # calibrated effective dither is mandatory
            if not self.p.get('nearby_modes',{}).get('components'):raise DeviceError('Nearby-mode indicator definition is unset')
            state=o.hw().snapshot();obs=o.observation();self.native(state,obs,self.p,locked=True)
            p=parameters(dict(parameters={}),request['parameters']);dc=self.p['dither']
            minimum=o.policies()['current'].settle_s+max(dc['window_s'],8.,3/dc['frequency_hz']+1.)+dc.get('delivery_delay_s',3.)
            self.study=LockedCurrentStudy(p,state['outputs']['current'],self.planner.policies['current'],o.clock(),minimum)
            self.study_temperature=state['outputs']['temperature'];self.pending=[];self.proposed=[]
            o.audit('locked_current_study_started',parameters=p,outputs=state['outputs'])
            return dict(started=True)
        if action=='steady_study_status':
            if not self.study:raise DeviceError('No locked current study exists')
            return dict(status=self.study.status,reason=self.study.reason,points=list(self.study.rows))
        if action=='steady_study_stop':
            if self.study and self.study.status=='running':self.study.stop('cancelled');self.pending=[];self.proposed=[]
            return True
        if action=='steady_heartbeat':self.deadline=o.clock()+self.p['watchdog_s'];return True
        if action=='steady_read':
            state,obs=self.read_feedback();o.validate_state(state,o.policies())
            self.native(state,obs,self.p,locked=True);now=o.clock()
            # v5.1-hostcheck: the native interval is REPORTED (native_interval_last_s/median_s vs native_interval_s
            # used for the gains); a source-rate change no longer stops the lock (human decision 2026-10-22).
            # The lock is judged against the target the firmware holds NOW (ramped); self.target is the final one.
            judged=self.current_target
            error=judged-obs['frequency']['frequency_hz']
            if abs(error)>self.p['capture_hz']:
                return dict(state='lost',reason='frequency outside native capture band',actions=[],**self.dither_fields())
            pp=o.policies()['piezo'];dac=state['outputs']['piezo']
            if not pp.low<dac<pp.high:return dict(state='lost',reason='PZT at approved boundary',actions=[],**self.dither_fields())
            elapsed=now-self.last_target
            if self.current_target!=self.target:
                self.ramp_target(state,elapsed)
                judged=self.current_target
            self.last_target=now
            if (not self.commissioning and self.tec_reference_pending
                    and self.current_target==self.target and abs(error)<=min(self.tolerance,self.p['capture_hz'])
                    and control_ready(obs,self.p,self.target)):
                self.tec_reference_dac=dac;self.tec_reference_confirmed=True;self.tec_reference_pending=False
                self.planner.tec_reference_dac=dac
                o.audit('tec_context_reference_confirmed',dac=dac,source='initial lock success')
                self.persist()
            tuning=self.pid_tuning and not self.pid_tuning.closed
            if tuning:
                background_pi=self.background.epoch and self.background.epoch.get('family')=='pi'
                advance=True
                if background_pi:
                    from experiment_core.errors import OperationError
                    from ldcon.owners.ecdl_background_pid import validate_axes
                    epoch=self.background.epoch
                    try:
                        self.background.validate(epoch['token'],self.background_context(obs),
                            restoring=epoch['state']=='returning')
                        advance=validate_axes(self,epoch,state)
                    except OperationError as error:
                        self.background.block(str(error));advance=False
                if advance:self.pid_tuning.advance(state,obs)
            from ldcon.owners.ecdl_background_parameters import active as parameter_epoch,tick as parameter_tick
            was_parameter_epoch=parameter_epoch(self)
            if was_parameter_epoch:parameter_tick(self,state,obs)
            parameter_completed=was_parameter_epoch and not parameter_epoch(self)
            plan=dict(actions=[],blocked=[])

            from ldcon.owners.tec_rate import continuation_due,rate_only
            self.sync_target_response()
            full_supervision=now-self.last_supervision>=self.planner.p['supervision_interval_s']
            if full_supervision or continuation_due(self,now):
                response=None;dither_error=None
                if not full_supervision:
                    pass  # Fresh main feedback already checked; no new auxiliary fit for a TEC slice.
                elif self.dither_off():
                    # No modulation to fit (the bank holds zero codes): reported as 'paused', never a telemetry fault.
                    self.dither_unavailable(DITHER_PAUSED_REASON,status='paused');dither_error=MeasurementUnavailable(DITHER_PAUSED_REASON)
                elif self.p.get('dither',{}).get('verified') is True:
                    try:response=self.passive_dither()
                    except MeasurementUnavailable as dither_rejection:
                        self.dither_unavailable(str(dither_rejection));dither_error=dither_rejection
                else:
                    self.dither_unavailable('Verified passive dither is not configured',status='not_configured')
                try:
                    studying=self.study and self.study.status=='running'
                    if parameter_completed or self.background.reservations or parameter_epoch(self) and self.background.epoch['state']!='active':
                        plan=dict(actions=[],blocked=['runtime adjustment returning or awaiting reconciliation'])
                    elif self.response_control:
                        plan=dict(actions=[],blocked=['explicit response study holds automatic current and TEC'])
                    elif tuning:
                        plan=dict(actions=[],blocked=['native PID tuning holds current and TEC'])
                    elif studying:
                        tolerance=o.device['control_settings']['axes']['temperature']['readback_tolerance']
                        if abs(state['outputs']['temperature']-self.study_temperature)>tolerance:raise DeviceError('TEC changed during locked current study')
                        tick=self.study.tick(now,state['outputs']['current'])
                        if tick.get('measure'):
                            from ldcon.owners.ecdl_locked_study import nearby_modes
                            if dither_error is not None:raise dither_error
                            # Rule K (2026-10-27): an unverified dither configuration is reported (blocked /
                            # dither_unavailable in the cycle result, stationary_dither_unavailable audit); the lock continues.
                            if response is None:raise MeasurementUnavailable('Verified passive dither is not configured; current study point deferred')
                            row=self.study.record(now,state['outputs'],obs['frequency']['frequency_hz'],obs['metrics'],response,
                                                  nearby_modes(obs['metrics'],self.p['nearby_modes']))
                            row.update(current_unit=o.policies()['current'].unit,
                                       dither_response_unit='nm / '+o.policies()['current'].unit,
                                       policy_revision=self.p['revision'],acquired_unix_s=obs['frequency'].get('acquired_unix_s'))
                            o.audit('locked_current_study_point',**row);plan['study_point']=row
                        elif tick.get('action'):plan['actions']=[tick['action']]
                        self.pending=[]
                    elif not self.commissioning:
                        state['tec_ff_paused']=self.tec_ff_paused
                        state['tec_rate_only']=rate_only(o.device)
                        state['tec_control_ready']=(control_ready(obs,self.p,judged,tolerance_hz=self.p['capture_hz'])
                            and (self.p.get('tec_center_reference')!='activation' or self.tec_reference_confirmed))
                        state['optical_maintenance_required']=(not adjustment_ready(obs,self.p,judged)
                            or self.p.get('tec_center_reference')=='activation' and not self.tec_reference_confirmed)
                        if self.pending:plan=dict(actions=list(self.pending),blocked=[])
                        else:
                            state['dither_source_unix_s']=self.dither_report.get('dither_source_unix_s')
                            state['optical_acquired_unix_s']=obs['frequency'].get('acquired_unix_s')
                            plan=self.planner.plan(now,now-self.last_supervision,state,obs['metrics'],response)
                except MeasurementUnavailable as telemetry_error:
                    # Auxiliary telemetry is not the optical feedback source.
                    # Keep native PI and its fresh optical supervision running,
                    # but never dispatch stale current/TEC proposals or fits.
                    self.discard_proposals('dither telemetry unavailable')
                    plan=dict(actions=[],blocked=['dither telemetry unavailable'],
                              dither_unavailable=str(telemetry_error))
                    o.audit('stationary_dither_unavailable',reason=str(telemetry_error),
                            outputs=state['outputs'])
                if parameter_epoch(self):
                    from ldcon.owners.ecdl_background_parameters import permit_proposal
                    kept=[]
                    for proposal in plan['actions']:
                        try:allowed=permit_proposal(self,proposal['axis'],proposal['value'])
                        except (ValueError,KeyError,TypeError):
                            self.background.returning('grant_revoked');allowed=False
                        if allowed:kept.append(proposal)
                        else:plan['blocked'].append('runtime adjustment return envelope/grant')
                    plan['actions']=kept;self.pending=[]
                if self.thermal_study.active:
                    try:
                        for proposal in plan['actions']:
                            self.require_thermal_output(proposal['axis'],proposal['value'],state['outputs'])
                    except MeasurementUnavailable as rejection:
                        plan=dict(actions=[],blocked=[str(rejection)])
                if full_supervision:self.last_supervision=now
                self.supervision_sequence+=1;self.proposed=list(plan['actions'])
                self._thermal_proposals_revoked=False
                o.audit('stationary_supervision',outputs=state['outputs'],metrics=obs['metrics'],**plan)
            result=dict(state='locked',frequency_hz=obs['frequency']['frequency_hz'],error_hz=error,
                        mode_id=obs.get('mode_id'),
                        acquired_s=obs['frequency'].get('acquired_unix_s') or obs['frequency']['acquired_s'],
                        source_acquired_unix_s=obs['frequency'].get('acquired_unix_s'),
                        outputs=state['outputs'],single_mode=obs['single_mode'],maintenance_required=obs['maintenance_required'],metrics=obs['metrics'],**plan)
            result.update(self.dither_fields());result.update(self.tec_context_fields())
            from ldcon.owners.ecdl_target_settling import owner_evidence
            result['tec_centering']=owner_evidence(self,state,plan)
            result['supervision_sequence']=self.supervision_sequence
            result.update(self.native_input_fields());result['native_interval_s']=self.interval  # report-only (v5.1-hostcheck)
            result['entry_gate_passed']=entry_quality(obs,self.p,judged)
            result['adjustment_ready_passed']=adjustment_ready(obs,self.p,judged)
            result['fringe_period_qualified']=(obs.get('fringe_analysis') or {}).get('period_qualified')
            authority=o.policy_authority
            result['owner_context']=dict(system_id=authority.system_id,generation=authority.generation,policy_revision=authority.revision)
            result['quality_baseline_blocked']=bool(self.response_control or (self.study and self.study.status=='running') or self.background.epoch or tuning or self.current_target!=self.target)
            result['control_ready_passed']=control_ready(obs,self.p,judged,tolerance_hz=self.p['capture_hz'])
            result.update(target_frequency_hz=self.target,ramped_target_hz=self.current_target,target_ramp_active=self.current_target!=self.target)
            result['target_feedforward_enabled']=self.p.get('target_feedforward_enabled',False)
            result['target_feedforward']=copy.deepcopy(self.target_ff)
            # target-ff design (2026-10-27): report-only room for the next target step (never a completion gate)
            result['pzt_headroom_hz']=(self.target_ff or {}).get('pzt_headroom_hz') if self.current_target!=self.target else self.pzt_headroom(state)
            if parameter_epoch(self):
                from ldcon.owners.ecdl_background_parameters import values
                result['parameter_adjustment']=dict(family=self.background.epoch['family'],phase=self.background.epoch['phase'],parameters=values(self))
            result['fine_quality_passed']=bool(obs['single_mode'] and not obs['maintenance_required'])
            if tuning:
                raw_i=state['servo_status'].get('pid_integral')
                integral_limited=not isinstance(raw_i,(int,float)) or abs(raw_i)>=.95*self.pid_tuning.raw_bound
                result['pid_tuning']={**self.pid_tuning.status(),
                    'eligible':bool(adjustment_ready(obs,self.p,judged) and not integral_limited),
                    'integral_limited':integral_limited}
                if self.background.epoch and self.background.epoch.get('strategy')=='validated_candidates':
                    raw_bound=max(abs(state['servo_config'][key]) for key in ('pid_integral_min','pid_integral_max'))
                    valid=type(raw_i) in (int,float) and math.isfinite(raw_i) and raw_bound>0 and abs(raw_i)<=raw_bound*(1+1e-9)
                    result['pid_tuning']['integral_observation']=dict(valid=valid,
                        usage=abs(raw_i)/raw_bound if valid else None,
                        saturated=bool(valid and abs(raw_i)>=.98*raw_bound))
            if self.commissioning:
                self.trial.add(result,self.tolerance);result['commission']=self.trial.status()
            return result
        if action=='steady_adjust':
            try:
                return self.adjust(request)
            except MeasurementUnavailable as rejection:
                if o.fault:raise
                # An owner-confirmed pre-write observation rejection is a hold,
                # never a reason to replay the proposal without a fresh plan.
                self.discard_proposals('optical observation rejected before adjustment')
                o.audit('stationary_adjustment_deferred',reason=str(rejection))
                return dict(applied=False,reason='fresh optical observation required',
                            measurement_rejection=str(rejection))
        raise DeviceError('Unknown stationary operation')
    def discard_proposals(self,reason):
        # Only unexecuted continuous TEC/FF proposals are discarded. Search state
        # belongs to its explicitly admitted adjustment card, never this owner.
        self.pending=[];self.proposed=[]

    def read_feedback(self,*,spectral_only=False):
        # Only read operations belong in this block. A read outage can wait;
        # physical writes remain in separately fault-tracked operations.
        try:
            read_started=time.time()
            state=self.o.hw().snapshot()
            state['read_started_unix_s']=read_started
            self.note_servo_config(state.get('servo_config'))
            return state,self.o.observation(spectral_only=spectral_only)
        except OSError as error:
            if self.o.fault:raise
            raise MeasurementUnavailable('Feedback read temporarily unavailable: '+str(error)) from error

    def adjust(self,request):
        o=self.o
        self.sync_target_response()
        if self._thermal_proposals_revoked:
            raise MeasurementUnavailable('Thermal study scope ended; await a fresh owner proposal')
        proposal=request['proposal']
        if not self.proposed or proposal!=self.proposed[0]:raise DeviceError('Only the latest owner-generated action can be accepted')
        axis=proposal['axis']
        if axis not in ('current','temperature'):raise DeviceError('Locked adjustment supports current and TEC only')
        state,obs=self.read_feedback();self.native(state,obs,self.p,locked=True)
        self.sync_target_response()
        if self._thermal_proposals_revoked:
            raise MeasurementUnavailable('Thermal study scope ended during feedback; await a fresh owner proposal')
        from ldcon.owners.ecdl_background_parameters import active as parameter_epoch,permit_proposal
        if parameter_epoch(self):
            from experiment_core.errors import OperationError
            try:
                e=self.background.epoch
                self.background.validate(e['token'],self.background_context(obs),restoring=bool(getattr(self,'_parameter_returning',False)))
                allowed=permit_proposal(self,axis,proposal['value'])
            except (ValueError,KeyError,TypeError,OperationError):
                self.background.returning('runtime_trial_fenced');allowed=False
            if not allowed:
                self.discard_proposals('runtime trial proposal not admitted')
                return dict(applied=False,reason='runtime trial return envelope or grant')
        if axis=='temperature' and (not control_ready(obs,self.p,self.current_target,tolerance_hz=self.p['capture_hz']) or (self.study and self.study.status=='running')):
            raise MeasurementUnavailable('TEC held while branch/frequency is unconfirmed or a study owns the axes')
        p=o.policies()[axis];previous=state['outputs'][axis]
        since_write=o.clock()-o.last_write[axis]
        if since_write+1e-9<p.update_interval_s:
            self.proposed.pop(0);return dict(applied=False,reason='device update interval hold')
        from ldcon.owners.ecdl_tec_pair import slew_elapsed,enabled,preflight,compensate
        elapsed=slew_elapsed(axis,p,self.p,since_write)
        target=proposal['value']
        self.require_thermal_output(axis,target,state['outputs'])
        if axis=='temperature' and proposal.get('reason')=='PZT slow centering':
            from ldcon.owners.tec_rate import rate_only
            if rate_only(o.device):
                from ldcon.owners.ecdl_target_settling import center_dac
                center=center_dac(self.p,o.policies()['piezo'],self.tec_reference_dac)
                needed=self.planner.p['tec_gain']*(state['outputs']['piezo']-center)/self.p['counts_per_temperature_unit']
                if needed*(target-previous)<=0:
                    self.discard_proposals('TEC centering direction changed; replan from fresh feedback')
                    return dict(applied=False,reason='TEC centering direction changed')
                if abs(needed)<abs(target-previous):
                    target=previous+needed
                    proposal=dict(proposal,value=target)
        if p.resolution:target=round(target/p.resolution)*p.resolution
        delta=max(-min(p.max_step,p.max_slew_per_s*elapsed),min(min(p.max_step,p.max_slew_per_s*elapsed),target-previous))
        value=previous+delta
        if p.resolution:
            delta=math.copysign(math.floor((abs(delta)+1e-12)/p.resolution)*p.resolution,delta)
            value=round((previous+delta)/p.resolution)*p.resolution
        if abs(value-previous)<1e-12:
            self.proposed.pop(0);return dict(applied=False,reason='device resolution hold')
        if parameter_epoch(self):
            from ldcon.owners.ecdl_background_parameters import permit_output
            try:allowed=permit_output(self,axis,value)
            except (ValueError,KeyError,TypeError):
                self.background.returning('grant_revoked');allowed=False
            if not allowed:
                self.discard_proposals('quantized output outside runtime envelope')
                return dict(applied=False,reason='runtime trial quantized output not admitted')
        paired=enabled(self,proposal)
        self.require_thermal_output(axis,value,state['outputs'])
        if paired:
            from ldcon.owners.ecdl_tec_pair import ff_target
            projected={**state['outputs'],'temperature':value}
            paired_current,_=ff_target(self.planner,projected)
            self.require_thermal_output('current',paired_current,state['outputs'])
            preflight(self,state,value)
        self.sync_target_response()
        if self._thermal_proposals_revoked:
            raise MeasurementUnavailable('Thermal study scope ended before write; await a fresh owner proposal')
        pair=None
        self.adjusting=True
        try:
            result=o._dispatch(dict(action='locked_move',lease=o.lease,axis=axis,value=value))
            if paired:pair=compensate(self,result)
        finally:self.adjusting=False
        if axis=='current':
            self.planner.ack_current(result['outputs'],proposal.get('reason'))
        self.proposed.pop(0)
        self.pending=[proposal] if abs(value-proposal['value'])>max(p.resolution/2,1e-12) else []
        if pair and pair['status']=='pending':self.pending=[]  # compensate before any further TEC step
        return dict(applied=True,value=result['outputs'][axis],**({'tec_current_pair':pair} if pair else {}))
    def qualify(self,state,obs,target,tolerance,profile):
        """Owner qualification = the CURRENT reading (single mode, no maintenance, |f-target|<=tolerance) in an
        unchanged context. v5.1-hostcheck (human decision 2026-10-22): no 30 s span / 31-stamp history and no
        >1 s stamp-gap reset; the observation itself is already current-cycle (ECDLOwner.observation)."""
        context=dict(outputs=state['outputs'],current=state['current'],temperature=state['temperature'],target=target)
        now=self.o.clock();sample=obs['frequency'];stamp=sample.get('acquired_unix_s') or sample['acquired_s']
        self.qualification_context=context
        if not obs['single_mode'] or obs['maintenance_required']:
            self.qualification=[]
            return dict(ready=False,reason='Initial point is not a clean single mode',valid_samples=0)
        if abs(sample['frequency_hz']-target)>tolerance:
            self.qualification=[]
            return dict(ready=False,reason='Initial frequency outside target tolerance',valid_samples=0)
        raw=state['servo_status'].get('last_system_interval_us')
        self.qualification=[dict(stamp=stamp,received_s=now,
            interval_s=raw*1e-6 if type(raw) in (int,float) and math.isfinite(raw) else None)]
        return dict(ready=True,valid_span_s=0.,valid_samples=1,frequency_hz=sample['frequency_hz'],stamp=stamp)

    def commission_action(self,request):
        o=self.o;action=request['action'];t=self.trial
        if not t or not self.commissioning:raise DeviceError('Explicit native commissioning session required')
        if action=='commission_raise_bound':
            if not self.active or t.fixed_stopped:raise DeviceError('Integral bound is raised only in the first closed-loop phase')
            limit=t.raise_bound();ki=self.o.hw().connections['piezo'].get('config')['pid_ki']
            self.write(dict(pid_integral_min=-limit/ki,pid_integral_max=limit/ki),'commissioning integral stage %d (%.0f counts)'%(t.bound_index,limit))
            o.audit('commission_integral_stage',stage=t.bound_index,limit_count=limit,schedule=t.schedule)
            return dict(**t.status(),integral_limit_count=limit,stage_index=t.bound_index,schedule=t.schedule,at_approved=t.at_approved_bound())
        if action=='commission_next':
            if not self.active or t.stage not in (0,1,2):raise DeviceError('Unexpected commissioning target stage')
            if t.stage==0 and not t.fixed_stopped and not t.at_approved_bound():
                raise DeviceError('Raise the integral bound to the approved value before the frequency steps')
            t.advance();t.stage+=1
            self.target=t.target+(self.p['commissioning']['step_hz'] if t.stage==1 else -self.p['commissioning']['step_hz'] if t.stage==2 else 0.)
            o.audit('commission_target',stage=t.stage,target_hz=self.target)
            return t.status()
        if action=='commission_fixed_stop':
            if not self.active or t.stage!=3:raise DeviceError('Fixed-reference stop is out of sequence')
            t.advance();before=o.hw().snapshot()
            self.write(dict(pid_multiplier=0),'commission fixed-reference disable only')
            after=o.hw().snapshot()
            if not after['open_loop'] or abs(after['outputs']['piezo']-t.origin['piezo'])>1.:
                o.fault='Fixed-reference disable readback mismatch';raise DeviceError(o.fault)
            self.active=False;self.armed=False;self.stop_event.set();t.fixed_stopped=True
            o.audit('commission_fixed_disable',before=before['outputs'],after=after['outputs'])
            self.persist()
            return dict(held=True,reference_dac=t.origin['piezo'],before=before['outputs'],after=after['outputs'])
        if action=='commission_actual_hold':
            if not self.active or not t.fixed_stopped or t.stage!=4:raise DeviceError('Actual-DAC handover is out of sequence')
            t.advance();state=o.hw().snapshot()
            correction=state['outputs']['piezo']-state['servo_config']['control_offset']
            if not 20.<=abs(correction)<=self.p['commissioning']['max_correction_count']:
                raise DeviceError('Actual-DAC handover requires a nonzero, bounded measured PI correction')
            t.dither_response=self.passive_dither()
            result=self.hold('commission actual-DAC handover')
            t.actual_hold_correction=correction;t.held_dac=result['outputs']['piezo'];t.current_held=True;t.stage=5
            return dict(**result,tested_correction_count=correction)
        if action=='commission_hold_sample':
            if not t.current_held or t.stage!=5:raise DeviceError('No commissioned actual-DAC hold to observe')
            state=o.hw().snapshot();o.validate_state(state,o.policies());obs=o.observation()
            if not state['open_loop']:raise DeviceError('PI was unexpectedly re-enabled after hold')
            row=dict(outputs=state['outputs'],frequency_hz=obs['frequency']['frequency_hz'],single_mode=obs['single_mode'],
                     acquired_s=obs['frequency'].get('acquired_unix_s') or obs['frequency']['acquired_s'])
            t.add(row,self.tolerance);row['commission']=t.status();return row
        if action=='commission_complete':
            if not t.current_held or t.stage!=5:raise DeviceError('PI commissioning has not completed its hold')
            t.advance();t.completed=True
            proof=dict(status='verified',firmware=self.p['firmware_version'],science_target_hz=t.target,
                hold_target_hz=t.hold_target,actual_hold_correction_count=t.actual_hold_correction,
                configured_integral_limit_count=self.p.get('approved_integral_limit_count',self.p['integral_limit_count']),
                integral_schedule=t.schedule,integral_stages=t.bound_events,
                configured_total_correction_limit_count=self.p['commissioning']['max_correction_count'],
                max_observed_correction_count=t.max_observed_correction,stages=t.events,
                quality=dict(t.quality),  # rule K: single-mode labels during commissioning are reported, not a veto
                dither_response_nm_per_current_unit=t.dither_response,
                native_interval_s=self.interval,dither_programming=self.programmed_dither,
                handover_atomic=False,measurement_bandwidth='HTTP samples plus native timestamped telemetry; sub-sample transients are not resolved',
                native_quality_limitation='Firmware accepts index/seq/numeric band; owner checks UDP source, route, flags and quality at each observation',
                profile=self.p)
            path=o.audit_path.parent/('pi-commission-'+o.lease+'.json')
            from ldcon.core.records import atomic_json
            atomic_json(path,proof);o.audit('pi_commission_verified',evidence=str(path),proof=proof)
            self.commissioning=False
            return dict(evidence=str(path),**proof)
        raise DeviceError('Unsupported commissioning operation')

    def dither_unavailable(self,reason,*,status='unavailable'):
        self.dither_report=dict(dither_response=None,dither_response_valid=False,
            dither_analysis_unix_s=time.time(),dither_source_unix_s=None,
            dither_analysis_status=status,dither_analysis_reason=reason)

    def dither_fields(self):
        result=dict(self.dither_report)
        source=result.get('dither_source_unix_s')
        result['dither_source_age_s']=max(0.,time.time()-source) if source is not None else None
        profile=getattr(self,'p',None) or self.o.device.get('control_settings',{}).get('steady_lock',{})
        window=profile.get('window',{})
        maximum_age=max(2*profile.get('supervision_interval_s',10.),
                        profile.get('dither',{}).get('window_s',13.)+profile.get('dither',{}).get('delivery_delay_s',3.)+2.)
        paused=self.dither_off(profile)
        if not self.active:
            result.update(dither_response=None,dither_response_valid=False,dither_analysis_status='not_locked')
        elif paused:
            result.update(dither_response=None,dither_response_valid=False,dither_analysis_status='paused',
                          dither_analysis_reason=DITHER_PAUSED_REASON)
        elif source is not None and result['dither_source_age_s']>maximum_age:
            result.update(dither_response=None,dither_response_valid=False,dither_analysis_status='stale')
        result.update(dither_paused=paused,dither_warning_threshold=window.get('warning_response'),
                      dither_edge_threshold=window.get('edge_response'),
                      dither_response_unit='nm / '+self.o.policies()['current'].unit)
        return result

    def passive_dither(self):
        if self.dither_off():
            self.dither_unavailable(DITHER_PAUSED_REASON,status='paused')
            raise MeasurementUnavailable(DITHER_PAUSED_REASON)
        try:return self.read_passive_dither()
        except MeasurementUnavailable as error:
            self.dither_unavailable(str(error));raise
        except DeviceError as error:
            self.dither_unavailable(str(error),status='configuration_error');raise

    def read_passive_dither(self):
        """Read existing native telemetry only, filtered to one configured servo."""
        if self.dither_blackout_until is not None and time.time()<self.dither_blackout_until:
            raise MeasurementUnavailable('Dither analysis window overlaps the locked sweep')
        c=self.p.get('dither',{})
        for name in ('frequency_hz','window_s','effective_amplitude_peak','minimum_samples'):
            value=c.get(name)
            if type(value) not in (int,float) or not math.isfinite(value) or value<=0:
                raise DeviceError('Invalid passive dither configuration: '+name)
        end=time.time()-c.get('delivery_delay_s',3.)
        fit_window=max(c['window_s'],3/c['frequency_hz']+1)
        from ldcon.drivers.native_dither_telemetry import read_native_dither
        trace=read_native_dither(c,end,fit_window);times=trace['times_s']
        response=dither_response(times,trace['dac_counts'],c,self.p['wavelength_nm_per_count'])
        self.dither_report=dict(dither_response=response,dither_response_valid=True,
            dither_analysis_unix_s=time.time(),dither_source_unix_s=times[-1],
            dither_analysis_status='valid',dither_source_start_unix_s=times[0],
            dither_fit_window_s=fit_window,dither_sample_count=len(times),
            dither_provenance=trace['provenance'])
        return response
