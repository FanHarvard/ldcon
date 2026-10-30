"""Owner-only native PI gain transitions; raw integrator bounds stay fixed."""
import copy
import math
import uuid
from pathlib import Path
from ldcon.core.contracts import DeviceError,MeasurementUnavailable
from ldcon.core.device_conditions import finite
from ldcon.core.interface_schema import digest,load_mapping,validate_laser
from ldcon.core.records import atomic_json
from ldcon.owners.ecdl_steady_policy import adjustment_ready

C_NM=299792458e9


def correction_envelope(commissioned_kp,capture_nm,integral_limit_count,kp,ki,raw_bound,factors):
    """Approved correction envelope of a commissioned native PI and the worst-case total of every declared factor.

    envelope = commissioned P over the capture interval + approved integral bound (counts). The commissioned
    total is the actual baseline (kp*capture + ki*raw_bound, equal to the envelope for a verified baseline).
    Each declared factor scales P and I together with the raw integral bounds fixed (factor 1 is the return to
    the baseline); every resulting total must stay inside the envelope. Reductions always do; a larger total is
    refused.
    """
    envelope=abs(finite(commissioned_kp))*capture_nm+finite(integral_limit_count)
    if not math.isfinite(envelope) or envelope<=0:raise DeviceError('Invalid commissioned PI correction envelope')
    commissioned=abs(kp)*capture_nm+abs(ki)*raw_bound
    if commissioned>envelope*(1+1e-9):
        raise DeviceError('Native commissioned PI exceeds its approved correction envelope')
    trials={str(float(f)):finite(f)*commissioned for f in factors}
    if any(total>envelope*(1+1e-9) for total in trials.values()):
        raise DeviceError('Native PI trial exceeds approved commissioned correction envelope')
    return dict(envelope_count=envelope,commissioned_total_count=commissioned,
                trial_total_count=max(trials.values()),trial_totals_count=trials,
                envelope_source='commissioned kp*capture_nm + integral_limit_count')


