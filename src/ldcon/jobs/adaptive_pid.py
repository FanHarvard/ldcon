"""Finite evidence-led coordinate search with paired incumbent–trial–incumbent windows.

This is passive in-loop performance comparison, not plant identification. Its
blocked uncertainty estimate does not establish a transfer function or a
confidence interval for independent noise. Device adapters prove all commands.
"""
import copy
import math
import statistics


def metrics(rows, actuator, activity_weight):
    times = [t for t,_ in rows]
    errors = [r['error'] for _,r in rows]
    outputs = [r['output'] for _,r in rows]
    band = rows[-1][1]['control_deadband']
    resolution = actuator.config['resolution']
    mean = statistics.mean(errors)
    rms = math.sqrt(statistics.mean(x*x for x in errors))
    centered = math.sqrt(statistics.mean((x-mean)**2 for x in errors))
    duration = times[-1]-times[0]
    activity = sum(abs(b-a) for a,b in zip(outputs,outputs[1:]))/duration/resolution
    center_t = statistics.mean(times)
    denominator = sum((t-center_t)**2 for t in times)
    drift = sum((t-center_t)*(e-mean) for t,e in zip(times,errors))/denominator
    variance = sum((e-mean)**2 for e in errors)
    lag = sum((a-mean)*(b-mean) for a,b in zip(errors,errors[1:]))/variance if variance else 0.
    blocks = [rows[i*len(rows)//4:(i+1)*len(rows)//4] for i in range(4)]
    block_scores, block_means = [], []
    for block in blocks:
        values = [r['error'] for _,r in block]
        tv = sum(abs(b[1]['output']-a[1]['output']) for a,b in zip(block,block[1:]))
        block_scores.append(math.sqrt(statistics.mean(x*x for x in values))/band+
                            activity_weight*tv/(block[-1][0]-block[0][0])/resolution)
        block_means.append(statistics.mean(values))
    return dict(rms=rms,mean=mean,centered_std=centered,peak=max(abs(x) for x in errors),
        activity_per_s=activity,drift_per_s=drift,lag1=lag,
        score=rms/band+activity_weight*activity,
        score_se=statistics.stdev(block_scores)/2,mean_se=statistics.stdev(block_means)/2,
        block_scores=block_scores,block_means=block_means,
        median_interval_s=statistics.median(b-a for a,b in zip(times,times[1:])),
        samples=len(rows),span_s=duration,center_t=center_t,
        acquired_start_s=times[0],acquired_end_s=times[-1],
        integral_usage_max=max((r.get('integral_usage') or 0) for _,r in rows),
        uncertainty_basis='Four contiguous blocks; conservative decision heuristic, not independent-noise confidence')


class AdaptiveComparison:
    def __init__(self, tuner, pid, actuator):
        if not hasattr(actuator,'adaptive_candidate') or not hasattr(pid,'configure_strategy'):
            raise ValueError('This actuator has no reviewed independent-gain candidate proof')
        self.t=tuner
        pid.configure_strategy('adaptive_coordinate')
        self.step=tuner.p['adaptive_step']
        self.executed=0
        self.seen=set()
        self.last_move=None
        self.history=[]
        self.exclusions=[]
        self.ready_at=None
        self.last_acquired=None
        self.candidate=None
        self.finalize=False
        self.phase('adaptive_baseline')

    def phase(self,name):
        self.t.phase=name;self.t.rows=[];self.ready_at=None;self.last_acquired=None

    def restart(self,reason):
        self.t.rows=[];self.ready_at=None;self.last_acquired=None
        self.t._status('running',reason=reason,comparison_restarted=True,
                       executed_trials=self.executed,pid=self.t.best_pid)

    @staticmethod
    def key(pid):
        return tuple((k,round(float(pid[k]),12)) for k in ('kp','ki','kd'))

    def enough_time(self,now):
        p=self.t.p
        # A B window and a returned-A window, settles plus conservative ramp
        # allowance. Never start another test just to consume the deadline.
        return self.t.started+p['timeout_s']-now >= 2*p['window_s']+3*p['settle_s']+12

    def directions(self,analysis,band):
        if abs(analysis['mean'])>.25*band and analysis['lag1']>=0:
            order=[('ki',1),('kp',1),('ki',-1),('kp',-1)]
            reason='Persistent target bias: examine integral authority before noise reduction'
        elif analysis['lag1']<-.1 or analysis['activity_per_s']>1:
            order=[('ki',-1),('kp',-1),('ki',1),('kp',1)]
            reason='Alternating error/activity proxy: examine lower loop action; not a diagnosed oscillation'
        else:
            order=[('ki',-1),('ki',1),('kp',-1),('kp',1)]
            reason='Compare integral action first, then independently observable proportional action'
        if self.last_move:
            axis,direction,accepted=self.last_move
            preferred=(axis,direction if accepted else -direction)
            order=[preferred]+[x for x in order if x!=preferred]
            reason='Continue the measured improving direction' if accepted else 'Reverse the last unsuccessful coordinate direction'
        return order,reason

    def select(self,now,analysis,rows,pid,actuator):
        t=self.t;p=t.p
        if self.executed>=p['adaptive_max_trials'] or not self.enough_time(now):
            self.finish_search(pid,actuator,'Trial/deadline budget does not support another complete paired comparison')
            return
        directions,reason=self.directions(analysis,rows[-1][1]['control_deadband'])
        had_tested=False
        for axis,direction in directions:
            step=self.step
            while step>=p['adaptive_min_step']-1e-12:
                candidate=copy.deepcopy(t.best_pid)
                candidate[axis]*=1+direction*step
                key=self.key(candidate)
                if key in self.seen:
                    had_tested=True
                    break  # Give the other coordinate its turn before refining.
                if key!=self.key(t.best_pid):
                    proof=actuator.adaptive_candidate(candidate,rows,p['minimum_effect_count'])
                    proposal=dict(pid=candidate,axis=axis,direction=direction,step=step,
                                  selection_reason=reason,proof=proof)
                    t._emit('online_adaptive_proposal',proposal)
                    if proof['admissible']:
                        self.seen.add(key)
                        self.candidate=proposal;self.baseline=analysis
                        self.executed+=1
                        pid.retune(candidate)
                        self.phase('adaptive_trial')
                        t._status('running',candidate=proposal,executed_trials=self.executed,pid=pid.pid)
                        return
                    self.exclusions.append(proposal)
                    if 'measurability' in proof.get('reason',''):
                        break  # A smaller change cannot exceed this effect threshold.
                step/=2
        if had_tested and self.step>p['adaptive_min_step']+1e-12:
            self.step=max(p['adaptive_min_step'],self.step/2)
            t._emit('online_adaptive_refinement',dict(step=self.step,reason='Previously tested directions exhausted; refine the coordinate step'))
            self.select(now,analysis,rows,pid,actuator)
            return
        self.finish_search(pid,actuator,'No untested candidate passes bounds, authority and measurability checks')

    def finish_search(self,pid,actuator,reason):
        self.t._emit('online_adaptive_search_result',dict(reason=reason,executed_trials=self.executed,
            comparisons=self.history,excluded_proposals=self.exclusions,best_pid=self.t.best_pid))
        self.t._finalize(pid,actuator)

    def comparison(self,before,trial,after,band,tolerance):
        p=self.t.p;z=p['confidence_multiplier']
        # Ramps and missing windows need not take equal time. Interpolate the
        # incumbent at the trial's actual acquisition center, not wall-clock
        # scheduling times. The objective still includes target bias/drift.
        weight=(trial['center_t']-before['center_t'])/(after['center_t']-before['center_t'])
        if not 0<weight<1:
            return dict(accepted=False,disposition='inconclusive',
                        reason='Trial acquisition times are not bracketed by incumbent windows')
        ref=(1-weight)*before['score']+weight*after['score']
        ref_se=math.hypot((1-weight)*before['score_se'],weight*after['score_se'])
        uncertainty=z*math.hypot(ref_se,trial['score_se'])
        score_return=abs(before['score']-after['score'])
        mean_return=abs(before['mean']-after['mean'])
        if (score_return>max(.25*max(before['score'],after['score']),z*math.hypot(before['score_se'],after['score_se']))
                or mean_return>max(.25*band,z*math.hypot(before['mean_se'],after['mean_se']))
                or max(abs(x['drift_per_s'])*x['span_s'] for x in (before,trial,after))>band
                or max(x['median_interval_s'] for x in (before,trial,after))>1.25*min(x['median_interval_s'] for x in (before,trial,after))):
            return dict(accepted=False,disposition='inconclusive',reference_weight=weight,
                        reason='Incumbent return/drift/cadence does not support a comparable A-B-A result')
        improvement=ref-trial['score']
        guards=(trial['rms']<=min(before['rms'],after['rms'])*1.05
            and abs(trial['mean'])<=max(min(abs(before['mean']),abs(after['mean']))*1.1,tolerance)
            and trial['peak']<=max(min(before['peak'],after['peak'])*1.1,band))
        accepted=guards and improvement>max(p['minimum_improvement']*ref,uncertainty)
        if accepted:
            disposition,reason='accepted','Paired improvement exceeds effect/uncertainty requirements'
        elif abs(improvement)<=uncertainty:
            disposition,reason='inconclusive','Paired score difference overlaps the configured uncertainty margin'
        else:
            disposition='rejected'
            reason=('Candidate significantly worsened the paired score' if improvement<0 else
                    'Resolved change does not meet minimum improvement or bias/peak guards')
        return dict(accepted=accepted,disposition=disposition,reason=reason,
            reference_score=ref,reference_weight=weight,score_improvement=improvement,uncertainty_margin=uncertainty)

    def tick(self,now,row,pid,actuator):
        t=self.t;p=t.p
        if not t._ready(pid):
            self.ready_at=None;t.rows=[]
            return
        if self.ready_at is None:
            self.ready_at=now+p['settle_s'];t.rows=[]
        if now<self.ready_at:
            return
        if row['limited'] or abs(row['filtered_error'])>2*row['control_deadband']:
            if t.phase=='adaptive_trial':
                self.last_move=(self.candidate['axis'],self.candidate['direction'],False)
                result=dict(accepted=False,disposition='rejected',reason='Candidate lost correction eligibility',candidate=self.candidate)
                self.history.append(result);t._emit('online_adaptive_result',result)
                pid.retune(t.best_pid);self.phase('adaptive_baseline')
            else:
                self.restart('Incumbent observation is ineligible; waiting within unchanged deadline')
            return
        acquired=row.get('acquired_s',now)
        if self.last_acquired is not None:
            if acquired<=self.last_acquired:
                return
            if acquired-self.last_acquired>actuator.config['max_sample_gap_s']:
                self.restart('Incomplete comparison window after acquisition gap')
                return
        self.last_acquired=acquired
        if self.finalize:
            self.finish_search(pid,actuator,'Accepted profile restored and freshly eligible; remaining budget is insufficient for another pair')
            return
        t.rows.append((acquired,copy.deepcopy(row)))
        if t.rows[-1][0]-t.rows[0][0]<p['window_s'] or len(t.rows)<p['min_samples']:
            return
        analysis=metrics(t.rows,actuator,p['activity_weight'])
        t._emit('online_adaptive_observation',dict(phase=t.phase,analysis=analysis,pid=pid.pid))
        if t.phase=='adaptive_baseline':
            self.select(now,analysis,t.rows,pid,actuator)
        elif t.phase=='adaptive_trial':
            self.trial=analysis
            pid.retune(t.best_pid)
            self.phase('adaptive_return')
        else:
            result=self.comparison(self.baseline,self.trial,analysis,row['control_deadband'],row['requested_tolerance'])
            result.update(candidate=self.candidate,before=self.baseline,trial=self.trial,returned=analysis)
            self.history.append(result);t._emit('online_trial_result',result)
            self.last_move=(self.candidate['axis'],self.candidate['direction'],result['accepted'])
            if result['accepted']:
                t.best_pid=copy.deepcopy(self.candidate['pid']);t.accepted+=1
                pid.retune(t.best_pid);self.phase('adaptive_baseline')
                self.finalize=self.executed>=p['adaptive_max_trials'] or not self.enough_time(now)
            else:
                if result['disposition']=='inconclusive':
                    self.step=max(p['adaptive_min_step'],self.step/2)
                self.select(now,analysis,t.rows,pid,actuator)
