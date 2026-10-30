"""Device-free cards: read wavelength, or acquire a frequency using temperature."""
from dataclasses import dataclass
import math
from typing import Protocol
from ldcon.core.contracts import FrequencySample,DeviceError,MeasurementUnavailable
from ldcon.core.device_conditions import TemperatureConditions,ConditionError,finite


@dataclass(frozen=True)
class TemperatureReading:
    setpoint_c:float
    temperature_c:float
    observed_s:float
    tec_enabled:bool
    ld_enabled:bool
    tec_current_a:float=0.
    current_a:float=0.

    def state(self):
        return {'setpoint_c':self.setpoint_c,'temperature_c':self.temperature_c,
                'tec_enabled':self.tec_enabled,'ld_enabled':self.ld_enabled,'tec_current_a':self.tec_current_a,'current_a':self.current_a}


class FrequencyPort(Protocol):
    def read_frequency(self)->FrequencySample:...


class TemperaturePort(Protocol):
    conditions:TemperatureConditions
    frequency_hz_per_c:float|None
    def read_temperature(self)->TemperatureReading:...
    def ramp_temperature(self,value_c:float,duration_s:float)->TemperatureReading:...


class CardCancelled(DeviceError):
    pass


def sample_trace(sample,now,max_age):
    """Rule K (2026-10-27): validity is a finite Hz reading of this cycle; the wavemeter quality label is a reported column."""
    frequency=sample.frequency_hz
    valid=(sample.unit=='Hz' and type(frequency) in (int,float) and math.isfinite(frequency) and frequency>0)
    age=now-sample.acquired_s
    control_valid=valid and sample.acquisition_known and math.isfinite(age) and 0<=age<=max_age
    return {'frequency_hz':frequency if valid else None,'wavelength_nm':299792458e9/frequency if valid else None,
            'sample_quality':sample.quality,'quality_flagged':sample.quality!='valid','sample_valid':valid,'control_valid':control_valid,'acquisition_known':sample.acquisition_known,
            'sample_acquired_s':sample.acquired_s,'sample_received_s':sample.received_s,
            'sample_age_s':age if math.isfinite(age) else None,'sequence':sample.sequence}


def _check_cancel(cancel):
    if cancel():raise CardCancelled('Card stopped; no further output writes')


def monitor_wavelength(wavelength:FrequencyPort,p,*,clock,sleep,cancel,emit):
    started=clock();samples=0;valid=0;flagged=0
    while clock()-started<p['duration_s']:
        _check_cancel(cancel)
        try:
            sample=wavelength.read_frequency();row=sample_trace(sample,clock(),p['max_sample_age_s'])
            samples+=1;valid+=int(row['sample_valid']);flagged+=int(row['quality_flagged']);emit('trace',row)
        except DeviceError as e:emit('measurement_error',{'error':str(e)})
        _check_cancel(cancel)
        sleep(min(p['sample_interval_s'],max(0,p['duration_s']-(clock()-started))))
    return {'status':'succeeded' if valid else 'failed','message':None if valid else 'No valid wavelength samples',
            'samples':samples,'valid_samples':valid,'quality_flagged_samples':flagged,'output_writes':0}


def _require_frequency(row,emit,stage):
    """A finite fresh reading is required (data validity); a quality label is reported (rule K), never a refusal."""
    if row['quality_flagged']:emit('measurement_quality',{'stage':stage,**row})
    if not row['control_valid']:
        emit('measurement_rejected',{'stage':stage,**row})
        raise DeviceError(f"Frequency measurement rejected during {stage}: finite={row['sample_valid']}, "
                          f"age_s={row['sample_age_s']}, acquisition_known={row['acquisition_known']}")
    return int(row['quality_flagged'])


