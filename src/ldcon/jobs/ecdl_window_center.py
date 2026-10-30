"""Execute a device-neutral window policy inside the existing steady worker.

No device connections or extra leases are created. Source qualification, output
limits, native PI and uncertain-write handling remain in the instrument owner.
"""
import copy
import math
import statistics
import time

from ldcon.core.contracts import MeasurementUnavailable
from ldcon.core.instrument_client import BrokerError
from ldcon.jobs.window_center import WindowCenterPolicy, parameters, fit_position_features, observable


def fill_metric_limits(p, control_settings):
    """Selected metric observables without explicit boundary_limits: the laser's optical single_mode_max for the selected indices."""
    if p['boundary_limits']:
        return p
    limits=((control_settings or {}).get('optical') or {}).get('single_mode_max') or {}
    p['boundary_limits']={name:float(limits[name[8:]]) for name in p['observables']
                          if name.startswith('metrics.') and type(limits.get(name[8:])) in (int,float)}
    if not p['boundary_limits']:
        raise ValueError('Window search needs boundary_limits or the laser optical single_mode_max for its indices')
    return p


def same_mode_band_hz(steady, control_settings):
    """Use the current recovery threshold, then the adopted owner capture band.

    Archived recovery artifacts do not supply executable policy. The window's
    own explicit hop_threshold_hz takes precedence at its caller.
    """
    steady = steady or {}
    if {'recovery_artifact', 'recovery_ref'} & set(steady):
        raise ValueError('Legacy recovery artifacts are retired; configure the current recovery threshold')
    threshold = (steady.get('recovery') or {}).get('hop_threshold_hz')
    if type(threshold) in (int, float) and threshold > 0:
        return float(threshold)
    capture = (((control_settings or {}).get('steady_lock') or {}).get('capture_hz'))
    if type(capture) in (int, float) and capture > 0:
        return float(capture)
    return None


