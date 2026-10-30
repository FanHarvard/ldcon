"""Owner-only runtime TEC/FF epochs with bounded physical checkpoint return."""
import copy
import math
from ldcon.core.adjustment_registry import PARAMETER_FAMILIES,owner_policy,parameters,number
from ldcon.owners.background_grants import read_grant
from ldcon.owners.ecdl_steady_policy import SteadyPlanner,validate_profile,control_ready,entry_quality,adjustment_ready
from ldcon.core.contracts import DeviceError,MeasurementUnavailable
from ldcon.core.instrument_client import BrokerError


def active(n):
    return bool(n.background.epoch and n.background.epoch.get('family') in PARAMETER_FAMILIES)


def values(n):
    ff=n.parameter_ff_config or {}
    return dict(tec_gain=n.p['tec_gain'],tec_dt_cap_s=n.p['tec_dt_cap_s'],
                supervision_interval_s=n.p['supervision_interval_s'],piezo_center_dac=n.tec_reference_dac,
                current_per_temperature=ff.get('current_per_temperature'))


def grant(n,e):
    b=read_grant(n.o.laser['id'],e['identity'],lease=n.o.lease)
    plans=b.get('adjustment_parameters')
    if not isinstance(plans,dict):raise ValueError('Monitor parameter plans must be a mapping')
    approved=parameters(plans.get(e['family']),e['family'])
    if (e['family'] not in b.get('families',[]) or approved!={k:v for k,v in e['parameters'].items() if k!='candidate'}
        or b.get('handoff_timeout_s')!=e['timeout_s']):raise ValueError('Runtime trial differs from the current monitor grant')
    return b


def install(n,v,state):
    # No direct hardware effect; next owner plan still enforces physical bounds.
    p=copy.deepcopy(n.p)
    for key in ('tec_gain','tec_dt_cap_s','supervision_interval_s'):p[key]=v[key]
    ff=copy.deepcopy(n.parameter_ff_config)
    if ff:ff['current_per_temperature']=v['current_per_temperature']
    if p.get('tec_current_feedforward'):p['tec_current_feedforward']=copy.deepcopy(ff)
    validate_profile(p,n.strategy)
    origin=copy.deepcopy(n.planner.origin)
    planner=SteadyPlanner(p,n.planner.policies,state['outputs'],n.strategy,tec_reference_dac=v['piezo_center_dac'])
    planner.origin=origin  # Never expand an activation-relative span by rebuilding a planner.
    n.discard_proposals('background runtime parameter transaction')
    n.p=p;n.planner=planner;n.parameter_ff_config=ff;n.tec_reference_dac=v['piezo_center_dac']
    n.parameter_sequence+=1;n.last_supervision=n.o.clock()


def steps(n,e,outputs):
    total=0
    for axis in ('current','temperature'):
        p=n.o.policies()[axis];delta=abs(outputs[axis]-e['checkpoint']['outputs'][axis])
        dt=min(p.update_interval_s,e['checkpoint']['parameters']['tec_dt_cap_s']) if axis=='temperature' else p.update_interval_s
        step=min(p.max_step,p.max_slew_per_s*dt)
        if p.resolution:step=math.floor((step+1e-12)/p.resolution)*p.resolution
        tolerance=max(p.resolution/2,1e-12)
        if delta>tolerance and step<=0:raise ValueError('No quantized return step for '+axis)
        if delta>tolerance:total+=math.ceil((delta-tolerance)/step)
    return (total+2)*e['return_period_s']


def return_bounds(n,e,bounds):
    """Intersect human-approved excursions with the existing handoff budget.

    Keep the approved ceiling separate from the usable envelope. A wider human
    ceiling must not disable every trial or silently extend foreground delay.
    """
    result=copy.deepcopy(bounds)
    def costs():
        values={}
        for axis in result:
            p=n.o.policies()[axis];origin=e['checkpoint']['outputs'][axis]
            dt=min(p.update_interval_s,e['checkpoint']['parameters']['tec_dt_cap_s']) if axis=='temperature' else p.update_interval_s
            step=min(p.max_step,p.max_slew_per_s*dt)
            if p.resolution:step=math.floor((step+1e-12)/p.resolution)*p.resolution
            delta=max(abs(v-origin) for v in result[axis]);tol=max(p.resolution/2,1e-12)
            if delta>tol and step<=0:raise ValueError('No quantized return step for '+axis)
            values[axis]=(math.ceil(max(0.,delta-tol)/step) if step>0 else 0,step)
        return values
    available=math.floor(e['timeout_s']/e['return_period_s']+1e-12)-2
    if available<0:raise ValueError('Handoff budget cannot cover checkpoint verification')
    c=costs()
    while sum(v[0] for v in c.values())>available:
        axis=max(c,key=lambda a:c[a][0]);count,step=c[axis]
        origin=e['checkpoint']['outputs'][axis];span=(count-1)*step
        result[axis]=[max(result[axis][0],origin-span),min(result[axis][1],origin+span)]
        c=costs()
    return result


