"""Finite baseline/candidate comparison using the shared background lifecycle."""
import copy
import math
import statistics
from pathlib import Path

from experiment_core.records import atomic_json
from ldcon.core.adjustment_registry import parameters,number
from ldcon.jobs.background_pid_session import BackgroundPIDSession,_Refused
from ldcon.jobs.adaptive_window_session import finite
from ldcon.jobs.measurement_statistics import numeric_summary


def summarize(rows,objective,center):
    errors=[r['error_hz'] for r in rows];dac=[r['outputs']['piezo'] for r in rows]
    measured=numeric_summary(errors)
    if objective=='frequency_rms_hz':score=measured['rms']
    elif objective=='pzt_midrange_rms_count':score=math.sqrt(statistics.mean((v-center)**2 for v in dac))
    else:score=sum(abs(b-a) for a,b in zip(dac,dac[1:]))/(rows[-1]['acquired_s']-rows[0]['acquired_s'])
    cadence=[r.get('native_interval_last_s',r.get('native_interval_s')) for r in rows]
    cadence=[v for v in cadence if finite(v) and v>0]
    tec=[r['outputs']['temperature'] for r in rows]
    return dict(score=score,samples=len(rows),start=rows[0]['acquired_s'],end=rows[-1]['acquired_s'],
        source_interval_s=statistics.median(cadence) if cadence else None,
        observation_interval_s=statistics.median(b['acquired_s']-a['acquired_s'] for a,b in zip(rows,rows[1:])),
        tec_excursion_v=max(tec)-min(tec),frequency_rms_hz=measured['rms'],
        statistics={'frequency_error_hz':measured})