class WindowCenterSession:
    def __init__(self, request, p, lock, seed, emit, publish, steady, *, clock=time.monotonic, wall_clock=time.time, decisions=None):
        self.request=request;self.p=parameters({'parameters':p},{})
        self.clock=clock;self.emit=emit;self.publish=publish;self.steady=steady or {}
        self.decisions=decisions
        self.wall_clock=wall_clock
        self.seed=copy.deepcopy(seed or {})
        if self.p['target_frequency_hz'] is None:
            self.p['target_frequency_hz']=self.steady.get('target_frequency_hz',self.seed.get('target_frequency_hz'))
        if self.p['hop_threshold_hz'] is None:
            self.p['hop_threshold_hz']=same_mode_band_hz(self.steady,lock.session.device.get('control_settings'))
        self.hop_count=0;self.hop_budget_exhausted=False;self.finish_pending=None
        if self.seed and 'measured_unix_s' in self.seed:
            self.seed['measured_s']=clock()-(self.wall_clock()-self.seed['measured_unix_s'])
        self.axis=lock.recovery_ports()['current'].policy
        if not self.p['measure_dither']:
            fill_metric_limits(self.p,lock.session.device['control_settings'])
        self.point_dither=self.p['measure_dither'] and self.p['mode']!='position'
        self.fast_slow=bool(self.p['slow_observables']) and self.point_dither
        self.interval=max(self.axis.update_interval_s,self.p['settle_s'])
        self.slow_fit_delay=0.
        if self.point_dither:
            d=lock.session.device['control_settings']['steady_lock']['dither']
            self.slow_fit_delay=max(d['window_s'],3/d['frequency_hz']+1.)+d.get('delivery_delay_s',3.)
            self.interval=max(self.axis.update_interval_s,d['window_s']+d.get('delivery_delay_s',3.),3/d['frequency_hz']+1.)
            if self.fast_slow:self.interval=max(self.axis.update_interval_s,1.)
        self.confirm_fit_source=None
        self.next_at=0.;self.policy=None;self.controlling=False
        self.status='running';self.needs_recovery=False;self.recovery_reason=None
        self.result={};self.confirming=[];self.verifying=False;self.scan_points=[];self.last_signature=None;self.soft_scan_times=[]
        self.position_started=clock();self.position_origin=None;self.position_target=None
        self.position_ready_at=0.;self.position_stamp=None;self.position_interrupted=False
        self.position_expected=None;self.position_steps=0
        self.return_origin=None;self.safe_interior=None;self.return_expected=None
        self.returning=None;self.return_last_stamp=None;self.return_ready_at=0.
        self.return_interrupted=False;self.write_unknown=False;self.acknowledged_moves=0
        self.baseline_move_count=0
        self.return_context_error=None

    @property
    def freeze_reference(self):
        return self.controlling or bool(self.policy and self.policy.state not in ('monitoring','complete'))

    def record(self, kind, payload):
        self.emit(kind,{**payload,'request_id':self.request['request_id'],
            'boundary_thresholds':self.p['boundary_limits'],'warning_thresholds':self.p['early_limits'],
            'position_warning_fraction':self.p['position_warning_fraction'],
            '_context':dict(card=self.request['card_id'],operation='tune.ecdl_window_center')})

    def _end_control(self, lock):
        if self.controlling:
            lock.response_end();self.controlling=False

    def finish(self, lock, status, reason=None):
        # Whole-worker shutdown must not be delayed by an auxiliary return.
        # An unknown write can never be relabelled as a successful cancellation.
        if self.write_unknown or lock.uncertain_output:
            self.status='unknown'
            self.result.update(status='unknown',reason='window output outcome requires review',terminal_reason=reason)
            self.result['return_outcome']=dict(status='skipped',reason='uncertain output; no return write')
            if self.decisions is not None:self.decisions.finish(self.result)
            self.publish(dict(request_id=self.request['request_id'],card_id=self.request['card_id'],**self.result))
            self.record('window_center_end',self.result)
            return
        if self.returning:
            self.result['return_outcome']={**self.returning,'status':'interrupted','reason':'worker stopped before return verification'}
            self.returning=None
        self._end_control(lock);self.status=status
        self.result.update(status=status,reason=reason)
        if self.decisions is not None:self.decisions.finish(self.result)
        self.publish(dict(request_id=self.request['request_id'],card_id=self.request['card_id'],**self.result))
        self.record('window_center_end',self.result)

    def _read(self, lock, *, dither=True):
        frames=[lock.response_info() for _ in range(3)]
        last=copy.deepcopy(frames[-1]);obs=last['observation']
        stamps=[r['observation']['frequency'].get('acquired_unix_s',r['observation']['frequency']['acquired_s']) for r in frames]
        if not all(b>a for a,b in zip(stamps,stamps[1:])):
            raise MeasurementUnavailable('Window fringe group needs distinct acquisitions')
        if len({r['observation']['mode_id'] for r in frames})!=1:
            raise MeasurementUnavailable('Optical branch changed within window fringe group')
        for axis in ('current','temperature'):
            if len({r['outputs'][axis] for r in frames})!=1:
                raise MeasurementUnavailable('Axis context changed within window fringe group')
        # Never average a hop into an apparently valid same-mode frequency.
        frequencies=[r['observation']['frequency']['frequency_hz'] for r in frames]
        threshold=self.p['hop_threshold_hz']
        if threshold and max(frequencies)-min(frequencies)>threshold:
            raise MeasurementUnavailable('Frequency discontinuity within fringe group')
        metrics=obs.get('metrics') or {}
        obs['metrics']={k:statistics.median(r['observation']['metrics'][k] for r in frames) for k in metrics}
        votes=[r['observation']['single_mode'] for r in frames
               if type(r['observation']['single_mode']) is bool]
        obs['single_mode']=(sum(votes)>=2) if len(votes)>=2 else None
        last['acquired_s']=obs['frequency']['acquired_s'];last['acquired_unix_s']=stamps[-1]
        if self.p['average_samples']>1 or self.fast_slow:
            # Never hide a single urgent raw fringe frame in the group median.
            for name,limit in self.p['urgent_limits'].items():
                if name.startswith('metrics.'):
                    values=[observable(frame,name) for frame in frames]
                    if any(type(v) in (int,float) and math.isfinite(v) and v>=limit for v in values):
                        obs['metrics'][name[8:]]=max(v for v in values if type(v) in (int,float) and math.isfinite(v))
            if self.p['require_single_mode'] and any(not r['observation']['single_mode'] for r in frames):
                obs['single_mode']=False
        need_slow=self.point_dither
        if self.fast_slow and self.policy and self.clock()<self.policy.last_move_s+self.slow_fit_delay:
            need_slow=False
        if self.point_dither:
            # response_info may contain a cached fit. It is not evidence for
            # this current point until its complete source window qualifies.
            for key in list(last):
                if key.startswith('dither_'):del last[key]
            last.update(dither_response=None,dither_response_valid=False,dither_acquisition_status='waiting',
                        dither_acquisition_reason='awaiting wholly post-move fit')
        if dither and need_slow and 'dither_response' in self.p['observables'] and self.p['mode']!='position':
            try:
                info=lock.response_dither_info()
                source=info.get('dither_source_unix_s');start=info.get('dither_source_start_unix_s')
                if info.get('dither_analysis_status')=='paused' or info.get('dither_paused') is True:
                    # Dither pause (2026-10-27): the slow observable cannot exist; disabled dither measurement is unaffected.
                    raise MeasurementUnavailable('Dither paused (steady_lock.dither.paused): dither_response observable unavailable')
                if (info.get('dither_response_valid') is not True or
                    type(info.get('dither_response')) not in (int,float) or not math.isfinite(info['dither_response']) or
                    any(type(v) not in (int,float) or not math.isfinite(v) for v in (source,start)) or start>source or source>self.wall_clock()):
                    raise MeasurementUnavailable('Timestamped valid dither fit required')
                last.update({k:v for k,v in info.items() if k.startswith('dither_')})
                last['dither_source_start_s']=self.clock()-(self.wall_clock()-start)
                after=self.policy.last_move_s if self.policy else self.position_started
                if last['dither_source_start_s']<=after:
                    raise MeasurementUnavailable('Dither fit still contains pre-move data')
                last.update(dither_acquisition_status='valid',dither_acquisition_reason=None)
            except MeasurementUnavailable as error:
                urgent=(self.p['require_single_mode'] and not obs['single_mode']) or any(
                    type(observable(last,k)) in (int,float) and math.isfinite(observable(last,k)) and observable(last,k)>=v
                    for k,v in self.p['urgent_limits'].items())
                if not urgent and not self.fast_slow:raise
                last['dither_response']=None
                last.update(dither_response_valid=False,dither_acquisition_status='waiting',dither_acquisition_reason=str(error))
        last['fringe_group']=frames
        return last

    def _return_context(self, lock, row):
        """Qualify a current-only return without requiring fine single mode."""
        from ldcon.owners.ecdl_steady_policy import entry_quality
        profile=lock.session.device['control_settings']['steady_lock']
        origin=self.return_origin
        for frame in row['fringe_group']:
            obs=frame['observation'];f=obs['frequency'];outputs=frame['outputs']
            values=[f.get('frequency_hz'),f.get('acquired_s'),*outputs.values()]
            if any(type(v) not in (int,float) or not math.isfinite(v) for v in values):
                raise MeasurementUnavailable('Finite return observation required')
            # v5.1-hostcheck (human decision 2026-10-22): wavemeter quality/status/aligned labels are informational.
            if not 0<=self.clock()-f['acquired_s']<=self.p['measurement_max_age_s']:
                raise MeasurementUnavailable('Fresh return observation required')
            if frame.get('native_pi_active') is not True:
                return 'native PI is not active'
            if not isinstance(obs.get('mode_id'),str) or not obs['mode_id']:
                return 'optical branch identity is unavailable'
            if origin and (obs.get('mode_id')!=origin['mode_id']
                    or abs(outputs['temperature']-origin['temperature'])>self.p['temperature_tolerance']):
                return 'optical branch or TEC context changed'
            target=self.p['target_frequency_hz']
            bounds=[v for v in (self.p['hop_threshold_hz'],profile.get('capture_hz')) if v is not None]
            if target is not None and bounds and abs(f['frequency_hz']-target)>min(bounds):
                return 'frequency left the same-mode return band'
            qualified={**obs,'maintenance_required':obs.get('maintenance_required',False)}
            if not entry_quality(qualified,profile,target if target is not None else f['frequency_hz']):
                raise MeasurementUnavailable('Configured coarse entry quality required for return')
            if not self.axis.low<=outputs['current']<=self.axis.high:
                return 'current outside owner bounds'
        expected=self.return_expected
        if expected is not None and abs(row['outputs']['current']-expected)>max(self.axis.resolution/2,1e-10):
            return 'current changed outside acknowledged window path'
        return None

    def _remember_return_point(self, lock, row):
        try:unsafe=self._return_context(lock,row)
        except MeasurementUnavailable:return
        self.return_context_error=unsafe
        if unsafe:return
        evidence=dict(row['outputs'],mode_id=row['observation']['mode_id'],
            acquired_unix_s=row['acquired_unix_s'],metrics=dict(row['observation']['metrics']))
        if self.return_origin is None:
            self.return_origin=evidence
            self.return_expected=row['outputs']['current']
        ready_at=self.position_ready_at if self.p['mode']=='position' else self.policy.ready_at if self.policy else 0.
        if ready_at is None or self.clock()<ready_at:return
        selected={k:observable(row,k) for k in self.p['observables']}
        if ((not self.p['require_single_mode'] or row['observation']['single_mode']) and
                all(type(v) in (int,float) and math.isfinite(v) for v in selected.values()) and
                all(selected[k]<v for limits in (self.p['boundary_limits'],self.p['urgent_limits']) for k,v in limits.items())):
            self.safe_interior=evidence

    def _move(self, lock, value):
        try:result=lock.response_current(value)
        except MeasurementUnavailable:
            if lock.uncertain_output:self.write_unknown=True
            raise
        except BrokerError as error:
            if error.code not in ('range','configuration','measurement') or lock.uncertain_output:
                self.write_unknown=True
            raise
        except Exception:
            self.write_unknown=True
            raise
        if lock.uncertain_output:
            self.write_unknown=True
            raise RuntimeError('Window write outcome requires review')
        if result.get('applied'):
            value=result.get('value')
            if type(value) not in (int,float) or not math.isfinite(value) or not self.axis.low<=value<=self.axis.high:
                self.write_unknown=True
                raise RuntimeError('Window write acknowledgement lacks a valid current')
            self.return_expected=result['value'];self.acknowledged_moves+=1
        return result

    def _request_finish(self, lock, status, reason):
        if status=='succeeded':
            self.finish(lock,status,reason);return
        if self.returning:
            # A repeated cancel cannot renew either budget or the return target.
            if status=='cancelled':
                self.returning['terminal_status']=status
                self.returning['terminal_reason']=reason
            return
        if self.write_unknown or lock.uncertain_output:
            self.finish(lock,status,reason);return
        target=self.safe_interior or self.return_origin
        if (not self.acknowledged_moves or target is None or self.needs_recovery or self.return_context_error
                or self.return_interrupted or self.position_interrupted):
            self.result['return_outcome']=dict(status='skipped',reason=self.return_context_error or 'no acknowledged excursion or safe return context')
            self.finish(lock,status,reason);return
        if abs(self.return_expected-target['current'])<=max(self.axis.resolution/2,1e-10):
            self.result['return_outcome']=dict(status='not_needed',target=target['current'])
            self.finish(lock,status,reason);return
        amount=min(self.axis.max_step,(self.axis.high-self.axis.low)*self.p['step_fraction'],
                   self.axis.max_slew_per_s*self.axis.update_interval_s)
        if self.axis.resolution:amount=math.floor((amount+1e-12)/self.axis.resolution)*self.axis.resolution
        if amount<=0:
            self.result['return_outcome']=dict(status='unavailable',reason='return step below owner resolution')
            self.finish(lock,status,reason);return
        budget=min(self.p['max_steps'],math.ceil(abs(self.return_expected-target['current'])/amount)+1)
        timeout=min(self.p['timeout_s'],(budget+2)*(self.interval+self.p['settle_s']+self.p['measurement_max_age_s']))
        self.returning=dict(terminal_status=status,terminal_reason=reason,target=target['current'],
            original_status=status,original_reason=reason,
            target_evidence=copy.deepcopy(target),
            target_source='verified_interior' if self.safe_interior else 'origin',steps=0,
            max_steps=budget,deadline_s=self.clock()+timeout,step=amount)
        self.confirming=[];self.verifying=False;self.next_at=0.;self.return_last_stamp=None
        self.return_ready_at=self.clock()+self.p['settle_s']
        if self.policy:self.policy.pending=None
        self.result['return_outcome']={**self.returning,'status':'returning'}
        self.publish(dict(request_id=self.request['request_id'],card_id=self.request['card_id'],status='running',**self.result))
        self.record('window_return_started',self.returning)

    def _finish_return(self, lock, outcome, reason=None, row=None):
        returning=self.returning;self.returning=None
        self.result['return_outcome']={**returning,'status':outcome,'reason':reason}
        if row:self.result['return_outcome']['outputs']=row['outputs']
        self.record('window_return_end',self.result['return_outcome'])
        self.finish(lock,returning['terminal_status'],returning['terminal_reason'])

    def _return(self, lock):
        returning=self.returning
        if self.write_unknown or lock.uncertain_output:
            self.finish(lock,'unknown',returning['terminal_reason']);return
        if self.return_interrupted or self.needs_recovery:
            self._finish_return(lock,'unsafe','original-mode recovery owns the rig');return
        if self.clock()>returning['deadline_s']:
            self._finish_return(lock,'expired','finite return deadline; outputs held');return
        if self.clock()<max(self.next_at,self.return_ready_at):return
        self.next_at=self.clock()+self.interval
        try:
            row=self._read(lock,dither=False)
            unsafe=self._return_context(lock,row)
            stamp=row['acquired_unix_s']
            if self.return_last_stamp is not None and stamp<=self.return_last_stamp:
                raise MeasurementUnavailable('Return needs a new optical acquisition')
            self.return_last_stamp=stamp
            if unsafe:
                self._finish_return(lock,'unsafe',unsafe,row);return
            if self.clock()>returning['deadline_s']:
                self._finish_return(lock,'expired','return deadline reached during observation',row);return
            current=row['outputs']['current']
            if abs(current-returning['target'])<=max(self.axis.resolution/2,1e-10):
                self._finish_return(lock,'verified','fresh same-context return verified',row);return
            if returning['steps']>=returning['max_steps']:
                self._finish_return(lock,'exhausted','finite return step budget; outputs held',row);return
            if not self.controlling:
                lock.response_begin();self.controlling=True
                unsafe=self._return_context(lock,row)
                if unsafe:
                    self._finish_return(lock,'unsafe',unsafe,row);return
            if self.clock()>returning['deadline_s']:
                self._finish_return(lock,'expired','return deadline reached before write',row);return
            delta=max(-returning['step'],min(returning['step'],returning['target']-current))
            value=current+delta
            if self.axis.resolution:value=round(value/self.axis.resolution)*self.axis.resolution
            result=self._move(lock,value)
            if result.get('applied'):
                returning['steps']+=1;self.return_ready_at=self.clock()+self.p['settle_s']
                self.record('command_applied',dict(interface='current',value=result['value'],phase='window_return'))
            else:self.record('window_command_deferred',dict(phase='window_return',result=result))
        except MeasurementUnavailable as error:
            if self.write_unknown or lock.uncertain_output:raise
            self.record('window_measurement_wait',dict(phase='window_return',reason=str(error)))
        except BrokerError as error:
            if error.code!='range' or self.write_unknown or lock.uncertain_output:raise
            self._finish_return(lock,'rejected','owner rejected return: '+str(error))

    def poll(self, lock, cancel=False):
        if self.status!='running':return
        if cancel:
            self._request_finish(lock,'cancelled','user cancelled window operation')
            if self.status!='running':return
        if self.finish_pending:
            # hop budget exhausted (after_recovery): the lock stays locked at its restored point, no return move
            status,reason=self.finish_pending;self.finish_pending=None
            self.result=dict(self.policy._result(),done=False,hop_budget_exhausted=True,max_hops=self.p['max_hops'])
            self._request_finish(lock,status,reason);return
        if self.returning:
            self._return(lock);return
        now=self.clock()
        if self.p['mode']!='monitor' and now-self.position_started>self.p['timeout_s']:
            self._request_finish(lock,'failed','window operation deadline reached');return
        if self.position_interrupted:
            self._request_finish(lock,'failed','position interrupted by original-mode recovery');return
        if now<self.next_at or self.needs_recovery:return
        self.next_at=now+self.interval
        try:row=self._read(lock)
        except MeasurementUnavailable as error:
            self.record('window_measurement_wait',dict(reason=str(error)));return
        now=self.clock()
        if not self.controlling and self.policy and self.policy.state=='monitoring':
            # Automatic TEC/current control can run between separate studies.
            self.return_origin=None;self.safe_interior=None;self.return_expected=None;self.acknowledged_moves=0
        self._remember_return_point(lock,row)
        if self.p['mode']=='position':
            if now-self.position_started>self.p['timeout_s']:
                self._request_finish(lock,'failed','position verification deadline reached');return
            self._position(lock,row,now);return
        if self.policy is None:
            self.policy=WindowCenterPolicy(self.p,row['outputs'],self.axis,now,self.seed or None)
            self.policy.soft_scan_times=self.soft_scan_times
            if self.fast_slow and not self.controlling:
                lock.response_begin();self.controlling=True;now=self.clock()
        if self.policy.state=='monitoring' and self.seed and now-self.seed.get('measured_s',now)>self.p['seed_ttl_s']:
            self.record('window_context_restudy',dict(reason='measured window expired'))
            self.seed={};self.policy=WindowCenterPolicy(self.p,row['outputs'],self.axis,now)
            self.policy.soft_scan_times=self.soft_scan_times
        if self.verifying:
            self._confirm(lock,row);return
        state=self.policy.tick(now,row);self.result=state
        self.record('window_center_observation',dict(policy=state,outputs=row['outputs'],
            observation=row['observation'],
            **{k:v for k,v in row.items() if k.startswith('dither_')}))
        if state.get('recovery_required'):
            self.needs_recovery=True;self.recovery_reason=state.get('warning','window optical branch loss');return
        if state.get('done') and state['state']!='complete':
            if self.p['require_single_mode'] and not row['observation']['single_mode'] and not self.p['initial_interior_search']:
                # Rule K (2026-10-27): same-branch spectral degradation is recorded in the result; the search
                # finishes like any other unresolved search and does not request original-branch recovery.
                state['degraded']=True;self.result=state
                self.record('window_clean_point_degraded',dict(reason='same-branch spectrum degraded before a clean window interior was found'))
            # Old thermal context cannot seed a new scan. A qualified clean
            # observation can start a fresh bounded two-sided study instead.
            if self.p['mode']=='monitor' and state['state']=='invalidated' and (not self.p['require_single_mode'] or row['observation']['single_mode']):
                self.seed={};self.policy=None;self.scan_points=[]
                self.record('window_context_restudy',dict(reason=state.get('warning')));return
            self._request_finish(lock,'failed',state.get('warning','window unresolved'));return
        if state.get('center_verified') or state.get('done'):
            if not self.controlling:
                lock.response_begin();self.controlling=True
            self.confirming=[];self.verifying=True;self._confirm(lock,row);return
        action=state.get('action')
        if action:
            if self.decisions is not None:
                from experiment_core.errors import Deadline
                try:
                    choice=self.decisions.choose(action,row)
                except Deadline:
                    self._request_finish(lock,'failed','decision budget exhausted');return
                except Exception as error:
                    # Provider faults park only this subordinate operation. The
                    # existing finite operation/context deadlines still apply.
                    self.record('window_decision_wait',dict(error_type=type(error).__name__))
                    self.policy.pending=None
                    return
                if choice in ('finish','cancel'):
                    self.decisions.context.cancelled=True
                    self._request_finish(lock,'cancelled' if choice=='cancel' else 'inconclusive',
                                         'decision provider ended search');return
                if choice=='repeat' or self.clock()-row['acquired_s']>self.p['measurement_max_age_s']:
                    self.policy.pending=None
                    self.record('window_decision_wait',dict(reason='repeat requested or proposal observation expired'))
                    return
            try:
                if not self.controlling:
                    lock.response_begin();self.controlling=True
                result=(self.decisions.move(self,lock,self._move,action['value']) if self.decisions is not None
                        else self._move(lock,action['value']))
            except MeasurementUnavailable as error:
                if self.write_unknown or lock.uncertain_output:raise
                self.record('window_command_deferred',dict(reason=str(error),proposal=action));return
            except BrokerError as error:
                if error.code!='range' or lock.uncertain_output:raise
                self.record('window_command_deferred',dict(reason=str(error),proposal=action))
                self._request_finish(lock,'failed','window proposal rejected by owner: '+str(error));return
            if result.get('applied'):
                self.policy.acknowledge(result['value'],now=self.clock())
                self.record('command_applied',dict(interface='current',value=result['value'],phase='window_center'))
            else:self.record('window_command_deferred',dict(proposal=action,result=result))
        elif state['state']=='monitoring':self._end_control(lock)
        if state['state'] in ('seek_edge','return_origin','centering','slow_boundary'):
            self.scan_points.append(row)
        signature=(state['state'],state.get('center'),state.get('steps'))
        if signature!=self.last_signature:
            self.publish(dict(request_id=self.request['request_id'],card_id=self.request['card_id'],status='running',**state))
            self.last_signature=signature

    def _confirm(self, lock, row):
        target=self.p['target_frequency_hz'];obs=row['observation']
        selected={k:observable(row,k) for k in self.p['observables']}
        if any(type(v) not in (int,float) or not math.isfinite(v) for v in selected.values()):
            self.confirming=[]
            self.record('window_measurement_wait',dict(reason='Finite selected midpoint features required'));return
        if any(selected[k]>=v for k,v in self.p['boundary_limits'].items()):
            self._request_finish(lock,'failed','Selected boundary metric is not clean at the proposed midpoint');return
        if self.p['require_single_mode'] and self.p['initial_interior_search'] and not obs['single_mode']:
            self._request_finish(lock,'failed','Single mode was not confirmed at the proposed midpoint');return
        tolerance=self.steady.get('tolerance_hz',self.p['hop_threshold_hz'])
        if ((self.p['require_single_mode'] and not obs['single_mode']) or (target is not None and tolerance is not None
                and abs(obs['frequency']['frequency_hz']-target)>tolerance)):
            self.confirming=[];self.verifying=False;self.needs_recovery=True;self.recovery_reason='window midpoint qualification failed';return
        if any(type(observable(row,k)) in (int,float) and math.isfinite(observable(row,k)) and observable(row,k)>=v for k,v in self.p['urgent_limits'].items()):
            self._request_finish(lock,'failed','Urgent metric exceeded at proposed midpoint');return
        if self.point_dither:
            source=row.get('dither_source_unix_s')
            if source is None or self.confirm_fit_source is not None and source<=self.confirm_fit_source:
                return
            self.confirm_fit_source=source
        self.confirming.append(row)
        if len(self.confirming)<3:return
        model=fit_position_features(self.scan_points,self.p['observables'],require_single_mode=self.p['require_single_mode'])
        if model.get('candidates'):
            self.policy.features=model
        # Fix the feature zero only after a newly verified center. Do not
        # continuously adapt a reference while the spectrum is degrading.
        for name,m in self.policy.features.get('candidates',{}).items():
            values=[observable(r,name) for r in self.confirming]
            if all(type(v) in (int,float) and math.isfinite(v) for v in values):
                m['intercept']=statistics.mean(values)-m['slope']*self.policy.midpoint
        self.policy.quiet_until=self.clock()+self.p['soft_quiet_s']
        self.result=self.policy._result()
        means={k:statistics.mean(observable(r,k) for r in self.confirming) for k in self.p['early_limits']}
        conflicts=[k for k,threshold in self.p['early_limits'].items() if means[k]>=threshold]
        self.result.update(center_definition=('branch_limited_window' if self.policy and 'hop' in self.policy.edge_kinds.values() else
                                             'single_mode_window' if self.p['require_single_mode'] else 'selected_limits_window'),
                           require_single_mode=self.p['require_single_mode'],soft_warning_at_center=conflicts,soft_proxy_qualification='unresolved' if conflicts else 'provisional')
        self.record('window_midpoint_verified',dict(policy=self.result,feature_model=self.policy.features,
            outputs=row['outputs'],samples=self.confirming))
        self.seed=dict(low=list(self.policy.edges[-1]),high=list(self.policy.edges[1]),
            temperature=row['outputs']['temperature'],mode_id=obs['mode_id'],
            target_frequency_hz=target,measured_s=self.clock(),measured_unix_s=self.wall_clock(),
            feature_model=self.policy.features,qualification=self.policy.qualification())
        self.confirming=[];self.verifying=False;self.scan_points=[];self._end_control(lock)
        if self.acknowledged_moves>self.baseline_move_count:
            self.record('quality_adjustment_accepted', dict(family='current',
                action_id=self.request['request_id']+':center:'+str(self.acknowledged_moves),
                accepted_unix_s=self.wall_clock()))
            self.baseline_move_count=self.acknowledged_moves
        if self.p['mode']=='center_only':self._request_finish(lock,'succeeded','fresh midpoint verified')
        else:
            self.publish(dict(request_id=self.request['request_id'],card_id=self.request['card_id'],status='running',**self.result))

    def _position(self, lock, row, now):
        """Bounded move to a selected operating point; never claims a window center."""
        obs=row['observation'];frequency=obs['frequency'];stamp=row['acquired_unix_s']
        finite_values=[frequency.get('frequency_hz'),row['acquired_s'],stamp,*row['outputs'].values()]
        # v5.1-hostcheck (human decision 2026-10-22): wavemeter quality/status/aligned labels are informational.
        if (any(type(v) not in (int,float) or not math.isfinite(v) for v in finite_values)
                or not 0<=now-row['acquired_s']<=self.p['measurement_max_age_s']
                or self.position_stamp is not None and stamp<=self.position_stamp):
            self.record('window_measurement_wait',dict(reason='new qualified position observation required'));return
        self.position_stamp=stamp
        target=self.p['target_frequency_hz'];threshold=self.p['hop_threshold_hz']
        if (self.position_origin is not None and obs['mode_id']!=self.position_origin['mode_id']
                or target is not None and threshold is not None and abs(frequency['frequency_hz']-target)>threshold):
            self.needs_recovery=True;self.recovery_reason='mode lost during operating-point move';return
        if self.position_origin is None:
            self.position_origin=dict(row['outputs'],mode_id=obs['mode_id'])
            self.position_expected=row['outputs']['current']
            delta=(self.axis.high-self.axis.low)*self.p['position_offset_fraction']
            if self.axis.resolution:
                delta=math.copysign(math.floor((abs(delta)+1e-12)/self.axis.resolution)*self.axis.resolution,delta)
            value=row['outputs']['current']+delta
            if self.axis.resolution:value=round(value/self.axis.resolution)*self.axis.resolution
            if self.p['position_offset_fraction'] and abs(value-row['outputs']['current'])<1e-12:
                self._request_finish(lock,'failed','selected offset below axis resolution');return
            if not self.axis.low<=value<=self.axis.high:
                self._request_finish(lock,'failed','selected operating point outside owner bounds');return
            self.position_target=value
        if abs(row['outputs']['temperature']-self.position_origin['temperature'])>self.p['temperature_tolerance']:
            self._request_finish(lock,'failed','TEC context changed during operating-point move');return
        if not self.controlling:
            lock.response_begin();self.controlling=True
        if self.clock()-self.position_started>self.p['timeout_s']:
            self._request_finish(lock,'failed','position verification deadline reached');return
        now=self.clock()
        if not 0<=now-row['acquired_s']<=self.p['measurement_max_age_s']:
            self.confirming=[]
            self.record('window_measurement_wait',dict(reason='position observation expired during control acquisition'));return
        current=row['outputs']['current']
        if self.position_expected is not None and abs(current-self.position_expected)>max(self.axis.resolution/2,1e-10):
            self._request_finish(lock,'failed','current changed outside confirmed operating-point path');return
        urgent=any(type(observable(row,k)) not in (int,float) or not math.isfinite(observable(row,k))
                   or observable(row,k)>=v for k,v in self.p['urgent_limits'].items())
        tolerance=self.steady.get('tolerance_hz',threshold)
        from ldcon.owners.ecdl_steady_policy import entry_quality
        profile=lock.session.device['control_settings']['steady_lock']
        qualified=entry_quality(obs,profile,target if target is not None else frequency['frequency_hz']) if profile.get('entry_gate') else obs['single_mode']
        clean=qualified and not urgent and (target is None or tolerance is None or abs(frequency['frequency_hz']-target)<=tolerance)
        if now<self.position_ready_at or not clean:
            self.confirming=[]
            self.result=dict(state='position_waiting',target=self.position_target,center=None,
                             reason='settling or qualified optical conditions required before next move')
            self.publish(dict(request_id=self.request['request_id'],card_id=self.request['card_id'],status='running',**self.result));return
        if abs(current-self.position_target)>max(self.axis.resolution/2,1e-10):
            if self.position_steps>=self.p['max_steps']:
                self._request_finish(lock,'failed','operating-point move reached finite step budget');return
            amount=min(self.axis.max_step,(self.axis.high-self.axis.low)*self.p['step_fraction'])
            delta=max(-amount,min(amount,self.position_target-current))
            if self.axis.resolution:
                delta=math.copysign(math.floor((abs(delta)+1e-12)/self.axis.resolution)*self.axis.resolution,delta)
            value=current+delta
            if self.axis.resolution:value=round(value/self.axis.resolution)*self.axis.resolution
            if abs(value-current)<1e-12:
                self._request_finish(lock,'failed','position step below axis resolution');return
            try:result=self._move(lock,value)
            except MeasurementUnavailable as error:
                if self.write_unknown or lock.uncertain_output:raise
                self.record('window_command_deferred',dict(reason=str(error)));return
            except BrokerError as error:
                if error.code!='range' or lock.uncertain_output:raise
                self._request_finish(lock,'failed','position rejected by owner: '+str(error));return
            if result.get('applied'):
                self.position_expected=result['value'];self.position_steps+=1
                self.position_ready_at=self.clock()+self.p['settle_s'];self.confirming=[]
                self.record('command_applied',dict(interface='current',value=result['value'],phase='operating_point'))
            return
        self.confirming.append(row)
        self.result=dict(state='position_verifying',target=self.position_target,verified_samples=len(self.confirming),center=None)
        self.publish(dict(request_id=self.request['request_id'],card_id=self.request['card_id'],status='running',**self.result))
        if len(self.confirming)>=3:
            self.record('operating_point_verified',dict(outputs=row['outputs'],samples=self.confirming,center_claimed=False))
            self._request_finish(lock,'succeeded','selected operating point freshly verified; window midpoint not inferred')
            if self.status == 'succeeded' and self.position_steps:
                self.record('quality_adjustment_accepted', dict(family='current',
                    action_id=self.request['request_id']+':position', accepted_unix_s=self.wall_clock()))

    def before_recovery(self):
        # The owner hold/arm lifecycle resets response_control. Never release
        # or replay a pre-hop proposal while native recovery owns the rig.
        self.controlling=False;self.confirming=[];self.confirm_fit_source=None;self.verifying=False
        self.return_interrupted=bool(self.returning)
        self.return_origin=None;self.safe_interior=None;self.return_expected=None;self.acknowledged_moves=0
        self.return_context_error=None
        if self.p['mode']=='position':self.position_interrupted=True
        policy=self.policy
        if policy is None or self.p['mode']=='position' or self.returning or not policy.resumable:
            # no window step in progress (midpoint confirmation, monitoring, return after cancel): old restudy
            self.policy=None;self.seed={};return
        # 2026-10-27: a confirmed hop during a window step IS the boundary on that side. Record it (interval = last
        # on-branch current ... first off-branch current, kind hop) before the recovery runs; the search is suspended
        # with its boundaries and resumed after the re-arm. Each recovery counts against the card's max_hops.
        # the off-branch current of a lock-confirmed hop is the last OBSERVED current (a write not yet read back
        # was never observed on either branch); the policy attributes the side (last probing step, or the boundary
        # it had just accepted when travelling back from it)
        current=policy.confirmed_current
        source='policy' if self.needs_recovery else 'lock'
        hop=policy.suspend_for_recovery(current,reason=self.recovery_reason or 'confirmed original-mode frequency/identity loss (lock)',source=source)
        self.hop_count+=1
        self.hop_budget_exhausted=self.hop_count>self.p['max_hops']
        self.record('window_hop_boundary',dict(hop=hop,boundaries=policy._boundaries(),hop_count=self.hop_count,
            max_hops=self.p['max_hops'],budget_exhausted=self.hop_budget_exhausted,source=source))

    def after_recovery(self):
        self.needs_recovery=False;self.recovery_reason=None;self.next_at=0.
        policy=self.policy
        if policy is None or policy.state!='suspended':
            self.record('window_restudy_after_recovery',dict(reason='original branch freshly verified; no window step was in progress, old proposals and ends discarded'))
            return
        if self.hop_budget_exhausted:
            # ends the action at the next poll (needs the lock handle); the lock stays locked at the restored point
            self.finish_pending=('failed','hop budget exhausted')
            self.record('window_hop_budget_exhausted',dict(hop_count=self.hop_count,max_hops=self.p['max_hops'],boundaries=policy._boundaries()))
            return
        state=policy.resume_after_recovery(self.clock());self.result=state
        self.record('window_search_resumed_after_recovery',dict(policy=state,reason='original branch freshly verified; measured boundaries kept',
            next_side=state.get('next_side'),hop_count=self.hop_count,max_hops=self.p['max_hops']))
        self.publish(dict(request_id=self.request['request_id'],card_id=self.request['card_id'],status='running',**state))


