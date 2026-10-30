"""Target-response binding using existing CAS target control and source history.

This adapter runs outside the lock's queue: target changes are consumed by that
lock, so waiting inside its own operation callback would deadlock. No new lease
or direct controller write is created. Full-rate history is queried over the
actual request/completion interval; polling snapshots are not called full-rate.
"""
import copy
import time
from pathlib import Path

from ldcon.core.contracts import DeviceError, MeasurementUnavailable
from ldcon.jobs import lock_target
from ldcon.jobs.change_measure import finite
from ldcon.jobs.response_record import validated_history
from ldcon.jobs.target_response import StudyContext


class TargetMeasurementAdapter:
    def __init__(self,directory,laser,card,*,clock,sleep,cancel,emit,wall_clock=time.time,
                 api=None,history=None,target_runner=None,context=None):
        from ldcon.jobs.dashboard_api import Dashboard,state_dir_of
        from ldcon.jobs.wavelength_history import configured_reader
        self.directory,self.laser,self.card=Path(directory),laser,card
        self.clock,self.sleep,self.cancel,self.emit,self.wall_clock=clock,sleep,cancel,emit,wall_clock
        self.api=api or Dashboard.from_job_directory(self.directory)
        self.history=history or configured_reader(state_dir_of(self.directory))
        self.context=context or StudyContext(directory,laser,self.api,clock,cancel)
        self.target_runner=target_runner or lock_target.run_lock_target
        self.started_unix_s=wall_clock()
        self.requested_unix_s=None

    def check(self):self.context.check()

    def preflight(self,changes,mode,measurements,planned=None):
        self.context.check(force=True)
        if mode!='ordered' or len(changes)>1 or any(c['parameter']!='target_frequency_hz' for c in changes):
            raise DeviceError('Target changes use their own ordered owner transaction; split other parameters into batch steps')
        if measurements.get('dither') or measurements.get('optical_quality'):
            raise DeviceError('This full-rate target binding provides wavelength history only')
        current=self.context.document['target']['frequency_hz']
        expected=(planned or {}).get('target_frequency_hz',current)
        target=changes[0].get('value',expected+changes[0].get('offset',0.)) if changes else expected
        document=copy.deepcopy(self.context.document)
        document['target']['frequency_hz']=expected
        lock_target.check_change(document,target,enforce_step=False)
        recent=validated_history(self.history.read(self.wall_clock()-5.,self.wall_clock()))
        if recent.get('integrity_errors') or not recent.get('samples'):
            raise DeviceError('Recent source wavelength history is unavailable before target change')
        return dict(binding='target_owner',changes=[dict(parameter='target_frequency_hz',value=target)] if changes else [],
                    checkpoint=dict(target_frequency_hz=expected),planned=dict(target_frequency_hz=target),
                    mode=mode,measurements=measurements,revision_at_admission=self.context.expected_revision)

    def apply(self,plan,sample):
        self.context.check(force=True)
        current=self.context.document['target']['frequency_hz']
        if current!=plan['checkpoint']['target_frequency_hz']:
            raise DeviceError('Target changed since preflight')
        target=plan['changes'][0]['value']
        self.requested_unix_s=self.wall_clock()
        def cancelled():
            self.context.check(force=True)
            return False
        command=lock_target.validate(dict(target_frequency_hz=target,verify_samples=1,tolerance_hz=None,
            sample_interval_s=.1,timeout_s=plan.get('runtime_timeout_s',300.),queue_steps=True))
        result=self.target_runner(self.directory,copy.deepcopy(self.context.document),self.card,command,
            clock=self.clock,sleep=self.sleep,cancel=cancelled,emit=self.emit,api=self.context)
        if not result.get('locked'):
            raise DeviceError('Target saved without active lock; no physical step-response claimed')
        self.context.pending=False
        self.context.check(force=True)
        actual=self.context.document['target']['frequency_hz']
        if actual!=target:
            raise DeviceError('Target readback differs after owner acknowledgement')
        return dict(status='applied',readback=dict(target_frequency_hz=actual),owner_result=result,
                    started_unix_s=self.requested_unix_s,completed_unix_s=self.wall_clock(),
                    timestamp_kind='host_target_request_and_owner_acknowledgement')

    def read(self,measurements):
        if not measurements.get('wavelength'):return {}
        history=validated_history(self.history.read(self.wall_clock()-2.,self.wall_clock()))
        if history.get('integrity_errors') or not history.get('samples'):
            raise MeasurementUnavailable('Source wavelength history is not available yet')
        return {'wavelength':measurement_block(history['samples'][-1])}

    def complete_measurement(self,result,*,check=None,remaining_s=None):
        """Replace polling wavelength rows with every original history sample.

        A slow history backend may still return an incomplete tail: preserve its
        evidence and let the common coverage check mark measurement_incomplete.
        """
        if not result['parameters']['measurements'].get('wavelength'):return
        end=result['measurement_ended_unix_s']
        start=result['measurement_started_unix_s']
        begin=result.get('baseline_started_unix_s',start-result['parameters']['baseline_s'])
        request=self.requested_unix_s if result['parameters']['changes'] else result['application_completed_unix_s']
        from ldcon.jobs.response_record import acquire
        captured=acquire(self.history,begin,request,end,
            data_wait_s=min(result['parameters']['history_wait_s'],remaining_s) if remaining_s is not None else result['parameters']['history_wait_s'],
            max_gap_s=result['parameters']['max_sample_gap_s'],clock=self.clock,wait=self.sleep,
            check=check or (lambda:self.context.check(force=True)))
        history=captured['history']
        result['history_attempts']=captured['attempts']
        result['source_history']=history
        result['acquisition_kind']='source_history; original acquisition timestamps, no polling-rate claim'
        rows=[]
        for sample in history.get('samples',[]):
            block=measurement_block(sample);stamp=block['acquired_unix_s']
            if not block['valid'] or not begin<=stamp<=end:continue
            phase=('baseline' if stamp<request else
                   'transition' if stamp<result['application_completed_unix_s'] else
                   'wait' if stamp<start else 'measurement')
            row=dict(phase=phase,received_unix_s=self.wall_clock(),measurements={'wavelength':block})
            rows.append(row)
        # Errors in the source data are never repaired by sparse polling rows.
        if history.get('integrity_errors'):
            rows=[]
        rows.sort(key=lambda r:r['measurements']['wavelength']['acquired_unix_s'])
        seen=set();unique=[]
        for row in rows:
            stamp=row['measurements']['wavelength']['acquired_unix_s']
            if stamp not in seen:unique.append(row);seen.add(stamp)
        result['samples'][:]=unique
        self.emit('change_measure_history',dict(samples=unique,source_history=history))


def measurement_block(sample):
    stamp=sample.get('time_unix_s',sample.get('unix_s',sample.get('acquired_unix_s')))
    frequency=sample.get('frequency_hz')
    wavelength=sample.get('wavelength_nm')
    if frequency is None and finite(wavelength) and wavelength>0:frequency=299792458e9/wavelength
    if wavelength is None and finite(frequency) and frequency>0:wavelength=299792458e9/frequency
    return dict(acquired_unix_s=stamp,valid=finite(stamp) and finite(frequency) and frequency>0
                and finite(wavelength) and wavelength>0,
                values=dict(frequency_hz=frequency,wavelength_nm=wavelength))
