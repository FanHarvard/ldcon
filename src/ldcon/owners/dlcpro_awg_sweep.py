"""One external edge, split Fine inputs, bounded ADC timing and acquired WSU data.

Software PI defaults to hold; optional plateau tracking keeps TEC held. The target
coordinate is observed from Fine2, not substituted for an acquisition timestamp.
No physical mutation is replayed after an uncertain outcome.
"""
from copy import deepcopy
from dataclasses import asdict
import math
import uuid
import time

from ldcon.owners.dlcpro_awg_timing import observe_plateau, source_coverage

from ldcon.core.contracts import DeviceError, MeasurementUnavailable
from ldcon.drivers.dlcpro_analog import inspect, read_input, INPUTS
from ldcon.owners.dlcpro_awg_plan import configuration, compile_record
from ldcon.owners.dlcpro_awg_port import Port, PreparationInterrupted


class Sweep:
    def __init__(self, owner, config, authority, *, port_factory=Port, sleep=time.sleep):
        self.o, self.config, self.authority = owner, configuration(config), authority
        self.factory = port_factory
        self.sleep = sleep
        self.session = None
        self.port = None
        self.last = None
        self.tracking = None

    def active(self):
        return self.session is not None

    def view(self):
        s = deepcopy(self.session or self.last)
        if s:
            for key in ('token', 'adc', 'samples', 'arc_before', 'record', 'prearm_adc', 'tracking_samples'):
                s.pop(key, None)
        return s

    def _arc(self, field, value):
        o = self.o
        if field == 'signal':
            # The scalar reader returns numeric enums as floats, but DLC FW
            # 1.9 requires an integer Scheme atom for this selector.
            if type(value) not in (int,float) or not math.isfinite(value) or int(value) != value:
                raise ValueError('ARC signal selector must be an integer')
            value = int(value)
        return o.instrument._write(o.channel, o.lease,
            f'laser{o.channel}:dl:pc:external-input:{field}', value, 1e-9 if field == 'factor' else 0.)

    def _read(self):
        return read_input(self.o.instrument.driver, self.config['target_input'], clock=self.o.clock)

    def reserve(self, request, target_revision, *, token=None):
        """Reserve a held-output epoch; no physical I/O or invented heartbeat."""
        o = self.o
        o._healthy()
        if self.active() or o.mode != 'lock' or o.target_revision != target_revision:
            raise ValueError('A healthy lock with an unchanged target and no sweep is required')
        target_change = getattr(o, 'target_changes', None)
        if target_change and target_change.active():
            raise ValueError('Wait for the foreground target operation before arming')
        o.pi_trial.preempt('foreground_hardware_sweep')
        self.tracking = None
        self.session = dict(request=deepcopy(request), token=token or uuid.uuid4().hex,
            state='preparing', ready=False, armed=False, simulated=False,
            origin_target_hz=o.target, origin_target_revision=o.target_revision,
            started_s=o.clock(), renewed_s=o.clock(), last_adc_s=None,
            trigger_bounds_s=None, trigger_s=None, adc=[], samples=[],
            level_observations=[0]*len(request['plan']['levels']),
            timing_meta=dict(acquisition_timestamp_available=False,
                clock='host monotonic seconds',
                trigger_method='inferred from first distinguishable Fine input plateau',
                trigger_bounds_are_host_observation_brackets=True,
                adc_internal_delay_bound_s=None, timing_accuracy_verified=False),
            started_unix_s=time.time(), restored=False, playback_completed=False,
            physical_changed=False, cancel_requested=False, resume_allowed=True, prearm_adc=[])
        o.mode = 'awg_sweep'
        return dict(token=self.session['token'], **self.view())

    def _checkpoint(self):
        """Run only at known I/O boundaries, never retry an interrupted write."""
        with self.o.mutex:
            o, s = self.o, self.session
            if not s:
                raise PreparationInterrupted('Preparation epoch ended')
            if s['cancel_requested']:
                raise PreparationInterrupted('Caller cancelled preparation')
            if o.fault or o.instrument.channels[o.channel]['fault']:
                raise PreparationInterrupted('Owner fault during preparation')
            if o.mode != 'awg_sweep' or o.target_revision != s['origin_target_revision']:
                raise PreparationInterrupted('Controller context changed during preparation')
            if o.clock()-o.last_client > o.policy['client_timeout_s']:
                s['resume_allowed'] = False
                raise PreparationInterrupted('Main controller heartbeat expired during preparation')
            if o.clock()-s['renewed_s'] > self.config['watchdog_s']:
                raise PreparationInterrupted('Sweep heartbeat expired during preparation')
            if o.clock()-s['started_s'] > self.config['arm_timeout_s']:
                raise PreparationInterrupted('Preparation exceeded the arm deadline')

    def request_cancel(self, *, resume=True):
        if self.session:
            self.session['cancel_requested'] = True
            self.session['resume_allowed'] &= resume
        return self.view()

    def begin(self, request, target_revision, *, pzt_scale=1., token=None):
        # Synchronous helper for direct callers; protocol uses reserve + prepare.
        with self.o.mutex:
            self.reserve(request, target_revision, token=token)
        return self.prepare(pzt_scale=pzt_scale)

    def prepare(self, *, pzt_scale=1.):
        """One preparation executor owns the port; never hold owner.mutex on I/O."""
        o, p, s = self.o, self.config, self.session
        physical_started = False
        try:
            self._checkpoint()
            sample = o.read_control()
            if abs(sample.frequency_hz-o.target) > o.control['capture_hz']:
                raise MeasurementUnavailable('Optical frequency is outside capture before arming')
            snapshot = o.snapshot()
            record, proof = compile_record(p, s['request'], snapshot, o.policy, pzt_scale=pzt_scale)
            if p.get('track_plateaus', False):
                from ldcon.owners.dlcpro_awg_tracking import Tracking
                self.tracking = Tracking(o, p, s, snapshot)
                proof = self.tracking.admit(proof)
            analog = inspect(o.instrument.driver, o.channel, clock=o.clock)
            if any(v['enabled'] is not False for v in analog['routes'].values()):
                raise ValueError('Existing analog output modulation must be disabled before borrowing Fine inputs')
            for value in analog['inputs'].values():
                if not math.isfinite(value['value_v']) or abs(value['value_v']-p['adc_zero_v']) > p['adc_tolerance_v']:
                    raise ValueError('Fine input is not at the commissioned zero before arming')
            self._checkpoint()
            port = self.factory(p['awg'], self.authority)
            self.port = port
            with o.mutex:
                s.update(arc_before=analog['routes']['pc'], proof=proof)
            self._checkpoint()
            port.checkpoint = self._checkpoint
            self._collect()
            physical_started = True
            s['physical_changed'] = None  # In-flight state until a verified receipt.
            s['arm_receipt'] = port.arm(record, p['awg']['role'], s['request']['request_id'][:12])
            s['initial_arm_receipt'] = deepcopy(s['arm_receipt'])
            self._checkpoint()
            from ldcon.owners.dlcpro_awg_zero import observe, at_zero, corrected_proof
            def idle_window(phase):
                try:
                    return observe(self._read, p, self._checkpoint, self.sleep,
                                   s['prearm_adc'], phase=phase)
                except PreparationInterrupted:
                    raise
                except Exception as error:
                    self.finish('failed', str(error))
                    raise
            window = idle_window('after_initial_arm')
            if not at_zero(window, p):
                correction = (p['adc_zero_v']-window['value_v'])/p['adc_v_per_awg_v']
                s['zero_correction'] = dict(state='measured', correction_v=correction,
                    measured_idle=window, configured_zero_v=p['adc_zero_v'],
                    adc_v_per_awg_v=p['adc_v_per_awg_v'])
                try:
                    admitted = corrected_proof(p, proof, o.policy, correction)
                    if self.tracking:
                        admitted = self.tracking.admit(admitted)
                except Exception as error:
                    s['zero_correction'].update(state='rejected_before_write', reason=str(error))
                    self.finish('failed', str(error))
                    raise
                s['zero_correction'].update(state='proposed', proof=admitted)
                self._checkpoint()
                # Outside the read-only failure handler: a lost correction write
                # must fault/retain, never become permission to replay/cleanup.
                receipt = port.rearm_offset(correction)
                s['zero_correction'].update(state='rearmed', receipt=receipt)
                s['arm_receipt'] = receipt
                s['proof'] = proof = admitted
                window = idle_window('after_zero_correction')
                s['zero_correction']['after_rearm_idle'] = window
                if not at_zero(window, p):
                    s['zero_correction']['state'] = 'rejected_after_rearm'
                    self.finish('failed','Corrected input is not idle; no ARC enable or second correction')
                    raise ValueError('Corrected input is not idle; no ARC enable or second correction')
                s['zero_correction'].update(state='verified', verified_idle=window)
            try:
                adc = window['samples'][-1]
                sample = o.read_control()
                if abs(sample.frequency_hz-o.target) > o.control['capture_hz']:
                    raise MeasurementUnavailable('Optical frequency left capture during preparation')
                self._checkpoint()
            except Exception as error:
                self.finish('failed', str(error))
                raise
            for field, value in (('signal', INPUTS[p['pzt_input']][1]),
                                 ('factor', proof['pzt_factor_v_per_v']), ('enabled', True)):
                self._checkpoint()
                self._arc(field, value)
            if self.tracking:
                self.tracking.start(sample)
            with o.mutex:
                self._checkpoint()
                s.update(state='ready', ready=True, armed=True, last_adc_s=o.clock(), physical_changed=True)
                s['adc'].append(adc)
            o.audit(dict(event='awg_armed', request_id=s['request']['request_id'], proof=proof))
            return dict(token=s['token'], **self.view())
        except PreparationInterrupted as error:
            if self.session:
                if self.port and getattr(self.port, 'changed', physical_started):
                    self.finish('cancelled', str(error))
                else:
                    self._unchanged('cancelled', str(error))
            raise
        except BaseException as error:
            if self.session:
                if physical_started and getattr(self.port, 'changed', True):
                    self._uncertain('AWG/ARC preparation did not complete; inspect before recovery')
                else:
                    self._unchanged('failed', 'Preparation rejected before physical change: '+str(error))
            raise

    def _unchanged(self, verdict, reason):
        """A failed read-only preparation needs no hardware restoration."""
        o, s = self.o, self.session
        if self.port:
            self.port.close()
            self.port = None
        with o.mutex:
            resume = (s['resume_allowed'] and not o.fault
                and not o.instrument.channels[o.channel]['fault']
                and o.clock()-o.last_client <= o.policy['client_timeout_s']
                and o.target_revision == s['origin_target_revision'])
            # The next PI tick consumes a new acquisition; do not integrate the
            # preparation hold interval or replay its previous proposal.
            o.last_acquisition = o.clock()
            o.last_tec = o.clock()
            o.mode = 'lock' if resume else 'idle'
            s.update(state='failed' if verdict=='failed' else 'complete', ready=False,
                armed=False, restored=True, outputs_uncertain=False, verdict=verdict,
                reason=reason, completed_s=o.clock(), pi_resumed=resume, physical_changed=False)
            self.last, self.session = s, None

    def _collect(self):
        s=self.session
        if not s or not hasattr(self.o.observation,'samples_after'):
            return
        after=s['samples'][-1]['acquired_s'] if s['samples'] else s['started_s']
        try:s['samples'].extend(asdict(row) for row in self.o.observation.samples_after(after))
        except Exception as error:s.setdefault('source_read_errors',[]).append(type(error).__name__)

    def heartbeat(self, token):
        if not self.session or token != self.session['token']:
            raise ValueError('Sweep identity is no longer active')
        self.session['renewed_s'] = self.o.clock()
        return self.view()

    def _uncertain(self, reason):
        o = self.o
        if self.tracking:
            self.tracking.finish()
            self.tracking.s['tracking']['qualified'] = False
        o.fault = reason
        o.mode = 'idle'
        if self.session:
            self.session.update(state='failed', ready=False, armed=False,
                restored=False, outputs_uncertain=True, error=reason)
            self.last = self.session
            self.session = None
        # Deliberately retain port and physical lease, no speculative retry.
        o.audit(dict(event='owner_fault', reason=reason))

    def tick(self):
        if not self.session:
            return
        o, p, s = self.o, self.config, self.session
        if s['state'] == 'preparing':
            return
        now = o.clock()
        if s.get('cancel_requested'):
            self.finish('cancelled', 'Caller cancelled this sweep', resume=s['resume_allowed'])
            return
        if o.instrument.channels[o.channel]['fault']:
            self._uncertain('DLC channel fault during playback; retain unverified output state')
            return
        if o.fault:
            self.finish('failed', 'Owner fault during playback', resume=False)
            return
        if now-s['renewed_s'] > p['watchdog_s'] or now-o.last_client > o.policy['client_timeout_s']:
            self.finish('cancelled', 'sweep/controller heartbeat expired', resume=now-o.last_client <= o.policy['client_timeout_s'])
            return
        self._collect()
        if now-s['last_adc_s'] < p['adc_max_interval_s']/2:
            return
        try:
            adc = self._read()
        except Exception as error:
            # Read-only failures do not imply an uncertain output write. End the
            # known armed waveform through its normal cleanup, without retries.
            self.finish('failed', 'ADC read unavailable: '+type(error).__name__)
            return
        stamp = adc['completed_s']
        previous = s['adc'][-1]
        budget = self.tracking.io_budget if self.tracking else p['adc_max_interval_s']
        gap = stamp-previous['started_s']
        if stamp-adc['started_s'] > p['adc_max_interval_s']:
            self.finish('failed', 'ADC read bracket exceeded the original freshness budget')
            return
        if gap > budget:
            self.finish('failed', 'ADC observation gap exceeded the commissioned timing budget')
            return
        if gap > p['adc_max_interval_s']:
            s.setdefault('tracking_observation_gaps', []).append(dict(
                left_s=previous['started_s'], right_s=stamp, gap_s=gap))
        gain=p['adc_v_per_awg_v']
        if not math.isfinite(adc['value_v']) or not p['awg']['min_v']*gain-p['adc_tolerance_v'] <= adc['value_v']-p['adc_zero_v'] <= p['awg']['max_v']*gain+p['adc_tolerance_v']:
            self.finish('failed', 'ADC input left the approved waveform envelope')
            return
        s['adc'].append(adc);s['last_adc_s']=stamp
        plan=s['request']['plan'];levels=plan['levels']
        if s['trigger_s'] is None:
            if abs(adc['value_v']-p['adc_zero_v']) > 4*p['adc_tolerance_v']:
                first=next(i for i,v in enumerate(levels) if abs(v/p['frequency_hz_per_input_v'])*gain>4*p['adc_tolerance_v'])
                expected=levels[first]/p['frequency_hz_per_input_v']*gain
                if abs(adc['value_v']-p['adc_zero_v']-expected)>p['adc_tolerance_v']:
                    self.finish('failed','First observed plateau differs from the armed record');return
                offset=plan['lead_s']+first*plan['dwell_s']
                bounds=[previous['started_s']-offset,adc['completed_s']-offset]
                s.update(trigger_bounds_s=bounds,trigger_s=sum(bounds)/2,state='playing',ready=False)
                s['timing_meta']['estimated_trigger_s']=s['trigger_s']
                s['timing_meta']['host_bracket_width_s']=bounds[1]-bounds[0]
                # Leading zero levels can be evaluated retrospectively, but
                # this remains observation-based timing, not a hardware ACK.
                for old in s['adc']:
                    if not observe_plateau(s,p,old):
                        self.finish('failed','Observed plateau differs from the armed record');return
            elif now-s['started_s']>p['arm_timeout_s']:
                self.finish('cancelled','No external trigger observed before arm deadline');return
        else:
            if not observe_plateau(s,p,adc):
                self.finish('failed','Observed plateau differs from the armed record');return
            elapsed=stamp-s['trigger_s']
            s['instantaneous_offset_hz']=(adc['value_v']-p['adc_zero_v'])/gain*p['frequency_hz_per_input_v']
            if elapsed>=plan['duration_s']+(s['trigger_bounds_s'][1]-s['trigger_bounds_s'][0]):
                if abs(adc['value_v']-p['adc_zero_v'])>p['adc_tolerance_v']:
                    self.finish('failed','Record end is not at zero input');return
                if not all(s['level_observations']):
                    self.finish('failed','At least one record plateau has no interior observation');return
                s['playback_completed']=True
                self.finish('succeeded','Every plateau observed and record ended at the admitted idle input')
        if self.session and self.tracking:
            from ldcon.owners.dlcpro_awg_tracking import TrackingStop
            try:
                self.tracking.tick(adc)
            except TrackingStop as error:
                self.finish('failed', str(error), resume=False)
            except BaseException:
                self._uncertain('Tracking output outcome requires inspection; no automatic cleanup')
                raise

    def _resume_frequency(self):
        # This callback is read-only and is called only after verified physical
        # restoration. A broken source cannot turn those writes into unknowns.
        try:
            return self.o.read_control()
        except MeasurementUnavailable:
            raise
        except Exception as error:
            raise MeasurementUnavailable('Post-cleanup frequency unavailable: '+
                type(error).__name__+': '+str(error)) from error

    def finish(self, verdict, reason, *, resume=True):
        if not self.session:
            return self.view()
        o,s=self.o,self.session
        if self.tracking:
            self.tracking.finish()
        s.update(state='restoring',ready=False,stop_verdict=verdict,stop_reason=reason)
        try:
            self.port.checkpoint = lambda: None
            self.port.restore()
            s['awg_restored'] = True
            self._arc('enabled',False)
            old=s['arc_before']
            self._arc('signal',old['signal']);self._arc('factor',old['factor'])
            if old['enabled'] is not False:
                raise ValueError('Cannot restore an unadmitted analog enable')
            s['arc_restored'] = True
            s['physical_changed'] = True
            s['restored'] = True
            self._collect()
            if self.tracking:
                self.tracking.finish()
            s['source_coverage']=source_coverage(s)
            if o.fault or o.instrument.channels[o.channel]['fault']:
                raise MeasurementUnavailable('Faulted owner must not resume PI')
            sample=self._resume_frequency()
            from ldcon.owners.dlcpro_awg_return import restore_if_needed
            sample=restore_if_needed(self, sample)
            if o.target_revision!=s['origin_target_revision'] or abs(sample.frequency_hz-o.target)>o.control['capture_hz']:
                raise MeasurementUnavailable('Sweep ended outside the original PI capture context')
            self.port.close();self.port=None
            with o.mutex:
                # Recheck after all slow cleanup/lease operations. A stop that
                # arrived during cleanup must never re-enable software PI.
                resume = (resume and s.get('resume_allowed', True) and not o.fault
                    and not o.instrument.channels[o.channel]['fault']
                    and o.clock()-o.last_client <= o.policy['client_timeout_s']
                    and o.target_revision == s['origin_target_revision'])
                sample = self._resume_frequency()
                if abs(sample.frequency_hz-o.target)>o.control['capture_hz']:
                    raise MeasurementUnavailable('Fresh post-cleanup frequency is outside capture')
                held=o.instrument.confirmed_outputs[o.channel]['piezo']
                error=(o.target-sample.frequency_hz)/o.policy['axes']['piezo']['frequency_hz_per_unit']
                o.base=held-o.control['kp']*error-o.integral
                o.last_acquisition=sample.acquired_s;o.last_valid=o.clock();o.last_tec=o.clock()
                o.mode='lock' if resume else 'idle'
                s.update(state='complete',armed=False,restored=True,verdict=verdict,reason=reason,
                         completed_s=o.clock(),outputs_uncertain=False,pi_resumed=resume)
        except MeasurementUnavailable as error:
            o.mode='idle'
            if self.port:
                self.port.close();self.port=None
            s.update(state='failed',armed=False,restored=True,verdict='failed',
                     reason=reason+'; PI not resumed: '+str(error),pi_resume_reason=str(error),
                     completed_s=o.clock(),outputs_uncertain=False,pi_resumed=False)
        except BaseException as error:
            s['cleanup_error'] = dict(type=type(error).__name__,reason=str(error))
            self._uncertain('AWG/ARC restoration uncertain; output state requires inspection')
            raise
        if self.session:
            self.last=s;self.session=None
            o.audit(dict(event='awg_complete',request_id=s['request']['request_id'],
                verdict=s['verdict'],restored=s['restored'],reason=s['reason'],
                stop_verdict=s['stop_verdict'],stop_reason=s['stop_reason'],
                pi_resume_reason=s.get('pi_resume_reason')))
        return self.view()