class ControlMeasurementGate:
    """Hold all output during bounded read-only recovery of fresh sensor data."""
    def __init__(self,wavelength,temperature,p,*,clock,sleep,cancel,emit,deadline):
        self.wavelength=wavelength;self.temperature=temperature;self.p=p
        self.clock=clock;self.sleep=sleep;self.cancel=cancel;self.emit=emit;self.deadline=deadline;self.recovered=False

    def read(self,stage):
        self.recovered=False
        waiting_since=None;consecutive=0;last_acquisition=None
        while True:
            _check_cancel(self.cancel)
            before=self.clock()
            limit=self.deadline if waiting_since is None else min(self.deadline,waiting_since+self.p['measurement_recovery_timeout_s'])
            if before>=limit:raise DeviceError('Measurement recovery timed out; last accepted output is held')
            try:
                sample=self.wavelength.read_frequency()
            except (DeviceError,OSError) as error:
                # Only read-only acquisition is retried. Commands are never retried.
                row=None;reason=str(error)
            else:
                row=sample_trace(sample,self.clock(),self.p['max_sample_age_s'])
                reason=None if row['control_valid'] else 'Frequency sample is not valid for control'
            if reason is None:
                reader=getattr(self.temperature,'read_control_state',None) or self.temperature.read_temperature
                try:
                    reading=reader()
                except (MeasurementUnavailable,OSError) as error:
                    reason=str(error)
                else:
                    now=self.clock()
                    row=sample_trace(sample,now,self.p['max_sample_age_s'])
                    reason=None if row['control_valid'] else 'Frequency sample expired during actuator readback'
                    if not 0<=now-reading.observed_s<=self.p['max_sample_age_s']:
                        reason='Actuator readback is stale'
                    else:
                        # Fresh actuator faults remain fatal even if the optical
                        # sample expired during this read-only acquisition.
                        if hasattr(self.temperature,'validate_readback'):self.temperature.validate_readback(reading)
                        else:self.temperature.conditions.check(reading.state(),reading.setpoint_c,self.p['sample_interval_s'])
            if reason is not None:
                consecutive=0;last_acquisition=None
                if waiting_since is None:
                    waiting_since=before
                    self.emit('state',{'state':'waiting_measurement','stage':stage,'reason':reason})
                self.emit('measurement_rejected',{'stage':stage,'reason':reason,**(row or {})})
            elif waiting_since is None:
                return sample,reading
            else:
                acquisition=sample.acquired_unix_s if sample.acquired_unix_s is not None else sample.acquired_s
                if last_acquisition is None or acquisition>last_acquisition:consecutive+=1
                else:consecutive=0
                last_acquisition=acquisition
                self.emit('measurement_recovery_sample',{'stage':stage,'consecutive':consecutive,**row})
                if self.clock()>=min(self.deadline,waiting_since+self.p['measurement_recovery_timeout_s']):
                    raise DeviceError('Measurement recovery timed out; last accepted output is held')
                if consecutive>=self.p['measurement_recovery_samples']:
                    self.recovered=True
                    self.emit('state',{'state':'identifying' if 'identification' in stage else 'acquiring',
                                       'stage':stage,'recovered':True,'waited_s':self.clock()-waiting_since})
                    return sample,reading
            _check_cancel(self.cancel)
            limit=min(self.deadline,waiting_since+self.p['measurement_recovery_timeout_s'])
            if self.clock()>=limit:raise DeviceError('Measurement recovery timed out; last accepted output is held')
            self.sleep(min(self.p['sample_interval_s'],limit-self.clock()))


def _check_initial_detuning(target,sample,p,emit):
    if abs(target-sample.frequency_hz)>p['max_initial_detuning_hz']:
        emit('target_rejected',{'target_frequency_hz':target,'measured_frequency_hz':sample.frequency_hz,
                               'max_initial_detuning_hz':p['max_initial_detuning_hz']})
        raise ConditionError('Target is too far from the measured frequency for temperature tuning; check THz / nm units')


