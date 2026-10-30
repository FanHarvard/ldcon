"""In-lock one-shot passive tuning. It never owns or writes an actuator itself."""
import copy
import math
import statistics
from ldcon.core.device_conditions import finite
from ldcon.jobs.slow_control import validate_pid

DEFAULTS={'controller_card_id':'current-servo','window_s':8.,'min_samples':12,'timeout_s':60.,
          'gain_factors':[.5,.25],'minimum_improvement':.1,'activity_weight':.1,'save_settings':True,
          'strategy':'factor_reduction','adaptive_step':.2,'adaptive_min_step':.05,
          'adaptive_max_trials':4,'settle_s':5.,'minimum_effect_count':.1,'confidence_multiplier':2.}
TERMINAL={'succeeded','unchanged','failed','cancelled','rejected'}


def parameters(card,overrides):
    if not isinstance(overrides,dict) or not isinstance(card['parameters'],dict):raise ValueError('Online parameters must be mappings')
    if (set(card['parameters'])|set(overrides))-DEFAULTS.keys():raise ValueError('Unknown online tuning parameter')
    p={**copy.deepcopy(DEFAULTS),**card['parameters'],**overrides}
    if not isinstance(p['controller_card_id'],str) or not p['controller_card_id']:raise ValueError('Controller card is required')
    for key in ('window_s','timeout_s','minimum_improvement','activity_weight'):
        if finite(p[key],key)<=0:raise ValueError(key+' must be positive')
    if not 1<=p['window_s']<=60 or not 1<=p['timeout_s']<=300:raise ValueError('Online tuning window/deadline out of range')
    if not 0<p['minimum_improvement']<1:raise ValueError('Improvement must be between zero and one')
    if type(p['min_samples']) is not int or not 3<=p['min_samples']<=1000:raise ValueError('Invalid online sample count')
    factors=p['gain_factors']
    if not isinstance(factors,list) or not 1<=len(factors)<=4 or any(not 0<finite(x)<1 for x in factors):raise ValueError('Only bounded gain reduction trials are allowed')
    if type(p['save_settings']) is not bool:raise ValueError('save_settings must be boolean')
    if p['strategy'] not in ('factor_reduction','adaptive_coordinate'):raise ValueError('Unknown online tuning strategy')
    for key in ('adaptive_step','adaptive_min_step','settle_s','minimum_effect_count','confidence_multiplier'):finite(p[key],key)
    if not .01<=p['adaptive_min_step']<=p['adaptive_step']<=.5:raise ValueError('Adaptive coordinate step must be in [0.01, 0.5]')
    if not 1<=p['settle_s']<=60 or not .05<=p['minimum_effect_count']<=1 or not 2<=p['confidence_multiplier']<=5:raise ValueError('Adaptive evidence settings are outside supported bounds')
    if type(p['adaptive_max_trials']) is not int or not 1<=p['adaptive_max_trials']<=8:raise ValueError('Adaptive trial limit must be in [1, 8]')
    if p['strategy']=='adaptive_coordinate' and (p['window_s']<10 or p['min_samples']<12):raise ValueError('Adaptive comparison needs >=10 s and >=12 samples per window')
    return p