def begin(n,r,context,state,obs):
    family=r['family'];p=parameters(r['parameters'],family,require_candidate=True);policy=owner_policy(n.o.device,family)
    if n.p.get('tec_center_reference')=='activation' and (not n.tec_reference_confirmed or n.tec_reference_pending):
        raise MeasurementUnavailable('A confirmed stable TEC center is required before a parameter epoch')
    if n.strategy!='hold' or n.tec_ff_paused:raise ValueError('Runtime trials require an unpaused hold strategy')
    if not adjustment_ready(obs,n.p,n.current_target):raise MeasurementUnavailable('Runtime trial needs fresh frequency and branch context')
    if family=='feedforward' and not n.p.get('tec_current_feedforward'):raise ValueError('TEC-to-current FF must already be enabled and commissioned')
    original=values(n);target={**original,**p['candidate']}
    for key,value in p['candidate'].items():
        if key not in policy['bounds'] or not policy['bounds'][key][0]<=value<=policy['bounds'][key][1]:
            raise ValueError('Runtime candidate outside approved '+key+' bounds')
        if original[key] is None or not policy['bounds'][key][0]<=original[key]<=policy['bounds'][key][1]:
            raise ValueError('Checkpoint is not inside the approved '+key+' bounds')
    configured=n.o.device['control_settings']['steady_lock']
    # An explicitly approved background envelope, not the startup default,
    # governs trial gain. A tighter explicit invocation override still wins.
    if ('tec_gain' in p['candidate'] and 'tec_gain' in n.parameter_overrides
        and target['tec_gain']>n.parameter_overrides['tec_gain']) or target['tec_dt_cap_s']>configured['tec_dt_cap_s']:
        raise ValueError('Runtime trial cannot increase configured gain/dt ceilings')
    if 'piezo_center_dac' in p['candidate']:
        pp=n.o.policies()['piezo']
        if n.p.get('tec_center_reference')!='activation' or not n.tec_reference_confirmed or not pp.low<=target['piezo_center_dac']<=pp.high:
            raise ValueError('A confirmed activation PZT center inside owner bounds is required')
    test=copy.deepcopy(n.p)
    for key in ('tec_gain','tec_dt_cap_s','supervision_interval_s'):test[key]=target[key]
    validate_profile(test,n.strategy)
    epoch=dict(family=family,identity=r['identity'],parameters=p,timeout_s=r['handoff_timeout_s'])
    grant(n,epoch)
    period=max(.5,r['poll_interval_s'],*[n.o.policies()[a].update_interval_s for a in ('current','temperature')])
    native_keys=('pid_multiplier','pid_kp','pid_ki','pid_kd','pid_integral_min','pid_integral_max','target','control_offset')
    checkpoint=dict(parameters=original,outputs={a:state['outputs'][a] for a in ('current','temperature')},
                    native={k:state['servo_config'][k] for k in native_keys},planner_origin=copy.deepcopy(n.planner.origin),
                    pzt_midrange_dac=(n.o.policies()['piezo'].low+n.o.policies()['piezo'].high)/2)
    epoch.update(checkpoint=checkpoint,return_period_s=period)
    bounds={}
    for axis in ('current','temperature'):
        a=n.o.policies()[axis];origin=checkpoint['outputs'][axis];span=policy['max_excursion'][axis]
        low,high=max(a.low,origin-span),min(a.high,origin+span)
        if a.resolution:
            low=math.ceil((low-1e-12)/a.resolution)*a.resolution
            high=math.floor((high+1e-12)/a.resolution)*a.resolution
        if not low-1e-12<=origin<=high+1e-12:raise ValueError('Checkpoint is not on the permitted output grid')
        bounds[axis]=[low,high]
    approved_bounds=copy.deepcopy(bounds)
    bounds=return_bounds(n,epoch,bounds)
    worst={a:max(bounds[a],key=lambda x:abs(x-checkpoint['outputs'][a])) for a in bounds}
    budget=steps(n,epoch,worst)
    if budget>r['handoff_timeout_s']:raise ValueError('Full physical return exceeds the admitted handoff budget')
    n.background.begin(r['expected_generation'],context,checkpoint,r['handoff_timeout_s'],r['identity'])
    n.background.epoch.update(family=family,parameters=p,candidate=target,phase='baseline',physical_bounds=bounds,
        approved_physical_bounds=approved_bounds,return_envelope_limited=bounds!=approved_bounds,
        return_period_s=period,return_bound_s=budget,expires_at=n.o.clock()+p['timeout_s'],runtime_only=True)
    n.discard_proposals('runtime parameter epoch start')
    n.o.audit('background_parameter_checkpoint',**n.background.epoch)
    return n.background.status()