def acquire_temperature(wavelength:FrequencyPort,temperature:TemperaturePort,p,*,clock,sleep,cancel,emit):
    if p.get('frequency_hz_per_c') is not None:
        raise ValueError('Move frequency_hz_per_c from card parameters to device.control_settings.temperature before running')
    configured=getattr(temperature,'frequency_hz_per_c',None)
    if configured is not None:
        configured=finite(configured,'temperature device frequency_hz_per_c')
        if configured==0:raise ValueError('Temperature device tuning coefficient cannot be zero')
    elif not p['identify_slope']:
        raise ValueError('Temperature device coefficient is unset and identification is disabled')
    target=p['target_frequency_hz'] or 299792458e9/p['target_wavelength_nm']
    target=finite(target,'target frequency')
    started=clock();identification=None;flagged=0  # quality-flagged readings used for control (report-only, rule K)
    gate=ControlMeasurementGate(wavelength,temperature,p,clock=clock,sleep=sleep,cancel=cancel,emit=emit,deadline=started+p['timeout_s'])
    slope=configured
    if slope is None:
        # Reaching an already satisfied target does not require a probing move.
        satisfied=0;last=None
        while satisfied<p['stable_samples']:
            _check_cancel(cancel)
            sample,reading=gate.read('before identification')
            row=sample_trace(sample,clock(),p['max_sample_age_s'])
            flagged+=_require_frequency(row,emit,'before identification')
            _check_initial_detuning(target,sample,p,emit)
            temperature.conditions.check(reading.state(),reading.setpoint_c,p['sample_interval_s'])
            at=sample.acquired_unix_s if sample.acquired_unix_s is not None else sample.acquired_s
            if abs(target-sample.frequency_hz)>p['tolerance_hz'] or abs(reading.temperature_c-reading.setpoint_c)>p['settle_tolerance_c']:break
            if last is None or at>last:satisfied+=1;last=at
            emit('trace',{**row,'temperature_c':reading.temperature_c,'temperature_setpoint_c':reading.setpoint_c})
            if satisfied>=p['stable_samples']:return {'status':'succeeded','target_frequency_hz':target,'final_error_hz':target-sample.frequency_hz,'temperature_c':reading.temperature_c,'output_writes':0,'stable_samples':satisfied,'temperature_identification':None,'quality_flagged_samples':flagged}
            if clock()-started>=p['timeout_s']:raise DeviceError('Timed out validating the initial target')
            sleep(p['sample_interval_s'])
        from ldcon.jobs.temperature_identification import identify_temperature_slope
        identification=identify_temperature_slope(wavelength,temperature,p,clock=clock,sleep=sleep,cancel=cancel,emit=emit,deadline=started+p['timeout_s'])
        slope=identification['frequency_hz_per_c']
    slope=finite(slope,'temperature tuning slope')
    if slope==0:raise ValueError('Temperature slope cannot be zero')
    emit('temperature_calibration',dict(frequency_hz_per_c=slope,
        source='invocation_identification' if identification is not None else 'device_settings'))
    initial=temperature.read_temperature();origin=initial.setpoint_c
    stable=0;writes=0;last_acquisition=None
    emit('state',{'state':'acquiring','target_frequency_hz':target})
    while clock()-started<p['timeout_s']:
        _check_cancel(cancel)
        sample,observed=gate.read('target acquisition');now=clock()
        if gate.recovered:stable=0
        row=sample_trace(sample,now,p['max_sample_age_s'])
        flagged+=_require_frequency(row,emit,'target acquisition')
        if writes==0:_check_initial_detuning(target,sample,p,emit)
        for value in (observed.setpoint_c,observed.temperature_c,observed.observed_s,observed.tec_current_a):finite(value)
        if not 0<=now-observed.observed_s<=p['max_sample_age_s']:raise DeviceError('Temperature readback is stale')
        if not observed.tec_enabled or not observed.ld_enabled:raise DeviceError('LD and TEC must already be enabled; this card never switches outputs')
        if now-started>=p['timeout_s']:break
        acquisition=sample.acquired_unix_s if sample.acquired_unix_s is not None else sample.acquired_s
        fresh=last_acquisition is None or acquisition>last_acquisition
        error=target-sample.frequency_hz
        row.update(temperature_c=observed.temperature_c,temperature_setpoint_c=observed.setpoint_c,
                   target_frequency_hz=target,error_hz=error,fresh_sample=fresh)
        emit('trace',row)
        if not fresh:
            sleep(p['sample_interval_s']);continue
        last_acquisition=acquisition
        if abs(error)<=p['tolerance_hz']:
            if abs(observed.temperature_c-observed.setpoint_c)<=p['settle_tolerance_c']:stable+=1
            else:stable=0
            if stable>=p['stable_samples']:
                emit('state',{'state':'reached','error_hz':error})
                return {'status':'succeeded','target_frequency_hz':target,'final_error_hz':error,
                        'temperature_c':observed.temperature_c,'output_writes':writes+(identification['output_writes'] if identification else 0),
                        'stable_samples':stable,'temperature_identification':identification,'quality_flagged_samples':flagged}
            sleep(p['sample_interval_s']);continue
        stable=0
        # Anchor to measured temperature, not repeated accumulation on a lagging setpoint.
        desired=observed.temperature_c+p['gain']*error/slope
        guard=temperature.conditions
        excursion=p['max_excursion_c'] if p['max_excursion_c'] is not None else guard.high-guard.low
        low=max(guard.low,origin-excursion);high=min(guard.high,origin+excursion)
        if writes==0:
            estimated=observed.temperature_c+error/slope
            emit('target_check',{'estimated_temperature_c':estimated,'low_c':low,'high_c':high})
            margin=p['tolerance_hz']/abs(slope)
            if estimated<low-margin or estimated>high+margin:
                raise ConditionError(f'Target requires estimated {estimated:.6g} C outside permitted [{low}, {high}] C; check THz / nm units')
        desired=max(low,min(high,desired));delta=desired-observed.setpoint_c
        requested_rate=min(p['requested_slew_c_per_s'],abs(error/slope)/p['approach_time_s'])
        dt=p['sample_interval_s'];step=math.copysign(min(abs(delta),requested_rate*dt,guard.step),delta)
        if abs(step)<1e-12:
            if observed.setpoint_c<=low+1e-12 or observed.setpoint_c>=high-1e-12:raise ConditionError('Target cannot be reached inside the permitted temperature excursion')
            sleep(dt);continue
        # A state-dependent device limit can be lower than the requested variable rate.
        last_error=None
        for _ in range(40):
            candidate=observed.setpoint_c+step
            try:guard.check(observed.state(),candidate,dt);break
            except ConditionError as e:
                last_error=e;step*=.5
                if abs(step)<1e-12:raise last_error
        else:raise last_error
        _check_cancel(cancel)
        if clock()-started>=p['timeout_s']:break
        emit('command_requested',{'interface':'temperature','temperature_c':candidate,
                                 'duration_s':dt,'requested_slew_c_per_s':requested_rate,
                                 'accepted_slew_c_per_s':abs(step)/dt})
        # Persistent intent precedes every write. This port exposes no current/enable commands.
        _check_cancel(cancel)
        accepted=temperature.ramp_temperature(candidate,dt);writes+=1
        emit('command_applied',{'interface':'temperature','temperature_c':accepted.setpoint_c})
        _check_cancel(cancel);sleep(dt)
    raise DeviceError('Temperature acquisition timed out; last accepted output is held')