class BackgroundParameterSession(BackgroundPIDSession):
    def __init__(self,directory,lock,*,family,adjustment_parameters,**kwargs):
        self.family=family;self.rows=[];self.baseline=None;self.comparison=None;self.phase='baseline';self.last_source=None;self.last_native_seq=None
        super().__init__(directory,lock,pid_parameters=adjustment_parameters,**kwargs)

    def _parameters(self,raw,lock):
        if not callable(getattr(lock,'background_parameters_target',None)):
            raise ValueError('Owner adapter lacks runtime parameter epochs')
        return parameters(raw,self.family,require_candidate=True)

    def _start(self):
        self._fence()
        info=self.lock.response_info()
        if self.expected_system_id and (info.get('owner_context') or {}).get('system_id')!=self.expected_system_id:
            raise _Refused('Owner belongs to another laser')
        status=self.lock.background_status()
        if not self._reference_before_begin(info,status):return
        identity=dict(self.background,controller_job_id=self.directory.parents[2].name)
        identity={k:identity[k] for k in ('monitor_id','generation','grant_revision','controller_job_id')}
        result=self._effect('begin',lambda:self.lock.background_begin(family=self.family,parameters=self.p,
            identity=identity,expected_generation=status['generation'],
                    expected_operating_mode_revision=self.background.get('expected_operating_mode_revision'),poll_interval_s=self.poll_interval_s,
            handoff_timeout_s=self.background['handoff_timeout_s']))
        self.epoch=result['epoch'];self.active=True
        self._remember_epoch(self.epoch)
        if not self._reference_after_begin():return
        self.center=self.epoch['checkpoint']['pzt_midrange_dac']
        self.skip_row=True
        self.phase_started=self.clock();self.rows=[]
        atomic_json(self.directory/'checkpoint.json',self.epoch)
        self._status('measuring',phase=self.phase,parameters=self.epoch['checkpoint']['parameters'])

    def _sample(self,row):
        if row is None:return
        stamp=row.get('acquired_s');source=row.get('source_acquired_unix_s') or stamp
        # Never consume the row which preceded owner admission/candidate commit.
        if not finite(stamp) or not finite(source) or self.last_source is not None and source<=self.last_source:return
        self.last_source=source
        seq=row.get('native_input_seq')
        previous_native=self.last_native_seq
        self.last_native_seq=seq if type(seq) is int else None
        if row.get('native_input_stale') is not False or type(seq) is not int or (previous_native is not None and seq<=previous_native):
            self.rows=[];self._status('waiting_for_measurement',reason='Fresh advancing native input required');return
        if self.skip_row:
            self.skip_row=False;return
        if self.clock()-self.phase_started<self.p['settle_s']:return
        if row.get('control_ready_passed') is not True or row.get('adjustment_ready_passed', row.get('entry_gate_passed')) is not True or row.get('target_ramp_active') or row.get('state')!='locked':
            self.rows=[];self._status('waiting_for_measurement',reason='Fresh unchanged lock context required');return
        try:
            number(row['error_hz'],'error')
            for key in ('piezo','temperature','current'):number(row['outputs'][key],key)
        except (ValueError,KeyError,TypeError):
            self.rows=[];return
        if self.rows and stamp<=self.rows[-1]['acquired_s']:return
        if self.rows and stamp-self.rows[-1]['acquired_s']>self.p['max_sample_gap_s']:
            self.rows=[]
        item={k:copy.deepcopy(row[k]) for k in ('acquired_s','error_hz','outputs')}
        for key in ('native_interval_last_s','native_interval_s','supervision_sequence'):
            if key in row:item[key]=row[key]
        self.rows.append(item)
        if len(self.rows)>100000:raise _Refused('Comparison evidence budget exceeded')
        self._status('measuring',phase=self.phase,samples=len(self.rows),candidate=self.p['candidate'])
        if len(self.rows)<self.p['min_samples'] or stamp-self.rows[0]['acquired_s']<self.p['window_s']:return
        # A 100 s candidate must not be adopted before its slow loop has acted.
        # Four planner cycles cover at least two TEC/FF opportunities; actual
        # thermal motion is still independently required for FF identification.
        first,last=self.rows[0].get('supervision_sequence'),self.rows[-1].get('supervision_sequence')
        if type(first) is not int or type(last) is not int or last-first<4:return
        result=summarize(self.rows,self.p['objective'],self.center)
        result['supervision_cycles']=last-first
        atomic_json(self.directory/(self.phase+'-measurements.json'),dict(summary=result,rows=self.rows))
        atomic_json(self.directory/('segment-%03d-%s-measurements.json'%(self.segment,self.phase)),dict(summary=result,rows=self.rows))
        if self.phase=='baseline':
            self.baseline=result;self._fence()
            receipt=self._effect('candidate',lambda:self.lock.background_parameters_target(self.epoch['token']))
            if receipt.get('epoch',{}).get('phase')!='candidate':raise _Refused('Owner did not accept candidate')
            self.phase='candidate';self.phase_started=self.clock();self.rows=[]
            return
        self.comparison=dict(objective=self.p['objective'],baseline=self.baseline,candidate=result)
        a,b=self.baseline['source_interval_s'],result['source_interval_s']
        if a is None or b is None or abs(b-a)>a*self.p['maximum_cadence_fraction']:
            self.preempt('incomparable_source_cadence');return
        a,b=self.baseline['observation_interval_s'],result['observation_interval_s']
        if self.p['objective']=='pzt_activity_count_per_s' and abs(b-a)>a*self.p['maximum_cadence_fraction']:
            self.preempt('incomparable_source_cadence');return
        if min(self.baseline['tec_excursion_v'],result['tec_excursion_v'])<self.p['minimum_tec_excursion_v']:
            self.preempt('insufficient_thermal_excitation');return
        if result['score']>=self.baseline['score']*(1-self.p['minimum_improvement_fraction']):
            self.preempt('not_improved');return
        # Improvement in a chosen auxiliary objective cannot hide worse frequency locking.
        if result['frequency_rms_hz']>self.baseline['frequency_rms_hz']*(1+self.p['minimum_improvement_fraction']):
            self.preempt('frequency_regressed');return
        self._fence()
        receipt=self._effect('adopt',lambda:self.lock.background_adopt(self.epoch['token']))
        self._completed(receipt,'succeeded')

    def _completed(self,receipt,status):
        if status=='cancelled' and self.returning in ('not_improved','insufficient_thermal_excitation','incomparable_source_cadence','frequency_regressed'):
            status='unchanged'
        super()._completed(receipt,status)
        path=self.directory/'results'/(self.family+'.json')
        import json
        result=json.loads(path.read_text())
        result['comparison']=self.comparison
        atomic_json(path,result)
        atomic_json(self.directory/('segment-%03d-%s-result.json'%(self.segment,self.family)),result)
        if self.continuation is not None:
            self.continuation['evidence']=result
            atomic_json(self.directory/('segment-%03d-outcome.json'%self.segment),self.continuation)
            self._status(self.continuation['state'])
        # Details stay with the job; no notifications or external upload here.
        from html import escape
        (self.directory/'report.html').write_text('<!doctype html><html lang="en"><meta charset="utf-8"><title>Runtime adjustment</title><h1>'+escape(self.family)+' comparison</h1><pre>'+escape(json.dumps(result,indent=2))+'</pre></html>')