def check_invariants(n,e,state):
    for key,value in e['checkpoint']['native'].items():
        if not math.isclose(state['servo_config'][key],value,rel_tol=1e-8,abs_tol=1e-12):
            raise ValueError('Native PI/target configuration changed during runtime trial: '+key)
    for axis,(low,high) in e['physical_bounds'].items():
        if not low-1e-12<=state['outputs'][axis]<=high+1e-12:
            raise ValueError('Physical runtime trial escaped its returnable envelope: '+axis)


def finish(n,e,context,state,adopted):
    if not adopted and values(n)!=e['checkpoint']['parameters']:raise ValueError('Runtime checkpoint parameters did not restore')
    n.planner.rebase_ff(state['outputs']);n.discard_proposals('runtime trial completed')
    receipt=dict(confirmed=True,response_ended=True,native_pi_active=n.active,
        actual_parameters=values(n),outputs={a:state['outputs'][a] for a in ('current','temperature')},
        runtime_only=True,thermal_equilibrium_verified=False,modified_axes=['current','temperature'],
        modified_settings=sorted(e['parameters']['candidate']),native_configuration_unchanged=True)
    n.background.complete(e['token'],context,receipt,adopted=adopted)
    n.background.release([r for r in n.background.reservations if r.startswith('direct:')])
    n.o.audit('background_parameter_completed',**n.background.last)
    return n.background.status()


def restore(n,e,context,state):
    if not e.get('parameters_restored'):
        install(n,e['checkpoint']['parameters'],state);e['parameters_restored']=True
    for axis in ('current','temperature'):
        p=n.o.policies()[axis];value=e['checkpoint']['outputs'][axis]
        if abs(value-state['outputs'][axis])>max(p.resolution/2,1e-12):
            proposal=dict(axis=axis,value=value,reason='runtime trial checkpoint return')
            n.proposed=[proposal]
            n._parameter_returning=True
            try:n.adjust(dict(proposal=proposal))
            finally:n._parameter_returning=False
            return n.background.status()
    return finish(n,e,context,state,False)


def tick(n,state,obs):
    if not active(n):return
    bg=n.background;e=bg.epoch
    try:
        context=n.background_context(obs)
        check_invariants(n,e,state)
        if e['state']=='active':
            try:grant(n,e)
            except (ValueError,KeyError,TypeError):bg.returning('grant_revoked')
            if n.o.clock()>=e['expires_at']:bg.returning('experiment_timeout')
        bg.validate(e['token'],context,restoring=e['state']=='returning')
        if e['state']=='returning':restore(n,e,context,state)
    except MeasurementUnavailable:
        n.discard_proposals('runtime restoration awaiting fresh measurement')
    except Exception as error:
        bg.block(str(error));n.discard_proposals('runtime epoch requires reconciliation')
        # Unknown writes already quarantine the physical owner. Never replay.
        if n.o.fault:raise


def dispatch(n,r,context,state,obs):
    bg=n.background;e=bg.epoch
    if r['action']=='background_begin':return begin(n,r,context,state,obs)
    e=bg.validate(r['token'],context,restoring=r['action']=='background_restore')
    check_invariants(n,e,state)
    if r['action']=='background_restore':return restore(n,bg.epoch,context,state)
    grant(n,e)
    if not adjustment_ready(obs,n.p,n.current_target):
        raise MeasurementUnavailable('Fresh eligible optical quality required before candidate/adoption')
    if r['action']=='background_parameters_target':
        if e['phase']!='baseline':raise ValueError('Runtime candidate is already applied; inspect without replay')
        install(n,e['candidate'],state);bg.epoch['phase']='candidate'
        n.o.audit('background_parameters_applied',token=e['token'],parameters=values(n))
        return bg.status()
    if r['action']=='background_adopt':
        if e['phase']!='candidate' or values(n)!=e['candidate']:raise ValueError('No verified candidate to adopt')
        return finish(n,e,context,state,True)
    raise ValueError('Unsupported runtime adjustment operation')


def permit_proposal(n,axis,value):
    if not active(n):return True
    e=n.background.epoch
    if e['state']!='active':
        # Only the dedicated restoration caller may generate output proposals.
        return bool(getattr(n,'_parameter_returning',False) and value==e['checkpoint']['outputs'][axis])
    grant(n,e)
    low,high=e['physical_bounds'][axis]
    if not low<=value<=high:return False
    outputs={**e['checkpoint']['outputs'],axis:value}
    # The other axis may already be at the furthest allowed point.
    for other in ('current','temperature'):
        if other!=axis:outputs[other]=max(e['physical_bounds'][other],key=lambda v:abs(v-e['checkpoint']['outputs'][other]))
    return steps(n,e,outputs)<=e['timeout_s']


def permit_output(n,axis,value):
    if not active(n):return True
    e=n.background.epoch
    low,high=e['physical_bounds'][axis]
    if not low-1e-12<=value<=high+1e-12:return False
    if e['state']!='active':
        return bool(getattr(n,'_parameter_returning',False) and e['state']=='returning')
    grant(n,e)
    return True