# ------------------------------------------------------------------------------------------ unlocked (rule F standalone)
def _unlocked_row(ports):
    """One fresh owner observation as a policy row (open loop: the rig owner's observe + output readbacks)."""
    obs=ports['observation'].read_mode()
    f=obs.frequency
    frequency=dict(frequency_hz=f.frequency_hz,acquired_s=f.acquired_s,acquired_unix_s=getattr(f,'acquired_unix_s',None) or f.acquired_s)
    outputs={axis:ports[axis].readback() for axis in ('current','temperature','piezo')}
    return dict(observation=dict(frequency=frequency,mode_id=obs.mode_id,single_mode=obs.single_mode,
                                 metrics=dict(obs.metrics or {}),maintenance_required=obs.maintenance_required),
                outputs=outputs,acquired_s=f.acquired_s,acquired_unix_s=frequency['acquired_unix_s'])


def run_unlocked(ports, laser, p, *, clock, sleep, cancel, emit, publish=None, poll_s=.05):
    """Run the common current-window traversal through an open-loop owner.

    Dither measurement requires a running lock's timestamped fit. Disabled dither
    uses only selected fresh metrics; a hop holds and hands off to recovery.
    """
    from ldcon.core.contracts import DeviceError
    from ldcon.jobs.operating_point import lock_device
    from ldcon.core.interface_cards import CardCancelled
    publish=publish or (lambda value: None)
    p=parameters({'parameters':p},{})
    if p['measure_dither'] or p['mode']!='center_only':
        raise DeviceError('Standalone window search requires center_only and measure_dither=false')
    if not ports['loop'].is_open_loop():
        raise DeviceError('A lock holds the rig: run the midpoint action through the steady lock')
    fill_metric_limits(p,lock_device(laser)[2].get('control_settings'))
    axis=ports['current'].policy
    started=clock();first=_unlocked_row(ports)
    policy=WindowCenterPolicy(p,first['outputs'],axis,clock())
    start_current=first['outputs']['current']
    emit('window_unlocked_start',dict(start=first['outputs'],mode_id=first['observation']['mode_id'],
                                  boundary_limits=p['boundary_limits'],settle_s=p['settle_s'],max_step=axis.max_step))
    path=[]
    state=None
    while True:
        if cancel():
            state=dict(policy._result(warning='cancelled'),state='cancelled');break
        if clock()-started>p['timeout_s']:
            state=dict(policy._result(warning='window deadline'),state='unresolved');break
        try:row=_unlocked_row(ports)
        except MeasurementUnavailable as error:
            emit('window_measurement_wait',dict(reason=str(error)));sleep(poll_s);continue
        state=policy.tick(clock(),row)
        obs=row['observation']
        step=dict(state=state['state'],current=row['outputs']['current'],frequency_hz=obs['frequency']['frequency_hz'],
                  mode_id=obs['mode_id'],metrics={k:obs['metrics'].get(k[8:]) for k in p['observables'] if k.startswith('metrics.')},
                  action=state.get('action'),t_s=clock()-started)
        if state.get('action') or not path or path[-1]['state']!=step['state']:
            path.append(step);emit('window_unlocked_step',step)
        if state.get('recovery_required') or state.get('done'):break
        action=state.get('action')
        if action:
            ports['current'].move(action['value'],axis.update_interval_s)
            policy.acknowledge(action['value'],now=clock())
            continue
        sleep(poll_s)
    returned=None
    if state['state'] not in ('complete','recovery_required'):
        # unresolved / cancelled / deadline: bounded return to the start point (no midpoint is invented)
        returned=_return_unlocked(ports,axis,start_current,clock=clock,sleep=sleep)
    result=dict(status='succeeded' if state['state']=='complete' else 'recovery_required' if state['state']=='recovery_required' else state['state'],
                algorithm='current_window',measure_dither=False,center=state.get('center'),boundaries=state.get('boundaries'),
                start_current=start_current,final_current=ports['current'].readback(),hop=state.get('hop'),
                warning=state.get('warning'),returned_to_start=state.get('returned_to_start',returned),
                duration_s=clock()-started,steps=len([s for s in path if s.get('action')]),path=path,points=policy.points,
                boundary_limits=p['boundary_limits'])
    emit('window_unlocked_result',{k:v for k,v in result.items() if k not in ('path','points')})
    publish(result)
    if state['state']=='cancelled':
        raise CardCancelled('Window search cancelled (returned toward the start point)')
    return result


def _return_unlocked(ports, axis, target, *, clock, sleep, max_steps=200):
    steps=0
    while steps<max_steps:
        current=ports['current'].readback()
        if abs(current-target)<=max(axis.resolution/2,1e-10):return True
        value=current+max(-axis.max_step,min(axis.max_step,target-current))
        if axis.resolution:value=round(value/axis.resolution)*axis.resolution
        ports['current'].move(value,axis.update_interval_s);steps+=1
        sleep(axis.update_interval_s)
    return False

