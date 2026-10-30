"""Native PI candidates with symmetric integral bounds and proved return paths.

Only live coefficients/bounds change. No target, offset, reset, enable or policy
write is exposed. Configuration effects obey the approved external-modulation
budget when enabled, including effective DAC quantization, and always preserve
the complete physical output envelope.
"""
import copy
import math
import time

from ldcon.owners.esp32_native_owner import channel_metadata, CONTROLLED, PRESERVED
from ldcon.core.instrument_client import BrokerError
from ldcon.owners.native_current_tuning import config
from ldcon.owners.native_policy_safety import actual_envelope, check_envelope

FIELDS = ('pid_kp','pid_ki','pid_kd','pid_integral_min','pid_integral_max')


def candidate(value):
    if (not isinstance(value,dict) or set(value)!={'kp_count_per_nm','ki_count_per_nm_s','integral_limit_count'}
            or any(type(v) not in (int,float) or not math.isfinite(v) for v in value.values())
            or value['kp_count_per_nm']==0 or value['ki_count_per_nm_s']==0 or value['integral_limit_count']<=0):
        raise ValueError('A finite signed P/I and positive symmetric integral limit are required')
    return copy.deepcopy(value)


def pid_from_config(cfg):
    return dict(kp=cfg['pid_kp'],ki=cfg['pid_ki'],kd=cfg['pid_kd'],
                integral_limit_count=max(abs(cfg['pid_integral_min']),abs(cfg['pid_integral_max']))*abs(cfg['pid_ki']))


def pid_from_candidate(value):
    value=candidate(value)
    return dict(kp=value['kp_count_per_nm'],ki=value['ki_count_per_nm_s'],kd=0.,
                integral_limit_count=value['integral_limit_count'])


def close_pid(left,right):
    return (isinstance(left,dict) and set(left)==set(right)
            and all(type(left[k]) in (int,float) and math.isfinite(left[k])
                    and math.isclose(left[k],right[k],rel_tol=1e-10,abs_tol=1e-12) for k in right))


