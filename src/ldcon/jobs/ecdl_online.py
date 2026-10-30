"""One worker inbox for native PID trials and locked current studies."""
import copy
import time
from ldcon.core.contracts import MeasurementUnavailable
from ldcon.owners.ecdl_locked_study import StudyInbox
from ldcon.jobs.online_pid import OnlinePIDTuner
from ldcon.jobs.pi_algorithms import ALGORITHMS as PI_ALGORITHMS, parameters
from ldcon.core.records import atomic_json,read_json


class NativePIDAdapter:
    def __init__(self,lock,state):
        self.lock=lock;self.state=state;self.pid=copy.deepcopy(state['pid']);self.config=state['config']
    @property
    def uncertain_output(self):return self.lock.uncertain_output
    def ready(self):return self.state['ready']
    def retune(self,pid):
        self.state=self.lock.pid_target(pid);self.pid=copy.deepcopy(pid)
    def save_pid(self,pid):
        result=self.lock.pid_save(pid);self.state=result;self.config=result['config'];return result
    def finish(self,status):self.state=self.lock.pid_finish(status);self.config=self.state['config']
    def lock_ended(self):pass # the main finally holds the actual DAC and ends the owner trial


class ECDLOnlineBridge(StudyInbox):
    def __init__(self,*args,steady=None):
        super().__init__(*args);self.tuner=None;self.pid=None;self.last_valid=None;self.gap_started=None
        self.window=None;self.steady=steady or {};self.last_lock=None
        from ldcon.owners.ecdl_hot_control import HotControlInbox
        self.hot=HotControlInbox(self.directory,self.card_id,self.revision,self.emit)
        from ldcon.jobs.window_control import WindowControl
        self.window_control=WindowControl(self)

    @property
    def freeze_recovery_reference(self):
        return bool(self.hot.busy or self.active or self.window and self.window.status=='running' and self.window.freeze_reference)

    @property
    def recovery_requested(self):
        return bool(self.window and self.window.needs_recovery)

    def before_recovery(self):
        self.window_control.invalidate('Original-mode recovery')
        self.hot.interrupt()
        if self.window and self.window.status=='running':self.window.before_recovery()

    def after_recovery(self):
        if self.window and self.window.status=='running':self.window.after_recovery()

    def publish(self,value):
        control=getattr(self,'window_control',None)
        if (control and control.active_id==value.get('request_id') and self.last_lock
                and self.last_lock.uncertain_output and value.get('status') in ('cancelled','failed','succeeded','rejected')):
            value={**value,'status':'unknown','reason':'Uncertain owner output; inspect before any further action'}
            control.queue.complete(control.active_id,'unknown',result=value,reason=value['reason'])
            control.active_id=None
        if self.pid:
            self.revision=self.pid.state['device_revision']
            atomic_json(self.directory/'controller-live.json',dict(controller_card_id=self.card_id,
                settings_revision=self.revision,config=self.pid.config))
        atomic_json(self.directory/'online-status.json',value)
        if hasattr(self,'window_control'):self.window_control.finish(value)

    def poll(self,lock):
        self.last_lock=lock
        self.window_control.sync()
        self.hot.revision=self.revision
        self.hot.poll(lock,busy=bool(self.active or self.tuner and self.tuner.active or self.window and self.window.status=='running'))
        if self.hot.busy:return
        path=self.directory/'online-request.json'
        if path.exists():
            request=read_json(path)
            matching=next((c for c in self.deck['cards'] if c['id']==request.get('card_id')),None)
            if matching and matching['operation']=='tune.ecdl_window_center':
                path.unlink()
                try:
                    if (self.active or self.tuner and self.tuner.active or self.window and self.window.status=='running'
                        or request.get('job_id')!=self.directory.name or request.get('settings_revision')!=self.revision
                        or request.get('controller_card_id')!=self.card_id or time.time()>request.get('expires_unix_s',0)):
                        raise ValueError('Inactive, expired, busy or mismatched window request')
                    from ldcon.jobs.window_center import parameters as window_parameters
                    from ldcon.jobs.ecdl_window_center import WindowCenterSession
                    p=window_parameters(matching,request['parameters'])
                    seed=lock.session.device['control_settings'].get('window_center_seed')
                    self.window=WindowCenterSession(request,p,lock,seed,self.emit,self.publish,self.steady)
                    self.publish(dict(request_id=request['request_id'],card_id=request['card_id'],status='running'))
                except Exception as error:
                    self.publish(dict(request_id=request.get('request_id'),card_id=request.get('card_id'),status='rejected',reason=str(error)))
                    if lock.uncertain_output:raise
            elif self.window and self.window.status=='running':
                path.unlink();self.emit('online_tuning_rejected',dict(request_id=request.get('request_id'),reason='Window operation is active'))
            elif matching and matching['operation'] in PI_ALGORITHMS:
                path.unlink()
                began=False
                try:
                    if (self.active or self.tuner and self.tuner.active or request.get('job_id')!=self.directory.name
                            or request.get('settings_revision')!=self.revision or request.get('controller_card_id')!=self.card_id
                            or time.time()>request.get('expires_unix_s',0)):
                        raise ValueError('Inactive, expired, busy or mismatched native PID request')
                    p=parameters(matching,request['parameters'])
                    request['operation']=matching['operation']
                    if p['controller_card_id']!=self.card_id:raise ValueError('Wrong stationary controller')
                    if p['strategy']!='factor_reduction':
                        raise ValueError('This ECDL adapter has no reviewed independent-gain candidate proof')
                    # Owners up to broker v5.3 validate only the factor-reduction keys and reject
                    # the newer adaptive defaults; the owner trial reads gain_factors only.
                    legacy=('controller_card_id','window_s','min_samples','timeout_s','gain_factors',
                            'minimum_improvement','activity_weight','save_settings')
                    state=lock.pid_begin({k:p[k] for k in legacy});began=True
                    self.pid=NativePIDAdapter(lock,state);pending=[{**request,'parameters':p}]
                    self.tuner=OnlinePIDTuner(lambda:pending.pop() if pending else None,self.publish,
                        self.emit,time.monotonic,lambda r:read_json(self.directory/'online-cancel.json',{}).get('request_id')==r['request_id'])
                    self.last_valid=None;self.gap_started=None
                    self.publish(dict(request_id=request['request_id'],card_id=request['card_id'],status='queued'))
                except Exception as error:
                    if began:
                        lock.pid_finish('cancelled')
                        raise
                    self.publish(dict(request_id=request.get('request_id'),card_id=request.get('card_id'),status='rejected',reason=str(error)))
                    if getattr(lock,'uncertain_output',False):raise
            elif self.tuner and self.tuner.active:
                path.unlink();self.emit('online_tuning_rejected',dict(request_id=request.get('request_id'),reason='Native PID tuning is active'))
        super().poll(lock)
        if self.window and self.window.status=='running':
            cancel=read_json(self.directory/'online-cancel.json',{}).get('request_id')==self.window.request['request_id']
            try:queued=self.window_control.queue.get(self.window.request['request_id'])
            except KeyError:queued=None
            cancel=cancel or bool(queued and queued.get('cancel_requested'))
            self.window.poll(lock,cancel)

    def cycle(self,row,tolerance):
        self.window_control.cycle(row,self.last_lock,busy=bool(row.get('pid_tuning') or self.hot.busy or self.active
            or self.tuner and self.tuner.active or self.window and self.window.status=='running' or self.recovery_requested))
        if not self.tuner or not row.get('pid_tuning'):return
        self.pid.state=row['pid_tuning']
        acquired=row['acquired_s']
        if self.last_valid is not None and acquired-self.last_valid>self.pid.config['max_sample_gap_s']:
            self.tuner.restart_window('Optical comparison gap exceeded device bound; reacquiring complete window')
        self.last_valid=acquired;self.gap_started=None
        data=dict(error=row['error_hz'],filtered_error=row['error_hz'],output=row['outputs']['piezo'],
            limited=not row['pid_tuning']['eligible'],control_deadband=tolerance,requested_tolerance=tolerance)
        self.tuner.tick(time.monotonic(),data,self.pid,self.pid)

    def invalidate(self,reason):
        self.window_control.invalidate(reason)
        if not self.tuner:return
        now=time.monotonic()
        if self.gap_started is None:self.gap_started=now
        self.emit('online_measurement_wait',dict(reason=reason,elapsed_s=now-self.gap_started))

    def close(self):
        try:
            self.hot.interrupt()
            if self.tuner:self.tuner.close(self.pid)
            if self.window and self.window.status=='running' and self.last_lock:
                self.window.finish(self.last_lock,'cancelled','stationary worker exiting')
        finally:
            self.window_control.close()