class OnlinePIDTuner:
    def __init__(self,receive,publish,emit,clock,cancel_request=lambda request:False):
        self.receive=receive;self.publish=publish;self.emit=emit;self.clock=clock;self.cancel_request=cancel_request
        self.active=None;self.rows=[];self.phase='idle';self.adaptive=None;self.finishing=None

    def _emit(self,kind,payload):
        if self.active:
            from ldcon.jobs.pi_algorithms import operation_for_strategy
            payload={**payload,'_context':dict(card=self.active['card_id'],
                operation=self.active.get('operation') or operation_for_strategy(self.active['parameters'].get('strategy')))}
        self.emit(kind,payload)

    def restart_window(self,reason):
        """Discard an incomplete comparison without reusing its samples."""
        if self.active and not self.finishing:
            if self.adaptive:
                self.adaptive.restart(reason)
                return
            self.rows=[];self.window_started=self.clock()
            self._status('running',reason=reason,comparison_restarted=True,pid=self.best_pid)

    def _status(self,status,**extra):
        value={'request_id':self.active['request_id'],'card_id':self.active['card_id'],'status':status,
               'phase':self.phase,'elapsed_s':self.clock()-self.started,**extra}
        self.publish(value);self._emit('online_tuning',value)

    @staticmethod
    def _ready(pid):
        return not hasattr(pid,'ready') or pid.ready()

    def _complete(self,status,pid,reason):
        if hasattr(pid,'finish'):pid.finish(status)
        self._status(status,reason=reason,accepted_trials=self.accepted,pid=pid.pid)
        self.active=None;self.phase='idle';self.rows=[];self.finishing=None;self.adaptive=None

    def _finish(self,status,pid,reason=None):
        if status not in ('succeeded','unchanged'):pid.retune(self.original_pid)
        if not self._ready(pid):
            self.finishing=(status,reason);self.phase='restoring';self.rows=[]
            self._status('running',reason=reason,restoring=True,pid=pid.pid)
            return
        self._complete(status,pid,reason)

    def _finalize(self,pid,actuator):
        if self.accepted and self.p['save_settings']:
            self._status('running',saving=True,pid=pid.pid)
            result=actuator.save_pid(self.best_pid);self._emit('device_settings_saved',result)
        self._finish('succeeded' if self.accepted else 'unchanged',pid,
                     ('Accepted a confirmed independent gain change' if self.adaptive else 'Accepted a conservative reduction') if self.accepted else 'No reliable improvement; original settings retained')

    def invalidate(self,reason,pid):
        if self.active:self._finish('failed',pid,reason)

    def _metrics(self,rows,actuator):
        errors=[r['error'] for _,r in rows];outputs=[r['output'] for _,r in rows]
        scale=actuator.config.get('resolution',0.) or max(1e-12,max(outputs)-min(outputs))
        activity=sum(abs(b-a) for a,b in zip(outputs,outputs[1:]))/max(1,len(outputs)-1)/scale
        rms=math.sqrt(statistics.mean(e*e for e in errors));mean=statistics.mean(errors);peak=max(abs(e) for e in errors)
        band=max(1e-12,rows[-1][1]['control_deadband'])
        slope=(errors[-1]-errors[0])/max(1e-12,rows[-1][0]-rows[0][0])
        return {'rms':rms,'mean':mean,'peak':peak,'activity':activity,'drift_per_s':slope,
                'score':rms/band+self.p['activity_weight']*activity}

    def _trial(self,pid):
        factor=self.p['gain_factors'][self.trial_index]
        trial=copy.deepcopy(self.original_pid)
        for key in ('kp','ki','kd'):trial[key]*=factor
        pid.retune(trial);self.phase='trial';self.rows=[];self.window_started=self.clock()
        self._status('running',factor=factor,pid=pid.pid)

    def _healthy(self,row):
        return not row['limited'] and abs(row['filtered_error'])<=2*max(row['control_deadband'],1e-12)

    def _comparison_inconclusive(self,metrics,row):
        return abs(metrics['drift_per_s'])*self.p['window_s']>row['control_deadband']

    def _accept(self,metrics,old,row):
        return (metrics['score']<old['score']*(1-self.p['minimum_improvement']) and
                metrics['rms']<=old['rms']*1.05 and
                abs(metrics['mean'])<=max(abs(old['mean'])*1.1,row['requested_tolerance']) and
                metrics['peak']<=max(old['peak']*1.1,row['control_deadband']))

    def tick(self,now,row,pid,actuator):
        request=self.receive()
        if request:
            if self.active:
                self._emit('online_tuning_rejected',{'request_id':request['request_id'],'reason':'Another trial is active'})
            else:
                self.active=request;self.p=request['parameters'];self.original_pid=copy.deepcopy(pid.pid);self.best_pid=copy.deepcopy(pid.pid)
                self.started=now;self.window_started=now;self.phase='baseline';self.rows=[];self.accepted=0;self.trial_index=0;self.finishing=None;self.finalizing=False
                try:
                    if self.p.get('strategy')=='adaptive_coordinate':
                        from ldcon.jobs.adaptive_pid import AdaptiveComparison
                        self.adaptive=AdaptiveComparison(self,pid,actuator)
                    elif hasattr(pid,'configure_strategy'):
                        pid.configure_strategy('factor_reduction')
                except ValueError as error:
                    self._status('rejected',reason=str(error));self.active=None;self.phase='idle';return
                self._status('running',pid=pid.pid)
        if not self.active:return
        if self.finishing:
            if self._ready(pid):self._complete(self.finishing[0],pid,self.finishing[1])
            return
        if self.cancel_request(self.active):self._finish('cancelled',pid,'Online tuning cancelled; lock continues');return
        if now-self.started>=self.p['timeout_s']:self._finish('failed',pid,'Online tuning deadline exceeded');return
        if self.adaptive:
            self.adaptive.tick(now,row,pid,actuator)
            return
        healthy=self._healthy(row)
        if not healthy:
            if self.phase=='trial':self._finish('failed',pid,'Lock lost eligibility or hit a constraint');return
            self.rows=[];self.window_started=now;return
        if not self._ready(pid):
            self.rows=[];self.window_started=now
            self._status('running',transitioning=True,pid=pid.pid)
            return
        if self.finalizing:self._finalize(pid,actuator);return
        self.rows.append((now,copy.deepcopy(row)))
        if now-self.window_started<self.p['window_s'] or len(self.rows)<self.p['min_samples']:return
        metrics=self._metrics(self.rows,actuator)
        if self._comparison_inconclusive(metrics,row):
            self._finish('unchanged' if self.phase=='baseline' else 'failed',pid,'Measurement drift makes comparison inconclusive');return
        if self.phase=='baseline':
            self.best_metrics=metrics;self._trial(pid);return
        old=self.best_metrics
        accepted=self._accept(metrics,old,row)
        self._emit('online_trial_result',{'accepted':accepted,'baseline':old,'trial':metrics,'pid':pid.pid})
        if accepted:self.best_metrics=metrics;self.best_pid=copy.deepcopy(pid.pid);self.accepted+=1
        else:pid.retune(self.best_pid)
        self.trial_index+=1
        if self.trial_index<len(self.p['gain_factors']):self._trial(pid);return
        pid.retune(self.best_pid)
        if self._ready(pid):self._finalize(pid,actuator)
        else:
            self.finalizing=True;self.phase='settling_best';self.rows=[]
            self._status('running',transitioning=True,pid=pid.pid)

    def close(self,pid):
        if self.active and hasattr(pid,'lock_ended'):
            pid.lock_ended();self._status('cancelled',reason='Stationary lock ended; actual DAC held')
            self.active=None;return
        if self.active:self._finish('cancelled',pid,'Lock stopped; online tuning stopped')