class FlexibleNativeTrial:
    def __init__(self, owner, before, value, handoff_timeout_s):
        self.owner=owner; self.original=copy.deepcopy(before['servo_config'])
        self.expected=copy.deepcopy(self.original);self.started=time.time();self.changed=[]
        self.strategy='validated_candidates';self.saved=False;self.last_acquisition=None
        self.original_pid=pid_from_config(self.original)
        self.candidate_pid=pid_from_candidate(value)
        self.target=copy.deepcopy(self.original_pid)
        self.path=[]
        p=owner.policy;c=p['calibration'];dac=p['dac']
        if (self.original['pid_kd']!=0 or self.original['pid_integral_max']<=0
                or not math.isclose(self.original['pid_integral_min'],-self.original['pid_integral_max'],rel_tol=1e-9)):
            raise ValueError('Native flexible PI requires symmetric existing integral bounds and D=0')
        for key,field in [('kp','kp_count_per_nm'),('ki','ki_count_per_nm_s')]:
            if self.candidate_pid[key]*p['native'][field]<=0 or self.original_pid[key]*p['native'][field]<=0:
                raise ValueError('Candidate gains must oppose the commissioned response sign')
        metadata=channel_metadata(dac)
        self.quantum=1 << (16-metadata['bits'])
        per_code=abs(c['current_a_per_v']*metadata['voltage_step_v']/self.quantum)*(1+c['gain_relative_uncertainty'])
        from ldcon.owners.native_modulation_limits import budget_a
        self.current_step_a=budget_a(p)
        # Without a host current ceiling, only the complete physical DAC span
        # bounds this effect; every intermediate configuration is still proved.
        self.effect_cap=(self.current_step_a/per_code if self.current_step_a is not None else 65535)-2*self.quantum
        if not self.effect_cap>0:
            raise ValueError('Host step/slew budget cannot cover a quantized native configuration transition')
        self.period=max(2*p['max_sample_age_s'],p['update_interval_s'])+owner.interval_s
        self.max_steps=max(0,math.floor(handoff_timeout_s/self.period)-1)
        self.candidate_cfg=self._configuration(self.original,self.candidate_pid)
        self._proof(self.original);self._proof(self.candidate_cfg)
        outward=self._plan(self.original,self.candidate_cfg)
        # Any point at which foreground can arrive has its own proved return.
        self.maximum_return_steps=0
        for cfg in [self.original,*[row['config'] for row in outward]]:
            self.maximum_return_steps=max(self.maximum_return_steps,len(self._plan(cfg,self.original)))
        self.return_bound_s=(self.maximum_return_steps+1)*self.period
        self.outward_steps=len(outward)

    def _configuration(self, cfg, pid):
        raw=pid['integral_limit_count']/abs(pid['ki'])
        if not math.isfinite(raw) or raw<=0:raise ValueError('Candidate raw integral bound is not finite and positive')
        return dict(cfg,pid_kp=pid['kp'],pid_ki=pid['ki'],pid_kd=0.,pid_integral_min=-raw,pid_integral_max=raw)

    def _proof(self,cfg):
        return check_envelope(self.owner,self.owner.policy,actual_envelope(self.owner.policy,cfg))

    @staticmethod
    def _error_bound(cfg):
        # Use the actual configured receiver and target-modulation equation.
        adc=[cfg['mod_valid_min'],cfg['mod_valid_max']]
        targets=[cfg['target']]+[cfg['target']+cfg['multiplier_target']*(x-cfg['baseline_target']) for x in adc]
        return max(abs(a-b) for a in targets for b in (cfg['system_min'],cfg['system_max']))

    def _plan(self,start,end):
        """Release P/raw-I contribution first; expand only after smaller terms."""
        current=copy.deepcopy(start); rows=[]
        def move(axis,desired):
            nonlocal current
            while not math.isclose(current[axis],desired,rel_tol=1e-12,abs_tol=1e-15):
                if len(rows)>=self.max_steps:
                    raise ValueError('Candidate or checkpoint return exceeds the foreground handoff budget')
                sensitivity=(self._error_bound(current) if axis=='pid_kp' else
                             max(abs(current['pid_integral_min']),abs(current['pid_integral_max'])) if axis=='pid_ki'
                             else abs(current['pid_ki']))
                maximum=self.effect_cap/max(sensitivity,1e-300)
                delta=desired-current[axis]
                value=current[axis]+math.copysign(min(abs(delta),maximum),delta)
                if value==current[axis]:raise ValueError('Native transition is below floating-point resolution')
                values={axis:value}
                if axis=='pid_integral_max':values['pid_integral_min']=-value
                next_cfg=dict(current,**values)
                proof=self._proof(next_cfg)
                effect=abs(value-current[axis])*sensitivity
                if effect>self.effect_cap*(1+1e-12):raise ValueError('Native transition exceeds host effect budget')
                rows.append(dict(values=values,config=next_cfg,maximum_delta_count=effect,
                                 quantized_delta_bound_count=effect+2*self.quantum,proof=proof))
                current=next_cfg
        if abs(end['pid_kp'])<abs(current['pid_kp']):move('pid_kp',end['pid_kp'])
        move('pid_integral_max',min(current['pid_integral_max'],end['pid_integral_max']))
        move('pid_ki',end['pid_ki'])
        move('pid_integral_max',end['pid_integral_max'])
        move('pid_kp',end['pid_kp'])
        return rows

    def state(self):
        cfg=self.owner.expected_config or self.expected
        pid=pid_from_config(cfg)
        configuration=config(self.owner,cfg)
        configuration.update(validated_candidates=True,optimization_bounds=None,
            integral_bounds_adjustable=True,configuration_effect_cap_count=self.effect_cap,
            quantized_step_bound_a=self.current_step_a,return_bound_s=self.return_bound_s)
        return dict(pid=pid,target=copy.deepcopy(self.target),ready=not self.path and close_pid(pid,self.target),
                    config=configuration,changed_steps=len(self.changed),saved=False,strategy=self.strategy)

    def set_target(self,pid):
        if not close_pid(pid,self.original_pid) and not close_pid(pid,self.candidate_pid):
            raise BrokerError('range','Only the admitted candidate or its checkpoint may be selected')
        desired=self.original if close_pid(pid,self.original_pid) else self.candidate_cfg
        try:self.path=self._plan(self.expected,desired)
        except ValueError as exc:raise BrokerError('range',str(exc)) from exc
        self.target=copy.deepcopy(pid)
        return self.state()

    def step(self):
        if not self.path:return self.state()
        o=self.owner;before=o._read_before(require_input=True);cfg=before['servo_config']
        if o.last_optical==self.last_acquisition or o.clock()-o.last_write<o.policy['update_interval_s']:
            raise BrokerError('measurement','Fresh acquisition and native gain update interval are required')
        if any(not math.isclose(cfg[k],self.expected[k],rel_tol=1e-9,abs_tol=1e-9) for k in (*CONTROLLED,*PRESERVED)):
            raise o._latch('Native flexible PI reference changed outside this trial')
        row=self.path[0]
        self._proof(dict(cfg,**row['values']))
        o._write(row['values'],before,'bounded native PI/integral configuration transition')
        self.expected=copy.deepcopy(row['config']);self.path.pop(0);self.last_acquisition=o.last_optical
        self.changed.append(dict(values=row['values'],maximum_delta_count=row['maximum_delta_count'],
                                 quantized_delta_bound_count=row['quantized_delta_bound_count']))
        return self.state()

    def finish(self,status):
        state=self.state()
        if not state['ready'] or status!='succeeded' and not close_pid(state['pid'],self.original_pid):
            raise BrokerError('busy','Native flexible PI checkpoint return is incomplete')
        self.owner.pid_history.append(dict(started_unix_s=self.started,completed_unix_s=time.time(),status=status,
            original=self.original_pid,final=state['pid'],saved=False,strategy=self.strategy,changes=self.changed,
            return_bound_s=self.return_bound_s))
        self.owner.pid_history=self.owner.pid_history[-20:]
        self.owner._persist();self.owner.pid_tuning=None
        return state