from ldcon.core.card_definition import CardDefinition
INTERFACE_CATALOG={c.id:c for c in [
    CardDefinition('notify.teams','submit',(),
        'Queue a Teams webhook notification directly or as another card’s fallback.',
        'Secret environment reference only; durable deduplication and unknown-delivery handling, independent of device control.',
        ('teams_card.py','card_fallback.py'),outputs=()),
    CardDefinition('acquire.metrics','acquire_metrics',('metric_reader',),
        'Retrieve physical quantities through interchangeable read-only metric sources.',
        'Quantity semantics and units are separate from source bindings and queries. Preserve quality, acquisition time and provenance.',
        ('metric_cards.py','metric_interface.py','metric_quantities.py'),outputs=()),
    CardDefinition('agent.resident','run_resident',(),
        'Create or reconnect a persistent Claude agent by default, with per-laser artifact editing and diagnostic requests; Codex remains selectable.',
        'Device-free resident mailbox; persisted sessions, explicit stop, no replay of uncertain requests.',
        ('resident_agent.py','agent_adapters.py'),continuous=True,outputs=()),
    CardDefinition('agent.request','submit',(),
        'Submit a diagnostic request to a resident agent card in the same deck.',
        'Durable idempotent submission; queue acceptance is distinct from completed diagnosis.',
        ('resident_agent.py',),outputs=()),
    CardDefinition('agent.task','run',(),
        'Execute a bounded task in the resident conversation or an explicitly selected task profile.',
        'Resident adjustment returns each measurement to the same conversation; scoped owner execution, no recursive delegation, verified outcomes.',
        ('agent_task.py','agent_bundle.py','agent_profiles.py'),outputs=()),
    CardDefinition('agent.monitor','run',(),
        'Monitor recorded control health on a schedule or sustained conditions and request a bounded task.',
        'Independent observer; no model during waiting, optional approved idle adjustment, foreground restoration priority.',
        ('agent_monitor.py','agent_background.py'),continuous=True,outputs=()),
    CardDefinition('agent.history_review','run',(),
        'Analyze accumulated normal and failure data for correlations and better algorithms.',
        'Read-only historical research; proposed knowledge is separate from validated findings and physical authority.',
        ('agent_task.py','agent_history.py'),outputs=()),
    CardDefinition('sweep.servo_modulation','run_sweep',('servo_modulation',),
        'Triangle or staircase frequency shifts through the servo external modulation input.',
        'Calibrated software ARB; continuous playback or buffered external trigger. Exclusive controller/AWG leases and verified restoration.',
        ('modulation_sweep.py','drivers/frequency_sweep.py'),outputs=('servo_modulation',)),
    CardDefinition('sweep.current_modulation','run_sweep',('current_modulation',),
        'Triangle or staircase frequency shifts through the current controller external modulation input.',
        'Validate the total DC plus modulation current before programming. Continuous or labscript-triggered ARB, with verified restoration.',
        ('modulation_sweep.py','drivers/frequency_sweep.py'),outputs=('current_modulation',)),
    CardDefinition('tune.sweep_tracking','run_card',(),
        '掃引の追従の評価と機械的な補正（単発・機器に触れない、規範 F）。掃引の代理ジョブ（proxy_job か request_id）の判定とセッションの追従統計から outcome.tracking を作り直し、FF 利得の推奨（ショットごとの option、レーザー定義は変えない）を保存します。機械的な規則がなければ常駐エージェントに解析を依頼します。代理ジョブは判定の公開時に同じ手順を自分で行います。',
        'Rule F one-shot (sweep_tracking.py), also run by the sweep proxy at its outcome. No device I/O: assessment ok|degraded|aborted with advice, the FF-gain rule (DAC slope vs feed-forward slope) as a stored per-shot recommendation, otherwise one non-blocking notice to the resident agent.',
        ('sweep_tracking.py',),outputs=()),
    CardDefinition('analyze.shot_sweep','run_card',(),
        'ショット後解析（単発・読取専用、規範 F）。掃引の代理ジョブ（proxy_job か request_id）について、InfluxDB（なければ VictoriaMetrics）から ESP32 の記録を読み、記録の形を mod_raw/adc_raw に当てはめて段の開始時刻を求め、波長計の各点に level index・遷移中か・遷移からの時間を付けます。代理ジョブは結果の公開時に同じ手順を自分の動作として起動します。',
        'Rule F one-shot procedure (shot_analysis.py), also started by the sweep proxy at its outcome. No device I/O: InfluxDB first, VictoriaMetrics fallback after its search lag; contract ldcon.shot-analysis/v1 published at <proxy job>/shot-analysis/ and GET jobs/<proxy>/analysis.',
        ('shot_analysis.py',),outputs=()),
    CardDefinition('report.grafana_dashboard','export_dashboard',(),
        'Export a Grafana dashboard JSON while a lock continues running.',
        'No device I/O. Freeze the reference from the selected running or assigned target; use editable Grafana variables and four-column time series.',
        ('grafana_dashboard.py',),outputs=()),
    CardDefinition('commission.ecdl_pi','run_pi_commission',('ecdl_stationary_lock',),
        'Bounded native PI start, signed target-step, fixed-reference stop and actual-DAC hold verification.',
        'Owner qualifies the actual initial point, enforces a small integral correction limit, and records measured handover evidence before stationary operation.',
        ('ecdl_pi_commission.py','ecdl_steady_owner.py'),outputs=('piezo',)),
    CardDefinition('servo.ecdl_steady_lock','run_steady_lock',('ecdl_stationary_lock',),
        'Stationary ECDL native PZT PI with a slow TEC outer loop and optional calibrated current feedforward.',
        'Hold the chosen current bias by default; measure the same-mode window through the separate window-center card. Preserve finite measurement holds, owner watchdog and qualified recovery. Device-side native handover commissioning is mandatory.',
        ('ecdl_steady.py','ecdl_steady_policy.py','ecdl_steady_owner.py'),continuous=True,
        outputs=('piezo','current','temperature')),
    CardDefinition('study.ecdl_current_response','run_response_study',('ecdl_stationary_lock','bounded_axis','optical_mode_monitor','open_loop_state'),
        'Study a same-mode current path with multiple selected observables and explicit recovery recipes.',
        'Keep TEC fixed during bidirectional mapping; record passive response, arbitrary scalar fringe indicators and mode loss. Hold actual DAC on a hop, replay a directed recovery path and verify the original optical branch before rearming.',
        ('ecdl_response_study.py','ecdl_relock.py'),outputs=('piezo','current','temperature')),
    CardDefinition('tune.ecdl_window_center','WindowCenterPolicy',('ecdl_stationary_lock',),
        'Measure a current window using selected optical observables and optional fresh per-point dither.',
        'Use one traversal policy with explicit timing, step and observation limits. Keep TEC fixed, distinguish optical-quality edges from verified branch edges, and never combine different branches. No multimode observation is required. Missing boundaries leave the center unresolved; a hop delegates to verified original-branch recovery.',
        ('window_center.py',),outputs=('current',)),
    CardDefinition('study.ecdl_locked_current_map','LockedCurrentStudy',('ecdl_stationary_lock',),
        'Measure passive dither response and nearby-mode strength versus current during stationary locking.',
        'Attach to the existing lock worker and owner lease. Hold TEC, sweep both current directions, retain raw fringe indicators and return to the initial bias while the lock remains valid.',
        ('ecdl_locked_study.py',),outputs=('current',)),
    CardDefinition('study.ecdl_thermal_context','run_context_study',
        ('optical_mode_monitor','bounded_axis','open_loop_state'),
        'Diagnostic slow TEC walk with bounded unidirectional current context maps.',
        'Native feedback remains held; freeze TEC/PZT during each map. Relative low-side-peak references do not certify single mode. No PI activation; require independent fresh target qualification.',
        ('ecdl_thermal_context.py','fringe_sweep_reference.py'),outputs=('temperature','current')),
    CardDefinition('tune.ecdl_initial_point','acquire_ecdl',
        ('optical_mode_monitor','bounded_axis','open_loop_state'),
        'ECDL initial operating point: slow TEC approach, current maintenance and learned round-trip recovery paths.',
        'Keep the piezo approximately centered in open loop. Pause TEC during mode loss, map current history, retain a verified single-mode bias, then resume. Device adapters own units, calibration and limits.',
        ('ecdl_prelock.py',),outputs=('piezo','temperature','current')),
    CardDefinition('tune.pid_factor_reduction','OnlinePIDTuner',(),
        'Compare a finite list of proportional P/I gain reductions inside the running lock.',
        'The algorithm is fixed: scale all gain terms by each configured factor, compare valid baseline/trial windows and retain only a measured improvement. Unsupported owner transitions are rejected.',('online_pid.py','pi_algorithms.py')),
    CardDefinition('tune.pid_coordinate_search','AdaptiveComparison',(),
        'Compare independent P/I coordinate candidates inside the running lock.',
        'The algorithm is fixed: bidirectional coordinate search with temporal uncertainty checks and owner-confirmed transitions. Requires a controller adapter with independent-gain candidate proof; ECDL uses the general PI adjustment task.',('adaptive_pid.py','pi_algorithms.py')),
    CardDefinition('servo.native_current','run_native_current',('native_current_lock',),
        'DFB/DBR wavelength lock through an independent ESP native current PI.',
        'Measured external-current calibration, exclusive CLD bias interlock, bounded DAC acquisition, verified native enable/stop and finite input watchdog; preserves the external modulation source.',
        ('native_current_card.py','esp32_native_owner.py','esp32_native_client.py'),continuous=True,outputs=('current_a',)),
    CardDefinition('tune.native_current','run_native_acquire',('native_current_lock',),
        'Finite DFB/DBR operating-point acquisition through a measured external-current response.',
        'Keep native PI disabled, hold CLD bias/TEC and the waveform generator, settle each bounded DAC step, then require fresh averaged target stability. Persist response provenance and the final fixed reference.',
        ('native_current_acquire.py','esp32_native_owner.py'),outputs=('current_a',)),
    CardDefinition('servo.current_pid','servo_current',('frequency_monitor','current_controller'),
        'Continuous current PID servo with shared device settings.',
        'Owner captures activation current; device absolute and relative limits, output interval, anti-windup and bounded sensor recovery apply.',('current_cards.py','current_settings.py'),outputs=('current_a',)),
    CardDefinition('identify.current_response','identify_current',('frequency_monitor','current_controller'),
        'One-shot bounded current response measurement and conservative PI estimation.',
        'Return check and SNR required before persisting shared device PID and output interval. No output enables or AWG writes.',('current_cards.py','current_settings.py'),outputs=('current_a',)),
    CardDefinition('monitor.wavelength','monitor_wavelength',('frequency_monitor',),
        'Read timestamped optical frequency and wavelength through a sensor interface.',
        'Read-only; preserve acquisition age, channel and cycle. No actuator is opened.',('interface_cards.py',)),
    CardDefinition('tune.temperature_once','acquire_temperature',('frequency_monitor','temperature_controller'),
        'Acquire a per-invocation frequency target using temperature only.',
        'Read the signed slope from device settings, or identify it through a bounded round trip for this invocation. Device absolute candidate and slew conditions override requested speed.',
        ('interface_cards.py','temperature_settings.py','device_conditions.py'),outputs=('temperature_c',))]}