class NativePIDTuning:
    def __init__(self,native,parameters,*,independent=False):
        self.n=native;self.o=native.o;self.p=parameters
        cfg=native.p.get('online_pid',{})
        if cfg.get('enabled') is not True:raise DeviceError('Native online PI tuning is not enabled by the device')
        self.step=None if independent else finite(cfg['max_step_count']);self.settle=0. if independent else finite(cfg['settle_s']);self.max_gap=finite(cfg['max_sample_gap_s'])
        if (not independent and not 0<self.step<=native.p['commissioning']['max_correction_count']/10) or self.settle<0 or self.max_gap<=0:
            raise DeviceError('Invalid device PI transition bound')
        if native.study and native.study.status=='running':raise DeviceError('Current study is active')
        if native.current_target!=native.target:raise MeasurementUnavailable('PI target ramp must finish before tuning')
        state,obs=native.read_feedback();native.native(state,obs,native.p,locked=True)
        if not adjustment_ready(obs,native.p,native.current_target):raise MeasurementUnavailable('Fresh admitted branch context required for PI baseline')
        self.original=copy.deepcopy(state['servo_config']);self.original_profile=copy.deepcopy(native.p)
        minimum=None if independent else finite(cfg['minimum_gain_factor'])
        if not independent and (not 0<minimum<1 or min(parameters['gain_factors'])<minimum):
            raise DeviceError('Native gain reduction exceeds device trial envelope')
        slope=abs(native.p['wavelength_nm_per_count'])
        expected_gains=dict(pid_kp=native.p['p_sample']/slope,pid_ki=native.p['g_sample']/(native.interval*slope),
            system_min=C_NM/(native.target+native.p['capture_hz']),system_max=C_NM/(native.target-native.p['capture_hz']),
            target=C_NM/native.target,
            pid_integral_min=-native.p['integral_limit_count']/(native.p['g_sample']/(native.interval*slope)),
            pid_integral_max=native.p['integral_limit_count']/(native.p['g_sample']/(native.interval*slope)))
        for key,value in expected_gains.items():
            if not math.isclose(self.original[key],value,rel_tol=1e-9,abs_tol=1e-12):
                raise DeviceError('Native baseline differs from commissioned profile: '+key)

        self.expected={k:self.original[k] for k in ('pid_kp','pid_ki','pid_kd','pid_integral_min','pid_integral_max','control_offset','system_min','system_max','target')}
        if self.original['pid_kd']!=0:raise DeviceError('Native online adapter supports commissioned PI only')
        self.raw_bound=max(abs(self.original['pid_integral_min']),abs(self.original['pid_integral_max']))
        if self.raw_bound<=0:raise DeviceError('Invalid native raw integral bounds')
        self.capture_nm=max(abs(C_NM/(native.target+d)-C_NM/native.target) for d in (-native.p['capture_hz'],native.p['capture_hz']))
        # A trial reduces an already-commissioned PI. Its envelope is the approved commissioned correction
        # (P over the capture interval + the approved integral bound), not commissioning.max_correction_count,
        # which bounds only the FIRST stage of commissioning an unverified PI (design 2026-10-22, 2026-10-28).
        if getattr(native,'commissioning',False) or 'approved_integral_limit_count' in native.p:
            raise DeviceError('Native PI commissioning is in progress; a PI trial requires the commissioned profile')
        self.envelope=correction_envelope(expected_gains['pid_kp'],self.capture_nm,native.p['integral_limit_count'],
                                          self.original['pid_kp'],self.original['pid_ki'],self.raw_bound,
                                          [1.] if independent else parameters['gain_factors'])
        self.conversion=(C_NM/native.target)**2/C_NM
        self.original_pid=dict(kp=self.original['pid_kp']*self.conversion,
            ki=self.original['pid_ki']*self.conversion,kd=0.,derivative_filter_s=0.,integral_limit=native.p['integral_limit_count'])
        self.target_factor=1.;self.changed_at=self.o.clock()-self.settle;self.saved=False;self.closed=False
        self.revision=digest(self.o.device);self.id=uuid.uuid4().hex
        self.allowed={1.,*parameters['gain_factors']};self.steps=0
        if not independent:
            native.discard_proposals('native PI tuning holds current and TEC')
            self.o.audit('native_pid_tuning_started',trial=self.id,parameters=parameters,original=self.original,
                         transition_max_count=self.step,raw_integral_bounds_fixed=not independent,
                         correction_envelope=self.envelope,
                         observation_scope='approximately 1 Hz optical/DAC samples, not full native bandwidth')

    def factor_for(self,pid):
        if set(pid)!=set(self.original_pid):raise DeviceError('Native PI parameter keys differ')
        factor=finite(pid['kp'])/self.original_pid['kp']
        if not any(math.isclose(factor,x,rel_tol=1e-8) for x in self.allowed):raise DeviceError('Native gain scale was not declared for this trial')
        expected={**self.original_pid,'kp':self.original_pid['kp']*factor,'ki':self.original_pid['ki']*factor}
        if any(not math.isclose(finite(pid[k]),v,rel_tol=1e-8,abs_tol=1e-15) for k,v in expected.items()):
            raise DeviceError('Native PI trial may only scale commissioned P and I together')
        return factor

    def at_checkpoint(self):
        return self.target_factor == 1.

    def target(self,pid):
        self.target_factor=self.factor_for(pid)
        self.o.audit('native_pid_target',trial=self.id,factor=self.target_factor)
        return self.status()

    def ready(self):
        return all(math.isclose(self.expected[k],self.original[k]*self.target_factor,rel_tol=1e-10,abs_tol=1e-12)
                   for k in ('pid_kp','pid_ki')) and self.o.clock()-self.changed_at>=self.settle

    def advance(self,state,obs):
        for k,v in self.expected.items():
            if not math.isclose(state['servo_config'][k],v,rel_tol=1e-8,abs_tol=1e-12):
                raise DeviceError('External native PID configuration change: '+k)
        if self.closed:return
        # Reductions wait for explicit branch readiness. Restoration remains bounded
        # by the commissioned input capture interval even if side peaks appear.
        if self.target_factor<1 and not adjustment_ready(obs,self.n.p,self.n.current_target):return
        if self.o.clock()-self.changed_at<.1:return
        for key,bound in (('pid_kp',self.capture_nm),('pid_ki',self.raw_bound)):
            desired=self.original[key]*self.target_factor;current=self.expected[key]
            if math.isclose(current,desired,rel_tol=1e-10,abs_tol=1e-12):continue
            delta=max(-self.step/bound,min(self.step/bound,desired-current))
            value=current+delta
            self.n.write({key:value},'bounded native PI gain transition')
            self.expected[key]=value;self.changed_at=self.o.clock();self.steps+=1
            self.o.audit('native_pid_transition',trial=self.id,key=key,value=value,
                         maximum_config_induced_count=abs(delta)*bound,target_factor=self.target_factor)
            break

    def status(self):
        pid={**self.original_pid,'kp':self.original_pid['kp']*self.target_factor,'ki':self.original_pid['ki']*self.target_factor}
        return dict(ready=self.ready(),pid=pid,trial=self.id,steps=self.steps,
            actual_native={k:self.expected[k] for k in ('pid_kp','pid_ki')},
            integral_limit_count=self.expected['pid_ki']*self.raw_bound,
            device_revision=digest(self.o.device),config=dict(pid=pid,output_unit='count',measurement_unit='Hz',resolution=1.,
            observation_scope='sampled optical/DAC residual; full native bandwidth is not evaluated',
            transition_max_count=self.step,max_sample_gap_s=self.max_gap,
            effective_integral_limit_count=self.expected['pid_ki']*self.raw_bound,raw_integral_bounds_fixed=True))

    def save(self,pid):
        if self.factor_for(pid)!=self.target_factor or not self.ready():raise DeviceError('Native gains have not settled at accepted settings')
        factor=self.target_factor;profile=copy.deepcopy(self.original_profile)
        profile['p_sample']*=factor;profile['g_sample']*=factor;profile['integral_limit_count']*=factor
        evidence=self.o.audit_path.parent/('native-pid-tuning-'+self.id+'.json')
        proof=dict(trial=self.id,factor=factor,steps=self.steps,original=self.original,
            accepted_native=copy.deepcopy(self.expected),profile=profile,
            transition_max_count=self.step,raw_integral_bounds_fixed=True,
            handover_scope='Original commissioned envelope retained; reduced gains evaluated separately',
            observation_scope='sampled optical/DAC residual; full native bandwidth is not evaluated')
        atomic_json(evidence,proof)
        profile['online_tuning_evidence']=str(evidence)
        path=self.o.laser_file
        if path is None:raise DeviceError('Native PID settings file is unavailable')
        before=path.read_text();laser=validate_laser(load_mapping(before))
        if digest(laser['devices']['rig'])!=self.revision:raise DeviceError('Laser device settings changed during native trial')
        laser['devices']['rig']['control_settings']['steady_lock']=profile
        validate_laser(laser)
        if path.read_text()!=before:raise DeviceError('Concurrent native settings edit')
        from ldcon.core.deck_yaml import dump_document
        import os,tempfile
        fd,name=tempfile.mkstemp(prefix=path.name+'.',dir=path.parent)
        try:
            with os.fdopen(fd,'w') as stream:stream.write(dump_document(laser));stream.flush();os.fsync(stream.fileno())
            os.replace(name,path)
            self.o.device['control_settings']['steady_lock']=profile;self.n.p=profile;self.saved=True
        finally:
            if os.path.exists(name):os.unlink(name)
        self.o.audit('native_pid_settings_saved',trial=self.id,evidence=str(evidence),factor=factor)
        return dict(saved=True,evidence=str(evidence),**self.status())

    def finish(self,status):
        if not self.ready():raise DeviceError('Native PI transition must settle before ending tuning')
        if status not in ('succeeded','unchanged') and self.target_factor!=1.:
            raise DeviceError('Failed tuning must restore original gains')
        if not self.saved:
            profile=copy.deepcopy(self.original_profile)
            for key in ('p_sample','g_sample','integral_limit_count'):profile[key]*=self.target_factor
            self.n.p=profile
        self.closed=True;self.n.discard_proposals('native PI tuning ended')
        self.o.audit('native_pid_tuning_finished',status=status,saved=self.saved,**self.status())
        return self.status()