# Old experiments remain importable for stored results and shared recovery
# algorithms. New starts use the explicit discovery-to-lock workflow below.
LEGACY_INTERFACE_CATALOG={name:INTERFACE_CATALOG.pop(name) for name in (
    'tune.ecdl_initial_point','study.ecdl_thermal_context',
    'study.ecdl_current_response','study.ecdl_locked_current_map')}
for definition in (
    CardDefinition('study.ecdl_mode_map','run_mode_map',
        ('optical_mode_monitor','bounded_axis','open_loop_state'),
        '目標波長の周辺を一方向の電流掃引でマップ化し、近さ・観測窓幅・パターン品質から枝の候補を選びます。',
        'PI off, TEC/PZT held. Preserve raw fringes, full discovery path and calibration; phase-free qualified candidates only. Hop cause remains unknown.',
        ('ecdl_mode_map.py','fringe_peaks.py'),outputs=('current',)),
    CardDefinition('prepare.ecdl_recovery','run_recovery_card',
        ('optical_mode_monitor','bounded_axis','open_loop_state'),
        'Prepare a branch-independent relative-current recovery route and optionally qualify it with explicit perturbations.',
        'Infer the return frequency from recent stable timestamped observations in the current operation epoch. '
        'Use one algorithm with an explicit fallback route and optional frequency/TEC/PZT-context parameter specializations; never require a live branch map. '
        'Agent mode delegates evidence analysis to the pinned task model and validates its proposal within the admitted route corridor. '
        'Empty perturbations produce an unverified proposal only. Explicit current perturbations and the same executor provide observed-hop/return evidence. '
        'Frequency return, optical quality and PI capture are separate. The task model authors its explanation; no proposal grants wider output authority.',
        ('recovery_procedure.py','recovery_reference.py','recovery_history.py','recovery_decisions.py'),outputs=('current',)),
    # Retired 2026-10-27 (merged into prepare.ecdl_recovery); kept here so old jobs/artifacts stay readable, then
    # moved into LEGACY_INTERFACE_CATALOG below.
    CardDefinition('plan.ecdl_recovery','run_recovery_plan',(),
        '選択した枝の実測マップから、準備移動と方向を保った復旧手順・検証条件を作ります。',
        'Offline proposal with map hash and optical/axis context. A plan is not a verified recovery.',
        ('ecdl_recovery_plan.py',),outputs=()),
    CardDefinition('test.ecdl_recovery','run_recovery_test',
        ('optical_mode_monitor','bounded_axis','open_loop_state'),
        '指定したPZT・電流の有界変化でホップを試み、復旧手順で元の光学状態へ戻れるか実測します。',
        'Explicit finite perturbations; no observed hop is inconclusive. Fresh optical requalification and no retry after uncertain writes.',
        ('ecdl_recovery_test.py',),outputs=('piezo','current','temperature')),
    CardDefinition('tune.ecdl_branch_hop','run_branch_hop',
        ('optical_mode_monitor','bounded_axis','open_loop_state'),
        'Intentionally seek the first observed branch hop and hold there for signal checks.',
        'PI must already be off. Move current or PZT in one direction inside the existing owner envelope; '
        'stop at the first frequency discontinuity and confirm with fresh independent optical samples. '
        'Hold the final output without recovery, target changes or lock restart. '
        'The first observed branch is not proof of nearest-mode adjacency. Save wavelength/frequency for a later manual target choice.',
        ('ecdl_branch_hop.py',),outputs=('current','piezo')),
    CardDefinition('recover.ecdl_mode_hop','run_mode_recovery',
        ('optical_mode_monitor','bounded_axis','open_loop_state'),
        'Return toward the stable pre-hop frequency using the shared directed relative-current route.',
        'Read the latest same-laser epoch-labelled history; freeze a stable pre-hop reference before any output write. '
        'Select a matching parameter specialization or the explicit fallback, without branch-map admission. '
        'Keep TEC/PZT unchanged; validate the full current path through the owner. Fresh sustained frequency return is separate '
        'from optical quality and PI capture. Standalone recovery never starts the PI. Missing, stale or superseded references '
        'require new observations; uncertain writes are never replayed.',
        ('ecdl_mode_recovery.py','recovery_procedure.py','recovery_reference.py','recovery_history.py'),outputs=('current',)),
    CardDefinition('tune.operating_point','run_operating_point_card',(),
        '動作点の変更（汎用・単発）。軸ごとの移動指定 {axis, mode, target} のリストで、ロック中は稼働中ロックが保持する基準（sr422: TEC 外側ループの DAC 中心、LDC 電流基準；sr1092: native PI 中心）を、ロック解除中は保持出力（PI off）を、owner の既存の制限内で段階的に動かし、読み戻しと光学測定で確認します。'
        'target は絶対値・相対量・保存結果（直近の window-center 中点、activation）のいずれか。軸の意味づけはレーザー設定（control_settings.operating_point.axes）で与えます。'
        'schedule: [{moves, simultaneous, settle_s?, at_unix_s?, on_failure: hold|rollback_step|rollback_all}]（moves と排他）で、複数軸の同時移動（1 tick に各軸 1 段）、段ごとの待ち・予約時刻・失敗時の保持／段の巻き戻し／全体の巻き戻し（同じ有界経路）を指定できます。',
        'One procedure (rule F). Locked: the request is filed into the running lock job and executed under its lease as the lock card\'s own action; open-loop output moves are refused while the PI holds the outputs. Unlocked: bounded open-loop moves on the laser\'s own owner session. Owner bounds, step/slew, update interval, readback verification and uncertain-write quarantine apply to every write; nothing is retried.',
        ('operating_point.py',),outputs=('current','piezo','temperature')),
    CardDefinition('tune.pi_response','run_card',(),
        'Optimize native PI with a delegated task, learn settling criteria from measured response, and validate transition completion.',
        'No fixed convergence MHz threshold. The task selects an owner-validated PI/integral candidate; '
        'a full-rate repeated target round trip trains and independently validates a causal completion profile. '
        'Only validated matching profiles are enabled for target and operating-point transitions. '
        'Foreground operations preempt the experiment. No physical safety bounds or calibration changes.',
        ('pi_response.py','settling_profile.py','learned_settling.py'),outputs=()),
    CardDefinition('study.target_response','run_card',(),
        'Measure target-step response from full-rate wavelength history while the existing lock runs.',
        'Finite offsets relative to the starting target use the normal owner-mediated target path. '
        'Fetch every new ESP32-consumed wavelength from InfluxDB, record request/confirmation times, '
        'and assess sustained mean/RMS error, sample gaps and finite-record settling. Return to the starting '
        'target only while this study still owns the configuration revision. Foreground work preempts it. '
        'No gain/limit changes, automatic profile adoption, lock start or hardware recovery. '
        'An existing lock and configured readable history are required at execution.',
        ('target_response.py','target_response_analysis.py','target_response_decisions.py','study_execution.py',
         'wavelength_history.py','drivers/influx_wavelength.py'),outputs=()),
    CardDefinition('tune.lock_target','run_lock_target',(),
        'Change the shared laser target. Optional queue_steps splits a large request into bounded, sequential steps in one server-side job.',
        'Uses the configuration API and the owner bounded ramp. With queue_steps, each step stays within the laser maximum change and completes before the next. ECDL completion also requires fresh PZT readback at the active TEC center for at least one outer-loop interval; frequency arrival alone does not advance the queue. Cancellation, failure, timeout or a newer configuration stops remaining steps. Absolute target bounds remain enforced. Unlocked changes are saved only, without claiming measured settling. No process restart.',
        ('lock_target.py','target_sequence.py','ecdl_target_settling.py','dashboard_api.py'),outputs=()),
    CardDefinition('laser.emission','run_emission',(),
        'Electrical output ON/OFF with fresh measured-current verification; TEC is preserved.',
        'Reserve intentional-off inhibition, stop this laser control work through normal cleanup, then execute the owner recipe. Driver enable or bounded current modulation. Current evidence does not prove optical extinction. ON leaves the laser enabled but unlocked; acquisition/lock is a separate explicit operation.',
        ('emission.py','emission_adapters.py','emission_card.py'),outputs=('emission','current')),
    CardDefinition('tune.device_parameters','run_device_parameters',(),
        '機器設定の変更（単発、規範 I）。changes: [{instrument, channel?, parameter, value}] を登録簿経由で各機器の owner に送ります。owner がカタログ（型・単位・範囲・分類）、lease とチャネル書込み権限、稼働中ループとの衝突（lease 保持中の出力、burst/変調、PI 動作中）を確認して書き込み、読み戻して検証します。'
        '分類: free は承認不要、safety は人間の直接承認（approval_id）、maintainer は拒否。TEC・DAC・LDC のような動作点は通常 tune.operating_point で変えます。',
        'Rule I. Catalog per owner (describe/owner-state.json, registry parameter_catalog). Every refusal before the first write; owner applies, reads back and verifies; an uncertain write faults the owner (no retry). Legacy owners: CLD writes disabled (bias lease), scope read-only, sr1092 native policy = laser-definition fields saved through the dashboard (live native_hot). Approval ledger: free / safety (direct human approval) / maintainer refused.',
        ('device_parameters.py','dashboard_api.py'),outputs=()),
    CardDefinition('tune.ecdl_operating_point','run_operating_point',
        ('optical_mode_monitor','bounded_axis','open_loop_state'),
        'Align the present branch toward the requested frequency with calibrated TEC and current feedforward; keep PI off.',
        'Measure a stable checkpoint before each intentional step. Apply the owner-bounded TEC move and its explicit current feedforward, '
        'wait the configured thermal interval, then compare measured versus predicted frequency change. A hop restores the recent '
        'TEC/PZT checkpoint and executes recovery_procedure.execute with v2 fallback/specialization parameters; no archived branch frequency is an admission gate. '
        'Only confirmed frequency-return failures may retry. Quality is reported independently. Optional final bounded PZT trim uses the current axis calibration.',
        ('ecdl_operating_point.py',),outputs=('piezo','temperature','current'))):
    INTERFACE_CATALOG[definition.id]=definition
# 2026-10-27: plan.ecdl_recovery / test.ecdl_recovery retired (prepare.ecdl_recovery); readable, not startable.
LEGACY_INTERFACE_CATALOG.update({name:INTERFACE_CATALOG.pop(name) for name in ('plan.ecdl_recovery','test.ecdl_recovery')})
LEGACY_INTERFACE_CATALOG['tune.pid_factor_reduction']=INTERFACE_CATALOG.pop('tune.pid_factor_reduction')

# Offline DLC commissioning and unit-independent optical calibration (2026-10-28).
for definition in (
    CardDefinition('identify.axis_frequency','run',('optical_mode_monitor','bounded_axis','open_loop_state'),
        'Measure signed optical frequency response of a configured axis in both directions.',
        'Hold competing loops, retain fresh raw optical evidence, reject discontinuities, quantify hysteresis and residuals, and produce a calibration proposal with a vector PDF. Never adopt calibration automatically.',
        ('jobs/axis_frequency.py','jobs/axis_frequency_report.py'),outputs=('axis',)),
    CardDefinition('servo.dlcpro_frequency','run',('dlcpro_frequency_control',),
        'DLC Pro Ethernet PZT frequency PI with slow TEC recentering and internal current feedforward.',
        'Independent instrument and laser authorities, exclusive channel ownership, fresh optical quality observations, conditional anti-windup, finite missing-input hold and verified writes. Hardware commissioning is mandatory.',
        ('jobs/dlcpro_cards.py','owners/dlcpro_owner.py','drivers/dlcpro.py'),continuous=True,outputs=('piezo','temperature')),
    CardDefinition('sweep.dlcpro_frequency','run',('dlcpro_frequency_control',),
        'DLC Pro native triangle or sine scan, optionally with slow whole-cycle frequency centering.',
        'PI is held. Only complete acquisition-time-weighted cycles may correct scan center near cycle boundaries. Gapped or undersampled cycles hold center; TEC follows center, never instantaneous scan position.',
        ('jobs/dlcpro_cards.py','owners/dlcpro_owner.py','drivers/dlcpro.py'),outputs=('piezo','temperature')),
):
    INTERFACE_CATALOG[definition.id]=definition

# Fixed algorithm identities: the legacy mixed response operation is historical only.
LEGACY_INTERFACE_CATALOG['study.target_response'] = INTERFACE_CATALOG.pop('study.target_response')
from ldcon.jobs.response_algorithms import ALGORITHMS as _RESPONSE_ALGORITHMS
_RESPONSE_DESCRIPTIONS = {
    'measure.target_response': 'Measure prescribed target steps and record full-rate response without an agent.',
    'optimize.settling_criteria': 'Measure target responses and ask a task model to propose validated settling criteria.',
    'optimize.target_feedforward': 'Compare admitted target feedforward and internal step candidates, then evaluate settling.',
    'optimize.target_thermal': 'Compare admitted target feedforward and ECDL TEC centering parameters using response evidence.',
    'optimize.target_step': 'Search the target step size with the fixed deterministic response criterion.',
}
for _operation in _RESPONSE_ALGORITHMS:
    INTERFACE_CATALOG[_operation] = CardDefinition(_operation, 'run_card', (),
        _RESPONSE_DESCRIPTIONS[_operation],
        'The operation fixes the algorithm. Shared source-history recording preserves acquisition timestamps and all waveform data. '
        'Owner validation, finite experiments and foreground supersession apply. The model proposes; host checks evidence and authority before adoption.',
        ('target_response.py','response_algorithms.py','response_record.py'), outputs=())
INTERFACE_CATALOG['optimize.sweep_feedforward'] = CardDefinition(
    'optimize.sweep_feedforward', 'run_card', (),
    'Compare staircase sweep LDC/PZT feedforward pairs, settling and recorded mode-continuous spans by cadence.',
    'A finite paired-baseline experiment through registered sweep children. Full-rate unfitted wavelength error, '
    'complete level visits, repeated trials and thermal drift evidence qualify recommendations. '
    'Every child restores its own checkpoint. Cancellation may wait for a finite record to finish; uncertain '
    'restoration or loss of mode continuity prevents another trial. Saved settings are never changed. '
    'Requires explicit candidate pairs, waveforms, dwell, budgets and evaluation criteria; no agent is required.',
    ('jobs/sweep_ff_plan.py','jobs/sweep_ff_optimization.py','jobs/sweep_ff_analysis.py','jobs/sweep_ff_transport.py'),
    outputs=('optical.frequency','optical.wavelength'))
for _operation, _description in (
    ('measure.change','Apply an explicitly validated owner transaction, wait, and measure selected observables.'),
    ('measure.batch','Preflight an ordered array of change-and-measure steps and retain each result.'),
):
    INTERFACE_CATALOG[_operation] = CardDefinition(_operation, 'run_card', (), _description,
        'Wavelength, optical quality and dither are independent switches. Empty changes observe only; no measurements performs verified application only. '
        'Atomic, coordinated and ordered are distinct owner capabilities, never silently substituted. '
        'Fresh source timestamps, raw samples, gaps, partial outcomes and application receipts are saved before PDF reporting. '
        'A preset supplies no physical authority. Unknown writes stop the sequence without retry. Foreground operations take priority.',
        ('change_measure.py','change_measure_adapter.py','measurement_execution.py'),outputs=('selected_observables',))
